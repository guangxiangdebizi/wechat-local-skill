"""经明确授权的 UIA 控件发送实验；控件点击/按键需要独立开关。"""
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


class AmbiguousControlResults(AccessError):
    pass


def walk(control, depth=0):
    if isinstance(control, (list, tuple)):
        for root in control:
            yield from walk(root, depth)
        return
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


def click_control(control, audit):
    import win32gui
    import win32process

    if control.IsOffscreen or not control.IsEnabled:
        raise AccessError("Target control is not visible and enabled")
    rect = control.BoundingRectangle
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise AccessError("Target control has an empty rectangle")
    point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    hwnd = win32gui.WindowFromPoint(point)
    if not hwnd or win32process.GetWindowThreadProcessId(hwnd)[1] != control.ProcessId:
        raise AccessError("Target control is occluded by another process; no click allowed")
    audit["coordinates"] = True
    audit["coordinate_source"] = "uia_bounding_rectangle"
    audit["mouse"] = True
    control.Click(simulateMove=False, waitTime=0.3)


def navigate(control, observe, label, timeout=3.0, audit=None, allow_control_input=False):
    import uiautomation as auto

    if allow_control_input:
        if audit is None:
            raise AccessError("Control input requires an operation audit")
        click_control(control, audit)
        result = wait_for(observe, timeout)
        audit.setdefault("navigation_actions", []).append(
            {"provider": "uia_located_mouse", "method": "Click", "effect_verified": bool(result)})
        if result:
            return result
        raise AccessError(label + ": control click did not produce the required UI state")
    if hasattr(control, "SetFocus"):
        try:
            control.SetFocus()
        except Exception:
            pass
    # Only navigation may try another provider after a verified no-op.
    for kind, method in (("SelectionItemPattern", "Select"),
                         ("InvokePattern", "Invoke"),
                         ("LegacyIAccessiblePattern", "DoDefaultAction"),
                         ("LegacyIAccessiblePattern", "Select")):
        provider = control.GetPattern(getattr(auto.PatternId, kind))
        if provider is None or (method == "DoDefaultAction" and not provider.DefaultAction):
            continue
        action = {"provider": kind, "method": method, "effect_verified": False}
        try:
            if kind == "LegacyIAccessiblePattern" and method == "Select":
                provider.Select(auto.AccessibleSelection.TakeFocus | auto.AccessibleSelection.TakeSelection,
                                waitTime=0.2)
            else:
                getattr(provider, method)(waitTime=0.2)
        except Exception as exc:
            action["error_type"] = type(exc).__name__
        result = wait_for(observe, timeout)
        action["effect_verified"] = bool(result)
        if audit is not None:
            audit.setdefault("navigation_actions", []).append(action)
        if result:
            return result
    raise AccessError(label + ": no provider produced the required UI state; no input fallback")


def exact_item(root, who, list_token):
    containers = [control for control in walk(root)
                  if (control.AutomationId or "").split(".")[-1] == list_token]
    hits = []
    identities = set()
    for container in containers:
        for item in container.GetChildren():
            if normalized_name(item) != who or getattr(item, "IsOffscreen", False):
                continue
            identity = tuple(item.GetRuntimeId()) if hasattr(item, "GetRuntimeId") else (id(item),)
            if identity not in identities:
                identities.add(identity)
                hits.append(item)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        error = AmbiguousControlResults("Multiple distinct visible exact session/search results; refusing selection")
        error.candidates = [{"type": item.ControlTypeName, "class": item.ClassName,
                             "aid_hash": hashlib.sha256((item.AutomationId or "").encode()).hexdigest()[:16],
                             "runtime_hash": hashlib.sha256(str(
                                 item.GetRuntimeId() if hasattr(item, "GetRuntimeId") else id(item)
                             ).encode()).hexdigest()[:16]} for item in hits]
        raise error
    return None


def exact_search_item(root, who, audit):
    recent = {"最常使用", "最近使用"}
    sections = recent | {"联系人", "群聊", "公众号", "聊天记录", "功能"}
    rows, identities = [], set()
    for container in walk(root):
        if (container.AutomationId or "").split(".")[-1] != "search_list":
            continue
        section = None
        for item in container.GetChildren():
            aid = item.AutomationId or ""
            name = normalized_name(item)
            if not aid and name in sections:
                section = name
                continue
            if not aid.split(".")[-1].startswith("search_item_"):
                continue
            if name != who or getattr(item, "IsOffscreen", False):
                continue
            identity = tuple(item.GetRuntimeId()) if hasattr(item, "GetRuntimeId") else (id(item),)
            if identity not in identities:
                identities.add(identity)
                rows.append({"control": item, "aid": aid, "section": section})
    audit["search_candidates"] = [
        {"type": row["control"].ControlTypeName, "class": row["control"].ClassName,
         "section": row["section"], "aid_hash": hashlib.sha256(row["aid"].encode()).hexdigest()[:16]}
        for row in rows]
    # A frequent/recent copy can be removed only when its same non-empty result ID is present.
    primary_ids = {row["aid"] for row in rows if row["section"] not in recent}
    pruned = [row for row in rows if not (row["section"] in recent and row["aid"] in primary_ids)]
    audit["search_candidate_count_after_pruning"] = len(pruned)
    if len(pruned) > 1:
        raise AmbiguousControlResults("Multiple logical exact search results; refusing selection")
    return pruned[0]["control"] if len(pruned) == 1 else None


def client_roots(pid, main_hwnd):
    import uiautomation as auto
    import win32gui
    import win32process

    handles = [main_hwnd]

    def collect(hwnd, unused):
        if hwnd != main_hwnd and win32gui.IsWindowVisible(hwnd):
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                handles.append(hwnd)

    win32gui.EnumWindows(collect, None)
    return [auto.ControlFromHandle(hwnd) for hwnd in handles]


def target_input(control, who):
    return ((control.AutomationId or "").split(".")[-1] == "chat_input_field"
            and control.ControlTypeName == "EditControl" and control.Name == who)


def safe_summary(root):
    import uiautomation as auto

    rows = []
    controls = list(walk(root))
    controls.sort(key=lambda control: (normalized_name(control) != "文件传输助手",
                  not any(aid_hit(control, token) for token in
                          ("session_list", "search_list", "chat_input_field", "chat_message_list"))))
    for control in controls:
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
                     "terminal_anchor": tokens[-1] in anchors,
                     "offscreen": bool(getattr(control, "IsOffscreen", False)),
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


def control_preflight(pid, who, audit, allow_control_input=False):
    import uiautomation as auto

    audit["stage"] = "window_controls"
    hwnd, root = main_window(pid)
    if allow_control_input:
        import win32gui
        import win32process

        foreground = win32gui.GetForegroundWindow()
        if not foreground or win32process.GetWindowThreadProcessId(foreground)[1] != pid:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                root.SetFocus()
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
                     if target_input(control, who)]
    if not input_matches:
        tabs = [control for control in walk(root)
                if control.Name == "微信" and "XTabBarItem" in (control.ClassName or "")]
        has_sessions = any(aid_hit(control, "session_list") for control in walk(root))
        if len(tabs) == 1 and not has_sessions:
            root = navigate(tabs[0], lambda: refreshed(
                lambda current: any(aid_hit(c, "session_list") for c in walk(current))), "Chat tab",
                audit=audit, allow_control_input=allow_control_input)
        audit["stage"] = "session_lookup"
        try:
            selected = exact_item(root, who, "session_list")
        except AmbiguousControlResults as exc:
            audit["session_selection_ambiguous"] = True
            audit["session_candidates"] = exc.candidates
            # Do not pick an ambiguous session; a separate exact search may resolve it.
            selected = None
        if selected is None:
            audit["stage"] = "search_open"
            boxes = [control for control in walk(root)
                     if control.ControlTypeName == "EditControl" and control.Name == "搜索"]
            if not boxes:
                search = find_one(root, lambda control: control.ControlTypeName == "ButtonControl"
                                  and control.Name == "搜索", "Search button")
                root = navigate(search, lambda: refreshed(lambda current: any(
                    c.ControlTypeName == "EditControl" and c.Name == "搜索" for c in walk(current))),
                    "Search button", audit=audit, allow_control_input=allow_control_input)
                boxes = [control for control in walk(root)
                         if control.ControlTypeName == "EditControl" and control.Name == "搜索"]
            if len(boxes) != 1:
                audit["control_summary"] = safe_summary(root)
                raise AccessError("Search did not expose one editable provider; no coordinate fallback")
            search_value = pattern(boxes[0], "ValuePattern")
            if search_value.IsReadOnly or search_value.Value not in ("", who):
                raise AccessError("Search has existing input or is read-only; refusing to overwrite")
            if not search_value.Value:
                if not search_value.SetValue(who, waitTime=0.8) or search_value.Value != who:
                    raise AccessError("Search ValuePattern failed")
            else:
                audit["reused_exact_search"] = True
            audit["stage"] = "search_results"
            selected = wait_for(lambda: exact_search_item(client_roots(pid, hwnd), who, audit))
            if selected is None:
                root = auto.ControlFromHandle(hwnd)
                audit["control_summary"] = safe_summary(root)
                raise AccessError("No unique exact result from control search")
        audit["stage"] = "chat_selection"
        audit["chat_selection_attempted"] = True
        root = navigate(selected, lambda: refreshed(lambda current: any(
            target_input(c, who) for c in walk(current))), "Exact chat item", audit=audit,
            allow_control_input=allow_control_input)
        audit["target_chat_open_confirmed"] = True
    audit["stage"] = "chat_input"
    editor = find_one(root, lambda control: target_input(control, who), "Exact target chat input")
    audit["target_chat_open_confirmed"] = True
    value = pattern(editor, "ValuePattern")
    if value.IsReadOnly or value.Value:
        raise AccessError("Chat input is read-only or has an existing draft; refusing to overwrite")
    button = find_one(root, lambda control: control.ControlTypeName == "ButtonControl"
                      and control.Name in ("发送", "发送(S)"), "Send button")
    if not allow_control_input:
        pattern(button, "InvokePattern")
    return value, button, editor


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


def send_once(db, username, value, button, text, audit, receipt, fingerprint, timeout=15.0,
              editor=None, allow_control_input=False):
    before = {row_identity(row) for row in db.get_messages(username, 100)}
    if freshness(db)["wal_merge_failed"]:
        raise AccessError("Baseline snapshot is stale; no message will be prepared")
    started = int(time.time())
    data = {"fingerprint": fingerprint, "phase": "preparing", "started": started,
            "baseline": [list(identity) for identity in before]}
    save_receipt(receipt, data)
    try:
        if value.IsReadOnly or value.Value:
            raise AccessError("Chat input changed after preflight; refusing to overwrite")
        audit["text_set_attempted"] = True
        try:
            audit["text_set_reported_success"] = bool(value.SetValue(text, waitTime=0.2))
        except Exception as exc:
            audit["text_set_error_type"] = type(exc).__name__
        if value.Value != text and allow_control_input and editor is not None and value.Value == "":
            click_control(editor, audit)
            if not editor.HasKeyboardFocus:
                raise AccessError("Exact target editor did not acquire keyboard focus")
            audit["keyboard"] = True
            editor.SendKeys(text, charMode=True, waitTime=0.3)
        if value.Value != text:
            raise AccessError("ValuePattern could not set and verify exact message text")
        audit["text_prepared"] = True
        if not wait_for(lambda: button.IsEnabled, timeout=2.0):
            raise AccessError("Send button remains disabled after verified text entry")
        data["phase"] = "send_invocation_started"
        save_receipt(receipt, data)
        audit["send_invoked"] = True
        try:
            if allow_control_input:
                audit["send_action"] = "uia_located_control_click"
                click_control(button, audit)
            else:
                audit["send_action"] = "invoke_pattern"
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
    return row.get("sort_seq"), row.get("local_id"), row.get("create_time"), row.get("shard")


def new_outgoing(rows, baseline, text, started):
    return [row for row in rows if row_identity(row) not in baseline
            and row.get("type") == "文本" and row.get("content") == text
            and row.get("is_outgoing") is True and row.get("create_time", 0) >= started - 2]


def reconcile_receipt(db, username, text, path, fingerprint, confirmed_record):
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("fingerprint") != fingerprint or saved.get("phase") != "send_result_uncertain":
        raise AccessError("Only the same uncertain operation can be reconciled")
    rows = db.get_messages(username, 100)
    if freshness(db)["wal_merge_failed"]:
        raise AccessError("Cannot reconcile from a stale snapshot")
    baseline = {tuple(identity) for identity in saved.get("baseline", [])}
    hits = new_outgoing(rows, baseline, text, saved["started"])
    hits = [row for row in hits if (row["sort_seq"], row["local_id"], row["create_time"]) == tuple(confirmed_record)
            and row["create_time"] >= saved["started"]]
    if len(hits) != 1:
        raise AccessError("Specified record is not a unique exact outgoing confirmation")
    saved.update({"phase": "local_outgoing_confirmed", "record": list(row_identity(hits[0])),
                  "reconciled_from": "send_result_uncertain", "sender_mapping": hits[0].get("sender_mapping")})
    save_receipt(path, saved)


def main():
    p = argparse.ArgumentParser(description="纯 Python UIA 控件发送实验；不使用 Computer Use 或截图")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--chat", default="filehelper")
    p.add_argument("--text", default="Codex 微信接口测试")
    p.add_argument("--allow-temporary-accessibility", action="store_true")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--allow-control-input", action="store_true",
                   help="单独获准后允许 UIA 定位点击及必要的正文按键；默认禁用")
    p.add_argument("--additional-chat", action="append", default=[],
                   help="明确获准的额外精确接收人；仅在文件传输助手成功后继续")
    p.add_argument("--reconcile-only", action="store_true", help="仅回读确认原不确定操作，不调用控件或发送")
    p.add_argument("--confirm-record", nargs=3, type=int, metavar=("SORT_SEQ", "LOCAL_ID", "CREATE_TIME"))
    p.add_argument("--request-id", help="实际发送必填；同一操作始终使用同一个 ID，禁止换 ID 自动重发")
    args = p.parse_args()
    if args.commit and (not args.request_id or not args.request_id.strip()):
        p.error("--commit requires a non-empty --request-id")
    if args.reconcile_only and (args.commit or args.additional_chat or not args.request_id or not args.confirm_record):
        p.error("--reconcile-only requires --request-id and --confirm-record, without --commit or additional chats")
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
            targets = [resolve_chat(db, query) for query in [args.chat, *args.additional_chat]]
            if args.additional_chat and (not args.commit or targets[0]["username"] != "filehelper"):
                raise AccessError("Additional recipients require a committed filehelper-first operation")
            if len({target["username"] for target in targets}) != len(targets):
                raise AccessError("Repeated recipient in the same operation")
            if args.reconcile_only:
                target = targets[0]
                path = receipt_path(config["account"], args.request_id)
                fingerprint = hashlib.sha256(json.dumps(
                    [config["account"], target["username"], args.text]).encode()).hexdigest()
                reconcile_receipt(db, target["username"], args.text, path, fingerprint, args.confirm_record)
                print(json.dumps({"experimental": True, "backend": "local_wcdb_receipt_reconciliation",
                                  "send_invoked": False, "local_outgoing_confirmed": True,
                                  "process_memory_writes": False}))
                return 0
            operations = []
            for index, target in enumerate(targets):
                who = target.get("remark") or target.get("nick_name") or target["username"]
                if target["username"] != "filehelper":
                    if resolve_chat(db, who)["username"] != target["username"]:
                        raise AccessError("Display name does not resolve to the requested exact account")
                operation = {"operation_index": index, "coordinates": False, "keyboard": False,
                             "send_invoked": False, "local_outgoing_confirmed": False}
                receipt = fingerprint = None
                if args.commit:
                    request_id = args.request_id if index == 0 else args.request_id + ":" + hashlib.sha256(
                        target["username"].encode()).hexdigest()
                    receipt = receipt_path(config["account"], request_id)
                    fingerprint = hashlib.sha256(json.dumps(
                        [config["account"], target["username"], args.text]).encode()).hexdigest()
                    if receipt.exists():
                        saved = json.loads(receipt.read_text(encoding="utf-8"))
                        if saved.get("fingerprint") == fingerprint and saved.get("phase") == "local_outgoing_confirmed":
                            matches = [row for row in db.get_messages(target["username"], 100)
                                       if list(row_identity(row)) == saved.get("record")
                                       and row.get("content") == args.text and row.get("is_outgoing") is True]
                            if len(matches) == 1 and not freshness(db)["wal_merge_failed"]:
                                operation["previously_confirmed"] = True
                                operation["local_outgoing_confirmed"] = True
                        if not operation["local_outgoing_confirmed"]:
                            check_receipt(receipt, fingerprint)
                operations.append((target, who, operation, receipt, fingerprint))
            audit["operations"] = [entry[2] for entry in operations]
            audit["control_input_authorized"] = args.allow_control_input
            if args.allow_control_input:
                audit["backend"] = "uia_located_control_input_experiment"
            with accessibility_gate(args.pid, args.allow_temporary_accessibility, audit):
                for target, who, operation, receipt, fingerprint in operations:
                    if operation["local_outgoing_confirmed"]:
                        continue
                    try:
                        value, button, editor = control_preflight(args.pid, who, operation, args.allow_control_input)
                        operation["preflight_passed"] = True
                        if args.commit:
                            send_once(db, target["username"], value, button, args.text, operation,
                                      receipt, fingerprint, editor=editor,
                                      allow_control_input=args.allow_control_input)
                    except Exception as exc:
                        operation["error"] = str(exc)
                        operation.setdefault("control_summary", safe_summary(main_window(args.pid)[1]))
                        raise
                audit["preflight_passed"] = True
    except Exception as exc:
        audit["error"] = str(exc)
        if hasattr(exc, "candidates"):
            audit["ambiguous_candidates"] = exc.candidates
    operations = audit.get("operations", [])
    audit["send_invoked"] = any(operation.get("send_invoked") for operation in operations)
    audit["coordinates"] = any(operation.get("coordinates") for operation in operations)
    audit["keyboard"] = any(operation.get("keyboard") for operation in operations)
    audit["confirmed_operation_count"] = sum(bool(operation.get("local_outgoing_confirmed")) for operation in operations)
    audit["local_outgoing_confirmed"] = bool(operations) and audit["confirmed_operation_count"] == len(operations)
    print(json.dumps(audit, ensure_ascii=False))
    return 0 if audit.get("preflight_passed") and "error" not in audit else 2


if __name__ == "__main__":
    raise SystemExit(main())
