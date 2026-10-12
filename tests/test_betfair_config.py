"""Config validation tests on synthetic configs under temporary state roots (no real config is read)."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import config as cfg
from betangel_research.betfair.errors import ConfigError
from betangel_research.betfair.paths import FORBID_NETWORK_ENV

import test_betfair_support as support


class ConfigTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)

    def load(self, overrides=None):
        path = support.write_test_config(self.paths, overrides)
        return cfg.load_config(path, paths=self.paths)

    def refused(self, overrides, pattern=None):
        with self.assertRaises(ConfigError) as caught:
            self.load(overrides)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        return caught.exception

    def test_example_config_validates(self):
        loaded = cfg.load_config(support.write_test_config(self.paths), paths=self.paths)
        self.assertEqual(loaded["profile"], "default")
        self.assertEqual(loaded["credentials"]["cert_path"],
                         str(self.paths.home / ".config/betangel-research/betfair/client-2048.crt"))
        self.assertEqual(loaded["orders"]["paper"]["commission_rate"], 0.05)
        self.assertFalse(loaded["orders"]["live"]["armed"])
        # Defaults fill keys a file leaves out; the example and DEFAULTS agree apart from commission.
        example = json.loads(support.EXAMPLE_CONFIG.read_text())
        del example["orders"]["paper"]["commission_rate"], example["orders"]["paper"]["commission_source"]
        self.assertEqual(example, cfg.DEFAULTS)
        minimal = {"schema_version": 1, "orders": {"paper": {"commission_rate": 0.02,
                                                             "commission_source": "official_example_basic_2pct"}}}
        self.paths.config_path.write_text(json.dumps(minimal))
        merged = cfg.load_config(self.paths.config_path, paths=self.paths)
        self.assertEqual(merged["orders"]["caps"], cfg.DEFAULTS["orders"]["caps"])

    def test_unknown_key_rejected(self):
        self.refused({"extra": True}, "unknown_config_key:extra")
        self.refused({"http": {"proxy": "x"}}, r"unknown_config_key:http\.proxy")
        self.refused({"credentials": {"username": "x"}}, "unknown_config_key")
        self.refused({"recorder": {"market_filter": {"event_type_ids": ["7"], "venues": ["Ascot"]}}},
                     "market_filter_unknown_key")

    def test_bool_not_number(self):
        self.refused({"orders": {"caps": {"max_orders_per_minute": True}}})
        self.refused({"orders": {"caps": {"max_stake_per_order": True}}})
        self.refused({"http": {"timeout_s": False}})
        self.refused({"stream": {"port": True}})
        self.refused({"orders": {"live": {"armed": 1}}}, "armed_must_be_boolean")

    def test_kill_cancels_own_orders_must_be_true(self):
        """Amendment A3, D4: the key stays so older configs load, but a kill always sweeps our bets; false (the
        old kill_file_only kill, which exited 0 with no sweep) is a config error, and non-booleans stay one."""
        self.assertIs(self.load()["orders"]["live"]["kill_cancels_own_orders"], True)
        self.refused({"orders": {"live": {"kill_cancels_own_orders": False}}},
                     "^orders.live.kill_cancels_own_orders_must_be_true$")
        self.refused({"orders": {"live": {"kill_cancels_own_orders": 1}}}, "kill_cancels_own_orders_must_be_boolean")

    def test_cap_above_ceiling_rejected(self):
        self.load({"orders": {"caps": {"max_stake_per_order": 50}}})
        self.refused({"orders": {"caps": {"max_stake_per_order": 50.01}}}, "max_stake_per_order_above_absolute_ceiling")
        self.refused({"orders": {"caps": {"max_instructions_per_request": 11}}}, "above_absolute_ceiling")
        self.refused({"orders": {"caps": {"max_book_age_s": 5.5}}}, "above_absolute_ceiling")
        self.refused({"orders": {"rules": {"external_exposure_tolerance": 200.01}}}, "above_absolute_ceiling")
        self.refused({"http": {"order_deadline_s": 13}}, r"http\.order_deadline_s_above_absolute_ceiling")
        self.refused({"session": {"max_successful_logins_per_10min": 61}}, "above_absolute_ceiling")
        self.refused({"session": {"max_consecutive_failed_logins": 4}}, "above_absolute_ceiling")
        self.refused({"recorder": {"max_markets": 181}}, "above_absolute_ceiling")
        self.refused({"orders": {"caps": {"max_stake_per_order": 2.001}}}, "at_most_2dp")
        self.refused({"orders": {"caps": {"max_stake_per_order": 0}}}, "positive")
        self.refused({"orders": {"rules": {"min_stake": 0.5}}}, "min_stake_below_1_00")

    def test_commission_rate_and_source_required(self):
        self.assertNotIn("commission_rate", cfg.DEFAULTS["orders"]["paper"])
        self.assertNotIn("commission_source", cfg.DEFAULTS["orders"]["paper"])
        for key in ("commission_rate", "commission_source"):
            with self.subTest(key=key):
                config = support.make_test_config(self.paths)
                del config["orders"]["paper"][key]
                self.paths.config_path.write_text(json.dumps(config))
                with self.assertRaisesRegex(ConfigError, f"missing_config_key:orders.paper.{key}"):
                    cfg.load_config(self.paths.config_path, paths=self.paths)
        self.refused({"orders": {"paper": {"commission_source": "guess"}}}, "commission_source_not_allowed")
        self.refused({"orders": {"paper": {"commission_rate": 1.0}}}, "commission_rate")

    def test_no_mode_key(self):
        self.refused({"mode": "live"}, "unknown_config_key:mode")
        self.refused({"orders": {"mode": "live"}}, r"unknown_config_key:orders\.mode")
        self.refused({"orders": {"live": {"mode": "live"}}}, "unknown_config_key")
        self.assertNotIn("mode", cfg.DEFAULTS)
        self.assertNotIn("mode", cfg.DEFAULTS["orders"])

    def test_cert_paths_inside_worktree_rejected(self):
        repo = self.paths.home / "repo"
        (repo / ".git").mkdir(parents=True)
        self.refused({"credentials": {"cert_path": str(repo / "client.crt")}}, "cert_path_inside_git_worktree")
        linked = self.paths.home / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: /elsewhere\n")
        self.refused({"credentials": {"key_path": str(linked / "keys" / "client.key")}}, "key_path_inside_git_worktree")
        self.refused({"credentials": {"key_path": str(repo / "client.key")}}, "key_path_inside_git_worktree")
        if support.IN_GIT_CHECKOUT:              # the real checkout too, when the tree is one (not an export)
            self.refused({"credentials": {"key_path": str(support.REPO_ROOT / "client.key")}},
                         "key_path_inside_git_worktree")
        self.refused({"credentials": {"cert_path": "relative/client.crt"}}, "cert_path_must_be_absolute")
        self.refused({"credentials": {"cert_path": "~someone/client.crt"}}, "other_user_home")

    def test_recorder_root_inside_worktree_rejected(self):
        repo = self.paths.home / "repo"
        (repo / ".git").mkdir(parents=True)
        self.refused({"recorder": {"root": str(repo / "data" / "private" / "betfair")}}, "root_inside_git_worktree")
        self.refused({"read": {"out_root": str(repo / "outputs")}}, "out_root_inside_git_worktree")
        if support.IN_GIT_CHECKOUT:              # the real checkout too, when the tree is one (not an export)
            self.refused({"recorder": {"root": str(support.REPO_ROOT / "data" / "private" / "betfair")}},
                         "root_inside_git_worktree")
            self.refused({"read": {"out_root": str(support.REPO_ROOT / "outputs")}}, "out_root_inside_git_worktree")
        self.refused({"recorder": {"root": "data/recordings"}}, "root_must_be_absolute")
        outside = self.paths.home / "big-disk" / "recordings"
        loaded = self.load({"recorder": {"root": str(outside)}, "read": {"out_root": "~/reads"}})
        self.assertEqual(loaded["recorder"]["root"], str(outside))
        self.assertEqual(loaded["read"]["out_root"], str(self.paths.home / "reads"))

    def test_config_sha_stable_under_key_order(self):
        config = support.make_test_config(self.paths)
        first = self.paths.profile_dir / "a.json"
        second = self.paths.profile_dir / "b.json"
        first.write_text(json.dumps(config, sort_keys=True))
        reordered = dict(reversed(list(config.items())))
        reordered["orders"] = dict(reversed(list(config["orders"].items())))
        second.write_text(json.dumps(reordered, indent=4))
        a = cfg.load_config(first, paths=self.paths)
        b = cfg.load_config(second, paths=self.paths)
        self.assertEqual(cfg.config_sha256(a), cfg.config_sha256(b))
        changed = cfg.load_config(support.write_test_config(self.paths, {"orders": {"caps": {"max_orders_per_day": 199}}}),
                                  paths=self.paths)
        self.assertNotEqual(cfg.config_sha256(a), cfg.config_sha256(changed))

    def test_hosts_for_each_jurisdiction(self):
        expected = {
            "com": ("identitysso.betfair.com", "identitysso-cert.betfair.com"),
            "com.au": ("identitysso.betfair.com.au", "identitysso-cert.betfair.com.au"),
            "it": ("identitysso.betfair.it", "identitysso-cert.betfair.it"),
            "es": ("identitysso.betfair.es", "identitysso-cert.betfair.es"),
            "ro": ("identitysso.betfair.ro", "identitysso-cert.betfair.ro"),
        }
        for jurisdiction, (sso, cert) in expected.items():
            with self.subTest(jurisdiction=jurisdiction):
                loaded = self.load({"jurisdiction": jurisdiction, "session": {"keepalive_interval_s": 600}})
                hosts = cfg.hosts_for(loaded)
                self.assertEqual(hosts, {"sso": sso, "sso_cert": cert, "api": "api.betfair.com",
                                         "stream_host": "stream-api.betfair.com", "stream_port": 443})
                self.assertEqual(cfg.allowed_http_hosts(loaded), frozenset({sso, cert, "api.betfair.com"}))
        # Regression (audit): Betfair documents New Zealand logins on the AUS/NZ (.com.au) SSO hosts with
        # api.betfair.com.au, so that pairing is com.au; com with the .au API host is refused.
        nz = self.load({"jurisdiction": "com.au", "api_host": "api.betfair.com.au",
                        "session": {"keepalive_interval_s": 600}})
        self.assertEqual(cfg.hosts_for(nz), {"sso": "identitysso.betfair.com.au",
                                             "sso_cert": "identitysso-cert.betfair.com.au",
                                             "api": "api.betfair.com.au", "stream_host": "stream-api.betfair.com",
                                             "stream_port": 443})
        self.refused({"api_host": "api.betfair.com.au"}, "api_host_must_be_api_betfair_com")
        for jurisdiction in ("it", "es", "ro"):
            with self.subTest(jurisdiction=jurisdiction):
                self.refused({"jurisdiction": jurisdiction, "api_host": "api.betfair.com.au",
                              "session": {"keepalive_interval_s": 600}}, "api_host_must_be_api_betfair_com")
        self.refused({"jurisdiction": "us"}, "jurisdiction_not_allowed")

    def test_api_and_stream_host_allowlists(self):
        self.refused({"api_host": "api.example.invalid"}, "api_host_not_allowed")
        self.refused({"stream": {"host": "stream.example.invalid"}}, "stream.host_not_allowed")
        self.refused({"stream": {"port": 444}}, "stream.port_must_be_443")
        self.load({"stream": {"host": "stream-api-integration.betfair.com"}})

    def test_keepalive_bounds_by_jurisdiction(self):
        cases = [("com", 21599, True), ("com", 21600, False), ("com", 59, False), ("it", 600, True),
                 ("it", 601, False), ("es", 601, False), ("com.au", 3600, True), ("com.au", 3601, False),
                 ("ro", 3601, False)]
        for jurisdiction, interval, ok in cases:
            with self.subTest(jurisdiction=jurisdiction, interval=interval):
                overrides = {"jurisdiction": jurisdiction, "session": {"keepalive_interval_s": interval}}
                if ok:
                    self.load(overrides)
                else:
                    self.refused(overrides, "keepalive_interval_s")

    def test_ex_best_offers_disp_refused(self):
        self.refused({"recorder": {"market_data_filter": {"fields": ["EX_BEST_OFFERS_DISP", "EX_MARKET_DEF"],
                                                          "ladderLevels": 3}}}, "ex_best_offers_disp_refused")

    def test_ex_market_def_required(self):
        self.refused({"recorder": {"market_data_filter": {"fields": ["EX_BEST_OFFERS", "EX_LTP"], "ladderLevels": 3}}},
                     "ex_market_def_required")
        config = support.make_test_config(self.paths)
        config["recorder"]["market_data_filter"] = {"fields": ["EX_MARKET_DEF"]}   # replaced wholesale, not merged
        self.paths.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ConfigError, "keys_must_be"):
            cfg.load_config(self.paths.config_path, paths=self.paths)

    def test_ladder_levels_capped_when_motif_compatible(self):
        fields = ["EX_BEST_OFFERS", "EX_MARKET_DEF"]
        self.refused({"recorder": {"market_data_filter": {"fields": fields, "ladderLevels": 4}}}, "not_motif_compatible")
        self.load({"recorder": {"motif_compatible": False,
                                "market_data_filter": {"fields": fields, "ladderLevels": 4}}})
        self.refused({"recorder": {"motif_compatible": False,
                                   "market_data_filter": {"fields": fields, "ladderLevels": 11}}}, "out_of_range")
        self.refused({"recorder": {"market_data_filter": {"fields": fields, "ladderLevels": True}}}, "out_of_range")

    def test_strategy_ref_pattern(self):
        self.load({"orders": {"customer_strategy_ref": "a" * 15}})
        for bad in ("a" * 16, "bar_manual", "", "bar manual", 7):
            with self.subTest(bad=bad):
                self.refused({"orders": {"customer_strategy_ref": bad}}, "customer_strategy_ref_invalid")

    def test_huge_numbers_are_config_errors(self):
        # Regression: ArithmeticError subclasses escaped as non-ValueErrors (CLI exit 4, not 2).
        cases = [
            {"orders": {"caps": {"max_stake_per_order": 1e30}}},
            {"orders": {"caps": {"max_stake_per_order": 10 ** 400}}},
            {"orders": {"caps": {"funds_reserve": 1e300}}},
            {"orders": {"rules": {"min_stake": 10 ** 30}}},
            {"http": {"timeout_s": 10 ** 400}},
            {"orders": {"caps": {"max_book_age_s": -10 ** 400}}},
            {"read": {"retry_backoff_s": [10 ** 400]}},
            {"orders": {"caps": {"max_orders_per_day": 10 ** 400}}},
        ]
        for overrides in cases:
            with self.subTest(overrides=str(overrides)[:80]):
                error = self.refused(overrides)
                self.assertIsInstance(error, ValueError)
        for value in (1e30, 10 ** 400, -10 ** 400, 1e15, 10 ** 12):
            with self.subTest(value=str(value)[:20]):
                with self.assertRaises(ConfigError):
                    cfg.money_pence(value)
        self.assertEqual(cfg.money_pence(2.5), 250)
        self.assertEqual(cfg.money_pence(99999999999.99), 9999999999999)
        # A huge literal in the file itself is refused by the strict loader.
        self.paths.config_path.write_text('{"schema_version": 1, "http": {"timeout_s": 1e400}}')
        with self.assertRaisesRegex(ConfigError, "config_not_strict_json"):
            cfg.load_config(self.paths.config_path, paths=self.paths)

    def test_unknown_key_text_masked(self):
        error = self.refused({"orders": {"caps": {support.PLANTED_SECRET + " pasted": 1}}}, "unknown_config_key")
        self.assertNotIn(support.PLANTED_SECRET, str(error))
        self.assertIn("<malformed_key>", str(error))
        self.refused({"orders": {"rules": {"event_type_ids": ["\u0667"]}}}, "event_type_ids")

    def test_override_path_needs_forbid_env_and_profile_match(self):
        from betangel_research.betfair.paths import StatePaths
        real_like = StatePaths(profile="default", env={})
        other = Path(tempfile.mkdtemp(dir=self.paths.home)) / "config.json"
        other.write_text(json.dumps(support.make_test_config(self.paths)))
        with self.assertRaisesRegex(ConfigError, "config_override_not_allowed"):
            cfg.load_config(other, paths=real_like)
        # Regression: the paths' env alone does not unlock --config while the process network is on.
        with patch.dict(os.environ):
            os.environ.pop(FORBID_NETWORK_ENV, None)
            with self.assertRaisesRegex(ConfigError, "config_override_not_allowed"):
                cfg.load_config(other, paths=self.paths)
        self.assertEqual(cfg.load_config(other, paths=self.paths)["profile"], "default")
        with self.assertRaisesRegex(ConfigError, "config_missing"):
            cfg.load_config(self.paths.profile_dir / "absent.json", paths=self.paths)
        self.refused({"profile": "other"}, "config_profile_does_not_match")
        self.paths.config_path.write_text('{"schema_version": 1, "schema_version": 1}')
        with self.assertRaisesRegex(ConfigError, "config_not_strict_json"):
            cfg.load_config(self.paths.config_path, paths=self.paths)
        self.refused({"stream": {"app_key_slot": "live"}}, "stream_live_key_needs_recorder_allow_live_key")
        self.refused({"orders": {"live": {"acknowledgements": ["no_sandbox", "no_sandbox"]}}}, "acknowledgements")
        self.refused({"orders": {"reconcile": {"window_s": 10}}}, "window_s")


if __name__ == "__main__":
    unittest.main()
