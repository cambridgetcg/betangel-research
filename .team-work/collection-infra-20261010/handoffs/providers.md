# Provider research handoff — 2026-10-10

Writer `/root/betangel_profit_evidence`; ownership limited to the two new research files and this handoff. No commits, accounts, purchases, credentials, betting connection or full article storage. Public official documentation only; no racing-news coverage or latency sample was performed by this reviewer.

## Delivered

- `docs/data-provider-options.md`: 1,256 whitespace-delimited words, recommendations, provider comparison and prospective collector contract.
- `research/data-provider-options.json`: seven provider options, 23 official sources with retrieval/access metadata, verified displayed prices and explicit nulls, contract and proposed trial.

## Recommendation and decisive findings

Start with a bounded seven-day GDELT metadata discovery trial and manually verified official announcements; use the existing 2017 Betfair files only for parser checks, not paired with current news. The Racing API structured context and Met Office forecasts are later candidate free-plan trials after authorized enrollment and field/terms verification. NewsAPI.org's free tier can check parsing, not minute-scale arrival.

1. [Betfair costs](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access), updated **2026-10-08 19:12**: £499 one-off non-refundable live activation for betting; delayed development access free. [Read-only live-key access is explicitly not permitted](https://support.developer.betfair.com/hc/en-us/articles/25033076334748-What-is-read-only-Betfair-API-access). Do not propose paying this fee as a collection-only route. Delayed price snapshots are documented as variable 1–180 seconds; the key targets the production exchange.
2. [The Racing API FAQ](https://www.theracingapi.com/) says today's cards/odds/results refresh three minutes; indexed [reference](https://api.theracingapi.com/documentation) still says five. [Terms](https://www.theracingapi.com/terms-of-service) make frequency indicative and identify public-source aggregation, not an official course feed. Paid price and point-in-time revision archive are unresolved. HTTP Basic credentials are required, including documented free endpoints. Non-runner result text is not a first-alert feed.
3. [The Racing API ratings](https://www.theracingapi.com/tools/ratings) explicitly document historical recalculation and later-form finalization of non-handicap ratings after 90 days. Exclude unversioned historical ratings from early predictors.
4. [NewsAPI.org pricing](https://newsapi.org/pricing): free Developer has 24-hour delay, 100 requests/day and one-month search depth; no staging/internal production. Business displays $449/month billed monthly. Full articles are unavailable; search depth is not a storage licence. [Terms](https://newsapi.org/terms) also restrict public data/statistics disclosure, so avoid committing third-party trial content/statistics without reviewing the applicable permission.
5. GDELT is a no-key candidate, not verified racing coverage. Its 15-minute infrastructure cadence is not an arrival SLA; article-list and timeline archive behavior differ. Dataset reuse terms do not license linked publishers' full articles. BHA public pages are references, not a verified bulk feed; an Irish authority/course allowlist remains to select. Met Office free Global Spot allows 360 calls/day; product-specific reuse terms remain to verify.

## Collector contract

Separate `event_time`, `published_at`, provider `first_seen_at`, local `received_at`, source `revised_at`, local `revision_received_at` and `analysis_completed_at`. Unknown clocks stay null. Version everything; availability is no earlier than exact-version receipt, explicit entity mapping `known_at` and analysis completion. Preserve syndication clusters and contradictions; ambiguous entity links abstain. Empty queries, caps, throttling and failures are coverage evidence, not proof of no news. Old publication labels on a newly fetched archive cannot become old receipt times.

## Verification and hashes

Local structural check passed: JSON parse, 23 unique source IDs, seven provider IDs, all provider/source references, all document web links present in the source inventory, local Markdown targets exist, access dates consistent. No code tests or live availability checks were run; documentation-only changes do not establish working integration.

SHA-256:

- `docs/data-provider-options.md`: `e99cee14421664bdf39ae60e11d1cfc4562c98056bb8511573f4d86bdd92aa00`
- `research/data-provider-options.json`: `b30745f3c159c6ab54059c8b64690fa17dc0757ee1a6eb71729b588c85de049e`

Access limitations are preserved: Racing API direct ReDoc was a JavaScript shell, linked OpenAPI returned 403 and paid plans did not render; official search-index text supplied endpoint examples. BHA iframe data was not inspected. No bypass or authenticated access attempted. Parent owns integration and independent review.
