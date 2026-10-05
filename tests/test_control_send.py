import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("control_send_trial", Path(__file__).resolve().parents[1] / "scripts" / "try_control_send.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeControl:
    def __init__(self, name="", aid="", children=(), providers=None):
        self.Name = name
        self.AutomationId = aid
        self.children = children
        self.providers = providers or {}
        self.ControlTypeName = "ListItemControl"
        self.ClassName = "fixture"
        self.IsEnabled = True

    def GetChildren(self):
        return self.children

    def GetPattern(self, kind):
        return self.providers.get(kind)


class FakeValue:
    IsReadOnly = False

    def __init__(self, value=""):
        self.Value = value
        self.writes = []

    def SetValue(self, value, waitTime=0):
        self.writes.append(value)
        self.Value = value
        return True


PATTERNS = ("ValuePattern", "InvokePattern", "SelectionItemPattern", "LegacyIAccessiblePattern")
FAKE_AUTO = SimpleNamespace(PatternId=SimpleNamespace(**{kind: kind for kind in PATTERNS}),
                            AccessibleSelection=SimpleNamespace(TakeFocus=1, TakeSelection=2))


class ControlSendTests(unittest.TestCase):
    def test_windows_line_endings_are_equivalent_without_ignoring_content(self):
        self.assertTrue(module.same_text("first\r\nsecond", "first\nsecond"))
        self.assertFalse(module.same_text("first\n\nsecond", "first\nsecond"))
        self.assertFalse(module.same_text("value 42", "value 24"))

    def test_prepared_resume_does_not_write_text_again(self):
        with tempfile.TemporaryDirectory() as directory:
            value, button, audit = FakeValue("test"), FakeControl(), {}
            db = mock.Mock()
            db.get_messages.side_effect = [[], [self.outgoing()]]
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}), \
                 mock.patch.object(module.time, "time", return_value=200), \
                 mock.patch.object(module, "invoke", return_value=True) as invoke:
                module.send_once(db, "fixture", value, button, "test", audit,
                                 Path(directory) / "receipt.json", "fingerprint", timeout=0,
                                 resume_prepared=True)
            self.assertTrue(audit["local_outgoing_confirmed"])
            self.assertEqual(value.writes, [])
            invoke.assert_called_once()

    def test_changed_prepared_draft_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            value, audit = FakeValue("user changed draft"), {}
            db = mock.Mock()
            db.get_messages.return_value = []
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}), \
                 mock.patch.object(module, "invoke") as invoke:
                with self.assertRaises(module.AccessError):
                    module.send_once(db, "fixture", value, FakeControl(), "test", audit,
                                     Path(directory) / "receipt.json", "fingerprint", timeout=0,
                                     resume_prepared=True)
            self.assertEqual(value.Value, "user changed draft")
            invoke.assert_not_called()

    def test_first_line_matching_is_exact(self):
        wanted = FakeControl("fixture\nold preview")
        wrong = FakeControl("fixture extra\npreview")
        root = FakeControl(children=[FakeControl(aid="MainView.session_list", children=[wanted, wrong])])
        self.assertIs(module.exact_item(root, "fixture", "session_list"), wanted)

    def test_ambiguous_results_are_rejected(self):
        root = FakeControl(children=[FakeControl(aid="search_list", children=[FakeControl("same"), FakeControl("same")])])
        with self.assertRaises(module.AccessError):
            module.exact_item(root, "same", "search_list")

    def test_ambiguity_diagnostics_do_not_disclose_names_or_ids(self):
        items = [FakeControl("private name", "private-id-one"),
                 FakeControl("private name", "private-id-two")]
        root = FakeControl(children=[FakeControl(aid="search_list", children=items)])
        with self.assertRaises(module.AmbiguousControlResults) as exc:
            module.exact_item(root, "private name", "search_list")
        self.assertEqual(len(exc.exception.candidates), 2)
        self.assertNotIn("private", json.dumps(exc.exception.candidates))

    def test_text_elsewhere_is_not_a_session(self):
        root = FakeControl(children=[FakeControl(aid="message_list", children=[FakeControl("fixture")])])
        self.assertIsNone(module.exact_item(root, "fixture", "session_list"))

    def test_ancestor_anchor_in_item_path_does_not_make_it_a_second_list(self):
        text = FakeControl("fixture")
        item = FakeControl("fixture", "MainView.session_list.item", children=[text])
        root = FakeControl(children=[FakeControl(aid="MainView.session_list", children=[item])])
        self.assertIs(module.exact_item(root, "fixture", "session_list"), item)

    def test_same_runtime_element_is_counted_only_once(self):
        item = FakeControl("fixture")
        item.GetRuntimeId = lambda: [1, 42]
        root = FakeControl(children=[FakeControl(aid="session_list", children=[item]),
                                     FakeControl(aid="MainView.session_list", children=[item])])
        self.assertIs(module.exact_item(root, "fixture", "session_list"), item)

    def test_offscreen_result_is_not_selected(self):
        item = FakeControl("fixture")
        item.IsOffscreen = True
        root = FakeControl(children=[FakeControl(aid="session_list", children=[item])])
        self.assertIsNone(module.exact_item(root, "fixture", "session_list"))

    def test_search_prefers_contact_over_same_id_recent_copy(self):
        recent = FakeControl("fixture", "search_item_identity")
        contact = FakeControl("fixture", "search_item_identity")
        root = FakeControl(aid="search_list", children=[FakeControl("最近使用"), recent,
                           FakeControl("联系人"), contact])
        audit = {}
        self.assertIs(module.exact_search_item(root, "fixture", audit), contact)
        self.assertEqual(len(audit["search_candidates"]), 2)
        self.assertEqual(audit["search_candidate_count_after_pruning"], 1)

    def test_different_search_ids_are_not_merged_by_name(self):
        root = FakeControl(aid="search_list", children=[FakeControl("联系人"),
                           FakeControl("same", "search_item_one"), FakeControl("same", "search_item_two")])
        with self.assertRaises(module.AmbiguousControlResults):
            module.exact_search_item(root, "same", {})

    def test_same_id_in_two_primary_rows_is_still_ambiguous(self):
        root = FakeControl(aid="search_list", children=[FakeControl("联系人"),
                           FakeControl("same", "search_item_one"), FakeControl("same", "search_item_one")])
        with self.assertRaises(module.AmbiguousControlResults):
            module.exact_search_item(root, "same", {})

    def test_search_headers_and_message_bodies_are_not_results(self):
        result = FakeControl("fixture", "Main.search_list.search_item_identity")
        root = FakeControl(aid="Main.search_list", children=[FakeControl("fixture"),
                           FakeControl("fixture", "message_body"), FakeControl("联系人"), result])
        self.assertIs(module.exact_search_item(root, "fixture", {}), result)

    def test_popup_roots_share_one_exact_search(self):
        result = FakeControl("fixture", "search_item_identity")
        popup = FakeControl(aid="search_list", children=[result])
        self.assertIs(module.exact_search_item([FakeControl(), popup], "fixture", {}), result)

    def test_only_terminal_editor_can_confirm_open_chat(self):
        editor = FakeControl("fixture", "Main.chat_input_field")
        editor.ControlTypeName = "EditControl"
        child = FakeControl("fixture", "Main.chat_input_field.child")
        child.ControlTypeName = "EditControl"
        self.assertTrue(module.target_input(editor, "fixture"))
        self.assertFalse(module.target_input(child, "fixture"))

    def test_only_new_exact_outgoing_message_confirms(self):
        old = {"sort_seq": 1, "local_id": 1, "create_time": 100, "type": "文本", "content": "test", "sender_id": 1,
               "is_outgoing": True}
        baseline = {module.row_identity(old)}
        current = dict(old, sort_seq=2, local_id=2, create_time=200)
        incoming = dict(current, sender_id=2, local_id=3, is_outgoing=False)
        substring = dict(current, content="test plus", local_id=4)
        stale = dict(current, create_time=90, local_id=5)
        self.assertEqual(module.new_outgoing([old, current, incoming, substring, stale], baseline, "test", 200), [current])

    def test_navigation_checks_effect_before_using_legacy(self):
        invoke = mock.Mock(Invoke=mock.Mock(return_value=True))
        legacy = mock.Mock(DefaultAction="activate", DoDefaultAction=mock.Mock(return_value=True))
        control = FakeControl(providers={"InvokePattern": invoke, "LegacyIAccessiblePattern": legacy})
        observe = mock.Mock(side_effect=[None, "new state"])
        with mock.patch.dict(sys.modules, {"uiautomation": FAKE_AUTO}):
            self.assertEqual(module.navigate(control, observe, "fixture", timeout=0), "new state")
        invoke.Invoke.assert_called_once()
        legacy.DoDefaultAction.assert_called_once()

    def test_navigation_success_code_without_effect_is_rejected(self):
        provider = mock.Mock(Invoke=mock.Mock(return_value=True))
        control = FakeControl(providers={"InvokePattern": provider})
        with mock.patch.dict(sys.modules, {"uiautomation": FAKE_AUTO}):
            with self.assertRaises(module.AccessError):
                module.navigate(control, lambda: None, "fixture", timeout=0)
        provider.Invoke.assert_called_once()

    def test_authorized_click_navigation_does_not_use_broken_patterns(self):
        control, audit = FakeControl(), {}
        with mock.patch.dict(sys.modules, {"uiautomation": FAKE_AUTO}):
            with mock.patch.object(module, "click_control") as click:
                result = module.navigate(control, lambda: "opened chat", "fixture", timeout=0,
                                         audit=audit, allow_control_input=True)
        self.assertEqual(result, "opened chat")
        click.assert_called_once_with(control, audit)
        self.assertTrue(audit["navigation_actions"][0]["effect_verified"])

    def test_authorized_click_without_chat_opening_still_fails(self):
        with mock.patch.dict(sys.modules, {"uiautomation": FAKE_AUTO}):
            with mock.patch.object(module, "click_control") as click:
                with self.assertRaises(module.AccessError):
                    module.navigate(FakeControl(), lambda: None, "fixture", timeout=0,
                                    audit={}, allow_control_input=True)
        click.assert_called_once()

    def test_occluded_control_is_not_clicked(self):
        control = FakeControl()
        control.IsOffscreen, control.ProcessId = False, 2
        control.BoundingRectangle = SimpleNamespace(left=1, top=2, right=20, bottom=30)
        control.Click = mock.Mock()
        gui = SimpleNamespace(WindowFromPoint=lambda point: 99)
        process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 3))
        with mock.patch.dict(sys.modules, {"win32gui": gui, "win32process": process}):
            with self.assertRaises(module.AccessError):
                module.click_control(control, {})
        control.Click.assert_not_called()

    def test_matching_visible_control_uses_its_uia_rectangle(self):
        control = FakeControl()
        control.IsOffscreen, control.ProcessId = False, 2
        control.BoundingRectangle = SimpleNamespace(left=1, top=2, right=20, bottom=30)
        control.Click = mock.Mock()
        gui = SimpleNamespace(WindowFromPoint=lambda point: 99)
        process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 2))
        audit = {}
        with mock.patch.dict(sys.modules, {"win32gui": gui, "win32process": process}):
            module.click_control(control, audit)
        control.Click.assert_called_once()
        self.assertEqual(audit["coordinate_source"], "uia_bounding_rectangle")

    def test_summary_redacts_names_and_account_ids(self):
        control = FakeControl("private name", "search_list.private-account-id")
        with mock.patch.dict(sys.modules, {"uiautomation": FAKE_AUTO}):
            result = module.safe_summary(control)
        self.assertEqual(result[0]["anchors"], ["search_list"])
        self.assertNotIn("private", json.dumps(result))

    def run_send(self, directory, rows, action=True, enabled=True, stale=None):
        path = Path(directory) / "receipt.json"
        value, button, audit = FakeValue(), FakeControl(), {}
        button.IsEnabled = enabled
        db = mock.Mock()
        db.get_messages.side_effect = rows
        freshness_results = stale or [{"wal_merge_failed": []}]
        fresh = mock.Mock(side_effect=freshness_results if stale else None,
                          return_value={"wal_merge_failed": []})
        effect = action if isinstance(action, Exception) else None
        with mock.patch.object(module, "invoke", return_value=action, side_effect=effect) as call:
            with mock.patch.object(module, "freshness", fresh), mock.patch.object(module.time, "time", return_value=200):
                try:
                    module.send_once(db, "fixture", value, button, "test", audit, path, "fingerprint", timeout=0)
                    error = None
                except module.AccessError as exc:
                    error = exc
        return path, value, audit, call, error

    @staticmethod
    def outgoing(local_id=2):
        return {"sort_seq": local_id, "local_id": local_id, "create_time": 200,
                "type": "文本", "content": "test", "sender_id": 1, "is_outgoing": True}

    def test_unknown_direction_cannot_confirm_even_for_old_reserved_id(self):
        row = dict(self.outgoing(), sender_id=2, is_outgoing=None)
        self.assertEqual(module.new_outgoing([row], set(), "test", 200), [])

    def test_reconciliation_updates_receipt_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            module.save_receipt(path, {"phase": "send_result_uncertain", "fingerprint": "original", "started": 200})
            db = mock.Mock()
            db.get_messages.return_value = [self.outgoing()]
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}), \
                 mock.patch.object(module, "invoke") as invoke, mock.patch.object(module, "click_control") as click:
                module.reconcile_receipt(db, "fixture", "test", path, "original", [2, 2, 200])
            self.assertEqual(json.loads(path.read_text())["phase"], "local_outgoing_confirmed")
            invoke.assert_not_called()
            click.assert_not_called()

    def test_send_confirms_even_when_provider_returns_false_or_raises(self):
        for action in (False, RuntimeError("provider failed after action")):
            with self.subTest(action=type(action).__name__), tempfile.TemporaryDirectory() as directory:
                path, value, audit, call, error = self.run_send(directory, [[], [self.outgoing()]], action)
                self.assertIsNone(error)
                self.assertTrue(audit["local_outgoing_confirmed"])
                self.assertEqual(value.writes, ["test"])
                self.assertEqual(json.loads(path.read_text())["phase"], "local_outgoing_confirmed")
                call.assert_called_once()

    def test_authorized_keyboard_fallback_and_send_click_are_verified(self):
        class EmptyValue(FakeValue):
            def SetValue(self, value, waitTime=0):
                return False

        with tempfile.TemporaryDirectory() as directory:
            value, button, audit = EmptyValue(), FakeControl(), {}
            editor = SimpleNamespace(HasKeyboardFocus=True, SendKeys=mock.Mock(
                side_effect=lambda text, **kwargs: setattr(value, "Value", text)))
            db = mock.Mock()
            db.get_messages.side_effect = [[], [self.outgoing()]]
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}), \
                 mock.patch.object(module.time, "time", return_value=200), \
                 mock.patch.object(module, "click_control") as click, \
                 mock.patch.object(module, "invoke") as invoke:
                module.send_once(db, "fixture", value, button, "test", audit,
                                 Path(directory) / "receipt.json", "fingerprint", timeout=0,
                                 editor=editor, allow_control_input=True)
            self.assertTrue(audit["keyboard"])
            self.assertTrue(audit["local_outgoing_confirmed"])
            self.assertEqual(click.call_args_list, [mock.call(editor, audit), mock.call(button, audit)])
            editor.SendKeys.assert_called_once_with("test", charMode=True, waitTime=0.3)
            invoke.assert_not_called()

    def test_keyboard_fallback_requires_exact_editor_focus(self):
        class EmptyValue(FakeValue):
            def SetValue(self, value, waitTime=0):
                return False

        with tempfile.TemporaryDirectory() as directory:
            value, audit = EmptyValue(), {}
            editor = SimpleNamespace(HasKeyboardFocus=False, SendKeys=mock.Mock())
            db = mock.Mock()
            db.get_messages.return_value = []
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}), \
                 mock.patch.object(module, "click_control") as click:
                with self.assertRaisesRegex(module.AccessError, "keyboard focus"):
                    module.send_once(db, "fixture", value, FakeControl(), "test", audit,
                                     Path(directory) / "receipt.json", "fingerprint", timeout=0,
                                     editor=editor, allow_control_input=True)
            editor.SendKeys.assert_not_called()
            click.assert_called_once()

    def test_uncertain_send_is_reserved_and_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, audit, call, error = self.run_send(directory, [[], []])
            self.assertIsInstance(error, module.AccessError)
            self.assertTrue(audit["send_invoked"])
            self.assertEqual(json.loads(path.read_text())["phase"], "send_result_uncertain")
            with self.assertRaises(module.AccessError):
                module.check_receipt(path, "fingerprint")
            self.assertEqual(value.writes, ["test"])
            call.assert_called_once()

    def test_disabled_send_restores_only_the_script_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, audit, call, error = self.run_send(directory, [[]], enabled=False)
            self.assertIsInstance(error, module.AccessError)
            self.assertEqual(value.writes, ["test", ""])
            self.assertTrue(audit["draft_cleanup_verified"])
            self.assertEqual(json.loads(path.read_text())["phase"], "failed_before_send")
            call.assert_not_called()

    def test_stale_baseline_never_prepares_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, audit, call, error = self.run_send(
                directory, [[]], stale=[{"wal_merge_failed": ["fixture"]}])
            self.assertIsInstance(error, module.AccessError)
            self.assertFalse(path.exists())
            self.assertEqual(value.writes, [])
            call.assert_not_called()

    def test_stale_outgoing_snapshot_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, audit, call, error = self.run_send(
                directory, [[], [self.outgoing()]],
                stale=[{"wal_merge_failed": []}, {"wal_merge_failed": ["fixture"]}])
            self.assertIsInstance(error, module.AccessError)
            self.assertFalse(audit.get("local_outgoing_confirmed", False))
            self.assertEqual(json.loads(path.read_text())["phase"], "send_result_uncertain")
            call.assert_called_once()

    def test_multiple_outgoing_records_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            path, value, audit, call, error = self.run_send(
                directory, [[], [self.outgoing(), self.outgoing(3)]])
            self.assertIsInstance(error, module.AccessError)
            self.assertFalse(audit.get("local_outgoing_confirmed", False))
            call.assert_called_once()

    def test_changed_operation_cannot_reuse_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            module.save_receipt(path, {"fingerprint": "original", "phase": "preparing"})
            with self.assertRaises(module.AccessError):
                module.check_receipt(path, "changed")

    def test_preexisting_draft_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            value, audit = FakeValue("user draft"), {}
            db = mock.Mock()
            db.get_messages.return_value = []
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}):
                with mock.patch.object(module, "invoke") as call:
                    with self.assertRaises(module.AccessError):
                        module.send_once(db, "fixture", value, FakeControl(), "test", audit,
                                         path, "fingerprint", timeout=0)
            self.assertEqual(value.Value, "user draft")
            self.assertEqual(value.writes, [])
            call.assert_not_called()

    def test_draft_cleanup_error_does_not_lose_the_receipt(self):
        class BrokenCleanupValue(FakeValue):
            def SetValue(self, value, waitTime=0):
                if not value:
                    raise RuntimeError("cleanup provider unavailable")
                return super().SetValue(value, waitTime)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            value, button, audit = BrokenCleanupValue(), FakeControl(), {}
            button.IsEnabled = False
            db = mock.Mock()
            db.get_messages.return_value = []
            with mock.patch.object(module, "freshness", return_value={"wal_merge_failed": []}):
                with self.assertRaisesRegex(module.AccessError, "Send button remains disabled"):
                    module.send_once(db, "fixture", value, button, "test", audit, path, "fingerprint", timeout=0)
            self.assertEqual(audit["draft_cleanup_error_type"], "RuntimeError")
            self.assertEqual(json.loads(path.read_text())["phase"], "failed_before_send")

    def test_commit_without_request_id_fails_before_opening_runtime(self):
        with mock.patch.object(sys, "argv", ["trial", "--pid", "1", "--runtime", "absent", "--commit"]):
            with mock.patch.object(module, "open_db") as open_db, mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit) as exc:
                    module.main()
        self.assertEqual(exc.exception.code, 2)
        open_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
