# Independent collection implementation review — 2026-10-10

**Final verdict: READY for the local metadata-store/synthetic-CLI scope**, after the narrow fixes described below. Initial verdict was CHANGES NEEDED for C1/C2; both are now resolved on re-review. Reviewer `/root/betangel_profit_evidence` did not author these artifacts. Provider research authored by this reviewer is excluded. No code, tests or production data were changed; no suite, replay, network probe or runtime fault injection was run during this review.

## Initial findings — both resolved

### C1 — P2: schema initialization can leave an accepted database without immutability guards

`betangel_research/collection.py:139` calls `executescript(SCHEMA)` without an explicit transaction covering schema creation. The script sets `user_version = 1` before `_open` creates the UPDATE/DELETE triggers at lines 141–145. SQLite DDL here is not enclosed by the later poll transaction. A failure or interruption after the script succeeds and before all triggers exist can leave the full five-table, version-1 database on disk.

On the next open, lines 136–147 validate only the table-name set and `user_version`, so this incomplete database is accepted. The documented database-enforced immutability guarantee can then be absent for some or all tables. The same check also accepts a version-1 database whose required accepted-version view is missing.

Suggested narrow correction: initialize tables, index, view, version and triggers under one explicit transaction, rolling it back on failure; validate required schema objects on reopening and reject an incomplete schema rather than silently trusting its version flag. Add a bounded regression check for interrupted initialization and missing required guards/view. This finding concerns bootstrap/reopen integrity; ordinary ingest transactions themselves appear atomic.

### C2 — P2: collect-demo ignores the fixture schema version

`betangel_research/__main__.py:136` loads the fixture, but lines 137–148 validate only synthetic classification and source/poll shapes. The checked-in fixture declares `schema_version: 1`; an otherwise identical fixture claiming an unknown version, or omitting the version, is accepted and written under v1 semantics.

Suggested correction: require the supported integer fixture version before configuring sources or ingesting polls, rejecting missing/unsupported values and booleans. Add a CLI check that unsupported fixture versions fail cleanly without creating receipt state. This prevents a future format with changed meanings from being silently interpreted as the present format.

## What passed static review

- Item and envelope validation precedes poll mutations. `BEGIN IMMEDIATE` and the connection context cover receipt/article/version/observation writes; exceptions roll them back. The inspected tests include a mid-transaction injected insert failure.
- Deterministic normalized hashes make an exact receipt retry idempotent, including after later polls. Other receipt times must advance strictly per source. Source registration conflicts roll back as a batch.
- Article first-seen and version first-received clocks use caller-supplied local receipt time, not source publication. The unauthenticated-clock limitation is explicit. A metadata change appends a new version without rewriting the previous version.
- Future publication/revision and revision-before-publication are quarantined per observation. Re-observation can later accept that same version without changing its original quarantine. `news_accepted_versions.first_accepted_at` records the first valid observation, preventing acceptance from being backdated to the original receipt. The view is an inventory of accepted versions, not a latest-version/as-of selector.
- Unknown publication remains null; empty-success, error and unchanged polls have distinct coverage labels. No result is converted into “no news.” Invalid input is rejected rather than partially ingested.
- Source-scoped IDs avoid cross-source accidental merges. Exact URL fallback does not pretend to deduplicate aliases or syndication. No entity links or P03 semantic context are manufactured.
- The fixture is visibly synthetic. Documentation and plan distinguish the local receipt store from proposed network adapters, scheduling, retention services, entity resolution and live inference. They do not claim a deployed live pipeline or proven edge.
- The current schema intentionally omits several future production fields, including query/pagination details, provider-first-seen and analysis completion; the documentation acknowledges this. Collection-plan clocks distinguish local first-seen from provider first-seen.

## Artifacts and evidence

Read complete `collection.py`, `tests/test_collection.py`, the collection CLI/test diff, synthetic fixture, `docs/collection-pipeline.md` and `research/collection-plan.json`. Module SHA-256 matched the coordinator's frozen candidate. The author's reported 20 passing module tests were not independently rerun; coordinator full-suite/demo results were in progress and are not claimed here.

Original-candidate SHA-256 values (superseded where listed below):

- `betangel_research/collection.py`: `4637fe85ed482abc82caf71a08f5c34c69c30079c7de25d8226919e0efb189a2`
- `tests/test_collection.py`: `e233bbef9cb988031c78f2110add2bc4e34b2c6e3ddb576bf6c3978d20bd8ce6`
- `research/fixtures/news-metadata.json`: `741f363756c6338b40c2f1d0973accd17655a942a3a229befa20e8e09d705bb8`
- `betangel_research/__main__.py`: `101fefe7ea99172da3176974b9f219b874acd4582fddf4ab8364991a52faa71b`
- `tests/test_cli.py`: `229605042905a0017a0367edd04dd1f1e3d8601cfc967aa055507c10aec82bc9`
- `docs/collection-pipeline.md`: `6c32caa42605fcdf6b00c7e5f1279aa96db8943a98c944adbc8e199ec9845b36`
- `research/collection-plan.json`: `9c63502a999bf6084de94f240f2ab196a255a8946c1354e30f0f65e811540fab`

No broader integrity, operating-system crash durability, multi-writer stress, retention/deletion policy, current provider accessibility or model behavior was validated.

## Narrow fix re-review

Read the changed bootstrap, schema validation, regression tests and CLI version guard. **C1 resolved:** `BEGIN IMMEDIATE` now starts inside `executescript`, and covers tables, view, version, guard creation and validation before commit. Errors explicitly roll back. Reopening checks the required accepted-version view and all ten named immutability guards' table attachments, rejecting missing objects without repairing them. The new tests deny the final trigger creation, check total schema/version rollback and a subsequent clean open, and reject missing-view/missing-guard databases through both configuration and ingest. This is completeness checking, not authentication against deliberate database tampering.

**C2 resolved:** collect-demo requires `type(schema_version) is int` and value 1 before source configuration. Missing, boolean and unknown versions are rejected. Added CLI cases exercise unknown and boolean versions. No new issue found in these bounded changes.

The collection author reports **22 module tests passed** after C1; coordinator owns final full-suite and demo checks after both edits. This review inspected the regression logic and verified file hashes, but did not rerun tests. Docs retain the accurate limitation that this is a local receipt/version store, and distinguish the recorded failed GDELT probe from coverage/latency validation.

Final reviewed SHA-256 values:

- `betangel_research/collection.py`: `a56d2a61b701173c0dc9ba808e3b56ac3348524c2bc569ad0cd46eba59515372`
- `tests/test_collection.py`: `70f0d86ed44102f76a685a9869e995223dfae76453d793ac65420187a61aa718`
- `betangel_research/__main__.py`: `b5f7459ec9b0c38d012e923c06c6bc18111b7e6d2136cb646542ff36823fc227`
- `tests/test_cli.py`: `247ff1c29d044dd10fb63343eb60f63866fcdbf6bccd38e9ae6b9c33ba6ff3e9`
- `docs/collection-pipeline.md`: `cc8ae7ec2b2976e17be63afff0d442c2b1c0a38e9deb6b4b9d4a0f2cea360fbe`
- Fixture and collection-plan hashes remain as in the original-candidate list.

READY does not cover scheduled collection, cloud deployment, real source completeness, timestamp authenticity, semantic correctness or trading performance.
