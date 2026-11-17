"""Contract s1 (research/pilots/s1-fav-t5.json), s1 step 1: the frozen rule file against contract p1 and Yu's figures,
and simulated s1 sessions, offline and synthetic.

The edit table below is the whole difference from p1: it is applied to p1's parsed value, and the result in the one
byte form (json.dumps with sort_keys, indent 2 and ensure_ascii, then a newline, the form that reproduces p1's bytes)
must be s1's bytes exactly. Each number of Yu's is pinned by value, with Yu's words and their date beside it. No test
here reads a page.

The sessions run pilot-register and pilot-run through cli.main in the test_betfair_support_run world (build_run with
s1's bytes and a config that holds s1's caps): a temporary state root, a fake clock, fake Keychain, transport, git,
catalogue and process table, a simulated exchange, and a fake confirmer that types exactly what each prompt asks (it
stands in for Yu; nothing here types a line into a terminal). They show what the code does with s1's numbers in that
world; they do not show Betfair's real behaviour (how fast Betfair settles, its loss limit) or a real terminal.

Isolation (the build brief's rule 17): every CLI call goes through cli_call (or the register, arm and settle helpers
built on it), which first checks with guard() that the state root is the harness's temporary one and not the account's
real state root; no CLI call runs at import time, in a class setup or after cleanup; an AST test over this file finds
no bare cli.main call, no subprocess and no CLI helper of the harness called outside the guarded helpers.
"""

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import paths as paths_module
from betangel_research.betfair import pilot, pilot_gate
from betangel_research.betfair.gateway import OrderGateway

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr

REPO = Path(__file__).resolve().parent.parent
P1_PATH = "research/pilots/p1-fav-t5.json"
S1_PATH = "research/pilots/s1-fav-t5.json"
P1_RAW = (REPO / P1_PATH).read_bytes()
S1_RAW = (REPO / S1_PATH).read_bytes()
P1_SHA256 = "4dbccb5a45f7a4779e0263d4df5048f582f10b8dc3bd22c5f2e93e791155de92"
S1_SHA256 = "f9d5a1e9278461290fca1d9c6fb70f620ecb6f2634e71d4348927d11fe60c39e"
S = sr.S
OFF = sr.OFF
DAY = sr.DAY_S * S
MISSING = object()

# The account's real state root, as the code would find it (paths.py: the password database, never $HOME). Built once,
# as text: nothing here lists, opens or creates anything under it. guard() refuses any CLI call whose root is in it.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_STATE_ROOT = REAL_HOME.joinpath(*paths_module.APP_PARTS)
REAL_APP_DIR = REAL_HOME.joinpath(*paths_module.APP_PARTS[:3])        # ~/Library/Application Support/betangel-research


def one_byte_form(value):
    """The one byte form of a rule file: it reproduces p1's 17,808 bytes from p1's value."""
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


# --- the frozen texts of s1 (build brief C.1; the page quotes these in full) ---------------------------------------

ENDING_CLAUSE = (
    "any breach of RS-19: a bet it does not allow; a session not started by the PILOT ARM line that Yu typed at the "
    "lab Mac's own keyboard, unless docs/betfair-pilot-policy.md, as merged on Yu's word after this file was frozen, "
    "defines a standing consent and the session was started under one that Yu typed at that keyboard in the form that "
    "page gives; or a money number changed without Yu. Until that page defines one, only Yu's typed PILOT ARM line "
    "starts a session, and no chat message is a standing consent")
PLACING_ENDS_AT_FIRST_OF = [
    "the lifetime cap",
    "the final date: placing is allowed only before 00:00 UTC of endings.final_date",
    ENDING_CLAUSE,
    "Yu ending the pilot in writing",
]
LATER_STEPS = {
    "governs": (
        "these lines, and the standing-consent exception in the third entry of placing_ends_at_first_of, allow "
        "nothing by themselves; each applies only where docs/betfair-pilot-policy.md, as merged on Yu's word after "
        "this file was frozen, allows it, and, for a start, only under Yu's own typed line that the page defines; what "
        "that page forbids is forbidden whatever this file says"),
    "lab_run_starts": "not_allowed_unless_the_policy_page_allows",
    "close_out_without_typed_line": "not_allowed_unless_the_policy_page_allows",
}
VERDICT = {
    "none": (
        "s1 runs no test and gives no verdict: no PASS, FAIL, INVALID or INCONCLUSIVE is given, and no NCLV, "
        "price-band or calibration figure is reported for s1"),
    "what_s1_checks": (
        "the path, not an edge: registration, arming, one decision per race, the send, the remainder cancel, the "
        "reads, settlement and the stops, with contract p1's rule at small numbers on the lab Mac; no mispricing is "
        "claimed or known, and each matched bet is expected to lose a little"),
    "kept_blocks": (
        "bsp, final_execution, inference, ledger, measurement_classes, metric, pause, reporting, rule, sample and "
        "validity_gates are byte for byte those of contract p1 (sha256 " + P1_SHA256 + "), so that the loader accepts "
        "this file and the fold treats each record as it does for p1; where one of them describes p1's sample, "
        "analysis or verdict it claims nothing for s1; where one restricts what may be seen, kept or published it "
        "binds s1 in full; no PT4 receipt is written for s1, so every s1 session is a first-check session, no s1 "
        "record joins a sample, and sample.n_eligible (400) cannot be reached by 70 records; in reporting, on the Air "
        "means the lab Mac, as it does for contract p1; pause carries s1's equity and acknowledged levels over to any "
        "later registration or replacement contract on this journal, so if Pilot 1's other bets are taken up later "
        "they continue s1's running total, as docs/betfair-pilot-policy.md says of a new strategy version"),
}
# Every frozen text of s1 by its path in the file (the page's quotes are compared with these, and with the file).
FROZEN_TEXTS = {
    **{f"endings.placing_ends_at_first_of[{i}]": text for i, text in enumerate(PLACING_ENDS_AT_FIRST_OF)},
    **{f"endings.later_steps.{key}": text for key, text in LATER_STEPS.items()},
    **{f"verdict.{key}": text for key, text in VERDICT.items()},
}

# --- the edit table: dotted path -> s1's value; every other path is p1's ------------------------------------------

EDITS = {
    "id": "s1",
    "frozen_on": "2026-11-17",                      # the UTC date of this freeze commit
    "money_set_by_yu_on": "2026-11-17",             # the date of Yu's figures for M1, M3, M6 and M7 (below)
    "customer_strategy_ref": "p1-fav-t5",           # F1, as recommended: kept (p1's value)
    "placement_limits.lifetime_place_records": 70,
    "placement_limits.place_records_per_utc_day": 5,
    "placement_limits.place_records_per_pt4_session_day": 5,   # p1's value
    "caps.max_daily_loss_p": 2000,                  # M1
    "caps.max_orders_per_day": 5,                   # M6
    "endings.final_date": "2027-02-01",             # M3: the first day without bets
    "endings.placing_ends_at_first_of": PLACING_ENDS_AT_FIRST_OF,
    "endings.later_steps": LATER_STEPS,             # S8, as recommended: a new nested block
    "verdict": VERDICT,                             # S7, as recommended: replaced, with no counts key
}
UNCHANGED_IN_TABLE = ("customer_strategy_ref", "placement_limits.place_records_per_pt4_session_day")
CHANGED_CAPS = ["caps.max_daily_loss_p", "caps.max_orders_per_day"]

# s1's caps as the config holds them (pounds for money, seconds for the book age), and the rule file's path.
S1_CONFIG = {"orders": {"caps": {"max_daily_loss": 20.0, "max_orders_per_day": 5},
                        "pilot": {"contract_path": S1_PATH}}}


def get(value, path):
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def put(value, path, new):
    *parents, last = path.split(".")
    for part in parents:
        value = value[part]
    value[last] = copy.deepcopy(new)


def apply_edits(p1_value, edits=EDITS):
    value = copy.deepcopy(p1_value)
    for path, new in edits.items():
        put(value, path, new)
    return value


def differing_paths(a, b, prefix=""):
    """Dotted paths where a and b differ (dicts are walked; any other value is compared whole)."""
    if isinstance(a, dict) and isinstance(b, dict):
        found = set()
        for key in set(a) | set(b):
            path = f"{prefix}.{key}" if prefix else key
            if key not in a or key not in b:
                found.add(path)
            else:
                found |= differing_paths(a[key], b[key], path)
        return found
    return set() if a == b else {prefix}


def p1_value():
    return json.loads(P1_RAW)


def s1_value():
    return json.loads(S1_RAW)


# --- isolation: every CLI call is guarded ---------------------------------------------------------------------------


def guard(env):
    """Before every CLI call: the state root is the harness's temporary one (its home a fresh temporary directory, the
    account-home lookup patched to it) and never the account's real state root."""
    root = Path(env.paths.root).absolute()
    home = Path(env.paths.home).absolute()
    temp = Path(tempfile.gettempdir()).resolve()
    if not (home.resolve().is_relative_to(temp) and root == home.joinpath(*paths_module.APP_PARTS)
            and Path(paths_module.account_home()).absolute() == home and not env.paths.overridden
            and root != REAL_STATE_ROOT and not root.is_relative_to(REAL_APP_DIR)):
        raise AssertionError("refusing a CLI call outside the harness's temporary state root")


def temp_paths(test):
    """gw.unoverridden_paths for a config the test writes itself (no CLI call): its config path is checked to sit
    under the temporary directory, and outside the account's real state root, before anything is written there."""
    paths = gw.unoverridden_paths(test)
    config_path = Path(paths.config_path).absolute()
    if not (config_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
            and not config_path.is_relative_to(REAL_APP_DIR)):
        raise AssertionError("refusing a config path outside the temporary directory")
    return paths


def cli_call(env, *argv, **kw):
    guard(env)
    return sr.run_cli(env, *argv, **kw)


def register(env, **kw):
    guard(env)
    return sr.register(env, **kw)


def arm(env, until="2000", **kw):
    guard(env)
    return sr.arm_run(env, until, **kw)


def settle(env, market_id):
    """A standalone settle-market with its typed line (the fake confirmer types SETTLE MARKET <market>)."""
    return cli_call(env, "settle-market", "--market", market_id, "--confirm-live", sr.token(env))


# --- commit 1, tests 1 to 7: the file ------------------------------------------------------------------------------


class FileTests(unittest.TestCase):
    def test_1_hashes(self):
        """p1 is unchanged (also pinned at tests/test_betfair_pilot.py:41); s1 is pinned by its raw-bytes sha256."""
        self.assertEqual(hashlib.sha256(P1_RAW).hexdigest(), P1_SHA256)
        self.assertEqual(len(P1_RAW), 17808)
        self.assertEqual(hashlib.sha256(S1_RAW).hexdigest(), S1_SHA256)
        self.assertEqual(pilot.contract_sha256(S1_RAW), S1_SHA256)

    def test_2_one_byte_form_ascii_no_blank_no_pound(self):
        self.assertEqual(one_byte_form(p1_value()), P1_RAW)
        self.assertEqual(one_byte_form(s1_value()), S1_RAW)
        text = S1_RAW.decode("ascii")                                   # raises on any non-ASCII byte
        self.assertNotIn("<" * 2, text)                                 # no blank of the build brief left
        self.assertNotIn(">" * 2, text)
        self.assertNotIn("\\u00a3", text.lower())                       # no escaped pound sign either
        self.assertNotIn(b"\xc2\xa3", S1_RAW)

    def test_3_loads(self):
        contract = pilot.contract_from_bytes(S1_RAW)
        self.assertEqual((contract.sha256, contract.id, contract.customer_strategy_ref),
                         (S1_SHA256, "s1", "p1-fav-t5"))
        self.assertEqual(set(s1_value()), pilot.CONTRACT_KEYS)
        self.assertEqual(len(s1_value()), 27)

    def test_4_p1_with_the_edit_table_is_s1(self):
        p1, s1 = p1_value(), s1_value()
        self.assertEqual(apply_edits(p1), s1)
        self.assertEqual(one_byte_form(apply_edits(p1)), S1_RAW)
        changed = {path for path in EDITS if get(p1, path) != EDITS[path]}
        self.assertEqual(changed, set(EDITS) - set(UNCHANGED_IN_TABLE))
        differ = differing_paths(p1, s1)

        def under(path, table_path):
            return path == table_path or path.startswith(table_path + ".")

        self.assertEqual({p for p in differ if not any(under(p, t) for t in changed)}, set())   # only table paths
        self.assertEqual({t for t in changed if not any(under(p, t) for p in differ)}, set())   # each one differs
        for key in sorted(set(p1) - {path.split(".")[0] for path in changed}):
            with self.subTest(block=key):
                self.assertEqual(s1[key], p1[key])
                self.assertEqual(one_byte_form(s1[key]), one_byte_form(p1[key]))
        self.assertEqual(s1["rule"], p1["rule"])
        self.assertEqual(set(s1["caps"]), set(p1["caps"]))              # p1's thirteen caps and config_names
        self.assertEqual(len(set(s1["caps"]) - {"config_names"}), 13)
        for key in sorted(set(p1["caps"]) - {"max_daily_loss_p", "max_orders_per_day"}):
            with self.subTest(cap=key):
                self.assertEqual(s1["caps"][key], p1["caps"][key])     # M7
        self.assertEqual(s1["endings"]["protocol_violation"], p1["endings"]["protocol_violation"])
        self.assertEqual(set(s1["endings"]), {"final_date", "later_steps", "placing_ends_at_first_of",
                                              "protocol_violation"})
        self.assertEqual(set(s1["verdict"]), {"none", "what_s1_checks", "kept_blocks"})
        for path, text in FROZEN_TEXTS.items():
            with self.subTest(frozen=path):
                name, _, index = path.partition("[")
                found = get(s1, name)
                self.assertEqual(found[int(index[:-1])] if index else found, text)

    def test_5_yus_numbers(self):
        """Each number of Yu's, by value.

        Question 2 as put on 2026-11-12: "**Its numbers:** £2 a bet, 5 bets a day, a £20 day stop, 70 bets in all
        (worst case £140), ending 31 January." Yu, 2026-11-14, about 12:21 UTC: "I'd do £10. 1-3 anything for me to
        decide? lgtm." (lab-Claude's reading: yes to questions 1 to 3 as put.) Item 2 as shown on 2026-11-16, in part:
        "Stake: £2 on every program order, back or lay." "Orders a day: 5 while you attend, lays included." "Open at
        once: £20, unchanged." Yu, 2026-11-16, 18:43:51 UTC, in part: "2. yes lgtm." Yu, 2026-11-17, about 09:11 UTC,
        to the six money questions a to f (lab-Claude's mapping: a = M1, b = M3, c = M5, d = M6, e = M7, f = M8): "a
        £20, b 31 Jan 2027, c not set yet, set it to £20 a day, d 5, e yes, f done later". M2 (5 a day for s1's whole
        life) rests on lab-Claude's reading of Yu, 2026-11-17, about 09:09 UTC: "including s1 recommendations. Lets
        review the money figures." as a yes to every recommendation of the s1 brief."""
        s1 = s1_value()
        contract = pilot.contract_from_bytes(S1_RAW)
        # Stake £2 in its three places.
        self.assertEqual(s1["rule"]["order"]["size_p"], 200)
        self.assertEqual(s1["caps"]["max_stake_per_order_p"], 200)
        self.assertEqual(s1["pause"]["new_place_counted_at_p"], -200)
        self.assertEqual(contract.size_p, 200)
        # 5 a day in both day limits (M2: for s1's whole life, every step).
        self.assertEqual(s1["placement_limits"]["place_records_per_utc_day"], 5)
        self.assertEqual(s1["placement_limits"]["place_records_per_pt4_session_day"], 5)
        self.assertEqual((contract.per_utc_day, contract.per_pt4_session_day), (5, 5))
        # 70 in all.
        self.assertEqual(s1["placement_limits"]["lifetime_place_records"], 70)
        self.assertEqual(contract.lifetime_place_records, 70)
        # Open at once £20, unchanged.
        self.assertEqual(s1["caps"]["max_total_exposure_p"], 2000)
        # M1: the day stop, "a £20".
        self.assertEqual(s1["caps"]["max_daily_loss_p"], 2000)
        # M6: the gateway's own orders a day, "d 5".
        self.assertEqual(s1["caps"]["max_orders_per_day"], 5)
        # M3: "b 31 Jan 2027" is the last betting day; the code reads final_date as the first day without bets.
        self.assertEqual(s1["endings"]["final_date"], "2027-02-01")
        self.assertEqual(contract.final_date_ns, pilot.parse_utc_ns("2027-01-31T00:00:00Z") + pilot.DAY_NS)
        self.assertEqual(s1["money_set_by_yu_on"], "2026-11-17")

    def test_6_arithmetic(self):
        s1 = s1_value()
        total, stake = s1["placement_limits"]["lifetime_place_records"], s1["rule"]["order"]["size_p"]
        self.assertEqual(total * stake, 14000)
        self.assertLess(total * stake, s1["pause"]["step_p"])          # the first pause level cannot be reached
        self.assertEqual(s1["pause"]["step_p"], 15000)
        self.assertGreater(s1["sample"]["n_eligible"], total)          # the sample cannot complete
        self.assertEqual(s1["sample"]["n_eligible"], 400)

    def config(self, overrides):
        paths = temp_paths(self)
        return gw.live_config(paths, support.deep_merge(sp.pilot_profile(S1_SHA256), overrides))

    def test_7_config_holds_contract(self):
        contract = pilot.contract_from_bytes(S1_RAW)
        caps = pilot.decimal_loads(S1_RAW)["caps"]
        config = self.config(S1_CONFIG)
        self.assertEqual(pilot_gate.config_holds_contract(config, contract, caps), [])
        for name, key in (("max_daily_loss", "caps.max_daily_loss_p"),
                          ("max_orders_per_day", "caps.max_orders_per_day"),
                          ("max_total_exposure", "caps.max_total_exposure_p"),
                          ("max_book_age_s", "caps.max_book_age_ms")):
            with self.subTest(missing=name):
                missing = copy.deepcopy(config)
                del missing["orders"]["caps"][name]
                self.assertEqual(pilot_gate.config_holds_contract(missing, contract, caps), [key])
        missing = copy.deepcopy(config)
        del missing["orders"]["rules"]["external_exposure_tolerance"]
        self.assertEqual(pilot_gate.config_holds_contract(missing, contract, caps),
                         ["caps.external_exposure_tolerance_p"])
        # Pilot 1's test profile (p1's caps) against s1: exactly the two caps that differ, by name.
        p1_caps = self.config({"orders": {"pilot": {"contract_path": S1_PATH}}})
        self.assertEqual(pilot_gate.config_holds_contract(p1_caps, contract, caps), CHANGED_CAPS)
        self.assertEqual(differing_paths(p1_value()["caps"], s1_value()["caps"]),
                         {key.split(".", 1)[1] for key in CHANGED_CAPS})


# --- commit 1, test 8: simulated s1 sessions -----------------------------------------------------------------------


def places(env):
    return [r for r in sr.order_requests(env) if r["method"].endswith(gw.PLACE_SUFFIX)]


def pilot_places(env):
    return [r for r in places(env) if r["params"].get("customerStrategyRef") == "p1-fav-t5"]


class S1Case(unittest.TestCase):
    def build(self, *, contract_raw=S1_RAW, overrides=None, **kw):
        if contract_raw is S1_RAW:
            overrides = support.deep_merge(S1_CONFIG, overrides or {})
        self.env = sr.build_run(self, contract_raw=contract_raw, overrides=overrides, **kw)
        return self.env

    def registered(self, **kw):
        env = self.build(**kw)
        code, out, err = register(env)
        self.assertEqual(code, 0, err)
        return env

    def refused(self, result, code):
        exit_code, out, err = result
        self.assertEqual(exit_code, 2, err + out)
        self.assertIn(f"Betfair order refused: {code}", err)
        return err

    def released(self, env):
        view = sr.admission_view(env)
        self.assertIsNone(view["admission"])
        self.assertFalse(view["inhibit"])
        self.assertFalse(sr.armed_file(env).exists())

    def withheld(self, env):
        view = sr.admission_view(env)
        self.assertIsNotNone(view["admission"])
        self.assertTrue(view["inhibit"])

    def markets(self, env, day, count, *, gap_s=900):
        return [sr.add_market(env, off_ns=OFF + day * DAY + i * gap_s * S) for i in range(count)]

    def session(self, env, day, count, *, gap_s=900):
        """Day `day`'s session (09:50Z, `count` races from 10:00Z): -> (exit code, summary, err)."""
        if day:
            sr.set_day(env, day)
        self.markets(env, day, count, gap_s=gap_s)
        code, out, err = arm(env)
        return code, json.loads(out) if out.strip() else None, err

    def session_stops(self, env):
        return [(s["scope"], s["reason"]) for s in sr.stops(env) if s["scope"] == "session"]

    def kinds(self, env, since=0):
        return [(e["kind"], (e["payload"].get("pilot") or {}).get("type")) for e in sr.events(env)[since:]]

    def refusals(self):
        """A spy on the gateway's latch step: the sorted rules of every refused pilot placement, each time. (R6 and R8
        to R10 refuse before the reservation, so such a refusal is not journaled as a risk decision; its session
        stops are.) It records and passes through; it changes nothing."""
        found = []
        original = OrderGateway._pilot_latch_failures

        def latch(gateway, failures, reservation_seq, **kw):
            found.append(sorted({f["rule"] for f in failures}))
            return original(gateway, failures, reservation_seq, **kw)

        patcher = patch.object(OrderGateway, "_pilot_latch_failures", latch)
        patcher.start()
        self.addCleanup(patcher.stop)
        return found


class SessionTests(S1Case):
    def test_8a_five_records_a_plain_end_at_the_day_limit_and_the_release(self):
        env = self.registered()
        code, summary, err = self.session(env, 0, 6)
        self.assertEqual(code, 0, err)
        self.assertEqual((summary["end_code"], summary["place_records"], summary["sends"], summary["reservations"]),
                         ("day_limit", 5, 5, 5))                       # the sixth race is not reserved
        self.assertEqual(len(pilot_places(env)), 5)
        self.assertEqual(len(sr.notes(env, "reservation")), 5)
        state = sr.fold(env)
        self.assertEqual(len(state.place_records), 5)
        self.assertEqual(state.sessions[-1]["ended_reason"], "session_end")
        self.assertEqual(self.session_stops(env), [("session", "session_end")])
        self.released(env)
        self.assertIn("recording released", err)

    def test_8b_one_session_a_day_and_the_next_day_is_again_a_first_check_session(self):
        env = self.registered()
        self.assertEqual(self.session(env, 0, 1)[0], 0)
        self.refused(arm(env), "pilot_arm_already_today")
        code, summary, err = self.session(env, 1, 6)
        self.assertEqual(code, 0, err)
        self.assertEqual((summary["end_code"], summary["place_records"], summary["pt4_records"]), ("day_limit", 5, 5))
        self.assertIn("at most 5 PLACE records today", env.confirmer.prompts[-1]["banner"])
        state = sr.fold(env)
        self.assertIsNone(state.pt4_receipt_seq)
        self.assertEqual(len(state.sessions), 2)
        self.assertTrue(all(r.pt4 for r in state.place_records))

    def test_8j_the_arm_banner_and_what_it_rests_on(self):
        env = self.registered()
        self.assertEqual(self.session(env, 0, 1)[0], 0)
        prompt = env.confirmer.prompts[-1]
        self.assertEqual(prompt["expected"], f"PILOT ARM {S1_SHA256[:12]} 20270104 UNTIL 2000Z")
        self.assertIn("BACK £2.00, at most 5 PLACE records today", prompt["banner"])
        self.assertIn(f"contract {S1_SHA256[:12]}", prompt["banner"])
        self.assertNotIn(P1_SHA256[:12], prompt["banner"])
        # The banner is a literal (pilot_run.arm_banner): it would read £2.00 and 5 whatever the file said, so the
        # file's own values are asserted, and that the session was a first-check one.
        self.assertEqual((env.contract.size_p, env.contract.per_utc_day, env.contract.per_pt4_session_day),
                         (200, 5, 5))
        self.assertIsNone(sr.fold(env).pt4_receipt_seq)
        self.assertTrue(all(r.pt4 for r in sr.fold(env).place_records))

    def test_8h_one_rule_file_per_journal(self):
        """A journal that holds p1's notes refuses s1's registration before any typed line, and the reverse."""
        for first, second in ((P1_RAW, S1_RAW), (S1_RAW, P1_RAW)):
            with self.subTest(first=hashlib.sha256(first).hexdigest()[:12]):
                env = self.registered(contract_raw=first)
                self.assertEqual([n["contract_sha256"] for n in sr.notes(env, "registration")],
                                 [hashlib.sha256(first).hexdigest()])
                contract = pilot.contract_from_bytes(second)
                profile = sp.pilot_profile(contract.sha256)
                if second is S1_RAW:
                    profile = support.deep_merge(profile, S1_CONFIG)
                else:
                    profile = support.deep_merge(profile, {"orders": {"pilot": {"contract_path": P1_PATH}}})
                env.config = gw.live_config(env.paths, profile)
                env.state.contract_raw = second
                head, prompts = sr.events(env)[-1]["seq"], len(env.confirmer.prompts)
                self.refused(register(env), "pilot_journal_other_contract")
                self.assertEqual(sr.events(env)[-1]["seq"], head)
                self.assertEqual(len(env.confirmer.prompts), prompts)          # before any typed line
                self.assertEqual(sr.credential_reads(env), [])

    def test_8c_the_seventieth_record_is_sent_mid_session_and_there_is_no_seventy_first(self):
        """The 70th record lands mid-session, with day slots left, as it usually will in real sessions (70 = 14 x 5,
        so with five records every day the day limit would end that session first and hide the lifetime cap). Day 0
        has three races and days 1 to 13 five each: 68 records, every bet unmatched (the harness's default: its
        remainder is cancelled straight after the place report), so no open bet is left to block the next day. Day
        14 has five races: its first two records are the 69th and the 70th, both sent; the third race is neither
        reserved nor sent, although that day's limit of 5 is not reached. The next day ARM is refused. Every record
        is made by the real pilot-run in its own session; no journal row is written by hand."""
        env = self.registered()
        counts = [3] + [5] * 13
        for day, count in enumerate(counts):
            code, summary, err = self.session(env, day, count, gap_s=360)
            self.assertEqual(code, 0, err)
            self.assertEqual(summary["place_records"], count, (day, summary))
            self.assertEqual(sr.fold(env).lifetime_used, sum(counts[:day + 1]))
        self.assertEqual(sr.fold(env).lifetime_used, 68)
        sr.set_day(env, 14)
        last_day = self.markets(env, 14, 5, gap_s=360)
        code, out, err = arm(env)
        summary = json.loads(out)
        self.assertEqual(code, 0, err)
        self.assertEqual((summary["place_records"], summary["sends"], summary["reservations"]), (2, 2, 2), summary)
        self.assertEqual(summary["end_code"], "pa4_placing_ended")      # the ending, not the day limit, ends it
        state = sr.fold(env)
        self.assertEqual(state.sessions[-1]["ended_reason"], "session_end")
        self.released(env)
        self.assertEqual((state.lifetime_used, len(state.place_records)), (70, 70))
        self.assertEqual(sum(1 for r in state.place_records if r.submit_seq is not None), 70)   # the 70th was sent
        self.assertEqual(len(pilot_places(env)), 70)
        self.assertEqual(pilot.placing_ending(state, now_ns=env.clock.time_ns())["reason"], "lifetime_cap")
        for market in last_day[2:]:                                     # the third race on: no reservation, no send
            with self.subTest(market=market.market_id):
                self.assertNotIn(market.market_id, state.reservations)
                self.assertEqual([r for r in places(env) if r["params"].get("marketId") == market.market_id], [])
        sr.set_day(env, 15)
        self.markets(env, 15, 1)
        head = sr.events(env)[-1]["seq"]
        self.refused(arm(env), "pilot_arm_placing_ended")
        self.assertEqual(sr.events(env)[-1]["seq"], head)
        self.assertEqual(len(pilot_places(env)), 70)

    def test_8d_the_final_date_is_the_first_day_without_bets(self):
        """ARM on 2027-01-31, s1's last betting day, places; ARM on 2027-02-01, the file's final_date, is refused."""
        env = self.registered()
        days = (pilot.parse_utc_ns("2027-01-31T00:00:00Z") - pilot.parse_utc_ns(sp.DAY + "T00:00:00Z")) // DAY
        code, summary, err = self.session(env, days, 1)
        self.assertEqual(code, 0, err)
        self.assertEqual(pilot.utc_day(env.clock.time_ns()), "2027-01-31")
        self.assertEqual((summary["place_records"], summary["sends"]), (1, 1))
        sr.set_day(env, days + 1)
        self.assertEqual(pilot.utc_day(env.clock.time_ns()), "2027-02-01")
        self.markets(env, days + 1, 1)
        prompts = len(env.confirmer.prompts)
        self.refused(arm(env), "pilot_arm_placing_ended")
        self.assertEqual(len(env.confirmer.prompts), prompts)
        self.assertEqual(pilot.placing_ending(sr.fold(env), now_ns=env.clock.time_ns())["reason"], "final_date")

    def matched_history(self, env, counts):
        """Sessions on days 0, 1, ... with counts[day] races each, every pilot bet matched in full and left unsettled
        (the races never show CLOSED here, so no settle is offered)."""
        sr.fills(env, complete=True)
        for day, count in enumerate(counts):
            code, summary, err = self.session(env, day, count)
            self.assertEqual(code, 0, err)
            self.assertEqual((summary["place_records"], summary["fills"]), (count, count))
        return len(counts)

    def test_8e_an_eleventh_open_bet_stops_the_session(self):
        """M1 is £20 and open at once is £20: with ten £2 bets matched and unsettled (£20 open), the next placement
        is refused for total_exposure and daily_loss together; a daily_loss session stop is journaled before any
        reservation (no slot is used); the end is not plain and the release is withheld. The same history under
        contract p1 and Pilot 1's caps (£50 day stop) skips those races only and ends plainly."""
        env = self.registered()
        day = self.matched_history(env, [5, 5])
        before = len(sr.events(env))
        refused = self.refusals()
        code, summary, err = self.session(env, day, 2)
        self.assertEqual(code, 5, err)
        self.assertEqual(refused, [["daily_loss", "total_exposure"]])          # the first race; nothing after it
        self.assertEqual(len(pilot_places(env)), 10)
        self.assertEqual(sr.fold(env).lifetime_used, 10)                       # no slot used
        self.assertEqual((summary["reservations"], summary["place_records"]), (0, 0))
        self.assertEqual([k for k in self.kinds(env, before) if k[1] == "reservation"], [])
        self.assertEqual(self.session_stops(env)[-1], ("session", "daily_loss"))
        self.assertEqual(sr.fold(env).sessions[-1]["ended_reason"], "daily_loss")
        self.withheld(env)

        p1 = self.registered(contract_raw=P1_RAW)
        day = self.matched_history(p1, [5, 5])
        refused.clear()
        code, summary, err = self.session(p1, day, 2)
        self.assertEqual(code, 0, err)
        self.assertEqual(refused, [["total_exposure"]] * 2)                    # each race skipped, no session stop
        self.assertEqual((summary["end_code"], summary["reservations"], summary["place_records"]),
                         ("no_plannable_market", 0, 0))
        self.assertEqual(self.session_stops(p1)[-1], ("session", "session_end"))
        self.assertNotIn(("session", "daily_loss"), self.session_stops(p1))
        self.released(p1)

    def betfair_settles(self, env, markets):
        """Betfair settles these races: their books show CLOSED and our bets in them leave listCurrentOrders."""
        for market in markets:
            market.status = "CLOSED"
            for bet_id in [b for b, o in env.exchange.orders.items() if o["marketId"] == market.market_id]:
                env.exchange.orders.pop(bet_id)

    def test_8f_six_bets_settled_on_the_session_day_stop_its_fifth_bet(self):
        """M1 is £20: a typed settle books each bet at its worst case (£2) on the day it is typed. Six earlier bets,
        matched, then settled on the session day by settle-market and its typed SETTLE MARKET line (the fake
        confirmer stands in for Yu), book £12; the day's first four bets, matched, bring the day to £20; the fifth is
        refused with daily_loss, a session stop."""
        env = self.registered()
        day = self.matched_history(env, [5, 1])
        earlier = list(env.router.markets.values())
        self.assertEqual(len(earlier), 6)
        sr.set_day(env, day)
        self.betfair_settles(env, earlier)
        for market in earlier:
            code, out, err = settle(env, market.market_id)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["status"], "settled")
        settled = [e for e in sr.events(env) if e["kind"] == "settle"]
        self.assertEqual(len(settled), 6)
        refused = self.refusals()
        code, summary, err = self.session(env, day, 5)
        self.assertEqual(code, 5, err)
        self.assertEqual(refused, [["daily_loss"]])
        self.assertEqual((summary["place_records"], summary["fills"]), (4, 4))
        self.assertEqual(len(pilot_places(env)), 10)
        self.assertEqual(self.session_stops(env)[-1], ("session", "daily_loss"))
        self.withheld(env)

    def test_8g_a_manual_order_on_a_session_day_uses_one_of_its_five(self):
        """M6 is 5: the gateway's orders-a-day count takes every place instruction of the UTC day. One manual £2
        order, placed and cancelled before ARM (through the harness's gateway-level manual_place and a cancel, as
        tests/test_betfair_pilot_gateway.py does; there is no CLI-level manual place in the run harness), leaves four
        for the session: the fifth pilot bet meets rate_day, a session stop, and the release is withheld."""
        env = self.registered(start=pilot.utc_text_from_ns(OFF - 1800 * S))
        manual_market = sr.add_market(env, off_ns=OFF - 1500 * S)
        guard(env)
        placed = sp.manual_place(env, manual_market)
        self.assertEqual(placed["decision"], "accepted")
        canceller = sp.new_gateway(env)
        self.addCleanup(canceller.close)
        canceller.open(command="cancel")
        canceller.cancel(manual_market.market_id, [placed["orders"][0]["bet_id"]], source="cli_manual")
        canceller.close()
        self.assertEqual(len(places(env)), 1)
        sr.set_day(env, 0)
        self.assertEqual(env.clock.time_ns(), OFF - 600 * S)
        refused = self.refusals()
        code, summary, err = self.session(env, 0, 5)
        self.assertEqual(code, 5, err)
        self.assertEqual(refused, [["rate_day"]])
        self.assertEqual(summary["place_records"], 4)
        self.assertEqual(len(pilot_places(env)), 4)
        self.assertEqual(self.session_stops(env)[-1], ("session", "rate_day"))
        self.withheld(env)

    def test_8k_a_lost_bet_betfair_has_settled_counts_twice_until_its_settle_is_typed(self):
        """M1 is £20. Read in the code, not run against Betfair: R10 takes the worse of the journal's realised loss
        and Betfair's cleared figure, and a matched bet not yet settled by Yu's typed line stays in the exposure sum.
        Two earlier bets matched and unsettled; today's bets matched; Betfair settles each of today's races a minute
        after its off, all lost, and lists those bets as cleared, not current; nobody types a settle (the races'
        books still show OPEN in this world, so no settle is offered). At the fifth placement: £8 cleared + £4 + £8
        open + the new £2 = £22, above £20: refused with daily_loss. Only a real session shows how fast Betfair
        settles."""
        env = self.registered()
        sr.fills(env, complete=True)
        code, summary, err = self.session(env, 0, 2)
        self.assertEqual((code, summary["fills"]), (0, 2), err)
        sr.set_day(env, 1)
        today = self.markets(env, 1, 5)
        exchange, original = env.exchange, env.exchange.cleared_orders
        offs = {m.market_id: m.off_ns for m in today}

        def betfair_settles_lost():
            for bet_id, order in list(exchange.orders.items()):
                off = offs.get(order["marketId"])
                if off is None or env.clock.time_ns() < off + 60 * S or order["sizeMatched"] <= 0:
                    continue
                exchange.orders.pop(bet_id)
                exchange.cleared["SETTLED"].append({
                    "betId": bet_id, "marketId": order["marketId"], "selectionId": order["selectionId"],
                    "side": "BACK", "betStatus": "SETTLED", "betOutcome": "LOSE", "sizeSettled": order["sizeMatched"],
                    "priceMatched": order["averagePriceMatched"], "priceReduced": False,
                    "profit": -order["sizeMatched"],
                    "customerOrderRef": order["customerOrderRef"], "customerStrategyRef": order["customerStrategyRef"],
                    "placedDate": order["placedDate"], "settledDate": pilot.utc_text_from_ns(off + 60 * S)})

        def cleared_orders(status, **kw):
            betfair_settles_lost()
            return original(status, **kw)

        current = exchange.current

        def current_orders(**kw):
            betfair_settles_lost()
            return current(**kw)

        exchange.cleared_orders, exchange.current = cleared_orders, current_orders
        refused = self.refusals()
        code, out, err = arm(env)
        summary = json.loads(out)
        self.assertEqual(code, 5, err)
        self.assertEqual(refused, [["daily_loss"]])
        self.assertEqual((summary["place_records"], summary["fills"]), (4, 4))
        self.assertEqual(len(exchange.cleared["SETTLED"]), 4)
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])     # nobody typed a settle
        self.assertEqual(self.session_stops(env)[-1], ("session", "daily_loss"))
        floors = [e["payload"]["exchange_realized_today_pence"] for e in sr.events(env)
                  if e["kind"] == "startup_reconcile"]
        self.assertEqual(floors[-1], -800)                                     # Betfair's figure: four £2 losses
        self.withheld(env)

    def test_8i_pilot_1_caps_in_the_config_are_refused_by_name(self):
        env = self.build(overrides={"orders": {"caps": {"max_daily_loss": 50.0, "max_orders_per_day": 25}}})
        err = self.refused(register(env), "pilot_config_differs:" + ",".join(CHANGED_CAPS))
        line = [x for x in err.splitlines() if x.startswith("Betfair order refused:")]
        self.assertEqual(line, ["Betfair order refused: pilot_config_differs:" + ",".join(CHANGED_CAPS)])
        for value in ("50", "25", "20", "5000", "2000"):                    # names only, never a value
            self.assertNotIn(value, err)
        self.assertEqual(env.confirmer.prompts, [])                         # before any typed line
        self.assertEqual(sr.notes(env, "registration"), [])
        self.assertEqual(sr.credential_reads(env), [])


# --- rule 17: no CLI call outside the guarded helpers ---------------------------------------------------------------


class IsolationTests(unittest.TestCase):
    HARNESS_CLI = frozenset({"run_cli", "register", "arm_run", "end_session", "ack", "gap_resolved", "capture"})
    GUARDED = frozenset({"cli_call", "register", "arm"})

    def test_every_cli_call_goes_through_the_guard(self):
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names if a.name.split(".")[0] == "subprocess" or a.name.endswith(".cli")]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
                bad += [n for n in names if n.split(".")[0] == "subprocess" or n == "cli" or n.endswith(".cli")]
            elif isinstance(node, ast.Name) and node.id in ("subprocess", "cli"):
                bad.append(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in ("main", "Popen", "check_output") \
                    and isinstance(node.value, ast.Name) and node.value.id in ("cli", "subprocess"):
                bad.append(f"{node.value.id}.{node.attr}")
        self.assertEqual(bad, [])
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for func in functions:
            harness = [c for c in ast.walk(func) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and isinstance(c.func.value, ast.Name) and c.func.value.id == "sr"
                       and c.func.attr in self.HARNESS_CLI]
            if not harness:
                continue
            with self.subTest(function=func.name):
                self.assertIn(func.name, self.GUARDED)
                first = func.body[0]
                self.assertTrue(isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                                and isinstance(first.value.func, ast.Name) and first.value.func.id == "guard")
        self.assertEqual({f.name for f in functions if f.name in self.GUARDED and f.col_offset == 0}, set(self.GUARDED))
        module_calls = [c.func.id for stmt in tree.body if not isinstance(stmt, (ast.FunctionDef, ast.ClassDef))
                        for c in ast.walk(stmt) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id in self.GUARDED | {"settle"}]
        self.assertEqual(module_calls, [])                                  # nothing runs at import time
        hooks = {f.name for f in functions} & {"setUpClass", "setUpModule", "tearDownClass", "tearDownModule"}
        self.assertEqual(hooks, set())

    def test_the_guard_refuses_the_real_state_root(self):
        env = sr.build_run(self, contract_raw=S1_RAW, overrides=S1_CONFIG)
        guard(env)                                                          # the harness world passes
        real = paths_module.StatePaths.__new__(paths_module.StatePaths)
        real.root, real.home, real.overridden = REAL_STATE_ROOT, REAL_HOME, False
        with self.assertRaises(AssertionError):
            guard(type("Env", (), {"paths": real})())


if __name__ == "__main__":
    unittest.main()
