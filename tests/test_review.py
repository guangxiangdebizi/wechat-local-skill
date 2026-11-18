import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from wechat_local import review
from wechat_local.cards import file_state, summary
from wechat_local.cli import parser
from wechat_local.core import AccessError


TABLE = "Msg_" + "a" * 32


class Fixture:
    account = "fictional-account"
    wxid = "fictional-self"

    def __init__(self, root):
        self.account_dir = self.workdir = root
        self.paths = [Path(root) / name for name in ("a.db", "b.db")]
        for path in self.paths:
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
                conn.execute("INSERT INTO Name2Id VALUES (?)", (self.wxid,))
                conn.execute("INSERT INTO Name2Id VALUES ('fictional-peer')")
                conn.execute("CREATE TABLE " + TABLE + "(local_id INTEGER PRIMARY KEY, local_type, "
                             "real_sender_id, create_time, message_content, compress_content, sort_seq, source)")

    def insert(self, shard, messages):
        with closing(sqlite3.connect(self.paths[shard])) as conn, conn:
            conn.executemany("INSERT INTO " + TABLE + " VALUES(?,1,?,?,?,NULL,7,NULL)", messages)

    def _msg_conns(self, user):
        result = []
        for path in self.paths:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            result.append((conn, TABLE))
        return result

    @staticmethod
    def _msg_row_to_dict(raw):
        return dict(local_id=raw["local_id"], create_time=raw["create_time"], type="文本",
                    sender_username=raw["_sender_username"], is_outgoing=raw["_is_outgoing"],
                    content=raw["message_content"], shard=raw["_shard"], sort_seq=raw["sort_seq"])

    @staticmethod
    def nickname_map():
        return {"fictional-peer": "Example"}


def args(*flags):
    return parser().parse_args(["changes", "--chat", "fictional-peer", *flags])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Fixture(self.tmp.name)
        self.user = "fictional-peer"

    def changes(self, *flags):
        return review.changes(self.db, self.user, args(*flags))

    def test_unequal_shards_same_seq_resume_and_backfill(self):
        for shard, n in [(0, 135), (1, 101)]:
            self.db.insert(shard, [(i, 2, 900+i, "example") for i in range(1, n+1)])
        page = self.changes("--limit", "200", "--checkpoint", "inbox")
        self.assertTrue(page["has_more"])
        self.assertEqual(page, self.changes("--limit", "200", "--checkpoint", "inbox"))
        self.changes("--checkpoint", "inbox", "--ack", page["next_cursor"])
        self.db.insert(1, [(102, 2, 10, "late arrival")])
        end = self.changes("--checkpoint", "inbox")
        self.assertFalse(end["has_more"])
        self.assertEqual(len({r["evidence_id"] for r in page["messages"] + end["messages"]}), 237)
        self.changes("--checkpoint", "inbox", "--ack", end["next_cursor"])
        self.assertTrue(self.changes("--checkpoint", "inbox", "--ack", end["next_cursor"])["acknowledged"])
        self.assertEqual(self.changes("--checkpoint", "inbox")["messages"], [])
        with self.assertRaises(AccessError):
            self.changes("--checkpoint", "inbox", "--ack", page["next_cursor"])
        with self.assertRaises(AccessError):
            self.changes("--checkpoint", "other", "--ack", end["next_cursor"])

    def test_start_now_and_scoped_resume(self):
        self.db.insert(0, [(1, 1, 20, "old")])
        init = self.changes("--start-now")
        self.assertEqual(init["messages"], [])
        self.db.insert(1, [(1, 2, 10, "new")])
        page = self.changes("--cursor", init["next_cursor"])
        self.assertEqual([r["content"] for r in page["messages"]], ["new"])
        self.user = "different-chat"
        with self.assertRaises(AccessError):
            self.changes("--cursor", init["next_cursor"])

    def test_context_sender_dates_and_redacted_export(self):
        self.db.insert(0, [(1, 2, 9, "omit"), (2, 2, 10, "before"),
                           (3, 2, 11, "needle 13300000000"), (4, 1, 12, "needle"),
                           (5, 2, 13, "not in range")])
        q = SimpleNamespace(query="Needle", sender="incoming", context=1, limit=1)
        with review.window(self.db, self.user, 10, 13) as rows:
            result = review.search(rows, q)
        self.assertFalse(result["truncated"])
        item, = result["hits"]
        self.assertEqual(item["before"][0]["content"], "before")
        self.assertEqual(item["after"][0]["content"], "needle")
        path = Path(self.tmp.name) / "example.jsonl"
        with review.window(self.db, self.user, 11, 12) as rows:
            review.export(rows, path, "jsonl")
        saved = json.loads(path.read_text(encoding="utf8"))
        self.assertEqual(saved["content"], "needle [number]")
        self.assertEqual(item["match"]["evidence_id"], saved["evidence_id"])
        with self.assertRaises(FileExistsError):
            review.export(iter([]), path, "markdown")

    def test_card_allowlist_and_local_file_boundary(self):
        app = "<msg><appmsg><type>6</type><title>example.xlsx</title><aeskey>secret</aeskey></appmsg></msg>"
        card = summary(app)
        self.assertEqual(card, {"kind": "file", "title": "example.xlsx"})
        self.assertEqual(file_state(card, self.tmp.name, 1700000000), "not_found_in_month")
        self.assertEqual(file_state(dict(title="../other"), self.tmp.name, 1700000000), "unknown")
        self.assertEqual(summary("<!DOCTYPE x>" + app), {})
        self.assertEqual(summary(app.replace("example.xlsx", "https://example.invalid/private"))["title"], "")
        quote = app.replace("<type>6", "<type>57").replace("<aeskey>secret</aeskey>",
                    "<refermsg><type>1</type><content>original</content></refermsg>")
        self.assertEqual(summary(quote)["quoted_text"], "original")
        self.assertNotIn("quoted_text", summary(quote.replace("<type>1<", "<type>49<")))

    def test_mentions_require_metadata_and_handle_unknown(self):
        self.db.insert(0, [(1, 2, 9, "@Example"), (2, 2, 10, "ping")])
        with closing(sqlite3.connect(self.db.paths[0])) as conn, conn:
            conn.execute("UPDATE " + TABLE + " SET source=? WHERE local_id=2",
                         ("<msgsource><atuserlist>other,fictional-self</atuserlist></msgsource>",))
        with review.window(self.db, "fictional@chatroom", None, None) as rows:
            first, second = rows
        self.assertIsNone(first["mentions_me"])
        self.assertTrue(second["mentions_me"])
        self.assertEqual(second["sender_display"], "Example")

    def test_failed_export_removes_partial_owned_file(self):
        out = Path(self.tmp.name) / "review.md"
        def broken():
            yield {"create_time": 2, "type": "text", "content": "hello",
                   "evidence_id": "example", "is_outgoing": False}
            raise RuntimeError("read failed")
        with self.assertRaises(RuntimeError):
            review.export(broken(), out, "markdown")
        self.assertFalse(out.exists())
