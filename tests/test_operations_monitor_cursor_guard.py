"""Offline unit tests for #292 journal cursor guard; never call real systemd/PG."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "monitoring/operations/trad_bot_monitor.py"
spec = importlib.util.spec_from_file_location("trad_bot_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class FakeProc:
    def __init__(self, output):
        self.stdout = io.StringIO(output)
        self.returncode = None
        self.stopped = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped = True
        self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class JournalGapTests(unittest.TestCase):
    def check(self, line, selected=True):
        proc = FakeProc(line)
        with mock.patch.object(monitor.subprocess, "Popen", return_value=proc), \
             mock.patch.object(monitor.select, "select",
                               return_value=([proc.stdout], [], []) if selected
                               else ([], [], [])):
            monitor.verify_cursor_exists("test.service", "s=test")
        self.assertTrue(proc.stopped)

    def test_exact_cursor_is_accepted(self):
        self.check('{"__CURSOR":"s=test","MESSAGE":"ok"}\n')

    def test_different_cursor_fails_closed(self):
        with self.assertRaises(monitor.JournalGapError):
            self.check('{"__CURSOR":"s=other"}\n')

    def test_empty_output_fails_closed(self):
        with self.assertRaises(monitor.JournalGapError):
            self.check("")

    def test_invalid_json_fails_closed(self):
        with self.assertRaises(monitor.JournalGapError):
            self.check("not-json\n")

    def test_probe_timeout_fails_closed(self):
        with self.assertRaises(monitor.JournalGapError):
            self.check("", selected=False)

    def test_no_journal_processing_after_gap(self):
        with mock.patch.object(monitor, "verify_cursor_exists",
                               side_effect=monitor.JournalGapError("expired")), \
             mock.patch.object(monitor.subprocess, "Popen") as popen:
            with self.assertRaises(monitor.JournalGapError):
                monitor.read_journal("test.service", "s=expired")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
