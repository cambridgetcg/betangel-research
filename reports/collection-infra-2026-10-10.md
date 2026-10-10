# Collection and infrastructure work — 10 October 2026

The existing project now has a [private GitHub repository](https://github.com/cambridgetcg/betangel-research), a [collection pipeline design](../docs/collection-pipeline.md), [seven-provider comparison](../docs/data-provider-options.md), [AWS/Supabase/Fly cost model](../docs/infra-options.md), and a runnable local metadata receipt store. Cloud resources, provider subscriptions and betting-account integrations have not been created.

## Recommended sequence

1. Use the Mac and SQLite to validate source receipts, revisions and gaps. Measure news coverage before buying a feed; the price stream, race reference data and news discovery are separate sources.
2. When continuous collection is justified, run one persistent Linux worker. Prefer Fly + volume/Tigris for a container workflow, or Lightsail + S3 for AWS consolidation. A small VM is a starting sizing assumption, not a performance guarantee.
3. Add Supabase when shared Postgres, concurrent writers or a remote application justify it. Its Edge Functions are bounded jobs, not the host for a permanent Python stream. Bet Angel remains a separate Windows-host decision.

For the declared 30-day workload, the model gives **US$11.40** for Fly London + volume + snapshots + Tigris + specified egress, **US$35.30** for Fly + volume + Supabase, and a **US$12.50 known subtotal** for Lightsail 2GB + snapshots. The AWS subtotal excludes unresolved S3/log/secrets charges. All scenarios exclude data/news subscriptions, model calls, tax and other documented omissions; complete monthly totals remain unknown. The [cost model](../research/infra-options.json) supplies quantities and official source URLs, including Fly's 1 October pricing update.

NewsAPI's free Developer plan has a 24-hour delay and excludes internal production; its current monthly Business price is $449. It is not the first purchase for an unmeasured racing-news edge. [Official pricing](https://newsapi.org/pricing). GDELT can test public discovery, but coverage/reliability require measurement. The Racing API offers structured context with uncertain historical-vintage semantics and a 3-versus-5-minute documentation discrepancy, not a verified instantaneous news feed. [Provider evidence and unresolved checks](../docs/data-provider-options.md).

## Implemented and checked

`python3 -m unittest discover -s tests -v`: **84 tests passed** — the 60 existing tests, 22 metadata-store tests and 2 new CLI tests. Added checks cover receipt/version idempotency, source-local chronology, unknown timestamps, quarantine/re-observation, transaction rollback, interrupted schema setup, missing immutability guards, source registration and synthetic-fixture validation.

The synthetic demo creates **8 receipts, 3 source-qualified articles, 4 versions and 6 observations**. Repeating it recognizes all 8 receipts and leaves those counts unchanged. These constructed examples validate storage behavior; they are not actual collected racing news.

```sh
python3 -m betangel_research collect-demo \
  --db data/private/news-demo.sqlite
```

A single unauthenticated GDELT DOC query for English horse-racing articles over 24 hours returned **HTTP 429** after 11.393s. It produced no article metadata. Its actual request/completion clocks were imported into a separate local error receipt with `collection_failed_coverage_unknown`. No automatic retries or additional API requests were made. This checks failure recording, not normal API latency, news absence or racing coverage. [Exact verification summary and code hashes](collection-infra-2026-10-10.json).

A separate code reviewer found two issues: non-atomic schema initialization and acceptance of unknown fixture versions. Both were fixed and re-reviewed; final verdict READY for the local store/synthetic CLI. Another independent review checked provider claims and clock/coverage framing against primary sources. Neither reviewer ran the coordinator's full suite or public probe. Review handoffs are in the [task notes](../.team-work/collection-infra-20261010/).

The SQLite view returns all accepted versions, not a latest-as-of result. Receipt clocks are supplied by the caller and are not authenticated by the store. Production adapters, query/page audits, outbox/queue, raw-object writer, entity linking, semantic inference and the P03 context bridge remain design work. No forecast gain, execution behavior or ROI was measured.

## Repository publication

Publication is private, under `cambridgetcg/betangel-research`. Source code, synthetic fixtures, derived reports and research notes are included. Local SQLite files, credentials, ignored provider raw data and operational outputs remain excluded. Existing archive files and earlier strategy specifications were preserved. No cloud provisioning, deployment or CI outcome is implied by the GitHub push.
