"""Live arming checks A1-A19, the command matrix, the confirm token, anchors and the account registry.
Offline: fake Keychain, fake terminal, temporary state roots. These show which conditions refuse live
mode, not that the conditions match Betfair's real behaviour."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import arming
from betangel_research.betfair.arming import (ACK_IDS, CHECK_IDS, CLOUD_ENV_MARKERS, COMMAND_CHECKS, check_anchor,
                                              checks_for, dynamic_arming, expected_token, register_account,
                                              static_arming)
from betangel_research.betfair.config import config_sha256
from betangel_research.betfair.credentials import ANCHOR_SERVICE, anchor_write
from betangel_research.betfair.errors import NotArmed
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import StatePaths
from betangel_research.betfair.receipts import parse_utc_ns

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as orders

FINGERPRINT = "0123456789abcdef"


class _Session:
    def __init__(self, *, can_bet=True, token=True, fingerprint=FINGERPRINT):
        self._can_bet, self._token, self._fp = can_bet, token, fingerprint

    def can_bet(self):
        return self._can_bet

    def current_token(self):
        return object() if self._token else None

    def account_fingerprint(self):
        if self._fp is None:
            raise arming.AuthError("not_logged_in")
        return self._fp


class ArmingTests(unittest.TestCase):
    def setUp(self):
        self.paths = gw.unoverridden_paths(self)
        self.config = gw.live_config(self.paths)
        self.clock = support.FakeClock()
        self.runner = support.FakeKeychainRunner()
        self.confirmer = gw.FakeConfirmer()

    def static(self, *, config=None, command="place", paths=None, cli_mode="live", token="default",
               bet_angel_paused=False, env=None, confirmer="default", runner=None, journal_anchor=None,
               journal_state=None, journal=None):
        config = config or self.config
        if token == "default":
            token = expected_token(config, self.clock.time_ns())
        return static_arming(config, command=command, paths=paths or self.paths, cli_mode=cli_mode,
                             confirm_token=token, bet_angel_paused=bet_angel_paused, env=env or {},
                             confirmer=self.confirmer if confirmer == "default" else confirmer,
                             keychain_runner=runner or self.runner, journal_anchor=journal_anchor or {},
                             now_ns=self.clock.time_ns(), journal_state=journal_state, journal=journal)

    def book(self, **changes):
        return orders.live_book(self.clock, **changes)

    def dynamic(self, *, command="place", session=None, details=None, state=None, startup=None, book="default",
                paths="default", journal_fingerprint=None, now_mono=None):
        state = state if state is not None else dict(orders.empty_state("live"), ref_roundtrip_verified=True)
        return dynamic_arming(self.config, command=command, session=session or _Session(),
                              account_details=details if details is not None else {"currencyCode": "GBP"},
                              journal_state=state, startup=startup if startup is not None else {"foreign": []},
                              book=self.book() if book == "default" else book,
                              paths=self.paths if paths == "default" else paths,
                              journal_fingerprint=journal_fingerprint,
                              now_monotonic_ns=self.clock.mono_ns if now_mono is None else now_mono)

    def test_paper_needs_no_credentials(self):
        env = gw.build_paper_gateway(self)
        with patch("subprocess.run", side_effect=AssertionError("no Keychain in paper")):
            env.gateway.open(command="place")
            result = env.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(env.clock), source="api")
            env.gateway.close()
        self.assertEqual(result["decision"], "accepted")
        decisions = [e for e in env.journal.events() if e["kind"] == "risk_decision"]
        self.assertTrue(decisions and decisions[0]["payload"]["approved"])

    def test_each_condition_refuses_live(self):
        baseline_static = self.static()
        self.assertTrue(baseline_static["armed"], baseline_static)
        self.assertEqual(baseline_static["checked"], list(CHECK_IDS[:13]))
        baseline_dynamic = self.dynamic()
        self.assertTrue(baseline_dynamic["armed"], baseline_dynamic)
        self.assertEqual(baseline_dynamic["checked"], list(CHECK_IDS[13:]))
        key_temp = tempfile.TemporaryDirectory()
        self.addCleanup(key_temp.cleanup)
        key_dir = Path(key_temp.name)
        cert, key = key_dir / "client.crt", key_dir / "client.key"
        cert.write_text("synthetic cert")
        key.write_text("synthetic key")
        os.chmod(key, 0o644)
        cert_config = copy.deepcopy(self.config)
        cert_config["credentials"].update(login_method="cert", cert_path=str(cert), key_path=str(key))
        statics = {
            "A1": lambda: self.static(cli_mode="paper"),
            "A2": lambda: self.static(token="live-000000000000-20200101"),
            "A3": lambda: self.static(config=self._config(orders={"live": {"acknowledgements": list(ACK_IDS[:-1])}})),
            "A4": lambda: self.static(runner=support.FakeKeychainRunner(items={})),
            "A5": lambda: self.static(config=cert_config),
            "A6": lambda: self.static(config=self._broken_caps()),
            "A7": lambda: self.static(journal_state={"halted": True, "clock_regression": False}),
            "A8": lambda: self.static(config=self._config(account_currency="EUR")),
            "A9": lambda: self.static(config=self._config(orders={"live": {"heartbeat_api_enabled": True}})),
            "A10": lambda: self.static(env={"GITHUB_ACTIONS": "true"}),
            "A11": lambda: self.static(confirmer=gw.FakeConfirmer(present=False)),
            "A12": lambda: self.static(config=self._config(coexistence={"bet_angel": "unverified"})),
            "A13": lambda: self.static(paths=support.temp_state_paths(self)),
        }
        expected = {"A1": "not_armed", "A2": "confirm_token_mismatch", "A3": "acknowledgements_incomplete",
                    "A4": "keychain_credentials_missing", "A5": "cert_files_unsafe", "A6": "caps_invalid",
                    "A7": "halted", "A8": "jurisdiction_or_currency_unsupported", "A9": "heartbeat_unsupported_v1",
                    "A10": "egress_unattested_or_cloud", "A11": "operator_tty_required",
                    "A12": "bet_angel_coexistence_unverified", "A13": "state_root_invalid"}
        for check, build in statics.items():
            with self.subTest(check=check):
                report = build()
                self.assertFalse(report["armed"])
                self.assertEqual(report["by_check"], {check: [expected[check]]})
        unresolved = orders.state_with([orders.order_record(state="UNKNOWN", mode="live")], mode="live",
                                       ref_roundtrip_verified=True)
        dynamics = {
            "A14": (lambda: self.dynamic(session=_Session(can_bet=False)), "login_cannot_bet"),
            "A15": (lambda: self.dynamic(journal_fingerprint="fedcba9876543210"), "account_registered_to_other_profile"),
            "A16": (lambda: self.dynamic(details={"currencyCode": "USD"}), "account_currency_not_gbp_or_unknown"),
            "A17": (lambda: self.dynamic(state=unresolved), "unresolved_or_foreign_orders"),
            "A18": (lambda: self.dynamic(book=self.book(is_market_data_delayed=True)), "market_data_delayed_or_stale"),
            "A19": (lambda: self.dynamic(state=orders.empty_state("live")), "ref_roundtrip_unverified"),
        }
        for check, (build, code) in dynamics.items():
            with self.subTest(check=check):
                report = build()
                self.assertFalse(report["armed"])
                self.assertEqual(report["by_check"], {check: [code]})
        # Every failure is listed, never just the first.
        many = self.static(cli_mode="paper", token=None, env={"CI": "1"}, confirmer=None)
        self.assertEqual(many["failures"], ["not_armed", "confirm_token_mismatch", "egress_unattested_or_cloud",
                                            "operator_tty_required"])

    def _config(self, **overrides):
        return support.deep_merge(self.config, overrides)

    def _broken_caps(self):
        config = copy.deepcopy(self.config)
        config["orders"]["caps"]["max_stake_per_order"] = 51.0
        return config

    def test_dynamic_failure_variants(self):
        self.assertFalse(self.dynamic(details={})["armed"])
        self.assertFalse(self.dynamic(startup={"foreign": [{"bet_id": "1"}]})["armed"])
        self.assertFalse(self.dynamic(startup={"foreign": None})["armed"])
        self.assertFalse(self.dynamic(paths=None)["armed"])                    # A15 fails closed without paths
        self.assertFalse(self.dynamic(session=_Session(fingerprint=None))["armed"])
        stale = self.book(monotonic_ns=self.clock.mono_ns - 3_000_000_000)
        self.assertFalse(self.dynamic(book=stale)["armed"])
        future = self.book(monotonic_ns=self.clock.mono_ns + 1)
        self.assertFalse(self.dynamic(book=future)["armed"])
        for change in ({"fetched_by": "read_command"}, {"app_key_slot": "delayed"}, {"is_market_data_delayed": None},
                       {"catalogue_present": False, "invalid_reason": "catalogue_missing"}):
            with self.subTest(change=change):
                self.assertFalse(self.dynamic(book=self.book(**change))["armed"])
        deferred = self.dynamic(book=None)
        self.assertTrue(deferred["armed"])
        self.assertNotIn("A18", deferred["checked"])
        child = orders.order_record(state="SUBMITTING", mode="live", provisional_child=True,
                                    parent_ref="bro200101120000-aaaaaaaaaaaaaaaa")
        self.assertFalse(self.dynamic(state=orders.state_with([child], mode="live",
                                                              ref_roundtrip_verified=True))["armed"])

    def test_command_condition_matrix(self):
        expected = {
            "place": list(CHECK_IDS), "replace": list(CHECK_IDS), "live-probe": list(CHECK_IDS[:18]),
            "cancel": ["A4", "A5", "A8", "A10", "A12", "A13", "A14", "A15"],
            "reconcile": ["A4", "A5", "A8", "A10", "A12", "A13", "A14", "A15"],
            "reconcile-accept": ["A1", "A2", "A4", "A5", "A8", "A10", "A11", "A12", "A13", "A14", "A15"],
            "settle-market": ["A1", "A2", "A4", "A5", "A8", "A10", "A11", "A12", "A13", "A14", "A15"],
            "kill": ["A4", "A13"], "kill-account": ["A4", "A11", "A13"], "resume": ["A2", "A11", "A13"]}
        self.assertEqual({k: list(v) for k, v in COMMAND_CHECKS.items()}, expected)
        with self.assertRaises(ValueError):
            checks_for("updates")
        # cancel works unarmed and halted (no A1/A2/A7), but still needs the Keychain and a sound state root.
        engage(self.paths, "test", clock=self.clock)
        unarmed = self._config(orders={"live": {"armed": False}})
        report = self.static(config=unarmed, command="cancel", token=None, confirmer=None, cli_mode="paper")
        self.assertTrue(report["armed"], report)
        self.assertEqual(report["checked"], ["A4", "A5", "A8", "A10", "A12", "A13"])
        self.assertFalse(self.static(command="cancel", runner=support.FakeKeychainRunner(items={}))["armed"])
        kill = self.static(config=unarmed, command="kill", token=None, confirmer=None, cli_mode="paper",
                           env={"CI": "1"})
        self.assertTrue(kill["armed"])
        self.assertEqual(kill["checked"], ["A4", "A13"])
        account = self.static(command="kill-account", token=None, confirmer=None)
        self.assertEqual(account["failures"], ["operator_tty_required"])
        resume = self.static(command="resume", token=None)
        self.assertEqual(resume["failures"], ["confirm_token_mismatch"])
        # Login-only commands need a session, not can_bet.
        cancel_dynamic = self.dynamic(command="cancel", session=_Session(can_bet=False), book=None)
        self.assertTrue(cancel_dynamic["armed"])
        self.assertEqual(cancel_dynamic["checked"], ["A14", "A15"])
        self.assertFalse(self.dynamic(command="cancel", session=_Session(token=False), book=None)["armed"])

    def test_token_binds_config_and_utc_date(self):
        token = expected_token(self.config, self.clock.time_ns())
        self.assertRegex(token, r"^live-[0-9a-f]{12}-20200101$")
        self.assertEqual(token[5:17], config_sha256(self.config)[:12])
        changed = self._config(orders={"caps": {"max_stake_per_order": 1.5}})
        self.assertNotEqual(expected_token(changed, self.clock.time_ns()), token)
        next_day = parse_utc_ns("2020-01-02T00:00:00Z")
        self.assertTrue(expected_token(self.config, next_day).endswith("-20200102"))
        self.assertEqual(expected_token(self.config, parse_utc_ns("2020-01-01T23:59:59.999999Z")), token)
        self.assertFalse(self.static(token=expected_token(self.config, next_day))["armed"])
        for bad in (None, "", token.upper(), token + " ", "live-é" + token[6:], 12345):
            with self.subTest(token=bad):
                self.assertIn("confirm_token_mismatch", self.static(token=bad)["failures"])

    def test_env_token_ignored(self):
        token = expected_token(self.config, self.clock.time_ns())
        env = {name: token for name in ("BETANGEL_BETFAIR_CONFIRM_LIVE", "CONFIRM_LIVE", "BETFAIR_TOKEN")}
        with patch.dict(os.environ, env):
            report = self.static(token=None, env=env)
        self.assertEqual(report["failures"], ["confirm_token_mismatch"])
        source = Path(arming.__file__).read_text()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_cloud_env_markers_refuse(self):
        self.assertEqual(CLOUD_ENV_MARKERS, ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CODESPACES",
                                             "KUBERNETES_SERVICE_HOST", "AWS_EXECUTION_ENV", "FLY_APP_NAME",
                                             "FLY_MACHINE_ID"))
        for marker in CLOUD_ENV_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(self.static(env={marker: ""})["failures"], ["egress_unattested_or_cloud"])
        unattested = self._config(orders={"live": {"operator_attests_uk_egress": False}})
        self.assertEqual(self.static(config=unattested)["failures"], ["egress_unattested_or_cloud"])

    def test_missing_environment_fails_closed(self):
        report = static_arming(self.config, command="place", paths=self.paths, cli_mode="live",
                               confirm_token=expected_token(self.config, self.clock.time_ns()), bet_angel_paused=False,
                               env=None, confirmer=self.confirmer, keychain_runner=self.runner, journal_anchor={},
                               now_ns=self.clock.time_ns())
        self.assertEqual(report["failures"], ["egress_unattested_or_cloud"])

    def test_rolled_back_and_reextended_journal_is_a_mismatch(self):
        import shutil
        import sqlite3
        from betangel_research.betfair.errors import NotArmed as Refused
        from betangel_research.betfair.gateway import resume_journal
        from betangel_research.betfair.journal import OrderJournal
        env = gw.build_live_gateway(self)
        journal = env.journal
        for n in range(2):
            journal.append("note", {"text": f"before backup {n}"})
        backup_path = env.paths.orders_dir / "backup.sqlite"
        backup = sqlite3.connect(str(backup_path))
        journal._db.backup(backup)              # a consistent copy at seq 3, as a restore would bring back
        backup.close()
        for n in range(3):
            journal.append("note", {"text": f"after backup {n}"})
        anchor_write("default", dict(journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        anchored = journal.anchor()
        self.assertEqual(anchored["seq"], 6)
        journal.close()
        path = env.paths.journal_path("live")
        for suffix in ("-wal", "-shm"):
            if os.path.exists(str(path) + suffix):
                os.unlink(str(path) + suffix)
        shutil.copyfile(backup_path, path)
        restored = OrderJournal(path, mode="live", clock=env.clock)
        self.addCleanup(restored.close)
        for n in range(4):                      # re-extended past the anchor's seq (as two kills would)
            restored.append("note", {"text": f"forked {n}"})
        self.assertEqual(restored.anchor()["genesis_sha256"], anchored["genesis_sha256"])
        self.assertGreater(restored.anchor()["seq"], anchored["seq"])
        # The weak rule alone (same genesis, higher seq) would pass; the anchored event's hash does not.
        self.assertEqual(check_anchor(restored.anchor(), dict(anchored, updated_at="x")), [])
        report = self.static(journal_anchor=restored.anchor(), runner=env.runner,
                             journal_state=restored.fold(now_ns=env.clock.time_ns()))
        self.assertTrue(report["armed"])        # without the journal itself only the weak rule can run
        report = static_arming(env.config, command="place", paths=env.paths, cli_mode="live", confirm_token=env.token,
                               bet_angel_paused=False, env={}, confirmer=self.confirmer, keychain_runner=env.runner,
                               journal_anchor=restored.anchor(), now_ns=env.clock.time_ns(), journal=restored)
        self.assertEqual(report["failures"], ["journal_anchor_mismatch"])
        with self.assertRaises(Refused) as caught:
            resume_journal(env.config, paths=env.paths, mode="live", journal=restored, confirmer=self.confirmer,
                           confirm_token=env.token, keychain_runner=env.runner, env={}, clock=env.clock)
        self.assertEqual(caught.exception.rule, "journal_anchor_mismatch")
        self.assertEqual(json.loads(env.runner.items[(ANCHOR_SERVICE, "default")])["last_sha256"],
                         anchored["last_sha256"])

    def test_a13_reads_the_keychain_before_the_journal_head(self):
        """Amendment A3 (A18): given a journal, A13 reads the Keychain anchor first and that journal's head after
        it. A writer that journals and then re-anchors between the two reads can only leave journal >= Keychain,
        so there is no false journal_anchor_mismatch (the round-3 kill A13 race, repro kill_a13_race.py)."""
        from betangel_research.betfair.credentials import anchor_read
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        self.addCleanup(journal.close)
        journal.append("note", {"reason": "seed"})
        anchor_write(self.paths.profile, dict(journal.anchor(), updated_at=self.clock.utc_now()), runner=self.runner)
        journal.append("kill", {"reason": "kill", "scope": "own"})
        journal.append("halt", {"reason": "kill", "scope": "global"})
        stale_view = journal.anchor()              # a head read before the Keychain (the old order)
        journal.append("note", {"reason": "holder_report"})
        anchor_write(self.paths.profile, dict(journal.anchor(), updated_at=self.clock.utc_now()), runner=self.runner)
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(arming.anchor_failures(stale_view, anchor_read(self.paths.profile, runner=self.runner),
                                                journal=journal), ["journal_anchor_mismatch"])
        report = self.static(command="cancel", journal_anchor=stale_view, journal=journal, journal_state=state)
        self.assertTrue(report["armed"], report)
        reads = []

        def recording_runner(argv, **kwargs):
            if argv[1] == "find-generic-password" and ANCHOR_SERVICE in argv and "-w" in argv:
                reads.append("keychain")
            return self.runner(argv, **kwargs)

        real_anchor = journal.anchor

        def recording_anchor():
            reads.append("journal")
            return real_anchor()

        with patch.object(journal, "anchor", side_effect=recording_anchor):
            self.static(command="cancel", journal_anchor=stale_view, journal=journal, journal_state=state,
                        runner=recording_runner)
        self.assertEqual(reads[:2], ["keychain", "journal"])

    def test_kill_static_a13_checks_the_state_root_only(self):
        """Amendment A3, D4: the kill's static A13 is the state root only. It reads no Keychain anchor and no
        journal head, so no anchor state can stop the sweep before it starts (the gateway's one anchor check
        then degrades to an untrusted-journal sweep, never to no sweep)."""
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        self.addCleanup(journal.close)
        journal.append("note", {"reason": "seed"})
        head = journal.anchor()
        anchor_write(self.paths.profile, dict(head, seq=head["seq"] + 3, last_sha256="e" * 64,
                                              updated_at=self.clock.utc_now()), runner=self.runner)
        self.assertEqual(self.static(command="cancel", journal=journal)["failures"], ["journal_anchor_mismatch"])
        for command, confirmer in (("kill", None), ("kill-account", "default")):
            with self.subTest(command=command):
                self.runner.calls.clear()
                report = self.static(command=command, token=None, confirmer=confirmer, journal=journal,
                                     journal_anchor=head)
                self.assertTrue(report["armed"], report)
                self.assertFalse(any(ANCHOR_SERVICE in call["argv"] for call in self.runner.calls))
                self.assertEqual(arming.KILL_STATIC_A13_ROOT_ONLY, frozenset({"kill", "kill-account"}))
        overridden = support.temp_state_paths(self)
        self.assertEqual(self.static(command="kill", token=None, confirmer=None, paths=overridden)["failures"],
                         ["state_root_invalid"])

    def test_first_anchor_bootstrap_only_for_a_journal_without_orders(self):
        from betangel_research.betfair.arming import anchor_failures
        from betangel_research.betfair.journal import OrderJournal
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        self.addCleanup(journal.close)
        journal.append("kill", {"reason": "drill", "scope": "own"})
        journal.append("halt", {"reason": "kill", "scope": "global"})
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(anchor_failures(journal.anchor(), None, journal=journal, journal_state=state), [])
        self.assertEqual(anchor_failures(journal.anchor(), None, journal=journal, journal_state=None),
                         ["journal_anchor_missing"])
        self.assertEqual(self.static(journal_anchor=journal.anchor(), journal_state=state, command="resume")["failures"],
                         [])
        journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"), intent_id="int-1",
                       customer_order_ref=orders.REF, customer_ref=orders.CREF, market_id="1.1",
                       state_after="RECORDED")
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(anchor_failures(journal.anchor(), None, journal=journal, journal_state=state),
                         ["journal_anchor_missing"])

    def test_tty_required(self):
        self.assertEqual(self.static(confirmer=None)["failures"], ["operator_tty_required"])
        self.assertEqual(self.static(confirmer=gw.FakeConfirmer(present=False))["failures"], ["operator_tty_required"])

    def test_coexistence_unverified_needs_flag(self):
        unverified = self._config(coexistence={"bet_angel": "unverified"})
        self.assertEqual(self.static(config=unverified)["failures"], ["bet_angel_coexistence_unverified"])
        self.assertTrue(self.static(config=unverified, bet_angel_paused=True)["armed"])
        verified = self._config(coexistence={"bet_angel": "token_survival_verified"})
        self.assertTrue(self.static(config=verified)["armed"])

    def test_anchor_mismatch_and_missing(self):
        journal = {"genesis_sha256": "a" * 64, "seq": 5, "last_sha256": "b" * 64}
        keychain = dict(journal, updated_at="2020-01-01T12:00:00.000000Z")
        self.assertEqual(check_anchor({}, None), [])
        self.assertEqual(check_anchor(journal, keychain), [])
        self.assertEqual(check_anchor(dict(journal, seq=9, last_sha256="c" * 64), keychain), [])
        self.assertEqual(check_anchor(journal, None), ["journal_anchor_missing"])
        self.assertEqual(check_anchor({}, keychain), ["journal_anchor_mismatch"])
        self.assertEqual(check_anchor(dict(journal, genesis_sha256="d" * 64), keychain), ["journal_anchor_mismatch"])
        self.assertEqual(check_anchor(dict(journal, seq=4), keychain), ["journal_anchor_mismatch"])
        self.assertEqual(check_anchor(dict(journal, last_sha256="e" * 64), keychain), ["journal_anchor_mismatch"])
        self.assertEqual(check_anchor(journal, {"seq": 1}), ["journal_anchor_mismatch"])
        # Through static arming: the anchor comes from the (fake) Keychain.
        self.assertEqual(self.static(journal_anchor=journal)["failures"], ["journal_anchor_missing"])
        anchor_write(self.paths.profile, keychain, runner=self.runner)
        self.assertTrue(self.static(journal_anchor=journal)["armed"])
        self.assertEqual(self.static(journal_anchor={})["failures"], ["journal_anchor_mismatch"])
        locked = support.FakeKeychainRunner(find_exit=36)
        self.assertIn("journal_anchor_unreadable", self.static(runner=locked)["failures"])
        self.assertTrue(any(call["argv"][3] == ANCHOR_SERVICE for call in self.runner.calls))

    def test_second_profile_same_account_refused(self):
        register_account(self.paths, FINGERPRINT, "default")
        register_account(self.paths, FINGERPRINT, "default")           # idempotent
        self.assertEqual(json.loads(self.paths.accounts_file.read_text()), {FINGERPRINT: "default"})
        self.assertEqual(self.paths.accounts_file.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(NotArmed) as caught:
            register_account(self.paths, FINGERPRINT, "second")
        self.assertEqual(caught.exception.rule, "account_registered_to_other_profile")
        register_account(self.paths, "fedcba9876543210", "second")    # another account may use another profile
        self.paths.accounts_file.write_text(json.dumps({FINGERPRINT: "second"}))
        self.assertEqual(self.dynamic()["failures"], ["account_registered_to_other_profile"])
        self.paths.accounts_file.write_text("{not json")
        with self.assertRaises(NotArmed):
            register_account(self.paths, FINGERPRINT, "default")
        self.assertEqual(self.dynamic()["failures"], ["account_registered_to_other_profile"])
        # Through the gateway: the account already belongs to another profile.
        env = gw.build_live_gateway(self)
        from betangel_research.betfair.auth import account_fingerprint_for
        from betangel_research.betfair.secret import Secret
        fingerprint = account_fingerprint_for(Secret("synthetic-user"))
        env.paths.accounts_file.write_text(json.dumps({fingerprint: "elsewhere"}))
        report = env.gateway.open(command="place", confirm_token=env.token)
        self.addCleanup(env.gateway.close)
        self.assertFalse(report["armed"])
        self.assertIn("account_registered_to_other_profile", report["failures"])

    def test_registry_survives_losing_accounts_json(self):
        """Regression (audit): accounts.json was the only record; once it was deleted a second profile could
        register the same account with a fresh journal and fresh caps. Another profile's live journal
        carrying the fingerprint now refuses it too."""
        first = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        first.set_account_fingerprint(FINGERPRINT)
        first.close()
        register_account(self.paths, FINGERPRINT, "default")
        self.paths.accounts_file.unlink()
        second = StatePaths(profile="second")
        second.ensure()
        with self.assertRaises(NotArmed) as caught:
            register_account(second, FINGERPRINT, "second")
        self.assertEqual(caught.exception.rule, "account_registered_to_other_profile")
        self.assertFalse(second.accounts_file.exists())
        register_account(second, "fedcba9876543210", "second")         # a different account is fine
        register_account(self.paths, FINGERPRINT, "default")             # its own profile re-registers
        self.assertEqual(arming.journal_fingerprints(self.paths), {"default": FINGERPRINT})
        # A journal that exists but cannot be read fails closed.
        broken = second.journal_path("live")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b"not a database at all" * 20)
        with self.assertRaises(NotArmed) as caught:
            register_account(self.paths, FINGERPRINT, "default")
        self.assertEqual(caught.exception.rule, "account_registry_unverifiable")

    def test_account_currency_absent_refuses(self):
        for details in ({"currencyCode": None}, {}, {"currencyCode": "EUR"}, None, {"currencyCode": "gbp"}):
            with self.subTest(details=details):
                report = dynamic_arming(self.config, command="place", session=_Session(), account_details=details,
                                        journal_state=dict(orders.empty_state("live"), ref_roundtrip_verified=True),
                                        startup={"foreign": []}, book=None, paths=self.paths)
                self.assertEqual(report["failures"], ["account_currency_not_gbp_or_unknown"])

    def test_heartbeat_enabled_refuses(self):
        enabled = self._config(orders={"live": {"heartbeat_api_enabled": True}})
        self.assertEqual(self.static(config=enabled)["failures"], ["heartbeat_unsupported_v1"])

    def test_probe_exempt_from_a19_only(self):
        self.assertEqual(checks_for("live-probe"), CHECK_IDS[:18])
        no_roundtrip = orders.empty_state("live")
        probe = self.dynamic(command="live-probe", state=no_roundtrip)
        self.assertTrue(probe["armed"])
        self.assertNotIn("A19", probe["checked"])
        self.assertEqual(self.dynamic(command="place", state=no_roundtrip)["failures"], ["ref_roundtrip_unverified"])
        self.assertFalse(self.dynamic(command="live-probe", details={"currencyCode": "USD"})["armed"])
        self.assertFalse(self.static(command="live-probe", token=None)["armed"])

    def test_settle_market_check_set(self):
        """Amendment A3 (D3): settle-market has reconcile-accept's checks: armed, the token, a terminal and
        Bet Angel paused until T3, but no A7 (it runs halted or killed, since it sends no order), and A14
        needs a session only (a normal, not an emergency, login)."""
        self.assertEqual(checks_for("settle-market"), checks_for("reconcile-accept"))
        self.assertNotIn("A7", checks_for("settle-market"))
        self.assertIn("settle-market", arming.LOGIN_ONLY_COMMANDS)
        engage(self.paths, "test", clock=self.clock)
        killed = self.static(command="settle-market", bet_angel_paused=True)
        self.assertTrue(killed["armed"], killed)
        self.assertNotIn("A7", killed["checked"])
        self.assertIn("bet_angel_coexistence_unverified",
                      self.static(config=self._config(coexistence={"bet_angel": "unverified"}),
                                  command="settle-market")["failures"])
        for missing in ({"token": None}, {"confirmer": None}):
            with self.subTest(missing=missing):
                self.assertFalse(self.static(command="settle-market", **missing)["armed"])
        session = self.dynamic(command="settle-market", session=_Session(can_bet=False), book=None)
        self.assertTrue(session["armed"])
        self.assertEqual(session["checked"], ["A14", "A15"])

    def test_live_replace_refused_at_open_before_any_check(self):
        """Amendment A3 (D2): a live gateway opened for replace refuses before any arming check, Keychain
        read, lock, login or request (defence in depth: the CLI refuses earlier still)."""
        from betangel_research.betfair.errors import OrderRefused
        env = gw.build_live_gateway(self)
        calls, requests = len(env.runner.calls), len(env.transport.requests)
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.open(command="replace", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
        self.assertEqual((len(env.runner.calls), len(env.transport.requests)), (calls, requests))
        self.assertFalse(env.paths.gateway_lock.exists())
        self.assertIsNone(env.gateway._command)

    def test_failure_codes_are_snake_case_literals(self):
        for check, codes in arming.FAILURE_CODES.items():
            for code in codes:
                with self.subTest(check=check, code=code):
                    self.assertRegex(code, r"^[a-z0-9_]+$")
        self.assertEqual(ACK_IDS, arming.LIVE_ACK_IDS)
        self.assertEqual(set(arming.FAILURE_CODES), set(CHECK_IDS))


if __name__ == "__main__":
    unittest.main()
