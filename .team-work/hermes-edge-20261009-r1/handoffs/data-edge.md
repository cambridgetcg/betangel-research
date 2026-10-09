# Hermes handoff: data-edge

Session: `20261009_181607_21379d`. Requested Astra/ultra; installed route sends max. Completed with process result exit 0. Author: external Hermes Agent; coordinator saved returned final report verbatim below. Proposals are not independently verified backtests.

Tool limitation: the visible `web_extract` call failed because the configured native search backend does not extract URLs. This stream does not fully audit any provider-native search activity. Coordinator source checks and final decisions are recorded separately.

---

Data and linkage decision — 9 October 2026

Recommendation: advance E01, testing whether displayed depth and recent matched volume improve a fixed-horizon price forecast beyond current quotes and price history. Defer cross-provider form integration.

Evidence status: web-documentation review plus the supplied packet; no independent local execution, authenticated data retrieval, new backtest or profit verification. The eight inspected Irish races remain engineering data, not a holdout.

1. Price, volume and order-book information

Mechanism hypothesis: liquidity imbalance and changing matched activity may precede repricing. Counterhypothesis: current quotes already incorporate this information; apparent prediction merely anticipates stale LTP updates.

Availability: Betfair documents ADV at one-second intervals with best offers and traded information; BASIC lacks depth/volume, while PRO includes full ladders. ADV is therefore the minimum documented package suitable for this experiment—not evidence that purchasing it improves forecasts. [S1] ([historicdata.betfair.com](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf))

The current support page says history begins April 2015; the undated specification says May 2015. Exact UK coverage must be checked against a file inventory rather than inferred from either statement. [S1–S2] ([support.developer.betfair.com](https://support.developer.betfair.com/hc/en-us/articles/360002407732-What-data-is-provided-by-the-Historical-Data-service))

Keys: marketId, selectionId and publisher pt. The packet establishes actual sample fields and free sample access, but only for Punchestown, 29 April 2017. Wider historical downloads require acquired store entitlements and authentication; no current price was verified. [S3] ([support.developer.betfair.com](https://support.developer.betfair.com/hc/en-us/articles/12859956891932-How-Can-I-Make-HTTP-Requests-to-the-Historical-Data-API))

Confounders: spread, odds level, quote age, conflation, withdrawals and liquidity regime. Aggregate ladder changes do not identify individual cancellations or queue priority.

Minimal ablation: identical price/quote baseline versus that baseline plus best-level size imbalance and trailing matched-volume change.

2. Cross-runner relations

Mechanism hypothesis: rivals’ repricing or liquidity changes lead the focal runner.

Counterhypothesis: the relationship is mechanical. Betfair’s documented cross-matching mechanism uses offers across selections; normalized price shares also share a denominator. Apparent linkage need not reveal additional information. [S4] ([betfair-developer-docs.atlassian.net](https://betfair-developer-docs.atlassian.net/wiki/pages/viewpage.action?pageId=2686993&pageVersion=3))

Availability/status: reuse the same ADV/PRO markets, history and access conditions; no second provider is necessary. Join rivals by marketId at a common pt cutoff, preserving selectionId and the then-known active field.

Confounders: asynchronous observations, common news, removals, normalization and cross-matching status.

Minimal ablation: remove all rival-history features and refit with identical folds, current-field controls and tuning budget. Diagnose normalization-only improvements using an unnormalized runner-price target as a secondary check. E02 should follow E01, not become another primary selection opportunity.

3. Prior-performance and context links

Mechanism hypothesis: prior course/distance/going suitability adds outcome information beyond current price. Counterhypothesis: it is already priced, or gains arise from revised ratings and selective linkage.

Documented availability: The Racing API offers historical racecards from 23 January 2023 on Pro—not 2017. Its results endpoint normally covers the preceding year; Pro’s paid historical add-on extends queries to 1 January 2005. Public documentation contains response examples; authenticated historical payloads were not tested. Sectional coverage was not established. [S5] ([api.theracingapi.com](https://api.theracingapi.com/documentation?utm_source=openai))

Important distinction: the vendor claims pre-race ratings preserve point-in-time information, but its 4 July 2026 changelog also describes recalculating the historical ratings catalogue and finalizing non-handicap performance figures after 90 days using subsequent evidence. Original publication timestamps and archived rating vintages remain unverified here. Do not substitute settled ratings for earlier observables. [S6] ([theracingapi.com](https://www.theracingapi.com/tools/ratings))

Keys: vendor race_id, horse_id and course_id exist; profiles include birth/pedigree information. No Betfair-to-provider identifier contract was established. [S5] ([api.theracingapi.com](https://api.theracingapi.com/documentation?utm_source=openai))

Required linkage: validate race venue/date/time and runner identity using name, country suffix and birth/pedigree corroboration; preserve ambiguity rather than force matches.

Other confounders: revised going, changing distances, missing histories and trainer/horse-quality effects. Minimal ablation: market-only calibration versus the same model plus one frozen prior-performance group, assessed with race-level log loss.

The packet’s verified join is different: 112 settled rows matched using CSV EVENT_ID/SELECTION_ID against the stripped stream market ID/runner ID. That is same-provider SP linkage, not a horse-history crosswalk. Stream eventId identifies the meeting here; 14 removed runners also require separate accounting.

4. Primary experiment specification

Retain the framework’s 180-second lookback, decision 120 seconds before the initially published scheduled start, and 60-second horizon.

Extend replay to reconstruct ADV quotes. Primary target: normalized inverse quote-midpoint share, using the midpoint of best back/lay decimal odds. This avoids making stale LTP the sole target.

Compare:

1. No-change and momentum benchmarks.
2. A price-only fitted baseline including current quotes, spread and own-price history.
3. The identical model plus best-level size imbalance and trailing 30-second matched-volume change.

Keep one population, horizon, model class and tuning budget. Use equal-race MAE, chronological development blocks, a sealed later block and day/meeting-block uncertainty. Register the minimum useful improvement before scoring.

Treat publisher-time availability as an assumption, not measured receipt latency. Artificial delays are sensitivity simulations. Any improvement advances forecasting research only; it establishes neither fills nor net returns.

5. Acquisition checklist—no purchase

1. Register a consecutive UK venue/date population; verify jurisdiction rather than substituting the Irish samples.
2. Establish permitted ADV inventory, retention/reuse rights and completeness before requesting data. Current authorization permits neither account access nor purchase.
3. Require manifest fields: source URL, retrieval time, package, market/date coverage, checksum, terms and missing-file reasons. Keep raw data outside Git.
4. Validate images, zero prices, ladder deletions, equal-pt ordering, suspensions, withdrawals and scheduled-time changes before scoring.
5. Keep the inspected samples exclusively for engineering. Seal the new final block before inspecting outcomes.
6. For later E03 work, require a timestamped historical racecard example, rating-version evidence and an audited crosswalk first. No scraping around restrictions.

No permitted, consecutive UK ADV dataset was established in this review. Acquisition remains the blocking dependency.

Source register

All accessed 9 October 2026. Betfair and The Racing API have commercial interests; their documentation establishes advertised capabilities, not independent profitability.

S1 — Betfair specification; undated.
https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf

S2 — Historical coverage; updated 24 September 2026.
https://support.developer.betfair.com/hc/en-us/articles/360002407732-What-data-is-provided-by-the-Historical-Data-service

S3 — Historical API access; updated 24 September 2026.
https://support.developer.betfair.com/hc/en-us/articles/12859956891932-How-Can-I-Make-HTTP-Requests-to-the-Historical-Data-API

S4 — Cross-matching explanation; archived version dated 29 October 2020.
https://betfair-developer-docs.atlassian.net/wiki/pages/viewpage.action?pageId=2686993&pageVersion=3

S5 — API documentation; v1.4.4, June 2026.
https://api.theracingapi.com/documentation

S6 — Ratings methodology; changelog through 4 July 2026.
https://www.theracingapi.com/tools/ratings
