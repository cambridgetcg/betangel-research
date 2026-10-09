# First real-format replay: engineering result

**9 October 2026. No strategy has been promoted and no ROI is estimated.**

All three publisher samples describe the same eight WIN markets at Punchestown, Ireland, on 29 April 2017. Each replay retained 113 runner observations at the decision/label cutoffs on one date. A later settled archive has 112 runners: eligibility here uses the then-known field, not the final starter list. Across packages these are eight races, not 24 independent races.

The fixed configuration observes a 180-second lookback, decides 120 seconds before the initially published scheduled start, and forecasts a price-share index 60 seconds ahead. It also injects 2, 10 and 30 seconds of data delay. The four forecast rules and every delay are reported; no best rule is selected for deployment. Raw and per-run outputs remain local/ignored. [Aggregate JSON](sample-replay-2026-10-09.json) includes hashes and all scores.

## Zero-delay comparison

MAE below is equal-weight-per-market absolute error in normalized inverse-LTP share, multiplied by 100 (percentage-share points). **Lower is better.** These are price-index errors, not returns or calibrated winning-probability errors.

| Package | No change | Momentum | Mean reversion | Rival linkage | Replay seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| BASIC | 0.6100 | 0.7025 | 0.5926 | 0.7629 | 0.12 |
| ADV | 0.4059 | 0.5563 | 0.4297 | 0.6098 | 2.98 |
| PRO | 0.4296 | 0.5527 | 0.4569 | 0.5883 | 6.56 |

In Advanced and Pro, none of the three additional fixed signals beats the no-change baseline on this sample. Basic gives a slightly different ordering. Sampling resolution changes observed targets as well as features, so comparing package error magnitudes does not establish the causal value of buying richer data. The relative ranking within each package is exploratory too.

Runtime is one local run including parsing/scoring/provenance work, not a controlled benchmark or measured network/exchange latency. Python standard library only; no fitted model or paid compute.

## Delayed Advanced forecasts

| Artificial delay | No change MAE | Momentum MAE | Mean reversion MAE | Rival linkage MAE |
| --- | ---: | ---: | ---: | ---: |
| 0s | 0.4059 | 0.5563 | 0.4297 | 0.6098 |
| 2s | 0.3977 | 0.5404 | 0.4349 | 0.5891 |
| 10s | 0.4568 | 0.5604 | 0.5274 | 0.6822 |
| 30s | 0.6224 | 0.8252 | 0.6394 | 0.9558 |

All delay comparisons use the same eligible market cohort. Delay effects need not be monotonic on a tiny sample; these observations do not measure the benefit of faster infrastructure. Delayed directional accuracy is deliberately omitted because its undelayed reference was unavailable to the delayed predictor. MAE remains valid as a target-level forecast error.

## What the run does and does not establish

- The parser processed 1,251 Basic, 71,764 Advanced and 407,927 Pro messages and correctly produced complete reports. In each run, 26 non-WIN markets were excluded by the declared population. Exact reasons/IDs and LTP ages are retained in JSON.
- The one available event date produces zero chronological folds. There is no unseen-date validation, uncertainty interval, feature-group ablation or fitted model.
- Prices are LTP, not executable quotes. There is no order-book fill simulation, economic backtest, commission-adjusted strategy return or claimed live speed advantage.
- Every sample and score is now inspected. Future tests must use new data and log all variants; this result cannot become a fresh holdout later.
- The data audit found identity/date/zero-price/image traps before strategy claims. Avoiding erroneous joins is necessary research quality, not evidence that the market is mispriced.

Next milestone: acquire a permitted consecutive multi-date population, freeze one primary hypothesis and baseline before its final period, and measure whether additional feature groups improve a later-date forecast. Add a realistic execution layer only if forecasting evidence survives.
