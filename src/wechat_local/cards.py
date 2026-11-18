"""Allowlisted summaries; never return a media payload or fetch its URL."""
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


def xml_root(text):
    if not isinstance(text, str) or len(text) > 500_000:
        return None
    text = text.partition(":\n")[2] if re.match(r"^[^<>\s:]{1,128}:\n", text) else text
    if not text.lstrip().startswith("<") or re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        return None
    try:
        return ET.fromstring(text)
    except (ValueError, ET.ParseError):
        return None


def label(s):
    if not s or any(c in s for c in "<>") or re.search(r"(?:https?://|aeskey=|cdnattachurl=)", s, re.I):
        return ""
    return " ".join(s.split())[:500]


def summary(body):
    root = xml_root(body)
    if root is None:
        return {}
    app = root if root.tag == "appmsg" else root.find("appmsg")
    if app is None:
        return {}
    kind = {"5": "link", "6": "file", "57": "quote"}.get(app.findtext("type", "").strip())
    if not kind:
        return {}
    result = {"kind": kind, "title": label(app.findtext("title"))}
    if kind == "quote" and app.findtext("refermsg/type") == "1":
        result.update(quoted_text=label(app.findtext("refermsg/content")),
                      quoted_author=label(app.findtext("refermsg/displayname")))
    return result


def file_state(card, account_dir, ts):
    name = card.get("title", "")
    if not name or len(name) > 240 or name in (".", "..") or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        return "unknown"
    try:
        root = Path(account_dir).resolve()
        month = datetime.fromtimestamp(ts).strftime("%Y-%m")
        path = (root / "msg" / "file" / month / name).resolve()
        reserved = os.path.isreserved(path) if hasattr(os.path, "isreserved") else path.is_reserved()
        if not path.is_relative_to(root) or reserved:
            return "unknown"
        return "same_name_in_month" if path.is_file() else "not_found_in_month"
    except (OSError, OverflowError, ValueError):
        return "unknown"
