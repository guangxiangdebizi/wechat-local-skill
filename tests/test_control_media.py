import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


scripts = Path(__file__).resolve().parents[1] / "scripts"
with mock.patch.object(sys, "path", [str(scripts), *sys.path]):
    spec = importlib.util.spec_from_file_location("control_media_trial", scripts / "try_control_send_media.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


class FakeClipboard:
    def __init__(self, initial=None, fail_drop=False):
        self.data = dict(initial or {13: "original text"})
        self.sequence = 1
        self.fail_drop = fail_drop

    def OpenClipboard(self):
        pass

    def CloseClipboard(self):
        pass

    def EnumClipboardFormats(self, previous):
        return next((fmt for fmt in sorted(self.data) if fmt > previous), 0)

    def GetClipboardData(self, fmt):
        data = self.data[fmt]
        if fmt == 15 and isinstance(data, bytes):
            offset = struct.unpack_from("<I", data)[0]
            return tuple(part for part in data[offset:].decode("utf-16-le").split("\0") if part)
        return data

    def EmptyClipboard(self):
        self.data.clear()
        self.sequence += 1

    def SetClipboardData(self, fmt, value):
        if fmt == 15 and self.fail_drop:
            self.fail_drop = False
            raise RuntimeError("clipboard write failed")
        self.data[fmt] = value
        self.sequence += 1

    def GetClipboardSequenceNumber(self):
        return self.sequence


class MediaTests(unittest.TestCase):
    @staticmethod
    def modules(clipboard):
        return mock.patch.dict(sys.modules, {"win32clipboard": clipboard,
                               "win32con": SimpleNamespace(CF_HDROP=15, CF_LOCALE=16)})

    def test_dropfiles_header_and_unicode_termination(self):
        raw = module.dropfiles(["C:/fixture/中文.png"])
        self.assertEqual(struct.unpack_from("<IiiII", raw), (20, 0, 0, 0, 1))
        self.assertEqual(raw[20:].decode("utf-16-le"), "C:/fixture/中文.png\0\0")

    def test_clipboard_text_restored_after_success_and_exception(self):
        for failing in (False, True):
            clipboard, audit = FakeClipboard(), {}
            with self.subTest(failing=failing), self.modules(clipboard):
                try:
                    with module.file_clipboard(Path("fixture.png"), audit):
                        self.assertEqual(clipboard.GetClipboardData(15), ("fixture.png",))
                        if failing:
                            raise ValueError("operation failed")
                except ValueError:
                    pass
            self.assertEqual(clipboard.data, {13: "original text"})
            self.assertTrue(audit["clipboard_restored"])

    def test_setup_failure_restores_original_clipboard(self):
        clipboard, audit = FakeClipboard(fail_drop=True), {}
        with self.modules(clipboard):
            with self.assertRaises(RuntimeError):
                with module.file_clipboard(Path("fixture.png"), audit):
                    self.fail("setup should have failed")
        self.assertEqual(clipboard.data, {13: "original text"})
        self.assertTrue(audit["clipboard_restored"])

    def test_unsupported_handle_format_is_left_unchanged(self):
        clipboard = FakeClipboard({2: 12345})
        with self.modules(clipboard):
            with self.assertRaises(module.AccessError):
                with module.file_clipboard(Path("fixture.png"), {}):
                    self.fail("unsupported format should have failed")
        self.assertEqual(clipboard.data, {2: 12345})

    def test_user_clipboard_change_is_not_overwritten(self):
        clipboard, audit = FakeClipboard(), {}
        with self.modules(clipboard):
            with module.file_clipboard(Path("fixture.png"), audit):
                clipboard.EmptyClipboard()
                clipboard.SetClipboardData(13, "user changed clipboard")
        self.assertEqual(clipboard.data, {13: "user changed clipboard"})
        self.assertFalse(audit["clipboard_restored"])

    def test_only_unique_new_outgoing_images_can_confirm(self):
        old = {"sort_seq": 1, "local_id": 1, "create_time": 100, "type": "图片", "is_outgoing": True}
        new = dict(old, sort_seq=2, local_id=2, create_time=200)
        incoming = dict(new, local_id=3, is_outgoing=False)
        unknown = dict(new, local_id=4, is_outgoing=None)
        text = dict(new, local_id=5, type="文本")
        self.assertEqual(module.new_images([old, new, incoming, unknown, text], {module.row_identity(old)}, 200), [new])


if __name__ == "__main__":
    unittest.main()
