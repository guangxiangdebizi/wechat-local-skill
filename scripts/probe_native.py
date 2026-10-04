"""Static PE anchors, never a proof of a callable native write ABI."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
from pathlib import Path
import re
import struct

import pefile


TOKENS = (b"NetSceneSendMsg", b"newsendmsg", b"SendTextMessage", b"SendText",
          b"SendMsg", b"SnsPost", b"SnsUpload", b"SnsComment", b"snscomment",
          b"mmsnspost", b"mmsnscomment", b"StartTask", b"Begin StartSendMessageSyncStage",
          b"GetAddSendMessageToDb", b"SaveSendMessagesAtOnce", b"TextMessageHandler",
          b"TextMessageSendSource", b"OnSendMessage", b"SendMessageTo",
          b"invalid apiname", b"apiname", b"-11028")


def probe(path: Path):
    raw = path.read_bytes()
    pe = pefile.PE(data=raw, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
                                         pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXCEPTION"]])
    sections = [{"name": s.Name.rstrip(b"\0").decode(), "rva": s.VirtualAddress,
                 "raw": s.PointerToRawData, "size": s.SizeOfRawData,
                 "executable": bool(s.Characteristics & 0x20000000)} for s in pe.sections]
    exports = [symbol.name.decode(errors="replace") for symbol in
               getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []) if symbol.name]
    functions = sorted((item.struct.BeginAddress, item.struct.EndAddress) for item in
                       getattr(pe, "DIRECTORY_ENTRY_EXCEPTION", []))
    starts = [start for start, _ in functions]

    def offset_to_rva(offset):
        for section in sections:
            if section["raw"] <= offset < section["raw"] + section["size"]:
                return section["rva"] + offset - section["raw"]
        return None

    anchors = []
    for token in TOKENS:
        for match in list(re.finditer(re.escape(token), raw))[:20]:
            offset = match.start()
            start = offset
            while start > max(0, offset - 180) and 32 <= raw[start - 1] < 127:
                start -= 1
            end = offset
            while end < min(len(raw), offset + 240) and 32 <= raw[end] < 127:
                end += 1
            anchors.append({"token": token.decode(), "string": raw[start:end].decode(errors="replace"),
                            "rva": offset_to_rva(start), "token_rva": offset_to_rva(offset), "xrefs": []})
    addresses = {}
    for anchor in anchors:
        for key in ("rva", "token_rva"):
            if anchor[key] is not None:
                addresses.setdefault(anchor[key], []).append(anchor)
    for section in sections:
        if not section["executable"]:
            continue
        data = raw[section["raw"]:section["raw"] + section["size"]]
        for match in re.finditer(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....", data, re.S):
            rva = section["rva"] + match.start()
            target = rva + 7 + struct.unpack_from("<i", match.group(), 3)[0]
            if target not in addresses:
                continue
            index = bisect.bisect_right(starts, rva) - 1
            function = functions[index][0] if index >= 0 and rva < functions[index][1] else None
            for anchor in addresses[target]:
                if len(anchor["xrefs"]) < 12:
                    anchor["xrefs"].append({"rva": rva, "function_rva": function})
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
            "machine": hex(pe.FILE_HEADER.Machine), "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
            "exports": exports, "sections": sections, "anchors": anchors,
            "native_write_validated": False,
            "warning": "Static anchors are research candidates, NOT callable functions or verified ABI."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.dll)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"sha256": result["sha256"], "size": result["size"], "export_count": len(result["exports"]),
                      "relevant_exports": [name for name in result["exports"] if name in
                                           ("WeChatMain", "SetWeixinCallbackFunc")],
                      "anchor_counts": {token.decode(): sum(a["token"] == token.decode() for a in result["anchors"])
                                        for token in TOKENS},
                      "anchored_functions": len({x["function_rva"] for a in result["anchors"] for x in a["xrefs"]}),
                      "native_write_validated": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
