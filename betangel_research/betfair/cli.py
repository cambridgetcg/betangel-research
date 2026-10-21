"""Betfair client command line: python3 -m betangel_research.betfair <command>.

Paper is the default for every order command; live needs --mode live on each order command and
every arming condition for that command in docs/betfair-order-path.md. A live place, live-probe,
settle-market, reconcile --accept or resume also needs --confirm-live TOKEN and the operator typing
a line at a terminal; a live cancel is a full cancel of our own bets, allowed while halted or killed,
and is sent at once with no typed line. --confirm-live without --mode live is refused
(confirm_live_needs_mode_live), so a forgotten --mode live never quietly acts on the paper journal.
Live replace and live partial cancels were removed in v1 (paper keeps both); settle-market (live
only, no --mode flag) settles one CLOSED market by hand, since nothing settles a live order
automatically. Output is one JSON document (record prints JSON lines); "decision" in an order result
is the risk decision, not Betfair's verdict: the orders' states and error codes say what happened.
Exit codes: 0 success; 2 input error or refusal, nothing sent ("Betfair input error: ..." or
"Betfair order refused: <rule>"); 3 kill_incomplete*; 4 an unexpected exception before any order
request ("Betfair stopped: <ClassName>; inspect local configuration/state"); 5 (live) the command
began an order request (place or cancel) and did not end as asked (amendment A3, X5), or a live
reconcile left an order UNKNOWN or ABSENT_UNCONFIRMED: one stderr line per order says what is true and
what to do, e.g. never resend an unknown place (run status or reconcile --mode live), a FAILED place
left nothing live, a bet the cancel did not take off is still live and can be cancelled again, a bet
Betfair lists only as cleared is not resting. While config coexistence.bet_angel is unverified every
live command a line names carries --bet-angel-paused. A place or live-probe whose open() found an
earlier UNKNOWN order live is refused (earlier_unknown_resolved_at_open, exit 2): it may be a resend.
No traceback, header, token or raw response text is ever printed.

--config, --state-root and the in-process deps work only under BETANGEL_BETFAIR_FORBID_NETWORK=1,
the test harness switch that also turns the network off.

Every gateway command installs SIGTERM, SIGINT and SIGHUP handlers that stop it with SignalStop, so
the gateway still closes (journal anchor, lock) and the session still logs out; the gateway itself
holds those signals back from just before an order request until its report is journaled. From the
first order request on (the gateway's on_send hook) the command defers them instead: the report,
the reconcile of an unknown outcome, the close and the logout still run, the result is printed and
the command exits 5 ("Betfair stopped after an order request was sent"). kill installs them right
after it starts and never defers them: a signal during a kill is kill_incomplete_interrupted, exit 3
with the website / Bet Angel advice, never exit 4. The kill never takes or waits for the gateway
lock (amendment A3, D4), and neither a journal it cannot read nor an invalid --profile stops it
engaging the KILL file; an unreadable journal does not stop its sweep either (config strategy ref).

record traps SIGTERM, SIGINT and SIGHUP (a closed terminal) as its very first statement: each only asks
the recorder to stop, so it closes the segment, writes the receipt and logs out, and record exits 0.
Before it reads a credential, record takes recording admission (admission.py): INHIBIT or a live
ADMISSION, another recorder holding .recorder.lock, or unreadable admission state refuses it with exit
2 and "Betfair recording refused: <outcome>" (a JSON "refused" line on stdout names the outcome).
recordings index and verify-day are offline and read only; their stdout carries counts only.
"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys

from .errors import BetfairError, CredentialError, OrderRefused, safe_code
from .paths import StatePaths, network_forbidden, overrides_allowed

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPS_KEYS = frozenset({"home", "keychain_runner", "http_transport", "stream_socket_factory", "clock", "confirmer",
                       "env"})
READ_STATUS = "betfair_read_response_personal_use_not_for_redistribution"
GITIGNORE_PATTERNS = ("*.crt", "*.key", "*.p12", "*.pfx", "*.pem")
LOGIN_RESET_PHRASE = "RESET LOGIN FAILURES"
LIVE_REPLACE_REMOVED = ("Betfair order refused: live_replace_removed_v1: live replace is not available in v1; nothing "
                        "was sent. To move a bet, cancel it (cancel --mode live --market M --bet-id B) and place a new "
                        "one (place --mode live ...).")
LIVE_PARTIAL_CANCEL_REMOVED = ("Betfair order refused: live_partial_cancel_removed_v1: live cancels are full "
                               "cancels in v1; nothing was sent. To reduce a bet, cancel it and place a new one.")
# Exit code 5 (amendment A3, X5): the command began an order request (place or cancel) and did not end as
# asked; one stderr line per order says what is true and what to do.
EXIT_SENT_NOT_AS_REQUESTED = 5
SENT_ADVICE = "do not resend; run status or reconcile --mode live"
WITHHELD_CODE = "exchange_reduction_withheld_until_settle"
CLEARED_CODE = "cleared_row_bet_exists_worst_case"
RESOLVED_AT_OPEN_RULE = "earlier_unknown_resolved_at_open"
# While config coexistence.bet_angel is unverified (until T3), A12 refuses every live command but kill without
# this flag, so every live command a line tells the operator to run carries it then ({flag} in the texts below).
PAUSED_FLAG = " --bet-angel-paused"
# Operator advice printed after a settle-market refusal (amendment A3 section 3.1).
SETTLE_ADVICE = {
    "settle_market_unresolved_orders": ("resolve them first with reconcile --mode live{flag}; an order still "
                                        "ABSENT_UNCONFIRMED after that needs reconcile --mode live{flag} --accept "
                                        "<ref> --confirm-live TOKEN; nothing was settled"),
    "settle_market_journal_changed": "the journal changed after you typed; nothing was settled; run the command again",
    "settle_market_book_read_failed": "the market could not be read; nothing was settled; try again",
    "settle_market_book_missing": ("Betfair no longer returns this market; see docs: its worst case stays counted; "
                                   "nothing was settled"),
    "settle_market_not_closed": "the market is not CLOSED yet; settle it after Betfair has settled it",
    "settle_market_current_read_failed": "our current orders could not be read; nothing was settled; try again",
    "settle_market_bets_still_current": ("check these bets' status on the Betfair website or in Bet Angel and settle "
                                         "again once Betfair has settled them"),
}
PAPER_LIVE_HELP = ("Paper by default; live needs --mode live, every arming condition in docs/betfair-order-path.md, "
                   "and typing the order at a terminal")
HELP = {
    "init-config": "Copy the example config to the profile path if absent; offline, writes nothing else",
    "doctor": "Offline checks of config, paths, cert files and Keychain item presence (never values); --network "
              "adds login, keepAlive and logout statuses only",
    "keychain-setup": "Print the Keychain commands to run by hand; runs nothing and reads no secret",
    "login-reset": "Clear the consecutive-login-failure block after typing RESET LOGIN FAILURES; offline",
    "event-types": "Read-only listEventTypes on the delayed key; personal use, no orders",
    "catalogue": "Read-only listMarketCatalogue on the delayed key; personal use, no orders",
    "book": "Read-only normalised market books on the delayed key; personal use, no orders",
    "orders-current": "Read-only listCurrentOrders; personal use, places nothing",
    "orders-cleared": "Read-only listClearedOrders; personal use, places nothing",
    "funds": "Read-only getAccountFunds; balances are personal data, never written to reports",
    "account": "Read-only whitelisted getAccountDetails (currency, country, discount, points); no names",
    "record": "Record the delayed-key market stream to local files; read-only, no orders; personal use only; "
              "refused while recording is inhibited for live work",
    "verify-recording": "Offline hash and replay check of one recording session",
    "recordings": "Offline, read-only structural index of one UTC day's recordings (index); the per-market index "
                  "stays in the state root and stdout carries counts only",
    "verify-day": "Offline, read-only check of one UTC day's recording sessions against their receipts and the "
                  "run ledger; prints counts and failed check names only",
    "place": PAPER_LIVE_HELP,
    "cancel": ("Cancel own bets by betId. Paper by default; live needs --mode live and A4, A5, A8, A10, A12, A13 plus "
               "a login; allowed while halted or killed; live cancels are full cancels (no --size-reduction); sent "
               "at once with no typed confirmation"),
    "replace": "Paper only: replace own paper bets at a new price; live replace was removed in v1",
    "live-probe": "Live only: LAY 1.00 @ 1.01, looked up by ref then cancelled at once (max loss 0.01), to verify "
                  "order refs; needs every "
                  "arming condition except A19 and typing at a terminal; the replace step was removed with live "
                  "replace in v1",
    "paper-step": "Paper only: advance paper orders on a new book; never sends anything",
    "paper-replay": "Offline deterministic paper simulation over recorded segments; not execution evidence",
    "reconcile": "Resolve unknown order outcomes by reading the exchange; never resends; live exchange evidence "
                 "never lowers counted exposure and settles nothing (use settle-market); --accept needs live "
                 "arming and typing at a terminal",
    "settle-market": "Live only: settle one CLOSED market by hand after checking the website; books each order's "
                     "worst case; needs --confirm-live TOKEN and --bet-angel-paused until T3",
    "journal": "Offline view and hash-chain check of the order journal",
    "status": "Offline summary: kill file, HALT, unresolved orders, rates, exposure, token inputs",
    "kill": "Engage the global kill file and HALT, then cancel our own unmatched bets (live) without waiting for "
            "any lock, checking again for about 35 s; matched bets cannot be undone; exit 3 means cancel open bets on "
            "the Betfair website or in Bet Angel now; --scope account --confirm-account-wide also cancels Bet "
            "Angel's orders",
    "resume": "Clear the HALT after the kill file was removed by hand; live needs --mode live --confirm-live TOKEN, "
              "then typing RESUME LIVE",
}


class _Done(Exception):
    """Internal: stop with an exit code and a safe stderr line."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SignalStop(BaseException):
    """SIGTERM, SIGINT or SIGHUP during a gateway command. A BaseException (like KeyboardInterrupt), so
    no ordinary error handler swallows it and every finally block (close, logout) still runs."""


class _SignalGuard:
    """SIGTERM, SIGINT and SIGHUP raise SignalStop while a gateway command runs. They are recorded and
    ignored instead (deferring) during cleanup, so a second signal cannot cut short the anchor write or
    the logout, and from the first order request on (order_sent, the gateway's on_send hook), so the
    report, step 15 and the result JSON still happen after the order went out (R3#16)."""

    def __init__(self):
        self.deferring = False
        self.sent = False
        self.pending = []
        self.previous = {}

    def handler(self, signum, frame):
        if self.deferring:
            self.pending.append(signum)
            return
        raise SignalStop(signal.Signals(signum).name)

    def order_sent(self):
        """on_send: an order request is about to go out; from now on signals are deferred."""
        self.sent = True
        self.deferring = True

    def install(self):
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                self.previous[sig] = signal.signal(sig, self.handler)
        except ValueError:          # not the main thread: the process default stays
            self.restore()

    def restore(self):
        previous, self.previous = self.previous, {}
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class _LiveBannerConfirmer:
    """Wraps the confirmer of a live command: MODE=LIVE goes to stderr before every prompt (spec section 9.1)."""

    def __init__(self, inner, err):
        self.inner = inner
        self.err = err

    def require_present(self):
        return self.inner.require_present()

    def confirm_line(self, expected, *, banner):
        self.err.write("MODE=LIVE\n")
        self.err.flush()
        return self.inner.confirm_line(expected, banner=banner)


class _Context:
    def __init__(self, deps, out, err):
        from .receipts import SYSTEM_CLOCK
        self.command = None             # the subcommand (main sets it)
        self.paused_flag = ""           # PAUSED_FLAG once a loaded config says coexistence is unverified
        self.deps = deps
        self.out = out
        self.err = err
        self.clock = deps.get("clock") or SYSTEM_CLOCK
        self.env = deps.get("env") if deps.get("env") is not None else os.environ
        self.http_transport = deps.get("http_transport")
        self.stream_socket_factory = deps.get("stream_socket_factory")
        self.confirmer_dep = deps.get("confirmer")
        runner = deps.get("keychain_runner")
        if runner is None and network_forbidden(deps.get("env")):
            runner = _harness_keychain_runner           # the harness never reaches /usr/bin/security
        self.keychain_runner = runner
        self.warnings = []
        self.signals = None             # the _SignalGuard of the running gateway command (_gateway)

    def order_sent(self):
        """True once the running command's gateway began an order request (its on_send hook ran)."""
        return self.signals is not None and self.signals.sent

    def confirmer(self):
        if self.confirmer_dep is not None:
            return self.confirmer_dep
        from .presence import TtyConfirmer
        return TtyConfirmer()

    def live_confirmer(self):
        return _LiveBannerConfirmer(self.confirmer(), self.err)

    def print_json(self, value):
        self.out.write(json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n")
        self.out.flush()


def _harness_keychain_runner(argv, **kwargs):
    raise OSError("keychain_disabled_under_forbid_network")


def _safe_text(exc):
    """Exception text fit for stderr and JSON: redacted when it holds a registered secret.

    The scan runs on the raw text, before whitespace is folded and the text cut to 240 characters:
    a secret holding a tab or newline, or one straddling the cut, would no longer match afterwards
    and would be printed (whole or as a prefix)."""
    try:
        raw = str(exc)
    except Exception:          # an exception whose __str__ fails is reported by class name only
        return type(exc).__name__
    text = raw if isinstance(exc, BetfairError) else f"{type(exc).__name__}: {raw}"
    folded = " ".join(text.split())[:240]
    from .secret import contains_registered
    try:
        # The folded form is checked too: folding can join a secret that holds a single space.
        if any(contains_registered(form) for form in (raw, text, folded)):
            return type(exc).__name__ + ": redacted"
    except TypeError:
        return type(exc).__name__
    return folded


def _csv(value, name):
    items = [item.strip() for item in (value or "").split(",") if item.strip()]
    if not items:
        raise ValueError(f"{name} needs at least one value")
    return items


# --- parser ------------------------------------------------------------------------------------


def _add_order_flags(sub, *, live_only=False):
    if not live_only:
        sub.add_argument("--mode", choices=["paper", "live"], default="paper")
    sub.add_argument("--confirm-live", dest="confirm_live")
    sub.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused",
                     help="operator states Bet Angel is logged out or not managing positions")


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair", description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="default")
    parser.add_argument("--config", help="test harness only (BETANGEL_BETFAIR_FORBID_NETWORK=1)")
    parser.add_argument("--state-root", dest="state_root", help="test harness only (BETANGEL_BETFAIR_FORBID_NETWORK=1)")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name):
        return commands.add_parser(name, help=HELP[name], description=HELP[name])

    add("init-config")
    doctor = add("doctor")
    doctor.add_argument("--network", action="store_true")
    doctor.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    keychain = add("keychain-setup")
    keychain.add_argument("--include-passphrase", action="store_true", dest="include_passphrase")
    add("login-reset")
    for name in ("event-types", "funds", "account"):
        sub = add(name)
        sub.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
        sub.add_argument("--out", action="store_true")
    catalogue = add("catalogue")
    catalogue.add_argument("--event-type", dest="event_type", required=True)
    catalogue.add_argument("--countries", required=True)
    catalogue.add_argument("--market-types", dest="market_types", required=True)
    catalogue.add_argument("--from", dest="from_utc")
    catalogue.add_argument("--to", dest="to_utc")
    catalogue.add_argument("--max", dest="max_results", type=int, default=100)
    catalogue.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    catalogue.add_argument("--out", action="store_true")
    book = add("book")
    book.add_argument("--market", required=True)
    book.add_argument("--price-data", dest="price_data", default="EX_BEST_OFFERS,EX_TRADED")
    book.add_argument("--depth", type=int)
    book.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    book.add_argument("--out", action="store_true")
    current = add("orders-current")
    current.add_argument("--market")
    current.add_argument("--strategy-ref", dest="strategy_ref")
    current.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    current.add_argument("--out", action="store_true")
    cleared = add("orders-cleared")
    cleared.add_argument("--status", required=True, choices=["SETTLED", "VOIDED", "LAPSED", "CANCELLED"])
    cleared.add_argument("--from", dest="from_utc", required=True)
    cleared.add_argument("--to", dest="to_utc")
    cleared.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    cleared.add_argument("--out", action="store_true")
    record = add("record")
    record.add_argument("--max-seconds", dest="max_seconds", type=float)
    record.add_argument("--end-at", dest="end_at", help="UTC instant (Z or offset) at which recording stops, on the "
                                                         "wall clock")
    record.add_argument("--invocation-id", dest="invocation_id",
                        help="written into recorder.pid, both receipts and the run-ledger row")
    record.add_argument("--require-delayed-key", action="store_true", dest="require_delayed_key",
                        help="refuse unless the session uses the delayed key")
    record.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    verify = add("verify-recording")
    verify.add_argument("--session", required=True)
    recordings = add("recordings")
    recordings_commands = recordings.add_subparsers(dest="recordings_command", required=True)
    index = recordings_commands.add_parser("index", help=HELP["recordings"], description=HELP["recordings"])
    index.add_argument("--day", required=True, help="UTC date YYYY-MM-DD")
    verify_day = add("verify-day")
    verify_day.add_argument("--day", required=True, help="UTC date YYYY-MM-DD")
    place = add("place")
    place.add_argument("--market", required=True)
    place.add_argument("--selection", required=True, type=int)
    place.add_argument("--side", required=True, choices=["BACK", "LAY"])
    place.add_argument("--price", required=True)
    place.add_argument("--size", required=True)
    place_book = place.add_mutually_exclusive_group()
    place_book.add_argument("--book")
    place_book.add_argument("--fetch-book", action="store_true", dest="fetch_book")
    _add_order_flags(place)
    cancel = add("cancel")
    cancel.add_argument("--market", required=True)
    cancel.add_argument("--bet-id", dest="bet_id", required=True)
    cancel.add_argument("--size-reduction", dest="size_reduction")
    _add_order_flags(cancel)
    replace = add("replace")
    replace.add_argument("--market", required=True)
    replace.add_argument("--bet-id", dest="bet_id", required=True)
    replace.add_argument("--new-price", dest="new_price", required=True)
    replace_book = replace.add_mutually_exclusive_group()
    replace_book.add_argument("--book")
    replace_book.add_argument("--fetch-book", action="store_true", dest="fetch_book")
    _add_order_flags(replace)
    probe = add("live-probe")
    probe.add_argument("--market", required=True)
    probe.add_argument("--selection", required=True, type=int)
    _add_order_flags(probe, live_only=True)
    step = add("paper-step")
    step.add_argument("--market", required=True)
    step_book = step.add_mutually_exclusive_group(required=True)
    step_book.add_argument("--book")
    step_book.add_argument("--fetch-book", action="store_true", dest="fetch_book")
    step.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    replay = add("paper-replay")
    replay.add_argument("--input", required=True)
    replay.add_argument("--orders", required=True)
    replay.add_argument("--out", required=True)
    reconcile = add("reconcile")
    reconcile.add_argument("--accept")
    _add_order_flags(reconcile)
    settle = add("settle-market")
    settle.add_argument("--market", required=True)
    _add_order_flags(settle, live_only=True)          # live only: no --mode flag at all
    journal = add("journal")
    journal.add_argument("--verify", action="store_true")
    journal.add_argument("--state")
    journal.add_argument("--mode", choices=["paper", "live"], default="paper")
    add("status")
    kill = add("kill")
    kill.add_argument("--scope", choices=["own", "account"], default="own")
    kill.add_argument("--confirm-account-wide", action="store_true", dest="confirm_account_wide")
    kill.add_argument("--reason", required=True)
    kill.add_argument("--bet-angel-paused", action="store_true", dest="bet_angel_paused")
    resume = add("resume")
    resume.add_argument("--mode", choices=["paper", "live"], default="paper")
    resume.add_argument("--confirm-live", dest="confirm_live")
    return parser


# --- shared setup ------------------------------------------------------------------------------


def _paths(args, ctx):
    return StatePaths(profile=args.profile, home=ctx.deps.get("home"), root_override=args.state_root,
                      env=ctx.deps.get("env"))


def _config(args, paths):
    from .config import load_config
    path = Path(args.config) if args.config else paths.config_path
    return load_config(path, paths=paths)


def _paused_flag(config):
    """PAUSED_FLAG while config coexistence.bet_angel is unverified, else ''."""
    return PAUSED_FLAG if config["coexistence"]["bet_angel"] == "unverified" else ""


def _coexistence(config, args):
    if config["coexistence"]["bet_angel"] == "unverified" and not getattr(args, "bet_angel_paused", False):
        raise _Done(2, "Betfair input error: bet_angel_coexistence_unverified; add --bet-angel-paused (you state Bet "
                       "Angel is logged out or not managing positions); kill needs no flag")


def _read_client(config, args, ctx, paths, *, purpose="read"):
    """(session, ReadClient) on the slot for purpose; the caller closes the session (logout)."""
    from .api import JsonRpcClient, ReadClient
    from .auth import SessionManager
    from .credentials import load_credentials
    _coexistence(config, args)
    credentials = load_credentials(config, purpose=purpose, profile=paths.profile, runner=ctx.keychain_runner)
    session = SessionManager(config, credentials, paths=paths, transport=ctx.http_transport, clock=ctx.clock,
                             coexistence_ack=bool(getattr(args, "bet_angel_paused", False)))
    rpc = JsonRpcClient(config, session, clock=ctx.clock)
    read = ReadClient(rpc, paths=paths, clock=ctx.clock,
                      catalogue_cache_max_age_s=config["read"]["catalogue_cache_max_age_s"])
    return session, read


def _load_book(path):
    from .receipts import strict_json_loads
    from .schemas import validate_book_snapshot
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        raise ValueError("book_file_unreadable") from None
    return validate_book_snapshot(strict_json_loads(text))


def _anchor_warning(code):
    """The stderr warning for a Keychain anchor publication that failed (gateway close, resume)."""
    if code == "journal_anchor_not_extended":
        return (f"{code}; the journal no longer extends the Keychain anchor (restored from an older copy?): the "
                "anchor was left ahead, so the next live command refuses (journal_anchor_mismatch); see "
                "docs/betfair-order-path.md section 7")
    return f"{code}; the Keychain anchor lags the journal until the next live command writes it"


@contextlib.contextmanager
def _gateway(args, ctx, *, command, mode, confirm=None):
    """An opened OrderGateway for one command; closes it (anchor, lock), logs out and closes the journal.

    confirm(gateway), live only, runs after static arming and before open(): the operator types the
    order lines then, while no lock is held (spec section 9.3 steps 2-4). SIGTERM, SIGINT and SIGHUP
    stop the command with SignalStop until the gateway begins an order request; from then on they are
    deferred (ctx.signals), so the report, step 15, the cleanup and the result still happen and the
    command then exits 5. The cleanup below always runs. A journal anchor that could not be written on
    close is reported on stderr (ctx.warnings).
    """
    from .gateway import OrderGateway
    from .journal import OrderJournal
    from .risk import RiskGuard
    paths = _paths(args, ctx)
    config = _config(args, paths)
    ctx.paused_flag = _paused_flag(config)
    paths.ensure()
    held = {"gateway": None, "session": None, "journal": None}
    signals = _SignalGuard()
    signals.install()
    ctx.signals = signals
    try:
        guard = RiskGuard(config, mode=mode, clock=ctx.clock, paths=paths)
        if mode == "paper":
            held["journal"] = OrderJournal(paths.journal_path(mode), mode=mode, clock=ctx.clock)
            read = None
            if getattr(args, "fetch_book", False):
                held["session"], read = _read_client(config, args, ctx, paths, purpose="read")
            held["gateway"] = OrderGateway(config, paths=paths, mode="paper", journal=held["journal"], guard=guard,
                                           read=read, clock=ctx.clock, env=dict(ctx.env))
            held["gateway"].open(command=command)
        else:
            _live_gateway(args, ctx, paths, config, guard, command=command, confirm=confirm, held=held,
                          on_send=signals.order_sent)
        yield held["gateway"], config, paths
    finally:
        signals.deferring = True
        try:
            if held["gateway"] is not None:
                closed = held["gateway"].close()
                if closed.get("anchor_error"):
                    ctx.warnings.append(_anchor_warning(closed["anchor_error"]))
        finally:
            try:
                if held["session"] is not None:
                    held["session"].close()
            finally:
                try:
                    if held["journal"] is not None:
                        held["journal"].close()
                finally:
                    signals.restore()


def _existing_journal(paths, mode, clock, *, busy_timeout_s=30):
    """The journal if its file exists (opening it creates nothing new), else None."""
    from .journal import OrderJournal
    path = paths.journal_path(mode)
    return OrderJournal(path, mode=mode, clock=clock, busy_timeout_s=busy_timeout_s) if path.exists() else None


def _journal_view(journal, now_ns):
    """(anchor, fold or None) of a journal that may be None (absent)."""
    from .errors import JournalError
    if journal is None:
        return {}, None
    try:
        return journal.anchor(), journal.fold(now_ns=now_ns)
    except JournalError:
        return journal.anchor(), None


def _live_gateway(args, ctx, paths, config, guard, *, command, confirm, held, on_send=None):
    """Static arming first (no secret is read, and no live journal created, before it passes), then
    credentials, the operator's typed lines (no lock held), login and open. Fills held as it goes.
    on_send goes to the gateway, except for the kill (it never defers a signal, amendment A3 A16)."""
    from .api import JsonRpcClient, ReadClient
    from .arming import static_arming
    from .auth import SessionManager
    from .credentials import load_credentials
    from .errors import NotArmed
    from .gateway import OrderGateway
    from .journal import OrderJournal
    kill = command in ("kill", "kill-account")
    if not kill:
        _coexistence(config, args)          # kill needs A4 and A13 only (spec section 9.2)
    confirmer = ctx.live_confirmer()
    now_ns = ctx.clock.time_ns()
    # The kill's static A13 is the state root only (amendment A3, D4): no journal is opened for it here.
    existing = None if kill else _existing_journal(paths, "live", ctx.clock)
    try:
        anchor, journal_state = _journal_view(existing, now_ns)
        static = static_arming(config, command=command, paths=paths, cli_mode="live",
                               confirm_token=getattr(args, "confirm_live", None),
                               bet_angel_paused=bool(getattr(args, "bet_angel_paused", False)), env=dict(ctx.env),
                               confirmer=confirmer, keychain_runner=ctx.keychain_runner, journal_anchor=anchor,
                               now_ns=now_ns, journal=existing,
                               journal_state=journal_state if existing is None or journal_state is not None
                               else {"halted": True})          # an unfoldable journal reads as halted (A7)
    finally:
        if existing is not None:
            existing.close()
    if not static["armed"]:
        ctx.print_json({"mode": "live", "command": command, "armed": False, "failures": static["failures"]})
        raise NotArmed(static["failures"][0], detail={"failures": static["failures"]})
    ctx.err.write("MODE=LIVE\n")
    ctx.err.flush()
    held["journal"] = (_kill_journal(paths, ctx.clock) if kill
                       else OrderJournal(paths.journal_path("live"), mode="live", clock=ctx.clock))
    if kill:
        _kill_line(ctx, "reading credentials from the Keychain; if macOS asks, click Allow (not Always Allow)")
    credentials = load_credentials(config, purpose="live_orders", profile=paths.profile, runner=ctx.keychain_runner)
    held["session"] = session = SessionManager(config, credentials, paths=paths, transport=ctx.http_transport,
                                               clock=ctx.clock,
                                               coexistence_ack=bool(getattr(args, "bet_angel_paused", False)))
    rpc = JsonRpcClient(config, session, clock=ctx.clock)
    read = ReadClient(rpc, paths=paths, clock=ctx.clock,
                      catalogue_cache_max_age_s=config["read"]["catalogue_cache_max_age_s"])
    gateway = OrderGateway(config, paths=paths, mode="live", journal=held["journal"], guard=guard, rpc=rpc, read=read,
                           confirmer=confirmer, clock=ctx.clock, env=dict(ctx.env), keychain_runner=ctx.keychain_runner,
                           on_send=None if kill else on_send)
    if confirm is not None:
        confirm(gateway)
    if kill:
        _kill_line(ctx, "logging in (emergency session)")
        if config["coexistence"]["bet_angel"] == "unverified":
            _kill_line(ctx, "F67 (inferred, unverified): if a new login ends other sessions, Bet Angel and any "
                            "running gateway may be logged out; check Bet Angel after the kill.")
    gateway.open(command=command, confirm_token=getattr(args, "confirm_live", None),
                 bet_angel_paused=bool(getattr(args, "bet_angel_paused", False)))
    held["gateway"] = gateway


def _kill_journal(paths, clock):
    """The kill's live journal connection, which waits only briefly for another writer (its events are a
    record, amendment A3 D4); or, when the file cannot be opened at all (not a database, no permission, another
    schema), an UnreadableJournal stand-in: the sweep still runs, on the config strategy ref only, and the kill
    ends incomplete. Nothing replaces or repairs the file."""
    from .journal import OrderJournal, UnreadableJournal
    from .killswitch import JOURNAL_BUSY_TIMEOUT_S
    try:
        return OrderJournal(paths.journal_path("live"), mode="live", clock=clock,
                            busy_timeout_s=JOURNAL_BUSY_TIMEOUT_S)
    except Exception:          # whatever opening it raised: never stops the sweep
        return UnreadableJournal("live")


def _sent_advice(flag=""):
    """What to do after a place whose outcome is unknown."""
    return SENT_ADVICE + flag


def _withheld_line(view, flag=""):
    """An order Betfair shows (partly) cancelled, lapsed or voided while the journal keeps its worst case (D3)."""
    return (f"Betfair order {view['customer_order_ref']} bet {view['bet_id'] or 'unknown'}: Betfair shows part or all "
            f"of it cancelled, lapsed or voided ({WITHHELD_CODE}); it stays counted at worst case and shows as "
            f"EXECUTABLE until you settle its market with settle-market{flag} after the market closes; status and "
            f"reconcile change nothing; cancel it again only if orders-current{flag} still shows an unmatched part")


def _cleared_line(view, flag=""):
    """An order whose bet Betfair lists only among its cleared orders (reconcile's live evidence rule, D3): the
    bet exists but is not current, and the journal keeps its worst case until settle-market."""
    return (f"Betfair order {view['customer_order_ref']} bet {view['bet_id'] or 'unknown'}: Betfair lists this bet "
            f"only among its cleared orders (lapsed, cancelled, voided or settled), not as a current bet "
            f"({CLEARED_CODE}); it is not resting; it stays counted at worst case and shows as EXECUTABLE until you "
            f"settle its market with settle-market{flag} after the market closes; status and reconcile change "
            f"nothing; cancel it again only if orders-current{flag} still shows an unmatched part")


def _evidence_line(view, flag=""):
    """The withheld or cleared-row line for an EXECUTABLE order carrying one of those codes, else None."""
    if view["state"] == "EXECUTABLE" and view["error_code"] == WITHHELD_CODE:
        return _withheld_line(view, flag)
    if view["state"] == "EXECUTABLE" and view["error_code"] == CLEARED_CODE:
        return _cleared_line(view, flag)
    return None


def _outcome_line(view, send_exception=None, flag=""):
    """One stderr line for an order that did not end as asked (X5). send_exception: the place result's
    journaled exception codes, which say why a FAILED order was not sent."""
    ref, state, code = view["customer_order_ref"], view["state"], view["error_code"] or "none"
    if state == "FAILED":
        cause = (send_exception or {}).get("code")
        why = f" ({cause})" if cause and cause != code else ""
        if cause == "kill_engaged_before_send":
            return (f"Betfair order not live: {ref} FAILED {code}{why}; nothing was sent: a kill is engaged, so do "
                    f"not place again until the kill has been dealt with")
        return (f"Betfair order not live: {ref} FAILED {code}{why}; nothing is live for this order; a fresh place is "
                f"safe")
    evidence = _evidence_line(view, flag)
    if evidence is not None:
        return evidence
    return f"Betfair order sent; outcome not as requested: {ref} {state} {code}; {_sent_advice(flag)}"


def _cancel_line(view, flag=""):
    """One stderr line for a cancel target that did not end as asked. A cancel may always be sent again (it
    can only take our own bet off), so it never says "do not resend"; a bet still live says so."""
    from .schemas import UNRESOLVED_STATES
    ref, state, code = view["customer_order_ref"], view["state"], view["error_code"]
    bet = view["bet_id"] or "unknown"
    evidence = _evidence_line(view, flag)
    if evidence is not None:
        return evidence
    if state == "EXECUTABLE" and view["size_remaining"] > 0:
        why = f" ({code})" if code else ""
        return (f"Betfair cancel not done: {ref} bet {bet} is still live with {view['size_remaining']:.2f} "
                f"unmatched{why}; run cancel again now, or cancel it on the Betfair website or in Bet Angel")
    if state in UNRESOLVED_STATES:
        return (f"Betfair cancel outcome unknown: {ref} bet {bet} {state} {code or 'none'}; it may still be live: "
                f"run reconcile --mode live{flag}, then cancel it again if it is still live, or cancel it on the "
                f"Betfair website or in Bet Angel")
    return (f"Betfair cancel outcome not as requested: {ref} bet {bet} {state} {code or 'none'}; cancel it again only "
            f"if orders-current{flag} still shows an unmatched part")


def _place_problems(views, send_exception=None, flag=""):
    """X5 for place: an order FAILED, UNKNOWN or ABSENT_UNCONFIRMED (any unresolved state), or one Betfair
    already shows reduced (withheld) or lists only among its cleared orders -> its lines."""
    from .schemas import UNRESOLVED_STATES
    return [_outcome_line(v, send_exception, flag) for v in views
            if v["state"] == "FAILED" or v["state"] in UNRESOLVED_STATES or v["error_code"] in (WITHHELD_CODE,
                                                                                               CLEARED_CODE)]


def _cancel_problems(views, flag=""):
    """X5 for a full cancel: a targeted order unresolved (UNKNOWN, CANCEL_PENDING, ...), carrying an error
    code, or still EXECUTABLE with size remaining -> its lines."""
    from .schemas import UNRESOLVED_STATES
    return [_cancel_line(v, flag) for v in views if v["state"] in UNRESOLVED_STATES or v["error_code"] is not None
            or (v["state"] == "EXECUTABLE" and v["size_remaining"] > 0)]


def _cancel_notes(views):
    """Informational lines (exit code unchanged): a target that matched, in part or whole, before the cancel."""
    return [f"Betfair cancel: {v['customer_order_ref']} bet {v['bet_id'] or 'unknown'} matched "
            f"{v['size_matched']:.2f} before the cancel; nothing of it remains unmatched"
            for v in views if v["state"] == "EXECUTION_COMPLETE" and v["size_matched"] > 0]


def _stopped_line(what, command, flag, *, result_printed):
    """The line for a command stopped (a signal, or an unexpected exception) after its gateway began an order
    request. Only a place is told not to resend; a cancel may always be sent again (the bet may still be live)."""
    shown = "; result above" if result_printed else ""
    if command == "cancel":
        return (f"Betfair stopped after a cancel request was sent ({what}){shown}; the bet may still be live: check "
                f"orders-current{flag} and cancel it again if it is, or cancel it on the Betfair website or in Bet "
                f"Angel")
    if command == "live-probe":
        return (f"Betfair stopped after an order request was sent ({what}){shown}; do not run the probe again; the "
                f"probe bet may still be live: check orders-current{flag} and cancel it, or cancel it on the Betfair "
                f"website or in Bet Angel")
    if result_printed:
        return f"Betfair stopped after an order request was sent ({what}); result above; do not resend"
    return f"Betfair stopped after an order request was sent ({what}); {_sent_advice(flag)}"


def _exit_after_send(ctx, lines):
    """Live, after the result JSON was printed: exit 5 with one line per order that did not end as asked
    (R3#15) and the signal deferred after the order went out (R3#16); else exit 0."""
    signals = ctx.signals
    if ctx.order_sent() and signals.pending:
        lines = lines + [_stopped_line(signal.Signals(signals.pending[0]).name, ctx.command, ctx.paused_flag,
                                       result_printed=True)]
    if lines:
        raise _Done(EXIT_SENT_NOT_AS_REQUESTED, "\n".join(dict.fromkeys(lines)))
    return 0


def _resolved_lines(result, flag=""):
    """earlier_unknown_resolved_at_open: one line per earlier order this command's open() found live."""
    markets = {item["customer_order_ref"]: item["market_id"]
               for item in ((result.get("refusal") or {}).get("detail") or {}).get("orders") or ()}
    return [f"Betfair: earlier order {v['customer_order_ref']} in market {markets.get(v['customer_order_ref'], '?')} "
            f"had an unknown outcome and Betfair shows it as a bet: {v['state']} bet {v['bet_id'] or 'unknown'} "
            f"matched {v['size_matched']:.2f} unmatched {v['size_remaining']:.2f}; this order was not sent: check "
            f"status and orders-current{flag}, then run the command again only if you still want it"
            for v in result.get("orders") or ()]


def _refused(result, flag=""):
    """The exit-2 message for a refused order result."""
    rule = safe_code((result.get("refusal") or {}).get("rule") or "refused")
    lines = [f"Betfair order refused: {rule}"]
    if rule == RESOLVED_AT_OPEN_RULE:
        lines += _resolved_lines(result, flag)
    elif result.get("mode") == "paper" and str(rule).startswith("validation:V2"):
        lines.append("this ran in paper mode (the default): for a live bet add --mode live")
    return _Done(2, "\n".join(lines))


def _order_result(ctx, result, *, action):
    ctx.print_json(result)
    if isinstance(result, dict) and result.get("decision") == "refused":
        raise _refused(result, ctx.paused_flag)
    if result.get("mode") != "live":
        return 0                                  # paper exit codes are unchanged (amendment A3, A9)
    views = result.get("orders") or []
    if action == "place":
        return _exit_after_send(ctx, _place_problems(views, result.get("send_exception"), ctx.paused_flag))
    for line in _cancel_notes(views):
        ctx.err.write(line + "\n")
    return _exit_after_send(ctx, _cancel_problems(views, ctx.paused_flag))


# --- offline commands --------------------------------------------------------------------------


def cmd_init_config(args, ctx):
    from .config import config_sha256, load_config
    paths = _paths(args, ctx)
    target = Path(args.config) if args.config else paths.config_path
    example = json.loads((REPO_ROOT / "research" / "betfair-config.example.json").read_text(encoding="utf-8"))
    example["profile"] = paths.profile
    status = "config_exists"
    if not target.exists():
        paths.ensure()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(example, indent=2, sort_keys=False) + "\n")
        status = "config_created"
    config = load_config(target, paths=paths)
    ctx.print_json({"status": status, "path": str(target), "config_sha256": config_sha256(config)})
    return 0


def cmd_keychain_setup(args, ctx):
    from .credentials import keychain_setup_commands
    paths = _paths(args, ctx)
    ctx.print_json({"status": "commands_printed_not_run",
                    "note": "Run each by hand; -w is last so security prompts for the value. The live key uses "
                            "-T \"\": answer Allow, never Always Allow (effect unverified, first-live-test T0).",
                    "commands": keychain_setup_commands(paths.profile, include_passphrase=args.include_passphrase)})
    return 0


def _gitignore_report():
    try:
        lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    except OSError:
        return {"checked": False}
    return {"checked": True, "covered": {pattern: pattern in lines for pattern in GITIGNORE_PATTERNS},
            "note": "informational: certificate files belong outside every worktree in any case"}


def cmd_doctor(args, ctx):
    from .arming import static_arming
    from .auth import ledger_status
    from .config import config_sha256
    from .credentials import SERVICES, anchor_read, check_cert_files, keychain_present
    from .errors import ConfigError
    paths = _paths(args, ctx)
    queries = {"ran": 0, "could_not_run": 0, "minimal_env": True}
    inner = ctx.keychain_runner

    def counting_runner(argv, **kwargs):
        """Counts whether /usr/bin/security could be run at all, and with the minimal environment."""
        from .credentials import minimal_env
        queries["minimal_env"] &= kwargs.get("env") == minimal_env()
        try:
            result = (inner if inner is not None else subprocess.run)(argv, **kwargs)
        except (OSError, subprocess.SubprocessError):
            queries["could_not_run"] += 1
            raise
        queries["ran"] += 1
        return result

    report = {"status": "doctor_report_not_health_guarantee", "paths": paths.describe(),
              "gitignore": _gitignore_report()}
    try:
        config = _config(args, paths)
    except ConfigError as exc:
        report["config"] = {"valid": False, "error": _safe_text(exc)}
        ctx.print_json(report)
        return 0
    report["config"] = {"valid": True, "config_sha256": config_sha256(config)}
    cred = config["credentials"]
    if cred["login_method"] == "cert" or cred["cert_path"] is not None:
        try:
            report["cert_files"] = check_cert_files(cred["cert_path"], cred["key_path"])
        except CredentialError as exc:
            report["cert_files"] = {"status": "cert_files_unsafe", "failures": exc.failures}
    kinds = [k for k in SERVICES if k != "cert_key_passphrase" or cred["key_passphrase_in_keychain"]]
    report["keychain_present"] = {kind: keychain_present(kind, paths.profile, runner=counting_runner)
                                  for kind in kinds}
    # True only when every presence query actually ran, under the minimal environment; "absent" from a
    # security binary that never started would otherwise look like a real answer.
    report["keychain_env_ok"] = (queries["ran"] == len(kinds) and queries["could_not_run"] == 0
                                 and queries["minimal_env"])
    report["keychain_queries"] = dict(queries)
    try:
        anchor = anchor_read(paths.profile, runner=ctx.keychain_runner)
        report["journal_anchor"] = {"status": "present" if anchor else "absent",
                                    "seq": anchor["seq"] if anchor else None}
    except CredentialError as exc:
        report["journal_anchor"] = {"status": "unreadable", "error": _safe_text(exc)}
    try:
        journal = _existing_journal(paths, "live", ctx.clock)
    except (OSError, sqlite3.Error, ValueError, BetfairError) as exc:
        journal = None
        report["live_journal"] = {"status": "unreadable", "error": type(exc).__name__}
    try:
        journal_anchor, journal_state = _journal_view(journal, ctx.clock.time_ns())
        arming = static_arming(config, command="place", paths=paths, cli_mode="live", confirm_token=None,
                               bet_angel_paused=False, env=dict(ctx.env), confirmer=None,
                               keychain_runner=ctx.keychain_runner, journal_anchor=journal_anchor,
                               now_ns=ctx.clock.time_ns(), journal_state=journal_state, journal=journal)
    finally:
        if journal is not None:
            journal.close()
    report["live_arming_static"] = {"failures": arming["failures"],
                                    "note": "confirm_token_mismatch and operator_tty_required are expected here"}
    try:
        report["login_ledger"] = ledger_status(config, paths, clock=ctx.clock)
    except BetfairError as exc:
        report["login_ledger"] = {"status": "unreadable", "error": _safe_text(exc)}
    if args.network:
        session, _ = _read_client(config, args, ctx, paths)
        try:
            login = session.login(emergency=False, coexistence_ack=bool(args.bet_angel_paused))
            keep = session.keep_alive()
            report["network"] = {"login_status": login["status"], "can_bet": login["can_bet"],
                                 "keep_alive_status": keep["status"], "keep_alive_error": keep["error"]}
            logout = session.logout()
            report["network"]["logout_status"] = logout["status"]
        finally:
            session.close()
    ctx.print_json(report)
    return 0


def cmd_login_reset(args, ctx):
    from .auth import append_login_reset
    paths = _paths(args, ctx)
    from .constants import LOGIN_BAN_S
    ctx.confirmer().confirm_line(LOGIN_RESET_PHRASE, banner=LOGIN_RESET_BANNER.format(ban_min=LOGIN_BAN_S // 60))
    ctx.print_json(append_login_reset(paths, clock=ctx.clock))
    return 0


def cmd_verify_recording(args, ctx):
    from .recorder import verify_recording
    result = verify_recording(Path(args.session))
    ctx.print_json(result)
    if result.get("status") != "verified":
        raise _Done(2, "Betfair input error: recording_failed_verification")
    return 0


def cmd_paper_replay(args, ctx):
    from .paper import run_paper_replay
    paths = _paths(args, ctx)
    config = _config(args, paths)
    inputs = [Path(item) for item in _csv(args.input, "--input")]
    report = run_paper_replay(inputs, Path(args.orders), config["orders"]["paper"], Path(args.out),
                              repo_root=REPO_ROOT, clock=ctx.clock)
    ctx.print_json({"status": report["status"], "output": str(args.out), "model": report["model"],
                    "orders": len(report["simulated_execution"]["orders"]),
                    "totals": report["simulated_execution"]["totals"], "economic_result": report["economic_result"],
                    "limitations": report["limitations"]})
    return 0


def _journal_summary(path, mode, clock, *, state_filter=None, verify=False):
    from .journal import OrderJournal
    from .risk import STATUS_EXPOSURE_BASIS, exposure_by_market
    if not Path(path).exists():
        return {"mode": mode, "status": "journal_absent"}
    journal = OrderJournal(path, mode=mode, clock=clock)
    now_ns = clock.time_ns()
    try:
        out = {"mode": mode, "status": "journal_present", "anchor": journal.anchor()}
        if verify:
            out["verify"] = journal.verify()
        state = journal.fold(now_ns=now_ns)
    finally:
        journal.close()
    orders = [o for o in state["orders"].values() if state_filter is None or o["state"] == state_filter]
    # Every order, as the risk guard's R8-R10 count them (a REPLACED bet's matched part stays at risk until
    # settled; live gives a matched portion no hedge credit, amendment A3 C1).
    exposure = exposure_by_market(list(state["orders"].values()), hedge_credit=(mode == "paper"))
    submitted = state["submitted_instruction_wall_ns"]
    rates = {"submitted_instructions_last_minute": sum(1 for t in submitted if t > now_ns - 60 * 10**9),
             "submitted_instructions_last_hour": sum(1 for t in submitted if t > now_ns - 3600 * 10**9),
             "orders_today": state["orders_today"], "exchange_floor": state["exchange_floor"],
             "transactions_by_clock_hour": state["transactions_by_clock_hour"]}
    out.update(halted=state["halted"], halt_reasons=state["halt_reasons"], clock_regression=state["clock_regression"],
               unresolved_markets=state["unresolved_markets"], orders_today=state["orders_today"], rates=rates,
               ref_roundtrip_verified=state["ref_roundtrip_verified"], exposure_pence_by_market=exposure,
               exposure_pence_total=sum(exposure.values()), exposure_basis=STATUS_EXPOSURE_BASIS[mode],
               orders=sorted(orders, key=lambda o: o["customer_order_ref"]))
    return out


def cmd_journal(args, ctx):
    paths = _paths(args, ctx)
    summary = _journal_summary(paths.journal_path(args.mode), args.mode, ctx.clock, state_filter=args.state,
                               verify=args.verify)
    ctx.print_json(summary)
    if args.verify and summary.get("verify", {}).get("status") == "broken":
        raise _Done(2, "Betfair input error: journal_chain_broken")
    return 0


def cmd_status(args, ctx):
    from .arming import ACK_IDS
    from .config import config_sha256
    from .errors import ConfigError
    from .paths import kill_engaged
    from .receipts import utc_from_ns
    paths = _paths(args, ctx)
    now_ns = ctx.clock.time_ns()
    out = {"status": "local_state_summary_not_health_guarantee", "kill_engaged": kill_engaged(paths),
           "utc_date": utc_from_ns(now_ns)[:10], "paths": paths.describe(), "default_mode": "paper"}
    try:
        config = _config(args, paths)
        live = config["orders"]["live"]
        out["config_sha256"] = config_sha256(config)
        out["confirm_token_inputs"] = {"config_sha256_prefix": out["config_sha256"][:12],
                                       "utc_date": out["utc_date"].replace("-", ""),
                                       "formula": "live-<config_sha256[:12]>-<UTC YYYYMMDD>"}
        out["live_config_checks"] = {"armed_flag": live["armed"],
                                     "acknowledgements_complete": sorted(live["acknowledgements"]) == sorted(ACK_IDS),
                                     "jurisdiction": config["jurisdiction"],
                                     "account_currency": config["account_currency"],
                                     "heartbeat_api_enabled": live["heartbeat_api_enabled"],
                                     "operator_attests_uk_egress": live["operator_attests_uk_egress"],
                                     "coexistence": config["coexistence"]["bet_angel"]}
    except ConfigError as exc:
        out["config"] = {"valid": False, "error": _safe_text(exc)}
    out["journals"] = {mode: _journal_summary(paths.journal_path(mode), mode, ctx.clock) for mode in ("paper", "live")}
    ctx.print_json(out)
    return 0


# --- read commands -----------------------------------------------------------------------------


def _read_command(args, ctx, call, *, command):
    from .config import config_sha256
    from .receipts import atomic_write_json, canonical_sha256, code_hashes, utc_compact_from_ns
    paths = _paths(args, ctx)
    config = _config(args, paths)
    session, read = _read_client(config, args, ctx, paths)
    try:
        started = ctx.clock.utc_now()
        result = call(read, config)
        received = ctx.clock.utc_now()
    finally:
        session.close()
    document = {"status": READ_STATUS, "key_type": read.key_type, "app_key_slot": read.app_key_slot,
                "request_started_at": started, "received_at": received, "result": result}
    if getattr(args, "out", False):
        root = Path(config["read"]["out_root"]) if config["read"]["out_root"] else paths.reads_dir
        request_id = f"read-{utc_compact_from_ns(ctx.clock.time_ns())}-{secrets.token_hex(4)}"
        directory = root / request_id
        paths.ensure()
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        response_sha = atomic_write_json(directory / "response.json", {"result": result})
        receipt = {"schema_version": 1, "status": READ_STATUS, "request_id": request_id, "command": command,
                   "key_type": read.key_type, "app_key_slot": read.app_key_slot, "request_started_at": started,
                   "received_at": received, "response_sha256": response_sha,
                   "result_canonical_sha256": canonical_sha256({"result": result}),
                   "config_sha256": config_sha256(config), "code_sha256": code_hashes(REPO_ROOT),
                   "timestamp_provenance": "local_wall_clock_utc_not_independently_verified",
                   "limitations": ["Betfair data for personal non-commercial use; keep outside Git",
                                   "receipt clocks are unauthenticated local clocks"]}
        atomic_write_json(directory / "receipt.json", receipt)
        document["out"] = str(directory)
    ctx.print_json(document)
    return 0


def cmd_event_types(args, ctx):
    return _read_command(args, ctx, lambda read, config: read.list_event_types(), command="event-types")


def cmd_catalogue(args, ctx):
    from .filters import to_rest_market_filter, validate_neutral_filter
    neutral = validate_neutral_filter({"event_type_ids": _csv(args.event_type, "--event-type"),
                                       "country_codes": _csv(args.countries, "--countries"),
                                       "market_types": _csv(args.market_types, "--market-types"),
                                       "market_ids": []})
    rest = to_rest_market_filter(neutral, start_from_utc=args.from_utc, start_to_utc=args.to_utc)
    return _read_command(args, ctx, lambda read, config: read.list_market_catalogue(rest, max_results=args.max_results),
                         command="catalogue")


def cmd_book(args, ctx):
    markets = _csv(args.market, "--market")
    price_data = tuple(_csv(args.price_data, "--price-data"))

    def call(read, config):
        if args.depth is None:
            return [read.market_snapshot(m, price_data=price_data, fetched_by="read_command") for m in markets]
        from .api import normalise_market_book
        rows = {m: read.catalogue_row(m) for m in markets}
        observed_at, monotonic_ns = ctx.clock.utc_now(), ctx.clock.monotonic_ns()    # before the request
        books = read.list_market_book(markets, price_data=price_data, depth=args.depth)
        return [normalise_market_book(book, catalogue_item=rows.get(book.get("marketId")), key_type=read.key_type,
                                      app_key_slot=read.app_key_slot, observed_at=observed_at,
                                      monotonic_ns=monotonic_ns, fetched_by="read_command") for book in books]
    return _read_command(args, ctx, call, command="book")


def cmd_orders_current(args, ctx):
    markets = _csv(args.market, "--market") if args.market else None
    refs = _csv(args.strategy_ref, "--strategy-ref") if args.strategy_ref else None
    return _read_command(args, ctx, lambda read, config: read.list_current_orders(
        market_ids=markets, customer_strategy_refs=refs), command="orders-current")


def cmd_orders_cleared(args, ctx):
    return _read_command(args, ctx, lambda read, config: read.list_cleared_orders(
        args.status, settled_from_utc=args.from_utc, settled_to_utc=args.to_utc), command="orders-cleared")


def cmd_funds(args, ctx):
    return _read_command(args, ctx, lambda read, config: read.get_account_funds(), command="funds")


def cmd_account(args, ctx):
    return _read_command(args, ctx, lambda read, config: read.get_account_details(), command="account")


class _RecordStop:
    """record's stop flag. SIGTERM, SIGINT and SIGHUP (a closed terminal, C1) only set it, so the recorder stops
    at its next check, closes the segment, writes the receipt and logs out."""

    SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

    def __init__(self):
        self.requested = False
        self.previous = {}

    @classmethod
    def install(cls):
        stop = cls()
        try:
            for sig in cls.SIGNALS:
                stop.previous[sig] = signal.signal(sig, stop.handler)
        except ValueError:          # not the main thread: the process default stays
            stop.restore()
        return stop

    def handler(self, signum, frame):
        self.requested = True

    def restore(self):
        previous, self.previous = self.previous, {}
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def cmd_record(args, ctx):
    stop = _RecordStop.install()      # the very first statement (C1, C5): a SIGTERM from admission is never lost
    try:
        return _record(args, ctx, stop)
    finally:
        stop.restore()


def _record(args, ctx, stop):
    """record: check the flags, take recording admission (admission.recorder_admission: INHIBIT, ADMISSION and
    .recorder.lock, then recorder.pid) and only then read credentials and run the recorder under that ticket."""
    from .admission import check_invocation_id, recorder_admission
    from .errors import AdmissionRefused
    from .esa import StreamClient
    from .receipts import parse_utc_ns
    from .recorder import StreamRecorder
    from .transport import open_stream_socket
    try:
        invocation_id = check_invocation_id(args.invocation_id)
    except ValueError:
        raise _Done(2, "Betfair input error: invocation_id_invalid; use 1-64 of A-Z a-z 0-9 . _ : - starting with a "
                       "letter or digit") from None
    end_at_ns = None
    if args.end_at is not None:
        try:
            end_at_ns = parse_utc_ns(args.end_at)
        except ValueError:
            raise _Done(2, "Betfair input error: end_at_invalid; give a UTC instant such as 2026-10-21T22:00:00Z") \
                from None
        if end_at_ns <= ctx.clock.time_ns():
            raise _Done(2, "Betfair input error: end_at_not_after_now; the recording window would not be positive")
    paths = _paths(args, ctx)
    config = _config(args, paths)
    if args.require_delayed_key and config["stream"]["app_key_slot"] != "delayed":
        raise _Done(2, "Betfair input error: recorder_delayed_key_required; stream.app_key_slot is not delayed")
    stream = config["stream"]
    factory = ctx.stream_socket_factory or open_stream_socket

    def emit(event):
        ctx.out.write(json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str) + "\n")
        ctx.out.flush()

    try:
        with recorder_admission(paths, invocation_id=invocation_id, clock=ctx.clock) as ticket:
            if stop.requested:          # a signal arrived before any credential was read: nothing to record
                emit({"summary": {"status": "not_started", "stop_reason": "stop_requested_before_start",
                                  "invocation_id": invocation_id}})
                return 0
            session, read = _read_client(config, args, ctx, paths, purpose="stream")

            def stream_factory():
                return StreamClient(host=stream["host"], port=stream["port"], session=session, socket_factory=factory,
                                    clock=ctx.clock, heartbeat_ms=stream["heartbeat_ms"],
                                    conflate_ms=stream["conflate_ms"], connect_timeout_s=stream["connect_timeout_s"],
                                    max_line_bytes=stream["max_line_bytes"])

            try:
                recorder = StreamRecorder(config, paths=paths, session=session, stream_factory=stream_factory,
                                          market_lister=read.precount_markets, clock=ctx.clock, emit=emit,
                                          invocation_id=invocation_id, require_delayed_key=args.require_delayed_key)
                receipt = recorder.run(stop_requested=lambda: stop.requested, max_seconds=args.max_seconds,
                                       end_at_ns=end_at_ns, admission=ticket)
            finally:
                session.close()
    except AdmissionRefused as exc:
        emit({"refused": {"outcome": exc.outcome, "detail": exc.detail, "owner": exc.owner,
                          "generation": exc.generation, "invocation_id": invocation_id}})
        raise _Done(2, f"Betfair recording refused: {exc}") from None
    emit({"summary": {"status": receipt.get("status"), "session_id": receipt.get("session_id"),
                      "complete": receipt.get("complete"), "segments": len(receipt.get("segments") or []),
                      "stop_reason": receipt.get("stop_reason"), "invocation_id": invocation_id}})
    return 0


def _recordings_root(paths, config):
    return Path(config["recorder"]["root"]) if config["recorder"]["root"] is not None else Path(paths.recordings_dir)


def cmd_recordings(args, ctx):
    """recordings index --day D: offline, read only (no credential, no network); writes the per-market index under
    the profile's recordings/index and prints counts only."""
    from .recordings import build_index, day_bounds
    try:
        day_bounds(args.day)
    except ValueError:
        raise _Done(2, "Betfair input error: day_invalid; give a UTC date YYYY-MM-DD") from None
    paths = _paths(args, ctx)
    config = _config(args, paths)
    ctx.print_json(build_index(paths, root=_recordings_root(paths, config), day=args.day, clock=ctx.clock))
    return 0


def cmd_verify_day(args, ctx):
    from .recordings import day_bounds, verify_day
    try:
        day_bounds(args.day)
    except ValueError:
        raise _Done(2, "Betfair input error: day_invalid; give a UTC date YYYY-MM-DD") from None
    paths = _paths(args, ctx)
    config = _config(args, paths)
    result = verify_day(paths, root=_recordings_root(paths, config), day=args.day)
    ctx.print_json(result)
    if result["status"] != "verified":
        raise _Done(2, "Betfair input error: day_failed_verification")
    return 0


# --- order commands ----------------------------------------------------------------------------


def _paper_book(args):
    if getattr(args, "book", None):
        return _load_book(args.book)
    return None


def cmd_place(args, ctx):
    from .errors import OrderRefused as Refused
    from .ticks import parse_money, parse_price
    instruction = {"selection_id": args.selection, "handicap": 0, "side": args.side,
                   "price_h": parse_price(args.price), "size_p": parse_money(args.size), "persistence_type": "LAPSE"}
    if args.mode == "live" and args.book:
        raise Refused("book_source_not_allowed_live")
    book = _paper_book(args) if args.mode == "paper" else None
    if args.mode == "paper" and book is None and not args.fetch_book:
        raise ValueError("paper place needs --book FILE or --fetch-book")
    def confirm(gateway):
        gateway.preconfirm("place", market_id=args.market, instructions=[instruction])

    with _gateway(args, ctx, command="place", mode=args.mode, confirm=confirm) as (gateway, config, paths):
        result = gateway.place(args.market, [instruction], book=book, source="cli_manual",
                               confirm_token=args.confirm_live)
    return _order_result(ctx, result, action="place")


def cmd_cancel(args, ctx):
    if args.mode == "live" and args.size_reduction is not None:
        # Live cancels are full cancels in v1: refused before parsing, the gateway, the Keychain, lock or login.
        raise _Done(2, LIVE_PARTIAL_CANCEL_REMOVED)
    from .ticks import parse_money
    bet_ids = _csv(args.bet_id, "--bet-id")
    reductions = None
    if args.size_reduction is not None:
        if len(bet_ids) != 1:
            raise ValueError("--size-reduction applies to exactly one --bet-id")
        reductions = [parse_money(args.size_reduction)]
    with _gateway(args, ctx, command="cancel", mode=args.mode) as (gateway, config, paths):
        result = gateway.cancel(args.market, bet_ids, source="cli_manual", size_reductions=reductions)
    return _order_result(ctx, result, action="cancel")


def cmd_replace(args, ctx):
    if args.mode == "live":
        # D2: refused before any price parsing, coexistence check, arming, journal, Keychain, lock or login.
        raise _Done(2, LIVE_REPLACE_REMOVED)
    from .ticks import parse_price
    replaces = [{"bet_id": args.bet_id, "new_price_h": parse_price(args.new_price)}]
    book = _paper_book(args)
    if book is None and not args.fetch_book:
        raise ValueError("paper replace needs --book FILE or --fetch-book")
    with _gateway(args, ctx, command="replace", mode="paper") as (gateway, config, paths):
        result = gateway.replace(args.market, replaces, book=book, source="cli_manual")
    return _order_result(ctx, result, action="replace")


def cmd_live_probe(args, ctx):
    def confirm(gateway):
        gateway.preconfirm("live-probe", market_id=args.market, selection_id=args.selection)

    with _gateway(args, ctx, command="live-probe", mode="live", confirm=confirm) as (gateway, config, paths):
        result = gateway.probe(args.market, args.selection, confirm_token=args.confirm_live)
    ctx.print_json(result)
    flag = ctx.paused_flag
    if result["place"]["decision"] != "accepted":
        raise _refused(result["place"], flag)
    placed = result["place"]["orders"]
    lines = _place_problems(placed, result["place"].get("send_exception"), flag)
    cancelled = (result.get("cancel") or {}).get("orders") or []
    final = cancelled[0] if cancelled else (placed[0] if placed else None)
    from .schemas import UNRESOLVED_STATES
    if final is not None and (final["state"] in UNRESOLVED_STATES or final["size_remaining"] > 0):
        # The probe bet was not taken off (R3#15): name it and say how to cancel it.
        lines.append(_cancel_line(final, flag) if cancelled else _outcome_line(final, flag=flag))
        bet = final["bet_id"]
        lines.append(f"Betfair live-probe: probe bet {bet} (LAY 1.00 @ 1.01) may still be live; cancel it now: cancel "
                     f"--mode live{flag} --market {args.market} --bet-id {bet}, or on the Betfair website or in Bet "
                     f"Angel"
                     if bet else
                     f"Betfair live-probe: probe order {final['customer_order_ref']} may still be live and its betId "
                     f"is unknown; run reconcile --mode live{flag} and check the Betfair website or Bet Angel")
    return _exit_after_send(ctx, lines)


def cmd_paper_step(args, ctx):
    book = _paper_book(args)
    with _gateway(args, ctx, command="place", mode="paper") as (gateway, config, paths):
        result = gateway.advance(args.market, book=book)
    withheld = sum("passive_fill_unavailable_delayed_rest_trd_empty" in flags for flags in result["flags"].values())
    if withheld:
        ctx.warnings.append(f"passive_fill_unavailable_delayed_rest_trd_empty ({withheld} order(s)): a "
                            "delayed-or-unknown REST interval had an empty traded ladder, which is not evidence that "
                            "nothing traded; for passive fills run record, then paper-replay on its segments")
    ctx.print_json(result)
    return 0


def cmd_reconcile(args, ctx):
    command = "reconcile-accept" if args.accept else "reconcile"

    def confirm(gateway):
        if args.accept:
            gateway.preconfirm("reconcile-accept", accept_ref=args.accept)

    with _gateway(args, ctx, command=command, mode=args.mode, confirm=confirm) as (gateway, config, paths):
        result = gateway.reconcile(accept_ref=args.accept, confirm_token=args.confirm_live)
    ctx.print_json(result)
    lines = ([_unresolved_line(item, ctx.paused_flag) for item in result.get("unresolved") or ()]
             if result.get("mode") == "live" else [])          # paper exit codes are unchanged (A9)
    if lines:
        # Live: an order whose outcome is still not known is not a success: exit 5, one line per order (X5).
        raise _Done(EXIT_SENT_NOT_AS_REQUESTED, "\n".join(lines))
    return 0


def _unresolved_line(item, flag=""):
    """One line for an order a live reconcile left UNKNOWN or ABSENT_UNCONFIRMED."""
    ref, state = item["customer_order_ref"], item["state"]
    head = (f"Betfair reconcile: {ref} in market {item['market_id']} is still {state} "
            f"({item['error_code'] or 'none'}); new orders stay blocked; do not resend")
    if state == "ABSENT_UNCONFIRMED":
        return (f"{head}; if the Betfair website and Bet Angel show no such bet, run reconcile --mode live{flag} "
                f"--accept {ref} --confirm-live TOKEN")
    return f"{head}; run reconcile --mode live{flag} again later, and check the Betfair website or Bet Angel"


def cmd_settle_market(args, ctx):
    """Live only: the operator settles one CLOSED market by hand (amendment A3, D3). The banner and the
    typed line SETTLE MARKET <M> come before the lock; a settle_market_* refusal prints its JSON and the
    advice, exit 2, and nothing is settled."""
    def confirm(gateway):
        gateway.preconfirm("settle-market", market_id=args.market)

    try:
        with _gateway(args, ctx, command="settle-market", mode="live", confirm=confirm) as (gateway, config, paths):
            result = gateway.settle_market(args.market, confirm_token=args.confirm_live)
    except OrderRefused as exc:
        if not exc.rule.startswith("settle_market_"):
            raise
        ctx.print_json({"mode": "live", "status": "refused", "market_id": args.market, "rule": exc.rule,
                        "detail": exc.detail})
        advice = SETTLE_ADVICE.get(exc.rule)
        raise _Done(2, f"Betfair order refused: {exc.rule}"
                    + (f"; {advice.format(flag=ctx.paused_flag)}" if advice else ""))
    ctx.print_json(result)
    return 0


KILL_ADVICE = "cancel open bets on the Betfair website or in Bet Angel now"
KILL_ENGAGED_LINE = ('KILL file engaged; no gateway sends a new order from now on. If this kill does not end with '
                     '"kill complete", cancel open bets on the Betfair website or in Bet Angel.')
KILL_COMPLETE_LINE = ("Betfair kill complete: none of our bets has an unmatched (EXECUTABLE) part on the final check. "
                      "A request delayed in the network can still arrive later: if an order was being sent when you "
                      "killed, check Bet Angel or the website.")
KILL_REQUESTED_LINE = ("cancels were requested for {count} bet(s) of ours (cancel_requested_bet_ids above); the kill "
                       "moves no order in the journal, so status still shows those it knows as EXECUTABLE, counted at "
                       "worst case, until you settle their market with settle-market{flag} after it closes; none has "
                       "an unmatched part left, so do not cancel them again")
# Login-ledger refusals of the kill's emergency login, each with what the operator can do. Only the failure cap is
# cleared by login-reset: a ban is never lifted by it, and a repair of an unreadable ledger starts a new ban.
LEDGER_ADVICE = {
    "login_failures_need_operator": ("the login ledger refused (login_failures_need_operator): check why logins "
                                     "failed, then login-reset"),
    "login_banned": ("the login ledger refused (login_banned): logins, the kill's included, are banned until {until}; "
                     "login-reset does not lift a ban"),
    "login_ledger_unreadable": ("the login ledger is unreadable (login_ledger_unreadable; a line torn by a crash?): "
                                "login-reset repairs it but starts a {ban_min}-minute login ban that also blocks the "
                                "kill's login"),
}
LOGIN_RESET_BANNER = ("Reset the persisted login-failure block. A login ban is not lifted. If the login ledger has an "
                      "unreadable line, the reset skips it and starts a {ban_min}-minute login ban that blocks every "
                      "login, the kill's emergency login included.")
# A valid profile name standing in for the state root alone (kill with an invalid --profile): the root and its KILL
# file are the same for every profile; nothing under profiles/ is touched.
ROOT_ONLY_PROFILE = "default"


def _kill_line(ctx, text):
    ctx.err.write(f"Betfair kill: {text}\n")
    ctx.err.flush()


def _journal_kill_incomplete(paths, clock):
    """HALT kill_incomplete into an existing live journal that has events; never raises, never waits for
    another writer longer than the kill's short busy timeout."""
    from .killswitch import JOURNAL_BUSY_TIMEOUT_S
    journal = None
    try:
        journal = _existing_journal(paths, "live", clock, busy_timeout_s=JOURNAL_BUSY_TIMEOUT_S)
        if journal is None:
            return "no_live_journal"
        if not journal.anchor():
            return "live_journal_empty"
        journal.append("halt", {"reason": "kill_incomplete", "scope": "global"})
        return "appended"
    except Exception:          # never stops or hides the exit 3
        return "failed"
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception:
                pass


def _ledger_detail(exc):
    """Extra stderr text when the login ledger refused the kill's login (LEDGER_ADVICE), else ''."""
    from .constants import LOGIN_BAN_S
    from .errors import AuthError
    if not isinstance(exc, AuthError) or exc.status not in LEDGER_ADVICE:
        return ""
    until = getattr(exc, "until_utc", None)
    return "; " + LEDGER_ADVICE[exc.status].format(until=safe_code(until) if until else "unknown",
                                                   ban_min=LOGIN_BAN_S // 60)


def _kill_invalid_profile(args, ctx):
    """kill with a --profile that is no valid profile name: STATE_ROOT/KILL, which is the same for every
    profile, is engaged all the same (a typo under stress must never skip it), then exit 3 with the advice.
    Nothing else runs: the journal, config and credentials belong to a profile."""
    from .killswitch import engage
    from .paths import kill_engaged
    root = StatePaths(profile=ROOT_ONLY_PROFILE, home=ctx.deps.get("home"), root_override=args.state_root,
                      env=ctx.deps.get("env"))
    out = {"scope": args.scope, "error": "invalid_profile"}
    try:
        out["kill"] = engage(root, args.reason, clock=ctx.clock)
    except OSError as exc:
        out["kill"] = {"status": "kill_file_not_engaged", "error": "failed:" + safe_code(type(exc).__name__)}
    out["status"] = "kill_incomplete_invalid_profile"
    out["kill_file_engaged"] = kill_engaged(root)
    ctx.print_json(out)
    state = ("the KILL file is engaged" if out["kill_file_engaged"]
             else "the KILL file could NOT be created, so other processes can still place orders")
    raise _Done(3, "Betfair kill incomplete: kill_incomplete_invalid_profile (--profile takes lower-case letters, "
                   f"digits and hyphens, at most 32; no bet was swept); {state}; {KILL_ADVICE}")


def _kill_detail(out):
    """Extra stderr text for an incomplete kill whose cause the operator must act on."""
    parts = []
    if "account_differs_from_journal" in (out.get("notes") or ()):
        parts.append("the Keychain login is not the journal's Betfair account (account_differs_from_journal), so "
                     "our bets on the journal's account were not swept")
    if (out.get("account_wide") or {}).get("reason") == "operator_tty_required":
        parts.append("the account-wide cancel was not sent: it needs a terminal (operator_tty_required)")
    return "".join("; " + part for part in parts)


def cmd_kill(args, ctx):
    """Kill file first (no config, lock or login), then the journal HALT, then the sweep (amendment A3, D4).

    The kill never takes or waits for .gateway.lock, and never waits for the journal longer than its short
    busy timeout (killswitch.JOURNAL_BUSY_TIMEOUT_S): a kill or halt it cannot append (locked by another
    writer, or refused) is noted and the sweep still runs. Nor does a damaged journal stop it: one that cannot
    be opened or read at all (not a database, no permission, a row SQLite cannot decode) is noted
    ("journal": "unreadable"), the gateway sweeps the config strategy ref only on an UnreadableJournal stand-in,
    and the kill ends kill_incomplete_journal_unreadable. An invalid --profile still engages the KILL file (it
    is the same for every profile) and ends kill_incomplete_invalid_profile. T0, read right after this process
    engaged the KILL file, times the re-sweeps (T0 + 20 s and T0 + 35 s); when this engage failed the gateway
    times them from its own engage instead. The CLI appends kill and halt only to a live journal that already
    has events: an absent or empty journal gets them from the gateway (which anchors them on close), so the
    kill never leaves a journal with events and no Keychain anchor behind. Static arming is A4 (kill-account
    adds A11) and the state root (A13); no Bet Angel statement is needed. --scope account without
    --confirm-account-wide, or without a terminal for its phrase (A11), still sweeps our own bets and then
    reports kill_incomplete. Anything but kill_complete with the KILL file engaged is exit 3 with the website
    / Bet Angel advice and a HALT kill_incomplete in the journal when it can be written, and so is SIGINT,
    SIGTERM or KeyboardInterrupt anywhere after the engage (kill_incomplete_interrupted). Progress lines go
    to stderr.
    """
    from .arming import static_arming
    from .config import load_config
    from .errors import ConfigError
    from .killswitch import JOURNAL_BUSY_TIMEOUT_S, engage
    from .paths import kill_engaged
    try:
        paths = _paths(args, ctx)
    except ConfigError as exc:
        if safe_code(str(exc)) != "invalid_profile":
            raise
        return _kill_invalid_profile(args, ctx)
    out = {"scope": args.scope}
    signals = _SignalGuard()          # from here on SIGINT and SIGTERM end the kill exit 3 with advice, never 4
    signals.install()

    def incomplete(reason, detail=""):
        signals.deferring = True      # a second signal must not cut the HALT, the JSON or the advice short
        out["status"] = reason
        if out.get("journal_halt_kill_incomplete") != "appended":
            # Every exit 3 journals HALT kill_incomplete when it can (amendment A3 section 4 step 10), unless
            # the gateway's kill already did.
            out["journal_halt_kill_incomplete"] = _journal_kill_incomplete(paths, ctx.clock)
        out["kill_file_engaged"] = kill_engaged(paths)
        ctx.print_json(out)
        state = ("the KILL file is engaged" if out["kill_file_engaged"]
                 else "the KILL file could NOT be created, so other processes can still place orders")
        raise _Done(3, f"Betfair kill incomplete: {reason}{detail}; {state}; {KILL_ADVICE}")

    def failed(exc):
        return "failed:" + safe_code(getattr(exc, "code", None) or type(exc).__name__)

    try:
        # The state root is repaired first, as every order command's _gateway() does: a root whose mode was
        # damaged must never leave the kill doing less than place or cancel would. Neither step may stop
        # the kill: a failure is reported and the journal HALT and the sweep are still tried.
        try:
            paths.ensure()
        except OSError as exc:
            out["state_root"] = failed(exc)
        try:
            engaged = engage(paths, args.reason, clock=ctx.clock)
        except OSError as exc:
            engaged = {"status": "kill_file_not_engaged", "error": failed(exc)}
        out["kill"] = engaged
        if engaged["status"] == "kill_file_not_engaged":
            # No T0 from here: when the file appears later (the gateway's engage, or another process), orders
            # could still be sent until then, so the gateway times the re-sweeps from its own engage.
            engaged_monotonic_ns = None
            _kill_line(ctx, "the KILL file could NOT be created, so other processes can still place orders; "
                            "cancel open bets on the Betfair website or in Bet Angel")
        else:
            engaged_monotonic_ns = ctx.clock.monotonic_ns()          # T0: the re-sweeps are timed from here
            _kill_line(ctx, KILL_ENGAGED_LINE)
        journal = None
        try:
            journal = _existing_journal(paths, "live", ctx.clock, busy_timeout_s=JOURNAL_BUSY_TIMEOUT_S)
            if journal is None:
                out["journal_halt"] = "no_live_journal_kill_file_only"
            elif not journal.anchor():
                out["journal_halt"] = "live_journal_empty_gateway_journals_it"
            else:
                try:
                    journal.append("kill", {"reason": engaged.get("reason", "kill"), "scope": args.scope})
                    journal.append("halt", {"reason": "kill", "scope": "global"})
                    out["journal_halt"] = "appended"
                except Exception as exc:
                    # Refused, another writer held the journal past the short busy timeout, or a damaged row: the
                    # KILL file still blocks every new send, and the sweep must not wait for the journal (D4).
                    out["journal_halt"] = failed(exc)
        except Exception as exc:
            # The journal cannot be opened or read at all: the sweep still runs, on the config strategy ref only.
            out["journal_halt"] = failed(exc)
            out["journal"] = "unreadable"
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass
        try:
            path = Path(args.config) if args.config else paths.config_path
            config = load_config(path, paths=paths)        # orders.live.kill_cancels_own_orders must be true
        except ConfigError as exc:
            out["error"] = _safe_text(exc)
            return incomplete("kill_incomplete_config_unreadable", f" ({safe_code(str(exc))})")
        ctx.paused_flag = _paused_flag(config)
        command = "kill-account" if args.scope == "account" and args.confirm_account_wide else "kill"
        account_wide_blocked = None

        def arming(name):
            # A4 (kill-account adds A11) and the state root: the kill's static A13 reads no Keychain anchor
            # and no journal head (its one anchor check runs in the gateway and never stops the sweep).
            return static_arming(config, command=name, paths=paths, cli_mode="live", confirm_token=None,
                                 bet_angel_paused=args.bet_angel_paused, env=dict(ctx.env),
                                 confirmer=ctx.live_confirmer(), keychain_runner=ctx.keychain_runner,
                                 journal_anchor={}, now_ns=ctx.clock.time_ns())

        # The session the kill used: "login" is filled in from the gateway once it has logged in.
        out["session"] = {"logged_in": False, "coexistence": config["coexistence"]["bet_angel"]}
        try:
            static = arming(command)
            if command == "kill-account" and static["failures"] == ["operator_tty_required"]:
                # No terminal for the account-wide phrase (A11): our own bets are still swept, as a plain kill
                # (D4); only the account-wide part fails closed (not sent), and the kill ends kill_incomplete.
                command, account_wide_blocked = "kill", "operator_tty_required"
                static = arming(command)
        except Exception as exc:          # never exit 4 on the kill path: exit 3 with the advice
            out["error"] = _safe_text(exc)
            return incomplete("kill_incomplete_not_armed")
        if not static["armed"]:
            out["arming_failures"] = static["failures"]
            return incomplete("kill_incomplete_not_armed")
        opened = False
        try:
            with _gateway(args, ctx, command=command, mode="live") as (gateway, _, _):
                opened = True
                out["session"].update(logged_in=True, login=gateway.login_kind)
                result = gateway.kill(scope=args.scope, reason=args.reason,
                                      confirm_account_wide=args.confirm_account_wide and command == "kill-account",
                                      engaged_monotonic_ns=engaged_monotonic_ns,
                                      progress=lambda text: _kill_line(ctx, text))
        except _Done:
            raise
        except Exception as exc:          # any failure (OSError included) is an incomplete kill: exit 3, JSON, advice
            out["error"] = _safe_text(exc)
            return incomplete("kill_incomplete" if opened else "kill_incomplete_no_session", _ledger_detail(exc))
        out.update({key: value for key, value in result.items() if key != "kill"})
        if account_wide_blocked is not None:
            out["account_wide"] = {"status": "not_sent", "reason": account_wide_blocked, "exception": None}
        if out["kill"].get("status") == "kill_file_not_engaged" and isinstance(result.get("kill"), dict):
            out["kill_retry"] = result["kill"]          # the gateway engaged it again after repairing the root
        if result.get("status") != "kill_complete":
            status = result.get("status") or "kill_incomplete"
            if out.get("journal") == "unreadable":
                status = "kill_incomplete_journal_unreadable"          # swept on the config strategy ref only
            return incomplete(status, _kill_detail(out))
        if not kill_engaged(paths):
            return incomplete("kill_incomplete_kill_file_not_engaged")
        signals.deferring = True
        ctx.print_json(out)
        ctx.err.write(KILL_COMPLETE_LINE + "\n")
        if out.get("cancel_requested_bet_ids"):
            _kill_line(ctx, KILL_REQUESTED_LINE.format(count=len(out["cancel_requested_bet_ids"]),
                                                       flag=ctx.paused_flag))
        return 0
    except (SignalStop, KeyboardInterrupt) as exc:
        signals.deferring = True
        out["interrupted"] = (exc.args[0] if isinstance(exc, SignalStop) and exc.args
                              else type(exc).__name__)
        return incomplete("kill_incomplete_interrupted")
    finally:
        signals.restore()


def cmd_resume(args, ctx):
    from .credentials import publish_anchor
    from .gateway import resume_journal
    from .journal import OrderJournal
    paths = _paths(args, ctx)
    config = _config(args, paths)
    paths.ensure()
    journal = OrderJournal(paths.journal_path(args.mode), mode=args.mode, clock=ctx.clock)
    try:
        if args.mode == "live":
            ctx.err.write("MODE=LIVE\n")
            ctx.err.flush()
        # resume_journal checks A13 in full (the anchored event's hash) before it appends, so the anchor
        # written here never blesses a rolled-back journal. It is published like every other anchor
        # (credentials.publish_anchor: serialized, the head read under its lock), after the gateway lock is
        # released, so a holder or kill that published a newer head is never overwritten (re-review R1).
        result = resume_journal(config, paths=paths, mode=args.mode, journal=journal,
                                confirmer=ctx.live_confirmer() if args.mode == "live" else None,
                                confirm_token=args.confirm_live, keychain_runner=ctx.keychain_runner,
                                env=dict(ctx.env), clock=ctx.clock)
        if args.mode == "live":
            try:
                result["anchor_written"] = publish_anchor(journal, paths, runner=ctx.keychain_runner,
                                                          clock=ctx.clock) is not None
            except CredentialError as exc:
                result["anchor_written"] = False
                ctx.warnings.append(_anchor_warning(safe_code(str(exc))))
    finally:
        journal.close()
    ctx.print_json(result)
    return 0


COMMANDS = {
    "init-config": cmd_init_config, "doctor": cmd_doctor, "keychain-setup": cmd_keychain_setup,
    "login-reset": cmd_login_reset, "event-types": cmd_event_types, "catalogue": cmd_catalogue, "book": cmd_book,
    "orders-current": cmd_orders_current, "orders-cleared": cmd_orders_cleared, "funds": cmd_funds,
    "account": cmd_account, "record": cmd_record, "verify-recording": cmd_verify_recording,
    "recordings": cmd_recordings, "verify-day": cmd_verify_day, "place": cmd_place,
    "cancel": cmd_cancel, "replace": cmd_replace, "live-probe": cmd_live_probe, "paper-step": cmd_paper_step,
    "paper-replay": cmd_paper_replay, "reconcile": cmd_reconcile, "settle-market": cmd_settle_market,
    "journal": cmd_journal, "status": cmd_status, "kill": cmd_kill, "resume": cmd_resume,
}


def main(argv=None, *, deps=None):
    """Run one command; returns the exit code. deps (tests only) needs BETANGEL_BETFAIR_FORBID_NETWORK=1."""
    out, err = sys.stdout, sys.stderr
    deps = dict(deps or {})
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if deps and (set(deps) - DEPS_KEYS or not overrides_allowed(deps.get("env"))):
        err.write("Betfair input error: deps_not_allowed\n")
        return 2
    if (args.config or args.state_root) and not overrides_allowed(deps.get("env")):
        err.write("Betfair input error: override_not_allowed\n")
        return 2
    if getattr(args, "confirm_live", None) is not None and getattr(args, "mode", "live") == "paper":
        # R3#18: a forgotten --mode live must not quietly act on the paper journal (resume, place, cancel).
        err.write("Betfair input error: confirm_live_needs_mode_live\n")
        return 2
    ctx = _Context(deps, out, err)
    ctx.command = args.command
    try:
        return COMMANDS[args.command](args, ctx)
    except _Done as done:
        err.write(done.message + "\n")
        return done.code
    except (KeyboardInterrupt, SignalStop, Exception) as exc:          # never a traceback or raw text
        if ctx.order_sent():
            # An order request went out before this stopped the command: never the "nothing sent" codes 2 or 4.
            err.write(_stopped_line(type(exc).__name__, ctx.command, ctx.paused_flag, result_printed=False) + "\n")
            return EXIT_SENT_NOT_AS_REQUESTED
        if isinstance(exc, OrderRefused):
            err.write(f"Betfair order refused: {exc.rule}\n")
            return 2
        if isinstance(exc, (BetfairError, ValueError)):
            err.write(f"Betfair input error: {_safe_text(exc)}\n")
            return 2
        err.write(f"Betfair stopped: {type(exc).__name__}; inspect local configuration/state\n")
        return 4
    finally:
        # Warnings never change the exit code: the command's own result (an order may have been sent)
        # is what the operator acts on, and a non-zero code there could invite a resend.
        for warning in ctx.warnings:
            err.write(f"Betfair warning: {warning}\n")
