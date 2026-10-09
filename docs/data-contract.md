# Data and experiment contracts

## Identity and time

An external observation has `source`, `source_entity_id`, `feature`, numeric `value`, `event_time` (measurement time) and `available_at` (when this version became available). An identity mapping has `source`, `source_entity_id`, internal `entity_id` and `known_at`. Do not substitute a name match or a later mapping without recording when it became known.

`betangel_research.temporal.join_asof(entity_id, decision_time, observations, links, delay_seconds=0, max_age_seconds=None)` only admits measurements, versions and mappings available by the delayed decision cutoff. It chooses the latest measurement, then the latest available revision of that measurement. Conflicting mappings at the same version or conflicting feature versions raise errors, independent of input order. Features are source-qualified and return their timestamps for inspection.

All timestamps must be timezone-aware ISO strings. The contract supports millisecond precision and rejects finer timestamps instead of rounding future information backward. Measurement time cannot follow availability time. `max_age_seconds` applies to measurement age, not the age of a later revision. These checks cannot prove that a provider's declared timestamp is truthful; the acquisition manifest and provenance still matter.

This generic join is unit-tested. The current exchange replay uses the provider's native market/selection IDs directly; it does not yet join a cross-provider horse-history database. The SP matching exercise is a format audit only.

## Historical stream replay

`python3 -m betangel_research replay --input PATH --config PATH --out NEW_DIRECTORY` accepts one uncompressed JSONL or `.bz2` stream. Input market-change publish timestamps must be nondecreasing. It rejects segmented messages. Multiple independent archives must not be naively concatenated into an unordered stream; multi-file chronological orchestration remains later work.

V1 reconstructs **last traded price and market metadata only**. It does not reconstruct bid/ask ladders, traded volume, orders or fills. Missing deltas preserve prior values; zero/nonfinite odds are unavailable; market images reset prices. Suspensions/closure invalidate carried prices. On reopening, fresh LTP observations are required. The first published scheduled start anchors cutoffs; revisions before a used snapshot disqualify that market. Actual off-time is never substituted retrospectively.

Publisher `pt` is the historical availability proxy. No genuine local receipt timestamps are present in these samples. The delay scenarios are assumptions about later receipt, not measurements of an execution advantage.

Each snapshot contains only updates at or before its cutoff. The sample configuration uses a 180-second lookback, decision two minutes before the initially published start, and label one minute later. All needed snapshots must be open and pre-play, with matching active runner sets, complete valid prices, and a market message within the declared 90-second age limit. LTP itself may be older; its age is separately reported. Future label eligibility can select the sample, so every exclusion is recorded. Delays are compared on their common eligible cohort.

## Predictions and scores

At each cutoff, normalize inverse LTP across the active field to form a **price-share index**. This is not an assertion about true winning probabilities. The four fixed forecast levels are:

1. Last observed share unchanged.
2. Linear continuation of its lookback change.
3. Equal-sized reversal of that change.
4. Continuation adjusted by minus 0.5 times the strongest observed rival's change.

The rival is selected from the observed snapshot only; ties use stable IDs. Forecasts are clipped and normalized. Coefficients and horizons are fixed, not tuned on these scores. Even the own-runner normalized series already depends on the field, so E02 is an incremental rival-feature experiment, not a test of complete runner independence. The three sample tiers contain the same races and must not be pooled as independent trials.

For scoring, both predicted target share and actual target share have the undelayed decision share subtracted. This reference cancels in MAE. It is **not an input to the forecasting function**. Delayed directional accuracy is suppressed because the sign relative to this unavailable reference would not be a usable delayed direction prediction. Zero-delay accuracy uses down/flat/up with ±1e-12 counted flat; it is not a betting win rate.

Primary metric: equal weight per market, then equal weight per runner within each market. Reports also show unbalanced per-runner MAE, eligible races/dates, per-day scores and exclusions. The date-fold helper constructs expanding past-day blocks; it does not fit models, purge arbitrary overlapping labels, or certify out-of-sample results. One date produces no folds. Proper outcome-probability scoring, fitted models and blocked uncertainty are future components.

## Economics, provenance and repeatability

The replay produces **no P&L or ROI**. `settled_market_costs` is a separate arithmetic helper for already established gross market results in one currency; it applies ordinary commission only to positive net market totals and subtracts supplied fixed/additional costs. It neither creates fills nor calculates special fee eligibility. Passing already-net exports would double-count commission.

Every run records raw-file/config/code SHA-256, Python version, Git revision/dirty state, exclusions and all declared variants. Output directories cannot be overwritten. An append-only local JSONL ledger records successful runs; failed commands currently return an error and must also be noted in research records. The ledger is not a tamper-proof registry or a substitute for preregistration.

Raw inputs, runner-level outputs and the local ledger are Git-ignored. Committed reports contain aggregate engineering results and provenance references. Their code hashes identify the exact implementation even if reports were generated before a commit. Inspection permanently makes these samples exploratory; new holdout claims require untouched later data.
