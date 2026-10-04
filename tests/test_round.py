import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("verify_round", Path(__file__).resolve().parents[1] / "scripts" / "verify_round.py")
round_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(round_module)


class RoundTests(unittest.TestCase):
    @staticmethod
    def response(command, base, sent=False):
        if command[0] == "send":
            if "--commit" in command:
                return {"ok": False, "error": "Native write ABI not validated for this client"}
            return {"ok": True, "result": {"dry_run": True, "sent": sent}}
        if command[0] == "status":
            return {"ok": True, "result": {"processes": [{"name": "fixture"}]}}
        if command[0] == "doctor":
            return {"ok": True, "result": {"read_checks_passed": True}}
        if command[0] == "moments":
            return {"ok": True, "result": {"moments": [{"text": "fixture"}]}}
        if command[0] == "capabilities":
            return {"ok": True, "result": {"interfaces": {"send_text": "native_profile_unresolved"}}}
        return {"ok": True, "result": {}}

    def run_round(self, arguments, response=None):
        out = io.StringIO()
        with patch("sys.argv", ["verify_round.py", "--account", "synthetic", *arguments]), \
             patch.object(round_module, "check", side_effect=response or self.response), \
             contextlib.redirect_stdout(out):
            code = round_module.main()
        return code, json.loads(out.getvalue())

    def test_passing_read_checks_do_not_satisfy_write_requirement(self):
        code, report = self.run_round(["--require-writes"])
        self.assertEqual(code, 2)
        self.assertTrue(report["read_and_guard_checks_passed"])
        self.assertFalse(report["full_read_write_passed"])
        self.assertNotIn("synthetic", json.dumps(report))

    def test_preview_with_unexpected_send_fails_verification(self):
        code, report = self.run_round([], lambda command, base: self.response(command, base, sent=True))
        self.assertEqual(code, 2)
        self.assertFalse(report["read_and_guard_checks_passed"])
        self.assertEqual(report["read_and_guard_checks"]["send_preview"], "fail")


if __name__ == "__main__":
    unittest.main()
