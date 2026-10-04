from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys

from . import __version__
from .core import (AccessError, account_list, contacts, database_for, freshness,
                   moments, open_db, public_message, resolve_chat, running_clients, state_lock)


def capabilities():
    return {"version": __version__, "backend": "readonly_process_memory_plus_local_wcdb",
            "gui_automation": False, "process_memory_writes": False,
            "interfaces": {"status": "implemented", "accounts": "implemented",
                           "contacts": "implemented", "sessions": "implemented",
                           "history": "implemented", "moments": "implemented",
                           "send_text": "native_profile_unresolved",
                           "moment_publish": "native_profile_unresolved",
                           "moment_like": "native_profile_unresolved",
                           "moment_comment": "native_profile_unresolved"},
            "data_scope": "locally_cached_data_only; not all server-side history"}


def bounded(value):
    n = int(value)
    if not 1 <= n <= 200:
        raise argparse.ArgumentTypeError("limit must be between 1 and 200")
    return n


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("offset cannot be negative")
    return n


def parser():
    p = argparse.ArgumentParser(description="微信本地数据接口：默认只读，不使用 Computer Use")
    p.add_argument("--db-dir")
    p.add_argument("--account")
    subs = p.add_subparsers(dest="command", required=True)
    for name in ("capabilities", "status", "accounts", "doctor"):
        subs.add_parser(name)
    s = subs.add_parser("sessions")
    s.add_argument("--limit", type=bounded, default=20)
    c = subs.add_parser("contacts")
    c.add_argument("--query", default="")
    c.add_argument("--limit", type=bounded, default=50)
    h = subs.add_parser("history")
    h.add_argument("--chat", required=True)
    h.add_argument("--limit", type=bounded, default=20)
    h.add_argument("--offset", type=nonnegative, default=0)
    m = subs.add_parser("moments")
    m.add_argument("--author", help="精确的内部用户名、微信号、昵称或备注")
    m.add_argument("--limit", type=bounded, default=20)
    m.add_argument("--offset", type=nonnegative, default=0)
    s = subs.add_parser("send")
    s.add_argument("--chat", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--commit", action="store_true")
    return p


def execute(args):
    if args.command == "capabilities":
        return capabilities()
    if os.name != "nt":
        raise AccessError("This runtime requires Windows x64")
    if args.command == "status":
        return {"processes": running_clients(), "capabilities": capabilities()}
    if args.command == "accounts":
        root, accounts = account_list(args.db_dir)
        return {"db_dir": root, "accounts": accounts}
    if args.command == "send" and args.commit:
        raise AccessError("Native write ABI not validated for this client; no message was sent. No GUI fallback.")
    with state_lock():
        db = open_db(args.db_dir, args.account)
        if args.command == "doctor":
            checks = {}
            for basename, sql in (
                ("contact.db", "SELECT count(*) FROM contact"),
                ("session.db", "SELECT count(*) FROM SessionTable"),
                ("sns.db", "SELECT count(*) FROM SnsTimeLine"),
            ):
                try:
                    with contextlib.closing(database_for(db, basename)) as conn:
                        count = conn.execute(sql).fetchone()[0]
                        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
                    checks[basename] = {"rows": count, "quick_check": integrity}
                except Exception as exc:
                    checks[basename] = {"error": str(exc)}
            try:
                sample = db.get_messages("filehelper", limit=3)
                checks["history"] = {"chat": "filehelper", "sample_count": len(sample),
                                     "text_count": sum(row["type"] == "文本" for row in sample)}
            except Exception as exc:
                checks["history"] = {"error": str(exc)}
            result = {"checks": checks, "selected_databases": len(db._db_files),
                      "validated_keys": len(db._keys), "unkeyed": db.unkeyed,
                      "key_cache": "DPAPI; plaintext databases protected by per-user DACL",
                      "read_checks_passed": all("error" not in check and check.get("quick_check", "ok") == "ok"
                                                for check in checks.values())}
        elif args.command == "contacts":
            result = {"contacts": contacts(db, args.query, args.limit)}
        elif args.command == "sessions":
            rows = db.get_sessions(args.limit)
            for row in rows:
                row["display_name"] = db.get_nickname(row["username"])
            result = {"sessions": rows}
        elif args.command == "history":
            target = resolve_chat(db, args.chat)
            result = {"chat": target, "messages": [public_message(row) for row in
                       db.get_messages(target["username"], args.limit, args.offset)]}
        elif args.command == "moments":
            author = resolve_chat(db, args.author)["username"] if args.author else None
            result = {"moments": moments(db, args.limit, args.offset, author)}
        elif args.command == "send":
            target = resolve_chat(db, args.chat)
            result = {"dry_run": True, "sent": False, "chat": target,
                      "text": args.text, "text_sha256": hashlib.sha256(args.text.encode()).hexdigest(),
                      "blocked_by": "native_write_profile_unresolved"}
        else:
            raise AccessError("Unsupported command")
        result["freshness"] = freshness(db)
        return result


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = execute(parser().parse_args(argv))
        output = {"ok": True, "result": result}
        status = 0
        if isinstance(result.get("read_checks_passed"), bool) and not result["read_checks_passed"]:
            output["ok"], status = False, 2
    except (AccessError, RuntimeError, OSError) as exc:
        output = {"ok": False, "error": str(exc)}
        status = 2
    print(json.dumps(output, ensure_ascii=False, default=str))
    return status


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
