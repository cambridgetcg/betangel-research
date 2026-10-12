"""Neutral market filter mapping tests (synthetic ids and countries)."""

import inspect
import unittest

from betangel_research.betfair import filters
from betangel_research.betfair.constants import REST_MARKET_FILTER_KEYS, STREAM_MARKET_FILTER_KEYS
from betangel_research.betfair.errors import ConfigError

import test_betfair_support as support

NEUTRAL = {"event_type_ids": ["7"], "country_codes": ["GB", "IE"], "market_types": ["WIN"], "market_ids": ["1.1"]}


class FilterTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_rest_keys_never_stream_keys(self):
        rest = filters.to_rest_market_filter(NEUTRAL, start_from_utc="2020-01-01T11:30:00.123457Z",
                                             start_to_utc="2020-01-01T13:00:00+01:00")
        self.assertEqual(rest, {"eventTypeIds": ["7"], "marketCountries": ["GB", "IE"], "marketTypeCodes": ["WIN"],
                                "marketIds": ["1.1"],
                                "marketStartTime": {"from": "2020-01-01T11:30:00.123Z",
                                                    "to": "2020-01-01T12:00:00.000Z"}})
        self.assertTrue(set(rest) <= set(REST_MARKET_FILTER_KEYS))
        self.assertFalse({"countryCodes", "marketTypes"} & set(rest))
        partial = filters.to_rest_market_filter({"event_type_ids": ["7"], "market_types": []})
        self.assertEqual(partial, {"eventTypeIds": ["7"]})
        only_from = filters.to_rest_market_filter(NEUTRAL, start_from_utc="2020-01-01T11:30:00Z")
        self.assertEqual(only_from["marketStartTime"], {"from": "2020-01-01T11:30:00.000Z"})
        with self.assertRaisesRegex(ConfigError, "start_from_utc"):
            filters.to_rest_market_filter(NEUTRAL, start_from_utc="2020-01-01T11:30:00")
        with self.assertRaisesRegex(ConfigError, "reversed"):
            filters.to_rest_market_filter(NEUTRAL, start_from_utc="2020-01-02T00:00:00Z",
                                          start_to_utc="2020-01-01T00:00:00Z")

    def test_stream_keys_never_time(self):
        stream = filters.to_stream_market_filter(NEUTRAL)
        self.assertEqual(stream, {"eventTypeIds": ["7"], "countryCodes": ["GB", "IE"], "marketTypes": ["WIN"],
                                  "marketIds": ["1.1"]})
        self.assertTrue(set(stream) <= set(STREAM_MARKET_FILTER_KEYS))
        self.assertFalse({"marketCountries", "marketTypeCodes", "marketStartTime"} & set(stream))
        self.assertEqual(list(inspect.signature(filters.to_stream_market_filter).parameters), ["f"])
        self.assertEqual(filters.to_stream_market_filter({"country_codes": ["GB"]}), {"countryCodes": ["GB"]})

    def test_neutral_filter_exact_keys(self):
        self.assertEqual(filters.validate_neutral_filter(NEUTRAL), NEUTRAL)
        bad = [
            {"event_type_ids": ["7"], "venues": ["Ascot"]},
            {"eventTypeIds": ["7"]},
            {"market_ids": ["2.123"]},
            {"market_ids": ["1.1234567890123"]},
            {"country_codes": ["gb"]},
            {"event_type_ids": [7]},
            {"event_type_ids": "7"},
            {"event_type_ids": ["7", "7"]},
            {},
            {"event_type_ids": [], "market_ids": []},
            ["7"],
        ]
        for item in bad:
            with self.subTest(item=item):
                with self.assertRaises(ConfigError):
                    filters.validate_neutral_filter(item)

    def test_identifiers_ascii_digits_only(self):
        # Regression: \d matches any Unicode decimal digit; such ids must never reach the wire.
        for item in ({"market_ids": ["1.\u0661\u0662\u0663"]}, {"event_type_ids": ["\u0667"]},
                     {"market_ids": ["1.\uff11"]}, {"event_type_ids": ["\u09ed"]}):
            with self.subTest(item=item):
                with self.assertRaises(ConfigError):
                    filters.validate_neutral_filter(item)
                with self.assertRaises(ConfigError):
                    filters.to_rest_market_filter(item)
        self.assertIsNone(filters.MARKET_ID_RE.fullmatch("1.\u0661\u0662"))

    def test_unknown_key_text_masked(self):
        with self.assertRaises(ConfigError) as caught:
            filters.validate_neutral_filter({"event_type_ids": ["7"], support.PLANTED_SECRET + " x": []})
        self.assertNotIn(support.PLANTED_SECRET, str(caught.exception))
        self.assertIn("<malformed_key>", str(caught.exception))

    def test_market_data_filter_rules(self):
        good = {"fields": ["EX_BEST_OFFERS", "EX_MARKET_DEF"], "ladderLevels": 3}
        self.assertEqual(filters.validate_market_data_filter(good, motif_compatible=True), good)
        with self.assertRaises(ConfigError):
            filters.validate_market_data_filter(dict(good, fields=["EX_MARKET_DEF", "EX_UNKNOWN"]), motif_compatible=True)
        with self.assertRaises(ConfigError):
            filters.validate_market_data_filter(dict(good, fields=["EX_MARKET_DEF", "EX_MARKET_DEF"]),
                                                motif_compatible=False)
        with self.assertRaises(ConfigError):
            filters.validate_market_data_filter(good, motif_compatible="yes")

    def test_motif_compatible_refuses_fields_the_motif_reader_rejects(self):
        """Regression (audit): motif_compatible only capped ladderLevels, so PRO fields (atb/atl, SP) or a
        filter without EX_BEST_OFFERS (no batb/batl) recorded files the motif reader marks unsupported."""
        for fields, code in ((["EX_ALL_OFFERS", "EX_MARKET_DEF"], "EX_ALL_OFFERS"),
                             (["EX_BEST_OFFERS", "SP_PROJECTED", "EX_MARKET_DEF"], "SP_PROJECTED"),
                             (["EX_BEST_OFFERS", "SP_TRADED", "EX_MARKET_DEF"], "SP_TRADED"),
                             (["EX_LTP", "EX_MARKET_DEF"], "EX_BEST_OFFERS_required")):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ConfigError, "not_motif_compatible:" + code):
                    filters.validate_market_data_filter({"fields": fields, "ladderLevels": 3}, motif_compatible=True)
                self.assertEqual(filters.validate_market_data_filter({"fields": fields, "ladderLevels": 3},
                                                                     motif_compatible=False)["fields"], fields)
        default = {"fields": ["EX_BEST_OFFERS", "EX_TRADED", "EX_TRADED_VOL", "EX_LTP", "EX_MARKET_DEF"],
                   "ladderLevels": 3}
        self.assertEqual(filters.validate_market_data_filter(default, motif_compatible=True), default)


if __name__ == "__main__":
    unittest.main()
