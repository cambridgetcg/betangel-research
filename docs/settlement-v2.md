# Settlement rule v2 (research only)

Status: **proposed: needs Astra's agreement (Yu's decision 2026-10-31).** Until Astra agrees, c2 D11 option (a) stands, and no study reports a result that rests on this rule. Nothing has run on real data.

For Yu, Nuance, Astra and the lab operator. Author: lab-Claude, 2026-10-31, against betangel-research main dda38a8.

- The code is `betangel_research/betfair/backtest/settlement_v2.py`. Its tests are in `tests/test_betfair_backtest_settlement_v2.py`: synthetic market definitions run through the real ingest, index, replay, cohort and outcome. Two new coverage-audit counts are tested in `tests/test_betfair_backtest_archive.py`.
- *Inference* marks reasoning that was not measured.

## Why, and what it overrides

On 2026-10-31 Yu answered "yes" to: "Should we agree a research-only settlement rule with Astra, so win-or-lose tests can run on old races?" Today `outcome.py` never infers "not void" (`betangel_research/betfair/backtest/outcome.py:17-19`; c2 D11 option (a) with Q5, [backtest plan](backtest-plan.md) line 1370). So every bet on BASIC history is unresolved (`void_unknown`), and no win-or-lose test can run.

The rule overrides that one inference, and only for a research study that opts in:

- The study's own registration names the token `not_void_when_published_settlement_complete`. Its code calls `settlement_v2.market_outcome(record, decision, void_rule=...)` with that token, and any other value is refused.
- P0 and every backtest contract are unchanged. `outcome.market_outcome` still sets `market_void` to None. Schema 1 still allows only `unresolved` in `settlement`, so a contract naming the token fails with `settlement.market_void:not_allowed`, and `AccountingRules` still refuses any other rule. No module imports `settlement_v2`; a test pins this, so neither the runner nor the certification can reach it. A study that opts in adds itself to that test, under review.
- Contract p1 is unchanged, and so are its ledger, its N and its verdict. Pilot 1 settles from live books under its own rules (`research/pilots/p1-fav-t5.json`), and `pilot.py` does not import the rule.

## The rule

The rule sets `market_void` to False when all of these hold on the `MarketOutcome` that `outcome.market_outcome` builds from the market's primary Historical Data member:

1. The market is CLOSED at the end. This is the index's `final_status`, so a market reopened after CLOSED fails.
2. The last `bspReconciled` is true.
3. At least one runner is listed at the end, and each listed runner's final status is WINNER, LOSER, REMOVED, REMOVED_VACANT or HIDDEN. ACTIVE, PLACED, null, a missing status and any other text fail.
4. The number of WINNERs equals the declared `numberOfWinners`, which must be an integer of at least 1.
5. The two guards accounting already applies hold: the member was read whole, and no result was revised after CLOSED.

An unknown value never meets a condition. When any condition fails, `market_void` stays None (`void_unknown`), as today. The rule never sets void to True, because the files have no void flag. It never overrides positive void evidence, and it adds or removes no other reason. So:

- A dead heat in a WIN market (two WINNERs against 1) stays unresolved, with reasons `void_unknown` and `dead_heat`.
- A WIN market that declares 2 and has two WINNERs meets the rule, but it is still `not_single_winner_market`.
- A bet on a HIDDEN, removed or unpriced runner of a complete market stays unresolved for its own reason.
- HIDDEN counts as final because accounting already treats it as non-winning (`accounting.py:122-124`).
- `bspReconciled` turning true after CLOSED is no revision. The index's `results_revised_after_close` looks only at runner status, bsp, listing and `numberOfWinners`, and the rule reads the last `bspReconciled`.

Results that rest on the rule are labelled "settled as published in the file, research only".

## Are the fields in the files?

| Field | BASIC | ADVANCED and PRO |
|---|---|---|
| `md.status` | in BASIC's definition list (plan line 57) | every 2017 sample market reached CLOSED in each tier (plan lines 120, 122); its final status is not stated |
| `md.bspReconciled` | in BASIC's definition list (plan line 57) | *Inference* only, from `MARKET_DEF` being in every tier (plan lines 33-35). No repository line shows it in an ADVANCED or PRO file. |
| `md.numberOfWinners` | in BASIC's definition list (plan line 57) | read in the 21 EACH_WAY markets across the three 2017 samples (plan line 121); not stated for WIN |
| `md.runners.status` | statuses seen in the BASIC sample (plan line 52) | no unknown status in the three samples (plan line 121) |

Plan line 121's "no definition field outside the allowlist" and "no unknown runner status" show that nothing unexpected appeared in the samples, not that any field was present. None of this covers the real 2015-05..2016-04 corpus. So before any study uses the rule, `settlement_v2.coverage_check(report, tier)` must pass on the coverage audit of that study's own data root. Each of `md.status`, `md.bspReconciled`, `md.numberOfWinners` and `md.runners.status` must be present in every market of that tier and market type, and the audit must carry the new counts. Any problem stops the study until a person has explained it in the registration. A market whose primary holds no record of it, for example, shows no field at all.

Nothing in code ties `coverage_check` to `market_outcome(..., void_rule=...)`: the gate is procedural. So a study that adds itself to `test_no_module_imports_the_rule` also shows its `coverage_check` result in its registration, and its review checks that.

## What it cannot see

- **A void race that looks settled** (CLOSED, reconciled, exactly one WINNER) would count as not void. *Inference:* a race voided before settlement would more likely show no WINNER, which condition 4 catches. The repository has no evidence either way, so every result states this assumption.
- **What the audit sizes.** Two new integer counts in the coverage audit, beside `dead_heats`, count markets CLOSED at the end (as accounting reads it) that the rule rejects: `closed_no_winner` (no runner listed at the end is WINNER) and `closed_bsp_not_reconciled` (the last `bspReconciled` is not true). *Inference:* they are an indirect sign of how common voids may be, not a bound on void races that look settled, which no count can see. If `closed_no_winner` is large, voids may be common, and the residual risk needs more care.
- **Rejections no count isolates.** A market that reached CLOSED and was then reopened or reset fails condition 1. The audit counts it in `closed_dead_heat_not_assessable`, because the index judges no dead heat for a market not CLOSED at the end (`market_index._dead_heat`). That count also holds markets whose `numberOfWinners` is missing or below 1, or whose type the dead-heat rule does not cover, and does not tell them apart. A CLOSED market with a runner listed at the end ACTIVE, PLACED or null fails condition 3. It is in no count unless that runner also lacks a bsp (`markets_missing_final_bsp`); `markets_with_unrecognised_runner_status` counts only status text the index does not know. `tests/test_betfair_backtest_archive.py` pins both.
- **Late resettlement** after the file ends cannot be seen. `results_revised_after_close` covers only changes inside the file.
- **It is an inference about Betfair's settlement**, not Betfair's settlement, and no evidence that the race was run.

## Reporting apart, and the sensitivity check

Every unresolved bet is reported apart, in counts and stakes, and never mixed into the resolved figures. Bets are grouped by their full `reasons` tuple, not only the first reason. `void_unknown` is on every bet in a market the rule did not resolve, so it says only that the rule failed; the other reasons say why.

| Group | Accounting reasons |
|---|---|
| dead heat | `dead_heat` |
| no winner | `no_winner_in_result` |
| not CLOSED or not reconciled | `market_not_closed`, `closed_unknown`, `bsp_not_reconciled`, `bsp_reconciled_unknown` |
| runner not final or not listed | `other_runner_status_unexpected`, `own_runner_not_listed`, `own_status_unknown`, `own_status_unexpected` |
| not single-winner or unjudgeable | `not_single_winner_market`, `dead_heat_unknown` |
| removed after the decision | `other_runner_removed_after_decision`, `own_runner_removed_after_decision`, `removals_unknown` |
| revised or incomplete | `result_revised_after_close`, `revision_unknown`, `outcome_incomplete`, `outcome_completeness_unknown` |
| BSP unusable | `bsp_missing`, `bsp_out_of_range` |
| market partly unresolved | `market_partly_unresolved` |
| other | a bet with no reason above (for example `market_void`, which this rule never sets, or `void_unknown` alone, as on P0's path) |

A bet can be in several groups. For each group, `only_here` counts the bets whose every reason except `void_unknown` is in that group. `settlement_v2.sensitivity(settlement, variant)` gives, per variant:

- **Primary:** the resolved ROI, N_R / S_R, with every unresolved bet dropped.
- **Worst case:** every unresolved bet counted lost, (N_R − S_U) / (S_R + S_U). This is the existing `roi_worst_case_unresolved_lost` (plan §8).
- **Per group:** the worst case with that group's `only_here` bets dropped instead of counted lost.
- **`worst_case_turns_negative`:** the worst case turns a positive resolved ROI negative. Such a result is reported as fragile.

Settling dead heats by a dead-heat rule is not built. The repository cites no source for Betfair's dead-heat rule on BSP backs, so that needs a cited source and Astra's agreement first. Which way dropping dead heats biases a result is unknown, since it removes both part-wins and losses. This is the lab's reading of Yu's "a sensitivity check that drops them", and Yu or Astra should confirm it.

## What it changes elsewhere

- **The coverage audit** (`coverage_audit.py`) gains the two counts above. They are integer counts like the rest, and the report stays at schema 1, as when earlier counts were added (95906c5).
- **The engine certificate key changes.** Every `.py` file in the backtest package is in the certificate's source descriptor (`certificate.py:16-17`; plan §9). Adding `settlement_v2.py` (21 → 22 files) and editing `coverage_audit.py` change the descriptor digest, and so the key. After merge, the lab's certificate (CERT_KEY `579ac61d…`, plan line 532) no longer matches, and any later `run-backtest` needs `certify-engine` again. No other source file changes, no certified case reads either file, and P0's contract sha256 is unchanged.
- **Accounting's settling path meets real data for the first time.** P0 is the only backtest contract, and under it every bet on real data is unresolved, because `market_void` is never False (plan line 1370). So the parts of `accounting.py` that settle a bet won or lost and compute its amount have run only on synthetic data: the tests, the engineering known-answer cases (`engineering.py` `KAT_CASES`, whose docstring says `market_void` False appears only there) and the certification's synthetic worlds (`certification_fixtures.py`). Under this rule, choices that never decided a real bet become live:
  - HIDDEN on another listed runner counts as non-winning (`accounting.py:122-124`).
  - A runner ACTIVE at the decision that ends HIDDEN is not removed after the decision. Only REMOVED and REMOVED_VACANT are (`outcome.py:49`, `:118`).
  - A LOSER needs a bsp, or the bet stays unresolved (`accounting.py:54`).
  - A win is rounded down to the penny (`accounting.py:112`).

  These are c2's accounting choices, unchanged here. Choice 2 below covers HIDDEN only for the rule's own condition 3.
- PR #50 also adds two backtest modules. Each PR edits the same descriptor-count line in `tests/test_betfair_backtest_boundaries.py`, so whichever merges second sets it to 24.

## Choices for Yu or Astra

1. The two guards (read whole; no revision after CLOSED) are part of the inference.
2. HIDDEN counts as a final status.
3. The rule uses equality (WINNER count = `numberOfWinners`), not "at least".
4. The reading of the sensitivity check. Dead heats settled by rule are not built.
5. The strictness of the coverage gate: every market of the tier must show each field.
6. Whether accounting's choices that become live (HIDDEN, removal, a LOSER's bsp, rounding; see "What it changes elsewhere") stand for a study.
7. The token name `not_void_when_published_settlement_complete`.
8. What is not built here:
   - a schema 2 contract token and certified known-answer cases for backtest contracts;
   - an accounting change for a study whose entry price is not BSP, such as the movement study's K follow-up priced at P. Accounting settles basis BSP only (`accounting.py:109`).
