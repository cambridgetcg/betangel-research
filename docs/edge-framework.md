# Turning data, speed and connections into testable claims

Proposed framework, **9 October 2026**. No hypothesis below has passed a real-data test. [Strategy cards](../research/strategy-cards.json) hold the proposed experiments; their exact dates, thresholds and budgets still need registration.

**多數據、分析快、識串連，可以係研究優勢；要證明交易 edge，就要證明佢提供市場價格以外、當時用得到、扣成本後仍然有用嘅資訊。** More observations can also mean more correlated ticks and more chances to discover a coincidence. Faster research saves our time; faster decisions matter only if useful information survives the complete delay.

## Four falsifiable candidates

| Card | Proposed mechanism | Comparison that could disprove it |
| --- | --- | --- |
| **E01: Price and volume** | Recent order-book/volume changes contain information about the next fixed-horizon price change. | Does the extra data improve on no-change, simple momentum and a price-only model on later races? The market may already incorporate it. |
| **E02: Connections between runners** | Competitors' movements add information beyond the runner's own history. | Remove the other-runner feature group and refit with the same tuning budget. Apparent improvement may be a mechanical consequence of probabilities moving together. |
| **E03: Earlier performance** | Earlier sectionals, pace and context improve race-outcome probabilities beyond current prices. | Compare market-only calibration with the same model plus earlier-performance features. The information may already be priced, or apparent gains may come from later revisions. |
| **E04: Speed that survives delay** | A frozen predictive increment remains useful with realistic data age and computation time. | Delay inputs and measure score decay against a baseline available at the same decision time. An effect that exists only with unavailable information is unusable. |

Choose **one primary card** after the data audit. E02 extends E01; E04 stress-tests an existing signal. These are not four independent opportunities to select whichever test looks best.

## What was actually knowable?

Preserve entity IDs, source/version, event time, publication/availability time, retrieval time and decision time. A historical download's retrieval timestamp is not evidence of historical delivery speed. Record genuine receipt times when available; label reconstructed availability assumptions explicitly.

Join each feature to a decision only when its event **and availability** precede the relevant cutoff; use a backward lookup with a declared maximum age. Never substitute a later revision because its original race date is old. [Feast documents backward point-in-time retrieval](https://docs.feast.dev/getting-started/concepts/point-in-time-joins); our additional availability rule is necessary where publication and event time differ. [BHA explains that past performance figures can change](https://www.britishhorseracing.com/regulation/adjusting-handicap-rating/).

Missing availability evidence means exclude that feature from a strict historical claim or label the result conditional. Keep the runner set known at the decision, including subsequently withdrawn runners; register how such cases are scored. Define decisions from a schedule/status observable then, not retrospectively from the actual off. Never forward-fill through suspensions. Retain missingness and exclusion counts.

## Baselines, ablations and validation

For price movement, use no-change and simple momentum with the identical horizon, population and price definition. For outcomes, normalize inverse pre-decision odds within the then-known field and compare with market-only calibration fitted on training data. These are benchmarks, not assertions that odds are true probabilities. Uniform probabilities are a sanity baseline, not sufficient competition.

Use race-level log loss as the primary outcome score; report Brier score and calibration too. Price targets need a declared tick/probability scale and primary error metric. [Proper scoring rules](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf) assess probability forecasts; a high winner-picking rate alone does not measure probability quality or profit.

Split by calendar blocks and keep whole races together. Fit preprocessing, imputation, calibration and feature selection only inside each training fold. Use chronological inner validation to select variants, then one later locked test. Purge training labels whose outcome windows overlap the test and allow the declared availability gap. [Scikit-learn's leakage guidance](https://scikit-learn.org/stable/common_pitfalls.html) supports training-only transformations; its [time-series split documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) warns about future-to-past evaluation. Irregular race times require calendar/group logic rather than blindly splitting rows.

Refit with one feature group removed, holding folds, model class and tuning budget constant. Test data quantity with a predeclared learning curve on the same later evaluation period. Report uncertainty by day/meeting blocks, not independent ticks. Group ablation matters because [correlated features can mislead individual permutation importance](https://scikit-learn.org/stable/modules/permutation_importance.html). These checks support predictive attribution, not causal proof.

## Reject, remain uncertain, or advance

1. **Reject an invalid experiment:** temporal leakage, unreconciled IDs or unreported selection changes invalidate its result.
2. **Reject this variant or remain uncertain:** no held-out improvement rejects the proposed advantage for this specification; wide intervals mean insufficient evidence, not proof of no possible edge.
3. **Advance only to further research:** preregister a minimum useful score improvement, uncertainty method and multiplicity policy. Require its lower confidence bound to exceed that threshold on the locked test, survive planned ablations and show no dependence on one exceptional meeting. Do not choose thresholds after seeing test results.
4. **Economic evidence is a separate gate:** later simulations must include spread, delays, queues, partial/missed fills, market impact, withdrawals and all applicable fees. A price touch is not an order fill. The [Betfair simulation paper](https://arxiv.org/html/2510.16008v1) explicitly describes queue and impact limitations. Net simulated gains still do not establish live profits.

Log every tested feature set, horizon, population and model, including failures. Repeated test-set peeking spends its independence. [Bailey et al. (2015)](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) explain why strategy selection among many backtests creates overfitting; a single attractive holdout does not erase earlier searches. If several claims are tested, predeclare a family-wise correction or a separate future confirmation period.

## Local v1 and what comes later

V1 scope is stream-delta replay, pre-off last-traded-price snapshots, fixed no-change/momentum/mean-reversion/linkage forecasts, injected data-delay scenarios, market-balanced scores, chronological-fold checks and point-in-time join contracts. Public samples from one 2017 meeting and synthetic fixtures check this pipeline; their scores are exploratory engineering results, not an untouched holdout or strategy validation. Insufficient dates must be reported as no holdout. Last-traded prices cannot establish execution P&L. The existing two-race pilot remains descriptive.

Later work needs consecutive historical data, frozen experiment specifications, trained models, group ablations, blocked uncertainty estimates and realistic execution assumptions. No fixed sample count guarantees statistical power. Measure input age and computation p50/p95 separately; replay cannot measure network or exchange acceptance latency. No VPS benchmark, expected ROI or live orders are implied.
