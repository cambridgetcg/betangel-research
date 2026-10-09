# Hermes handoff: strategy-design

Session: `20261009_181607_6c19ce`. Requested Astra/ultra; installed route sends max. Completed with process result exit 0. Author: external Hermes Agent; coordinator saved returned final report verbatim below. Proposals are not independently verified backtests.

Tool limitation: the visible `web_extract` call failed because the configured native search backend does not extract URLs. This stream does not fully audit any provider-native search activity. Coordinator source checks and final decisions are recorded separately.

---

E01 offline experiment proposal — 9 October 2026

Status: proposed_not_validated. Design only; no local files independently inspected, code executed, or profits verified.

Observed constraint and selection

The supplied replay report says Advanced/Pro momentum, reversal and linkage rules failed to beat no-change. The eight selected Punchestown races from 29 April 2017 remain permanently inspected engineering data—not a holdout.

Select E01: volume may help distinguish continuation from reversal conditional on price history. Stock-market research motivates that interaction, but provides no horse-racing evidence. Volume is unsigned activity, not inferred buying pressure. E02 is a secondary rival-feature ablation, not an alternative winner if E01 fails. ([nber.org](https://www.nber.org/papers/w8312?utm_source=openai))

Universe and temporal contract

Use a new, permitted, consecutive population of Irish horse-racing WIN markets, one Advanced-format feed throughout. Its specification includes runner traded volume and one-second sampling; this documents fields, not predictive value. No package or country selection by results. ([historicdata.betfair.com](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf))

Let S be the first published scheduled start, observed before the lookback. Make one decision at t=S−120 seconds, using 180 seconds of history; label at t+60 seconds. Never substitute actual off-time.

Use backward snapshots with publisher pt at or before each cutoff. Identity and any separately supplied event/availability timestamps must also satisfy the cutoff. Publisher time remains an availability proxy, not measured receipt latency.

Define q_i(s) as inverse LTP normalized across the active field. Target: q_i(t+60)−q_i(t), a price-share change—not a winning probability.

Eligibility and withdrawals

At decision require complete native IDs, valid prices/volume, unchanged active field and uninterrupted open/pre-play status throughout lookback, no observed schedule revision, and message/LTP ages ≤90 seconds at feature endpoints. Audit image resets and volume corrections; missing volume is not zero, and negative counter differences invalidate that window.

Preserve every issued forecast. Later withdrawals, additions, suspension, in-play transition or invalid target snapshots censor the whole market’s label; never retrospectively remove individual runners. Report decision-cohort coverage, reasons by meeting/day and bounded missing-label sensitivity. Scored performance is conditional on observable labels, not all-race performance.

Features, models and bounded search

P: decision q, own q changes over 30/60/180 seconds, field size and LTP age.

V: log1p runner-volume increments over those windows, zero-volume flags, and matching price-change × log-volume interactions.

R: 60-second share/volume changes of the strongest rival at decision, selected by q with stable-ID tie-breaking. Normalized q already contains field information; E02 tests incremental rival information.

Fit race-weighted, standardized, mean-loss ridge regression to the target; add predictions to decision q and project onto the probability simplex. Compare P, P+V and P+V+R using identical folds and penalty grid {0.1,1,10}. E02 removes R and refits. No additional features, horizons, models or subgroup searches.

Baselines: fitted P, no-change, and projected q(t)+(60/180)[q(t)−q(t−180)] momentum. Evaluate identical cohorts.

Chronological fitting and calibration

Use early new dates for replay audits and precision calibration, then expanding training blocks with subsequent calendar-block validation for penalty selection. Learn preprocessing only from training data. ([scikit-learn.org](https://scikit-learn.org/stable/common_pitfalls.html))

Keep meetings/days intact. Purge training labels reaching subsequent validation feature windows; add an audit-derived availability gap. Freeze exact dates, folds, preprocessing, coefficients and exclusions before opening the final later test. Refit on pre-test data once; no updating during test.

Log every configuration and failure. A trial cap does not erase earlier searching; no holdout-driven redesign or E02 substitution. ([davidhbailey.com](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf))

Evaluation and gates

Primary metric: MAE, equally weighted by race and then runner. Estimate paired baseline-minus-E01 improvements using stationary bootstrap blocks of consecutive Europe/Dublin calendar days, retaining all meetings. Freeze block length using pilot dependence; weak-dependence/stationarity assumptions remain limitations. Report leave-one-meeting/day-out results. ([tandfonline.com](https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476870?utm_source=openai))

Before candidate comparisons, the coordinator sets a positive research-usefulness fraction r. Define δ as the larger of r×pilot no-change MAE and the measured reconstruction-uncertainty floor. Pilot paired day-block variance determines a fixed test duration targeting interval half-width ≤δ/2. Freeze δ, coverage tolerances and dates before holdout access; insufficient feasible precision means stop, not invent a sample requirement.

Use Bonferroni-adjusted simultaneous intervals across baseline contrasts. Advance only if every lower bound exceeds δ, all leave-one-group-out improvements stay positive, and coverage/missingness checks pass. An upper bound ≤δ rejects useful improvement for this specification; overlapping bounds mean inconclusive. Leakage or unreconciled replay errors invalidate evaluation. Any further confirmation needs newly registered untouched dates.

Later economic gate

A predictive pass permits only a separately locked offline execution experiment: quote depth, spread, receipt/order delays, conservative queue assumptions, partial/missed fills, impact, withdrawals, fixed sizing/exits and applicable historical fees. Commission is assessed on positive net market winnings, with other charges potentially applicable. No fee rate is assumed here. LTP-derived profit is prohibited; simulated gains would not establish live profitability. ([support.betfair.com](https://support.betfair.com/app/answers/detail/betfair-charges))

JSON proposal

    {
      "status": "proposed_not_validated",
      "primary_hypothesis": "E01 volume improves 60-second price-share forecasts beyond price-only and fixed baselines",
      "features": {
        "primary": ["P", "V"],
        "declared_ablation": "E02: P+V+R versus refitted P+V"
      },
      "baselines": ["price_only_ridge", "no_change", "scaled_momentum"],
      "temporal_contract": {
        "anchor": "first_published_start",
        "decision_offset_s": -120,
        "lookback_s": 180,
        "horizon_s": 60,
        "availability": "publisher_pt_proxy"
      },
      "evaluation": {
        "metric": "race_balanced_MAE",
        "split": "chronological_tuning_then_single_locked_test",
        "uncertainty": "day_block_bootstrap_with_meetings_intact",
        "penalties": [0.1, 1, 10],
        "minimum_improvement": null
      },
      "required_missing_inputs": [
        "permitted_consecutive_corpus_and_currency_manifest",
        "validated_volume_replay",
        "exact_calendar_boundaries",
        "pilot_variance_and_block_length",
        "usefulness_margin_and_coverage_limits",
        "receipt_evidence_and_historical_execution_cost_inputs"
      ],
      "implementation_steps": [
        "extend_replay_outside_preserved_pilot",
        "test_images_counters_withdrawals_and_future_data_invariance",
        "calibrate_and_register",
        "fit_and_freeze",
        "evaluate_once_and_report_all_gates"
      ]
    }

Sources

All accessed 9 October 2026. Betfair is the commercial operator/data seller; academic and documentation sources do not validate this experiment.

Llorente et al., May 2001; daily stocks, not racing:
https://www.nber.org/papers/w8312

Betfair feed specification, undated:
https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf

Scikit-learn leakage guidance, undated:
https://scikit-learn.org/stable/common_pitfalls.html

Politis–Romano, 1994:
https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476870

Bailey et al., 27 February 2015; includes investment-industry affiliation:
https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf

Betfair charges, undated; current terms are not historical fee evidence:
https://support.betfair.com/app/answers/detail/betfair-charges
