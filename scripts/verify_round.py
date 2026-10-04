"""Capability-by-capability checks; preflight is never labeled a live write test."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def check(argv, base):
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "wechat_local", *base, *argv],
                            capture_output=True, text=True, encoding="utf-8", timeout=180)
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "non_json_command_result"}
    return parsed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account")
    p.add_argument("--db-dir")
    p.add_argument("--runtime", type=Path, help="Optional installed skill runtime.json")
    p.add_argument("--require-writes", action="store_true")
    args = p.parse_args()
    config = json.loads(args.runtime.read_text(encoding="utf-8")) if args.runtime else {}
    base = []
    for flag, value in (("--account", args.account or config.get("account")),
                        ("--db-dir", args.db_dir or config.get("db_dir"))):
        if value:
            base += [flag, value]
    checked = {}
    for name, command in (
        ("status", ["status"]), ("database_integrity", ["doctor"]),
        ("contacts", ["contacts", "--query", "filehelper", "--limit", "3"]),
        ("sessions", ["sessions", "--limit", "3"]),
        ("history", ["history", "--chat", "filehelper", "--limit", "3"]),
        ("moments", ["moments", "--limit", "3"]),
    ):
        response = check(command, base)
        result = response.get("result", {})
        passed = response.get("ok", False)
        if name == "status":
            passed = passed and bool(result.get("processes"))
        if name == "database_integrity":
            passed = passed and result.get("read_checks_passed", False)
        if name == "moments":
            passed = passed and all("parse_error" not in item for item in result.get("moments", []))
        checked[name] = "pass" if passed else "fail"
    preview = check(["send", "--chat", "filehelper", "--text", "Codex interface test"], base)
    checked["send_preview"] = "pass" if (preview.get("ok") and preview["result"].get("sent") is False
                                               and preview["result"].get("dry_run") is True) else "fail"
    commit = check(["send", "--chat", "filehelper", "--text", "Codex interface test", "--commit"], base)
    checked["unvalidated_send_rejection"] = "pass" if (commit.get("ok") is False
        and "Native write ABI not validated" in commit.get("error", "")) else "fail"
    caps = check(["capabilities"], base).get("result", {}).get("interfaces", {})
    writes = {name: {"implementation": caps.get(name, "unknown"),
                     "live_write_test": "not_run_unresolved_interface"}
              for name in ("send_text", "moment_publish", "moment_like", "moment_comment")}
    passed = all(value == "pass" for value in checked.values())
    print(json.dumps({"read_and_guard_checks": checked, "read_and_guard_checks_passed": passed,
                      "writes": writes, "full_read_write_passed": False,
                      "writes_attempted": "preflight_only; no outbound message/post/reaction",
                      "privacy": "No account identifiers or record bodies included"}))
    return 2 if not passed or args.require_writes else 0


if __name__ == "__main__":
    raise SystemExit(main())
