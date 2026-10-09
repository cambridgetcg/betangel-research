# ADV microstructure sampler handoff

Writer: `/root/betangel_profit_evidence`; completed 2026-10-09. Owned only the two new Python files below and this handoff. No commits, v1 stream/evaluation/config changes, accounts, orders, labels or profitability calculations.

## Interface and semantics

`sample_features(path, decision_before_start_s=120, lookback_s=30, max_snapshot_age_s=10)` returns JSON-serializable `records` and `audit`. Required keys plus `previous_observed_ms` are present. Additional per-side quote observation times distinguish carried-forward quotes from fresh market messages.

- WIN markets only, market type known by decision; country is recorded but unfiltered. Excluded market types remain in the audit.
- Initial published `marketTime` fixes both cutoffs. Include all updates exactly at each cutoff. Future schedule revisions, removals, PRO fields and outcomes cannot veto a frozen decision snapshot.
- ADV `batb`/`batl` are keyed by levels 0–2. A level-zero replacement discards its old price, and a zero-size deletion does not infer promotion from cached level one.
- Images clear definition, runners, quotes and cumulative volume. OPEN/preplay is required; suspension/in-play clears quotes, including quotes accompanying suspension. Active-field changes during the interval invalidate surviving runners. Removed runners are not emitted as active records.
- `volume_delta` uses only each runner's cumulative `tv`, never market volume or a moving price-key `trd` bucket. Missing endpoints, any observed decrease/invalid volume, image reset, status break or field change within the window make the delta undefined. Recovery above the original total does not hide an intervening decrease.
- Both endpoint market messages must be at most 10 seconds old by default. Missing lookback records remain ineligible; unobserved decision cutoffs and markets without a known active field remain in `audit.market_exclusions`.
- BASIC/unknown, PRO/mixed, virtual and handicap field evidence is unsupported and reported. Invalid ADV triples and unordered publisher time raise `ValueError`. Detection is based on fields observed by decision, not authenticated package identity.

## Checks actually completed

`python3 -m unittest tests.test_microstructure -v`: **21 tests passed**, including changing level-zero prices, zero deletion, runner-versus-market/price-bucket volume, decreased-then-recovered volume, invalid/missing/null fields, image resets, suspension/reopen, in-play, removals, schedule changes, future-data invariance, separate endpoint staleness, missing decisions, unsupported formats, crossed books, malformed levels and timestamp ordering.

Real local ADV public sample engineering call, with default settings: **71,764 messages, 34 source markets, 8 WIN decision markets, 113 records, 113 feature-eligible records, 26 non-WIN market exclusions**. All records were IE. JSON serialization with `allow_nan=False` passed; every current/prior publisher timestamp respected its corresponding cutoff. This is one already inspected 2017 meeting, not an untouched holdout or proof of edge. A preliminary run before the requested WIN scope filter returned 455 records across all 34 markets; those preliminary counts are superseded by the final WIN-scoped check.

## Exact code hashes (SHA-256)

- `betangel_research/microstructure.py`: `5a171a5ac17a239253071c5a1173a024b08fc2e373ea64d36ffc1ae85f46d4c5`
- `tests/test_microstructure.py`: `76d41dc808ed16f332365e5e6936541e2fe2307caf2465e75a63cd92513eefcb`
- Input `data/raw/betfair-samples/horse-racing-adv-sample.bz2`: `fd80f8625bf17cb8d9eb1a0797aefc50d0b9b679777c8dd525f866ccc8c1766f`

## Primary semantics sources

Accessed 2026-10-09, public read-only browsing:

1. [Betfair Historical Data Feed Specification](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf), four-page official PDF, no document date shown. Defines publisher `pt`, replacement images, cumulative runner `tv`, ADV level-keyed triples and PRO price-keyed ladders. Supports the parser contract; it does not validate our implementation.
2. [Advanced Historical Data — How do I interpret updates?](https://support.developer.betfair.com/hc/en-us/articles/360018468438-Advanced-Historical-Data-How-do-I-interpret-updates), Neil T, displayed update **2026-09-24 13:35**. Worked ADV examples establish level zero as best, levels 0–2, and replacement of a conflicting old top quote without a separate old-price cancellation.
3. [Why does traded volume decrease as well as increase?](https://support.developer.betfair.com/hc/en-us/articles/360006560978-Why-does-traded-volume-decrease-as-well-as-increase), Neil T, displayed update **2026-09-24 13:35**. Explains that currency-rate changes can alter `trd` amounts, including decreases. This directly concerns `trd`; it motivates caution about treating volume as economic trade flow and does not prove every `tv` decrease has that cause.

## Limits and review focus

This is a conservative ADV feature sampler, **not the E01 validator**, a full book validator, execution simulator or live strategy. `eligible` means measurement requirements passed; it is not a trade recommendation or evidence of profitability. Missing packets, hidden counter resets, feed completeness, exchange-side queue priority, virtual liquidity, receipt latency and fills are not reconstructed. An old unchanged quote can remain valid in a delta feed; its true observation time is returned, while the default age gate applies to market messages. Cumulative unsigned volume is an activity proxy, not buy/sell aggressor classification or proof that size was executed at today's best price. Malformed post-decision runner data is not interpreted once a market snapshot is frozen; global timestamp order is still checked by the existing reader.

Parent owns strategy runner integration and final independent review. No broad repository suite was run by this writer; parent can run the combined suite after integration.
