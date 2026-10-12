"""Operator presence: the typed echo on the terminal. Offline: a local pseudo-terminal stands in for
/dev/tty. These show the echo is exact and goes through the terminal, not that a human typed it."""

import os
import select
import threading
import time
import unittest

from betangel_research.betfair import presence
from betangel_research.betfair.errors import NotPresent, OrderRefused
from betangel_research.betfair.presence import TtyConfirmer, order_echo_line, probe_echo_line

import test_betfair_support as support


class _Stdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class PresenceTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.tty_path = os.ttyname(self.slave)

    def read_until(self, needle, timeout=5.0):
        data = b""
        deadline = time.monotonic() + timeout
        while needle not in data:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "prompt never appeared on the terminal")
            ready, _, _ = select.select([self.master], [], [], remaining)
            if ready:
                data += os.read(self.master, 4096)
        return data

    def confirm_in_thread(self, confirmer, expected, banner="MODE=LIVE test banner"):
        outcome = {}

        def run():
            try:
                confirmer.confirm_line(expected, banner=banner)
                outcome["ok"] = True
            except BaseException as exc:          # reported to the main thread
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def test_require_present_non_tty_refused(self):
        with self.assertRaises(NotPresent) as caught:
            TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(False)).require_present()
        self.assertEqual(caught.exception.rule, "operator_tty_required")
        with self.assertRaises(NotPresent):
            TtyConfirmer(tty_path=os.path.join(os.sep, "nonexistent-tty-for-test"), stdin=_Stdin(True)).require_present()

        class Broken:
            def isatty(self):
                raise ValueError("closed")

        with self.assertRaises(NotPresent):
            TtyConfirmer(tty_path=self.tty_path, stdin=Broken()).require_present()
        regular = os.path.join(support.REPO_ROOT, "README.md")
        with self.assertRaises(NotPresent):          # a regular file is not a terminal
            TtyConfirmer(tty_path=regular, stdin=_Stdin(True)).require_present()
        TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(True)).require_present()

    def test_echo_exact_match(self):
        line = order_echo_line("1.234567", 12345, "LAY", 101, 100)
        confirmer = TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(True))
        thread, outcome = self.confirm_in_thread(confirmer, line)
        shown = self.read_until(b"> ")
        self.assertIn(b"MODE=LIVE test banner", shown)
        self.assertIn(b"Type exactly: " + line.encode(), shown)
        os.write(self.master, ("  " + line + "  \n").encode())     # surrounding blanks are stripped
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, {"ok": True})

    def test_echo_mismatch_refused(self):
        line = order_echo_line("1.234567", 12345, "BACK", 350, 200)
        confirmer = TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(True))
        for answer in ("1.234567 12345 BACK 3.50 2.01", "1.234567 12345 back 3.50 2.00", "", "x" * 2000):
            with self.subTest(answer=answer[:30]):
                thread, outcome = self.confirm_in_thread(confirmer, line)
                self.read_until(b"> ")
                os.write(self.master, answer.encode()[:900] + b"\n")
                if len(answer) > 900:
                    os.write(self.master, answer.encode()[900:] + b"\n")
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(outcome.get("error"), OrderRefused)
                self.assertEqual(outcome["error"].rule, "operator_echo_mismatch")
                self.assertNotIn(line, str(outcome["error"]))
        # Input typed before the prompt appeared is discarded: only the answer after it counts.
        os.write(self.master, (line + "\n").encode())
        thread, outcome = self.confirm_in_thread(confirmer, line)
        self.read_until(b"> ")
        os.write(self.master, b"not the order\n")
        thread.join(5)
        self.assertIsInstance(outcome.get("error"), OrderRefused)
        with self.assertRaises(NotPresent):           # no terminal: refused before anything is shown
            TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(False)).confirm_line(line, banner="MODE=LIVE")
        with self.assertRaises(ValueError):
            confirmer.confirm_line("two\nlines", banner="MODE=LIVE")

    def pipe(self, data=b""):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        if data:
            os.write(write_fd, data)
        os.close(write_fd)
        return read_fd

    def test_answer_size_cap_and_timeout(self):
        # More than MAX_ANSWER_BYTES before any newline is no answer at all (refused as a mismatch).
        self.assertIsNone(presence._read_line(self.pipe(b"x" * (presence.MAX_ANSWER_BYTES + 100) + b"\n")))
        self.assertEqual(presence._read_line(self.pipe(b"x" * 1000 + b"\n")), "x" * 1000)
        self.assertIsNone(presence._read_line(self.pipe(b"\xff\xfe\n")))
        self.assertIsNone(presence._read_line(self.pipe()))                  # EOF
        # An unanswered prompt is refused after the timeout instead of waiting for ever.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        started = time.monotonic()
        with self.assertRaises(OrderRefused) as caught:
            presence._read_line(read_fd, timeout_s=0.05)
        self.assertEqual(caught.exception.rule, "operator_echo_timeout")
        self.assertLess(time.monotonic() - started, 2.0)
        confirmer = TtyConfirmer(tty_path=self.tty_path, stdin=_Stdin(True), answer_timeout_s=0.05)
        with self.assertRaises(OrderRefused) as caught:
            confirmer.confirm_line("1.1 1 LAY 1.01 1.00", banner="MODE=LIVE")
        self.assertEqual(caught.exception.rule, "operator_echo_timeout")
        for bad in (0, -1, True, "60", 7200):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                TtyConfirmer(tty_path=self.tty_path, answer_timeout_s=bad)

    def test_order_echo_line_format(self):
        self.assertEqual(order_echo_line("1.234567", 12345, "LAY", 101, 100), "1.234567 12345 LAY 1.01 1.00")
        self.assertEqual(probe_echo_line("1.234567", 12345), "PROBE 1.234567 12345 LAY 1.01 1.00")
        # Live replace was removed in v1: no replace echo line and no probe replace step exist.
        self.assertFalse(hasattr(presence, "replace_echo_line"))
        with self.assertRaises(TypeError):
            probe_echo_line("1.234567", 12345, replace_step=True)
        self.assertEqual(order_echo_line("1.1", 7, "BACK", 100000, 5000), "1.1 7 BACK 1000.00 50.00")
        for args in (("2.1", 1, "LAY", 101, 100), ("1.1", 0, "LAY", 101, 100), ("1.1", 1, "lay", 101, 100),
                     ("1.1", 1, "LAY", 1.01, 100), ("1.1", 1, "LAY", 101, 0), ("1.1", True, "LAY", 101, 100)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                order_echo_line(*args)


if __name__ == "__main__":
    unittest.main()
