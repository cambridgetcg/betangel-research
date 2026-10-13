"""Constants, provenance and error-taxonomy tests: offline checks of compiled facts, not of the live API."""

import json
from pathlib import Path
import unittest

from betangel_research.betfair import constants, errors
from betangel_research.betfair.secret import Secret

import test_betfair_support as support

FACTS_PATH = support.REPO_ROOT / "research" / "betfair-facts.json"
CONSTANTS_SOURCE = Path(constants.__file__).read_text()
PLANTED = support.PLANTED_SECRET


def public_constants():
    return sorted(name for name in dir(constants) if name.isupper() and not name.startswith("_") and name != "PROVENANCE")


class ConstantsTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_every_constant_has_provenance_fact_id(self):
        for name in public_constants():
            with self.subTest(name=name):
                self.assertIn(name, constants.PROVENANCE)
                ids = constants.PROVENANCE[name]
                self.assertIsInstance(ids, tuple)
                self.assertTrue(ids)
                for fact_id in ids:
                    self.assertRegex(fact_id, r"^F\d{2}$")
        self.assertEqual(set(constants.PROVENANCE), set(public_constants()))

    def test_fact_ids_exist_in_research_facts_json(self):
        facts = json.loads(FACTS_PATH.read_text())
        known = {fact["id"] for fact in facts["facts"]}
        # F77 (settle-market's inferred premise) was added by the v1 live narrowing (amendment A3).
        self.assertEqual(known, {f"F{n:02d}" for n in range(1, 78)})
        for fact in facts["facts"]:
            self.assertEqual(list(fact), facts["fields"])
            self.assertIn(fact["accessed"], ("2026-10-10", "2026-10-12"))   # register, second round
            self.assertIn(fact["evidence_type"], facts["vocabulary"]["evidence_type"])
            if fact["secondary_evidence_type"] is not None:
                self.assertIn(fact["secondary_evidence_type"], facts["vocabulary"]["evidence_type"])
            self.assertIn(fact["verification_status"], facts["vocabulary"]["verification_status"])
            self.assertTrue(fact["sources"])
            # Reserved for our own first-live-test receipts (spec section 15): a fact carrying it cites one;
            # test_betfair_facts_doc checks the receipt against the checklist and the committed reports.
            if "first_person_observation" in (fact["evidence_type"], fact["secondary_evidence_type"]):
                self.assertTrue(any(s.startswith("live-test:T") for s in fact["sources"]))
            if any(source.startswith("local:") for source in fact["sources"]) and len(fact["sources"]) == 1:
                self.assertNotIn(fact["evidence_type"], ("official_documentation", "first_person_observation"))
        by_id = {fact["id"]: fact for fact in facts["facts"]}
        self.assertEqual(by_id["F62"]["secondary_evidence_type"], "forum_participant_opinion")
        # The one exit code credentials.anchor_read interprets is disclosed against its fact.
        self.assertIn("exit 44", by_id["F68"]["code_handling"])
        for name, ids in constants.PROVENANCE.items():
            for fact_id in ids:
                with self.subTest(name=name, fact=fact_id):
                    self.assertIn(fact_id, known)

    def test_classic_ladder_350_ticks_101_to_100000(self):
        ladder = []
        for low, high, step in constants.CLASSIC_BANDS:
            ladder.extend(range(low, high, step))
        ladder.append(constants.CLASSIC_MAX_PRICE_H)
        self.assertEqual(len(ladder), 350)
        self.assertEqual(len(ladder), constants.CLASSIC_LADDER_TICKS)
        self.assertEqual(ladder, sorted(set(ladder)))
        self.assertEqual((ladder[0], ladder[-1]), (101, 100000))
        self.assertEqual(constants.CLASSIC_MIN_PRICE_H, 101)
        for price in (101, 199, 200, 202, 300, 305, 410, 620, 1050, 2100, 3200, 5500, 11000, 99000):
            self.assertIn(price, ladder)
        for price in (100, 201, 301, 405, 610, 1020, 2050, 3100, 5200, 10500):
            self.assertNotIn(price, ladder)

    def test_ceilings_positive_and_below_vendor_limits(self):
        ceilings = constants.ABSOLUTE_CEILINGS
        self.assertEqual(set(ceilings), set(constants.CEILING_PATHS))
        for name, value in ceilings.items():
            with self.subTest(name=name):
                self.assertNotIsInstance(value, bool)
                self.assertGreater(value, 0)
        self.assertLessEqual(ceilings["max_instructions_per_request"], constants.PLACE_INSTRUCTIONS_LIMIT_IT)
        self.assertLessEqual(ceilings["session.max_successful_logins_per_10min"],
                             constants.MAX_SUCCESSFUL_LOGINS_PER_MINUTE)
        self.assertLess(ceilings["max_orders_per_minute"], constants.INSTRUCTIONS_PER_SECOND_LIMIT)
        self.assertLess(ceilings["max_transactions_per_hour"], constants.TRANSACTION_CHARGE_THRESHOLD_PER_HOUR)
        self.assertLess(ceilings["recorder.max_markets"], constants.STREAM_DEFAULT_SUBSCRIPTION_LIMIT)
        self.assertLess(ceilings["http.order_deadline_s"], constants.ORDER_VISIBILITY_S)
        self.assertLessEqual(ceilings["max_orders_per_hour"], ceilings["max_orders_per_day"])

    def test_no_order_method_names_in_constants(self):
        for word in ("place" + "Orders", "cancel" + "Orders", "replace" + "Orders", "update" + "Orders",
                     "Heartbeat" + "APING", "heartbeat/json-rpc"):
            with self.subTest(word=word):
                self.assertNotIn(word, CONSTANTS_SOURCE)
        for method in constants.READ_METHODS:
            self.assertTrue(method.split("/")[-1].startswith(("list", "get")))

    def test_read_methods_exact_set(self):
        self.assertEqual(constants.READ_METHODS, frozenset({
            "SportsAPING/v1.0/listEventTypes", "SportsAPING/v1.0/listMarketCatalogue",
            "SportsAPING/v1.0/listMarketBook", "SportsAPING/v1.0/listCurrentOrders",
            "SportsAPING/v1.0/listClearedOrders", "AccountAPING/v1.0/getAccountFunds",
            "AccountAPING/v1.0/getAccountDetails"}))

    def test_allowed_paths_exclude_heartbeat(self):
        self.assertEqual(constants.ALLOWED_PATHS, frozenset({
            "/api/certlogin", "/api/login", "/api/keepAlive", "/api/logout",
            "/exchange/betting/json-rpc/v1", "/exchange/account/json-rpc/v1"}))
        for path in constants.ALLOWED_PATHS:
            self.assertNotIn("heartbeat", path.lower())
            self.assertIn(path, constants.PATH_HOST_KIND)
        self.assertNotIn("api.betfair.com", constants.STREAM_HOSTS)
        self.assertEqual(constants.STREAM_PORT, 443)


class ErrorTaxonomyTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_surface_required_and_codes_differ_by_surface(self):
        self.assertEqual(errors.classify("UNEXPECTED_ERROR", surface="aping"), "unknown_outcome")
        self.assertEqual(errors.classify("UNEXPECTED_ERROR", surface="stream_status"), "stream_reconnect")
        self.assertEqual(errors.classify("TIMEOUT", surface="execution_report"), "unknown_outcome")
        self.assertEqual(errors.classify("TIMEOUT", surface="stream_status"), "stream_fatal")
        with self.assertRaises(ValueError):
            errors.classify("UNEXPECTED_ERROR", surface="nowhere")

    def test_unknown_codes_unrecognized(self):
        self.assertEqual(errors.classify("ANGX-0004", surface="angx"), "unrecognized")
        self.assertEqual(errors.classify("SOMETHING_NEW", surface="aping"), "unrecognized")
        self.assertEqual(errors.classify(-32603, surface="jsonrpc"), "unknown_outcome")
        self.assertEqual(errors.classify("-32700", surface="jsonrpc"), "planner_bug")
        self.assertEqual(errors.classify("HTTP_503", surface="http"), "unknown_outcome")
        self.assertEqual(errors.classify("HTTP_404", surface="http"), "unrecognized")
        self.assertEqual(errors.classify("BET_TAKEN_OR_LAPSED", surface="instruction_report"), "market_state")
        self.assertEqual(errors.classify("INSUFFICIENT_FUNDS", surface="execution_report"), "terminal_account")
        self.assertEqual(errors.classify("SUBSCRIPTION_LIMIT_EXCEEDED", surface="stream_status"),
                         "stream_subscription_limit")
        self.assertEqual(set(errors.ERROR_CATEGORIES.values()) | {"unrecognized"}, set(errors.CATEGORIES))

    def test_login_classification(self):
        self.assertEqual(errors.classify_login("SUCCESS", None), "proceed")
        self.assertEqual(errors.classify_login("LIMITED_ACCESS", "PENDING_AUTH"), "reads_only")
        self.assertEqual(errors.classify_login("CERT_AUTH_REQUIRED", None), "cert_setup_required")
        self.assertEqual(errors.classify_login("KYC_REQUIRED", None), "operator_action_on_website")
        self.assertEqual(errors.classify_login("FAIL", "TEMPORARY_BAN_TOO_MANY_REQUESTS"), "login_ban")
        self.assertEqual(errors.classify_login("SUCCESS", "MULTI_FACTOR_AUTHENTICATION_REQUIRED"),
                         "operator_action_on_website")
        self.assertEqual(errors.classify_login("FAIL", "STRONG_CODE_FAIL"), "auth_error")
        self.assertEqual(errors.classify_login("INVALID_USERNAME_OR_PASSWORD", None), "auth_error")

    def test_exception_text_is_code_only(self):
        exc = errors.TransportError("deadline_exceeded", phase="receive")
        self.assertEqual(str(exc), "receive:deadline_exceeded")
        self.assertTrue(exc.may_have_arrived)
        self.assertFalse(errors.TransportError("connect_failed", phase="connect").may_have_arrived)
        api = errors.ApiError("TOO_MANY_REQUESTS", surface="aping", request_uuid="synthetic-uuid")
        self.assertEqual((api.category, str(api)), ("rate_limited", "aping:TOO_MANY_REQUESTS:rate_limited"))
        refused = errors.NotArmed("not_armed", {"failures": ["not_armed"]})
        self.assertIsInstance(refused, errors.OrderRefused)
        self.assertEqual(str(refused), "not_armed")
        self.assertIsInstance(errors.ConfigError("x"), ValueError)
        self.assertEqual(str(errors.LoginBanned("2020-01-01T12:21:00.000000Z")),
                         "login_banned_until:2020-01-01T12:21:00.000000Z")

    def test_free_text_and_secrets_never_reach_exception_text(self):
        Secret(PLANTED)
        cases = [
            errors.ApiError("token=abc123secretvalue", surface="angx"),
            errors.ApiError("Session token " + PLANTED + " expired", surface="angx"),
            errors.ApiError(PLANTED, surface="aping"),
            errors.TransportError("bad code with spaces", phase="receive"),
            errors.StreamError({"errorMessage": PLANTED}),
            errors.AuthError("FAIL", error_code="detail: " + PLANTED),
            errors.OrderRefused("rule\nwith newline"),
            errors.LoginBanned("until " + PLANTED),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                for text in (str(exc), repr(exc), repr(exc.args)):
                    self.assertNotIn(PLANTED, text)
                    self.assertNotIn("abc123secretvalue", text)
                    self.assertIn(errors.MALFORMED_CODE, text)
        self.assertEqual(cases[0].code, errors.MALFORMED_CODE)
        self.assertEqual(cases[0].category, "unrecognized")
        # Ordinary codes pass through unchanged, ints included.
        self.assertEqual(errors.ApiError(-32603, surface="jsonrpc").code, "-32603")
        self.assertEqual(errors.ApiError("ANGX-0004", surface="angx").code, "ANGX-0004")
        self.assertEqual(str(errors.ApiError("DSC-0019", surface="dsc")), "dsc:DSC-0019:unknown_outcome")
        self.assertEqual(errors.OrderRefused("validation:V9b").rule, "validation:V9b")

    def test_api_error_category_must_be_known(self):
        with self.assertRaisesRegex(ValueError, "category"):
            errors.ApiError("TIMEOUT", surface="execution_report", category="no_chnage")
        with self.assertRaisesRegex(ValueError, "surface"):
            errors.ApiError("TIMEOUT", surface="nowhere")
        with self.assertRaisesRegex(ValueError, "surface"):
            errors.ApiError("TIMEOUT", surface="nowhere", category="unknown_outcome")
        explicit = errors.ApiError("TIMEOUT", surface="execution_report", category="unknown_outcome")
        self.assertEqual(explicit.category, "unknown_outcome")


if __name__ == "__main__":
    unittest.main()
