from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET


class AccessError(RuntimeError):
    pass


def state_dir() -> Path:
    override = os.environ.get("WECHAT_LOCAL_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ["LOCALAPPDATA"]) / "wechat-local-skill"


def protect_directory(path: Path) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    path.mkdir(parents=True, exist_ok=True)
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for principal in (sid, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, principal)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )


@contextlib.contextmanager
def state_lock():
    import win32api
    import win32event

    tag = hashlib.sha256(str(state_dir()).encode()).hexdigest()[:24]
    handle = win32event.CreateMutex(None, False, "Local\\WechatLocal-" + tag)
    acquired = False
    try:
        result = win32event.WaitForSingleObject(handle, 180000)
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise AccessError("Another local WeChat operation is still running")
        acquired = True
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(handle)
        win32api.CloseHandle(handle)


def running_clients() -> list[dict]:
    import psutil
    import win32api

    clients = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        if (process.info["name"] or "").lower() not in ("weixin.exe", "wechat.exe"):
            continue
        item = {"pid": process.pid, "name": process.info["name"], "exe": process.info["exe"]}
        try:
            version = win32api.GetFileVersionInfo(process.info["exe"], "\\")
            high, low = version["FileVersionMS"], version["FileVersionLS"]
            item["version"] = ".".join(map(str, (high >> 16, high & 65535, low >> 16, low & 65535)))
        except (OSError, TypeError):
            item["version"] = None
        clients.append(item)
    return clients


def account_list(db_dir: str | None = None) -> tuple[str, list[dict]]:
    from .vendor.replica_db import auto_detect_db_dir, list_accounts

    root = db_dir or auto_detect_db_dir()
    if not root:
        raise AccessError("WeChat data directory not found; supply --db-dir")
    return root, list_accounts(root)


def select_account(accounts: list[dict], account: str | None) -> str:
    if account:
        if any(item["account"] == account for item in accounts):
            return account
        raise AccessError("Account not found in the selected WeChat data directory")
    if len(accounts) != 1:
        raise AccessError("Specify --account explicitly; found %d account directories" % len(accounts))
    return accounts[0]["account"]


def open_db(db_dir: str | None = None, account: str | None = None):
    from .vendor.replica_db import WeChatDB
    import psutil
    import win32crypt

    root, accounts = account_list(db_dir)
    selected = select_account(accounts, account)
    private_root = state_dir()
    protect_directory(private_root)
    cache = private_root / hashlib.sha256(selected.encode()).hexdigest()[:24]
    protect_directory(cache)

    class PrivateDB(WeChatDB):
        def _collect_db_files(self):
            wanted = {"contact.db", "session.db", "message_resource.db", "sns.db"}
            return [entry for entry in super()._collect_db_files()
                    if Path(entry[1]).name in wanted or re.fullmatch(r"message_\d+\.db", Path(entry[1]).name)]

        def _find_weixin_pids(self):
            return [p.pid for p in psutil.process_iter(["name"])
                    if (p.info["name"] or "").lower() == "weixin.exe"]

        def _load_or_extract_keys(self, master_key=None):
            # Only this explicit account/cache is used. No cfg-key fallback,
            # cross-account guessing, plaintext key files or UI activation.
            path = Path(self.keys_file)
            if path.is_file():
                try:
                    raw = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 1)[1]
                    self._keys = {rel: bytes.fromhex(key) for rel, key in json.loads(raw).items()}
                except Exception as exc:
                    raise AccessError("Cannot decrypt this account's DPAPI key cache") from exc
                self._keys = {rel: key for rel, key in self._keys.items() if self._key_works(rel)}
            self.master_key = self.cfg_dword = None
            missing = [rel for rel, _, _ in self._db_files if rel not in self._keys]
            if missing:
                self._keys.update(self.extract_keys())
                self._save_keys()
            self.unkeyed = [rel for rel, _, _ in self._db_files if not self._key_works(rel)]

        def _save_keys(self):
            if not self._keys:
                return
            raw = json.dumps({rel: key.hex() for rel, key in self._keys.items()}).encode()
            sealed = win32crypt.CryptProtectData(raw, "wechat-local account keys", None, None, None, 1)
            path = Path(self.keys_file)
            temp = path.with_suffix(".tmp")
            temp.write_bytes(sealed)
            temp.replace(path)

        def _open(self, rel):
            if rel not in self._keys:
                raise AccessError("No validated key for database: " + rel)
            return super()._open(rel)

    return PrivateDB(db_dir=root, account=selected, workdir=str(cache), keys_file=str(cache / "keys.dpapi"))


def database_for(db, basename: str):
    for relative, original, _ in db._db_files:
        if Path(original).name == basename:
            return db._open(relative)
    raise AccessError("Database not present: " + basename)


def exact_candidate(hits: list[dict], query: str) -> dict:
    exact = {item["username"]: item for item in hits
             if query in (item.get("username"), item.get("nick_name"), item.get("remark"), item.get("alias"))}
    if len(exact) != 1:
        raise AccessError("Chat must match one exact username, alias, nickname or remark; found %d" % len(exact))
    return next(iter(exact.values()))


def contacts(db, query: str = "", limit: int = 50) -> list[dict]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with contextlib.closing(database_for(db, "contact.db")) as conn:
        rows = conn.execute(
            "SELECT username, alias, nick_name, remark FROM contact "
            "WHERE username LIKE ? ESCAPE '\\' OR alias LIKE ? ESCAPE '\\' "
            "OR nick_name LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\' "
            "ORDER BY username LIMIT ?", ("%" + escaped + "%",) * 4 + (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_chat(db, query: str) -> dict:
    if query in ("filehelper", "文件传输助手"):
        return {"username": "filehelper", "nick_name": "文件传输助手", "remark": "", "alias": ""}
    if not query.strip():
        raise AccessError("Chat query cannot be empty")
    with contextlib.closing(database_for(db, "contact.db")) as conn:
        hits = [dict(row) for row in conn.execute(
            "SELECT username, alias, nick_name, remark FROM contact "
            "WHERE username=? OR alias=? OR nick_name=? OR remark=?", (query,) * 4,
        ).fetchall()]
    return exact_candidate(hits, query)


def public_message(row: dict) -> dict:
    fields = ("local_id", "sort_seq", "create_time", "type", "sender_id", "sender_username")
    result = {key: row.get(key) for key in fields}
    content = row.get("content", "")
    # Media XML can embed usable CDN credentials. Keep raw payloads out of
    # ordinary agent output; this release exposes text and media placeholders.
    if row.get("type") == "文本" and isinstance(content, str):
        result["content"] = content
    else:
        result["content"] = "[" + str(row.get("type", "unknown")) + "]"
    return result


def parse_moment(xml: str | bytes, username: str, tid: int) -> dict:
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8")
    if len(xml) > 4_000_000 or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise AccessError("Unsupported or oversized Moments XML")
    root = ET.fromstring(xml)
    timeline = root.find("TimelineObject")
    if timeline is None:
        timeline = root

    def text(path, default=""):
        return timeline.findtext(path, default=default) or default

    def interactions(path):
        return [{"username": node.findtext("username", ""),
                 "nickname": node.findtext("nickname", ""),
                 "content": node.findtext("content", ""),
                 "reply_to": node.findtext("reply_username", ""),
                 "comment_id": node.findtext("comment_id", ""),
                 "ref_comment_id": node.findtext("ref_comment_id", "")}
                for node in root.findall(path)
                if node.findtext("b_deleted", "0") != "1"
                and node.findtext("isDeleted", "0") != "1"]

    media = []
    for node in root.findall(".//mediaList/media"):
        size = node.find("size")
        media.append({"id": node.findtext("id", ""), "type": node.findtext("type", ""),
                      "dimensions": dict(size.attrib) if size is not None else {}})
    return {"tid": tid, "id": text("id"), "username": text("username", username),
            "create_time": int(text("createTime", "0")), "text": text("contentDesc"),
            "media": media, "likes": interactions(".//like_user_list/user_comment"),
            "comments": interactions(".//comment_user_list/user_comment")}


def moments(db, limit=20, offset=0, username=None) -> list[dict]:
    where, params = (" WHERE user_name=?", (username,)) if username else ("", ())
    with contextlib.closing(database_for(db, "sns.db")) as conn:
        rows = conn.execute("SELECT tid, user_name, content FROM SnsTimeLine" + where
                            + " ORDER BY tid DESC LIMIT ? OFFSET ?", params + (limit, offset)).fetchall()
    result = []
    for row in rows:
        try:
            item = parse_moment(row["content"], row["user_name"], row["tid"])
            item["nickname"] = db.get_nickname(item["username"])
        except (ET.ParseError, ValueError, UnicodeError, AccessError) as exc:
            item = {"tid": row["tid"], "parse_error": type(exc).__name__}
        result.append(item)
    return result


def freshness(db) -> dict:
    stale = []
    for stamp in Path(db.workdir).glob("*.stamp"):
        try:
            if int(stamp.read_text().split(",")[-1]) == -1:
                stale.append(stamp.name.removesuffix(".stamp"))
        except (OSError, ValueError):
            pass
    return {"source": "local_cache_not_full_remote_history", "read_at": int(time.time()),
            "wal_merge_failed": stale,
            "client_running": bool(running_clients()) if os.name == "nt" else False}
