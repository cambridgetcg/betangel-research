import hashlib
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from betangel_research import feeds


URL = "https://feeds.example.test/news/rss.xml"
HOST = "feeds.example.test"
RSS_ITEM = """<item><guid isPermaLink="false">story-1</guid>
<title><![CDATA[A <b>synthetic</b> title &amp; preview]]></title>
<link>/story-1</link><pubDate>Sat, 10 Oct 2026 10:30:00 +0100</pubDate>
<description><![CDATA[ARTICLE BODY MUST NOT BE RETURNED]]></description></item>"""


def rss(items=RSS_ITEM):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            + items + '</channel></rss>').encode()


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = rss() if payload is None else payload
        self.status = status
        self.headers = headers or []
        self.position = 0
        self.reads = 0
        self.closed = False

    def getheaders(self):
        return self.headers

    def read1(self, size):
        self.reads += 1
        data = self.payload[self.position:self.position + size]
        self.position += len(data)
        return data

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = Mock()
        self._active_socket = self.sock
        self.requests = []
        self.closed = False

    def request(self, method, target, headers):
        self.requests.append((method, target, headers))

    def getresponse(self):
        # Real http.client clears sock when a response declares Connection: close.
        self.sock = None
        return self.response

    def close(self):
        self.closed = True


class FeedParserTests(unittest.TestCase):
    def parse(self, payload, **kwargs):
        return feeds.parse_feed(payload, source_url=URL, **kwargs)

    def test_rss_metadata_only_markup_relative_url_and_rfc822_timezone(self):
        result = self.parse(rss())
        self.assertEqual(result["parser_error_count"], 0)
        self.assertEqual(result["format"], "rss2")
        self.assertEqual(result["items"], [{"provider_id": "story-1", "url": "https://feeds.example.test/story-1",
                                           "title": "A synthetic title & preview",
                                           "published_at": "2026-10-10T09:30:00.000000Z", "revised_at": None}])
        self.assertNotIn("ARTICLE BODY", json.dumps(result))

    def test_atom_updated_is_not_invented_publication(self):
        payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>urn:example:entry1</id><title type="html">A &lt;b&gt;title&lt;/b&gt;</title>
        <link rel="alternate" href="../item"/><link rel="self" href="/api/entry1"/>
        <updated>2026-10-10T12:00:00+02:00</updated><content>NOT METADATA</content>
        </entry></feed>'''
        result = self.parse(payload)
        self.assertEqual(result["items"][0]["title"], "A title")
        self.assertIsNone(result["items"][0]["published_at"])
        self.assertEqual(result["items"][0]["revised_at"], "2026-10-10T10:00:00.000000Z")
        self.assertEqual(result["items"][0]["url"], "https://feeds.example.test/item")
        self.assertIn("publication_unknown", result["flags"])
        self.assertNotIn("NOT METADATA", json.dumps(result))

    def test_title_normalization_flag_distinguishes_plain_text_from_edits(self):
        plain = self.parse(rss('<item><title>Plain title &amp; preview</title><link>/plain</link></item>'))
        markup = self.parse(rss())
        spaced = self.parse(rss('<item><title>Title  with\nextra space</title><link>/spaced</link></item>'))
        self.assertNotIn("title_normalized", plain["flags"])
        self.assertEqual(plain["items"][0]["title"], "Plain title & preview")
        self.assertIn("title_normalized", markup["flags"])
        self.assertIn("title_normalized", spaced["flags"])

    def test_atom_text_html_xhtml_keep_distinct_title_semantics(self):
        cases = [('type="text"', 'Horse &lt;Alpha&gt; returns', 'Horse <Alpha> returns', False),
                 ('', 'Horse &lt;Alpha&gt; returns', 'Horse <Alpha> returns', False),
                 ('type="html"', 'Horse &lt;b&gt;Alpha&lt;/b&gt; returns', 'Horse Alpha returns', True),
                 ('type="xhtml"', '<div xmlns="http://www.w3.org/1999/xhtml">Horse <b>Alpha</b> &lt;Beta&gt;</div>',
                  'Horse Alpha <Beta>', False)]
        for attribute, content, expected, normalized in cases:
            with self.subTest(attribute=attribute):
                payload = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                           f'<title {attribute}>{content}</title><link href="/story"/></entry></feed>').encode()
                result = self.parse(payload)
                self.assertEqual(result["items"][0]["title"], expected)
                self.assertEqual("title_normalized" in result["flags"], normalized)

    def test_generic_dublin_core_date_is_not_assumed_publication(self):
        payload = b'''<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
        <item><title>Generic date only</title><link>/story</link>
        <dc:date>2026-10-10T10:00:00Z</dc:date></item></channel></rss>'''
        result = self.parse(payload)
        self.assertIsNone(result["items"][0]["published_at"])
        self.assertIsNone(result["items"][0]["revised_at"])
        self.assertIn("publication_unknown", result["flags"])

    def test_malformed_and_timezone_unknown_dates_skip_rows_visibly(self):
        malformed = RSS_ITEM.replace("story-1", "bad-date").replace("Sat, 10 Oct 2026 10:30:00 +0100", "yesterday")
        naive = RSS_ITEM.replace("story-1", "naive-date").replace("Sat, 10 Oct 2026 10:30:00 +0100", "Sat, 10 Oct 2026 10:30:00")
        result = self.parse(rss(RSS_ITEM + malformed + naive))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["parser_error_count"], 2)
        self.assertIn("malformed_date", result["flags"])
        self.assertIn("partial_parse", result["flags"])

    def test_unknown_publication_is_null_not_receipt_or_channel_date(self):
        item = '<item><title>No date</title><link>/unknown</link></item>'
        result = self.parse(rss('<lastBuildDate>Sat, 10 Oct 2026 10:00:00 GMT</lastBuildDate>' + item))
        self.assertIsNone(result["items"][0]["published_at"])
        self.assertIsNone(result["items"][0]["revised_at"])
        self.assertIsNone(result["items"][0]["provider_id"])

    def test_future_source_date_preserved_for_ingest_quarantine(self):
        result = self.parse(rss(RSS_ITEM.replace("2026", "2099")))
        self.assertTrue(result["items"][0]["published_at"].startswith("2099"))

    def test_exact_duplicates_removed_but_conflicts_fail_whole_feed(self):
        exact = self.parse(rss(RSS_ITEM + RSS_ITEM))
        self.assertEqual(len(exact["items"]), 1)
        self.assertIn("exact_duplicate_item_removed", exact["flags"])
        for items in (RSS_ITEM + RSS_ITEM.replace("preview", "revision"),
                      RSS_ITEM.replace("preview", "revision") + RSS_ITEM):
            result = self.parse(rss(items))
            self.assertTrue(result["fatal_error"])
            self.assertEqual(result["items"], [])
            self.assertIn("conflicting_duplicate_identity", result["flags"])

    def test_xml_dtd_entities_utf16_and_malformed_content_fail_closed(self):
        examples = [b'<!DOCTYPE rss [<!ENTITY leak SYSTEM "file:///private/data">]><rss version="2.0"><channel>&leak;</channel></rss>',
                    b'<!DOCTYPE rss SYSTEM "https://evil.test/feed.dtd"><rss version="2.0"/>',
                    rss().decode().encode("utf-16"), b'<rss version="2.0"><channel>SECRET MALFORMED']
        for payload in examples:
            with self.subTest(payload=payload[:20]):
                result = self.parse(payload)
                self.assertTrue(result["fatal_error"])
                self.assertEqual(result["items"], [])
                self.assertNotIn("SECRET", json.dumps(result))

    def test_declared_non_utf8_encoding_is_refused(self):
        result = self.parse(rss().replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"'))
        self.assertIn("unsupported_xml_encoding", result["flags"])
        self.assertTrue(result["fatal_error"])

    def test_byte_item_and_depth_limits_are_explicit(self):
        cases = [(rss(), {"max_bytes": 20}, "response_too_large"),
                 (rss(RSS_ITEM + RSS_ITEM.replace("story-1", "story-2")), {"max_items": 1}, "item_limit_exceeded"),
                 (rss("<x>" * 70 + "</x>" * 70), {}, "xml_structure_limit")]
        for payload, limits, flag in cases:
            with self.subTest(flag=flag):
                result = self.parse(payload, **limits)
                self.assertTrue(result["fatal_error"])
                self.assertIn(flag, result["flags"])
                self.assertEqual(result["items"], [])

    def test_empty_supported_feed_is_distinct_from_unsupported_format(self):
        valid = self.parse(rss(""))
        invalid = self.parse(b'<html><body>No feed</body></html>')
        self.assertFalse(valid["fatal_error"])
        self.assertEqual(valid["parser_error_count"], 0)
        self.assertTrue(invalid["fatal_error"])

    def test_invalid_link_and_xml_base_are_visible_failures(self):
        result = self.parse(rss(RSS_ITEM.replace("/story-1", "javascript:alert(1)")))
        self.assertIn("invalid_article_url", result["flags"])
        self.assertEqual(result["items"], [])
        result = self.parse(rss().replace(b'<channel>', b'<channel xml:base="https://other.test/">'))
        self.assertTrue(result["fatal_error"])
        self.assertIn("unsupported_xml_base", result["flags"])


class FeedFetchTests(unittest.TestCase):
    def fetch(self, response=None, **kwargs):
        connection = FakeConnection(response or FakeResponse())
        with patch.object(feeds, "_public_addresses", return_value=["1.1.1.1"]) as dns, \
                patch.object(feeds, "_PinnedHTTPSConnection", return_value=connection) as factory:
            result = feeds.fetch_feed(URL, allowed_hosts=[HOST], **kwargs)
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(factory.call_args.args[:2], (HOST, "1.1.1.1"))
        self.assertTrue(connection.closed)
        self.assertTrue(connection.response.closed)
        return result, connection

    def test_conditional_headers_safe_response_headers_and_metadata_hash(self):
        response = FakeResponse(headers=[("ETag", 'W/"123"'), ("Last-Modified", "Sat, 10 Oct 2026 09:00:00 GMT"),
                                         ("Set-Cookie", "private-session"), ("X-Secret", "do not persist")])
        result, connection = self.fetch(response, etag='W/"122"', last_modified="Fri, 09 Oct 2026 09:00:00 GMT")
        sent = connection.requests[0][2]
        self.assertEqual(sent["Accept-Encoding"], "identity")
        self.assertEqual(sent["If-None-Match"], 'W/"122"')
        self.assertEqual(sent["If-Modified-Since"], "Fri, 09 Oct 2026 09:00:00 GMT")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["response_bytes"], len(rss()))
        self.assertEqual(result["payload_sha256"], hashlib.sha256(rss()).hexdigest())
        self.assertEqual(set(result["headers"]), {"etag", "last-modified"})
        self.assertNotIn("private-session", json.dumps(result))

    def test_receipt_precedes_parser_completion_clock(self):
        calls = []
        original = feeds.parse_feed

        def clock():
            calls.append("clock")
            return f"2026-10-10T10:00:0{calls.count('clock')}.000000Z"

        def parse(*args, **kwargs):
            calls.append("parse")
            return original(*args, **kwargs)

        with patch.object(feeds, "_now", side_effect=clock), patch.object(feeds, "parse_feed", side_effect=parse):
            result, _ = self.fetch()
        self.assertEqual(calls, ["clock", "clock", "parse", "clock"])
        self.assertLess(result["request_started_at"], result["received_at"])
        self.assertLess(result["received_at"], result["normalization_completed_at"])

    def test_not_modified_has_no_body_parse_and_no_items(self):
        response = FakeResponse(status=304, headers=[("ETag", '"same"')])
        result, _ = self.fetch(response)
        self.assertEqual(result["status"], "not_modified")
        self.assertEqual(result["items"], [])
        self.assertEqual(response.reads, 0)
        self.assertIsNone(result["payload_sha256"])

    def test_redirects_refused_even_towards_allowed_or_private_hosts(self):
        for target in (URL, "https://127.0.0.1/private", "https://other.test/feed"):
            with self.subTest(target=target):
                result, connection = self.fetch(FakeResponse(status=302, headers=[("Location", target)]))
                self.assertEqual(result["outcome"], "redirect_refused")
                self.assertEqual(len(connection.requests), 1)
                self.assertNotIn("location", result["headers"])

    def test_rate_limit_captures_retry_after_without_retry(self):
        result, connection = self.fetch(FakeResponse(status=429, headers=[("Retry-After", "120")]))
        self.assertEqual(result["outcome"], "rate_limited")
        self.assertEqual(result["headers"]["retry-after"], "120")
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(connection.response.reads, 0)

    def test_compressed_and_oversized_responses_refused(self):
        cases = [(FakeResponse(headers=[("Content-Encoding", "gzip")]), 1000, "compressed_response_refused"),
                 (FakeResponse(headers=[("Content-Length", "2000")]), 1000, "response_too_large"),
                 (FakeResponse(payload=b"x" * 101), 100, "response_too_large")]
        for response, cap, outcome in cases:
            with self.subTest(outcome=outcome):
                result, _ = self.fetch(response, max_bytes=cap)
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["items"], [])
                self.assertLessEqual(result["response_bytes"], cap + 1)

    def test_incomplete_body_and_ambiguous_length_fail_closed(self):
        for response, outcome in [(FakeResponse(payload=b"short", headers=[("Content-Length", "100")]), "incomplete_response"),
                                  (FakeResponse(headers=[("Content-Length", "10"), ("Content-Length", "20")]), "ambiguous_response_headers")]:
            result, _ = self.fetch(response)
            self.assertEqual(result["outcome"], outcome)
            self.assertEqual(result["status"], "error")

    def test_partial_and_fatal_parse_outcomes_are_distinct(self):
        partial, _ = self.fetch(FakeResponse(payload=rss(RSS_ITEM + "<item><title>No URL</title></item>")))
        fatal, _ = self.fetch(FakeResponse(payload=b"not XML"))
        self.assertEqual((partial["status"], partial["outcome"], partial["parser_error_count"]), ("ok", "partial_parse", 1))
        self.assertEqual((fatal["status"], fatal["outcome"]), ("error", "parse_error"))

    def test_configuration_rejects_bad_destinations_and_header_injection_before_dns(self):
        cases = [("http://feeds.example.test/feed", {}), ("https://user:pass@feeds.example.test/feed", {}),
                 ("https://feeds.example.test:444/feed", {}), ("https://elsewhere.test/feed", {}),
                 (URL, {"etag": '"value"\r\nCookie: secret'}), (URL, {"timeout": float("inf")})]
        with patch.object(feeds, "_public_addresses") as dns:
            for url, options in cases:
                with self.subTest(url=url, options=options), self.assertRaises(ValueError):
                    feeds.fetch_feed(url, allowed_hosts=[HOST], **options)
            dns.assert_not_called()

    def test_dns_rejects_private_mixed_and_multicast_answers(self):
        for addresses in (["127.0.0.1"], ["::1"], ["169.254.169.254"], ["224.0.0.1"], ["1.1.1.1", "10.0.0.1"]):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
            with self.subTest(addresses=addresses), patch.object(feeds.socket, "getaddrinfo", return_value=records), \
                    patch.object(feeds, "_PinnedHTTPSConnection") as connection:
                result = feeds.fetch_feed(URL, allowed_hosts=[HOST])
                self.assertEqual(result["outcome"], "unsafe_destination")
                connection.assert_not_called()

    def test_dns_wait_has_a_deadline(self):
        released = threading.Event()

        def stalled(*args, **kwargs):
            released.wait(1)
            return []

        try:
            with patch.object(feeds.socket, "getaddrinfo", side_effect=stalled):
                started = time.monotonic()
                result = feeds.fetch_feed(URL, allowed_hosts=[HOST], timeout=0.02)
                self.assertEqual(result["outcome"], "timeout")
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            released.set()

    def test_read_deadline_interrupts_slow_response_after_connection_sock_cleared(self):
        released = threading.Event()
        response = FakeResponse()
        connection = FakeConnection(response)
        connection._active_socket.shutdown.side_effect = lambda *args: released.set()
        response.read1 = lambda size: (released.wait(1) and b"") or b""
        with patch.object(feeds, "_public_addresses", return_value=["1.1.1.1"]), \
                patch.object(feeds, "_PinnedHTTPSConnection", return_value=connection):
            started = time.monotonic()
            result = feeds.fetch_feed(URL, allowed_hosts=[HOST], timeout=0.02)
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(time.monotonic() - started, 0.5)
        connection._active_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_connection_pins_ip_and_keeps_hostname_for_tls(self):
        raw, wrapped, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = wrapped
        with patch.object(feeds.ssl, "create_default_context", return_value=context), \
                patch.object(feeds.socket, "create_connection", return_value=raw) as connect:
            connection = feeds._PinnedHTTPSConnection(HOST, "1.1.1.1", time.monotonic() + 5)
            connection.connect()
        self.assertEqual(connect.call_args.args[0], ("1.1.1.1", 443))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], HOST)
        wrapped.do_handshake.assert_called_once()


if __name__ == "__main__":
    unittest.main()
