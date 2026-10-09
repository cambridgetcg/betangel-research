# Motif prototype engineering results

**9 October 2026.** Implemented 14 forum observations → nine motifs → three offline policies. [Design and all 14 mappings](../docs/motif-strategies.md); [machine-readable catalogue](../research/motif-catalog.json); [exact run summaries](motif-prototypes-2026-10-09.json). The output establishes engineering behavior, not forecasting improvement, causal trader behavior or profitability.

## What runs

- **P01 — continuation with activity corroboration:** a prior midpoint move plus positive, valid unsigned runner matched-volume increment; explicit quote, spread, time and continuity gates. It emits a direction hypothesis or abstains. Aggregate volume does not identify an aggressor or intent.
- **P02 — public attention watch:** a timely tip or confirmed prior-race finish explicitly linked to the target market. One market-level watch, with `odds_direction=null`. More activity need not imply a predictable direction.
- **P03 — context abstention:** the identical P01 decision, retained only when supplied contextual assessment is compatible within a named source scope. Missing, uncertain, contradictory, late or expired context makes it abstain. The engine does not call an LLM; current assessments are synthetic fixtures.

The [fixed config](../research/experiments/motif-prototypes-v1.json) contains illustrative engineering thresholds, not fitted or validated trading parameters. All decisions preserve reasons and evidence. Frozen-packet processing delays are 0/2/10/30 seconds; processing delay never grants access to later context. Effective expiry accounts for signal lifetime, scheduled start, market-message age and applicable context limits.

## Actual demonstrations

| Dataset, zero injected delay | P01 runner hypotheses | P02 market watches | P03 runner hypotheses |
| --- | ---: | ---: | ---: |
| Nine constructed scenarios | 7 / 9 | 2 / 9 | 1 / 9 |
| Official ADV sample, eight WIN markets | 19 / 113 | 0 / 8 | 0 / 113 |

All other rows are retained as abstentions. These are **policy output counts, not correct predictions or winning bets**. The synthetic scenarios deliberately exercise compatible, contradictory, uncertain, late and absent context, no-trade quote movement, wide spreads, tips and prior-race finishes. No real outcomes are attached.

The real run read 71,764 messages / 34 source markets; 26 non-WIN markets were audited as outside scope. All 113 WIN feature records passed this sampler's measurement requirements. The sample is Punchestown, Ireland, 29 April 2017, already inspected, one meeting and one date. The 113-record count includes a runner still active at the decision but later removed; future removal does not veto this feature record. It is not 113 independent races or settled bets. [Earlier denominator audit](sample-replay-2026-10-09.md) explains the broader historical sample.

No real timestamped external context was supplied, so P02 and P03 correctly abstained throughout the ADV demonstration. Two-second injected delay produced the same counts in both datasets. At 10 and 30 seconds all policies abstained because the declared age/lifetime gates had expired. That is a deterministic policy check, **not an estimate of real market edge decay**.

The final runs took about 0.03s (synthetic) and 12.95s (ADV) locally. These whole-file batch times are not per-event decision latency, an optimized benchmark or evidence we can beat other participants to information. ADV `pt` is publisher time; actual historical receipt latency remains unknown.

## Measurement and verification

**60 unit/integration tests passed** with `python3 -m unittest discover -s tests -v`: 22 existing tests, 21 new sampler tests and 17 motif tests. The added tests cover level-zero replacement, zero deletion, volume identity and counter corrections, image/status/field continuity, as-of snapshots, unsupported formats, future-data invariance, input validation, contextual availability, identity/coverage validation, effective expiry and output protection.

The ADV parser uses level-keyed `batb`/`batl` and runner cumulative `tv`. It never subtracts traded amounts at two different moving best prices. Both sampler endpoint market messages must be at most 10s old; P01 requires issue-time market-message age strictly below 10s. **An individual carried quote or volume observation can be older**; its timestamp is separately returned. Missing packets, hidden resets, receipt latency, queue position, fills and complete feed continuity are not established. See [official ADV semantics](https://support.developer.betfair.com/hc/en-us/articles/360018468438-Advanced-Historical-Data-How-do-I-interpret-updates) and the [feed specification](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf).

A separate native reviewer inspected code, tests, contracts and source fidelity. Five timing/input/expiry findings were fixed and regression-tested; final verdict was READY within the declared fixed configuration and offline scope. The reviewer did not execute the test suite or replays. Existing E01 proposal, strategy cards, v1 stream/evaluation files and original archive were left unchanged.

Actual Hermes/Astra design critique was attempted with requested `ultra` (this route maps to `max`). It ended with exit 1 after about 395s: the provider reported five consecutive no-response attempts. **No Hermes research output was produced this round.** No model switch was made. Runtime metadata and hashes are in the [batch handoff](../.team-work/motif-prototypes-20261009/handoff.json); costs remain unknown. The native review described above completed separately.

## What would establish an edge

Data collection could add coverage and earlier reliable linkage; processing could make a frozen decision available sooner; contextual reasoning could identify stale/repeated news or alternative explanations. None of those increments has been measured here. More sources, faster batch execution and a persuasive explanation do not by themselves establish an advantage.

Next validation must define a consecutive multi-date corpus and prospective context log, freeze targets and thresholds, then compare: price-only versus price-plus-activity; P01 versus P03 on the same full cohort; mechanical coverage/availability gates versus semantic assessments; and equal-latency controls. Report abstentions and a predeclared fallback alongside retained-subset scores. P02 needs full event/non-event coverage with time-to-off and pre-event activity controls.

A modern LLM may already know outcomes of old races even when only old documents are supplied. Historical semantic explanations therefore remain retrospective engineering; a stronger test logs real source receipt, model/prompt versions, analysis completion, evidence and decisions prospectively before results arrive. Numerical scoring, independent holdout validation, costs and any later execution model remain separate work. No ROI is estimated.
