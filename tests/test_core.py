import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_local.cli import execute, parser
from wechat_local.core import (AccessError, contacts, exact_candidate, freshness,
                               parse_moment, public_message, resolve_chat, select_account)


class FakeDB:
    def __init__(self, path):
        self._db_files = [("contact/contact.db", str(path), 0)]
        self.path = path

    def _open(self, relative):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


class CoreTests(unittest.TestCase):
    def test_multiple_accounts_require_selection(self):
        accounts = [{"account": "a"}, {"account": "b"}]
        with self.assertRaises(AccessError):
            select_account(accounts, None)
        self.assertEqual(select_account(accounts, "b"), "b")
        with self.assertRaises(AccessError):
            select_account(accounts, "not-listed")

    def test_exact_resolution_does_not_pick_first_fuzzy_hit(self):
        candidates = [{"username": "one", "nick_name": "alice"},
                      {"username": "two", "nick_name": "alice2"}]
        self.assertEqual(exact_candidate(candidates, "alice")["username"], "one")
        with self.assertRaises(AccessError):
            exact_candidate(candidates, "ali")
        candidates[1]["nick_name"] = "alice"
        with self.assertRaises(AccessError):
            exact_candidate(candidates, "alice")

    def test_contacts_literal_wildcards_and_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contact.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE contact (username TEXT, alias TEXT, nick_name TEXT, remark TEXT)")
            conn.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
                ("one", "RAK_fixture", "first", "woman-emoji-fixture"),
                ("two", "other", "second", "100%"),
                ("three", "RAKXfixture", "third", "other"),
            ])
            conn.commit()
            conn.close()
            db = FakeDB(path)
            self.assertEqual([item["username"] for item in contacts(db, "%")], ["two"])
            self.assertEqual([item["username"] for item in contacts(db, "_")], ["one"])
            self.assertEqual(resolve_chat(db, "RAK_fixture")["username"], "one")
            with self.assertRaises(AccessError):
                resolve_chat(db, "RAK")

    def test_moments_real_container_shape_and_cdata(self):
        xml = """<SnsDataItem><TimelineObject><id>123</id><username>user</username>
          <createTime>1700000000</createTime><contentDesc><![CDATA[hello < world]]></contentDesc>
          <ContentObject><mediaList><media><id>asset</id><url>signed-secret-url</url>
          <size width="12" height="34"/></media></mediaList></ContentObject>
          </TimelineObject><LocalExtraInfo><comment_user_list>
          <user_comment><username>a</username><content>keep</content><comment_id>1</comment_id></user_comment>
          <user_comment><username>b</username><content>deleted</content><b_deleted>1</b_deleted></user_comment>
          </comment_user_list></LocalExtraInfo></SnsDataItem>"""
        moment = parse_moment(xml, "fallback", 42)
        self.assertEqual(moment["id"], "123")
        self.assertEqual(moment["tid"], 42)
        self.assertEqual(moment["text"], "hello < world")
        self.assertEqual(moment["create_time"], 1700000000)
        self.assertEqual(len(moment["comments"]), 1)
        self.assertNotIn("signed-secret-url", json.dumps(moment))

    def test_untrusted_xml_rejected(self):
        with self.assertRaises(AccessError):
            parse_moment('<!DOCTYPE x [<!ENTITY x "x">]><x/>', "", 1)

    def test_media_credentials_not_returned(self):
        row = {"type": "文件/链接/卡片", "content": '<msg aeskey="secret" />'}
        self.assertNotIn("secret", json.dumps(public_message(row)))
        self.assertEqual(public_message({"type": "文本", "content": "hello"})["content"], "hello")

    def test_commit_fails_before_memory_or_database_access(self):
        args = parser().parse_args(["send", "--chat", "filehelper", "--text", "test", "--commit"])
        with patch("wechat_local.cli.os.name", "nt"), patch("wechat_local.cli.open_db") as db:
            with self.assertRaises(AccessError):
                execute(args)
            db.assert_not_called()

    def test_invalid_pagination_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for tail in (["--limit", "0"], ["--offset", "-1"]):
                with self.assertRaises(SystemExit):
                    parser().parse_args(["history", "--chat", "filehelper"] + tail)

    def test_stale_wal_snapshot_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "session.db.stamp").write_text("3,1,1,1,1,-1")
            db = type("DB", (), {"workdir": tmp})()
            self.assertEqual(freshness(db)["wal_merge_failed"], ["session.db"])

    def test_no_gui_runtime_is_imported(self):
        forbidden = ("uiautomation", "pyautogui", "wechatauto", "winsdk")
        self.assertFalse(any(name.split(".")[0] in forbidden for name in sys.modules))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI/DACL integration")
    def test_windows_private_directory_and_dpapi(self):
        import win32crypt
        import win32security
        from wechat_local.core import protect_directory

        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "private"
            protect_directory(private)
            descriptor = win32security.GetNamedSecurityInfo(
                str(private), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
            self.assertTrue(descriptor.GetSecurityDescriptorControl()[0] & 0x1000)
            self.assertEqual(descriptor.GetSecurityDescriptorDacl().GetAceCount(), 2)
            fixture = b"synthetic-fixture-not-a-real-key"
            sealed = win32crypt.CryptProtectData(fixture, "unit-test", None, None, None, 1)
            self.assertNotIn(fixture, sealed)
            self.assertEqual(win32crypt.CryptUnprotectData(sealed, None, None, None, 1)[1], fixture)


if __name__ == "__main__":
    unittest.main()
