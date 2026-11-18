"""Single-chat review over existing read-only snapshots."""
import base64
import hashlib
import heapq
import json
import re
from collections import deque
from contextlib import ExitStack, contextmanager
from datetime import datetime
from itertools import islice
from pathlib import Path

from .cards import file_state, xml_root
from .core import AccessError, annotate_sender_rows, public_message


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def cursor(scope, positions, previous):
    value = [1, scope, positions, digest(previous)]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode(token, scope):
    try:
        if len(token) > 32768:
            raise ValueError
        version, owner, positions, base = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if (version != 1 or owner != scope or not isinstance(positions, dict)
                or not all(isinstance(k, str) and type(v) is int and 0 <= v < 2**63
                           for k, v in positions.items()) or not isinstance(base, str)):
            raise ValueError
        return positions, base
    except (ValueError, TypeError, RecursionError) as exc:
        raise AccessError("Invalid cursor or different account/chat/checkpoint") from exc


@contextmanager
def tables(db, user):
    with ExitStack() as stack:
        result = []
        for conn, table in db._msg_conns(user):
            stack.callback(conn.close)
            if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                raise AccessError("Unexpected message table")
            filename = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
            result.append((conn, table, Path(filename).name))
        if len({s for _, _, s in result}) != len(result):
            raise AccessError("Ambiguous shard identity")
        yield result


def shard_stream(db, user, entry, conditions, params, order, limit=None):
    conn, table, shard = entry
    sql = ("SELECT local_id, local_type, real_sender_id, create_time, message_content, "
           "compress_content, sort_seq, source FROM " + table + " WHERE " + conditions + " ORDER BY " + order)
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    cur = conn.execute(sql, params)
    while batch := cur.fetchmany(64):
        for raw in annotate_sender_rows(conn, batch, db.wxid, shard):
            row = public_message(db._msg_row_to_dict(raw))
            row["evidence_id"] = digest([db.account, user, shard, row["local_id"]])
            if row.get("card", {}).get("kind") == "file":
                row["card"]["local_file"] = file_state(row["card"], db.account_dir, row["create_time"])
            if user.endswith("@chatroom"):
                row["sender_display"] = db.nickname_map().get(row["sender_username"]) or None
                src = raw["source"]
                root = xml_root(src.decode("utf8", "replace") if isinstance(src, bytes) else src)
                names = root.find(".//atuserlist") if root is not None else None
                row["mentions_me"] = (db.wxid in re.split(r"[,;\s]+", names.text or "")
                                      if names is not None else None)
            yield row


def changes(db, user, args):
    name = args.checkpoint
    scope = digest([db.account, user, name])
    positions = {}
    path = Path(db.workdir) / ("review-" + scope + ".json")
    if name and path.exists():
        positions = decode(path.read_text(encoding="ascii"), scope)[0]
    if args.ack:
        if not name or args.cursor or args.start_now:
            raise AccessError("--ack needs --checkpoint alone")
        new, base = decode(args.ack, scope)
        if positions != new and (base != digest(positions) or any(new.get(k, -1) < v for k, v in positions.items())):
            raise AccessError("Checkpoint changed; read again before acknowledging")
        temp = path.with_suffix(".tmp")
        temp.write_text(args.ack, encoding="ascii")
        temp.replace(path)
        return {"acknowledged": True}
    if args.cursor:
        if name or args.start_now:
            raise AccessError("--cursor cannot be combined with --checkpoint or --start-now")
        positions = decode(args.cursor, scope)[0]
    if args.start_now and positions:
        raise AccessError("Cannot reinitialize an existing checkpoint")
    next_pos = dict(positions)
    with tables(db, user) as entries:
        if positions.keys() - {s for _, _, s in entries}:
            raise AccessError("Previously seen shard missing; checkpoint not advanced")
        for conn, table, shard in entries:
            high = conn.execute("SELECT MAX(local_id) FROM " + table).fetchone()[0] or 0
            if high < positions.get(shard, 0):
                raise AccessError("Shard appears reset; use a new checkpoint")
            if args.start_now:
                next_pos[shard] = high
        streams = [] if args.start_now else [
            shard_stream(db, user, entry, "local_id > ?", (positions.get(entry[2], 0),),
                         "local_id", args.limit + 1) for entry in entries]
        rows = list(islice(heapq.merge(*streams, key=lambda r: (r["local_id"], r["shard"])), args.limit + 1))
    for row in rows[:args.limit]:
        next_pos[row["shard"]] = row["local_id"]
    return {"messages": rows[:args.limit], "has_more": len(rows) > args.limit,
            "next_cursor": cursor(scope, next_pos, positions)}


@contextmanager
def window(db, user, start, end):
    if start is not None and end is not None and start >= end:
        raise AccessError("Expected --since before --until (exclusive)")
    where, params = ["1"], []
    for op, value in ((">=", start), ("<", end)):
        if value is not None:
            where.append("create_time " + op + " ?")
            params.append(value)
    with tables(db, user) as entries:
        streams = [shard_stream(db, user, e, " AND ".join(where), params, "create_time, local_id")
                   for e in entries]
        yield heapq.merge(*streams, key=lambda r: (r["create_time"], r["shard"], r["local_id"]))


def search(rows, args):
    before = deque(maxlen=args.context)
    ahead = deque(islice(rows, args.context + 1))
    hits = []
    while ahead:
        row = ahead.popleft()
        card = row.get("card", {})
        strings = [row["content"], card.get("title", ""), card.get("quoted_text", "")]
        sender = args.sender
        by = (sender is None or (sender == "me" and row["is_outgoing"] is True)
              or (sender == "incoming" and row["is_outgoing"] is False) or row["sender_username"] == sender)
        if by and any(args.query.casefold() in s.casefold() for s in strings):
            if len(hits) == args.limit:
                return {"hits": hits, "truncated": True}
            hits.append({"match": row, "before": list(before), "after": list(ahead)})
        before.append(row)
        ahead.extend(islice(rows, 1))
    return {"hits": hits, "truncated": False}


PII = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15,19}|(?:\+?86[- ]?)?1[3-9]\d{9})(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def export(rows, path, fmt):
    def redact(value):
        return EMAIL.sub("[email]", PII.sub("[number]", value)) if isinstance(value, str) else value

    count = 0
    with Path(path).open("x", encoding="utf8") as out:
        try:
            for row in rows:
                safe = {k: redact(row[k]) for k in ("create_time", "is_outgoing", "type", "content")}
                safe["evidence_id"] = row["evidence_id"]
                if "card" in row:
                    safe["card"] = {k: redact(v) for k, v in row["card"].items()}
                line = json.dumps(safe, ensure_ascii=False)
                if fmt == "markdown":
                    who = {True: "我", False: "对方", None: "未知"}[row["is_outgoing"]]
                    title = safe.get("card", {}).get("title", "")
                    quote = safe.get("card", {}).get("quoted_text", "")
                    body = " ".join(filter(None, [safe["content"], title, quote]))
                    line = datetime.fromtimestamp(row["create_time"]).isoformat() + " " + who + "\n\n" + "\n".join(
                        "    " + x for x in body.splitlines()) + "\n"
                out.write(line + "\n")
                count += 1
        except BaseException:
            out.close()
            Path(path).unlink(missing_ok=True)
            raise
    return {"exported": count, "format": fmt, "redaction": "common_numbers_and_email; not anonymous"}
