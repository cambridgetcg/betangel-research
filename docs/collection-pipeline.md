# Information collection pipeline

As of **10 October 2026**. [Provider research](data-provider-options.md) and [infrastructure choices](infra-options.md) support this design. The metadata store now has a direct RSS/Atom adapter and restart-safe scheduler. The [source investigation](newsapi-source-research.md), [enabled source registry](../research/collector-sources.json), [deployment guide](collector-deployment.md) and [setup receipt](../reports/direct-collection-2026-10-10.json) distinguish actual collection/deployment from future work. Entity resolution and live semantic inference remain unimplemented.

## Collect four different kinds of evidence

| Lane | What it supplies | Main limitation |
| --- | --- | --- |
| Race reference | Racecards, declared runners, horse/race IDs, going, later results | Today's returned record may contain revisions that were unavailable earlier. |
| Exchange market | Market definitions, selection IDs, scheduled times, quotes, volume, suspension/removal | Publisher time is not collector receipt time; a reconnect can break continuity. |
| News discovery | Article links, titles, provider metadata, changes and public statements | Discovery is incomplete; publication, indexing and receipt are different clocks. |
| Conditions | Weather forecasts and official course reports | Preserve the forecast/report vintage; later observations cannot become earlier features. |

An API subscription is useful only if it supplies the needed field, coverage, timestamp and retention rights. NewsAPI's documented article response has publication time and truncated content, rather than a verified race-event time or a complete article. Its Developer plan has a 24-hour delay and permits development/testing only, including restrictions on internal production. [Endpoint contract](https://newsapi.org/docs/endpoints/everything), [pricing and plan restrictions](https://newsapi.org/pricing).

Betfair's current personal Live App Key activation is £499 for betting purposes, with read-only access expressly not permitted. A data-only collector therefore needs an appropriate permitted route; paying that fee alone is not a research-feed solution. Existing public historical samples remain sufficient for local format work. [Official API cost/access notice](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access).

## Proposed flow

```mermaid
flowchart LR
    A[Allowed source adapters] --> B[Receipt and coverage log]
    B --> C[Immutable metadata versions]
    B --> D[Permitted raw objects]
    C --> E[Explicit race and selection links]
    E --> F[Evidence packet and context analysis]
    F --> G[Timed hypothesis or abstention]
    G --> H[Later outcomes and evaluation]
```

**Receipt before interpretation.** The collector records request start, network completion, normalization completion, HTTP status, safe response headers, response hash, parser version and skipped-entry flags. A timeout, 429, error or feed cap is a coverage limitation. A successful empty response means this request returned nothing; it never proves no relevant news exists. Source cadence, last success, conditional headers, exponential backoff, positive jitter and `Retry-After` persist across restarts. RSS/Atom feeds expose a current bounded window: pagination, historical completeness and overlapping query windows are not implemented.

**Immutable versions, separate repeats.** Identity is scoped to the source: provider article ID where present, otherwise the exact supplied URL. Changed metadata creates another version; an identical receipt retry creates no extra event. The same story syndicated by another outlet remains a separate source observation until an explicit story-cluster relation exists. Do not aggressively strip URL parameters, merge horse names or treat ten copies of a press release as ten independent signals.

**Link entities with evidence.** Keep provider horse/race IDs, Betfair market/selection IDs, meeting/date and market type separate. A name match is a candidate link, not a confirmed identity. Each accepted link needs `link_known_at`, source and mapping version. Ambiguous runners or multiple meetings remain unlinked. Actual future results must not be used to repair an earlier mapping retroactively.

**Then interpret.** A later reasoner receives only the available evidence packet and produces structured facts, affected entities, source IDs, novelty/uncertainty, an alternative explanation, analysis completion and expiry. Distinguish a factual non-runner announcement, a tipster opinion and a copied old headline. Retrieved text is evidence data; embedded requests never grant tools, credentials or order authority. A headline alone may be insufficient, and permitted full text is a separate acquisition question.

**Release only when ready.** A future context adapter must enforce:

```text
ready_at = max(received_at, normalization_completed_at,
               link_known_at, analysis_completed_at)
ready_at <= decision_cutoff < effective_expiry
```

`published_at`, provider first-seen and event time remain distinct nullable fields. A backfill retrieved today is available today, even if the article says it was published last year. Corrections receive their own receipt times. Current metadata storage keeps microsecond precision; any later bridge to the existing millisecond motif contract must not round availability backward. No metadata record is automatically promoted to P03 `compatible` context.

## What is implemented

The dependency-free [collection module](../betangel_research/collection.py) stores normalized metadata in local SQLite:

| Table/view | Purpose |
| --- | --- |
| `news_sources` | Explicit immutable source registration and synthetic/supplied classification. |
| `news_receipts` | Poll status, clocks, parser version, metadata hash, counts and coverage meaning. |
| `news_articles` | Source-qualified identity and immutable first local observation. |
| `news_versions` | Title, URL, optional publication/revision clocks and first receipt for each metadata version. |
| `news_observations` | A version's presence in a particular poll, with acceptance/quarantine flags. |
| `news_accepted_versions` | First non-quarantined receipt per version; publication time is never used as availability. Returns all accepted versions, not a latest-as-of selector. |

The store validates supplied clocks, records unknown publication as null and quarantines future publication/revision timestamps. A later valid observation may first become accepted later, without rewriting its earlier quarantine. Exact retries are idempotent; other receipts must advance strictly per source. Each poll is transactional; replaying a file is a series of poll transactions. Database triggers prevent ordinary UPDATE/DELETE of receipt/version records. This is not independent timestamp authentication, a licensed-news archive or a complete retention service.

The module accepts only metadata fields, with no article body. It does not fetch URLs, follow instructions in text, manage secrets or place orders. SQLite databases, raw responses and local operational outputs are ignored by Git; private GitHub contains code, synthetic fixtures and research notes. Any eventual provider-specific retention/deletion requirement must be implemented before collecting that provider's production data.

```sh
python3 -m betangel_research collect-demo \
  --fixture research/fixtures/news-metadata.json \
  --db data/private/news-demo.sqlite
```

Run it twice to inspect idempotent receipt replay. This demo command explicitly accepts synthetic fixtures only. The separate [collector](../betangel_research/collector.py) calls the same store with actual recorded receipt clocks; the store still labels those clocks caller-supplied, not independently verified. `scheduler.sqlite` preserves normalization completion and HTTP/parse audit fields alongside `metadata.sqlite`; provider first-seen, pagination and entity links remain absent.

## Direct-source worker

```sh
python3 -m betangel_research.collector --once
python3 -m betangel_research.collector --status
# Run continuously in the foreground; stop with Ctrl-C:
python3 -m betangel_research.collector
```

The checked-in registry enables GOV.UK equine news and Met Office news hourly. They provide government/health and weather press context, not comprehensive racing news, live racecards or course going. Attribution and policy URLs are retained in source registration; backup manifests also identify the configuration hash. Article links and metadata only are stored; the response body is discarded after bounded parsing.

One process owns the state directory. Before fetching it saves a cadence guard; after parsing it durably saves a pending receipt. Metadata commits before the scheduler acknowledges that receipt. If interrupted between commits, restart replays the exact receipt without duplicating versions. An interruption before durable response capture leaves a coverage-gap flag on the next attempt; responses never received cannot be recovered. `.snapshot.lock` coordinates a collector tick with consistent two-database backups. Both databases must be restored together.

Successful full parses update validators; partial parses and quarantined timestamps clear them so a later complete response can be retried. An unsolicited 304 is recorded as an error. Sources resolve only allowlisted HTTPS hosts to public IP addresses, pin that address with hostname-verified TLS, and refuse redirects/compression. XML declarations with DTD/entities and oversized/deep payloads are rejected. These restrictions can reject otherwise legitimate feeds; they are visible collection failures.

Original date strings are not retained; parsed source times are normalized to UTC. Met Office polls requiring title transformation are rejected because that source requires unchanged content. An unrepresentably large Retry-After pauses that source at the supported calendar ceiling with an explicit flag; it does not poison receipt recovery.

The 1GB state cap stops collection without deleting evidence. It is checked before each tick and may be exceeded by that final bounded tick. No automatic article retention/deletion or unbounded cloud archive schedule is enabled. A source requiring deletion or a different storage policy needs a corresponding implementation before activation. Machine restarts and local heartbeat logs are not an external alert service; monitor source last-success independently before relying on availability.

## Infra boundaries and first experiment

Start with this Mac for parser and fixture work. For 24/7 collection, use one persistent Linux worker close to the data/database region. A small Fly Machine or AWS Lightsail instance can perform this role; platform-specific prices and assumptions are in the [infra comparison](infra-options.md). Supabase is a candidate for managed Postgres and a later dashboard, not a requirement for initial single-writer SQLite work. Keep exchange stream chunks in compressed object storage where permitted; keep receipts, identities, extracted features and experiment indices in the database.

Separate a persistent stream worker from bounded scheduled pollers. Supabase Edge Functions have finite wall-clock and CPU limits; they cannot be treated as an unrestricted daemon. [Official limits](https://supabase.com/docs/guides/functions/limits). A later Bet Angel execution bridge needs its own supported Windows host. The Linux feed collector has dedicated Fly/Tigris resources; see the setup receipt for deployed machine and backup verification. No Windows host has been created.

The next acquisition experiment should fix a small consecutive meeting/date window and approved source list before observing outcomes. Measure source coverage, 429/error frequency, publication-to-receipt lag where publication is trustworthy, revisions, missing links, duplicate story clusters and analysis completion latency. Include empty/failed polls and source outages. Compare a mechanical metadata/coverage gate with semantic analysis at equal availability and delay; greater collection volume is not by itself an edge.

A single bounded GDELT DOC query was actually attempted on 10 October: HTTP 429 after about 11.4s. No article metadata was returned or counted as absent. That checks the failure path only; it does not measure GDELT's racing coverage or normal service latency. The local receipt and probe summary are recorded in the [verification report](../reports/collection-infra-2026-10-10.md).
