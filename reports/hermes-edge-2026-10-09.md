# Hermes / Astra research round

9 October 2026. This round develops an offline experiment; it does not establish a betting edge.

## Actual agent setup

Two external Hermes CLI researchers ran concurrently, followed by a fresh independent Hermes reviewer. The researchers received selected project evidence and web tools. Their completed reports are [data/edge research](../.team-work/hermes-edge-20261009-r1/handoffs/data-edge.md) and [strategy design](../.team-work/hermes-edge-20261009-r1/handoffs/strategy-design.md).

Every invocation requested `gpt-6-astra`, provider `openai-codex`, and `--reasoning ultra`. The installed Hermes route maps `ultra` to API `max`; it is not a separate wire-level ultra setting. [Official model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra) lists reasoning through max. No global settings were changed. Researchers had configured 480-second budgets; the reviewer had 360 seconds. [Runtime provenance](../.team-work/hermes-edge-20261009-r1/runtime.json) records sessions, commands, hashes and provider-reported usage, not a billing estimate.

Direct `web_extract` failed on the configured search-only backend. The coordinator separately checked key operator sources; [source checks](../.team-work/hermes-edge-20261009-r1/source-checks.md) distinguish successful checks from unavailable or unconfirmed material.

## Research direction

Both researchers selected **E01: incremental price/volume information**. The plausible mechanism is that participation and available liquidity sometimes add information beyond recent price changes. The counterhypothesis is that current prices already contain it, or an apparent improvement only predicts a stale last-traded price catching up.

The data researcher proposes quote depth and a quote-derived target; the strategy researcher proposes volume interactions and an LTP-derived target. They also differ on UK versus Irish markets. Those are substantive design choices requiring one declared contract, not interchangeable backtest variants to select by results.

Cross-runner E02 remains secondary: normalized shares and exchange cross-matching can create mechanical relationships. A linked-feature result must beat a matched baseline. Prior-performance E03 is deferred because historical availability, rating revisions and cross-provider identity links remain unverified.

## Scope of evidence

No new model fit, predictive backtest or execution simulation was run in this agent round. The [existing eight-race replay](sample-replay-2026-10-09.md) remains engineering evidence: the extra fixed rules failed to beat no-change on Advanced/Pro. It is not an untouched evaluation dataset. The immediate engineering dependency is validated quote/volume reconstruction; statistical evaluation additionally needs permitted consecutive data, locked dates and calibrated precision thresholds.

## Independent review and integrated decision

All three sessions completed with exit 0. The [independent reviewer](../.team-work/hermes-edge-20261009-r1/handoffs/independent-review.md) returned **CHANGES NEEDED** on the original proposals. The coordinator incorporated the design corrections below into [E01's machine-readable proposal](../research/experiments/e01-hermes-proposal.json). This is not a runnable replay config or completed preregistration; it has not received a second independent review.

| Item | Integrated decision |
| --- | --- |
| Population | One consecutive Irish WIN-market ADV corpus, all venues from an outcome-blind inventory. Ireland is a scope choice, not a claim of superior returns. No corpus has been acquired. |
| Timing | First published scheduled start minus 120 seconds; 180-second lookback; 60-second horizon. Publisher time is an explicit availability proxy. |
| Target | Normalize inverse best-back/best-lay decimal-odds midpoint across the active field; forecast its change. It is a synthetic price share. |
| Main comparison | Price/quote baseline versus the same model plus best-level size imbalance and 30-second matched-volume increment. The joint test cannot separate the two contributions. |
| Trial budget | Two ridge feature groups × three penalties per frozen chronological fold; no-change and momentum benchmarks; one sealed later evaluation. Defer E02/E03. |
| Missing labels | Preserve every forecast. Separate later field changes that make the target undefined from well-defined missing quotes; report all cohort losses. Unchanged quotes do not automatically mean stale data. |
| Statistical gate | Three paired baseline contrasts, 95% familywise Bonferroni intervals with calendar-day blocks. Margin, block length, dates and coverage limits require earlier calibration. Parser defects invalidate evaluation; they are not an uncertainty allowance. |

The coordinator also reconciled the review's denominator question against existing files: 126 final WIN runner identities comprise 112 settled SP identities and 14 removals. The 113 replay identities contain those 112 settled runners plus Simons Castle, whose first published removal was **17:10:49.148 UTC**, after its **17:09 UTC** label. Thirteen other removed identities are absent from the replay snapshots. [Audit with IDs, times and input hashes](../.team-work/hermes-edge-20261009-r1/denominator-audit.json). This audit did not rerun the replay or improve its forecasting result.

The next engineering task is ADV quote/volume reconstruction, including level conflicts, image replacement, deletions and counter corrections. [Betfair's update documentation](https://support.developer.betfair.com/hc/en-us/articles/360018468438-Advanced-Historical-Data-How-do-I-interpret-updates) confirms old offers can disappear without explicit cancellation updates; the stream does not supply individual queue positions. A final evaluation still depends on permitted data and a fully frozen implementation/configuration.

## Verification of this round

Final artifact checks are recorded in the batch brief. No application code changed, and the existing 22-test suite was not rerun. This round adds agent research, an independently critiqued proposal, source checks and a targeted identity audit; it supplies no new ROI estimate.
