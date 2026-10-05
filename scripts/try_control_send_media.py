"""显式授权的纯 Python 图片发送；UIA 定位、文件剪贴板、单次发送和回读。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import struct
import time

from try_control_send import (accessibility_gate, click_control, control_preflight,
                              client_roots, find_one, main_window, pattern, receipt_path,
                              row_identity, save_receipt, wait_for, walk)
from wechat_local.core import AccessError, freshness, open_db, resolve_chat, state_lock


def dropfiles(paths):
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(map(str, paths)) + "\0\0").encode("utf-16-le")


@contextlib.contextmanager
def file_clipboard(path, audit):
    import win32clipboard as clipboard
    import win32con

    clipboard.OpenClipboard()
    changed = False
    try:
        saved, fmt = {}, 0
        while True:
            fmt = clipboard.EnumClipboardFormats(fmt)
            if not fmt:
                break
            value = clipboard.GetClipboardData(fmt)
            if fmt == win32con.CF_HDROP:
                value = dropfiles(value)
            elif fmt == win32con.CF_LOCALE and isinstance(value, int):
                value = struct.pack("<I", value)
            elif not isinstance(value, (str, bytes)):
                raise AccessError("Unsupported clipboard handle format; original clipboard left unchanged")
            saved[fmt] = value
        changed = True
        clipboard.EmptyClipboard()
        clipboard.SetClipboardData(win32con.CF_HDROP, dropfiles([path]))
        if tuple(clipboard.GetClipboardData(win32con.CF_HDROP)) != (str(path),):
            raise AccessError("Clipboard file list did not verify")
        sequence = clipboard.GetClipboardSequenceNumber()
        audit["clipboard_file_verified"] = True
    except Exception:
        if changed:
            clipboard.EmptyClipboard()
            for fmt, value in saved.items():
                clipboard.SetClipboardData(fmt, value)
            audit["clipboard_restored"] = True
        raise
    finally:
        clipboard.CloseClipboard()
    try:
        yield
    finally:
        clipboard.OpenClipboard()
        try:
            if clipboard.GetClipboardSequenceNumber() == sequence:
                clipboard.EmptyClipboard()
                for fmt, value in saved.items():
                    clipboard.SetClipboardData(fmt, value)
                audit["clipboard_restored"] = True
            else:
                audit["clipboard_restored"] = False
                audit["clipboard_changed_during_operation"] = True
        finally:
            clipboard.CloseClipboard()


def new_images(rows, baseline, started):
    return [row for row in rows if row_identity(row) not in baseline
            and row.get("is_outgoing") is True and row.get("type") == "图片"
            and row.get("create_time", 0) >= started - 2]


def file_dialog(pid):
    import uiautomation as auto
    import win32gui
    import win32process

    handles = []
    def collect(hwnd, unused):
        if (win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
                and win32gui.GetWindowText(hwnd) in ("打开", "选择文件", "打开文件", "Open")):
            handles.append(hwnd)
    win32gui.EnumWindows(collect, None)
    hits = [auto.ControlFromHandle(hwnd) for hwnd in handles]
    if len(hits) > 1:
        raise AccessError("Multiple file dialogs; refusing selection")
    return hits[0] if hits else None


def reset_matching_file_dialog(pid, path, audit):
    import uiautomation as auto
    import win32gui

    dialog = file_dialog(pid)
    if dialog is None:
        return
    editor = find_one(dialog, lambda control: control.ControlTypeName == "EditControl"
                      and "文件名" in (control.Name or ""), "Existing file name editor")
    if pattern(editor, "ValuePattern").Value != str(path):
        raise AccessError("Unrelated existing file dialog; no navigation allowed")
    cancel = win32gui.GetDlgItem(dialog.NativeWindowHandle, 2)
    if not cancel:
        raise AccessError("Cannot cancel the matching owned file dialog")
    click_control(auto.ControlFromHandle(cancel), audit)
    if not wait_for(lambda: file_dialog(pid) is None, timeout=3):
        raise AccessError("Owned file dialog did not close")
    audit["matching_file_dialog_reset"] = True


def prepare_file_dialog(pid, path, audit):
    dialog = file_dialog(pid)
    resumed = dialog is not None
    if not resumed:
        root = main_window(pid)[1]
        buttons = [control for control in walk(root) if control.ControlTypeName == "ButtonControl"
                   and control.Name in ("发送文件", "发送文件...", "发送文件…", "文件")]
        if len(buttons) != 1:
            audit["toolbar_labels"] = [control.Name for toolbar in walk(root)
                                      if toolbar.ControlTypeName == "ToolBarControl"
                                      for control in walk(toolbar) if control.ControlTypeName == "ButtonControl"]
            raise AccessError("No unique file attachment control")
        click_control(buttons[0], audit)
        dialog = wait_for(lambda: file_dialog(pid), timeout=4)
    if dialog is None:
        raise AccessError("File attachment action did not expose a file dialog")
    editor = find_one(dialog, lambda control: control.ControlTypeName == "EditControl"
                      and "文件名" in (control.Name or ""), "File name editor")
    value = pattern(editor, "ValuePattern")
    if resumed and value.Value != str(path):
        raise AccessError("An unrelated existing file dialog must not be overwritten")
    audit["resumed_matching_file_dialog"] = resumed
    if value.IsReadOnly or not value.SetValue(str(path), waitTime=0.2) or value.Value != str(path):
        raise AccessError("Exact file path did not verify in file dialog")
    audit["file_path_verified"] = True
    import uiautomation as auto
    import win32gui
    open_handle = win32gui.GetDlgItem(dialog.NativeWindowHandle, 1)
    if not open_handle:
        raise AccessError("Native file dialog has no IDOK action")
    open_button = auto.ControlFromHandle(open_handle)
    audit["file_open_attempted"] = True
    click_control(open_button, audit)
    time.sleep(0.8)


def send_image(db, username, path, value, button, editor, receipt, fingerprint, audit, pid):
    baseline = {row_identity(row) for row in db.get_messages(username, 100)}
    if freshness(db)["wal_merge_failed"]:
        raise AccessError("Stale baseline; no media prepared")
    started = int(time.time())
    saved = {"fingerprint": fingerprint, "phase": "media_preparing", "started": started,
             "baseline": [list(identity) for identity in baseline],
             "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    save_receipt(receipt, saved)
    if value.IsReadOnly or value.Value:
        raise AccessError("Editor draft changed; refusing to paste media")
    def submit_and_confirm():
        nonlocal saved
        if not wait_for(lambda: value.Value and button.IsEnabled, timeout=5):
            raise AccessError("No media draft exposed through ValuePattern; no send attempted")
        audit["draft_prepared"] = True
        saved["phase"] = "media_send_invocation_started"
        save_receipt(receipt, saved)
        audit["send_invoked"] = True
        try:
            click_control(button, audit)
        except Exception as exc:
            audit["send_error_type"] = type(exc).__name__
        deadline = time.monotonic() + 25
        while True:
            rows = db.get_messages(username, 100)
            if freshness(db)["wal_merge_failed"]:
                raise AccessError("Stale media confirmation snapshot; do not resend")
            hits = new_images(rows, baseline, started)
            if len(hits) == 1:
                audit["local_outgoing_confirmed"] = True
                saved.update({"phase": "local_outgoing_confirmed", "record": list(row_identity(hits[0])),
                              "message_type": "图片", "sender_mapping": hits[0].get("sender_mapping")})
                save_receipt(receipt, saved)
                return
            if len(hits) > 1:
                raise AccessError("Multiple new outgoing images; do not resend")
            if time.monotonic() >= deadline:
                saved["phase"] = "media_send_result_uncertain"
                save_receipt(receipt, saved)
                raise AccessError("No unique new outgoing image confirmed; do not resend")
            time.sleep(0.6)

    if audit.get("route") == "file_dialog":
        prepare_file_dialog(pid, path, audit)
        submit_and_confirm()
        return
    with file_clipboard(path, audit):
        click_control(editor, audit)
        if not editor.HasKeyboardFocus:
            raise AccessError("Exact target editor did not acquire focus")
        audit["keyboard"] = True
        audit["file_paste_attempted"] = True
        editor.SendKeys("{Ctrl}v", charMode=False, waitTime=0.8)
        submit_and_confirm()


def main():
    p = argparse.ArgumentParser(description="纯 Python 图片发送实验，不使用 Computer Use 或截图")
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--chat", required=True)
    p.add_argument("--file", type=Path, action="append", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--allow-temporary-accessibility", action="store_true")
    p.add_argument("--allow-control-input", action="store_true")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--route", choices=("clipboard", "file_dialog"), default="file_dialog")
    args = p.parse_args()
    audit = {"experimental": True, "backend": "uia_file_dialog_image_send" if args.route == "file_dialog"
             else "uia_cf_hdrop_image_send", "screenshots": False,
             "computer_use": False, "dll_injection": False, "operations": []}
    try:
        if not args.commit or not args.allow_control_input:
            raise AccessError("Actual media sends require explicit --commit and --allow-control-input")
        config = json.loads(args.runtime.read_text(encoding="utf-8"))
        if not config.get("account"):
            raise AccessError("Explicitly bound account required")
        paths = [path.resolve(strict=True) for path in args.file]
        if any(path.suffix.lower() not in (".png", ".jpg", ".jpeg") or not path.is_file() for path in paths):
            raise AccessError("This experimental route accepts PNG/JPEG image files only")
        with state_lock():
            db = open_db(config.get("db_dir"), config["account"])
            target = resolve_chat(db, args.chat)
            who = target.get("remark") or target.get("nick_name") or target["username"]
            if target["username"] != "filehelper" and resolve_chat(db, who)["username"] != target["username"]:
                raise AccessError("Display name does not identify the same exact account")
            prepared = []
            for index, path in enumerate(paths):
                operation = {"operation_index": index, "send_invoked": False, "local_outgoing_confirmed": False,
                             "route": args.route}
                fingerprint = hashlib.sha256(json.dumps([config["account"], target["username"],
                    hashlib.sha256(path.read_bytes()).hexdigest()]).encode()).hexdigest()
                receipt = receipt_path(config["account"], args.request_id + ":" + str(index))
                if receipt.exists():
                    saved = json.loads(receipt.read_text(encoding="utf-8"))
                    retry_safe = saved.get("phase") == "failed_before_media_input" and saved.get("retry_safe") is True
                    if saved.get("fingerprint") != fingerprint or (saved.get("phase") != "local_outgoing_confirmed" and not retry_safe):
                        raise AccessError("Existing media receipt prevents repeating this operation")
                    matches = [row for row in db.get_messages(target["username"], 100)
                               if list(row_identity(row)) == saved.get("record")
                               and row.get("is_outgoing") is True and row.get("type") == "图片"]
                    if not retry_safe and (len(matches) != 1 or freshness(db)["wal_merge_failed"]):
                        raise AccessError("Original media confirmation cannot be revalidated")
                    if not retry_safe:
                        operation.update({"local_outgoing_confirmed": True, "previously_confirmed": True})
                prepared.append((path, receipt, fingerprint, operation))
                audit["operations"].append(operation)
            with accessibility_gate(args.pid, args.allow_temporary_accessibility, audit):
                for path, receipt, fingerprint, operation in prepared:
                    if operation["local_outgoing_confirmed"]:
                        continue
                    try:
                        if args.route == "file_dialog":
                            reset_matching_file_dialog(args.pid, path, operation)
                        value, button, editor = control_preflight(args.pid, who, operation, True, prefer_search=True)
                        send_image(db, target["username"], path, value, button, editor, receipt, fingerprint, operation, args.pid)
                    except Exception as exc:
                        operation["error"] = str(exc)
                        if receipt.exists() and not any(operation.get(key) for key in
                                ("file_paste_attempted", "file_open_attempted", "send_invoked")):
                            saved = json.loads(receipt.read_text(encoding="utf-8"))
                            saved.update({"phase": "failed_before_media_input", "retry_safe": True})
                            save_receipt(receipt, saved)
                        raise
    except Exception as exc:
        audit["error"] = str(exc)
    audit["confirmed_operation_count"] = sum(bool(op["local_outgoing_confirmed"]) for op in audit["operations"])
    print(json.dumps(audit, ensure_ascii=False))
    return 0 if "error" not in audit else 2


if __name__ == "__main__":
    raise SystemExit(main())
