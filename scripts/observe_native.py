"""Temporary metadata-only native-call observation; never invokes a send API."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import threading
import time

import frida
import pefile
import psutil

from wechat_local.core import protect_directory


EXPECTED_HASH = "10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5"
UNSTABLE_HASHES = {EXPECTED_HASH}
OBSERVED_TOKENS = {"Begin StartSendMessageSyncStage", "SaveSendMessagesAtOnce", "SendMsg"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if psutil.Process(args.pid).name().lower() != "weixin.exe":
        raise SystemExit("Target is not a Weixin.exe process")
    data = args.dll.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest in UNSTABLE_HASHES:
        raise SystemExit("This build crashed after Frida inventory/detach. Live attachment is disabled; use offline analysis.")
    if digest != EXPECTED_HASH:
        raise SystemExit("Unrecognized Weixin.dll hash; observation disabled")
    candidates = []
    if args.observe:
        if not args.probe or not args.output:
            raise SystemExit("Observation requires --probe and a private --output")
        probe = json.loads(args.probe.read_text(encoding="utf-8"))
        if probe["sha256"] != digest:
            raise SystemExit("Static probe belongs to another binary")
        pe = pefile.PE(data=data, fast_load=True)
        pe.parse_data_directories(directories=[3])
        starts = {item.struct.BeginAddress for item in pe.DIRECTORY_ENTRY_EXCEPTION}
        seen = set()
        for anchor in probe["anchors"]:
            wanted = anchor["token"] in OBSERVED_TOKENS or anchor["string"] in (
                "mars::stn::MMStartTask", "mars::stn::StnManager::StartTask")
            if not wanted:
                continue
            for xref in anchor["xrefs"]:
                rva = xref["function_rva"]
                if rva is not None and rva in starts and rva not in seen:
                    seen.add(rva)
                    candidates.append({"label": anchor["string"], "rva": rva,
                                       "prologue": pe.get_data(rva, 16).hex()})
        if not candidates or len(candidates) > 8:
            raise SystemExit("Unexpected observation candidate set")
        protect_directory(args.output.parent)
    source = """
const module = Process.getModuleByName('Weixin.dll');
const candidates = CANDIDATES;
let count = 0;
function frame(address) {
  const owner = Process.findModuleByAddress(address);
  return owner === null ? {module: null} : {module: owner.name, rva: address.sub(owner.base).toString()};
}
function kind(address) {
  if (address.isNull()) return {kind: 'null'};
  const range = Process.findRangeByAddress(address);
  if (range === null || !range.protection.includes('r')) return {kind: 'scalar_or_unreadable'};
  try {
    const first = address.readPointer();
    if (first.compare(module.base) >= 0 && first.compare(module.base.add(module.size)) < 0) {
      return {kind: 'module_vtable_candidate', first_word_rva: first.sub(module.base).toString()};
    }
    return {kind: 'readable_pointer'};
  } catch (_) { return {kind: 'unreadable'}; }
}
for (const candidate of candidates) {
  const prologue = Array.from(new Uint8Array(module.base.add(candidate.rva).readByteArray(16)))
    .map(b => b.toString(16).padStart(2, '0')).join('');
  if (prologue !== candidate.prologue) throw new Error('Live prologue differs from profiled binary');
}
for (const candidate of candidates) {
  Interceptor.attach(module.base.add(candidate.rva), {
    onEnter(args) {
      if (count >= 200) return;
      count++;
      send({event: 'call', index: count, label: candidate.label, rva: candidate.rva,
        thread: Process.getCurrentThreadId(),
        arguments: [0, 1, 2, 3].map(i => ({pointer: args[i].toString(), classification: kind(args[i])})),
        stack: Thread.backtrace(this.context, Backtracer.ACCURATE).slice(0, 14).map(frame)});
    }
  });
}
send({event: 'ready', pid: Process.id, arch: Process.arch, module: module.name,
      module_size: module.size, hooks: candidates.length,
      frida: Frida.version, sends: 0});
""".replace("CANDIDATES", json.dumps(candidates))
    closed = threading.Event()
    count = 0
    session = None
    script = None
    output = None
    try:
        if args.output:
            protect_directory(args.output.parent)
            output = args.output.open("x", encoding="utf-8")
        session = frida.get_local_device().attach(args.pid)
        session.on("detached", lambda *event: closed.set())
        script = session.create_script(source)

        def on_message(message, blob):
            nonlocal count
            if message["type"] == "send":
                payload = message["payload"]
                if output:
                    output.write(json.dumps(payload) + "\n")
                    output.flush()
                if payload.get("event") == "call":
                    count += 1
                elif payload.get("event") == "ready":
                    print(json.dumps(payload), flush=True)
            else:
                print(json.dumps({"error": message.get("description", message["type"])}), flush=True)
                closed.set()

        script.on("message", on_message)
        script.load()
        if args.observe:
            closed.wait(min(max(args.seconds, 1), 600))
        print(json.dumps({"observed_calls": count, "messages_sent": 0, "finished": True}), flush=True)
    finally:
        if script:
            try:
                script.unload()
            except frida.InvalidOperationError:
                pass
        if session:
            try:
                session.detach()
            except frida.InvalidOperationError:
                pass
        if output:
            output.close()


if __name__ == "__main__":
    main()
