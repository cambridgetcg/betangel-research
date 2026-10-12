"""Secret redaction and leak-check tests using the planted secret value."""

import copy
import json
import pickle
import tempfile
import unittest
from pathlib import Path

from betangel_research.betfair import receipts
from betangel_research.betfair.errors import SecretLeak
from betangel_research.betfair.secret import (FORBIDDEN_KEYS, Secret, assert_clean, normalise_key, refuse_forbidden,
                                              scrub)

import test_betfair_support as support


class SecretTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.secret = Secret(support.PLANTED_SECRET)

    def test_secret_redacted_repr_str_format(self):
        for text in (repr(self.secret), str(self.secret), f"{self.secret}", "{}".format(self.secret),
                     "%s" % self.secret, f"{self.secret!r}", format(self.secret, ">40"), repr([self.secret]),
                     repr({"token": self.secret})):
            with self.subTest(text=text):
                self.assertNotIn(support.PLANTED_SECRET, text)
                self.assertIn("<redacted>", text)
        self.assertEqual(self.secret.reveal(), support.PLANTED_SECRET)
        with self.assertRaises(TypeError):
            json.dumps({"x": self.secret})
        with self.assertRaises(AttributeError):
            self.secret._value = "changed"
        with self.assertRaises(ValueError):
            Secret("")
        with self.assertRaises(ValueError):
            Secret(None)

    def test_secret_pickle_refused(self):
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol):
                with self.assertRaises(TypeError):
                    pickle.dumps(self.secret, protocol=protocol)
        with self.assertRaises(TypeError):
            self.secret.__getstate__()
        self.assertIs(copy.deepcopy({"k": self.secret})["k"], self.secret)

    def test_registry_scan_blocks_write(self):
        with self.assertRaisesRegex(SecretLeak, "secret_value_in_output:out"):
            assert_clean(("prefix " + support.PLANTED_SECRET).encode(), where="out")
        with self.assertRaises(SecretLeak):
            assert_clean(json.dumps({"note": support.PLANTED_SECRET}), where="out")
        quoted = Secret('synthetic"quoted\\value')
        with self.assertRaises(SecretLeak):
            assert_clean(json.dumps({"note": quoted.reveal()}).encode(), where="out")
        assert_clean(b"nothing secret here", where="out")
        with self.assertRaises(SecretLeak) as caught:
            assert_clean(support.PLANTED_SECRET, where="journal")
        self.assertNotIn(support.PLANTED_SECRET, str(caught.exception))
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "receipt.json"
            with self.assertRaises(SecretLeak):
                receipts.atomic_write_json(target, {"note": "x" + support.PLANTED_SECRET})
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(temp).iterdir()), [])
            with self.assertRaises(SecretLeak):
                receipts.atomic_write_json(target, {"nested": [{"value": self.secret}]})

    def test_forbidden_keys_case_and_separator_insensitive(self):
        self.assertEqual(normalise_key("X-Authentication"), "xauthentication")
        self.assertEqual(normalise_key("session_Token"), "sessiontoken")
        self.assertEqual(normalise_key("first name"), "firstname")
        for key in ("X-Authentication", "SESSION_TOKEN", "sessionToken", "App-Key", "appKey", "Set-Cookie",
                    "sourceIdKey", "errorDetails", "First Name", "last_name", "headers", "ssoid", "Username"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(SecretLeak, "forbidden_key_in_output"):
                    refuse_forbidden({"outer": [{"inner": {key: "x"}}]}, where="row")
        refuse_forbidden({"session_id": "rec-1", "app_key_slot": "delayed", "token_count": 1, "key_type": "live"},
                         where="row")
        self.assertIn("xapplication", FORBIDDEN_KEYS)

    def test_scrub_drops_sourceidkey_and_names(self):
        raw = {"currentOrders": [{"betId": "1", "sourceIdKey": "synthetic-live-key-0001",
                                  "customerOrderRef": "bro-1"}],
               "firstName": "Synthetic", "lastName": "Person", "currencyCode": "GBP",
               "error": {"errorDetails": "x", "errorCode": "TOO_MUCH_DATA"}, "token": self.secret}
        cleaned = scrub(raw)
        self.assertEqual(cleaned, {"currentOrders": [{"betId": "1", "customerOrderRef": "bro-1"}],
                                   "currencyCode": "GBP", "error": {"errorCode": "TOO_MUCH_DATA"}})
        self.assertIn("sourceIdKey", raw["currentOrders"][0])
        refuse_forbidden(cleaned, where="scrubbed")


if __name__ == "__main__":
    unittest.main()
