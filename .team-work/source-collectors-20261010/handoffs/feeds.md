# Public feed adapter handoff

Implemented only `betangel_research/feeds.py` and `tests/test_feeds.py`. No live request, other file modification or commit was performed by this worker.

## Interface

```python
parse_feed(payload: bytes, *, source_url, max_bytes=1_000_000, max_items=200)
fetch_feed(url, *, allowed_hosts, etag=None, last_modified=None,
           timeout=10, max_bytes=1_000_000, max_items=200)
```

Parser output: `format`, `items`, `parser_error_count`, `flags`, `fatal_error`. Fetch output: actual `request_started_at`, `received_at`, `normalization_completed_at`, `status` (`ok`/`error`/`not_modified`), `outcome`, `http_status`, safe lowercase HTTP `headers`, `items`, `parser_error_count`, `flags`, `format`, `parser_version`, `response_bytes`, `payload_sha256`. Parser version is `public-feeds-v1`.

Items contain only `provider_id`, `url`, `title`, `published_at`, `revised_at`. No description, content or article body is returned. Missing publication is null. Future claimed dates remain claims for collection.py's receipt-aware quarantine. Generic RSS dc:date is ignored because its meaning is not necessarily publication. Raw date spelling is not retained; normalized dates are metadata, not original copies.

RSS2 pubDate uses explicit-zone RFC822 dates; Atom published/updated uses explicit-zone ISO dates. Malformed or timezone-unknown dates skip the row with a counted flag. Atom updated never substitutes for published. RSS titles have simple HTML markup removed; Atom text/html/xhtml preserve their distinct semantics. Literal escaped angle brackets in Atom text are retained. `title_normalized` marks a returned textual title changed by markup stripping or whitespace normalization relative to the XML-decoded textual title. Source terms requiring unchanged title text must reject that flag at integration; the helper does not determine usage rights.

Relative links resolve against the requested feed URL. XML Base is rejected explicitly. Exact duplicate identities are removed with a flag; conflicting versions of one provider ID/exact URL reject the whole feed, independent of ordering. Parser failures return bounded codes without source excerpts. A partially valid feed returns ok/partial_parse with valid items and counted skipped rows; this never establishes complete coverage or no-news absence. Fatal limits/XML/identity errors return error and no items.

## Transport boundaries

One HTTPS GET, port 443, exact allowed hostname; credentials, unsafe URL syntax and unbounded/invalid configuration are rejected before DNS. DNS answers must all be public/global and non-multicast. The connection pins the selected validated IP while default TLS verifies the allowed hostname. There are no proxy/environment routes, redirects, automatic retries or decompression. HTTP 429 preserves bounded Retry-After without a loop. Conditional ETag and Last-Modified headers are supported. Only seven response header names are retained, each at most 512 characters; cookies and Location are omitted.

Default body cap is 1 MB; configured cap cannot exceed 10 MB. The streaming read reads at most cap+1 bytes for overflow detection. Advertised length, actual length and unsupported compression are checked. DNS wait is bounded using a daemon resolver; an OS resolution already in progress may finish after timeout, but cannot initiate a request afterward. A deadline timer shuts down the active socket to stop slow header/body reads, including Connection: close responses for which http.client clears connection.sock. Network receipt is captured before parsing; normalization completion is separate. Neither timestamp measures availability to other traders.

UTF-8 only; DTD/entity declarations, alternate encodings, excessive XML depth/node count and excessive item count fail closed. The entire bounded feed is transiently parsed in memory; only metadata, its source-response hash and byte count escape. The response hash identifies the fetched feed bytes; it is not an article-body archive or source-authentication proof.

## Verification and review changes

`python3 -m unittest discover -s tests -p test_feeds.py -v` completed with exit 0: **27 network-free tests passed**, 0.064 seconds reported by unittest. Tests cover extraction, dates, missing publication, partial/fatal errors, body omission, title semantics/normalization flags, duplicate identities, XML attacks/limits, conditional headers, response bounds, 304/429/redirect behavior, private/mixed DNS rejection, bounded DNS/read waits, TLS hostname with IP pinning, and the receipt-before-parse clock boundary. No full-suite, deployed-network or live-source result is claimed here.

Independent reviewer findings corrected before handoff: removed generic dc:date-to-publication inference and stopped treating Atom plain-text titles as HTML. Coordinator request also added explicit title_normalized provenance and a regression for source contracts requiring unchanged titles. All delivered-file trailing-whitespace checks passed.

## SHA-256

- `betangel_research/feeds.py`: `f55d0be4eae3889e2400afc667575da8b23611f536a003804236b926a9ba29a1`
- `tests/test_feeds.py`: `564098e8e348720d708a612e63351555eddee0a8dc7a0b2925eb8a4ac526e0c7`
