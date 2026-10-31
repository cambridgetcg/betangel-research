# Movement study v1: Pilot 1's question on 2015-16 history

Status: Registered measurement `movement-v1`, proposed for review. Nothing has run on real data. Yu decided on 2026-10-31 to reverse the 2026-10-16 deferral of the price-movement diagnostic ([backtest plan §13](backtest-plan.md#13-open-questions)), so that this study runs before Pilot 1's first live bet.

For Yu, Nuance, Astra and the lab operator. Author: lab-Claude, 2026-10-31, against betangel-research main dda38a8, revised the same day after review.

- "The registration" means [research/studies/movement-v1.json](../research/studies/movement-v1.json). It holds every rule that decides anything; this page explains them and does not override them.
- "Contract p1" means [research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json); "pilot.py" means `betangel_research/betfair/pilot.py`.
- The code is `betangel_research/betfair/backtest/movement.py` (pure computation) and `movement_study.py` (the runner), tested by `tests/test_backtest_movement.py` on synthetic archives only.
- *Inference* marks arithmetic or reasoning that was not measured.

## What it measures, and why now

- Pilot 1 asks one question with real money: is the best back price five minutes before the off better than BSP after commission? This study asks the same question of history first, so the pilot starts with a prior and a measured spread, not an assumed one ([Pilot 1 plan](betfair-pilot-1-plan.md), the power illustration's SD).
- It rebuilds Pilot 1's rule exactly on the free 2015-05..2016-04 Betfair Historical Data files the lab holds (PRO carries full `atb`/`atl` ladders, ADVANCED `batb`/`batl`; both are supported). At off −30, −10, −5 and −2 minutes it makes Pilot 1's decision independently and prices the selected favourite's executable best back against its BSP, as NCLV = (1 + (P − 1)(1 − c)) / BSP − 1 with c = 2%, contract p1's formula.
- At the −5 minute decision it also asks whether the favourite's late move tells anything. M = ln(q(T−5) / q(T−30)), q being the favourite's field-normalised implied probability from back/lay midpoints. The cells are STEAM (M ≥ +0.05), FLAT and DRIFT (M ≤ −0.05).
- It decides nothing. Its registered pass tests exactly two hypotheses, the STEAM − FLAT and DRIFT − FLAT contrasts, under Holm; every other interval it prints is descriptive. It is not a backtest contract. It changes no existing source file, nor contract p1 (its N, its rule or its verdict), nor backtest-p0-bsp-v1. It does change the engine certificate's key (see [What it changes elsewhere](#what-it-changes-elsewhere)). P0's declared `ltp_to_bsp_movement_non_executable` stays declared and unbuilt: this study measures the executable best back that Pilot 1 would take, not LTP.

## How it rebuilds Pilot 1's decision

- Off is the first-published scheduled `marketTime`. The view at t holds every message published at or before t, the batch at t included. A market whose `marketTime` was revised before t is skipped and counted (`schedule_revised_before_decision`), because Pilot 1 would time its decision from the revised off.
- Every index record gets exactly one code at every decision time, in the registration's order. History needs seven gates first: a registered tier, a view, a registered month, an anchor known by t, a view still running at t, a readable member and an unrevised off. Then Pilot 1's predicates follow with pilot.py's codes and in its order: GB or IE, event type 7, WIN, ODDS, one winner, at least 5 runners ACTIVE and listed, then the selection. The selection is the only lowest best back (a tie is skipped), on the CLASSIC ladder, from 1.50 to 4.00, with at least £10 available at it, and a best lay 1 or 2 ticks above it.
- Quotes are folded by the same stream-cache rules that `betangel_research/betfair/book.py` documents (F54), and a test checks the two agree on seeded random sequences. PRO: a size of 0 deletes a price, and an empty list empties that side. ADVANCED: a size of 0 at level k empties level k and every deeper level, an empty list changes nothing, and the best price is level 0 of the contiguous prefix ordered by price. A null value changes nothing and is counted. The fold does not clear quotes while a market is suspended, as book.py does, because pilot.py has no status predicate; such decisions are counted instead (diagnostics).
- There is no `bspMarket` predicate. A market without BSP is issued and counted as `missing_bsp`, as Pilot 1 would count it. Market status and in-play are not predicates either, as in pilot.py.
- Each issued decision ends in one class: `outcome_unknown`, `void_own_runner_removed` (Pilot 1's void), `post_decision_removal`, `result_revised_after_close`, `missing_bsp` or `eligible`. Pilot 1 freezes BSP from the first read that shows the market CLOSED and ignores later corrections; these files give final values, so a market whose results changed after CLOSED is counted apart rather than measured. BSP is valid when the market ends CLOSED with `bspReconciled` true and the runner ends WINNER or LOSER with a BSP above 1. Only eligible bets enter NCLV.
- P is the best back at t, assumed filled in full at that price. So the study is the historical counterpart of contract p1's decision-price series, not of its fill-conditional primary.
- Each market is replayed from its index record's primary member with the index's own fold. Every view is checked against the index record (`replay.view_from_fold`), and the member's bytes against the manifest.
- One market is decided once. A record of a registered tier is keyed by its market id alone: the same market held as PRO and as ADVANCED under one data root refuses the run (`market_in_two_tiers`). Keep each tier in its own data root, as P0 does (backtest plan §10.1: the PRO archive is in `historic-pro`).

## The late move and its controls

- Population: eligible −5 minute bets only. A market drops out of it (and only it) for the first of four reasons. `window_unknown`: the member cannot be read through the window. `removal_in_window`: a runner was removed in (T−30, off]. `suspension_in_window`: the status was anything but OPEN in that window. `cell_undefined`: M cannot be computed, either because the favourite's spread is more than 3 ticks at either time, or because another ACTIVE runner lacks a two-sided quote. A runner added in the window is not an exclusion (the lead's rule names removals and suspensions only), though it changes the field q is normalised over. *Inference:* a removal after T−5 already makes the bet `post_decision_removal`, so in practice the removal exclusion bites only in (T−30, T−5]; a suspension anywhere in (T−30, off] still excludes, and a market turning in play is normally suspended first. `window_unknown` cannot arise for an eligible bet, since a member not read whole leaves the outcome unknown; it stays as a guard.
- Contrasts: STEAM − FLAT and DRIFT − FLAT in mean NCLV, two-sided, CR1 clustered by race date with t(G − 1), with Holm's correction over the two at a familywise 0.05. The registration writes the variance out. A contrast is tested only when each of its cells holds at least 200 bets on at least 15 race dates. These two contrasts are the only tests; the mean NCLV intervals by decision time, band and cell are unadjusted and descriptive.
- Negative controls, each giving the same two contrasts:
  - an earlier window: the cells from T−55 to T−30 instead, over the bets whose earlier cell is defined;
  - shuffled M: the exact expectation of each contrast over every permutation of the cells among the bets of each race date. No seed decides it; it is what the mean of ever more seeded within-date shuffles approaches. It shows how much of a contrast the dates' mix of cells and NCLV levels alone explains; *Inference (the final review's algebra, not measured):* it also picks up about 1/n_d of a real STEAM effect, where n_d is the number of late-move bets on a race date, because dates with more STEAM bets have higher date means. At about 8 bets a date that is an eighth of the effect, well under the half-effect kill threshold;
  - a seeded random non-favourite runner (seed 2, candidates in selection-id order) in place of the favourite, over the markets that have one.
- Kill rule: each control is compared with the main contrast over the same bets. It blocks a flag when it, or that main contrast, could not be computed, or when it is at least half that main contrast in absolute value *and* its own two-sided p is below 0.05 (or undefined). A control as large as that but within its own noise does not block.
- A STEAM or DRIFT cell gets `tradable_flag` only when four things hold: its mean NCLV is at least +0.5%, its contrast with FLAT is positive, that contrast survives Holm, and no control blocks it. The flag is for people to read, never a decision.
- The controls are specificity checks, not pure nulls. If moves are autocorrelated, the earlier window will echo a real late effect; if steamers of any runner beat BSP, the random runner will echo it. Either would block a real favourite-specific effect by design, and the tables show which.
- *Inference, from a synthetic simulation (scratch, not in the repository; 200 runs per setting).* The setup was 330 race dates × 8 markets, favourite NCLV SD 4%, a real STEAM effect of +1.7 pp and controls with no effect. Comparing point estimates alone, the controls blocked the flag by noise in 21% of runs (outsider NCLV SD 15%) and 57% (SD 30%), almost all through the random runner. Under the registered rule it was 6% and 3%, about the per-control 0.05, and the exact shuffled-M control never blocked. Under a smaller effect (+1.0 pp, outsider SD 10%) it went from 30% to 5%. The review's own run, with one seeded shuffle, also saw that shuffle alone block 11% of runs at a main t of about 3.8; the exact expectation removes that. Expect a flag to be blocked by noise in roughly one run in twenty per noisy control. The flip side: the same significance requirement means a noisy control, above all the random runner with outsider NCLV SDs of 15–30%, also rarely detects a real confound. A control that does not block is weak evidence of specificity, not proof of it.

## Two passes

1. **Blind pass.** It releases:
   - counts by code and class at every decision time;
   - records, issued and eligible counts by registered month, so a missing month shows as 0;
   - issued and eligible counts by decision time × price band (1.50–1.99, 2.00–2.48, 2.50–2.98, 3.00–4.00) × field size (5–7, 8–10, 11–14, 15+);
   - the diagnostics: decisions on books not OPEN or in play, and favourites whose best back size is under £2;
   - late-move, cell-by-price-band and control counts;
   - nearest-rank quantiles of ticks P − BSP by decision time and price band, *each minus its group's median*, which keep spread and shape but not location;
   - the SD of NCLV by decision time, band, field size and late-move cell.

   It releases no mean, median or other location of NCLV or of ticks, and no contrast, control effect or flag. With the counts, the SDs of all bets and of a set of bands give the between-band dispersion of the band means exactly (law of total variance), but not where any mean lies. The registration lists this.
2. **Registered pass.** It runs once, after people have read the blind tables, at the same commit and backtest sources, with the blind receipt of the same registration and the same input. It runs whatever the blind tables show. Only an engineering failure stops it (a refusal or failure code, counts that do not reconcile, an input missing a registered month, a defect in the code or input), and any stop is recorded with its reason. It adds the tick quantiles as they are, and mean NCLV with its CR1 interval for every group the blind pass gives an SD for. It also adds the contrasts with Holm, each control's contrasts beside the main contrasts over the same bets, the kill rule and the flags.

Both passes refuse unless the registration file's sha256 equals the one given on the command line, which the lead pins from the merged commit. Any edit to the registration is a new sha256. A group with fewer than 100 bets reports counts only, so no released quantile is a single market's minimum or maximum. Nothing is merged: no group is pooled with another after the blind pass.

## Predictions, signed before any data

These are this study's three stories for the T−5 mean NCLV, not an edit to the Pilot 1 plan. *Inference*, from the formula: at P = BSP, NCLV = −c(BSP − 1)/BSP. That is −0.67% at 1.50, −1.00% at 2.00, −1.20% at 2.50, −1.33% at 3.00 and −1.50% at 4.00. Half a one-tick spread costs about another 0.4% at 2.50, and the half-spread jumps at 2.00 and 3.00 where the tick widens.

- (a) **Late recreational money shortens favourites.** BSP ends below the T−5 best back, so T−5 NCLV is above the cost line (NCLV > 0: the price beats BSP after commission).
- (b) **Late money is informed and BSP is fair.** The T−5 *midpoint* is BSP plus noise, so NCLV is about −1% to −2%: half the spread plus commission.
- (c) **Late money drifts favourites.** BSP ends above the T−5 midpoint, so NCLV is below about −2%.

A result between 0 and about −1% fits none of the three cleanly, and the page will say so rather than pick one. The boundaries are not registered, so these are readings for people, not tests. Because the cost line and the half-spread depend on P, the late-move contrasts can carry a price-level effect when the cells' band mixes differ; the blind pass releases the cells' counts by price band so that is visible before any mean is read.

## What it cannot show

- **A prior, never proof.** These months count as inspected from P0's first run (backtest plan §3), so every figure is discovery material. Confirming anything needs data recorded after this registration.
- **Today's market.** 2015–16 depth was likely greater, and costs likely lower, than today's (the 2026-10-31 review's reading of a Racing Post article of 18 December 2024, not re-read here; the review recorded no URL or access date, so this is unverified). A historical NCLV would then likely overstate a current one. The study also applies today's 2% Basic rate to 2015–16 prices, not the commission then in force.
- **Fills, queue position and latency.** A full fill at the best back with zero delay is assumed. Pilot 1 measures fills.
- **BSP calibration.** NCLV assumes 1/BSP is a calibrated win probability, as contract p1 does. This study does not test it.
- **Void markets.** The files have no market-level void flag. A void race can sit in the eligible population; only the selected runner's removal is seen.
- **Ladder, currency and virtual prices.** CLASSIC is assumed, because index v1 does not record `priceLadderDefinition`. Every price is checked against the CLASSIC ladder instead. Sizes are read as GBP. Pilot 1 requests `EX_BEST_OFFERS` without `virtualise` and without `exBestOffersOverrides` (api.py). That this gives the non-virtual book, like these files' ladders, is the Betting API reference's statement, not checked here. If Betfair's default rollup at the minimum stake applies to that book (not checked here), a top level below the minimum stake is rolled up live but seen raw in these files. History could then say `depth_below_min`, or pick a different favourite, where live Pilot 1 would not. The blind pass counts favourites whose best back size is under £2 (the review's figure for the 2015–16 minimum stake, not verified), so the size of this risk is visible.
- **Suspended and in-play books.** The fold carries quotes through a suspension, and decisions are issued on suspended or in-play books, as pilot.py has no status predicate. The blind pass counts them at every decision time.
- **A single registered pass.** The code enforces the order of the passes on one machine, and the same code in both, not that the registered pass runs only once. Running it once is the operator's rule.

## Operator steps (on the lab, after the PR merges)

1. Check out the merged commit in a clean clone (`$CLEAN_CLONE`, outside `~/betangel-data`) and note it: `git rev-parse HEAD` (40 characters). The lead pins the registration hash from that commit: `shasum -a 256 research/studies/movement-v1.json`.
2. The study reads the data root's own `index/`, built by `archive-ingest` and `archive-index` (backtest plan §4.1). Use one tier's root (the PRO root is `historic-pro`). If the PRO archive is not indexed yet, that is a separate reviewed lab step that writes only under the data root. It is not part of this study.
3. Make a dedicated, private parent for this study's outputs, beside the run marker and never under it: `mkdir -m 700 -p ~/.local/state/lab-runs/movement-v1`. `~/.local/state/lab-runs/ACTIVE` is the run marker itself, a file the run wrapper writes and removes (betangel-suite DESIGN §6.8), so nothing may live under it. The parent is not a P0 lab-state root either: P0's is fixed at `~/betangel-data/lab-state` (backtest plan, runbook step 1), and `scripts/lab_runbook.py` `run_state` refuses any entry there but `certifications/`.
   Before each pass, write the marker (`: > ~/.local/state/lab-runs/ACTIVE`), so no suite engine or real-data command starts during the run; remove it when the pass ends, whatever its result. If a suite engine is installed and running, it takes `run/realdata.lock` first (DESIGN §6.8).
4. Run the blind pass into a new directory there. It must not exist yet, and neither may `<out>.partial`. Outside every Git worktree and every licensed-data tree is enforced:

   ```sh
   cd "$CLEAN_CLONE" && PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.movement_study --pass blind \
     --registration-sha256 "$REGISTRATION_SHA256" --archive-root "$PRO_DATA_ROOT" \
     --out ~/.local/state/lab-runs/movement-v1/blind --code-commit "$CODE_COMMIT"
   ```

   It prints one JSON line of counts, codes and hashes. It writes `summary.json`, `summary.md` and `receipt.json` into `<out>.partial` (mode 700, files 600) and renames that to `--out` only when all three are whole. The receipt names the commit, the registration sha256, the input sha256 (manifests, index heads and markets files) and the sha256 of the backtest sources on disk.
5. People read the blind `summary.md`, starting with the coverage by month. Nothing in the registration or the code may change after this point.
6. Run the registered pass once, with the same command plus `--pass registered`, a new `--out` (`~/.local/state/lab-runs/movement-v1/registered`) and `--blind-receipt ~/.local/state/lab-runs/movement-v1/blind/receipt.json`. It refuses a blind receipt of another commit or other backtest sources (`blind_receipt:another_code`).
7. The outputs stay on the lab. They are aggregates with no market or selection id, but they are derived from licensed data. Nothing enters Git or `reports/` without Yu's OK on the exact fields.

## What it changes elsewhere

- Two modules are added to `betangel_research/betfair/backtest/`, and no existing source file changes. The engine certificate's source descriptor covers every module in that directory, so its digest, and so the certificate key, changes. P0's dda38a8 certificate no longer matches this code. P0 is stopped by its own stop rule, and any later `run-backtest` needs `certify-engine` at the new sources. The boundary test's descriptor count goes from 21 to 23. The alternative, moving both modules to a sibling package to keep the key, would take simulated decisions outside `betangel_research/betfair/backtest`, which AGENTS.md reserves for them. Accepting the key change is the lead's decision, recorded in the PR.
- The runner is its own module (`python3 -m betangel_research.betfair.backtest.movement_study`), not a subcommand of the backtest CLI. That CLI's `__main__.py` is one of the certified sources and stays byte-identical.
- The CLASSIC ladder and the Student t routine are copied from `constants.py` and pilot.py, not imported. Importing either would add files to the backtest closure that the certificate descriptor is pinned to. Tests check that the copies agree.
- The study imports private helpers of certified modules (`runner._inputs`, `_records`, `_record_months`, `replay._member`, `market_index._messages`, `_checked_changes`, `_Market` and others), as `readiness.py` does. A later change to those internals therefore also changes what movement-v1 does.
