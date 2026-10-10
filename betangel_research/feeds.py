"""Bounded public RSS2/Atom metadata collection, without article-body retention.

No redirects, proxies, decompression, XML DTDs or entity declarations are used.
Publication clocks remain source claims; network receipt and parser completion
are separate local clocks. Metadata does not establish news truth or absence.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET


PARSER_VERSION = "public-feeds-v1"
ATOM = "{http://www.w3.org/2005/Atom}"
XHTML = "{http://www.w3.org/1999/xhtml}"
SAFE_HEADERS = {"etag", "last-modified", "retry-after", "content-type",
                "content-length", "content-encoding", "date"}


class _FeedFailure(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _limits(max_bytes, max_items):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 10_000_000:
        raise ValueError("max_bytes must be an integer between 1 and 10000000")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 1000:
        raise ValueError("max_items must be an integer between 1 and 1000")


def _url(value, *, https_only=False):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("invalid URL")
    try:
        value.encode("ascii")
        parts = urlsplit(value)
        if (parts.scheme not in (("https",) if https_only else ("http", "https"))
                or not parts.hostname or parts.username is not None or parts.password is not None
                or "\\" in value or (https_only and (parts.port not in (None, 443) or parts.fragment))):
            raise ValueError("invalid public URL")
        # Force validation of malformed/out-of-range port syntax for metadata too.
        parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid URL") from exc
    return parts


class _TitleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.suppressed += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.suppressed:
            self.suppressed -= 1

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def _text(element):
    return None if element is None else "".join(element.itertext()).strip()


def _title(element, *, atom=False):
    raw = _text(element)
    if not raw or len(raw) > 8192:
        raise _FeedFailure("invalid_title")
    kind = element.get("type", "text") if atom else "html"
    if kind not in ("text", "html", "xhtml"):
        raise _FeedFailure("unsupported_atom_title_type")
    if atom and kind in ("text", "html") and len(element):
        raise _FeedFailure("invalid_atom_title_structure")
    if kind == "xhtml" and (len(element) != 1 or element[0].tag != XHTML + "div"):
        raise _FeedFailure("invalid_atom_title_structure")
    if kind == "html":
        parser = _TitleText()
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    else:
        # Atom text is literal character data. XHTML has already been parsed as
        # XML; a second HTML parse would erase escaped literal angle brackets.
        text = raw
    title = " ".join(text.split())
    if not title or len(title) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise _FeedFailure("invalid_title")
    return title, title != raw


def _date(raw, kind):
    if raw is None:
        return None
    try:
        if kind == "rss":
            parsed = parsedate_to_datetime(raw)
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", raw):
                raise ValueError("date requires explicit timezone")
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("date requires explicit timezone")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError) as exc:
        raise _FeedFailure("malformed_date") from exc


def _item(entry, kind, source_url):
    if kind == "rss2":
        title, title_changed = _title(entry.find("title"))
        provider = _text(entry.find("guid"))
        link = _text(entry.find("link"))
        guid = entry.find("guid")
        if not link and guid is not None and guid.get("isPermaLink", "true").lower() == "true":
            link = provider
        pub_node = entry.find("pubDate")
        # Generic dc:date does not establish publication semantics. A future
        # source-specific adapter may map it only with an explicit contract.
        published = _date(_text(pub_node), "rss") if pub_node is not None else None
        revised = _date(_text(entry.find(ATOM + "updated")), "iso")
    else:
        title, title_changed = _title(entry.find(ATOM + "title"), atom=True)
        provider = _text(entry.find(ATOM + "id"))
        links = [node.get("href") for node in entry.findall(ATOM + "link")
                 if node.get("rel", "alternate") == "alternate" and node.get("href")]
        if len(set(links)) > 1:
            raise _FeedFailure("ambiguous_article_link")
        link = links[0] if links else None
        published = _date(_text(entry.find(ATOM + "published")), "iso")
        revised = _date(_text(entry.find(ATOM + "updated")), "iso")
    if provider is not None and (not provider or len(provider) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in provider)):
        raise _FeedFailure("invalid_provider_id")
    if not link:
        raise _FeedFailure("missing_article_url")
    # XML Base is deliberately unsupported, rather than silently resolving against
    # the wrong base. Normal relative links use the actual requested feed URL.
    if "{http://www.w3.org/XML/1998/namespace}base" in entry.attrib:
        raise _FeedFailure("unsupported_xml_base")
    url = urljoin(source_url, link)
    try:
        _url(url)
    except ValueError as exc:
        raise _FeedFailure("invalid_article_url") from exc
    return ({"provider_id": provider, "url": url, "title": title,
             "published_at": published, "revised_at": revised}, title_changed)


def parse_feed(payload, *, source_url, max_bytes=1_000_000, max_items=200):
    """Parse bounded UTF-8 RSS2/Atom. Invalid rows are skipped with counted flags.

    Missing publication is null, never substituted by updated/receipt time.
    Fatal XML/limit/ambiguous-identity errors return no items. No body text or
    parser exception containing source content is returned.
    """
    _limits(max_bytes, max_items)
    _url(source_url, https_only=True)
    if not isinstance(payload, bytes):
        raise ValueError("payload must be bytes")
    result = {"format": None, "items": [], "parser_error_count": 0, "flags": [], "fatal_error": False}
    flags = set()
    try:
        if len(payload) > max_bytes:
            raise _FeedFailure("response_too_large")
        text = payload.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            raise _FeedFailure("unsafe_xml_declaration")
        encoding = re.match(r"\s*<\?xml\b[^?]*\bencoding\s*=\s*['\"]([^'\"]+)", text, re.I)
        if encoding and encoding[1].lower().replace("-", "") != "utf8":
            raise _FeedFailure("unsupported_xml_encoding")
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = nodes = 0
        root = None
        for offset in range(0, len(text), 8192):
            parser.feed(text[offset:offset + 8192])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    nodes += 1
                    if root is None:
                        root = element
                    if depth > 64 or nodes > 20_000:
                        raise _FeedFailure("xml_structure_limit")
                    if "{http://www.w3.org/XML/1998/namespace}base" in element.attrib:
                        raise _FeedFailure("unsupported_xml_base")
                else:
                    depth -= 1
        parser.close()
        if root is not None and root.tag == "rss" and root.get("version") == "2.0":
            channels = root.findall("channel")
            if len(channels) != 1:
                raise _FeedFailure("invalid_rss_channel")
            entries, kind = channels[0].findall("item"), "rss2"
        elif root is not None and root.tag == ATOM + "feed":
            entries, kind = root.findall(ATOM + "entry"), "atom"
        else:
            raise _FeedFailure("unsupported_feed_format")
        result["format"] = kind
        if len(entries) > max_items:
            raise _FeedFailure("item_limit_exceeded")
        seen = {}
        for entry in entries:
            try:
                item, title_changed = _item(entry, kind, source_url)
            except _FeedFailure as exc:
                result["parser_error_count"] += 1
                flags.add(str(exc))
                continue
            if title_changed:
                flags.add("title_normalized")
            identity = ("provider", item["provider_id"]) if item["provider_id"] is not None else ("url", item["url"])
            if identity in seen:
                if seen[identity] != item:
                    raise _FeedFailure("conflicting_duplicate_identity")
                flags.add("exact_duplicate_item_removed")
                continue
            seen[identity] = item
            if item["published_at"] is None:
                flags.add("publication_unknown")
        result["items"] = list(seen.values())
        if result["parser_error_count"]:
            flags.add("partial_parse")
    except (UnicodeError, ET.ParseError) as exc:
        result.update(items=[], fatal_error=True)
        result["parser_error_count"] += 1
        flags.add("invalid_xml" if isinstance(exc, ET.ParseError) else "unsupported_xml_encoding")
    except _FeedFailure as exc:
        result.update(items=[], fatal_error=True)
        result["parser_error_count"] += 1
        flags.add(str(exc))
    result["flags"] = sorted(flags)
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _FeedFailure("timeout")
    return remaining


def _public_addresses(host, deadline):
    """Bound DNS wait, reject any private answer, then connect to the exact IP."""
    answers = queue.Queue(maxsize=1)

    def resolve():
        try:
            answers.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            answers.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        records = answers.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise _FeedFailure("timeout") from exc
    if isinstance(records, Exception):
        raise _FeedFailure("dns_error")
    addresses = []
    for record in records:
        address = record[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise _FeedFailure("unsafe_destination") from exc
        if (not parsed.is_global or parsed.is_multicast or parsed.is_reserved
                or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified):
            raise _FeedFailure("unsafe_destination")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _FeedFailure("dns_error")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, deadline):
        super().__init__(host, port=443, timeout=_remaining(deadline), context=ssl.create_default_context())
        self.address = address
        self.deadline = deadline
        self._active_socket = None

    def connect(self):
        # Numeric IP pins the socket to the already validated DNS answer; TLS
        # still verifies the original allowed hostname, with no proxy/tunnel.
        raw = socket.create_connection((self.address, 443), timeout=_remaining(self.deadline))
        self.sock = raw
        self._active_socket = raw
        try:
            wrapped = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock = wrapped
            self._active_socket = wrapped
            wrapped.settimeout(_remaining(self.deadline))
            wrapped.do_handshake()
        except Exception:
            self.close()
            raise


def _headers(response):
    selected = {}
    flags = set()
    for name, value in response.getheaders():
        name = name.lower()
        if name not in SAFE_HEADERS:
            continue
        if name in selected:
            raise _FeedFailure("ambiguous_response_headers")
        if len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            if name in ("content-length", "content-encoding"):
                raise _FeedFailure("unsafe_response_headers")
            flags.add("response_header_omitted")
            continue
        selected[name] = value
    return selected, flags


def fetch_feed(url, *, allowed_hosts, etag=None, last_modified=None,
               timeout=10, max_bytes=1_000_000, max_items=200):
    """One bounded HTTPS GET; no retries or redirects, including after HTTP 429.

    Invalid configuration raises ValueError before any request. Network/fatal
    parse failures return status=error; skipped invalid rows mark partial_parse.
    received_at ends network work; normalization_completed_at ends parsing.
    """
    _limits(max_bytes, max_items)
    parts = _url(url, https_only=True)
    if (not isinstance(allowed_hosts, (list, tuple, set, frozenset)) or not allowed_hosts
            or any(not isinstance(h, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", h)
                   or h.startswith(".") or h.endswith(".") for h in allowed_hosts)):
        raise ValueError("allowed_hosts must explicitly list DNS hostnames")
    if parts.hostname.lower() not in {h.lower() for h in allowed_hosts}:
        raise ValueError("request host is not allowlisted")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout must be finite and between 0 and 60 seconds")
    request_headers = {"User-Agent": "betangel-research/public-feeds-v1",
                       "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                       "Accept-Encoding": "identity"}
    for name, value in (("If-None-Match", etag), ("If-Modified-Since", last_modified)):
        if value is not None:
            if (not isinstance(value, str) or not value or len(value) > 512
                    or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise ValueError("conditional request header must be bounded ASCII without controls")
            request_headers[name] = value
    result = {"request_started_at": _now(), "received_at": None, "normalization_completed_at": None,
              "status": "error", "outcome": "network_error", "http_status": None, "headers": {},
              "items": [], "parser_error_count": 0, "flags": [], "format": None,
              "parser_version": PARSER_VERSION, "response_bytes": 0, "payload_sha256": None}
    deadline = time.monotonic() + timeout
    connection = None
    response = None
    timer = None
    payload = None
    flags = set()
    expired = threading.Event()
    try:
        addresses = _public_addresses(parts.hostname, deadline)
        # One connection attempt; subsequent polls, not this helper, own retries.
        connection = _PinnedHTTPSConnection(parts.hostname, addresses[0], deadline)

        def expire():
            expired.set()
            # http.client may clear connection.sock for a Connection: close
            # response while its response file still owns the live socket.
            active = connection._active_socket
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request("GET", target, headers=request_headers)
        response = connection.getresponse()
        _remaining(deadline)
        result["http_status"] = response.status
        result["headers"], header_flags = _headers(response)
        flags.update(header_flags)
        if response.status == 304:
            result.update(status="not_modified", outcome="not_modified")
        elif 300 <= response.status < 400:
            raise _FeedFailure("redirect_refused")
        elif response.status == 429:
            raise _FeedFailure("rate_limited")
        elif response.status != 200:
            raise _FeedFailure("http_error")
        else:
            if result["headers"].get("content-encoding", "identity").lower().strip() not in ("", "identity"):
                raise _FeedFailure("compressed_response_refused")
            length = result["headers"].get("content-length")
            if length is not None and not re.fullmatch(r"[0-9]+", length):
                raise _FeedFailure("invalid_content_length")
            if length is not None and int(length) > max_bytes:
                raise _FeedFailure("response_too_large")
            chunks = []
            while True:
                _remaining(deadline)
                chunk = response.read1(min(65536, max_bytes + 1 - result["response_bytes"]))
                if not chunk:
                    break
                chunks.append(chunk)
                result["response_bytes"] += len(chunk)
                if result["response_bytes"] > max_bytes:
                    raise _FeedFailure("response_too_large")
            _remaining(deadline)
            if length is not None and result["response_bytes"] != int(length):
                raise _FeedFailure("incomplete_response")
            payload = b"".join(chunks)
    except _FeedFailure as exc:
        result["outcome"] = str(exc)
        flags.add(str(exc))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        result["outcome"] = "timeout" if isinstance(exc, TimeoutError) or expired.is_set() or time.monotonic() >= deadline else "network_error"
        flags.add(result["outcome"])
    finally:
        # Timestamp network completion before parsing or normalization.
        result["received_at"] = _now()
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
    if payload is not None:
        result["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        parsed = parse_feed(payload, source_url=url, max_bytes=max_bytes, max_items=max_items)
        for field in ("format", "items", "parser_error_count"):
            result[field] = parsed[field]
        flags.update(parsed["flags"])
        if parsed["fatal_error"]:
            result["outcome"] = "parse_error"
        else:
            result["status"] = "ok"
            result["outcome"] = "partial_parse" if parsed["parser_error_count"] else "ok"
    result["flags"] = sorted(flags)
    result["normalization_completed_at"] = _now()
    return result
