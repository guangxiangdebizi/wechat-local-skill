import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location("control_send_trial", Path(__file__).resolve().parents[1] / "scripts" / "try_control_send.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeControl:
    def __init__(self, name="", aid="", children=()):
        self.Name = name
        self.AutomationId = aid
        self.children = children

    def GetChildren(self):
        return self.children


class ControlSendTests(unittest.TestCase):
    def test_first_line_matching_is_exact(self):
        wanted = FakeControl("fixture\nold preview")
        wrong = FakeControl("fixture extra\npreview")
        root = FakeControl(children=[FakeControl(aid="MainView.session_list", children=[wanted, wrong])])
        self.assertIs(module.exact_item(root, "fixture", "session_list"), wanted)

    def test_ambiguous_results_are_rejected(self):
        root = FakeControl(children=[FakeControl(aid="search_list", children=[FakeControl("same"), FakeControl("same")])])
        with self.assertRaises(module.AccessError):
            module.exact_item(root, "same", "search_list")

    def test_text_elsewhere_is_not_a_session(self):
        root = FakeControl(children=[FakeControl(aid="message_list", children=[FakeControl("fixture")])])
        self.assertIsNone(module.exact_item(root, "fixture", "session_list"))

    def test_only_new_exact_outgoing_message_confirms(self):
        old = {"sort_seq": 1, "local_id": 1, "create_time": 100, "type": "文本", "content": "test", "sender_id": 2}
        baseline = {module.row_identity(old)}
        current = dict(old, sort_seq=2, local_id=2, create_time=200)
        incoming = dict(current, sender_id=3, local_id=3)
        substring = dict(current, content="test plus", local_id=4)
        stale = dict(current, create_time=90, local_id=5)
        self.assertEqual(module.new_outgoing([old, current, incoming, substring, stale], baseline, "test", 200), [current])


if __name__ == "__main__":
    unittest.main()
