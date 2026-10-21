"""RS-19 pilot placement through the order gateway (P1-06), offline and synthetic.

These drive the real OrderGateway, journal, risk guard, JSON-RPC client and read client over a simulated exchange
(test_betfair_support_pilot) and the pure pilot module, and show: source "pilot" only on the pilot path; PA1-PA4 at
the pilot-place open; the note §4 protocol (reserve before fetch, one raw live-key row and book, one classification
with capacity in the same compare-and-append, SKIP(interrupted)); R13 before any intent; the note §5 first-byte gate
with the fresh journal head; the note §9 latches and durable stops; the critique-13 manual-order and sweep rules; and
a 495-record journal's fold cost inside the book-age window in real time. Every flow that should be legitimate ends
with a pilot fold showing no protocol violation. They do not show Betfair's real behaviour, the P1-08 executor
(register, ARM, ACK, the scheduler, the remainder-cancel cases) or the P1-10 capture writers.
"""

import contextlib
import copy
import hashlib
import inspect
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

from betangel_research.betfair import api as api_module
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair import cli
from betangel_research.betfair import config as cfg
from betangel_research.betfair import gateway as gateway_module
from betangel_research.betfair import pilot, pilot_gate
from betangel_research.betfair.errors import (ConfigError, JournalError, NotArmed, OrderRefused, SecretLeak,
                                              TransportError)
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.receipts import canonical_sha256, utc_from_ns
from betangel_research.betfair.risk import RULE_ORDER, RiskGuard

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as orders
import test_betfair_support_pilot as sp
from test_betfair_pilot import C_CAP2, Pilot, bodies
from test_betfair_support import dumps, rpc

S = sp.S
OFF = sp.OFF
C = sp.C


def contract_bytes(**changes):
    """Test-only contract bytes: contract p1 with dotted keys replaced (a different sha256)."""
    parsed = json.loads(sp.CONTRACT_RAW)
    for path, value in changes.items():
        target = parsed
        *parents, last = path.split(".")
        for part in parents:
            target = target[part]
        target[last] = value
    return json.dumps(parsed, indent=2, sort_keys=True).encode() + b"\n"


# --- the pure additions to pilot.py (Q1, Q8, P106-3) ---------------------------------------------------------------


class PilotHelperTests(unittest.TestCase):
    def test_validate_event_columns_is_the_fold_rule(self):
        record = {"market_id": "1.5", "type": "decision"}
        pilot.validate_event_columns("decision", record, {"market_id": "1.5"})
        for columns in ({"market_id": "1.6"}, {"market_id": None}, {"market_id": "1.5", "state_after": "RECORDED"}):
            with self.subTest(columns=columns), self.assertRaises(pilot.PilotError):
                pilot.validate_event_columns("decision", record, columns)
        pilot.validate_event_columns("capture_c", {}, {"customer_order_ref": "ref-1"})
        self.assertRaises(pilot.PilotError, pilot.validate_event_columns, "capture_c", {}, {})
        pilot.validate_event_columns("refusal", {}, {})
        self.assertFalse(hasattr(pilot, "_validate_event_columns"))

    def test_next_trigger_exact_boundaries(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        placed = journal_.place(session)
        cutoff = placed.off + C.bsp_cutoff_s * S + 1
        deadline = placed.off + C.deadline_s * S + 1
        self.assertEqual(pilot.next_trigger_ns(journal_.fold(now=placed.off)), cutoff)
        self.assertEqual(pilot.next_trigger_ns(journal_.fold(now=cutoff - 1)), cutoff)
        self.assertEqual(pilot.next_trigger_ns(journal_.fold(now=cutoff)), deadline)       # fired at its own time
        self.assertEqual(pilot.next_trigger_ns(journal_.fold(now=deadline - 1)), deadline)
        self.assertIsNone(pilot.next_trigger_ns(journal_.fold(now=deadline)))
        self.assertIsNone(pilot.next_trigger_ns(Pilot(pt4=True).fold()))

    def test_placement_authority_reserve_codes(self):
        journal_ = Pilot(pt4=True)
        self.assertEqual(pilot.placement_authority(journal_.fold(), phase="reserve", now_ns=journal_.now),
                         "session_not_armed")
        session = journal_.arm(0, until_h=12)
        state = journal_.fold()
        self.assertIsNone(pilot.placement_authority(state, phase="reserve", now_ns=journal_.now))
        noon = pilot.parse_utc_ns(pilot.utc_day(journal_.now) + "T12:00:00Z")
        self.assertEqual(pilot.placement_authority(state, phase="reserve", now_ns=noon), "session_expired")
        journal_.stop("x-1", "review", "manual_check")
        self.assertEqual(pilot.placement_authority(journal_.fold(), phase="reserve", now_ns=journal_.now),
                         "session_ended")
        self.assertRaises(pilot.PilotError, pilot.placement_authority, state, phase="send", now_ns=journal_.now)
        self.assertRaises(pilot.PilotError, pilot.placement_authority, state, phase="other", now_ns=journal_.now)
        self.assertIsNotNone(session)

    def test_placement_authority_lets_only_the_cap_records_own_send_through(self):
        """P106-3: after the PLACE record that takes the last slot, placing_ending is lifetime_cap; the send phase
        lets that record (and only it) through, and refuses an older record and any reservation."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        first = journal_.place(session, off=off, open_id="open-a")
        cap = journal_.place(session, off=off + 30 * 60 * S, open_id="open-b")
        state = journal_.fold(live_open_id="open-b", contract=C_CAP2)
        self.assertEqual(pilot.placing_ending(state, now_ns=state.now_ns)["reason"], "lifetime_cap")
        self.assertIsNone(pilot.placement_authority(state, phase="send", now_ns=journal_.now, session_id=session,
                                                    decision_seq=cap.seq))
        self.assertEqual(pilot.placement_authority(state, phase="send", now_ns=journal_.now, session_id=session,
                                                   decision_seq=first.seq), "placing_ended")
        self.assertEqual(pilot.placement_authority(state, phase="reserve", now_ns=journal_.now), "placing_ended")
        # Every other ending still refuses the cap record: the final date, for one.
        self.assertEqual(pilot.placement_authority(state, phase="send", now_ns=C.final_date_ns, session_id=session,
                                                   decision_seq=cap.seq), "placing_ended")
        # Under the full cap the older record is still placeable but its open ended: refused all the same.
        full = journal_.fold(live_open_id="open-b")
        self.assertEqual(pilot.placement_authority(full, phase="send", now_ns=journal_.now, session_id=session,
                                                   decision_seq=first.seq), "record_open_ended")
        self.assertEqual(pilot.placement_authority(full, phase="send", now_ns=journal_.now, session_id=session,
                                                   decision_seq=999), "record_unknown")
        journal_.intent(cap)
        journal_.submit(cap)
        self.assertEqual(pilot.placement_authority(journal_.fold(live_open_id="open-b"), phase="send",
                                                   now_ns=journal_.now, session_id=session, decision_seq=cap.seq),
                         "record_submitted")

    def test_placing_ending_unchanged_by_the_refactor(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        journal_.place(session)
        journal_.place(session)
        state = journal_.fold(contract=C_CAP2)
        self.assertEqual(pilot.placing_ending(state, now_ns=state.now_ns)["reason"], "lifetime_cap")
        self.assertEqual(pilot.placing_ending(state, now_ns=C.final_date_ns)["reason"], "lifetime_cap")
        self.assertIsNone(pilot.placing_ending(journal_.fold(), now_ns=journal_.now))
        self.assertEqual(pilot.placing_ending(journal_.fold(), now_ns=C.final_date_ns)["reason"], "final_date")

    def test_refusal_records_carry_the_evidence_digests(self):
        """Q8: a classify refusal carries the digests of the exact bytes it was given (and validates)."""
        journal_ = Pilot(pt4=True)
        journal_.arm(0)
        result = pilot.classify(C, journal_.fold(), reservation_seq=999, open_id="o", session_id="s0",
                                registration_receipt_sha256=journal_.registration, customer_order_ref="r",
                                row_body=b"row", book_body=b"", observed_wall_ns=OFF, observed_monotonic_ns=1)
        record = result["refusal_record"]
        self.assertEqual((record["row_sha256"], record["book_sha256"]),
                         (hashlib.sha256(b"row").hexdigest(), hashlib.sha256(b"").hexdigest()))
        self.assertEqual(pilot.validate_pilot_payload({"pilot": record}), "refusal")
        bad = pilot.classify(C, journal_.fold(), reservation_seq=999, open_id="o", session_id="s0",
                             registration_receipt_sha256=journal_.registration, customer_order_ref="r",
                             row_body="row", book_body=b"", observed_wall_ns=OFF, observed_monotonic_ns=1)
        self.assertEqual((bad["refusal"], bad["refusal_record"]["row_sha256"]), ("caller_input_invalid", None))
        self.assertRaises(pilot.PilotError, pilot.validate_pilot_payload,
                          {"pilot": {k: v for k, v in record.items() if k != "row_sha256"}})

    def test_read_failure_gives_skip_evidence_invalid_with_the_received_digests(self):
        """Q8/Q10: a failed or out-of-bounds read is SKIP(evidence_invalid) on the exact bytes received; it can never
        turn into a PLACE, and an invalid code refuses as caller input."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        res = journal_.reserve("1.77", off, session=session, open_id="open-z")
        row_body, book_body = bodies("1.77", off)
        args = dict(reservation_seq=res, open_id="open-z", session_id=session,
                    registration_receipt_sha256=journal_.registration, customer_order_ref="ref-z", row_body=row_body,
                    book_body=book_body, observed_wall_ns=off - 295 * S, observed_monotonic_ns=7)
        state = journal_.fold(now=off - 295 * S, live_open_id="open-z")
        self.assertEqual(pilot.classify(C, state, **args)["record"]["outcome"], "PLACE")
        skipped = pilot.classify(C, state, read_failure="book_read_clock_drift", **args)["record"]
        self.assertEqual((skipped["outcome"], skipped["code"]), ("SKIP", "evidence_invalid"))
        self.assertEqual((skipped["row_sha256"], skipped["book_sha256"]),
                         (hashlib.sha256(row_body).hexdigest(), hashlib.sha256(book_body).hexdigest()))
        self.assertEqual(pilot.classify(C, state, read_failure="Bad Code", **args)["refusal"], "caller_input_invalid")


# --- pilot_gate (codes, latches, the first-byte predicate, identity readers) ---------------------------------------


class PilotGateTests(unittest.TestCase):
    CODES = (["validation:V" + str(n) for n in range(0, 15)] + ["validation:V9b"] + list(RULE_ORDER)
             + list(pilot.R13_CODES)
             + [code for code in pilot_gate.AUTHORITY_CODES.values()]
             + ["pa1_contract_unreadable", "pa1_contract_invalid", "pa1_contract_differs", "pa2_identity_unknown",
                "pa2_commit_differs", "pa2_tree_differs", "pa2_tree_dirty", "pa2_code_hashes_differ",
                "pa2_module_root_differs", "pa3_registration_unreadable", "pa3_config_differs",
                "pa3_config_not_contract", "capability_body_mismatch", "earlier_unknown_resolved_at_open",
                "lock_busy", "kill_file", "transport_not_sent"])

    def test_refusal_codes_give_valid_notes_and_stop_ids(self):
        """Design §2.10: every V, R, PA and R13 code maps to a refusal note and derived stop ids that validate."""
        for rule in self.CODES:
            code = pilot_gate.refusal_code(rule)
            with self.subTest(rule=rule):
                self.assertRegex(code, r"^[a-z0-9_]{1,60}$")
                note = pilot.pilot_note("refusal", C.sha256, session_id="s", open_id="open-ab", reservation_seq=123456,
                                        code=code, row_sha256=None, book_sha256=None)
                self.assertEqual(pilot.validate_pilot_payload(note), "refusal")
                derived = f"{pilot.stop_id_for(code, 123456)}-{9_999_999}"
                self.assertRegex(derived, pilot.STOP_ID_RE.pattern)
                for res in (123456, None):
                    stop_id = pilot_gate.session_stop_id(code, reservation_seq=res, open_id="open-0123456789abcdef")
                    stop = pilot.stop_payload(C, stop_id, "session", code, source_seq=res)
                    self.assertEqual(pilot.validate_pilot_payload({"pilot": stop}), "stop")
        self.assertEqual(pilot_gate.refusal_code("validation:V13"), "validation_v13")
        self.assertEqual(pilot_gate.refusal_code("validation:V9b"), "validation_v9b")
        # The literal rule text is what made the map necessary: it gives an invalid stop id.
        self.assertIsNone(pilot.STOP_ID_RE.fullmatch(pilot.stop_id_for("validation:v13", 5) + "-9"))

    def test_latch_table(self):
        """Note §9 with Q11 (Yu 2026-10-21) and critique 2, table-driven."""
        cases = {
            ("validation:V13", None): "market", ("validation:V9b", "market_time_passed"): "market",
            ("validation:V9", "market_not_open"): "market", ("validation:V9", "market_inplay"): "market",
            ("validation:V9", "runner_not_active"): "review", ("validation:V9", "catalogue_missing"): "review",
            ("rate_minute", None): "market", ("rate_hour", None): "market", ("rate_day", None): "session",
            ("daily_loss", None): "session", ("unresolved_orders", None): "session", ("lock_busy", None): "session",
            ("kill_file", None): "session", ("halted", "kill_engaged"): "session",
            ("halted", "kill_state_unknown"): "session", ("halted", "journal_halted"): "none",
            ("halted", "clock_regression"): "review", ("unexplained_account_exposure", None): "none",
            ("funds", "insufficient_available_to_bet"): "review", ("market_exposure", None): "market",
            ("total_exposure", None): "market", ("transport_not_sent", None): "review",
            ("send_gate_failed", None): "review", ("connect_failed", None): "review",
            ("INVALID_SESSION_INFORMATION", None): "review", ("pilot_gate_head_unreadable", None): "market",
            ("pilot_send_book_off_differs", None): "market", ("journal_changed_since_decision", None): "market",
            ("book_fetch_failed", None): "market", ("pilot_submit_too_late", None): "review",
            ("r13_body_differs", None): "review", ("pa2_tree_dirty", None): "review",
            ("capability_decision_stale", None): "review", ("transactions_hour", None): "review",
            ("stake_cap", None): "review", ("validation:V0", None): "review", ("validation:V14", None): "review",
            ("not_armed", None): "review", ("gateway_lock_lost", None): "review",
            ("earlier_unknown_resolved_at_open", None): "review"}
        for (rule, reason), expected in cases.items():
            with self.subTest(rule=rule, reason=reason):
                self.assertEqual(pilot_gate.latch_for(rule, reason), expected)

    def gate_ctx(self, **changes):
        ctx = {"submit_head": (10, "a" * 64), "submit_ns": 1000 * S, "seen_ns": 1000 * S, "dbw": 990 * S,
               "dbm": 50 * S, "end_ns": 1050 * S}
        ctx.update(changes)
        return ctx

    def test_first_byte_checks_fail_independently(self):
        head = {"seq": 10, "last_sha256": "a" * 64}
        ok = dict(wall_ns=1001 * S, mono_ns=61 * S, head=head)
        self.assertIsNone(pilot_gate.first_byte_refusal(self.gate_ctx(), **ok))
        cases = {
            "pilot_gate_head_moved": (self.gate_ctx(), dict(ok, head={"seq": 11, "last_sha256": "b" * 64})),
            "pilot_gate_deadline": (self.gate_ctx(), dict(ok, wall_ns=1050 * S, mono_ns=110 * S)),
            "pilot_gate_window": (self.gate_ctx(), dict(ok, wall_ns=1049 * S, mono_ns=111 * S)),
            "pilot_gate_clock_drift": (self.gate_ctx(), dict(ok, wall_ns=1004 * S + 1)),
        }
        for code, (ctx, kw) in cases.items():
            with self.subTest(code=code):
                self.assertEqual(pilot_gate.first_byte_refusal(ctx, **kw), code)
        for head_value in ({}, None, {"seq": 10, "last_sha256": "b" * 64}):
            self.assertEqual(pilot_gate.first_byte_refusal(self.gate_ctx(), **dict(ok, head=head_value)),
                             "pilot_gate_head_moved")
        # Exactly 2 s of drift is allowed; 1 ns more is not (in both directions).
        self.assertIsNone(pilot_gate.first_byte_refusal(self.gate_ctx(), **dict(ok, wall_ns=1003 * S)))
        self.assertEqual(pilot_gate.first_byte_refusal(self.gate_ctx(), **dict(ok, wall_ns=999 * S - 1)),
                         "pilot_gate_clock_drift")

    def test_check_4_takes_the_largest_observed_time(self):
        """The expiry-clock edge and P106-1: the submit's recorded_at, or any own event's, at or past the end
        refuses even when the wall clock has stepped back below it."""
        ok = dict(wall_ns=1001 * S, mono_ns=61 * S, head={"seq": 10, "last_sha256": "a" * 64})
        self.assertEqual(pilot_gate.first_byte_refusal(self.gate_ctx(submit_ns=1050 * S), **ok), "pilot_gate_deadline")
        self.assertEqual(pilot_gate.first_byte_refusal(self.gate_ctx(seen_ns=1050 * S), **ok), "pilot_gate_deadline")
        self.assertIsNone(pilot_gate.first_byte_refusal(self.gate_ctx(seen_ns=1050 * S - 1), **ok))

    def test_read_failure_bounds(self):
        read = {"error": None, "body": b"{}", "status": 200, "sent_wall_ns": 100 * S, "sent_monotonic_ns": 10 * S,
                "received_wall_ns": 101 * S, "received_monotonic_ns": 11 * S}
        limit = 5 * S
        self.assertIsNone(pilot_gate.read_failure(read, deadline_ns=limit))
        cases = {"read_deadline_exceeded": dict(error="deadline_exceeded", body=None),
                 "read_no_body": dict(body=None), "read_http_status": dict(status=503),
                 "read_elapsed_negative": dict(received_wall_ns=99 * S, received_monotonic_ns=9 * S),
                 "read_elapsed_over_deadline": dict(received_wall_ns=105 * S + 1, received_monotonic_ns=15 * S + 1),
                 "read_clock_drift": dict(received_wall_ns=104 * S + 1)}
        for code, changes in cases.items():
            with self.subTest(code=code):
                self.assertEqual(pilot_gate.read_failure(dict(read, **changes), deadline_ns=limit), code)
        # A monotonic clock that stopped (a sleep) while the wall clock ran on: over the deadline on the wall.
        self.assertEqual(pilot_gate.read_failure(dict(read, received_wall_ns=110 * S), deadline_ns=limit),
                         "read_elapsed_over_deadline")

    def test_config_holds_contract(self):
        paths = support.temp_state_paths(self)
        support.write_test_config(paths, support.deep_merge(gw.LIVE_OVERRIDES, sp.pilot_profile()))
        config = cfg.load_config(paths.config_path, paths=paths)
        caps = pilot.decimal_loads(sp.CONTRACT_RAW)["caps"]
        self.assertEqual(pilot_gate.config_holds_contract(config, C, caps), [])
        cases = {"caps.max_stake_per_order_p": {"caps": {"max_stake_per_order": 1.5}},
                 "caps.max_total_exposure_p": {"caps": {"max_total_exposure": 19.99}},
                 "caps.max_book_age_ms": {"caps": {"max_book_age_s": 2.5}},
                 "caps.max_orders_per_day": {"caps": {"max_orders_per_day": 26}},
                 "caps.max_minutes_before_off": {"caps": {"max_minutes_before_off": 11}},
                 "caps.external_exposure_tolerance_p": {"rules": {"external_exposure_tolerance": 0.01}},
                 "customer_strategy_ref": {"customer_strategy_ref": "bar-manual"},
                 "rules.market_types": {"rules": {"market_types": ["WIN", "PLACE"]}}}
        for name, change in cases.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(config)
                changed["orders"] = support.deep_merge(changed["orders"], change)
                self.assertIn(name, pilot_gate.config_holds_contract(changed, C, caps))
        # max_book_age_s written as 2 (int) or 2.0 (float) both hold 2000 ms; the sha256 differs (Q14).
        as_int = copy.deepcopy(config)
        as_int["orders"]["caps"]["max_book_age_s"] = 2
        self.assertEqual(pilot_gate.config_holds_contract(as_int, C, caps), [])
        self.assertNotEqual(cfg.config_sha256(as_int), cfg.config_sha256(config))

    def test_registration_receipt_schema(self):
        good = {"schema": pilot_gate.REGISTRATION_SCHEMA, "contract_sha256": C.sha256, "code_commit": "c" * 40,
                "code_tree": "e" * 64, "code_hashes_digest": "d" * 64, "config_sha256": "f" * 64,
                "registered_at": "2027-01-04T09:00:00.000000Z", "journal_seq": 3}
        self.assertEqual(pilot_gate.validate_registration(dict(good)), good)
        for key, value in (("schema", "v2"), ("code_commit", "C" * 40), ("journal_seq", -1), ("journal_seq", True),
                           ("registered_at", "2027-01-04 09:00"), ("extra", 1)):
            with self.subTest(key=key), self.assertRaises(pilot_gate.GateRefusal):
                pilot_gate.validate_registration(dict(good, **{key: value}))
        with self.assertRaises(pilot_gate.GateRefusal):
            pilot_gate.validate_registration({k: v for k, v in good.items() if k != "journal_seq"})

    def test_session_window_reads_any_contract_without_one(self):
        journal_ = Pilot(pt4=True)
        self.assertFalse(pilot_gate.session_window_open(journal_.events, now_ns=journal_.now))
        journal_.arm(0, until_h=12)
        day = pilot.utc_day(journal_.now)
        noon = pilot.parse_utc_ns(day + "T12:00:00Z")
        self.assertTrue(pilot_gate.session_window_open(journal_.events, now_ns=noon - 1))
        self.assertFalse(pilot_gate.session_window_open(journal_.events, now_ns=noon))
        journal_.stop("x-1", "review", "manual_check")              # ended early: still counted until its end
        self.assertTrue(pilot_gate.session_window_open(journal_.events, now_ns=noon - 1))

    def test_pilot_gate_is_never_imported_by_pilot(self):
        import test_betfair_boundaries as boundaries
        self.assertFalse(boundaries.imports_betfair_module(boundaries.BETFAIR / "pilot.py", "pilot_gate"))
        self.assertTrue(boundaries.imports_betfair_module(boundaries.BETFAIR / "gateway.py", "pilot_gate"))


# --- config, journal, api and risk additions ------------------------------------------------------------------------


class PilotConfigTests(unittest.TestCase):
    def setUp(self):
        self.paths = support.temp_state_paths(self)

    def load(self, overrides):
        support.write_test_config(self.paths, overrides)
        return cfg.load_config(self.paths.config_path, paths=self.paths)

    def test_pilot_section_optional_and_exact(self):
        plain = self.load(None)
        self.assertNotIn("pilot", plain["orders"])
        self.assertFalse(cfg.pilot_enabled(plain))
        pilot_cfg = self.load(sp.pilot_profile())
        self.assertTrue(cfg.pilot_enabled(pilot_cfg))
        self.assertNotEqual(cfg.config_sha256(pilot_cfg), cfg.config_sha256(self.load(support.deep_merge(
            sp.pilot_profile(), {"orders": {"pilot": {"evidence_deadline_s": 4}}}))))
        section = sp.pilot_profile()["orders"]["pilot"]
        for change, pattern in (({"x": 1}, r"unknown_config_key:orders\.pilot\.x"),
                                ({"contract_path": "/abs/p1.json"}, "orders.pilot.contract_path_invalid"),
                                ({"contract_path": "research/pilots/../x.json"}, "orders.pilot.contract_path_invalid"),
                                ({"contract_sha256": "A" * 64}, "orders.pilot.contract_sha256_invalid"),
                                ({"evidence_deadline_s": 0}, "evidence_deadline_s_out_of_range"),
                                ({"evidence_deadline_s": 5.5}, "evidence_deadline_s_out_of_range"),
                                ({"evidence_deadline_s": True}, "evidence_deadline_s_must_be_finite_number")):
            with self.subTest(change=change), self.assertRaisesRegex(ConfigError, pattern):
                self.load({"orders": {"pilot": dict(section, **change)}})
        for missing in cfg.PILOT_KEYS:
            with self.subTest(missing=missing):
                support.write_test_config(self.paths)
                data = json.loads(self.paths.config_path.read_text())
                data["orders"]["pilot"] = {k: v for k, v in section.items() if k != missing}
                self.paths.config_path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ConfigError, f"missing_config_key:orders.pilot.{missing}"):
                    cfg.load_config(self.paths.config_path, paths=self.paths)
        with self.assertRaisesRegex(ConfigError, "config_object_expected:orders.pilot"):
            self.load({"orders": {"pilot": "research/pilots/p1-fav-t5.json"}})

    def test_configs_without_the_section_are_unchanged(self):
        """A config with no orders.pilot keeps its sha256, its daily token and the example's equality with DEFAULTS
        (tests/test_betfair_config.py pins the example); the strategy refs are the config's alone."""
        plain = self.load(None)
        self.assertNotIn("pilot", cfg.DEFAULTS["orders"])
        self.assertEqual(cfg.order_strategy_ref(plain, "cli_manual"), plain["orders"]["customer_strategy_ref"])
        self.assertEqual(cfg.own_strategy_refs(plain), ["bar-manual"])
        with_pilot = self.load(sp.pilot_profile())
        self.assertEqual(cfg.order_strategy_ref(with_pilot, "pilot"), "p1-fav-t5")
        self.assertEqual(cfg.order_strategy_ref(with_pilot, "cli_manual"), "bar-manual")
        self.assertEqual(cfg.own_strategy_refs(with_pilot), ["bar-manual", "p1-fav-t5"])


class PilotJournalTests(unittest.TestCase):
    def setUp(self):
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()

    def journal(self, mode="live"):
        journal = OrderJournal(self.paths.journal_path(mode), mode=mode, clock=self.clock)
        self.addCleanup(journal.close)
        return journal

    def intent(self, source):
        return {"order": gw.instruction(), "customer_strategy_ref": "p1-fav-t5", "is_probe": False, "parent_ref": None,
                "provisional_child": False, "source": source}

    def test_pilot_intent_live_only(self):
        live = self.journal("live")
        live.append("intent", self.intent("pilot"), intent_id="i-1", customer_order_ref="ref-1", customer_ref="c-1",
                    market_id="1.1", state_after="RECORDED")
        paper = self.journal("paper")
        with self.assertRaisesRegex(JournalError, "pilot_intent_in_paper_journal"):
            paper.append("intent", self.intent("pilot"), intent_id="i-1", customer_order_ref="ref-1",
                         customer_ref="c-1", market_id="1.1", state_after="RECORDED")
        self.assertEqual(list(paper.events()), [])
        with self.assertRaisesRegex(JournalError, "intent_source_invalid"):
            live.append("intent", self.intent("script"), intent_id="i-2", customer_order_ref="ref-2",
                        customer_ref="c-2", market_id="1.1", state_after="RECORDED")

    def test_head_fresh_is_not_stale_under_an_open_generator(self):
        """Q3: the map's repro. anchor() on the journal's own connection lags while an events() generator is open;
        head_fresh() reads a separate read-only connection and sees the committed head."""
        journal = self.journal()
        for n in range(3):
            journal.append("note", {"reason": f"n{n}"})
        generator = journal.events()
        next(generator)
        other = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        self.addCleanup(other.close)
        other.append("halt", {"reason": "kill_incomplete", "scope": "global"})
        stale, fresh = journal.anchor(), journal.head_fresh()
        generator.close()
        self.assertEqual((stale["seq"], fresh["seq"]), (3, 4))
        self.assertEqual(fresh, {"seq": 4, "last_sha256": journal.anchor()["last_sha256"]})
        spaced = OrderJournal(self.paths.orders_dir / "Application Support #1" / "j.sqlite", mode="live",
                              clock=self.clock)                 # the real state root has a space: the URI quotes it
        self.addCleanup(spaced.close)
        event = spaced.append("note", {"reason": "x"})
        self.assertEqual(spaced.head_fresh(), {"seq": 1, "last_sha256": event["event_sha256"]})
        empty = OrderJournal(self.paths.journal_path("paper"), mode="paper", clock=self.clock)
        self.addCleanup(empty.close)
        self.assertEqual(empty.head_fresh(), {})
        empty.close()
        os.replace(self.paths.journal_path("paper"), self.paths.orders_dir / "moved.sqlite")
        with self.assertRaises(Exception):                  # an unreadable head raises; the gate then refuses
            empty.head_fresh()

    def test_before_ns_refuses_a_late_append_and_nothing_else_changes(self):
        """P106-2: the append is conditional on its own recorded_at and the clock inside the write transaction."""
        journal = self.journal()
        now = self.clock.time_ns()
        journal.append("note", {"reason": "ok"}, before_ns=now + 1)
        with self.assertRaisesRegex(JournalError, "journal_append_too_late"):
            journal.append("note", {"reason": "late"}, before_ns=now)
        original = self.clock.time_ns

        def stalls():
            return original() + 10 * S              # recorded_at (utc_now) in time; the clock inside the write late

        with patch.object(self.clock, "time_ns", stalls):
            with self.assertRaisesRegex(JournalError, "journal_append_too_late"):
                journal.append("note", {"reason": "stalled"}, before_ns=now + 5 * S)
        self.assertEqual([e["payload"]["reason"] for e in journal.events()], ["ok"])
        self.assertRaisesRegex(JournalError, "before_ns_invalid", journal.append, "note", {"reason": "x"},
                               before_ns=1.5)


class PilotApiRawTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.paths = support.temp_state_paths(self)
        self.config = gw.live_config(self.paths, None)

    def client(self, responses, *, slot="live", purpose="live_orders"):
        runner = support.FakeKeychainRunner()
        from betangel_research.betfair.auth import SessionManager
        from betangel_research.betfair.credentials import load_credentials
        transport = support.FakeHttpTransport(responses, clock=self.clock)
        config = self.config if slot == "live" else gw.live_config(self.paths, {"read": {"app_key_slot": "delayed"}})
        credentials = load_credentials(config, purpose=purpose if slot == "live" else "read", profile="default",
                                       runner=runner)
        session = SessionManager(config, credentials, paths=self.paths, transport=transport, clock=self.clock)
        rpc_client = api_module.JsonRpcClient(config, session, clock=self.clock)
        return rpc_client, api_module.ReadClient(rpc_client, paths=self.paths, clock=self.clock), transport

    def login(self):
        return support.http_response({"token": gw.SESSION_TOKEN, "product": "x", "status": "SUCCESS", "error": ""})

    def test_raw_row_and_book_params_and_exact_bytes(self):
        nan_body = b'{"jsonrpc":"2.0","result":[{"marketId":"1.5","runners":[{"x":NaN}]}],"id":2}'
        error_body = b'{"jsonrpc":"2.0","error":{"code":-32099,"message":"ANGX-0003"},"id":3}'
        rpc_client, read, transport = self.client([self.login(), support.http_response(body=nan_body),
                                                  support.http_response(status=503, body=error_body)])
        row = read.raw_catalogue_row("1.5", deadline_s=5)
        book = read.raw_decision_book("1.5", deadline_s=5)
        self.assertIs(type(row["body"]), bytes)
        self.assertEqual((row["body"], row["status"], row["error"]), (nan_body, 200, None))
        self.assertEqual((book["body"], book["status"], book["error"]), (error_body, 503, None))
        bodies = [json.loads(r["body"]) for r in transport.requests[1:]]
        self.assertEqual(bodies[0]["params"], {"filter": {"marketIds": ["1.5"]}, "maxResults": 1, "marketProjection": [
            "EVENT", "EVENT_TYPE", "MARKET_DESCRIPTION", "MARKET_START_TIME"]})
        self.assertEqual(bodies[1]["params"], {"marketIds": ["1.5"],
                                               "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}})
        self.assertEqual([r["idempotent"] for r in transport.requests[1:]], [False, False])
        self.assertEqual([r["deadline_s"] for r in transport.requests[1:]], [5, 5])

    def test_one_attempt_no_relogin_no_retry(self):
        for label, item in (("timeout", support.transport_failure("receive", "deadline_exceeded")),
                            ("http 503", support.http_response(status=503, body=b"busy")),
                            ("session error", support.rpc_error_response(
                                -32099, "x", data={"exceptionname": "APINGException",
                                                   "APINGException": {"errorCode": "INVALID_SESSION_INFORMATION"}}))):
            with self.subTest(case=label):
                rpc_client, read, transport = self.client([self.login(), item])
                out = read.raw_decision_book("1.5", deadline_s=5)
                self.assertEqual(len(transport.requests), 2)            # the login and exactly one read
                if label == "timeout":
                    self.assertEqual((out["body"], out["error"]), (None, "deadline_exceeded"))
                else:
                    self.assertIsInstance(out["body"], bytes)

    def test_delayed_key_refused_and_methods_limited(self):
        rpc_client, read, transport = self.client([self.login()], slot="delayed")
        with self.assertRaisesRegex(OrderRefused, "pilot_read_requires_live_app_key"):
            read.raw_decision_book("1.5", deadline_s=5)
        self.assertEqual(transport.requests, [])
        live_rpc, _, _ = self.client([self.login()])
        self.assertRaises(ValueError, live_rpc.call_raw, api_module.FUNDS_METHOD, {}, deadline_s=5)

    def test_stamps_after_the_throttle_on_both_clocks(self):
        rpc_client, read, transport = self.client([self.login()] + [support.http_response(body=rpc())] * 5)
        for _ in range(4):
            read.raw_decision_book("1.5", deadline_s=5)
        before = self.clock.time_ns()
        out = read.raw_decision_book("1.5", deadline_s=5)          # the fifth call within a second sleeps first
        self.assertGreater(sum(self.clock.sleeps), 0)
        self.assertGreater(out["sent_wall_ns"], before)
        self.assertEqual((out["sent_wall_ns"], out["sent_monotonic_ns"]),
                         (out["received_wall_ns"], out["received_monotonic_ns"]))


class PilotRiskTests(unittest.TestCase):
    def setUp(self):
        self.clock = support.FakeClock()
        self.paths = support.temp_state_paths(self)

    def guard(self, overrides=None):
        support.write_test_config(self.paths, support.deep_merge(gw.LIVE_OVERRIDES, overrides or {}))
        config = cfg.load_config(self.paths.config_path, paths=self.paths)
        return RiskGuard(config, mode="live", clock=self.clock, paths=self.paths), config

    def decide(self, guard, **kw):
        book = orders.live_book(self.clock, market_time=utc_from_ns(self.clock.time_ns() + 300 * S))
        return guard.evaluate_place("1.1", [gw.instruction(price_h=350)], book=book,
                                    state=orders.state_with([], mode="live", ref_roundtrip_verified=True),
                                    arming={"armed": True, "failures": []}, now_ns=self.clock.time_ns(),
                                    now_monotonic_ns=self.clock.mono_ns, kill_engaged=False,
                                    customer_ref="brq200101120000-0000000000000001",
                                    customer_order_refs=["bro200101120000-0000000000000001"], **kw)

    def test_non_pilot_decision_id_unchanged_and_binding_changes_it(self):
        guard, _ = self.guard()
        plain = self.decide(guard)
        self.assertEqual(self.decide(guard, source="cli_manual")["decision_id"], plain["decision_id"])
        self.assertNotIn("pilot_record_sha256", plain)
        self.assertNotIn("customer_strategy_ref", plain)
        binding = {"record_sha256": "1" * 64, "contract_sha256": C.sha256}
        bound = self.decide(guard, source="pilot", pilot_binding=binding)
        other = self.decide(guard, source="pilot", pilot_binding=dict(binding, record_sha256="2" * 64))
        self.assertEqual(len({plain["decision_id"], bound["decision_id"], other["decision_id"]}), 3)
        self.assertEqual(bound["pilot_record_sha256"], "1" * 64)
        self.assertEqual(gateway_module._decision_payload(bound)["pilot_record_sha256"], "1" * 64)
        self.assertNotIn("pilot_record_sha256", gateway_module._decision_payload(plain))
        self.assertNotIn("pilot_binding", RULE_ORDER)
        for bad in ({"record_sha256": "1" * 64}, {"record_sha256": "x", "contract_sha256": C.sha256}, ["x"]):
            with self.subTest(binding=bad), self.assertRaises(ValueError):
                self.decide(guard, source="pilot", pilot_binding=bad)
        self.assertRaises(ValueError, self.decide, guard, source="pilot")
        self.assertRaises(ValueError, self.decide, guard, pilot_binding=binding)

    def test_strategy_ref_bound_by_source_under_a_pilot_config(self):
        """Astra's ruling on critique 13: manual orders bind bar-manual, pilot orders the pilot ref, in the decision
        id and in the body check; without orders.pilot the config ref binds as before."""
        guard, config = self.guard(sp.pilot_profile())
        manual = self.decide(guard, source="cli_manual")
        bound = self.decide(guard, source="pilot", pilot_binding={"record_sha256": "1" * 64,
                                                                   "contract_sha256": C.sha256})
        self.assertEqual((manual["customer_strategy_ref"], bound["customer_strategy_ref"]), ("bar-manual", "p1-fav-t5"))
        from betangel_research.betfair.risk import _params_match

        def params(ref):
            return {"marketId": "1.1", "customerRef": "brq200101120000-0000000000000001", "customerStrategyRef": ref,
                    "marketVersion": {"version": manual["market_version"]},
                    "instructions": [{"orderType": "LIMIT", "selectionId": gw.SELECTION, "handicap": 0, "side": "BACK",
                                      "customerOrderRef": "bro200101120000-0000000000000001",
                                      "limitOrder": {"size": 2.0, "price": 3.5, "persistenceType": "LAPSE"}}]}

        self.assertTrue(_params_match(manual, params("bar-manual"), config))
        self.assertFalse(_params_match(manual, params("p1-fav-t5"), config))     # manual cannot carry the pilot ref
        self.assertTrue(_params_match(bound, params("p1-fav-t5"), config))
        self.assertFalse(_params_match(bound, params("bar-manual"), config))     # and the pilot not the manual one
        plain_guard, plain_config = self.guard()
        plain = self.decide(plain_guard)
        self.assertTrue(_params_match(plain, params("bar-manual"), plain_config))

    def test_pre_reservation_rules(self):
        """Critique 1: R1, R3, R6, R8-R10 on the journal fold for one more 200p BACK, before any reservation."""
        guard, config = self.guard(sp.pilot_profile())
        now = self.clock.time_ns()
        clean = orders.state_with([], mode="live", ref_roundtrip_verified=True)
        self.assertEqual(guard.evaluate_pilot_reservation("1.9", state=clean, now_ns=now, size_p=200,
                                                          kill_engaged=False), [])
        full = orders.state_with([orders.order_record(customer_order_ref=f"r{i}", market_id=f"1.{i + 10}",
                                                      state="EXECUTION_COMPLETE", size_matched_p=200,
                                                      size_remaining_p=0, price_h=300, average_price_matched_h=300,
                                                      mode="live")
                                  for i in range(10)], mode="live", ref_roundtrip_verified=True)
        rules = [f["rule"] for f in guard.evaluate_pilot_reservation("1.9", state=full, now_ns=now, size_p=200,
                                                                     kill_engaged=False)]
        self.assertIn("total_exposure", rules)
        unresolved = orders.state_with([orders.order_record(customer_order_ref="u1", market_id="1.40", state="UNKNOWN",
                                                            mode="live")], mode="live", ref_roundtrip_verified=True)
        self.assertEqual([f["rule"] for f in guard.evaluate_pilot_reservation(
            "1.9", state=unresolved, now_ns=now, size_p=200, kill_engaged=False)][0], "unresolved_orders")
        self.assertEqual(guard.evaluate_pilot_reservation("1.9", state=clean, now_ns=now, size_p=200,
                                                          kill_engaged=True)[0]["reason"], "kill_engaged")
        busy = dict(clean, submitted_instruction_wall_ns=[now - S, now - 2 * S])
        self.assertEqual([f["rule"] for f in guard.evaluate_pilot_reservation(
            "1.9", state=busy, now_ns=now, size_p=200, kill_engaged=False)], ["rate_minute"])



# --- the gateway (P1-06 §6.3 onwards) -------------------------------------------------------------------------------


class PilotCase(unittest.TestCase):
    """One pilot env per test; every legitimate flow is checked to fold with no protocol violation."""

    def build(self, **kw):
        self.env = sp.build_pilot_gateway(self, **kw)
        return self.env

    def market(self, **kw):
        return sp.add_market(self.env, **kw)

    def open(self, gateway=None):
        return sp.open_pilot(self, self.env, gateway=gateway)

    def place(self, market, **kw):
        return sp.place(self.env, market, **kw)

    def clean(self):
        return sp.assert_no_violations(self, self.env)

    def kinds(self):
        return [e["kind"] for e in sp.events(self.env)]

    def notes(self, record_type=None):
        return [e["payload"]["pilot"] for e in sp.pilot_notes(self.env, record_type)]

    def carried(self, result, carrier_kind):
        """P1-08 §3.2: a review latch after a durable carrier is a review stop note '<code>-<res>-<carrier seq>' (the
        id the fold derives from that carrier), never a refusal note. -> ([(stop_id, reason)] of the review stop notes,
        the id expected for the last carrier_kind event)."""
        carrier = [e for e in sp.events(self.env) if e["kind"] == carrier_kind][-1]
        stops = [(n["stop_id"], n["reason"]) for n in self.notes("stop") if n["scope"] == "review"]
        return stops, lambda code: pilot.carried_review_stop_id(code, result["pilot"]["reservation_seq"],
                                                                carrier["seq"])

    def reads(self, name=None, market=None):
        return self.env.router.reads_of(name, market.market_id if market is not None else None) if name else \
            self.env.router.reads

    def assert_sent(self, result):
        self.assertEqual((result["decision"], result["refusal"]), ("accepted", None), result.get("refusal"))
        self.assertEqual(result["pilot"]["state"], "EXECUTABLE")
        self.assertEqual(len(self.env.router.order_requests), 1)

    def assert_not_sent_at_first_byte(self, result, code):
        env = self.env
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]], [("FAILED", "transport_not_sent")])
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual((env.transport.refused[-1]["code"], env.transport.bytes_written[-1]), (code, 0))
        state = sp.fold(env, live_open_id=env.gateway._open_id)
        record = state.place_records[-1]
        self.assertTrue(record.failed_not_sent)                       # the slot stays used
        self.assertEqual(state.lifetime_used, len(state.place_records))
        self.assertIn(record.market_id, state.reservations)          # the market stays reserved
        return state


class SourceAndE1Tests(PilotCase):
    def test_happy_path_order_of_events(self):
        env = self.build()
        market = self.market()
        report = self.open()
        self.assertEqual((report["armed"], report["failures"], report["command"]), (True, [], "pilot-place"))
        result = self.place(market)
        self.assert_sent(result)
        info = result["pilot"]
        self.assertEqual((info["outcome"], info["code"], info["latch"], info["unknown"]), ("PLACE", None, None, False))
        kinds = self.kinds()
        self.assertEqual(kinds[-7:], ["note", "note", "funds_check", "intent", "risk_decision", "submit", "report"])
        events = sp.events(env)
        reservation, decision = events[-7], events[-6]
        self.assertEqual((reservation["seq"], decision["seq"]), (info["reservation_seq"], info["decision_seq"]))
        record = decision["payload"]["pilot"]
        self.assertEqual(pilot.record_sha256(record), info["record_sha256"])
        self.assertEqual(events[-3]["payload"]["pilot_record_sha256"], info["record_sha256"])     # risk decision
        self.assertEqual(events[-2]["payload"]["pilot_record_sha256"], info["record_sha256"])     # submit
        self.assertEqual(events[-2]["payload"]["params"]["customerStrategyRef"], "p1-fav-t5")
        intent = events[-4]
        self.assertEqual((intent["payload"]["source"], intent["customer_order_ref"], intent["payload"]["order"]),
                         ("pilot", record["customer_order_ref"], pilot.expected_instruction(record)))
        request = env.router.order_requests[0]["params"]
        self.assertEqual((request["customerStrategyRef"], request["instructions"][0]["customerOrderRef"],
                          request["instructions"][0]["limitOrder"]), ("p1-fav-t5", record["customer_order_ref"],
                                                                      {"size": 2.0, "price": 3.0,
                                                                       "persistenceType": "LAPSE"}))
        # E1: no per-order echo and no operator_confirm; A11 still ran at open.
        self.assertEqual(env.confirmer.prompts, [])
        self.assertNotIn("operator_confirm", kinds)
        state = self.clean()
        self.assertEqual((state.lifetime_used, state.day_used), (1, {sp.DAY: 1}))
        # The remainder cancel of this open's own pilot bet (note §8) runs with source "pilot"; no PA gate.
        cancelled = env.gateway.cancel(market.market_id, [info["bet_id"]], source="pilot")
        self.assertEqual(cancelled["decision"], "accepted")
        self.clean()

    def test_pilot_source_refused_off_the_pilot_path(self):
        env = self.build()
        market = self.market()
        before = self.kinds()
        self.open()
        opened = self.kinds()
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.place(market.market_id, [gw.instruction()], source="pilot", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "pilot_source_needs_pilot_path")
        self.assertEqual((self.kinds(), env.router.order_requests, self.reads(sp.BOOK)), (opened, [], []))
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.cancel(market.market_id, ["300000000002"], source="pilot")
        self.assertEqual(caught.exception.rule, "pilot_source_needs_pilot_path")
        self.assertTrue(len(opened) > len(before))
        paper = gw.build_paper_gateway(self)
        paper.gateway.open(command="place")
        self.addCleanup(paper.gateway.close)
        with self.assertRaises(OrderRefused) as caught:
            paper.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(paper.clock), source="pilot")
        self.assertEqual(caught.exception.rule, "pilot_source_live_only")
        self.assertEqual(list(paper.journal.events()), [])

    def test_remainder_cancel_only_for_this_opens_own_bet(self):
        env = self.build()
        market = self.market()
        self.open()
        result = self.place(market)
        self.assert_sent(result)
        other = env.exchange.add_order(ref="bro-other", strategy_ref="bar-manual", market_id=market.market_id)
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.cancel(market.market_id, [other], source="pilot")
        self.assertEqual(caught.exception.rule, "pilot_source_needs_pilot_path")
        with self.assertRaises(OrderRefused):
            env.gateway.cancel(market.market_id, [result["pilot"]["bet_id"], other], source="pilot")
        env.gateway.close()
        manual = sp.new_gateway(env)
        manual.open(command="cancel")
        self.addCleanup(manual.close)
        with self.assertRaises(OrderRefused) as caught:                       # outside a pilot-place open
            manual.cancel(market.market_id, [result["pilot"]["bet_id"]], source="pilot")
        self.assertEqual(caught.exception.rule, "pilot_source_needs_pilot_path")

    def test_no_book_catalogue_country_or_instruction_parameter(self):
        env = self.build()
        market = self.market()
        self.open()
        before = self.kinds()
        for name in ("book", "catalogue", "country", "instructions", "row_body"):
            with self.subTest(parameter=name), self.assertRaises(TypeError):
                env.gateway.pilot_place(market.market_id, scheduler_off_ns=market.off_ns, confirm_token=env.token,
                                        **{name: {}})
        self.assertEqual((self.kinds(), self.reads()), (before, []))

    def test_non_present_operator_fails_a11_and_paper_refuses(self):
        env = self.build()
        env.gateway.confirmer.present = False
        with self.assertRaises(NotArmed) as caught:
            env.gateway.open(command="pilot-place", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "operator_tty_required")
        paper = gw.build_paper_gateway(self)
        paper.gateway.close()
        with self.assertRaises(OrderRefused) as caught:
            paper.gateway.open(command="pilot-place")
        self.assertEqual(caught.exception.rule, "pilot_place_is_live_only")
        with self.assertRaises(ConfigError):
            OrderGateway(paper.config, paths=paper.paths, mode="paper", journal=paper.journal, guard=paper.guard,
                         clock=paper.clock, env={}, pilot=env.ctx)

    def test_one_reservation_per_open(self):
        env = self.build()
        first, second = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        self.assert_sent(self.place(first))
        with self.assertRaises(OrderRefused) as caught:
            self.place(second)
        self.assertEqual(caught.exception.rule, "pilot_one_reservation_per_open")
        self.assertEqual(self.reads(sp.CATALOGUE, second), [])
        self.clean()


class DecisionTests(PilotCase):
    """Note §14 decision cases (:208) at the gateway: one fresh raw live-key row and book per reservation."""

    def test_cached_row_is_never_the_evidence(self):
        env = self.build()
        market = self.market()
        env.paths.ensure()
        stale = market.row({"MARKET_DESCRIPTION", "RUNNER_DESCRIPTION", "EVENT_TYPE", "MARKET_START_TIME"})
        stale = json.loads(dumps(dict(stale, marketStartTime="2027-01-04T11:00:00.000Z")))
        from betangel_research.betfair.receipts import atomic_write_json
        atomic_write_json(env.paths.catalogue_cache_dir / f"{market.market_id}.json",
                          {"schema_version": 1, "market_id": market.market_id, "cached_at": env.clock.utc_now(),
                           "row": stale})
        self.open()
        captured = []
        original = env.transport.request

        def record_bodies(*args, **kw):
            response = original(*args, **kw)
            captured.append(response["body"])
            return response

        with patch.object(env.transport, "request", side_effect=record_bodies):
            result = self.place(market)
        self.assert_sent(result)
        rows = self.reads(sp.CATALOGUE, market)
        self.assertEqual(len(rows), 1)
        record = self.notes("decision")[-1]
        served = [b for b in captured if b.startswith(b'{"jsonrpc":"2.0","result":[{"marketId":"' +
                                                      market.market_id.encode() + b'","marketName"')]
        self.assertEqual(record["row_sha256"], hashlib.sha256(served[0]).hexdigest())
        self.assertEqual(record["off_ns"], OFF)                     # the fresh row's off, not the cache's
        self.clean()

    def test_projections_and_a_row_without_event(self):
        env = self.build()
        market = self.market(row_changes={"event": None})
        self.open()
        result = self.place(market)
        self.assertEqual((result["decision"], result["pilot"]["outcome"], result["pilot"]["code"]),
                         ("skipped", "SKIP", "country_not_allowed"))
        (row,) = self.reads(sp.CATALOGUE, market)
        self.assertEqual(set(row["params"]["marketProjection"]),
                         {"EVENT", "EVENT_TYPE", "MARKET_DESCRIPTION", "MARKET_START_TIME"})
        self.assertEqual((row["params"]["maxResults"], row["params"]["filter"]), (1, {"marketIds": [market.market_id]}))
        self.assertEqual(env.router.order_requests, [])
        self.assertNotIn("intent", self.kinds())
        self.clean()

    def test_off_is_market_start_time_never_open_date(self):
        self.build()
        market = self.market(open_date="2027-01-04T09:00:00.000Z")
        self.open()
        self.assert_sent(self.place(market))
        record = self.notes("decision")[-1]
        self.assertEqual((record["off"], record["off_ns"]), (sp.off_text(OFF), OFF))
        self.clean()

    def test_delayed_book_and_every_read_on_the_live_key(self):
        self.build()
        market = self.market(delayed=True)
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "book_delayed"))
        self.assertEqual({read["slot"] for read in self.reads()}, {"live"})
        self.clean()

    def test_window_by_clock_control(self):
        """Observed 1 ns after off - 285 s: SKIP(window); before off - 300 s nothing is reserved at all."""
        env = self.build(start=pilot.utc_text_from_ns(OFF - 285 * S + 1000))
        market = self.market()
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "window"))
        self.assertEqual(self.notes("decision")[-1]["decision_book_wall_ns"], OFF - 285 * S + 1000)
        self.clean()
        early = self.build(start=pilot.utc_text_from_ns(OFF - 301 * S))
        market = self.market()
        self.open()
        before = self.kinds()
        result = self.place(market)
        self.assertEqual(result["refusal"]["rule"], "pilot_reserve_too_early")
        self.assertEqual((self.kinds(), self.reads()), (before, []))
        self.assertIsNotNone(early)

    def test_one_fetch_after_a_transient_failure_and_a_moved_head(self):
        env = self.build()
        market = self.market()
        self.open()
        env.router.next_response[sp.BOOK] = [support.transport_failure("receive", "deadline_exceeded")]
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "evidence_invalid"))
        self.assertEqual((len(self.reads(sp.CATALOGUE, market)), len(self.reads(sp.BOOK, market))), (1, 1))
        record = self.notes("decision")[-1]
        self.assertEqual(record["book_sha256"], hashlib.sha256(b"").hexdigest())
        sidecar = self.sidecar(record, "book")
        self.assertEqual(sidecar["read_failure"], "book_read_deadline_exceeded")
        self.clean()
        # A moved head re-classifies with the same bytes: still one row and one book.
        env2 = self.build()
        market2 = self.market()
        self.open()
        original = env2.gateway._pilot_append
        moved = []

        def foreign_note_first(payload, **kw):
            if payload["pilot"]["type"] == "decision" and not moved:
                writer = sp.other_writer(env2)
                moved.append(writer.append("note", {"reason": "another_process"}))
            return original(payload, **kw)

        with patch.object(env2.gateway, "_pilot_append", side_effect=foreign_note_first):
            result = self.place(market2)
        self.assert_sent(result)
        self.assertEqual((len(self.reads(sp.CATALOGUE, market2)), len(self.reads(sp.BOOK, market2))), (1, 2))
        self.assertEqual(len(self.notes("decision")), 1)
        self.assertGreater(result["pilot"]["decision_seq"], moved[0]["seq"])
        self.clean()

    def sidecar(self, record, name):
        directory = pilot_gate.evidence_dir(self.env.paths, self.env.contract.sha256)
        digest = record[f"{name}_sha256"]
        stem = f"{record['reservation_seq']:08d}-{name}-{digest}"
        self.assertEqual(hashlib.sha256((directory / f"{stem}.bin").read_bytes()).hexdigest(), digest)
        return json.loads((directory / f"{stem}.json").read_text())

    def test_failed_reads_keep_the_exact_bytes_and_skip(self):
        """Q8, Q10: a non-200 error body, an error envelope, a slow response and a sleep each give
        SKIP(evidence_invalid) on the exact bytes received, fetched once, with the read's failure in the sidecar."""
        error_body = b'{"jsonrpc":"2.0","error":{"code":-32099,"message":"ANGX-0003"},"id":2}'
        cases = {
            "http 503": ([support.http_response(status=503, body=b"<html>busy</html>")], None, "book_read_http_status",
                         b"<html>busy</html>"),
            "error envelope": ([support.http_response(body=error_body)], None, None, error_body),
            "slow": (None, ("advance", 6), "book_read_elapsed_over_deadline", None),
            "sleep": (None, ("step_wall", 3), "book_read_clock_drift", None),
        }
        for label, (script, clock_step, failure, body) in cases.items():
            with self.subTest(case=label):
                env = self.build()
                market = self.market()
                self.open()
                if script is not None:
                    env.router.next_response[sp.BOOK] = list(script)
                if clock_step is not None:
                    original = env.transport.request

                    def slow(*args, step=clock_step, original=original, **kw):
                        response = original(*args, **kw)
                        if json.loads(args[3])["method"].endswith(sp.BOOK):
                            getattr(env.clock, step[0])(step[1])
                        return response

                    patcher = patch.object(env.transport, "request", side_effect=slow)
                    patcher.start()
                    self.addCleanup(patcher.stop)
                result = self.place(market)
                self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "evidence_invalid"))
                self.assertEqual(len(self.reads(sp.BOOK, market)), 1)
                record = self.notes("decision")[-1]
                sidecar = self.sidecar(record, "book")
                self.assertEqual(sidecar["read_failure"], failure)
                if body is not None:
                    self.assertEqual(record["book_sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_evidence_write_failure_leaves_an_interrupted_reservation(self):
        env = self.build()
        market = self.market()
        self.open()
        with patch.object(pilot_gate, "atomic_write_bytes", side_effect=OSError("disk full")):
            result = self.place(market)
        self.assertEqual(result["refusal"]["rule"], "pilot_evidence_not_stored")
        self.assertEqual(self.notes("decision"), [])
        self.assertEqual((len(self.reads(sp.CATALOGUE, market)), len(self.reads(sp.BOOK, market))), (1, 1))
        env.gateway.close()
        state = self.clean()
        self.assertTrue(state.reservations[market.market_id]["interrupted"])
        with patch.object(pilot_gate, "atomic_write_bytes", side_effect=SecretLeak("secret_value_in_output:x")):
            other = self.market(off_ns=OFF + 60 * S)
            sp.set_time(env.clock, OFF + 60 * S - 299 * S)
            self.open()
            self.assertEqual(self.place(other)["refusal"]["rule"], "pilot_evidence_not_stored")

    def test_send_book_off_compared_as_an_instant(self):
        for text, refused in (("2027-01-04T10:00:00Z", False), ("2027-01-04T10:00:00.000Z", False),
                              ("2027-01-04T10:00:00.001Z", True)):
            with self.subTest(market_time=text):
                env = self.build()
                market = self.market(market_time=text)
                self.open()
                with patch.object(env.read, "store_catalogue_row"):     # the send book reads the served row
                    result = self.place(market)
                if refused:
                    self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]),
                                     ("pilot_send_book_off_differs", "market"))
                    self.assertEqual(env.router.order_requests, [])
                    self.assertNotIn("intent", self.kinds())
                else:
                    self.assert_sent(result)
                self.clean()

    def test_decimal_depth_edge(self):
        self.build()
        market = self.market()
        market.backs[sp.FAV] = (("3.00", "9.999999999999999999"),)
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "depth_below_min"))
        self.clean()

    def test_a_predicate_left_out(self):
        self.build()
        market = self.market(country="FR")
        self.open()
        self.assertEqual(self.place(market)["pilot"]["code"], "country_not_allowed")
        self.clean()

    def test_evidence_files_for_place_skip_and_refusal(self):
        env = self.build()
        placed, skipped, refused = self.market(), self.market(off_ns=OFF + 1800 * S, delayed=True), \
            self.market(off_ns=OFF + 3600 * S)
        self.open()
        self.assert_sent(self.place(placed))
        env.gateway.close()
        for market in (skipped, refused):
            sp.set_time(env.clock, market.off_ns - 299 * S)
            self.open()
            if market is refused:
                original = env.read.raw_decision_book

                def stop_during_the_read(*args, **kw):
                    out = original(*args, **kw)            # another process ends the session meanwhile
                    sp.note(env, "stop", journal=sp.other_writer(env), stop_id="yu-stop-s", scope="session",
                            reason="manual_check", level_p=None, source_seq=None)
                    return out

                with patch.object(env.read, "raw_decision_book", side_effect=stop_during_the_read):
                    result = self.place(market)
                self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("session_ended", "review"))
                note = self.notes("refusal")[-1]
                self.assertEqual(note["code"], "session_ended")
                files = sorted(p.name for p in pilot_gate.evidence_dir(env.paths, env.contract.sha256).iterdir()
                               if p.name.startswith(f"{note['reservation_seq']:08d}-"))
                self.assertEqual(len(files), 4)
                for name in ("row", "book"):
                    self.assertTrue(any(f.endswith(note[f"{name}_sha256"] + ".bin") for f in files))
            else:
                self.assertEqual(self.place(market)["pilot"]["outcome"], "SKIP")
            env.gateway.close()
        for record in self.notes("decision"):
            self.sidecar(record, "row")
            self.sidecar(record, "book")
        state = self.clean()
        self.assertEqual(len(state.place_records) + len(state.skip_records), 2)



class BindingTests(PilotCase):
    """R13 before any intent (note §4 "Binding"; design §2.5 F): a binding that fails journals a refusal note (a
    review stop), writes no intent and sends nothing, so the fold shows no breach."""

    def refused(self, alter=None, *, market_override=None):
        env = self.build()
        market = self.market()
        self.open()
        decide, send = env.gateway._pilot_decide, env.gateway._pilot_send

        def altered_decide(*args):
            ctx = decide(*args)
            if alter is not None and ctx.get("decision") == "place":
                alter(ctx)
            return ctx

        def altered_send(market_id, ctx, info):
            return send(market_override or market_id, ctx, info)

        with patch.object(env.gateway, "_pilot_decide", side_effect=altered_decide), \
                patch.object(env.gateway, "_pilot_send", side_effect=altered_send):
            result = self.place(market)
        self.assertEqual(result["decision"], "refused")
        self.assertNotIn("intent", self.kinds())
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual(result["pilot"]["latch"], "review")
        state = self.clean()
        self.assertTrue(any(stop["reason"] == result["refusal"]["rule"] for stop in state.active_stops))
        return result

    def test_edited_digest(self):
        self.assertEqual(self.refused(lambda ctx: ctx.update(digest="0" * 64))["refusal"]["rule"],
                         "r13_digest_mismatch")

    def test_body_that_differs_including_a_float_handicap(self):
        for changes in ({"handicap": 0.0}, {"price_h": 305}, {"size_p": 300}):
            with self.subTest(changes=changes):
                result = self.refused(lambda ctx: ctx.update(instruction=dict(ctx["instruction"], **changes)))
                self.assertEqual(result["refusal"]["rule"], "r13_body_differs")

    def test_another_market(self):
        self.assertEqual(self.refused(market_override="1.999")["refusal"]["rule"], "r13_market_differs")

    def test_another_ref(self):
        self.assertEqual(self.refused(lambda ctx: ctx.update(ref="bro-not-the-record"))["refusal"]["rule"],
                         "r13_ref_differs")

    def test_a_record_from_another_open(self):
        """A record reused from an earlier open is refused before any intent: by phase "send" (that record was sent:
        pilot_record_submitted, a review stop), and, with that check stood down, by R13 itself (r13_other_open)."""
        for stand_down in (False, True):
            with self.subTest(authority_record_check=not stand_down):
                env = self.build()
                first, second = self.market(), self.market(off_ns=OFF + 1800 * S)
                self.open()
                self.assert_sent(self.place(first))
                old_seq = sp.pilot_notes(env, "decision")[-1]["seq"]
                old_record = self.notes("decision")[-1]
                env.gateway.close()
                sp.set_time(env.clock, second.off_ns - 299 * S)
                decide, authority = env.gateway._pilot_decide, pilot.placement_authority

                def reuse(*args, decide=decide):
                    ctx = decide(*args)
                    ctx.update(decision_seq=old_seq, digest=pilot.record_sha256(old_record),
                               ref=old_record["customer_order_ref"],
                               instruction=pilot.expected_instruction(old_record))
                    return ctx

                def no_record_check(state, authority=authority, **kw):
                    return None if stand_down and kw.get("phase") == "send" else authority(state, **kw)

                self.open()
                with patch.object(env.gateway, "_pilot_decide", side_effect=reuse), \
                        patch.object(pilot, "placement_authority", side_effect=no_record_check):
                    result = self.place(second)
                self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]),
                                 ("r13_other_open" if stand_down else "pilot_record_submitted", "review"))
                self.assertEqual(len(env.router.order_requests), 1)
                self.assertEqual(self.kinds().count("intent"), 1)
                self.clean()


class ReservationTests(PilotCase):
    """Note §14 reservation cases (:209)."""

    def test_two_runners_on_one_market(self):
        env = self.build()
        market = self.market()
        self.open()
        self.assert_sent(self.place(market))
        env.gateway.close()
        reads = len(self.reads())
        before = self.kinds()
        second = sp.new_gateway(env)
        self.open(gateway=second)
        opened = self.kinds()
        result = self.place(market, gateway=second)
        self.assertEqual(result["refusal"]["rule"], "pilot_market_already_reserved")
        self.assertEqual((self.kinds(), len(self.reads())), (opened, reads))
        self.assertGreater(len(opened), len(before))
        state = self.clean()
        self.assertEqual(len(self.notes("reservation")), 1)
        self.assertNotIn("second_reservation", [v["code"] for v in state.violations])

    def test_a_lost_lock_file_cannot_reserve_one_market_twice(self):
        """Critique 17: the lock file unlinked under gateway A, gateway B opens; both try one market at once. B
        reserves while A is about to; A's compare-and-append fails, it folds again and finds the market taken."""
        env = self.build()
        market = self.market()
        self.open()
        os.unlink(env.paths.gateway_lock)
        other = sp.new_gateway(env)
        self.open(gateway=other)
        original = env.gateway._pilot_append
        results = {}

        def b_first(payload, **kw):
            if payload["pilot"]["type"] == "reservation" and "b" not in results:
                results["b"] = self.place(market, gateway=other)
            return original(payload, **kw)

        with patch.object(env.gateway, "_pilot_append", side_effect=b_first):
            results["a"] = self.place(market)
        self.assert_sent(results["b"])
        self.assertEqual(results["a"]["refusal"]["rule"], "pilot_market_already_reserved")
        self.assertEqual(len(self.notes("reservation")), 1)
        self.assertEqual((len(self.reads(sp.CATALOGUE, market)), len(self.reads(sp.BOOK, market))), (1, 2))
        self.clean()

    def test_day_limit_skip_cap_and_its_session_stop(self):
        env = self.build()
        sp.preload_place_records(env, 5)
        market = self.market(off_ns=env.clock.time_ns() + 299 * S)
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"], result["pilot"]["latch"]),
                         ("SKIP", "cap", "session"))
        stops = self.notes("stop")
        self.assertEqual([(s["scope"], s["reason"]) for s in stops], [("session", "day_limit")])
        self.assertEqual(env.router.order_requests, [])
        state = self.clean()
        self.assertEqual(state.lifetime_used, 5)

    def test_last_slot_race_with_another_runner(self):
        """Two runners racing for the PT4 day's last slot. Another runner's PLACE needs its own reservation, which
        ends this open (note §4), so on the moved head the same bytes re-classify to reservation_interrupted: a
        refusal note, no record, one fetch, and the slot is the other runner's (a deviation from design §6.5's
        SKIP(cap), which no legitimate writer can produce under the accepted fold)."""
        env = self.build()
        sp.preload_place_records(env, 4)
        market = self.market(off_ns=env.clock.time_ns() + 299 * S)
        self.open()
        original = env.gateway._pilot_append
        raced = []

        def other_runner_first(payload, **kw):
            if payload["pilot"]["type"] == "decision" and not raced:
                raced.extend(sp.preload_place_records(env, 1, journal=sp.other_writer(env)))
            return original(payload, **kw)

        with patch.object(env.gateway, "_pilot_append", side_effect=other_runner_first):
            result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("reservation_interrupted", "review"))
        self.assertEqual((len(self.reads(sp.CATALOGUE, market)), len(self.reads(sp.BOOK, market))), (1, 1))
        self.assertEqual(env.router.order_requests, [])
        state = self.clean()
        self.assertEqual((state.lifetime_used, raced[0][2]["lifetime_slot"]), (5, 5))
        self.assertTrue(state.reservations[market.market_id]["interrupted"])
        refusal = self.notes("refusal")[-1]
        evidence = pilot_gate.evidence_dir(env.paths, env.contract.sha256)
        for name in ("row", "book"):            # the refusal carries the digests of the bytes it classified (Q8)
            stem = f"{refusal['reservation_seq']:08d}-{name}-{refusal[name + '_sha256']}"
            self.assertEqual(hashlib.sha256((evidence / f"{stem}.bin").read_bytes()).hexdigest(),
                             refusal[name + "_sha256"])

    def crash(self, kind, pilot_type=None):
        env = self.build()
        market = self.market()
        self.open()
        with sp.die_after(env.journal, kind, pilot_type), self.assertRaises(sp.Death):
            self.place(market)
        env.gateway._release()                    # the process died: its lock is gone
        return env, market

    def test_crash_after_the_reservation(self):
        env, market = self.crash("note", "reservation")
        reads = len(self.reads())
        restarted = sp.new_gateway(env)
        report = self.open(gateway=restarted)
        self.assertTrue(report["armed"], report["failures"])
        state = sp.fold(env, live_open_id=restarted._open_id)
        self.assertTrue(state.reservations[market.market_id]["interrupted"])
        self.assertEqual(self.place(market, gateway=restarted)["refusal"]["rule"], "pilot_market_already_reserved")
        self.assertEqual(len(self.reads()), reads)
        self.clean()

    def test_crash_after_the_place_record_before_the_submit(self):
        env, market = self.crash("note", "decision")
        restarted = sp.new_gateway(env)
        self.open(gateway=restarted)
        self.assertEqual(self.place(market, gateway=restarted)["refusal"]["rule"], "pilot_market_already_reserved")
        state = self.clean()
        record = state.place_records[0]
        self.assertTrue(record.unsent)
        self.assertEqual((record.submit_seq, state.lifetime_used), (None, 1))
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual(record.measurement["class"], "definitely_unsent")

    def test_no_second_decision_after_cancel_settle_restart_or_resume(self):
        env = self.build()
        market = self.market()
        self.open()
        result = self.place(market)
        self.assert_sent(result)
        env.gateway.cancel(market.market_id, [result["pilot"]["bet_id"]], source="pilot")
        env.gateway.close()
        reads = len(self.reads(sp.CATALOGUE, market))

        def refused_again(gateway):
            self.open(gateway=gateway)
            self.assertEqual(self.place(market, gateway=gateway)["refusal"]["rule"], "pilot_market_already_reserved")
            gateway.close()
            self.assertEqual(len(self.reads(sp.CATALOGUE, market)), reads)

        refused_again(sp.new_gateway(env))                                  # after cancel and a restart
        market.status = "CLOSED"
        env.exchange.hide_cancelled_unmatched = True
        # The session is still armed here: P1-08 §4.13 refuses a second-process settle, so this one is pilot-run's
        # own settle point (in_session_settle), the only settle allowed during an armed session.
        settler = sp.new_gateway(env, in_session_settle=True)
        settler.preconfirm("settle-market", market_id=market.market_id)
        settler.open(command="settle-market", confirm_token=env.token)
        self.addCleanup(settler.close)
        self.assertEqual(settler.settle_market(market.market_id, confirm_token=env.token)["status"], "settled")
        settler.close()
        refused_again(sp.new_gateway(env))                                  # after settle-market
        resumed = sp.new_gateway(env)
        self.assertEqual(resumed.resume(confirm_token=env.token)["status"], "resumed")
        refused_again(sp.new_gateway(env))                                  # after RESUME
        self.clean()



class FirstByteTests(PilotCase):
    """Note §5 and §14 first-byte cases (:210): each refusal is FAILED(transport_not_sent) with no byte written, no
    order request, the slot still used and the market still reserved; every one would send on code without the
    pilot gate."""

    def at_gate(self, hook):
        self.env.transport.before_gate = hook

    def test_kill_file_creation_fails_but_the_halt_is_committed(self):
        env = self.build()
        market = self.market()
        self.open()
        killer = sp.new_gateway(env, clock=support.FakeClock(env.clock.utc_now()))   # its re-sweeps wait on its own

        def kill():
            killer.open(command="kill")
            with patch.object(gateway_module, "engage", side_effect=OSError("read-only")):
                out = killer.kill(reason="test")
            killer.close()
            self.assertEqual(out["status"], "kill_incomplete")

        self.at_gate(kill)
        result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "pilot_gate_head_moved")
        self.assertFalse(os.path.exists(env.paths.kill_file))
        self.assertIn("kill", self.kinds())
        self.clean()

    def test_kill_incomplete_halt_without_the_lock(self):
        env = self.build()
        market = self.market()
        self.open()
        self.at_gate(lambda: self.assertEqual(cli._journal_kill_incomplete(env.paths, env.clock), "appended"))
        self.assert_not_sent_at_first_byte(self.place(market), "pilot_gate_head_moved")
        self.clean()

    def test_a_pilot_stop_or_any_foreign_event_from_another_process(self):
        for label in ("pilot stop", "foreign note"):
            with self.subTest(case=label):
                env = self.build()
                market = self.market()
                self.open()
                writer = sp.other_writer(env)
                if label == "pilot stop":
                    self.at_gate(lambda: sp.note(env, "stop", journal=writer, stop_id="yu-check-1", scope="review",
                                                 reason="manual_check", level_p=None, source_seq=None))
                else:
                    self.at_gate(lambda: writer.append("note", {"reason": "capture_c_by_another_process"}))
                self.assert_not_sent_at_first_byte(self.place(market), "pilot_gate_head_moved")
                self.clean()

    def test_kill_file_at_the_first_byte_journals_a_session_stop(self):
        """Check 1 as today, and critique 2: the KILL file seen journals a session stop after the report."""
        env = self.build()
        market = self.market()
        self.open()
        self.at_gate(lambda: engage(env.paths, "test", clock=env.clock))
        result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "kill_engaged_before_send")
        self.assertEqual(result["pilot"]["latch"], "session")
        events = sp.events(env)
        self.assertEqual([e["kind"] for e in events[-2:]], ["report", "note"])
        stop = events[-1]["payload"]["pilot"]
        self.assertEqual((stop["type"], stop["scope"], stop["reason"]), ("stop", "session", "kill_file"))
        self.clean()

    def test_a_delayed_open(self):
        env = self.build()
        market = self.market()
        login = env.session.login

        def slow_login(**kw):
            env.clock.advance(20)
            return login(**kw)

        with patch.object(env.session, "login", side_effect=slow_login):
            self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"]), ("SKIP", "window"))
        self.assertEqual(env.router.order_requests, [])
        self.clean()

    def step_before_send_book(self, step):
        """Run step right before the funds read, which precedes the send book (fresh funds, fresh book)."""
        env = self.env
        funds = env.gateway._funds

        def stepped(*args, **kw):
            step()
            return funds(*args, **kw)

        return patch.object(env.gateway, "_funds", side_effect=stepped)

    def test_sleep_and_clock_jumps_back_are_caught_by_the_drift_check_alone(self):
        """A sleep (the wall clock 10 s ahead) is caught by the drift check alone. A jump back of 3 s, more than
        max_book_age_s, now refuses earlier, at the risk decision judged at the retained time (P106-R3-1): the
        fresh send book is 3 s old at the latest time the open has seen (V10, a review stop)."""
        env = self.build()
        market = self.market()
        self.open()
        with self.step_before_send_book(lambda: env.clock.step_wall(10)):
            result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "pilot_gate_clock_drift")
        self.clean()
        env = self.build()
        market = self.market()
        self.open()
        with self.step_before_send_book(lambda: env.clock.step_wall(-3)):
            result = self.place(market)
        self.assertEqual((result["decision"], result["pilot"]["latch"]), ("refused", "review"))
        self.assertIn(("validation:V10", "book_stale_wall_clock"), risk_failures(result))
        self.assertEqual(env.router.order_requests, [])
        self.clean()

    def test_a_jump_past_the_send_deadline_is_refused_before_the_submit(self):
        env = self.build()
        market = self.market()
        self.open()
        with self.step_before_send_book(lambda: env.clock.advance(60)):
            result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("pilot_submit_too_late", "review"))
        self.assertNotIn("submit", self.kinds())
        self.assertEqual(env.router.order_requests, [])
        self.clean()

    def test_check_4_at_the_first_byte_for_the_send_deadline_and_the_session_end(self):
        """With the 30 s margin stood down (an optimisation), a wall clock crossing the end between the submit and
        the first byte is refused by check 4: at the send deadline, and at a session end (UNTIL) that comes first
        (reachable only with the 6-minute end margin stood down too, Q13)."""
        for label, until in (("send deadline", sp.UNTIL), ("session end", OFF - 270 * S)):
            with self.subTest(end=label), patch.object(pilot_gate, "DECISION_MARGIN_NS", 0), \
                    patch.object(pilot_gate, "END_MARGIN_NS", 0):
                env = self.build(until_ns=until)
                market = self.market()
                self.open()
                end = min(OFF - 240 * S, until)
                with self.step_before_send_book(lambda: sp.set_time(env.clock, end - S // 2)):
                    self.at_gate(lambda: env.clock.step_wall(1))
                    result = self.place(market)
                self.assert_not_sent_at_first_byte(result, "pilot_gate_deadline")
                self.clean()

    def test_check_5_is_the_send_by_bound(self):
        """Monotonic time past the remaining window: send_by (the decision book's monotonic time plus the window
        left) stops the request before the gate runs; the gate's own check 5 is the same bound (unit-tested)."""
        with patch.object(pilot_gate, "DECISION_MARGIN_NS", 0):
            env = self.build()
            market = self.market()
            self.open()
            end = OFF - 240 * S
            with self.step_before_send_book(lambda: sp.set_time(env.clock, end - S // 2)):
                def mono_ahead():
                    env.clock.advance(1)
                    env.clock.step_wall(-1.5)

                self.at_gate(mono_ahead)
                result = self.place(market)
            self.assert_not_sent_at_first_byte(result, "send_window_expired")
            self.clean()

    def test_another_process_past_until_after_classification(self):
        """The expiry-clock edge: a writer whose clock is past UNTIL journals a note after the decision. Before the
        authority fold it makes the session expired there; after it, the intent's compare-and-append refuses. No
        intent either way, and no breach."""
        for when in ("before the authority fold", "during the funds read"):
            with self.subTest(when=when):
                env = self.build()
                market = self.market()
                self.open()
                late = support.FakeClock(utc_from_ns(sp.UNTIL + S))
                writer = sp.other_writer(env, clock=late)
                target = "_pilot_send" if when.startswith("before") else "_funds"
                original = getattr(env.gateway, target)

                def foreign_first(*args, original=original, **kw):
                    writer.append("note", {"reason": "another_process_after_until"})
                    return original(*args, **kw)

                with patch.object(env.gateway, target, side_effect=foreign_first):
                    result = self.place(market)
                expected = "pa3_session_expired" if when.startswith("before") else "journal_changed_since_decision"
                self.assertEqual(result["refusal"]["rule"], expected)
                self.assertNotIn("intent", self.kinds())
                self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_a_submit_stamped_past_the_end_is_never_written(self):
        """P106-2 for the submit: stamped at or after the boundary though G9 passed, it is refused inside the write
        (pilot_submit_too_late, the intent refused), not sent and not a breach."""
        env = self.build()
        market = self.market()
        self.open()
        append = env.journal.append

        def late_submit(kind, payload, **columns):
            if kind == "submit":
                with patch.object(env.clock, "utc_now", lambda: utc_from_ns(OFF - 240 * S)):
                    return append(kind, payload, **columns)
            return append(kind, payload, **columns)

        with patch.object(env.journal, "append", side_effect=late_submit):
            result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("pilot_submit_too_late", "review"))
        stops, carried_id = self.carried(result, "risk_decision")             # P1-08 §3.2: the refusing decision
        self.assertEqual((self.notes("refusal"), stops),
                         ([], [(carried_id("pilot_submit_too_late"), "pilot_submit_too_late")]))
        self.assertNotIn("submit", self.kinds())
        self.assertEqual([o["state"] for o in result["orders"]], ["REFUSED"])
        self.clean()

    def test_p106_1_jumps_during_own_appends_are_retained(self):
        """The intent or the risk decision stamped past the send deadline by a wall jump that then reverses: the
        largest observed time is kept (P106-1), so G9 refuses before the submit, though clock, drift and every
        foreign head check pass again."""
        for kind in ("intent", "risk_decision"):
            with self.subTest(jump_during=kind):
                env = self.build()
                market = self.market()
                self.open()
                append = env.journal.append

                def jumping(event_kind, payload, kind=kind, **columns):
                    if event_kind == kind:
                        env.clock.step_wall(70)
                        try:
                            return append(event_kind, payload, **columns)
                        finally:
                            env.clock.step_wall(-70)
                    return append(event_kind, payload, **columns)

                with patch.object(env.journal, "append", side_effect=jumping):
                    result = self.place(market)
                self.assertEqual(result["refusal"]["rule"], "pilot_submit_too_late")
                self.assertNotIn("submit", self.kinds())
                self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_p106_4_nothing_foreign_before_the_intent(self):
        """A foreign stop, or a second gateway reserving after a lost lock, during the funds or send-book fetch: no
        pilot intent is written (no intent_after_open_ended breach), nothing is sent."""
        for case in ("foreign stop", "second gateway"):
            with self.subTest(case=case):
                env = self.build()
                market, other_market = self.market(), self.market(off_ns=OFF - S)
                self.open()
                if case == "second gateway":
                    os.unlink(env.paths.gateway_lock)
                    other = sp.new_gateway(env)
                    self.open(gateway=other)
                funds = env.gateway._funds
                outcome = {}

                def interleave(funds=funds, case=case):
                    if case == "foreign stop":
                        sp.note(env, "stop", journal=sp.other_writer(env), stop_id="yu-check-2", scope="review",
                                reason="manual_check", level_p=None, source_seq=None)
                    else:
                        outcome["other"] = self.place(other_market, gateway=other)
                    return funds()

                with patch.object(env.gateway, "_funds", side_effect=interleave):
                    result = self.place(market)
                expected = "journal_changed_since_decision" if case == "foreign stop" else "gateway_lock_lost"
                self.assertEqual(result["refusal"]["rule"], expected)
                intents = [e for e in sp.events(env) if e["kind"] == "intent"]
                self.assertNotIn(result["pilot"]["customer_order_ref"], [e["customer_order_ref"] for e in intents])
                if case == "second gateway":
                    self.assert_sent(outcome["other"])
                else:
                    self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_residual_race_is_accepted_and_pinned(self):
        """Note §5 "What is left": a HALT committed after check 3 read the head is not seen; the order is sent."""
        env = self.build()
        market = self.market()
        self.open()
        head_fresh = env.journal.head_fresh

        def halt_after_the_read():
            head = head_fresh()
            sp.other_writer(env).append("halt", {"reason": "kill_incomplete", "scope": "global"})
            return head

        with patch.object(env.journal, "head_fresh", side_effect=halt_after_the_read):
            result = self.place(market)
        self.assert_sent(result)
        self.clean()

    def test_nothing_is_appended_between_the_submit_and_the_first_byte(self):
        env = self.build()
        market = self.market()
        self.open()
        seen = []
        self.at_gate(lambda: seen.append((env.journal.head_fresh(), sp.events(env)[-1])))
        self.assert_sent(self.place(market))
        head, last = seen[0]
        self.assertEqual((last["kind"], head), ("submit", {"seq": last["seq"], "last_sha256": last["event_sha256"]}))
        self.clean()



class OpenChecksTests(PilotCase):
    """PA1-PA4 at the pilot-place open (note §2, §6, §7; §14 commands :211) and Q6 (Yu 2026-10-21)."""

    def refused_open(self, env=None):
        env = env or self.env
        report = self.open()
        self.assertFalse(report["armed"])
        with self.assertRaises(NotArmed) as caught:
            self.place(self.market())
        env.gateway.close()
        return report["failures"], caught.exception.rule

    def review_codes(self):
        state = sp.fold(self.env)
        return [stop["reason"] for stop in state.active_stops if stop["scope"] == "review"]

    def test_identity_drift_refuses_with_a_review_stop(self):
        cases = {
            "pa3_config_differs": lambda env: sp.write_receipt(env, config_sha256="0" * 64),
            "pa2_commit_differs": lambda env: setattr(env.git, "commit", "1" * 40),
            "pa2_tree_differs": lambda env: setattr(env.git, "tree", "2" * 40),
            "pa2_tree_dirty": lambda env: setattr(env.git, "dirty", True),
            "pa2_code_hashes_differ": lambda env: env.state.disk_hashes.update({"x.py": "3" * 64}),
            "pa2_identity_unknown": lambda env: setattr(env.git, "fail", True),
            "pa1_contract_differs": lambda env: setattr(env.state, "contract_raw", contract_bytes(
                **{"placement_limits.lifetime_place_records": 494})),
            "pa1_contract_unreadable": lambda env: setattr(env.state, "contract_raw", OSError("gone")),
            "pa1_contract_invalid": lambda env: setattr(env.state, "contract_raw", b"{not json"),
        }
        for code, drift in cases.items():
            with self.subTest(code=code):
                env = self.build()
                if code == "pa3_config_differs":
                    sha = drift(env)
                    receipt = env.receipt_sha256
                    env.receipt_sha256 = sha
                    sp.note(env, "registration", registration_receipt_sha256=sha, predecessor_contract_sha256=None)
                    env.clock.advance(86400)
                    env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
                    self.assertNotEqual(receipt, sha)
                else:
                    drift(env)
                failures, rule = self.refused_open(env)
                self.assertEqual(rule, failures[0])
                self.assertIn(code, failures)
                refusal = self.notes("refusal")[-1]
                self.assertEqual((refusal["code"], refusal["contract_sha256"]), (code, sp.C.sha256))
                self.assertIn(code, self.review_codes())
                self.clean()

    def test_no_registration_journals_nothing(self):
        env = self.build(register=False, arm=False)
        before = self.kinds()
        failures, _ = self.refused_open()
        self.assertEqual(failures, ["pa3_registration_unreadable", "pa3_session_not_armed"])
        self.assertEqual([k for k in self.kinds() if k == "note"], [k for k in before if k == "note"])
        self.assertEqual(sp.pilot_notes(env), [])

    def test_repeated_opens_before_arm_or_after_the_end_pile_no_stops(self):
        """Q6/critique 4: an unarmed or expired session and an ended pilot refuse every open, with no new stop."""
        env = self.build(arm=False)
        for _ in range(3):
            self.assertEqual(self.refused_open()[0], ["pa3_session_not_armed"])
        self.assertEqual(self.notes("refusal"), [])
        env.session_id = sp.arm_session(env, until_ns=sp.OFF)
        sp.set_time(env.clock, sp.OFF)
        for _ in range(2):
            self.assertEqual(self.refused_open()[0], ["pa3_session_expired"])
        sp.note(env, "ending", reason="yu_written", code="yu_ended_in_writing")
        for _ in range(2):
            self.assertIn("pa4_placing_ended", self.refused_open()[0])
        self.assertEqual(self.notes("refusal"), [])
        self.clean()

    def test_an_active_stop_does_not_hide_a_new_identity_failure(self):
        env = self.build()
        sp.note(env, "stop", stop_id="yu-check-3", scope="review", reason="manual_check", level_p=None,
                source_seq=None)
        failures, rule = self.refused_open()
        self.assertEqual((failures, self.notes("refusal")), (["pa3_session_ended"], []))   # the stop ended it
        env.git.dirty = True
        failures, rule = self.refused_open()
        self.assertEqual(failures[0], "pa2_tree_dirty")
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["pa2_tree_dirty"])
        self.clean()

    def test_a_missing_contract_then_restored_keeps_its_review_stop(self):
        """Astra: when the damaged file cannot supply the contract, the refusal is journaled under the registered
        contract hash; restoring the file does not remove the stop it set."""
        env = self.build()
        env.state.contract_raw = OSError("gone")
        self.assertEqual(self.refused_open()[0][0], "pa1_contract_unreadable")
        env.state.contract_raw = sp.CONTRACT_RAW
        self.assertEqual(self.refused_open()[0], ["pa3_session_ended"])          # its stop ended the session
        self.assertIn("pa1_contract_unreadable", self.review_codes())
        env.clock.advance(86400)                                                  # and the next session too
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        self.assertEqual(self.refused_open()[0], ["pa4_pilot_stop"])
        self.assertEqual(len(self.notes("refusal")), 1)
        self.clean()

    def test_code_drift_after_the_imports_before_the_next_gateway(self):
        """The identity is pinned once per process: a disk change after it (and before this gateway) refuses."""
        env = self.build()
        report = self.open()
        self.assertTrue(report["armed"], report["failures"])
        env.gateway.close()
        env.state.disk_hashes = dict(sp.CODE_HASHES, **{"betangel_research/betfair/gateway.py": "9" * 64})
        later = sp.new_gateway(env)
        report = self.open(gateway=later)
        self.assertEqual(report["failures"], ["pa2_code_hashes_differ"])
        default = pilot_gate.PilotContext(repo_root=sp.REPO)
        self.assertEqual(default.pinned(), pilot_gate.process_code_hashes_sha256())
        self.assertIsNotNone(default.pinned())
        moved = pilot_gate.PilotContext(repo_root=sp.REPO / "tests", git_runner=env.git,
                                        code_hashes=lambda root: sp.CODE_HASHES,
                                        pinned_code_hashes_sha256=canonical_sha256(sp.CODE_HASHES))
        self.assertIn("pa2_module_root_differs", pilot_gate.code_identity(moved, pilot_gate.read_registration(
            env.paths, env.receipt_sha256)))

    def test_fold_failure_fails_placement_closed_and_cleanup_still_runs(self):
        """A pilot note naming another contract: every fold raises, so the open refuses pilot_fold_failed with
        nothing journaled, while cancel and kill (which never fold the pilot state) still run."""
        env = self.build()
        env.journal.append("note", pilot.pilot_note("refusal", "f" * 64, session_id=None, open_id=None,
                                                    reservation_seq=None, code="x", row_sha256=None,
                                                    book_sha256=None))
        before = self.kinds()
        failures, rule = self.refused_open()
        self.assertEqual((failures, rule), (["pilot_fold_failed"], "pilot_fold_failed"))
        self.assertEqual(self.kinds()[len(before):].count("note"), 0)
        bet = env.exchange.add_order(ref="bro-manual-1", strategy_ref="bar-manual")
        canceller = sp.new_gateway(env)
        canceller.open(command="cancel")
        self.addCleanup(canceller.close)
        self.assertTrue(canceller._report["armed"])
        canceller.close()
        killer = sp.new_gateway(env, clock=support.FakeClock(env.clock.utc_now()))
        killer.open(command="kill")
        self.addCleanup(killer.close)
        out = killer.kill(reason="test")
        self.assertIn(out["status"], ("kill_complete", "kill_incomplete"))
        self.assertIsNotNone(bet)

    def test_cleanup_runs_under_every_pa_failure_and_halt(self):
        """Note §7 and §8: PA1-PA4 gate placement only. Cancel, reconcile, kill and settle-market open armed under
        drifted identity, an unreadable contract, no session and a HALT."""
        env = self.build()
        env.git.dirty = True
        env.state.contract_raw = OSError("gone")
        env.state.disk_hashes = {"x.py": "1" * 64}
        sp.other_writer(env).append("halt", {"reason": "test", "scope": "global"})
        for command in ("cancel", "reconcile"):
            with self.subTest(command=command):
                gateway = sp.new_gateway(env)
                report = gateway.open(command=command)
                self.addCleanup(gateway.close)
                self.assertTrue(report["armed"], report["failures"])
                gateway.close()
        killer = sp.new_gateway(env, clock=support.FakeClock(env.clock.utc_now()))
        report = killer.open(command="kill")
        self.addCleanup(killer.close)
        self.assertTrue(report["armed"], report["failures"])
        self.assertIn(killer.kill(reason="test")["status"], ("kill_complete", "kill_incomplete"))
        self.assertEqual(env.state.contract_reads, 0)            # cleanup never reads the contract

    def test_resume_on_an_ended_contract(self):
        env = self.build()
        sp.note(env, "ending", reason="yu_written", code="yu_ended_in_writing")
        sp.other_writer(env).append("halt", {"reason": "test", "scope": "global"})
        self.assertEqual(env.gateway.resume(confirm_token=env.token)["halted"], False)
        failures, _ = self.refused_open()
        self.assertIn("pa4_placing_ended", failures)
        self.assertEqual(self.notes("refusal"), [])
        self.clean()


class StopsAndLatchTests(PilotCase):
    """Durable stops, the note §9 latches, carryover and critique 1-2."""

    def test_exposure_full_uses_no_slot(self):
        """Critique 1: with ten unsettled £2 orders R9 refuses before the reservation: nothing journaled, no read,
        no slot."""
        env = self.build(pt4_done=True)
        for n in range(10):
            market = self.market(off_ns=OFF + n * 1800 * S)
            sp.set_time(env.clock, market.off_ns - 299 * S)
            self.open()
            self.assertEqual(self.place(market)["pilot"]["state"], "EXECUTABLE")
            env.gateway.close()
        eleventh = self.market(off_ns=OFF + 10 * 1800 * S)
        sp.set_time(env.clock, eleventh.off_ns - 299 * S)
        self.open()
        before, reads = self.kinds(), len(self.reads())
        result = self.place(eleventh)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("total_exposure", "market"))
        self.assertEqual((self.kinds(), len(self.reads())), (before, reads))
        state = self.clean()
        self.assertEqual(state.lifetime_used, 10)

    def test_unresolved_orders_and_daily_loss_journal_a_session_stop_and_no_reservation(self):
        """Critique 1: R3 (an order of ours left unresolved by another process's cancel) and R10 end the session
        before any reservation; no slot is used."""
        env = self.build()
        first, second = self.market(), self.market(off_ns=OFF + 1800 * S)
        self.open()
        placed = self.place(first)
        self.assertEqual(placed["pilot"]["state"], "EXECUTABLE")
        env.gateway.close()
        sp.set_time(env.clock, second.off_ns - 299 * S)
        self.open()
        ref = placed["pilot"]["customer_order_ref"]
        sp.other_writer(env).append("submit", {"action": "cancel", "instruction_count": 1, "body_sha256": None,
                                              "decision_id": "dec-other", "params": None,
                                              "transitions": [{"customer_order_ref": ref, "state": "CANCEL_PENDING"}]},
                                    market_id=first.market_id)
        result = self.place(second)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("unresolved_orders", "session"))
        self.assertEqual((len(self.notes("reservation")), self.reads(sp.CATALOGUE, second)), (1, []))
        self.assertEqual([(s["scope"], s["reason"]) for s in self.notes("stop")], [("session", "unresolved_orders")])
        self.clean()
        raw = contract_bytes(**{"caps.max_daily_loss_p": 200})
        env = self.build(contract_raw=raw, overrides={"orders": {"caps": {"max_daily_loss": 2.0}}})
        first, second = self.market(), self.market(off_ns=OFF + 1800 * S)
        self.open()
        self.assertEqual(self.place(first)["pilot"]["state"], "EXECUTABLE")
        env.gateway.close()
        sp.set_time(env.clock, second.off_ns - 299 * S)
        self.open()
        result = self.place(second)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("daily_loss", "session"))
        self.assertEqual(len(self.notes("reservation")), 1)
        self.clean()

    def test_kill_file_seen_ends_the_session_in_the_journal(self):
        """Critique 2: a KILL file seen by the pilot path journals a session stop; after `rm KILL` the next open is
        refused (the same ARM line cannot place again)."""
        env = self.build()
        market = self.market()
        self.open()
        engage(env.paths, "test", clock=env.clock)
        result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("kill_engaged", "session"))
        self.assertEqual(self.notes("reservation"), [])
        stop = self.notes("stop")[-1]
        self.assertEqual((stop["scope"], stop["reason"]), ("session", "kill_file"))
        env.gateway.close()
        os.unlink(env.paths.kill_file)
        report = self.open()
        self.assertIn("pa3_session_ended", report["failures"])
        self.clean()

    def test_kill_file_at_the_risk_decision_ends_the_session(self):
        env = self.build()
        market = self.market()
        self.open()
        funds = env.gateway._funds

        def engage_then_funds():
            engage(env.paths, "test", clock=env.clock)
            return funds()

        with patch.object(env.gateway, "_funds", side_effect=engage_then_funds):
            result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("halted", "session"))
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual(self.notes("stop")[-1]["reason"], "kill_file")
        self.clean()

    def test_risk_refusals_and_their_latches(self):
        """Table-driven at the gateway: a V9 runner removal on the send book (review), V13 movement (market), a
        stale capability (review), each before any byte; each refusal note's derived stop id validates."""
        def runner_removed(env, market):
            market.statuses[sp.FAV] = "REMOVED"

        def price_moved(env, market):
            market.backs[sp.FAV] = (("3.20", "15.00"),)
            market.lays[sp.FAV] = (("3.25", "20.00"),)

        def stale_capability(env, market):
            stack = contextlib.ExitStack()
            stack.enter_context(patch.object(env.guard, "issue",
                                             side_effect=OrderRefused("capability_decision_stale")))
            self.addCleanup(stack.close)

        cases = {"validation:V9": (runner_removed, "review"), "validation:V13": (price_moved, "market"),
                 "capability_decision_stale": (stale_capability, "review")}            # workflow #5
        for rule, (change, latch) in cases.items():
            with self.subTest(rule=rule):
                env = self.build()
                market = self.market()
                self.open()
                funds = env.gateway._funds

                def changed_then_funds(funds=funds, change=change, market=market, env=env):
                    change(env, market)
                    return funds()

                with patch.object(env.gateway, "_funds", side_effect=changed_then_funds):
                    result = self.place(market)
                self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), (rule, latch))
                self.assertEqual(env.router.order_requests, [])
                self.assertNotIn("submit", self.kinds())
                state = self.clean()
                if latch == "review":
                    stop = [s for s in state.active_stops if s["scope"] == "review"][-1]
                    self.assertEqual(stop["reason"], pilot_gate.refusal_code(rule))
                    self.assertRegex(stop["stop_id"], pilot.STOP_ID_RE.pattern)
                else:
                    self.assertEqual(self.notes("refusal"), [])

    def test_mixed_causes_latch_every_cause(self):
        """A refusing risk decision with several causes: a session stop per session cause (rate_day, daily_loss),
        one refusal note for the first review cause, none for a market-only cause; the result names the strongest."""
        env = self.build()
        self.open()
        env.gateway._pilot_contract = env.contract
        env.gateway._pilot_session_id = env.session_id
        decision = {"detail": {"failures": [
            {"rule": "rate_hour", "reason": "orders_per_hour_cap"},
            {"rule": "rate_day", "reason": "orders_per_day_cap"},
            {"rule": "daily_loss", "reason": "daily_loss_budget_exceeded"},
            {"rule": "validation:V0", "reason": "x"}, {"rule": "funds", "reason": "y"}]}}
        context = {"reservation_seq": 77, "latch": None}
        env.gateway._pilot_latch_decision(context, decision)
        self.assertEqual(context["latch"], "review")
        self.assertEqual([(s["scope"], s["stop_id"]) for s in self.notes("stop")],
                         [("session", "rate-day-77-session"), ("session", "daily-loss-77-session")])
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["validation_v0"])
        market_only = {"reservation_seq": 78, "latch": None}
        env.gateway._pilot_latch_decision(market_only, {"detail": {"failures": [{"rule": "validation:V13",
                                                                                   "reason": "z"}]}})
        self.assertEqual((market_only["latch"], len(self.notes("refusal"))), ("market", 1))

    def test_a_pause_skip_journals_its_stops_and_survives_restart_and_resume(self):
        """Note §10's pause, scaled down with a test-only contract (pause step 400p): one sent bet at worst case
        -200p, so the next PLACE would reach -400p: SKIP(pause), the session and review stops journaled at once,
        nothing sent; a restart and RESUME leave placement refused."""
        raw = contract_bytes(**{"pause.step_p": 400})
        env = self.build(contract_raw=raw)
        first, second = self.market(), self.market(off_ns=OFF + 1800 * S)
        self.open()
        self.assertEqual(self.place(first)["pilot"]["state"], "EXECUTABLE")
        env.gateway.close()
        sp.set_time(env.clock, second.off_ns - 299 * S)
        self.open()
        result = self.place(second)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"], result["pilot"]["latch"]),
                         ("SKIP", "pause", "review"))
        self.assertEqual(sorted((s["scope"], s["stop_id"]) for s in self.notes("stop")),
                         [("review", "pause-400"), ("session", "pause-400-session")])
        self.assertEqual(len(env.router.order_requests), 1)
        env.gateway.close()
        self.assertEqual(env.gateway.resume(confirm_token=env.token)["status"], "resumed")
        restarted = sp.new_gateway(env)
        report = self.open(gateway=restarted)
        self.assertEqual(report["failures"], ["pa3_session_ended"])                 # the pause's session stop
        restarted.close()
        env.clock.advance(86400)
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        self.assertEqual(self.open(gateway=sp.new_gateway(env))["failures"], ["pa4_pilot_stop"])   # review: ACK
        self.clean()

    def test_reregistering_the_same_contract_keeps_slots_and_stops(self):
        env = self.build()
        market = self.market()
        self.open()
        self.assert_sent(self.place(market))
        env.gateway.close()
        sp.note(env, "stop", stop_id="yu-check-4", scope="review", reason="manual_check", level_p=None,
                source_seq=None)
        env.clock.advance(86400)
        env.receipt_sha256 = sp.register_pilot(env)
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        state = sp.fold(env)
        self.assertEqual((state.lifetime_used, len(self.notes("registration"))), (1, 2))
        report = self.open()
        self.assertIn("pa4_pilot_stop", report["failures"])
        self.assertIn(market.market_id, state.reservations)
        self.clean()

    def test_a_classify_refusal_and_its_ack(self):
        """P1-05 duty: a classify refusal is journaled with its digests and the fold derives the review stop
        '<code>-<res>-<note seq>'; a PILOT ACK of it (journaled as P1-08 will) and a new ARM let the next open place."""
        env = self.build()
        market, second = self.market(), self.market(off_ns=OFF + 86400 * S)
        self.open()
        original = env.read.raw_decision_book

        def stop_during_the_read(*args, **kw):
            out = original(*args, **kw)                    # another process ends the session meanwhile
            sp.note(env, "stop", journal=sp.other_writer(env), stop_id="yu-stop-s", scope="session",
                    reason="manual_check", level_p=None, source_seq=None)
            return out

        with patch.object(env.read, "raw_decision_book", side_effect=stop_during_the_read):
            result = self.place(market)
        self.assertEqual(result["refusal"]["rule"], "session_ended")
        note_event = sp.pilot_notes(env, "refusal")[-1]
        stop_id = f"session-ended-{result['pilot']['reservation_seq']}-{note_event['seq']}"
        self.assertIn(stop_id, [stop["stop_id"] for stop in sp.fold(env).active_stops])
        env.gateway.close()
        self.assertEqual(self.open()["failures"], ["pa3_session_ended"])      # journals the due stop, then refuses
        self.assertIn(stop_id, [stop["stop_id"] for stop in self.notes("stop")])
        env.gateway.close()
        sp.set_time(env.clock, second.off_ns - 299 * S - 3600 * S)
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        sp.set_time(env.clock, second.off_ns - 299 * S)
        # yu-stop-s ended only yesterday's session; the refusal's own review stop alone still holds the new one.
        self.assertEqual([stop["stop_id"] for stop in sp.fold(env).active_stops], [stop_id])
        self.assertEqual(self.open()["failures"], ["pa4_pilot_stop"])
        env.gateway.close()
        sp.note(env, "ack", stop_id=stop_id, cause_written=True)
        self.open()
        self.assert_sent(self.place(second))
        self.clean()

    def test_earlier_unknown_resolved_at_open_is_a_review_stop(self):
        """Note §9: a pilot order whose outcome was unknown, found live by the next open's reconcile, refuses that
        open's placement (earlier_unknown_resolved_at_open) with a review stop and nothing reserved."""
        from betangel_research.betfair.errors import ApiError
        env = self.build(pt4_done=True)
        market, second = self.market(), self.market(off_ns=OFF + 1800 * S)
        self.open()
        accept = env.exchange.place

        def placed_but_lost(params):
            env.exchange.place_handler = None
            accept(params)
            raise TransportError("connection_lost", phase="receive", may_have_arrived=True)

        env.exchange.place_handler = placed_but_lost
        env.exchange.read_failures["listCurrentOrders"] = [ApiError("TIMEOUT_ERROR", surface="aping")] * 200
        result = self.place(market)
        self.assertTrue(result["pilot"]["unknown"], result["pilot"])
        env.gateway.close()
        env.exchange.read_failures.clear()
        sp.set_time(env.clock, second.off_ns - 299 * S)
        self.open()
        self.assertTrue(env.gateway._resolved_at_open)
        for _ in range(2):                         # a second call in the same open: refused alike, no new note
            refused = self.place(second)
            self.assertEqual((refused["refusal"]["rule"], refused["pilot"]["latch"]),
                             ("earlier_unknown_resolved_at_open", "review"))
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["earlier_unknown_resolved_at_open"])
        self.assertEqual(len(self.notes("reservation")), 1)
        self.clean()


class AuthorityAtAppendTests(PilotCase):
    """P106-2 and P106-3 end to end, the trigger guard (Q12) and a fold that raises mid-protocol."""

    def test_late_reservation_and_decision_appends_are_refused_unwritten(self):
        for kind in ("reservation", "decision"):
            with self.subTest(late=kind):
                env = self.build()
                market = self.market()
                self.open()
                append = env.journal.append

                def late(event_kind, payload, kind=kind, **columns):
                    inner = payload.get("pilot") if isinstance(payload, dict) else None
                    if isinstance(inner, dict) and inner.get("type") == kind:
                        with patch.object(env.clock, "utc_now", lambda: utc_from_ns(sp.UNTIL)):
                            return append(event_kind, payload, **columns)
                    return append(event_kind, payload, **columns)

                with patch.object(env.journal, "append", side_effect=late):
                    result = self.place(market)
                step = "reserve" if kind == "reservation" else "decision"
                self.assertEqual(result["refusal"]["rule"], f"pilot_{step}_too_late")
                self.assertEqual(self.notes(kind), [])
                self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_the_cap_records_own_send_goes_out_and_nothing_after_it(self):
        """P106-3: with a one-record test contract, the PLACE record that takes the last slot is sent (its own send
        is not refused by the lifetime-cap ending); the next open is refused with no new stop."""
        raw = contract_bytes(**{"placement_limits.lifetime_place_records": 1,
                                "placement_limits.place_records_per_pt4_session_day": 1})
        env = self.build(contract_raw=raw)
        market = self.market()
        self.open()
        self.assert_sent(self.place(market))
        state = self.clean()
        self.assertEqual(pilot.placing_ending(state, now_ns=state.now_ns)["reason"], "lifetime_cap")
        env.gateway.close()
        report = self.open()
        self.assertIn("pa4_placing_ended", report["failures"])
        self.assertEqual(self.notes("refusal"), [])

    def preload_yesterday(self, off_offset_s):
        """A PLACE record on the previous day whose BSP-cutoff trigger (off + 24 h + 1 ns) lands off_offset_s
        after today's 09:55:01 reservation instant."""
        env = self.build(start=pilot.utc_text_from_ns(OFF - 299 * S - 86400 * S - 296 * S + off_offset_s * S),
                         until_ns=sp.UNTIL - 86400 * S, pt4_done=True)
        sp.preload_place_records(env, 1)
        sp.set_time(env.clock, OFF - 299 * S)
        env.session_id = sp.arm_session(env)
        return env

    def test_trigger_guard_exact_boundaries(self):
        cases = {"far": (300, "sent"), "inside the horizon": (60, "pilot_trigger_before_deadline"),
                 "within 2 s": (1, "sent")}
        for label, (offset, expected) in cases.items():
            with self.subTest(trigger=label):
                env = self.preload_yesterday(offset)
                trigger = pilot.next_trigger_ns(sp.fold(env))
                self.assertEqual(trigger - env.clock.time_ns(), offset * S + 1)
                market = self.market()
                self.open()
                result = self.place(market)
                if expected == "sent":
                    self.assert_sent(result)
                    if offset == 1:
                        self.assertGreater(sp.pilot_notes(env, "reservation")[-1]["recorded_at"],
                                           utc_from_ns(trigger))
                else:
                    self.assertEqual(result["refusal"]["rule"], expected)
                    self.assertNotIn(market.market_id, sp.fold(env).reservations)
                    self.assertEqual(self.reads(sp.CATALOGUE, market), [])
                self.clean()

    def test_a_fold_that_raises_never_crashes_mid_protocol(self):
        env = self.build()
        market = self.market()
        self.open()
        fold = pilot.fold
        calls = []

        def fails_on_the_classify_fold(*args, **kw):
            calls.append(1)
            if len(calls) == 2:                                   # the first fold after the reservation
                raise pilot.PilotError("events_invalid")
            return fold(*args, **kw)

        with patch.object(pilot, "fold", side_effect=fails_on_the_classify_fold):
            result = self.place(market)
        self.assertEqual(result["refusal"]["rule"], "pilot_fold_failed")
        self.assertEqual((len(self.notes("reservation")), self.notes("decision")), (1, []))
        env.gateway.close()
        self.assertTrue(self.clean().reservations[market.market_id]["interrupted"])


class ManualOrderTests(PilotCase):
    """Critique 13 with Astra's ruling and Yu's approval: the exceptions to "non-pilot behaviour unchanged"."""

    def manual(self, env, *, config=None, pilot_context=True, command="place"):
        gateway = sp.new_gateway(env, config=config, pilot_context=pilot_context)
        if command == "place":
            gateway.preconfirm("place", market_id="1.1", instructions=[gw.instruction()])
        else:
            gateway.preconfirm("live-probe", market_id="1.1", selection_id=gw.SELECTION)
        token = expected_token(config or env.config, env.clock.time_ns())
        try:
            gateway.open(command=command, confirm_token=token)
        finally:
            self.addCleanup(gateway.close)
        return gateway, token

    def test_manual_place_and_probe_refused_while_a_session_is_armed(self):
        """Read from the durable journal: also with orders.pilot and the PilotContext removed."""
        env = self.build()
        without = copy.deepcopy(env.config)
        without["orders"].pop("pilot")
        for label, config, context in (("pilot config", None, True), ("section removed", without, False)):
            for command in ("place", "live-probe"):
                with self.subTest(config=label, command=command):
                    before = self.kinds()
                    with self.assertRaises(OrderRefused) as caught:
                        self.manual(env, config=config, pilot_context=context, command=command)
                    self.assertEqual(caught.exception.rule, "manual_order_during_pilot_session")
                    self.assertEqual(self.kinds(), before)
        self.assertEqual(env.router.order_requests, [])

    def test_outside_a_session_manual_orders_carry_the_manual_ref(self):
        env = self.build(arm=False)
        market = self.market()
        order = gw.instruction(selection_id=sp.FAV, price_h=300)
        gateway = sp.new_gateway(env)
        gateway.preconfirm("place", market_id=market.market_id, instructions=[order])
        token = expected_token(env.config, env.clock.time_ns())
        gateway.open(command="place", confirm_token=token)
        self.addCleanup(gateway.close)
        result = gateway.place(market.market_id, [order], source="cli_manual", confirm_token=token)
        self.assertEqual(result["decision"], "accepted", result["refusal"])
        intent = [e for e in sp.events(env) if e["kind"] == "intent"][-1]
        self.assertEqual(intent["payload"]["customer_strategy_ref"], "bar-manual")
        self.assertEqual(env.router.order_requests[-1]["params"]["customerStrategyRef"], "bar-manual")
        submit = [e for e in sp.events(env) if e["kind"] == "submit"][-1]
        self.assertNotIn("pilot_record_sha256", submit["payload"])

    def test_untrusted_kill_sweeps_both_refs_without_pilot_artifacts(self):
        """The emergency sweep of a pilot-enabled profile names both refs from the config alone: an unfoldable journal,
        no registration receipt and no readable contract file."""
        env = self.build(register=False, arm=False)
        env.state.contract_raw = OSError("gone")
        sp.raw_insert(env, "note", {"transitions": [{"customer_order_ref": "bro-x", "state": "UNKNOWN"}]},
                      recorded_at=env.clock.utc_now())
        from betangel_research.betfair.credentials import anchor_write
        anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        killer = sp.new_gateway(env, clock=support.FakeClock(env.clock.utc_now()))
        report = killer.open(command="kill")
        self.addCleanup(killer.close)
        self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, report["notes"])
        out = killer.kill(reason="test")
        self.assertEqual(out["status"], "kill_incomplete")
        legs = [call[1].get("customer_strategy_refs") for call in env.exchange.calls
                if call[0] == "listCurrentOrders" and call[1].get("customer_strategy_refs")]
        self.assertEqual(sorted({ref for leg in legs for ref in leg}), ["bar-manual", "p1-fav-t5"])
        self.assertEqual(env.state.contract_reads, 0)
        self.assertFalse(env.paths.pilot_dir.exists())             # no receipt, no evidence: nothing pilot at all



class PerformanceTests(PilotCase):
    """Critique 3: with a representative journal (494 PLACE records and at least 20,000 events) the 495th placement's
    send book is still fresh at its first byte, in real elapsed time (time.perf_counter), not only on the FakeClock:
    the pilot fold runs before the funds read, never inside the book-age window. The clock moves with real time
    during the placement (RealTimeClock), so the risk decision and the first-byte gate judge the real duration, and
    the window ends when placeOrders reaches the router: after the gate and the transport's final send-by check
    (P1-08 timing; before, the before_gate hook ended it ahead of the gate's head read)."""

    def test_495th_record_inside_the_book_age_window(self):
        with patch.object(support, "FakeClock", sp.RealTimeClock):
            env = self.build(start="2026-12-14T08:00:00.000000Z", arm=False)
        events, today = sp.build_history(env, place_records=494, skips_per_place=1, min_events=20_000)
        self.assertGreaterEqual(events, 20_000)
        off = today + 19 * 3600 * S
        sp.set_time(env.clock, off - 299 * S - 60 * S)
        env.session_id = sp.arm_session(env, until_ns=today + 21 * 3600 * S)
        sp.set_time(env.clock, off - 299 * S)
        market = self.market(off_ns=off)
        marks = {}
        snapshot = env.read.market_snapshot

        def observed(*args, **kw):
            marks["book"] = time.perf_counter()
            return snapshot(*args, **kw)

        self.open()
        sp.first_byte_marks(env, marks)
        env.clock.start_real()
        started = time.perf_counter()
        with patch.object(env.read, "market_snapshot", side_effect=observed):
            result = self.place(market)
        total = time.perf_counter() - started
        window = marks["first_byte"] - marks["book"]
        limit = env.config["orders"]["caps"]["max_book_age_s"]
        self.assertLess(window, limit, f"book to first byte {window:.3f} s (whole placement {total:.3f} s)")
        self.assert_sent(result)
        record = self.notes("decision")[-1]
        self.assertEqual((record["lifetime_slot"], result["pilot"]["state"]), (495, "EXECUTABLE"))
        state = self.clean()
        self.assertEqual((state.lifetime_used, state.ending["reason"]), (495, "lifetime_cap"))



class LegacyPinTests(unittest.TestCase):
    """Non-pilot live behaviour byte for byte: a manual live place under a config with no orders.pilot journals the
    same events (every column and payload but the random event ids and hashes), the same decision id and the same
    request params as main 31ffc4c did (values computed on an export of that commit with deterministic refs)."""

    # NP-3 (P1-08 §1): the digests of this sequence computed on a 2d3b092 export (no P1-08 code at all).
    NON_PILOT_DIGESTS = {"events": "1fb1ba1188b37179293c24ea1c40c539e1820e348c39dc8aed36b4bfc63b7a9b",
                         "orders": "1bde15a23e9d778e6937f7b6f8f48a92678069b4380a9cfaf3c0158e10b80697",
                         "outputs": "8829ebadde085d33fa02cdeb26b1a1be77df4d486c8dbd638c7858440a107ae7"}

    def test_non_pilot_commands_unchanged_from_base(self):
        """§1's invariance test (NP-3): on a config without orders.pilot, place, cancel, live-probe, reconcile, place,
        settle-market twice (refused while the market is open, then settled once it is CLOSED and our bets are no
        longer current), kill, and resume after the KILL file is removed, through cli.main, give the same journal
        events (without event ids, chain hashes and monotonic stamps), the same order requests and the same
        (exit, stdout, stderr) per command as at 2d3b092. Refs, customer refs and intent ids are made deterministic."""
        import io as io_module
        import itertools
        from betangel_research.betfair import cli as cli_module
        from betangel_research.betfair.journal import OrderJournal as Journal
        env = gw.build_live_gateway(self)
        env.journal.close()
        self.assertNotIn("pilot", env.config["orders"])
        counter = itertools.count(1)
        deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
                "confirmer": env.confirmer}
        outputs = []

        def run(*argv):
            out, err = io_module.StringIO(), io_module.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_module.main(list(argv), deps=deps)
            root = str(env.paths.profile_dir.parent.parent)
            outputs.append([code, out.getvalue().replace(root, "<root>"), err.getvalue().replace(root, "<root>")])
            return code, out.getvalue()

        place = ["place", "--market", "1.1", "--selection", str(gw.SELECTION), "--side", "BACK", "--price", "3.50",
                 "--size", "2.00", "--mode", "live", "--confirm-live", env.token]
        with patch.object(gateway_module, "make_customer_order_ref", lambda now: f"ref-{next(counter):04d}"), \
                patch.object(gateway_module, "make_customer_ref", lambda now: f"cref-{next(counter):04d}"), \
                patch.object(gateway_module, "_intent_id", lambda: f"int-{next(counter):04d}"):
            bet = json.loads(run(*place)[1])["orders"][0]["bet_id"]
            run("cancel", "--market", "1.1", "--bet-id", bet, "--mode", "live", "--confirm-live", env.token)
            run("live-probe", "--market", "1.1", "--selection", str(gw.SELECTION), "--confirm-live", env.token)
            run("reconcile", "--mode", "live", "--confirm-live", env.token)
            bet2 = json.loads(run(*place)[1])["orders"][0]["bet_id"]
            run("settle-market", "--market", "1.1", "--confirm-live", env.token)
            run("cancel", "--market", "1.1", "--bet-id", bet2, "--mode", "live", "--confirm-live", env.token)
            env.exchange.market_status = "CLOSED"
            env.exchange.hide_cancelled_unmatched = True
            run("settle-market", "--market", "1.1", "--confirm-live", env.token)
            run("kill", "--reason", "invariance test")
            env.paths.kill_file.unlink()
            run("resume", "--mode", "live", "--confirm-live", env.token)
        self.assertEqual([o[0] for o in outputs], [0, 0, 0, 0, 0, 2, 0, 0, 0, 0])
        journal = Journal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        try:
            events = [{k: v for k, v in e.items() if k not in ("event_id", "prev_sha256", "event_sha256",
                                                               "monotonic_ns")} for e in journal.events()]
        finally:
            journal.close()
        found = {"events": canonical_sha256(events),
                 "orders": canonical_sha256([(r["method"], r["params"]) for r in env.router.order_requests]),
                 "outputs": canonical_sha256(outputs)}
        self.assertEqual(found, self.NON_PILOT_DIGESTS)

    MAIN = {"config_sha256": "08ab18b78cb15a9612f1de0d0962505ccb5458989b20b9d2904186f2d2dad921",
            "token": "live-08ab18b78cb1-20200101",
            "events_sha256": "bcb9d7d3dfc571fccf810d5a3321d792bdf80fe169f50847a971ecdaf1202d2f",
            "decision_id": ["dec-93b808ba777423c410f86059"],
            "requests": "ba25b807292728f5ce7db7f64e612161cdc48ee407c55869577d588674fb4949"}

    def test_manual_live_place_unchanged_from_main(self):
        counter = {"o": 0, "c": 0}

        def order_ref(now_ns, rng=None):
            counter["o"] += 1
            return f"bro200101120000-{counter['o']:016x}"

        def customer_ref(now_ns, rng=None):
            counter["c"] += 1
            return f"brq200101120000-{counter['c']:016x}"

        with patch.object(gateway_module, "make_customer_order_ref", order_ref), \
                patch.object(gateway_module, "make_customer_ref", customer_ref), \
                patch.object(gateway_module, "_intent_id", lambda: "int-0000000000000001"):
            env = gw.build_live_gateway(self)
            gw.open_live(self, env, instructions=[gw.instruction()])
            result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
            self.assertEqual(result["decision"], "accepted")
            env.gateway.close()
        events = [{k: v for k, v in e.items() if k not in ("event_id", "prev_sha256", "event_sha256", "monotonic_ns")}
                  for e in env.journal.events()]
        found = {"config_sha256": cfg.config_sha256(env.config), "token": env.token,
                 "events_sha256": canonical_sha256(events),
                 "decision_id": [e["payload"]["decision_id"] for e in events if e["kind"] == "risk_decision"],
                 "requests": canonical_sha256([r["params"] for r in env.router.order_requests])}
        self.assertEqual(found, self.MAIN)



class CodeReviewRound1Tests(PilotCase):
    """Astra's code review of 64aa0fd (P106-C1 to C4, item 5), the review workflow's items #1-#5 and the Codex item
    #10; each test failed on 64aa0fd. Production margins throughout unless a test says otherwise."""

    # --- P106-C1 -------------------------------------------------------------------------------------------------

    def test_c1_a_delayed_head_read_sends_nothing(self):
        """The head read comes before the KILL and freshness checks, and the transport rechecks its send-by after
        the gate: a head read delayed 3 s (past the 2 s book age) or 6 s (past the 5 s capability) sends nothing."""
        for delay in (3, 6):
            with self.subTest(delay_s=delay):
                env = self.build()
                market = self.market()
                self.open()
                head_fresh = env.journal.head_fresh

                def slow_head(delay=delay, head_fresh=head_fresh):
                    env.clock.advance(delay)
                    return head_fresh()

                with patch.object(env.journal, "head_fresh", side_effect=slow_head):
                    result = self.place(market)
                self.assert_not_sent_at_first_byte(result, "book_stale_before_send")
                self.assertEqual(result["pilot"]["latch"], "market")
                self.assertEqual(env.exchange.orders, {})
                self.clean()

    def test_c1_transport_rechecks_send_by_after_the_gate(self):
        from betangel_research.betfair import transport
        clock = support.FakeClock()
        send_by = clock.monotonic_ns() + 2 * S
        self.assertIsNone(transport.send_refusal(clock, send_by, lambda: None))
        self.assertEqual(transport.send_refusal(clock, send_by, lambda: clock.advance(2)), "send_window_expired")
        self.assertEqual(transport.send_refusal(clock, send_by, lambda: "book_stale_before_send"),
                         "send_window_expired")                  # the bound before the gate already passed

    # --- P106-C2 -------------------------------------------------------------------------------------------------

    def test_c2_a_raw_read_excursion_past_the_session_end_is_kept(self):
        """Astra's witness: the session ends at 10:10; both ends of the catalogue read observe 10:10:01; the clock
        returns before the book read. The open keeps that time: the classification refuses session_expired (a review
        stop), nothing is sent, and the session never becomes live again in this open."""
        until = OFF + 600 * S
        env = self.build(until_ns=until)
        market = self.market()
        self.open()
        raw = env.read.raw_catalogue_row

        def excursion(*args, **kw):
            wall = env.clock.wall_ns
            env.clock.wall_ns = until + S
            try:
                return raw(*args, **kw)
            finally:
                env.clock.wall_ns = wall

        with patch.object(env.read, "raw_catalogue_row", side_effect=excursion):
            result = self.place(market)
        self.assertEqual((env.router.order_requests, self.notes("decision")), ([], []))
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("session_expired", "review"))
        self.assertGreaterEqual(env.gateway._pilot_now(), until + S)
        self.assertLess(env.clock.time_ns(), until)
        self.clean()

    # --- P106-C3 -------------------------------------------------------------------------------------------------

    def test_c3_a_capability_refused_after_the_submit_keeps_its_review_stop(self):
        """Descheduled 6 s after the submit, before send_order consumes the capability: capability_expired, nothing
        sent, FAILED(transport_not_sent), and its review stop (Q11), unlike a first-byte refusal (market only). The
        next open is refused, and after a new ARM still refused until the ACK."""
        env = self.build()
        market, later = self.market(), self.market(off_ns=OFF + 86400 * S)
        self.open()
        env.gateway.on_send = lambda: env.clock.advance(6)
        result = self.place(market)
        self.assertEqual((result["send_exception"]["code"], result["pilot"]["latch"]), ("capability_expired", "review"))
        self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]], [("FAILED", "transport_not_sent")])
        self.assertEqual(env.router.order_requests, [])
        stops, carried_id = self.carried(result, "report")           # P1-08 §3.2: capability-expired-<res>-<report>
        self.assertEqual((self.notes("refusal"), stops),
                         ([], [(carried_id("capability_expired"), "capability_expired")]))
        env.gateway.close()
        env.gateway.on_send = None
        self.assertEqual(self.open()["failures"], ["pa3_session_ended"])
        env.gateway.close()
        stop_id = [s["stop_id"] for s in sp.fold(env).active_stops if s["reason"] == "capability_expired"][0]
        sp.set_time(env.clock, later.off_ns - 299 * S - 3600 * S)
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        sp.set_time(env.clock, later.off_ns - 299 * S)
        self.assertEqual(self.open()["failures"], ["pa4_pilot_stop"])
        env.gateway.close()
        sp.note(env, "ack", stop_id=stop_id, cause_written=True)
        self.open()
        self.assert_sent(self.place(later))
        self.clean()

    # --- P106-C4 -------------------------------------------------------------------------------------------------

    def test_c4_an_absent_pilot_section_or_context_sets_a_review_stop(self):
        """Both of Astra's restoration witnesses: in an armed session, an open without orders.pilot, or without the
        PilotContext, refuses pilot_not_configured with a refusal note from the journal alone; restoring them does
        not let the same ARM place."""
        for missing in ("section", "context"):
            with self.subTest(missing=missing):
                env = self.build()
                market = self.market()
                config = copy.deepcopy(env.config)
                if missing == "section":
                    config["orders"].pop("pilot")
                gateway = sp.new_gateway(env, config=config, pilot_context=missing == "section")
                report = gateway.open(command="pilot-place", confirm_token=expected_token(config,
                                                                                          env.clock.time_ns()))
                gateway.close()
                self.assertEqual(report["failures"], ["pilot_not_configured"])
                self.assertEqual([n["code"] for n in self.notes("refusal")], ["pilot_not_configured"])
                report = self.open()
                self.assertEqual(report["failures"], ["pa3_session_ended"])
                with self.assertRaises(NotArmed):
                    self.place(market)
                self.assertEqual(env.router.order_requests, [])
                self.clean()
        env = self.build(register=False, arm=False)                  # nothing registered: nothing journaled
        gateway = sp.new_gateway(env, pilot_context=False)
        self.assertEqual(gateway.open(command="pilot-place", confirm_token=sp.token(env))["failures"],
                         ["pilot_not_configured"])
        gateway.close()
        self.assertEqual(sp.pilot_notes(env), [])

    # --- item 5 --------------------------------------------------------------------------------------------------

    def test_item5_the_two_late_submit_latches(self):
        """A submit refused before it is written (pilot_submit_too_late: intent REFUSED, no submit, slot kept) is a
        review stop (Yu's Q11 default); a §5 gate refusal after the submit is FAILED(transport_not_sent), market
        only."""
        env = self.build()
        market = self.market()
        self.open()
        funds = env.gateway._funds

        def late_funds():
            env.clock.advance(60)                  # past the send deadline's 30 s margin before the submit
            return funds()

        with patch.object(env.gateway, "_funds", side_effect=late_funds):
            result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("pilot_submit_too_late", "review"))
        self.assertEqual(([o["state"] for o in result["orders"]], "submit" in self.kinds()), (["REFUSED"], False))
        state = self.clean()
        self.assertEqual((state.lifetime_used, state.place_records[0].submit_seq), (1, None))     # slot kept
        stops, carried_id = self.carried(result, "risk_decision")             # P1-08 §3.2: the refusing decision
        self.assertEqual((self.notes("refusal"), stops),
                         ([], [(carried_id("pilot_submit_too_late"), "pilot_submit_too_late")]))
        env2 = self.build()
        market2 = self.market()
        self.open()
        env2.transport.before_gate = lambda: env2.clock.advance(3)       # the book goes stale before the first byte
        result = self.place(market2)
        self.assert_not_sent_at_first_byte(result, "send_window_expired")
        self.assertEqual((result["pilot"]["latch"], self.notes("refusal")), ("market", []))
        self.assertEqual(sp.fold(env2).active_stops, ())
        self.clean()

    # --- workflow #1 ---------------------------------------------------------------------------------------------

    def notarmed_at_open(self, code, setup, *, raises=False):
        env = self.build(roundtrip=code != "ref_roundtrip_unverified")
        market = self.market()
        with setup(env):
            if raises:
                with self.assertRaises(NotArmed) as caught:
                    env.gateway.open(command="pilot-place", confirm_token=sp.token(env))
                self.assertEqual(caught.exception.rule, code)
            else:
                report = self.open()
                self.assertIn(code, report["failures"])
                with self.assertRaises(NotArmed):
                    self.place(market)
                env.gateway.close()
        self.assertEqual([n["code"] for n in self.notes("refusal")], [code])
        self.assertIn(code, [s["reason"] for s in sp.fold(env).active_stops])
        self.assertEqual(env.router.order_requests, [])
        self.clean()
        return env

    def test_workflow1_a14_can_bet(self):
        self.notarmed_at_open("login_cannot_bet", lambda env: patch.object(env.session, "can_bet",
                                                                             return_value=False))

    def test_workflow1_a15_registry(self):
        self.notarmed_at_open("account_registered_to_other_profile", lambda env: patch.object(
            gateway_module, "register_account", side_effect=NotArmed("account_registered_to_other_profile")))

    def test_workflow1_a16_currency(self):
        def eur(env):
            details = env.read.get_account_details
            return patch.object(env.read, "get_account_details", side_effect=lambda: dict(details(),
                                                                                         currencyCode="EUR"))
        self.notarmed_at_open("account_currency_not_gbp_or_unknown", eur)

    def test_workflow1_a19_ref_roundtrip(self):
        self.notarmed_at_open("ref_roundtrip_unverified", lambda env: contextlib.nullcontext())

    def test_workflow1_anchor_unreadable_and_write_failed(self):
        from betangel_research.betfair.errors import CredentialError
        self.notarmed_at_open("journal_anchor_unreadable", lambda env: patch.object(
            gateway_module, "anchor_read", side_effect=CredentialError("keychain_locked")), raises=True)
        self.notarmed_at_open("journal_anchor_write_failed", lambda env: patch.object(
            gateway_module, "publish_anchor", side_effect=CredentialError("anchor_write_failed")), raises=True)

    def test_workflow1_cleared_condition_still_refused_and_no_note_without_a_session(self):
        env = self.notarmed_at_open("login_cannot_bet", lambda env: patch.object(env.session, "can_bet",
                                                                                   return_value=False))
        self.assertEqual(self.open()["failures"], ["pa3_session_ended"])       # cleared, but the stop holds
        env.gateway.close()
        bare = self.build(arm=False)
        with patch.object(bare.session, "can_bet", return_value=False):
            self.assertIn("login_cannot_bet", self.open()["failures"])
        self.assertEqual(self.notes("refusal"), [])

    # --- workflow #3 ---------------------------------------------------------------------------------------------

    def test_workflow3_a_manual_bet_open_into_a_session_halts(self):
        """RS-19: no manual bet may be open during a session; if the code finds one, it halts. A bar-manual bet
        placed before ARM, still EXECUTABLE or matched and unsettled, makes the pilot open refuse and journal a
        HALT that ends the session; once it is cancelled or settled, RESUME LIVE and a new ARM let the pilot place."""
        for matched in (False, True):
            with self.subTest(matched_unsettled=matched):
                env = self.build(arm=False)
                manual_market = self.market(off_ns=OFF - 60 * S)
                placed = sp.manual_place(env, manual_market, matched=matched)
                self.assertEqual(placed["decision"], "accepted")
                env.session_id = sp.arm_session(env)
                report = self.open()
                self.assertIn("manual_bet_open_during_session", report["failures"])
                halts = [e for e in sp.events(env) if e["kind"] == "halt"]
                self.assertEqual([h["payload"]["reason"] for h in halts], ["manual_bet_open_during_session"])
                state = self.clean()
                self.assertEqual(state.sessions[-1]["ended_reason"], "halt")
                env.gateway.close()
                self.assertEqual([r["params"]["customerStrategyRef"] for r in env.router.order_requests],
                                 ["bar-manual"])
                bet = placed["orders"][0]["bet_id"]
                if matched:
                    manual_market.status = "CLOSED"
                    env.exchange.orders.pop(bet)
                    settler = sp.new_gateway(env)
                    settler.preconfirm("settle-market", market_id=manual_market.market_id)
                    settler.open(command="settle-market", confirm_token=sp.token(env))
                    self.addCleanup(settler.close)
                    settler.settle_market(manual_market.market_id, confirm_token=sp.token(env))
                    settler.close()
                else:
                    canceller = sp.new_gateway(env)
                    canceller.open(command="cancel")
                    self.addCleanup(canceller.close)
                    canceller.cancel(manual_market.market_id, [bet], source="cli_manual")
                    canceller.close()
                self.assertEqual(sp.new_gateway(env).resume(confirm_token=sp.token(env))["status"], "resumed")
                env.clock.advance(86400)
                env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
                market = self.market(off_ns=env.clock.time_ns() + 299 * S)
                self.open()
                self.assert_sent_after(self.place(market), 2 if matched else 3)      # manual, (cancel,) pilot
                self.clean()

    def assert_sent_after(self, result, requests):
        self.assertEqual((result["decision"], result["pilot"]["state"]), ("accepted", "EXECUTABLE"))
        self.assertEqual(len(self.env.router.order_requests), requests)

    # --- workflow #4 ---------------------------------------------------------------------------------------------

    def test_workflow4_a_session_armed_during_a_manual_place_refuses_it(self):
        """A session_start landing after the manual guard (here during the funds read) is in the decision basis's
        read, so the manual order is refused before its intent; nothing is sent and no review stop arises."""
        env = self.build(arm=False)
        market = self.market()
        order = gw.instruction(selection_id=sp.FAV, price_h=300)
        gateway = sp.new_gateway(env)
        self.addCleanup(gateway.close)
        gateway.preconfirm("place", market_id=market.market_id, instructions=[order])
        confirm = sp.token(env)
        gateway.open(command="place", confirm_token=confirm)
        funds = gateway._funds

        def funds_then_arm():
            value = funds()
            sp.note(env, "session_start", journal=sp.other_writer(env), session_id=sp.SESSION,
                    registration_receipt_sha256=env.receipt_sha256, utc_day=sp.DAY, until_ns=sp.UNTIL)
            return value

        with patch.object(gateway, "_funds", side_effect=funds_then_arm), self.assertRaises(OrderRefused) as caught:
            gateway.place(market.market_id, [order], source="cli_manual", confirm_token=confirm)
        self.assertEqual(caught.exception.rule, "manual_order_during_pilot_session")
        self.assertEqual((env.router.order_requests, self.kinds().count("intent")), ([], 0))
        self.assertEqual(sp.fold(env).active_stops, ())

    # --- workflow #2 ---------------------------------------------------------------------------------------------

    def test_workflow2_the_boundary_is_checked_again_before_commit(self):
        paths = support.temp_state_paths(self)
        clock = support.FakeClock()
        journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
        self.addCleanup(journal.close)
        journal.append("note", {"reason": "a"})
        before = clock.time_ns() + S
        folder = journal._committed_folder

        def slow(head):
            clock.advance(5)                       # a slow refold after the in-lock check, before INSERT/COMMIT
            return folder(head)

        with patch.object(journal, "_committed_folder", side_effect=slow), \
                self.assertRaisesRegex(JournalError, "journal_append_too_late"):
            journal.append("note", {"reason": "late-commit"}, before_ns=before)
        self.assertEqual([e["seq"] for e in journal.events()], [1])
        journal.append("note", {"reason": "next"})                  # the connection is clean afterwards
        self.assertEqual(journal.verify()["status"], "ok")

    # --- workflow #5 is in StopsAndLatchTests.test_risk_refusals_and_their_latches ---------------------------------

    # --- #10 (Codex, PR #32) -------------------------------------------------------------------------------------

    def test_item10_an_unregistered_pt_receipt_is_adopted_in_no_part(self):
        from test_betfair_pilot import sha
        unknown = sha("never-registered")
        cases = {"pt4": dict(kind="PT4", matched=1), "l_fields": dict(kind="PT1", verified=True),
                 "refuted": dict(kind="PT1", verified=False), "basis": dict(kind="PT1", basis="net")}
        for label, kw in cases.items():
            with self.subTest(case=label):
                journal_ = Pilot(pt1=False)
                kind = kw.pop("kind")
                seq = journal_.receipt(kind, registration=unknown, **kw)
                state = journal_.fold()
                self.assertEqual((state.pt4_receipt_seq, state.l_verified_seq, state.l_fields_refuted,
                                  state.profit_basis), (None, None, False, None))
                self.assertIn(seq, state.invalid_records)
                self.assertIn(f"pilot-record-invalid-{seq}", [s["stop_id"] for s in state.active_stops])
                session = journal_.arm(0)
                self.assertTrue(journal_.fold().sessions[-1]["pt4"])
                self.assertIsNotNone(session)
        registered = Pilot(pt1=False)
        seq = registered.receipt("PT4", matched=1, basis="net")
        state = registered.fold()
        self.assertEqual((state.pt4_receipt_seq, state.l_verified_seq, state.profit_basis, state.invalid_records),
                         (seq, seq, "net", ()))
        registered.arm(0)
        self.assertFalse(registered.fold().sessions[-1]["pt4"])



class ClockObservationTests(PilotCase):
    """P106-R2-1: every wall-clock read of a pilot-place open is kept in its high-water mark, the gateway's shared
    reads (_state, the risk decision's time) and its collaborators' included. Production margins throughout."""

    UNTIL = OFF + 600 * S                          # 10:10, inside the 6-minute end margin's reach at 09:55:01

    def test_r2_1_one_state_read_past_until_is_kept(self):
        """Astra's witness: the decision is made at about 09:55; the one _state() call inside _place_core sees
        10:10:01; the clock steps back before the later checks. Nothing is sent, and the mark keeps 10:10:01 (the
        risk decision, judged at it, refuses: P106-R3-1)."""
        env = self.build(until_ns=self.UNTIL)
        market = self.market()
        self.open()
        state = env.gateway._state
        seen = []

        def core_state():
            chosen = not seen and "_place_core" in [frame.function for frame in inspect.stack()[:6]]
            wall = env.clock.wall_ns
            if chosen:
                seen.append(True)
                env.clock.wall_ns = self.UNTIL + S
            try:
                return state()
            finally:
                env.clock.wall_ns = wall

        with patch.object(env.gateway, "_state", side_effect=core_state):
            result = self.place(market)
        self.assertEqual((seen, env.router.order_requests), ([True], []))
        # That read is the risk fold's, so the risk decision itself, at the retained time, refuses (P106-R3-1).
        self.assertEqual((result["decision"], result["pilot"]["latch"]), ("refused", "review"))
        self.assertIn(("validation:V10", "book_stale_wall_clock"), risk_failures(result))
        self.assertGreaterEqual(env.gateway._pilot_seen_ns, self.UNTIL + S)
        self.clean()

    def drive(self, step_at):
        """Open and place with every production wall-clock read counted from the open on; the step_at-th one sees
        UNTIL + 1 s, and the clock is back for the next. -> (env, result, reads counted when the order went out)."""
        env = self.build(until_ns=self.UNTIL)
        market = self.market()
        count = {"reads": 0, "active": False, "sent_at": None}
        package = str(sp.REPO / "betangel_research")

        def counted(read):
            def wall_read():
                caller = sys._getframe(1).f_code.co_filename
                if not count["active"] or not caller.startswith(package):
                    return read()
                count["reads"] += 1
                if count["reads"] != step_at:
                    return read()
                wall = env.clock.wall_ns
                env.clock.wall_ns = self.UNTIL + S
                try:
                    return read()
                finally:
                    env.clock.wall_ns = wall
            return wall_read

        accept = env.exchange.place

        def placed(params):
            count["sent_at"] = count["reads"]
            return accept(params)

        env.clock.time_ns, env.clock.utc_now = counted(env.clock.time_ns), counted(env.clock.utc_now)
        env.exchange.place = placed
        count["active"] = True
        try:
            self.open()
            try:
                result = self.place(market)
            except NotArmed as refused:                # a read during the open: the open itself refused
                result = {"refused_at_open": refused.rule}
        finally:
            count["active"] = False
        return env, result, count

    def test_r2_1_every_wall_read_past_until_sends_nothing(self):
        """Each production wall-clock read from the open to the first byte, in turn, sees UNTIL + 1 s at that read
        only: nothing is sent and the fold shows no violation. (Without a step the same open sends.)"""
        env, result, count = self.drive(step_at=0)
        self.assert_sent(result)
        reads = count["sent_at"]
        self.assertGreater(reads, 20)
        for step_at in range(1, reads + 1):
            with self.subTest(read=step_at):
                env, result, _ = self.drive(step_at=step_at)
                self.assertEqual(env.router.order_requests, [])
                self.clean()

    def test_r2_1_a_pilot_gateway_needs_one_observed_clock(self):
        env = self.build()
        with self.assertRaisesRegex(ConfigError, "pilot_gateway_needs_one_observed_clock"):
            OrderGateway(env.config, paths=env.paths, mode="live", journal=env.journal, guard=env.guard, rpc=env.rpc,
                         read=env.read, confirmer=gw.FakeConfirmer(), clock=env.clock, env={},
                         keychain_runner=env.runner, pilot=env.ctx)
        other = pilot_gate.ObservedClock(env.clock)                 # a second wrapper: the parts do not share it
        with self.assertRaisesRegex(ConfigError, "pilot_gateway_needs_one_observed_clock"):
            OrderGateway(env.config, paths=env.paths, mode="live", journal=env.journal, guard=env.guard, rpc=env.rpc,
                         read=env.read, confirmer=gw.FakeConfirmer(), clock=other, env={},
                         keychain_runner=env.runner, pilot=env.ctx)
        with self.assertRaisesRegex(ConfigError, "observed_clock_has_another_gateway"):
            OrderGateway(env.config, paths=env.paths, mode="live", journal=env.journal, guard=env.guard, rpc=env.rpc,
                         read=env.read, confirmer=gw.FakeConfirmer(), clock=env.gateway.clock, env={},
                         keychain_runner=env.runner, pilot=env.ctx)           # it would take env.gateway's reads
        plain = OrderGateway(env.config, paths=env.paths, mode="live", journal=env.journal, guard=env.guard,
                             rpc=env.rpc, read=env.read, confirmer=gw.FakeConfirmer(), clock=env.clock, env={},
                             keychain_runner=env.runner)
        self.assertIs(plain.clock, env.clock)                          # without a pilot nothing is required
        observed = env.gateway.clock
        self.assertEqual((observed.time_ns(), observed.utc_now()), (env.clock.time_ns(), env.clock.utc_now()))


def risk_failures(result):
    """(rule, reason) of every failure of a refused risk decision in a place result."""
    return [(f["rule"], f.get("reason")) for f in result["refusal"]["detail"].get("failures") or []]


def failure_report(code, exec_code=None):
    """A placeOrders handler for SimExchange: Betfair's FAILURE report with one instruction error code (and the
    report's own code)."""
    def handler(params):
        instruction = params["instructions"][0]
        return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": exec_code,
                "marketId": params["marketId"],
                "instructionReports": [{"status": "FAILURE", "errorCode": code, "instruction": instruction}]}
    return handler


def aping_error(code):
    """A placeOrders response: an APINGException with code (Betfair refused the call itself)."""
    return lambda record: support.rpc_error_response(
        -32099, "x", data={"exceptionname": "APINGException", "APINGException": {"errorCode": code}},
        rpc_id=json.loads(record["body"])["id"])


class ReviewRound2Tests(PilotCase):
    """The refute-first workflow on 2e8973f and Nuance's round 2 on 5d7b6d5 (R2-N2, the C2 residual cases, R2-N3, the
    latch residuals and wf3's). Each test fails on 5d7b6d5 unless its docstring says it pins what 5d7b6d5 already
    holds. Production margins throughout unless a test says otherwise."""

    UNTIL = OFF + 600 * S                          # the session ends at 10:10; the open starts at 09:55:01

    def reopen_is_refused(self, market):
        """After this open, the same ARM places nothing: the next open is refused pa3_session_ended."""
        env = self.env
        env.gateway.close()
        self.assertEqual(self.open()["failures"], ["pa3_session_ended"])
        with self.assertRaises(NotArmed):
            self.place(market)
        env.gateway.close()

    # --- item 1: R2-N2 --------------------------------------------------------------------------------------------

    def test_r2_n2_a_kill_file_wins_over_an_unreadable_head(self):
        """A file-only KILL that appears after the post-submit KILL check, while the gate's head read fails: the gate
        still returns kill_engaged_before_send and the session stop is journaled, so removing the file does not let
        the same ARM place again."""
        env = self.build()
        market, later = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        env.transport.before_gate = lambda: engage(env.paths, "test", clock=env.clock)
        with patch.object(env.journal, "head_fresh", side_effect=OSError("disk")):
            result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "kill_engaged_before_send")
        self.assertEqual(result["pilot"]["latch"], "session")
        self.assertEqual([(n["scope"], n["reason"]) for n in self.notes("stop")], [("session", "kill_file")])
        os.remove(env.paths.kill_file)
        self.reopen_is_refused(later)
        self.assertEqual(env.router.order_requests, [])
        self.clean()

    def test_r2_n2_a_kill_file_seen_after_any_not_sent_code_ends_the_session(self):
        """The belt: whatever code a request ended FAILED(transport_not_sent) with, a KILL file there now journals
        the kill_file session stop (here it appears inside the gate, after its KILL check, which then refuses on
        its deadline check)."""
        env = self.build()
        market, later = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()

        def kill_then_deadline(*args, **kw):
            engage(env.paths, "test", clock=env.clock)
            return "pilot_gate_deadline"

        with patch.object(pilot_gate, "first_byte_refusal", side_effect=kill_then_deadline):
            result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "pilot_gate_deadline")
        self.assertEqual((result["pilot"]["latch"], self.notes("refusal")), ("session", []))
        self.assertEqual([(n["scope"], n["reason"]) for n in self.notes("stop")], [("session", "kill_file")])
        os.remove(env.paths.kill_file)
        self.reopen_is_refused(later)
        self.clean()

    # --- item 2: the C2 residual cases ---------------------------------------------------------------------------

    def excursion(self, call):
        """call with the wall clock at UNTIL + 1 s (the monotonic clock untouched), then the clock put back."""
        env = self.env
        wall = env.clock.wall_ns
        env.clock.wall_ns = self.UNTIL + S
        try:
            return call()
        finally:
            env.clock.wall_ns = wall

    def assert_kept(self, result):
        env = self.env
        self.assertEqual(env.router.order_requests, [])
        self.assertGreaterEqual(env.gateway._pilot_seen_ns, self.UNTIL + S)
        self.assertLess(env.clock.time_ns(), self.UNTIL)
        self.clean()

    def test_r2_case_c_a_send_book_stamped_past_the_end_is_kept(self):
        """Pins 5d7b6d5 (its observed clock closes it; 2e8973f sent): the send book's observed_at reads 10:10:01, the
        book then looks stale and is fetched again at the returned clock. The open keeps 10:10:01: the risk decision,
        judged at it since P106-R3-1, refuses (a review stop; at 5d7b6d5 the submit was refused) and nothing is
        sent."""
        env = self.build(until_ns=self.UNTIL)
        market = self.market()
        self.open()
        snapshot, calls = env.read.market_snapshot, []

        def first_past_the_end(*args, **kw):
            calls.append(True)
            if len(calls) == 1:
                return self.excursion(lambda: snapshot(*args, **kw))
            return snapshot(*args, **kw)

        with patch.object(env.read, "market_snapshot", side_effect=first_past_the_end):
            result = self.place(market)
        self.assertEqual((len(calls), env.router.order_requests), (2, []))
        self.assertEqual((result["decision"], result["pilot"]["latch"]), ("refused", "review"))   # P106-R3-1
        self.assertIn(("validation:V10", "book_stale_wall_clock"), risk_failures(result))
        self.assert_kept(result)

    def test_r2_case_d_a_reservation_stamped_past_the_end_ends_the_open(self):
        """The reservation append is stamped 10:10:01 and refused journal_append_too_late; the clock returns. Pins
        5d7b6d5's mark (the stamp is an observed read), and the structural close fails on it: the open, having
        attempted its reservation, places nothing more."""
        env = self.build(until_ns=self.UNTIL)
        market, second = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        append, done = env.journal.append, []

        def late_reservation(kind, payload, **columns):
            inner = payload.get("pilot") if isinstance(payload, dict) else None
            if not done and isinstance(inner, dict) and inner.get("type") == "reservation":
                done.append(True)
                return self.excursion(lambda: append(kind, payload, **columns))
            return append(kind, payload, **columns)

        with patch.object(env.journal, "append", side_effect=late_reservation):
            result = self.place(market)
            self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("pilot_reserve_too_late", None))
            self.assertEqual(self.notes("reservation"), [])
            for again in (market, second):
                with self.assertRaises(OrderRefused) as caught:
                    self.place(again)
                self.assertEqual(caught.exception.rule, "pilot_one_reservation_per_open")
        self.assertEqual(self.reads(sp.CATALOGUE), [])
        self.assert_kept(result)

    def test_r2_case_d_one_reservation_attempt_per_open_whatever_the_clock(self):
        """The structural close without any clock step: a reservation whose head kept moving (another process's
        events, three attempts) ends the open's placements; a refusal before any append (too early) may be retried
        in the same open, which then places."""
        env = self.build()
        market, second = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        original, writer = env.gateway._pilot_append, sp.other_writer(env)

        def foreign_note_first(payload, **kw):
            if payload["pilot"]["type"] == "reservation":
                writer.append("note", {"reason": "another_process"})
            return original(payload, **kw)

        with patch.object(env.gateway, "_pilot_append", side_effect=foreign_note_first):
            result = self.place(market)
        self.assertEqual(result["refusal"]["rule"], "pilot_reserve_head_unstable")
        for again in (market, second):
            with self.assertRaises(OrderRefused) as caught:
                self.place(again)
            self.assertEqual(caught.exception.rule, "pilot_one_reservation_per_open")
        self.assertEqual((self.notes("reservation"), self.reads(), env.router.order_requests), ([], [], []))
        self.clean()
        early = self.build(start=pilot.utc_text_from_ns(OFF - 301 * S))
        market = self.market()
        self.open()
        self.assertEqual(self.place(market)["refusal"]["rule"], "pilot_reserve_too_early")
        early.clock.advance(2)
        self.assert_sent(self.place(market))
        self.clean()

    def test_r2_case_e_the_order_ref_read_past_the_end_is_kept(self):
        """Pins 5d7b6d5: the one clock read that mints the record's order ref sees 10:10:01; the clock returns. The
        open keeps that time, so the decision is not journaled as live and nothing is sent."""
        env = self.build(until_ns=self.UNTIL)
        market = self.market()
        self.open()
        store, mint, saved = pilot_gate.store_evidence, gateway_module.make_customer_order_ref, []

        def stored(*args, **kw):
            out = store(*args, **kw)
            if args[3] == "book":                  # the next wall read is the order ref's
                saved.append(env.clock.wall_ns)
                env.clock.wall_ns = self.UNTIL + S
            return out

        def minted(now_ns, *args, **kw):
            if saved and env.clock.wall_ns == self.UNTIL + S:
                env.clock.wall_ns = saved[0]
            return mint(now_ns, *args, **kw)

        with patch.object(pilot_gate, "store_evidence", side_effect=stored), \
                patch.object(gateway_module, "make_customer_order_ref", side_effect=minted):
            result = self.place(market)
        self.assertEqual(len(saved), 1)
        self.assertNotEqual(result["decision"], "accepted")
        self.assert_kept(result)

    # --- item 3: R2-N3 --------------------------------------------------------------------------------------------

    def test_r2_n3_one_refusal_note_per_open(self):
        """pilot_not_configured and a dynamic failure (can_bet False) in one armed open: one refusal note, for the
        first failure reached, and one review stop."""
        env = self.build()
        gateway = sp.new_gateway(env, pilot_context=False)
        with patch.object(gateway.rpc.session, "can_bet", return_value=False):
            report = gateway.open(command="pilot-place", confirm_token=sp.token(env))
        gateway.close()
        self.assertIn("pilot_not_configured", report["failures"])
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["pilot_not_configured"])
        self.assertEqual([s["reason"] for s in sp.fold(env).active_stops], ["pilot_not_configured"])
        self.clean()

    # --- item 4: latch residuals ---------------------------------------------------------------------------------

    def test_r2_4a_a_fault_inside_the_gate_is_a_review_stop(self):
        """send_gate_failed (the gate raised) is not a §5 refusal: Q11's review default, the stricter choice."""
        env = self.build()
        market, later = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        with patch.object(pilot_gate, "first_byte_refusal", side_effect=RuntimeError("fault")):
            result = self.place(market)
        self.assert_not_sent_at_first_byte(result, "send_gate_failed")
        stops, carried_id = self.carried(result, "report")             # P1-08 §3.2: the not-sent report carries it
        self.assertEqual((result["pilot"]["latch"], self.notes("refusal"), stops),
                         ("review", [], [(carried_id("send_gate_failed"), "send_gate_failed")]))
        self.reopen_is_refused(later)
        self.clean()

    def test_r2_4b_a_connect_failure_after_the_submit_is_a_review_stop(self):
        """Pins 5d7b6d5 (P106-C3 already gives it): a connect failure after the submit, no byte written, is
        FAILED(transport_not_sent) and, not being a §5 refusal, a review stop (Q11; note §9)."""
        env = self.build()
        market, later = self.market(), self.market(off_ns=OFF + 60 * S)
        self.open()
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("connect", "connect_failed")]
        result = self.place(market)
        self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]], [("FAILED", "transport_not_sent")])
        self.assertEqual((env.transport.bytes_written[-1], result["send_exception"]["code"]), (0, "connect_failed"))
        stops, carried_id = self.carried(result, "report")             # P1-08 §3.2: the not-sent report carries it
        self.assertEqual((result["pilot"]["latch"], self.notes("refusal"), stops),
                         ("review", [], [(carried_id("connect_failed"), "connect_failed")]))
        self.assertTrue(sp.fold(env, live_open_id=env.gateway._open_id).place_records[-1].failed_not_sent)
        self.reopen_is_refused(later)
        self.clean()

    def test_r2_4c_a_betfair_refusal_without_a_halt_is_a_review_stop(self):
        """placeOrders refused by Betfair (FAILED, rejected before processing) with no HALT: not named in note §9, so a
        review stop (Q11), journaled once; nothing is resent."""
        for code in ("INVALID_SESSION_INFORMATION", "TOO_MANY_REQUESTS"):
            with self.subTest(code=code):
                env = self.build()
                market, later = self.market(), self.market(off_ns=OFF + 60 * S)
                self.open()
                env.router.next_response[gw.PLACE_SUFFIX] = [aping_error(code)]
                result = self.place(market)
                self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]],
                                 [("FAILED", "rejected_before_processing")])
                self.assertEqual((result["send_exception"]["outcome"], len(env.router.order_requests)),
                                 ("FAILED_REJECTED", 1))
                self.assertFalse(env.journal.fold(now_ns=env.clock.time_ns())["halted"])
                stops, carried_id = self.carried(result, "report")    # P1-08 §3.2: FAILED_REJECTED carries it
                self.assertEqual((result["pilot"]["latch"], self.notes("refusal"), stops),
                                 ("review", [], [(carried_id(code), pilot_gate.refusal_code(code))]))
                self.reopen_is_refused(later)
                self.assertEqual(len(env.router.order_requests), 1)
                self.clean()

    def test_r2_4c_loss_limit_insufficient_funds_and_access_denied_keep_their_halt(self):
        """Note §9's LOSS_LIMIT_EXCEEDED and INSUFFICIENT_FUNDS (a FAILURE report) and a refused call that HALTs
        (ACCESS_DENIED) keep their HALT, which ends the session; no refusal note is added."""
        for code in ("LOSS_LIMIT_EXCEEDED", "INSUFFICIENT_FUNDS", "ACCESS_DENIED"):
            with self.subTest(code=code):
                env = self.build()
                market = self.market()
                self.open()
                if code == "ACCESS_DENIED":
                    env.router.next_response[gw.PLACE_SUFFIX] = [aping_error(code)]
                else:
                    env.exchange.place_handler = failure_report(code)
                result = self.place(market)
                self.assertEqual([o["state"] for o in result["orders"]], ["FAILED"])
                core = env.journal.fold(now_ns=env.clock.time_ns())
                self.assertEqual((core["halted"], core["halt_reasons"]), (True, ["terminal_account"]))
                self.assertEqual(self.notes("refusal"), [])
                self.assertEqual(result["pilot"]["latch"], "none")
                env.gateway.close()
                with self.assertRaises(NotArmed) as caught:      # A7, before the lock: the HALT holds
                    self.open()
                self.assertEqual(caught.exception.rule, "halted")
                self.assertEqual(len(env.router.order_requests), 1)
                self.clean()

    def test_r2_4c_every_refusing_place_report_has_its_latch(self):
        """Nuance's ruling: a place report in which Betfair refuses the instruction is never left at latch None. With
        no HALT, MARKET_NOT_OPEN_FOR_BETTING and MARKET_SUSPENDED (the post-send V9 market_not_open) end that market
        only and every other code is a review stop; where the instruction's code and the report's differ, the
        stronger counts (market only when both are market codes, or when one level carries only its marker for the
        other, ERROR_IN_ORDER or BET_ACTION_ERROR, and the other is a market code). A
        code that journals a HALT keeps it (latch none), a code the error table does not know included
        (unrecognized)."""
        cases = {   # (instruction code, report code) -> (latch, HALT reasons, the refusal note's code)
            ("MARKET_NOT_OPEN_FOR_BETTING", None): ("market", [], None),
            ("ERROR_IN_ORDER", "MARKET_SUSPENDED"): ("market", [], None),     # Betfair's shape for a suspended market
            ("MARKET_NOT_OPEN_FOR_BETTING", "MARKET_SUSPENDED"): ("market", [], None),       # both market codes
            ("RUNNER_REMOVED", "MARKET_SUSPENDED"): ("review", [], "RUNNER_REMOVED"),       # the stronger code counts
            ("MARKET_NOT_OPEN_FOR_BETTING", "ERROR_IN_MATCHER"): ("review", [], "ERROR_IN_MATCHER"),
            ("MARKET_NOT_OPEN_FOR_BETTING", "BET_ACTION_ERROR"): ("market", [], None),      # the report's marker
            ("RUNNER_REMOVED", "BET_ACTION_ERROR"): ("review", [], "RUNNER_REMOVED"),
            (None, "BET_ACTION_ERROR"): ("review", [], "BET_ACTION_ERROR"),                 # the marker alone
            ("RUNNER_REMOVED", None): ("review", [], "RUNNER_REMOVED"),
            ("BET_TAKEN_OR_LAPSED", None): ("review", [], "BET_TAKEN_OR_LAPSED"),
            ("ERROR_IN_MATCHER", None): ("review", [], "ERROR_IN_MATCHER"),
            ("INVALID_ODDS", None): ("none", ["validator_gap"], None),
            ("INVALID_BET_SIZE", None): ("none", ["validator_gap"], None),
            ("LOSS_LIMIT_EXCEEDED", None): ("none", ["terminal_account"], None),
            ("INSUFFICIENT_FUNDS", None): ("none", ["terminal_account"], None),
            ("SOMETHING_NEW_FROM_BETFAIR", None): ("none", ["unrecognized"], None)}
        for (code, exec_code), (latch, halts, noted) in cases.items():
            with self.subTest(code=code, report_code=exec_code):
                env = self.build()
                market_ = self.market()
                self.open()
                env.exchange.place_handler = failure_report(code, exec_code)
                result = self.place(market_)
                env.exchange.place_handler = None
                self.assertEqual(len(env.router.order_requests), 1)
                self.assertNotIn(result["orders"][0]["state"], ("EXECUTABLE", "EXECUTION_COMPLETE"))
                self.assertEqual(env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"], halts)
                self.assertEqual(result["pilot"]["latch"], latch)
                # P1-08 §3.2: no refusal note; one review stop note '<code>-<res>-<report seq>', reason normalised.
                stops, carried_id = self.carried(result, "report")
                self.assertEqual(self.notes("refusal"), [])
                self.assertEqual(stops, [(carried_id(noted), pilot_gate.refusal_code(noted))] if noted else [])
                self.assertEqual([s["reason"] for s in sp.fold(env).active_stops], [reason for _, reason in stops])
                if code == "ERROR_IN_ORDER":
                    # Not a definitive code: the order stays unresolved, and A17 refuses the next open until
                    # reconcile resolves it or Yu accepts it, on top of the market latch.
                    self.assertEqual((result["orders"][0]["state"], result["pilot"]["unknown"]),
                                     ("ABSENT_UNCONFIRMED", True))
                    env.gateway.close()
                    report = self.open()
                    self.assertEqual((report["armed"], report["failures"]), (False, ["unresolved_or_foreign_orders"]))
                    env.gateway.close()
                self.clean()
        self.assertEqual(pilot_gate.latch_for("SOMETHING_NEW_FROM_BETFAIR"), "review")    # with no HALT: review

    def first_byte_refusal(self, code):
        """Drive one placement to the §5 first-byte refusal code (the item-5 verifier's ways in) -> the result."""
        env = self.env
        with contextlib.ExitStack() as stack:
            if code == "send_window_expired":
                env.transport.before_gate = lambda: env.clock.advance(3)
            elif code == "book_stale_before_send":
                head_fresh = env.journal.head_fresh

                def slow_head():
                    env.clock.advance(3)
                    return head_fresh()

                stack.enter_context(patch.object(env.journal, "head_fresh", side_effect=slow_head))
            elif code == "pilot_gate_head_moved":
                writer = sp.other_writer(env)
                env.transport.before_gate = lambda: writer.append("note", {"reason": "another_process"})
            elif code == "pilot_gate_head_unreadable":
                stack.enter_context(patch.object(env.journal, "head_fresh", side_effect=OSError("disk")))
            elif code == "pilot_gate_clock_drift":
                funds = env.gateway._funds

                def stepped():
                    env.clock.step_wall(10)
                    return funds()

                stack.enter_context(patch.object(env.gateway, "_funds", side_effect=stepped))
            else:
                # deadline and window: the margins stood down so that the gate, not G9, is the check reached
                stack.enter_context(patch.object(pilot_gate, "DECISION_MARGIN_NS", 0))
                stack.enter_context(patch.object(pilot_gate, "END_MARGIN_NS", 0))
                funds, head_fresh = env.gateway._funds, env.journal.head_fresh

                def near_the_deadline():
                    sp.set_time(env.clock, OFF - 240 * S - S // 2)
                    return funds()

                stack.enter_context(patch.object(env.gateway, "_funds", side_effect=near_the_deadline))
                if code == "pilot_gate_deadline":
                    env.transport.before_gate = lambda: env.clock.step_wall(1)
                else:
                    def monotonic_past_the_window():
                        env.clock.advance(1)
                        env.clock.step_wall(-1)
                        return head_fresh()

                    stack.enter_context(patch.object(env.journal, "head_fresh",
                                                     side_effect=monotonic_past_the_window))
            return self.place(self.market())

    def test_r2_4e_every_first_byte_code_ends_that_market_only(self):
        """One case per FIRST_BYTE_CODES member, which is exactly this set: FAILED(transport_not_sent), latch market,
        no refusal note, no active stop."""
        codes = ("send_window_expired", "book_stale_before_send", "pilot_gate_head_moved",
                 "pilot_gate_head_unreadable", "pilot_gate_deadline", "pilot_gate_window", "pilot_gate_clock_drift")
        self.assertEqual(pilot_gate.FIRST_BYTE_CODES, frozenset(codes))
        for code in codes:
            with self.subTest(code=code):
                env = self.build()
                self.open()
                result = self.first_byte_refusal(code)
                self.assert_not_sent_at_first_byte(result, code)
                self.assertEqual((result["pilot"]["latch"], self.notes("refusal")), ("market", []))
                self.assertEqual(sp.fold(env).active_stops, ())
                self.clean()

    # --- item 5: wf3's residuals ---------------------------------------------------------------------------------

    def test_r2_5_a_replaced_bet_with_matched_size_is_unsettled(self):
        """open_manual_refs: a REPLACED bet's matched part settles later, so it counts as open until SETTLED or
        VOIDED (latent: live replace is refused); the rest of the table as before."""
        table = {("RECORDED", 0): False, ("EXECUTABLE", 0): True, ("EXECUTION_COMPLETE", 0): False,
                 ("EXECUTION_COMPLETE", 200): True, ("REPLACED", 0): False, ("REPLACED", 150): True,
                 ("SETTLED", 200): False, ("VOIDED", 200): False, ("LAPSED", 0): False, ("FAILED", 0): False,
                 ("UNKNOWN", 0): True, ("ABSENT_UNCONFIRMED", 0): True, ("CANCEL_PENDING", 0): True}
        for (state, matched), expected in table.items():
            with self.subTest(state=state, matched=matched):
                orders = {"bar-1": {"state": state, "size_matched_p": matched}}
                self.assertEqual(pilot_gate.open_manual_refs([], orders), ["bar-1"] if expected else [])
        pilot_intent = [{"kind": "intent", "customer_order_ref": "bar-1", "payload": {"source": "pilot"}}]
        self.assertEqual(pilot_gate.open_manual_refs(pilot_intent, {"bar-1": {"state": "REPLACED",
                                                                             "size_matched_p": 150}}), [])


class ReviewRound3Tests(PilotCase):
    """Astra's code review r3 of 9ef5c47..4536c2e (P106-R3-1, P106-R3-2); each test fails on 4536c2e. Production
    margins throughout."""

    def wall_step_at_state(self, location, seconds=3):
        """The first _state() read made from location (in its call stack) sees the wall clock seconds ahead; the
        clock is back before the next read (Astra's astra-r3-clock-risk-now.py)."""
        env, seen = self.env, []
        state = env.gateway._state

        def stepped():
            chosen = not seen and location in [frame.function for frame in inspect.stack()[:8]]
            wall = env.clock.wall_ns
            if chosen:
                seen.append(True)
                env.clock.wall_ns += seconds * S
            try:
                return state()
            finally:
                env.clock.wall_ns = wall

        return patch.object(env.gateway, "_state", side_effect=stepped), seen

    def test_r3_1_a_pre_risk_step_is_judged_at_the_retained_time(self):
        """The send book is stamped 09:55:01 (max age 2 s); one fold before the risk decision (arming's, or the risk
        fold itself) reads 09:55:04, then the clock returns. The risk decision is judged at 09:55:04: V10
        book_stale_wall_clock, a review stop, nothing sent."""
        for location in ("_live_arming", "_place_core"):
            with self.subTest(location=location):
                env = self.build()
                market = self.market()
                self.open()
                start = env.clock.time_ns()
                stepping, seen = self.wall_step_at_state(location)
                with stepping:
                    result = self.place(market)
                self.assertEqual(seen, [True])
                self.assertEqual((result["decision"], result["pilot"]["latch"]), ("refused", "review"))
                self.assertIn(("validation:V10", "book_stale_wall_clock"), risk_failures(result))
                self.assertEqual((env.router.order_requests, "submit" in self.kinds()), ([], False))
                self.assertEqual(env.gateway._pilot_seen_ns, start + 3 * S)
                self.clean()

    def test_r3_1_a_post_risk_step_bounds_the_book_age_at_the_first_byte(self):
        """After an approved risk decision, the submit's pre-COMMIT clock read, or the fold right after the submit,
        reads 3 s ahead; the clock returns. At the first byte the book is 3 s old at the retained time:
        book_stale_before_send, FAILED(transport_not_sent), that market only."""
        for location in ("submit_precommit", "post_submit_fold"):
            with self.subTest(location=location):
                env = self.build()
                market = self.market()
                self.open()
                start = env.clock.time_ns()
                append, time_ns = env.journal.append, env.clock.time_ns
                plan = {"reads": 0, "armed": False, "stepped": []}

                def read():
                    plan["reads"] += 1
                    if plan["armed"] and not plan["stepped"] and (location == "post_submit_fold"
                                                                   or plan["reads"] == 2):
                        plan["stepped"].append(True)
                        wall = env.clock.wall_ns
                        env.clock.wall_ns += 3 * S
                        try:
                            return time_ns()
                        finally:
                            env.clock.wall_ns = wall
                    return time_ns()

                def submit(kind, payload, **columns):
                    if kind != "submit":
                        return append(kind, payload, **columns)
                    plan["reads"], plan["armed"] = 0, location == "submit_precommit"    # in-lock, then pre-COMMIT
                    try:
                        event = append(kind, payload, **columns)
                    finally:
                        plan["armed"] = location == "post_submit_fold"                  # the next read is the fold's
                    return event

                with patch.object(env.journal, "append", side_effect=submit), \
                        patch.object(env.clock, "time_ns", side_effect=read):
                    result = self.place(market)
                self.assertEqual(plan["stepped"], [True])
                self.assert_not_sent_at_first_byte(result, "book_stale_before_send")
                self.assertEqual((result["pilot"]["latch"], self.notes("refusal")), ("market", []))
                self.assertEqual(env.gateway._pilot_seen_ns, start + 3 * S)
                self.clean()

    def test_r3_2_a_kill_file_adds_its_stop_to_a_review_stop(self):
        """A refused capability, or a connect failure, after the submit while a file-only KILL appears: the KILL's
        session stop and the code's own review stop are both journaled. Removing the KILL file and arming the next
        day does not let it place until the review stop is acknowledged."""
        for cause in ("capability_expired", "connect_failed"):
            with self.subTest(cause=cause):
                env = self.build()
                market, later = self.market(), self.market(off_ns=OFF + 86400 * S)
                self.open()
                if cause == "capability_expired":
                    def stall_then_kill():
                        env.clock.advance(6)
                        engage(env.paths, "test", clock=env.clock)

                    env.gateway.on_send = stall_then_kill
                else:
                    def kill_then_connect_failure(record):
                        engage(env.paths, "test", clock=env.clock)            # after the gate's KILL check
                        return support.transport_failure("connect", "connect_failed")

                    env.router.next_response[gw.PLACE_SUFFIX] = [kill_then_connect_failure]
                result = self.place(market)
                env.gateway.on_send = None
                self.assertEqual((result["send_exception"]["code"], result["pilot"]["latch"]), (cause, "review"))
                self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]],
                                 [("FAILED", "transport_not_sent")])
                self.assertEqual(env.transport.bytes_written[-1:] if cause == "connect_failed" else [0], [0])
                # P1-08 §3.2: the code's own review stop is the carried stop note; the stronger stop first (round 5).
                self.assertEqual(self.notes("refusal"), [])
                self.assertEqual([(n["scope"], n["reason"]) for n in self.notes("stop")],
                                 [("review", cause), ("session", "kill_file")])
                os.remove(env.paths.kill_file)
                env.gateway.close()
                self.assertEqual(self.open()["failures"], ["pa3_session_ended"])     # journals the due review stop
                env.gateway.close()
                stop_id = [s["stop_id"] for s in sp.fold(env).active_stops if s["reason"] == cause][0]
                sp.set_time(env.clock, later.off_ns - 299 * S - 3600 * S)
                env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
                sp.set_time(env.clock, later.off_ns - 299 * S)
                self.assertEqual(self.open()["failures"], ["pa4_pilot_stop"])
                with self.assertRaises(NotArmed):
                    self.place(later)
                env.gateway.close()
                sent_before = len(env.router.order_requests)
                sp.note(env, "ack", stop_id=stop_id, cause_written=True)
                self.open()
                result = self.place(later)
                self.assertEqual((result["decision"], result["pilot"]["state"]), ("accepted", "EXECUTABLE"))
                self.assertEqual(len(env.router.order_requests), sent_before + 1)
                self.clean()


class ReviewRound4Tests(PilotCase):
    """The refute-first workflow on d650056 (round 4); each test fails on d650056."""

    def one_reading(self, caller, step_s):
        """The first wall reading (time_ns) made by the gateway function caller is step_s seconds off; the clock is
        right again for the next. Installed on the raw clock under the shared observed one; -> the record."""
        env, hit = self.env, []
        clock = env.clock
        raw = type(clock).time_ns

        def time_ns():
            value = raw(clock)
            if not hit and sys._getframe(2).f_code.co_name == caller:
                hit.append(value)
                return value + int(step_s * S)
            return value

        clock.time_ns = time_ns
        self.addCleanup(lambda: clock.__dict__.pop("time_ns", None))
        return hit

    def test_r4_1_one_forward_reading_does_not_open_the_minute_window(self):
        """Orders at T - 59.5 s and T - 29.75 s, a cap of 2 a minute, then the third open at T with one reading 1 s
        ahead: at pilot_place's token read (the reservation's R6 check, at its own raw reading, refuses: nothing
        reserved) or at the reservation step's raw reading (the reservation passes; the risk decision, at its own
        raw reading, refuses rate_minute, that market only). Never a third request."""
        for caller, latch, reserved in (("pilot_place", "market", 2), ("_pilot_decide", "market", 3)):
            with self.subTest(transient_at=caller):
                env = self.build()
                start = env.clock.time_ns()
                lead = OFF - start
                markets = [self.market(off_ns=OFF + int(o * S)) for o in (0.0, 29.75, 59.5)]
                gateways = [env.gateway, sp.new_gateway(env), sp.new_gateway(env)]
                for market, gateway in zip(markets[:2], gateways[:2]):
                    if env.clock.time_ns() < market.off_ns - lead:
                        sp.set_time(env.clock, market.off_ns - lead)
                    self.open(gateway)
                    self.assertEqual(self.place(market, gateway=gateway)["decision"], "accepted")
                    gateway.close()
                sp.set_time(env.clock, markets[2].off_ns - lead)
                self.open(gateways[2])
                hit = self.one_reading(caller, 1.0)
                result = self.place(markets[2], gateway=gateways[2])
                self.assertEqual((len(hit), len(env.router.order_requests)), (1, 2))      # no third request
                self.assertEqual((result["decision"], result["refusal"]["rule"], result["pilot"]["latch"]),
                                 ("refused", "rate_minute", latch))
                self.assertEqual(len(self.notes("reservation")), reserved)
                self.clean()

    def test_r4_2_a_backward_reading_at_the_risk_decision_is_a_negative_age(self):
        """The one raw reading the risk decision takes, 1 s or 3 s behind the send book's observed_at (and the
        funds read), then right again: V10 book_age_negative and R11 refuse, as at 4536c2e; a review stop, nothing
        sent."""
        for step_s in (-1, -3):
            with self.subTest(step_s=step_s):
                env = self.build()
                market = self.market()
                self.open()
                state, armed = env.gateway._state, []

                def risk_fold():
                    out = state()
                    if "_place_core" in [frame.function for frame in inspect.stack()[:8]]:
                        armed.append(True)
                    return out

                clock = env.clock
                raw = type(clock).time_ns
                stepped = []

                def time_ns():
                    value = raw(clock)
                    if armed and not stepped:
                        stepped.append(value)
                        return value + step_s * S
                    return value

                clock.time_ns = time_ns
                self.addCleanup(lambda: clock.__dict__.pop("time_ns", None))
                with patch.object(env.gateway, "_state", side_effect=risk_fold):
                    result = self.place(market)
                self.assertEqual(len(stepped), 1)
                self.assertEqual((result["decision"], result["pilot"]["latch"]), ("refused", "review"))
                self.assertEqual(risk_failures(result), [("validation:V10", "book_age_negative"),
                                                         ("funds", "funds_stale_refresh_required")])
                self.assertEqual((env.router.order_requests, "submit" in self.kinds()), ([], False))
                self.clean()

    def test_r4_3_a_failed_resolved_at_open_note_is_retried(self):
        """earlier_unknown_resolved_at_open's note append fails once: the same open's retry writes it (the
        once-per-open flag is set only after the write), so a reopen under the same ARM is refused."""
        from betangel_research.betfair.errors import ApiError
        env = self.build(pt4_done=True)
        market, second, third = self.market(), self.market(off_ns=OFF + 1800 * S), self.market(off_ns=OFF + 3600 * S)
        self.open()
        accept = env.exchange.place

        def placed_but_lost(params):
            env.exchange.place_handler = None
            accept(params)
            raise TransportError("connection_lost", phase="receive", may_have_arrived=True)

        env.exchange.place_handler = placed_but_lost
        env.exchange.read_failures["listCurrentOrders"] = [ApiError("TIMEOUT_ERROR", surface="aping")] * 200
        self.assertTrue(self.place(market)["pilot"]["unknown"])
        env.gateway.close()
        env.exchange.read_failures.clear()
        sp.set_time(env.clock, second.off_ns - 299 * S)
        self.open()
        self.assertTrue(env.gateway._resolved_at_open)
        note, failed = env.gateway._pilot_note, []

        def fail_once(payload, **columns):
            if not failed and payload["pilot"]["type"] == "refusal":
                failed.append(True)
                raise JournalError("synthetic_append_failure")
            return note(payload, **columns)

        with patch.object(env.gateway, "_pilot_note", side_effect=fail_once):
            with self.assertRaises(JournalError):
                self.place(second)
        self.assertEqual(self.notes("refusal"), [])
        refused = self.place(second)                       # the same open, again
        self.assertEqual((refused["refusal"]["rule"], refused["pilot"]["latch"]),
                         ("earlier_unknown_resolved_at_open", "review"))
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["earlier_unknown_resolved_at_open"])
        env.gateway.close()
        sp.set_time(env.clock, third.off_ns - 299 * S)
        self.assertEqual(self.open()["failures"], ["pa3_session_ended"])
        with self.assertRaises(NotArmed):
            self.place(third)
        self.clean()


if __name__ == "__main__":
    unittest.main()
