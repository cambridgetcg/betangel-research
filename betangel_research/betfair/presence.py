"""Operator presence: before each live action the operator retypes it on the controlling terminal.

What this proves: that something attached to this process's terminal typed the exact line shown,
after the prompt appeared (input typed ahead of the prompt is discarded first). What it does not
prove: that Yu was there. A process running as Yu's user can fake a pty and type the line, so this
is friction against accidental or unattended orders (scripts, cron, CI, agents without a terminal),
not cryptographic proof of a human. The prompt and the answer go through /dev/tty, never through
stdin or stdout, so piping input into the command cannot answer it.

An unanswered prompt is refused after answer_timeout_s (operator_echo_timeout), so a prompt left
on screen never holds anything open indefinitely.
"""

import os
import select
import sys
import termios
import time

from .errors import NotPresent, OrderRefused
from .filters import MARKET_ID_RE

MAX_ANSWER_BYTES = 1024
ANSWER_TIMEOUT_S = 120.0


def _two_dp(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer number of hundredths")
    return f"{value // 100}.{value % 100:02d}"


def order_echo_line(market_id, selection_id, side, price_h, size_p):
    """The line the operator types for one order, e.g. "1.234567 12345 LAY 1.01 1.00"."""
    if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
        raise ValueError("market_id must look like 1.<digits>")
    if type(selection_id) is not int or selection_id <= 0:
        raise ValueError("selection_id must be a positive integer")
    if side not in ("BACK", "LAY"):
        raise ValueError("side must be BACK or LAY")
    return f"{market_id} {selection_id} {side} {_two_dp(price_h, 'price_h')} {_two_dp(size_p, 'size_p')}"


def probe_echo_line(market_id, selection_id):
    """The one line typed for live-probe: "PROBE 1.234567 12345 LAY 1.01 1.00" (place, then cancel; the
    replace step was removed with live replace in v1)."""
    return "PROBE " + order_echo_line(market_id, selection_id, "LAY", 101, 100)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _read_line(fd, *, timeout_s=None):
    """One line from the terminal (terminator removed), or None at EOF, on bad UTF-8 or past the cap.

    With timeout_s, an answer not complete in time raises OrderRefused("operator_echo_timeout")."""
    data = bytearray()
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while b"\n" not in data:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            ready = select.select([fd], [], [], remaining)[0] if remaining > 0 else []
            if not ready:
                raise OrderRefused("operator_echo_timeout")
        chunk = os.read(fd, 256)
        if not chunk:
            return None
        data += chunk
        if len(data) > MAX_ANSWER_BYTES:
            return None
    try:
        return bytes(data).split(b"\n", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


class TtyConfirmer:
    """The I15 confirmer: require_present() and confirm_line(expected, banner=...)."""

    def __init__(self, *, tty_path="/dev/tty", stdin=None, answer_timeout_s=ANSWER_TIMEOUT_S):
        if isinstance(answer_timeout_s, bool) or not isinstance(answer_timeout_s, (int, float)) or not (
                0 < answer_timeout_s <= 3600):
            raise ValueError("answer_timeout_s must be a number of seconds in (0, 3600]")
        self.tty_path = tty_path
        self.stdin = stdin
        self.answer_timeout_s = answer_timeout_s

    def _stdin_is_tty(self):
        stdin = sys.stdin if self.stdin is None else self.stdin
        try:
            return bool(stdin.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def _open(self):
        try:
            fd = os.open(self.tty_path, os.O_RDWR | os.O_NOCTTY)
        except (OSError, TypeError, ValueError):
            raise NotPresent("operator_tty_required") from None
        if not os.isatty(fd):
            os.close(fd)
            raise NotPresent("operator_tty_required")
        return fd

    def require_present(self):
        """stdin is a terminal and the terminal device opens read-write; else NotPresent."""
        if not self._stdin_is_tty():
            raise NotPresent("operator_tty_required")
        os.close(self._open())

    def confirm_line(self, expected, *, banner):
        """Show banner and the line to type; the answer (stripped) must equal it exactly."""
        if not isinstance(expected, str) or not expected or "\n" in expected or "\r" in expected:
            raise ValueError("expected must be one non-empty line")
        if not isinstance(banner, str) or "\x00" in banner:
            raise ValueError("banner must be text")
        self.require_present()
        fd = self._open()
        try:
            try:
                termios.tcflush(fd, termios.TCIFLUSH)      # discard anything typed before the prompt
            except (termios.error, OSError):
                pass
            _write_all(fd, (banner + "\nType exactly: " + expected + "\n> ").encode("utf-8"))
            answer = _read_line(fd, timeout_s=self.answer_timeout_s)
        finally:
            os.close(fd)
        if answer is None or answer.strip() != expected:
            raise OrderRefused("operator_echo_mismatch")
