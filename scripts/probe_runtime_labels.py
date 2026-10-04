"""Read-only exact-string localization in a Weixin process; no dump or injection."""
import argparse
import ctypes
import json
import re

import psutil

from wechat_local.vendor.replica_db import WeChatDB, _k32


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    if psutil.Process(args.pid).name().lower() != "weixin.exe":
        raise SystemExit("Target is not Weixin.exe")
    handle = _k32.OpenProcess(0x0010 | 0x0400, False, args.pid)
    if not handle:
        raise SystemExit("Read-only process handle unavailable")
    try:
        def read(address, size):
            buffer = ctypes.create_string_buffer(size)
            obtained = ctypes.c_size_t()
            if _k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(obtained)):
                return buffer.raw[:obtained.value]
            return None

        output = []
        for encoding in ("utf-8", "utf-16-le"):
            hits = WeChatDB._find_bytes(handle, read, "invalid apiname".encode(encoding))
            labels = set()
            for address in hits[:8]:
                data = read(max(0, address - 1024), 3072) or b""
                strings = re.findall(r"[ -~]{4,100}", data.decode(encoding, errors="replace"))
                for text in strings:
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{3,63}", text) and re.match(
                            r"(?:get|send|sns|api|open|share|login|contact|publish|check|status|request|version|Local)",
                            text, re.I):
                        labels.add(text)
            output.append({"encoding": encoding, "exact_marker_hits": len(hits),
                           "nearby_candidate_labels": sorted(labels)[:50]})
        print(json.dumps({"results": output, "raw_memory_saved": False,
                          "process_memory_writes": False, "messages_sent": 0}))
    finally:
        _k32.CloseHandle(handle)


if __name__ == "__main__":
    main()
