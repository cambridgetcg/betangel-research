# Local metadata collection handoff

Scope: dependency-free local SQLite metadata import only. No network, scheduler, accounts, cloud provisioning, article bodies, entity joins, semantic assessments or betting execution were added. Existing files were not modified by this worker.

## Implemented interface

```python
configure_sources(database_path, sources)
ingest_metadata(database_path, source_id, items, *, request_started_at, received_at,
                status="ok", parser_version="metadata-v1", synthetic=False)
```

`sources` is a nonempty list of `{source_id, data_kind, description}`. Data kind is `synthetic` or `supplied_metadata`; configuration is immutable and identical registration is idempotent. A poll's boolean synthetic flag must match its configured source. The explicit database path's parent directories are created. Existing unrelated or incomplete SQLite schemas are rejected without automatic repair. New schema creation, version assignment, accepted-version view and all ten immutability triggers commit atomically; initialization errors roll them all back. Reopening checks the required tables, view and named triggers on their expected tables.

Each item permits only `provider_id` (optional), `url`, `title`, `published_at` (optional/null), `revised_at` (optional/null). Unknown fields, including body/content/html, are rejected for the whole poll. Publication/revision clocks require explicit offsets, normalize to UTC microseconds and never become receipt times. URL identity is exact and source-scoped unless a provider ID is present. No URL aliasing, fuzzy entity matching or provider-ID migration is inferred. Multiple identities in one poll cannot collide.

Receipt return fields include receipt_id, source_id, request_started_at, received_at, status, parser_version, synthetic, payload_sha256, timestamp_provenance, coverage, item_count, accepted_count, quarantined_count, new_articles, new_versions and idempotent_replay. An exact receipt retry returns its original stored counts with idempotent_replay=true; callers must not count those again as fresh insertions. Otherwise receipt time must increase strictly per source; an equal-time different payload also fails closed. Request start must be no later than receipt. No machine clock manufactures historical availability.

Tables: `news_sources`, `news_receipts`, `news_articles`, `news_versions`, `news_observations`. SQLite rejects UPDATE/DELETE on those tables. One poll is transactional, including receipt, article, version and observation writes. The fixture is a sequence of independent atomic polls, not one transaction.

Future publication/revision and revision-before-publication are quarantined per observation. The original metadata/version and first-seen/first-received receipt are retained. `news_accepted_versions` is an all-versions view with earliest non-quarantined `first_accepted_at`; it is not a latest-version selector. A future-dated item first accepted on later re-observation keeps its original receipt timestamp but cannot use it as accepted availability. Unknown publication remains null with a quality flag. All receipt times are explicitly described as caller-supplied and unauthenticated.

## Evidence and checks

`python3 -m unittest discover -s tests -p test_collection.py -v` completed with exit 0: **22 tests passed**, 0.079 seconds reported by unittest after the independent-review fix. Checks cover temporal cutoffs, unchanged original revisions, future quarantine/re-observation, missing publication, offset normalization, source identity, exact retries after later polls, failed/empty/unchanged coverage, input rejection, SQL immutability, configured-source rollback, injected mid-write failure rollback, late schema-initialization failure rollback and incomplete-schema reopen rejection. Only this new test module was run by this worker; no full-suite, CLI or real-source collection claim is made.

The checked-in synthetic fixture contains eight polls. Tests replay it twice and verify eight total receipts, three articles, four versions and six observations. One observation is quarantined; a later observation accepts that unchanged version. Empty success, failure and not-modified polls are distinct; none assert there was no news. Explicit trailing-whitespace checks found none in the three delivered files.

## Independent-review fix

The reviewer found that the initial schema script committed before trigger creation; an interruption could leave a five-table database accepted without its guards. The corrected bootstrap begins its transaction inside the SQL script (avoiding executescript's pre-script commit behavior), creates all schema objects and guards, validates completeness, then commits. Exception handling explicitly rolls back. Reopening requires all five expected tables, the accepted-version view and all ten required triggers attached to their expected tables; it never repairs incomplete databases silently.

The two added regression tests exercise actual SQLite failure/reopen behavior. An authorizer denies the final CREATE TRIGGER after the tables, view, schema version and preceding guards exist within the transaction; the database then has zero schema objects and user_version=0, and a clean subsequent configure succeeds. Separate missing-view and missing-trigger cases reject both configure and ingest, leave the object missing and preserve the existing source/zero-receipt state. The original 20 tests also pass. No fixture, receipt semantics, CLI or other worker's files changed for this fix.

## Source-contract follow-up

Before a real adapter is implemented, record endpoint/query scope, pagination/completeness, receipt evidence, provider clock semantics, licence/retention terms and publisher-vs-aggregator identity outside this small schema. Keep GDELT discovery time distinct from publisher publication time. Neither a success status nor not-modified proves complete coverage; source config and supplied timestamps are not authenticated here. A separately versioned explicit linking/assessment process is still required before P02/P03 could consume real events. Direct database owners can bypass local triggers; this is not tamper-proof audit storage.

## Artifact hashes

- `betangel_research/collection.py`: `a56d2a61b701173c0dc9ba808e3b56ac3348524c2bc569ad0cd46eba59515372`
- `tests/test_collection.py`: `70f0d86ed44102f76a685a9869e995223dfae76453d793ac65420187a61aa718`
- `research/fixtures/news-metadata.json`: `741f363756c6338b40c2f1d0973accd17655a942a3a229befa20e8e09d705bb8`
