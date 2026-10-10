# Independent provider and collection research review

Reviewer: `/root/vps_research_costs`. Date: 2026-10-10.

**Scoped verdict: no material findings in the four reviewed documentation artifacts at the hashes below.** This is a read-only content/source review, not verification of collector implementation, live provider coverage or production readiness.

Reviewed `docs/data-provider-options.md`, `research/data-provider-options.json`, `docs/collection-pipeline.md` and `research/collection-plan.json`. Did not review this reviewer's own infrastructure report as independent work. No code tests, data-API calls, cloud actions, subscriptions or account access performed. Only this handoff was written.

Three material official sources independently spot-checked on 2026-10-10:

- [NewsAPI pricing](https://newsapi.org/pricing): Developer lists 24-hour delay and 100 requests/day; its FAQ excludes staging and production, including internal production. The reviewed documents accurately separate parsing experiments from timely production collection. Business pricing/search depth are vendor capabilities, not evidence of racing recall or immutable historical versions.
- [Betfair read-only access notice](https://support.developer.betfair.com/hc/en-us/articles/25033076334748-What-is-read-only-Betfair-API-access): collecting with a Live App Key without an intention to bet is not permitted; the notice directs data-only access toward the delayed key. The documents correctly avoid presenting the activation fee as a general collection-only licence.
- [The Racing API homepage FAQ](https://www.theracingapi.com/): currently states a three-minute update cadence for today's cards/odds/results. This supports the inventory's current vendor claim, not guaranteed first arrival. The five-minute indexed-documentation discrepancy was explicitly left unresolved; this review did not independently retrieve that older indexed claim or an authenticated response. Paid amount, per-field revision history and timed withdrawal alerts remain appropriately unverified.

Temporal and coverage reasoning reviewed:

- Event time, publication, provider detection, local receipt, version correction, identity-link knowledge and analysis completion are distinct. Backfill availability remains acquisition time, and revised historical ratings/later results are excluded from earlier knowledge by design.
- The proposed release rule waits for receipt, normalization, identity link and completed interpretation. Caller-supplied clocks and missing production fields are disclosed; no independent clock authenticity is claimed. Future publication/revision observations are described as quarantined. Millisecond adaptation must not move availability backward.
- Official announcements, structured racing data, exchange state, weather and general news are treated as different evidence. No tested UK/Irish racing-news completeness, measured speed advantage or verified withdrawal cause is asserted. Weather forecasts retain their vintage; observed going is separate.
- Query caps, outages, errors and empty successful responses do not establish absence of news. The collection document reports the actual single GDELT attempt as HTTP 429 after approximately 11.4 seconds, with no returned metadata and no racing-coverage or normal-latency inference. This review checked that wording, not the underlying probe receipt.
- Provider/publication clocks are not described as local receipt. Publication-to-receipt lag is expressly conditional on source-clock credibility and is not competitor latency. The old Betfair sample is retained for parser work, without joining current news to historical prices as a causal trial.
- Source-qualified IDs, uncertain entity linking, syndication, article rights and metadata-minimal retention are explicit. The current local store is distinguished from the proposed network adapters, production controls and future semantic bridge.

Remaining limitations are already disclosed, rather than newly discovered defects: racing-news coverage and true arrival latency are unmeasured; archive vintage/retention rights and some provider cadence/prices remain unresolved; no production collector or scheduled acquisition trial has been demonstrated by these four files. Module behavior and the GDELT probe evidence require their separate implementation/verification review.

Reviewed SHA-256 (rechecked unchanged before this handoff):

```text
e99cee14421664bdf39ae60e11d1cfc4562c98056bb8511573f4d86bdd92aa00  docs/data-provider-options.md
b30745f3c159c6ab54059c8b64690fa17dc0757ee1a6eb71729b588c85de049e  research/data-provider-options.json
cc8ae7ec2b2976e17be63afff0d442c2b1c0a38e9deb6b4b9d4a0f2cea360fbe  docs/collection-pipeline.md
9c63502a999bf6084de94f240f2ab196a255a8946c1354e30f0f65e811540fab  research/collection-plan.json
```
