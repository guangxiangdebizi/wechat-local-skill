"""经明确授权的 UIA 控件发送实验；无截图、坐标、键盘或 DLL 注入。"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
from pathlib import Path
import time

from wechat_local.core import (AccessError, freshness, open_db, protect_directory,
                               resolve_chat, state_dir, state_lock)


PROFILE_HASH = "10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5"
GATE_RVA = 0x0B135C38


def walk(control, depth=0):
    yield control
    if depth < 40:
        for child in control.GetChildren():
            yield from walk(child, depth + 1)


def find_one(root, predicate, label):
    hits = [control for control in walk(root) if predicate(control)]
    if len(hits) != 1:
        raise AccessError(label + ": expected exactly one control, found " + str(len(hits)))
    return hits[0]


def pattern(control, kind):
    import uiautomation as auto
    result = control.GetPattern(getattr(auto.PatternId, kind))
    if result is None:
        raise AccessError("Control does not expose " + kind)
    return result


def invoke(control):
    import uiautomation as auto
    action = control.GetPattern(auto.PatternId.InvokePattern)
    if action is not None:
        return action.Invoke(waitTime=0.2)
    legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
    if legacy is not None and legacy.DefaultAction:
        return legacy.DoDefaultAction(waitTime=0.2)
    raise AccessError("Control exposes no Invoke/default-action interface")


def aid_hit(control, token):
    return token in (control.AutomationId or "").split(".")


def normalized_name(control):
    return (control.Name or "").split("\n", 1)[0].strip()


def wait_for(observe, timeout=3.0):
    deadline = time.monotonic() + timeout
    while True:
        result = observe()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def navigate(control, observe, label, timeout=3.0):
    import uiautomation as auto

    # Only navigation may try another provider after a verified no-op.
    for kind, method in (("SelectionItemPattern", "Select"),
                         ("InvokePattern", "Invoke"),
                         ("LegacyIAccessiblePattern", "DoDefaultAction")):
        provider = control.GetPattern(getattr(auto.PatternId, kind))
        if provider is None or (method == "DoDefaultAction" and not provider.DefaultAction):
            continue
        getattr(provider, method)(waitTime=0.2)
        result = wait_for(observe, timeout)
        if result:
            return result
    raise AccessError(label + ": no provider produced the required UI state; no input fallback")


def exact_item(root, who, list_token):
    containers = [control for control in walk(root) if aid_hit(control, list_token)]
    hits = []
    for container in containers:
        for item in container.GetChildren():
            if normalized_name(item) == who:
                hits.append(item)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AccessError("Multiple exact session/search results; refusing selection")
    return None


def safe_summary(root):
    import uiautomation as auto

    rows = []
    for control in walk(root):
        if len(rows) == 70:
            break
        name = control.Name or ""
        tokens = (control.AutomationId or "").split(".")
        anchors = [token for token in tokens
                   if token in ("session_list", "search_list", "chat_input_field", "chat_message_list")]
        providers = [kind for kind in ("ValuePattern", "InvokePattern", "SelectionItemPattern",
                                      "LegacyIAccessiblePattern")
                     if control.GetPattern(getattr(auto.PatternId, kind)) is not None]
        rows.append({"type": control.ControlTypeName, "class": control.ClassName,
                     "anchors": anchors, "patterns": providers,
                     "label": name if name in ("微信", "搜索", "发送", "发送(S)", "文件传输助手") else "<redacted>",
                     "filehelper_first_line": normalized_name(control) == "文件传输助手"})
    return rows


def main_window(pid):
    import psutil
    import win32gui
    import win32process
    import uiautomation as auto

    if psutil.Process(pid).name().lower() != "weixin.exe":
        raise AccessError("Target is not Weixin.exe")
    windows = []

    def callback(hwnd, unused):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.GetWindowText(hwnd) == "微信":
            if win32gui.GetParent(hwnd) == 0:
                windows.append(hwnd)

    win32gui.EnumWindows(callback, None)
    if len(windows) != 1:
        raise AccessError("Main WeChat window is not unique")
    root = auto.ControlFromHandle(windows[0])
    root.GetWindowPattern().SetWindowVisualState(auto.WindowVisualState.Normal)
    return windows[0], root


@contextlib.contextmanager
def accessibility_gate(pid, allowed, audit):
    import win32api
    import win32con
    import win32process
    from ctypes import wintypes
    from wechat_local.vendor.replica_db import _k32

    rights = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    if allowed:
        rights |= win32con.PROCESS_VM_WRITE | win32con.PROCESS_VM_OPERATION
    handle = win32api.OpenProcess(rights, False, pid)
    original = None
    changed = False
    audit["accessibility_restored"] = False
    try:
        modules = [module for module in win32process.EnumProcessModules(handle)
                   if win32process.GetModuleFileNameEx(handle, module).lower().endswith("\\weixin.dll")]
        if len(modules) != 1:
            raise AccessError("Loaded Weixin.dll is not unique")
        base = modules[0]
        path = Path(win32process.GetModuleFileNameEx(handle, base))
        if hashlib.sha256(path.read_bytes()).hexdigest() != PROFILE_HASH:
            raise AccessError("Unrecognized Weixin.dll hash; no memory change allowed")
        address = base + GATE_RVA

        def read_byte():
            value, done = ctypes.c_ubyte(), ctypes.c_size_t()
            ok = _k32.ReadProcessMemory(int(handle), ctypes.c_void_p(address), ctypes.byref(value), 1, ctypes.byref(done))
            if not ok or done.value != 1:
                raise AccessError("Cannot read accessibility flag")
            return value.value

        def write_byte(value):
            _k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            _k32.WriteProcessMemory.restype = wintypes.BOOL
            data, done = ctypes.c_ubyte(value), ctypes.c_size_t()
            ok = _k32.WriteProcessMemory(int(handle), ctypes.c_void_p(address), ctypes.byref(data), 1, ctypes.byref(done))
            if not ok or done.value != 1 or read_byte() != value:
                raise AccessError("Accessibility flag write did not verify")

        original = read_byte()
        audit["original_accessibility_flag"] = original
        if original not in (0, 1):
            raise AccessError("Unexpected flag value; profile not applicable")
        if original == 0:
            if not allowed:
                raise AccessError("Accessibility controls unavailable; explicit temporary-flag authorization required")
            changed = True
            write_byte(1)
            audit["temporary_accessibility_change"] = True
        yield
    finally:
        try:
            if changed:
                write_byte(original)
                audit["accessibility_restored"] = True
            else:
                audit["accessibility_restored"] = original is not None and read_byte() == original
        finally:
            handle.Close()


def control_preflight(pid, who, audit):
    import uiautomation as auto

    hwnd, root = main_window(pid)
    if not any((control.ClassName or "").startswith("mmui::") for control in walk(root)):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            time.sleep(0.3)
            root = auto.ControlFromHandle(hwnd)
            if any((control.ClassName or "").startswith("mmui::") for control in walk(root)):
                break
        else:
            raise AccessError("No mmui controls exposed; no message was prepared or sent")
    def refreshed(predicate):
        current = auto.ControlFromHandle(hwnd)
        return current if predicate(current) else None

    input_matches = [control for control in walk(root)
                     if aid_hit(control, "chat_input_field") and control.Name == who]
    if not input_matches:
        tabs = [control for control in walk(root)
                if control.Name == "微信" and "XTabBarItem" in (control.ClassName or "")]
        has_sessions = any(aid_hit(control, "session_list") for control in walk(root))
        if len(tabs) == 1 and not has_sessions:
            root = navigate(tabs[0], lambda: refreshed(
                lambda current: any(aid_hit(c, "session_list") for c in walk(current))), "Chat tab")
        selected = exact_item(root, who, "session_list")
        if selected is None:
            boxes = [control for control in walk(root)
                     if control.ControlTypeName == "EditControl" and control.Name == "搜索"]
            if not boxes:
                search = find_one(root, lambda control: control.ControlTypeName == "ButtonControl"
                                  and control.Name == "搜索", "Search button")
                root = navigate(search, lambda: refreshed(lambda current: any(
                    c.ControlTypeName == "EditControl" and c.Name == "搜索" for c in walk(current))),
                    "Search button")
                boxes = [control for control in walk(root)
                         if control.ControlTypeName == "EditControl" and control.Name == "搜索"]
            if len(boxes) != 1:
                audit["control_summary"] = safe_summary(root)
                raise AccessError("Search did not expose one editable provider; no coordinate fallback")
            search_value = pattern(boxes[0], "ValuePattern")
            if search_value.IsReadOnly or search_value.Value:
                raise AccessError("Search has existing input or is read-only; refusing to overwrite")
            if not search_value.SetValue(who, waitTime=0.8):
                raise AccessError("Search ValuePattern failed")
            selected = wait_for(lambda: exact_item(auto.ControlFromHandle(hwnd), who, "search_list"))
            if selected is None:
                root = auto.ControlFromHandle(hwnd)
                audit["control_summary"] = safe_summary(root)
                raise AccessError("No unique exact result from control search")
        root = navigate(selected, lambda: refreshed(lambda current: any(
            aid_hit(c, "chat_input_field") and c.Name == who for c in walk(current))), "Exact chat item")
    editor = find_one(root, lambda control: aid_hit(control, "chat_input_field")
                      and control.Name == who, "Exact target chat input")
    value = pattern(editor, "ValuePattern")
    if value.IsReadOnly or value.Value:
        raise AccessError("Chat input is read-only or has an existing draft; refusing to overwrite")
    button = find_one(root, lambda control: control.ControlTypeName == "ButtonControl"
                      and control.Name in ("发送", "发送(S)"), "Send button")
    pattern(button, "InvokePattern")
    return value, button


def save_receipt(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def receipt_path(account, request_id):
    directory = state_dir() / "control-send-receipts"
    protect_directory(directory)
    key = hashlib.sha256(json.dumps([account, request_id]).encode()).hexdigest()
    return directory / (key + ".json")


def check_receipt(path, fingerprint):
    if path.exists():
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("fingerprint") != fingerprint:
            raise AccessError("Request ID was already bound to a different operation")
        raise AccessError("Request ID already reserved: " + str(receipt.get("phase"))
                          + "; inspect the original result, do not automatically resend")


def send_once(db, username, value, button, text, audit, receipt, fingerprint, timeout=15.0):
    before = {row_identity(row) for row in db.get_messages(username, 100)}
    if freshness(db)["wal_merge_failed"]:
        raise AccessError("Baseline snapshot is stale; no message will be prepared")
    started = int(time.time())
    data = {"fingerprint": fingerprint, "phase": "preparing", "started": started}
    save_receipt(receipt, data)
    try:
        if value.IsReadOnly or value.Value:
            raise AccessError("Chat input changed after preflight; refusing to overwrite")
        audit["text_set_attempted"] = True
        if not value.SetValue(text, waitTime=0.2) or value.Value != text:
            raise AccessError("ValuePattern could not set and verify exact message text")
        audit["text_prepared"] = True
        if not button.IsEnabled:
            raise AccessError("Send button remains disabled after verified text entry")
        data["phase"] = "send_invocation_started"
        save_receipt(receipt, data)
        audit["send_invoked"] = True
        try:
            audit["invoke_reported_success"] = bool(invoke(button))
        except Exception as exc:
            audit["invoke_error_type"] = type(exc).__name__
        deadline = time.monotonic() + timeout
        while True:
            rows = db.get_messages(username, 100)
            if freshness(db)["wal_merge_failed"]:
                raise AccessError("Outgoing snapshot is stale; do not automatically resend")
            hits = new_outgoing(rows, before, text, started)
            if len(hits) == 1:
                audit["local_outgoing_confirmed"] = True
                data["phase"] = "local_outgoing_confirmed"
                data["record"] = list(row_identity(hits[0]))
                save_receipt(receipt, data)
                return
            if len(hits) > 1:
                raise AccessError("Multiple new matching outgoing records; result is ambiguous")
            if time.monotonic() >= deadline:
                raise AccessError("No new exact outgoing record confirmed; do not automatically resend")
            time.sleep(0.7)
    except Exception:
        if not audit.get("send_invoked"):
            audit["draft_cleanup_verified"] = False
            # Do not clear a draft that the user changed during the operation.
            try:
                if audit.get("text_set_attempted") and value.Value == text:
                    value.SetValue("", waitTime=0.2)
                    audit["draft_cleanup_verified"] = value.Value == ""
            except Exception as exc:
                audit["draft_cleanup_error_type"] = type(exc).__name__
            data["phase"] = "failed_before_send"
        else:
            data["phase"] = "send_result_uncertain"
        save_receipt(receipt, data)
        raise


def row_identity(row):
    return row.get("sort_seq"), row.get("local_id"), row.get("create_time")


def new_outgoing(rows, baseline, text, started):
    return [row for row in rows if row_identity(row) not in baseline
            and row.get("type") == "文本" and row.get("content") == text
            and row.get("sender_id") == 2 and row.get("create_time", 0) >= started - 2]


def main():
    p = argparse.ArgumentParser(description="纯 UIA 控件发送实验；不使用 Computer Use、截图或坐标")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--chat", default="filehelper")
    p.add_argument("--text", default="Codex 微信接口测试")
    p.add_argument("--allow-temporary-accessibility", action="store_true")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--request-id", help="实际发送必填；同一操作始终使用同一个 ID，禁止换 ID 自动重发")
    args = p.parse_args()
    if args.commit and (not args.request_id or not args.request_id.strip()):
        p.error("--commit requires a non-empty --request-id")
    audit = {"experimental": True, "backend": "uia_value_and_invoke_patterns",
             "screenshots": False, "coordinates": False, "keyboard": False,
             "dll_injection": False, "temporary_accessibility_change": False,
             "send_invoked": False, "local_outgoing_confirmed": False}
    try:
        config = json.loads(args.runtime.read_text(encoding="utf-8"))
        if not config.get("account"):
            raise AccessError("Control experiments require an explicitly bound runtime account")
        with state_lock():
            db = open_db(config.get("db_dir"), config.get("account"))
            target = resolve_chat(db, args.chat)
            who = target.get("remark") or target.get("nick_name") or target["username"]
            if target["username"] != "filehelper":
                same = resolve_chat(db, who)
                if same["username"] != target["username"]:
                    raise AccessError("Display name does not resolve to the requested exact account")
            if args.commit:
                receipt = receipt_path(config["account"], args.request_id)
                fingerprint = hashlib.sha256(json.dumps(
                    [config["account"], target["username"], args.text]).encode()).hexdigest()
                check_receipt(receipt, fingerprint)
            with accessibility_gate(args.pid, args.allow_temporary_accessibility, audit):
                try:
                    value, button = control_preflight(args.pid, who, audit)
                except Exception:
                    import uiautomation as auto
                    audit.setdefault("control_summary", safe_summary(main_window(args.pid)[1]))
                    raise
                audit["preflight_passed"] = True
                if args.commit:
                    send_once(db, target["username"], value, button, args.text, audit,
                              receipt, fingerprint)
    except Exception as exc:
        audit["error"] = str(exc)
    print(json.dumps(audit, ensure_ascii=False))
    return 0 if audit.get("preflight_passed") and "error" not in audit else 2


if __name__ == "__main__":
    raise SystemExit(main())
