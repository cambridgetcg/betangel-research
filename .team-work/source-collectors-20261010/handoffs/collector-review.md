# Independent collector/feed review

Reviewer `/root/vps_research_costs`; 2026-10-10. **READY within the reviewed scope; no unresolved material finding.** Reviewed coordinator-owned `collector.py`/tests and worker-owned `feeds.py`/tests without modifying either implementation or test file. Read `collection.py` for ingestion/identity/clock integration and checked the enabled source configuration. This does not independently review this reviewer's source-research authorship, deployment, backup recovery or real network operation.

## Findings corrected before this verdict

1. Future-dated quarantined metadata previously allowed validators to suppress later refetch. Quarantine now clears validators and withholds success; a later unconditional receipt can accept the same version without making it available at the earlier quarantined time.
2. An extremely large numeric `Retry-After` previously overflowed scheduling after metadata commit, leaving an unreplayable pending receipt. Scheduling now saturates at a valid year-9999 timestamp, records an explicit pause flag and completes the pending receipt. It does not retry early.
3. Generic RSS `dc:date` no longer invents publication semantics. Missing `pubDate` remains unknown.
4. Atom `type=text` no longer passes through HTML stripping; escaped literal angle brackets survive. HTML and XHTML have separate parsing rules.
5. Source-policy title transformations are explicit. The Met Office source requires unmodified textual titles; `title_normalized` causes an error receipt with no article versions stored.
6. Error backoff no longer shortens a configured interval longer than one day. The configured source interval remains a minimum.

## Scope and evidence

Reviewed durable pending-receipt replay, the metadata-commit/scheduler-commit boundary, persisted in-flight attempts, conditional validators, partial parses, unsolicited 304s, 429 scheduling and positive-only jitter. Errors/unchanged/partial/quarantine states remain distinguishable. Receipt time is not substituted for source publication; accepted availability uses the first nonquarantined receipt. Normalization completion is separately recorded in the audit. Any future analysis bridge must still include normalization/link/analysis readiness; this collector does not establish that bridge or promote news into strategies.

Reviewed allowlisted HTTPS destinations, public-address validation and IP pinning with hostname TLS verification, bounded DNS/network waits, response size/XML limits, redirect/compression refusal and safe-header filtering. Source bodies are transient inputs to bounded parsing, not persisted outputs. Metadata-only output and response hashes do not imply article authenticity or complete coverage.

The active config contains only GOV.UK equine Atom and Met Office news RSS at hourly intervals, with policy URLs, attribution and limited context coverage. Met Office's title-preservation gate is enabled. Dates are normalized UTC values; original source-clock spelling is not retained. The state-size guard checks before a bounded poll, so it is a stop threshold, not a strict byte-perfect cap. Source policy changes, finite feed windows and external uptime still require ongoing observation.

**Independent checks actually run:** six network-free adversarial checks in temporary directories passed against the hashes below: literal Atom `<Alpha>` title; generic `dc:date` leaves publication null; future-date quarantine followed by unconditional later acceptance; 400-digit Retry-After completion with finite paused schedule; seven-day error interval preserved; transformed RSS title rejected by the source-policy gate with zero stored versions. Earlier checks exposed the corrected findings. These checks made no live source requests and did not provision or access cloud accounts.

The reviewed test files include crash replay, cadence, 304/429/partial handling, locks, storage stops, parser chronology, identity ambiguity, DNS and transport limits. Authors separately reported **17 collector tests** and **27 feed tests** passing. The coordinator subsequently reported **142 tests** passing. This reviewer did not rerun those full sets; its independent execution claim is the six focused checks above.

## Reviewed SHA-256

```text
1613332a9f01725ff5d74399adac32a39be579a4de65a852e84db99431e118d1  betangel_research/collector.py
d5e3c3fbe0cf8af5446a7e68f1e6c36f2ba2ec30a37662cf0855fcf9d5253cce  tests/test_collector.py
f55d0be4eae3889e2400afc667575da8b23611f536a003804236b926a9ba29a1  betangel_research/feeds.py
564098e8e348720d708a612e63351555eddee0a8dc7a0b2925eb8a4ac526e0c7  tests/test_feeds.py
a56d2a61b701173c0dc9ba808e3b56ac3348524c2bc569ad0cd46eba59515372  betangel_research/collection.py
672484e0bdcee807a281b5a2385958e36aab9b0a82ad69c489c3f4a70a2de9d3  research/collector-sources.json
```
