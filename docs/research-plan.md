# Research plan and economic validation

As of **2026-10-09**. Completed: public evidence review and import of the prior descriptive pilot. Not completed: historical exchange-data acquisition, model training, prospective forecasts or any profitability test.

## 1. Audit data before buying infrastructure

Use the Mac to inspect permitted historical samples and their actual fields. Record provider, licence/reuse terms, retrieval date, timestamps, market/selection IDs and missingness. The [official download guide](https://support.developer.betfair.com/hc/en-us/articles/360000402211-How-do-I-download-and-view-Betfair-Historical-Data) documents a separate historical download service; this repo has not bought or downloaded those files.

Choose a narrow, consecutive population of UK horse races before viewing outcome patterns. Record race type, distance, field size, scheduled/actual start and suspensions. For every data item distinguish event time, publication time and receipt time. Report missing races, withdrawals and exclusions rather than silently dropping them.

Slower price studies can begin with a data-availability audit. Claims about executable profit additionally require bid/ask prices, sufficient volume and an explicit execution model. Start with a small format sample before committing to a paid data range.

## 2. Register one primary hypothesis

[Candidate hypotheses](../research/hypotheses.csv) are proposals, not completed registrations or validated findings. After the data audit, freeze the selected population, dates, features, baselines, target and metric in a dated experiment document **before** inspecting the held-out period.

- **Price movement:** Does pre-off price/volume information improve a fixed-horizon forecast beyond no-change and simple momentum baselines? Later study simulated execution separately.
- **Outcome calibration:** Do earlier-race performance features improve forecasts beyond normalized pre-race market prices? Preserve rating-at-race and historical publication availability. Closing SP is a later comparator, not an earlier input.
- **Execution sensitivity:** How much of an apparent scalp result disappears under conservative queue, delay and partial-fill assumptions? This requires suitable detailed data and is a later research track.

Fit preprocessing and choose features using training data only. Split chronologically by whole races, keeping all runners from a race together. Reserve a later untouched period; avoid nearby overlapping windows crossing partitions. Account for meetings, recurring horses and correlated markets when reporting uncertainty.

## 3. Report forecasting and economic results separately

For outcome forecasts: race-level log loss, Brier score and calibration against the market baseline. For price forecasts: directional/no-change comparisons and error in a declared price/tick scale. A predictive improvement does not by itself imply realizable profit.

For any simulated economic result, state order timing, spread, stake/liability convention, liquidity, matching assumptions, delay, partial fills, withdrawals, reductions, suspensions and exits. Label it simulated; preserve both missed opportunities and adverse cases. Stress assumptions and report the cases where an apparent edge disappears.

Report calendar span, independent races/days, total trials, model variants tried, gross and net P&L, drawdown and uncertainty. A chosen sample-size target is not a statistical guarantee. Prefer day/meeting blocks where appropriate to treating every tick as independent. A later timestamped forecast record would test persistence without reusing the same historical outcomes.

## 4. Count costs correctly

Betfair normally charges commission on positive **net market** winnings, not on every winning order and not simply on aggregate profit across markets. The UK Basic package example is 2%; the user's actual package and market rules determine the applicable rate. Expert Fees and high-usage transaction charges can also apply under their conditions. They are not a universal flat surcharge. [Current official charges](https://www.betfair.com/www/GBR/en/aboutUs/Betfair.Charges/).

For an ordinary market with gross settled P&L `g_m` and applicable commission rate `c_m`:

```text
net_after_ordinary_commission = sum(g_m - c_m * max(g_m, 0))
net_after_all_costs = net_after_ordinary_commission
                      - applicable_additional_exchange_fees
                      - software - hosting - data - model_API_costs
```

If `g_m` comes from assumed execution, slippage/missed fills must already be reflected, or separately charged once; do not double-count them. An actual export may already deduct commission, so reconcile its semantics first.

Arithmetic example: one market +£10 and another −£10, each at 2% ordinary commission, total −£0.20 after commission. Deducting 2% from the combined zero would be wrong. Also distinguish return on stakes from return on capital: repeated reuse of the same bank makes them different quantities. Lay stakes and lay liability are different denominators.

## 5. Hosting economics, if subsequently needed

Current public prices checked 9 October 2026:

| Item | Monthly plan |
| --- | --- |
| [Bet Angel Recommended Windows VPS](https://www.betangel.com/vps/) | £39.99 including VAT and Windows licence; 4 vCPU, 8GB, 80GB. Software licence separate; minimum one month and automatic renewal. |
| [Bet Angel Professional](https://www.betangel.com/buy/) | £29.99 for one month. Annual option £149.99 paid upfront. |
| Both monthly plans | **£69.98/month**, before data/model costs and applicable exchange fees. |

Illustration only: if a future strategy actually earned £0.20 per market after variable costs, 350 markets would merely cover £69.98 of monthly fixed cost. This is **not** a forecast of returns or recommended volume. Current measured per-market advantage is unknown. Research on the existing Mac does not require this VPS purchase.

## Decision points

1. **Data feasible?** If required timestamps or fields cannot be established, narrow the question or stop that candidate.
2. **Forecast increment survives holdout?** If not, record the negative result; do not relabel the test set as training and reuse it as fresh evidence.
3. **Economic result survives costs and execution stress?** If not, the result is not evidence of a usable edge.
4. **Evidence strong enough to justify further work?** Discuss new data or infrastructure only against the measured result. No success rate or income estimate can currently be assigned.
