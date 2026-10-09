# Independent Hermes review

Session: `20261009_182206_eee9a6`. Requested Astra/ultra; installed route sends max. Fresh reviewer, packet-only review; no independent shell checks or new external source retrieval claimed. Final result exit 0.

---

Verdict: CHANGES NEEDED — for the proposed offline forecasting design. Execution remains blocked by data availability, replay implementation and calibration.

This is a packet-only review; file contents, hashes and execution results are supplied, not independently checked. Existing replay is engineering evidence, never holdout profit evidence. Package variants reuse the same races [A,C,R].

Priority findings

1. P0 — Fix now: resolve incompatible experiments [D,S].
The handoffs disagree on UK versus Ireland, quote-midpoint versus LTP targets, and depth-plus-volume versus multiscale volume/interactions. These are substantive differences, not interchangeable implementations. Adopt the single contract below. Include current quotes, spread and quote ages in the baseline so added activity features are not merely explaining stale LTP. A joint depth/volume test cannot attribute improvement separately to either component.

2. P0 — Fix now: distinguish selection, missingness and undefined targets [C,S].
Decision eligibility must use only information available at issuance. Preserve every issued forecast and record subsequent label invalidation. A withdrawal changes the original same-field target’s domain; it is not simply an unobserved quote. Separate undefined targets from well-defined but unavailable labels. Apply feasible-simplex missing-label bounds only to the latter. Report coverage and exclusion reasons against the full decision cohort, by day/meeting. Results remain conditional on label eligibility.

3. P0 — Fix now; calibrate later: make inference executable [S].
Dates, folds, usefulness fraction, dependence blocks, coverage limits and resampling budget remain unset. Use separate earlier calibration dates. Set the usefulness fraction before candidate comparisons; derive the research margin from calibration no-change MAE. Drop the undefined “reconstruction-uncertainty floor”: parser defects invalidate evaluation rather than justify a larger margin.
Specify 95% familywise Bonferroni intervals across the three frozen baseline contrasts, with calendar-day blocks retaining meetings. Freeze seed, resample count and block length. Precision planning is not a guarantee of achieved precision. Treat leave-group-out results as diagnostics, not additional significance tests or permission to discard adverse groups.

4. P0 — Future evidence required: corpus and implementation [C,D,S].
Neither handoff establishes a permitted consecutive corpus. Require inventory, rights, currency, checksums and missing-file accounting. C describes LTP/metadata replay, not validated quote/volume reconstruction or fitted-model evaluation.
Before fitting, validate exact field mappings, both quote sides/sizes, image replacement, ladder deletion, equal-pt ordering, volume-counter semantics/corrections, suspensions and future-data invariance. Missing volume is not zero; unchanged quotes are not automatically stale. The generic temporal join proves neither book reconstruction nor timestamp truthfulness.

5. P1 — Fix source claims now [D,R].
Coordinator verification supports advertised fields, access restrictions and mechanical cross-runner linkage—not predictive value. The Racing API endpoint/plan/date assertions remain unconfirmed; revised ratings and same-provider SP matching do not establish point-in-time horse-history feasibility. Defer E02/E03, with no fallback promotion if E01 fails.
Reconcile the 113 snapshot observations, 112 settled rows and reported 14 removals by identity, status and cutoff; these need not share a denominator. Receipt evidence and historical fees belong to a later execution study, not the present forecasting feasibility gate.

Corrected experiment contract

Population:
Consecutive Irish horse-racing WIN markets, all venues fixed from an outcome-blind inventory, one ADV feed and recorded currency. Ireland is a scope choice, not evidence of superiority. If unavailable, stop and re-register rather than switch countries/packages after scoring. Exclude inspected samples from validation.

Chronology:
S is the first published scheduled start, known before the lookback. Decision t=S−120 seconds; lookback 180 seconds; horizon 60 seconds. Use backward publisher-pt snapshots, explicitly an availability proxy. Require unchanged field and uninterrupted open/pre-play status through decision; reject schedule revisions known by decision. Never re-anchor issued forecasts retrospectively. Later field/status breaks invalidate labels without erasing forecasts.

Target:
For each runner, m=(best back odds+best lay odds)/2 and q=(1/m)/sum(1/m) across the active field. Predict q(t+60)−q(t). Require complete, valid, noncrossed two-sided states; freeze continuity/freshness rules before testing. This is a synthetic price-share target, not an executable quote or calibrated winning probability.

Features:
P contains q, m, relative spread, own q changes over 30/60/180 seconds, field size and both quote-change ages.
L adds only best-level size imbalance, (back size−lay size)/(back size+lay size), and log1p of the trailing 30-second cumulative runner matched-volume increment. Define sides explicitly; volume is unsigned. Missing or unreconciled negative increments invalidate the decision window.

Models and comparisons:
Race-weighted mean-loss ridge, training-only weighted standardization, penalties {0.1,1,10}. Compare P with P+L; select penalties using projected validation MAE. Add predicted change to current q and project onto the simplex. Additional baselines are unchanged q and projected q+(60/180)[q−q(t−180)]. Use identical cohorts.

Evaluation:
Equal weight per market, then per runner. Freeze expanding chronological folds, purging overlapping label/feature intervals. Only two fitted feature groups and the stated penalty grid; one final refit and one sealed later test. No rival/form rescue, subgroup search or interim test scoring. Advance forecasting research only if all simultaneous improvement lower bounds exceed the frozen margin and coverage/sensitivity gates pass. Otherwise distinguish failure, inconclusive precision and invalid evaluation. No result establishes a trading edge.

Remaining acceptance checklist

Fix now:
Written contract, feature semantics, trial ledger, censoring rules, inference/pass rules, and reconciliation of the brief’s pending status [B].

Requires future evidence:
Permitted corpus; trusted quote/volume reconstruction fixtures; chronology/leakage tests; calibrated dates, margin, blocks and coverage limits; frozen code/configuration/data hashes; controlled access to untouched test values.

No accounts, purchases, configuration changes, execution integration or preserved-pilot edits are authorized.

Artifact key — supplied SHA-256 identifiers, not recomputed

A — AGENTS.md
f221db036b49b968c74a2106a700ea2cba346bcf941355dc991d0534793b0a7e

B — .team-work/hermes-edge-20261009-r1/brief.md
a3cbde6a5a1cb9bd0f8cd454fe3860b7e1589ea6fc0a218a2d0971c93f5dc6be

C — docs/data-contract.md
a837ee073f2c6551a0411219d10f19d82ea14121dbe8f70cfd7039b6a9e0ce72

R — reports/sample-replay-2026-10-09.md
ebbf0e3264d7f490495fcd526f72b62df67e8efb0cb768f8c2e853dd3379c2ec

D — .team-work/hermes-edge-20261009-r1/handoffs/data-edge.md
7aba35902558ecef96c62e61c98aff0189b8eb7a591399da177073563575aa04

S — .team-work/hermes-edge-20261009-r1/handoffs/strategy-design.md
912f68ce98aa9a2364ce4a951ce2e0cccd3e16a61e9066241f1af48dab8d0194
