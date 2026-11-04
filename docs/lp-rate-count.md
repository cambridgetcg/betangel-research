# LP rate count: Stage 0 for Pilot 2

Status: Registration, written 2026-11-03 and first merged that day (PR #56, commit 9e69b73, stamped 23:04:55Z) before any recording was read for it. Amended once on 2026-11-04, before any qualifying count was printed ([amendment 1](#amendment-1-2026-11-04)); the amendment changes only which sessions are read and which code decides. The counter is not run again until amendment 1 has merged. The only run so far, on the Air on 2026-11-04, could print no more than NOT_ENOUGH_DAYS. On 2026-11-03 Yu chose "Count first": before any Pilot 2 code is built, count for free how often today's GB/IE horse-racing WIN markets show the spreads Pilot 2 would quote in. Later the same day Yu also settled which run decides Gate A, a minimum number of markets, and that qualifying counts stay hidden until then ([Yu's choices](#yus-choices-2026-11-03)).

For Yu, Nuance, Astra and the lab operator. Author: lab-Claude, 2026-11-03, against betangel-research main 35da293; amendment 1 on 2026-11-04, against main b36c215.

- The code is `scripts/lp_rate_count.py`, tested by `tests/test_lp_rate_count.py` on synthetic recordings only.
- The definition comes from the Pilot 2 design (lab note, 2026-11-03, sections expected_rate, go_live_gates and code_stages PR2). This page fixes the details that design left open. It changes nothing in Pilot 1.
- The design put this registration in the Pilot 2 plan (its PR1). Yu chose to count before anything else is written, so the count is registered here instead. If a Pilot 2 plan is written later, it links to this page and does not register the count again.
- *Inference* marks arithmetic or reasoning that was not measured.

## What it counts

The input is the delayed-key recordings the #41 recorder writes on Yu's Air: GB/IE, event type 7, WIN, with EX_BEST_OFFERS at 3 levels. The delayed key is 1–180 s late and conflated to 180 s (`betangel_research/betfair/recorder.py:14-15`, facts F44 and F51). The T6 live test recorded Betfair echoing that conflation, 180000 ms (`reports/betfair-live-tests-2026-10-13.json:129`). It is not licensed historical data.

- **Sessions.** It reads every directory under the recordings root named as the recorder names sessions (`rec-YYYYMMDDTHHMMSSZ-` plus 8 hex digits, the UTC start); other names are ignored. A session that started before 2026-11-03 23:05:00 UTC is not read ([amendment 1](#amendment-1-2026-11-04)). A session is read only if its receipt (`receipt.json`, or `receipt.partial.json` while it is still running or after a crash) gives:
  - key_type `delayed` and label `delayed_conflated`;
  - `conflate_ms_echoed` exactly 180000;
  - a subscription that asks for EX_BEST_OFFERS and EX_MARKET_DEF but not EX_BEST_OFFERS_DISP (virtual prices), with ladderLevels 1 to 3.
- **Segments.** In a session that is read, only the segments the receipt lists are read. Each one needs a matching sha256 and a passed replay check. A segment that fails part way through is dropped whole. Every session or segment that is skipped is counted under a fixed code, and so is a `rec-*` entry that is a file or a symlink (symlinks are never followed).
- **Markets.** GB or IE, event type 7, market type WIN, ODDS, one winner, CLASSIC price ladder, at least 5 ACTIVE runners, OPEN and not in play. All of these are checked in the snapshot used. The CLASSIC check follows the order path's own check (`betangel_research/betfair/validate.py:122`).
- **Runner rule (primary).** An ACTIVE runner whose best back b and best lay l are both on the CLASSIC ladder, with:
  - b < l;
  - 1.50 ≤ b ≤ 5.90;
  - l exactly 3 or 4 CLASSIC ticks above b.

  A market qualifies when at least one runner meets the rule.

## Off and snapshots

- **Off.** The off is the first `marketTime` the recordings publish for the market. Sessions are read in start order and segments in index order. Why:
  - It is fixed before any snapshot is chosen. The other option, the marketTime in the last definition at or before the snapshot, depends on the very snapshot it is meant to place.
  - It is the same anchor lp-v1 (`docs/lp-study.md:23`), movement-v1 (`docs/movement-study.md:21`) and the recordings index (`betangel_research/betfair/recordings.py:12-14`) use.
  - The recorder's replay check makes sure every market's first change carries a marketTime (`betangel_research/betfair/recorder.py:125-131`).

  A later revision does not move the off. Markets whose marketTime was revised at or before off −300 s are counted on their own line but not excluded, because the registered rule has no such exclusion.
- **Racing day.** A racing day is a UTC date of the off on which at least one market is usable at −300 s. A short day, such as one where the Air slept or the recorder crashed early, counts as a full racing day.
- **Snapshot at X.** This is the last recorded state at or before off + X. It holds every message with publish time pt ≤ off + X, so the batch published at off + X is included. Across segments, the latest such state is used.
  - A segment says nothing about a moment after its last line. Heartbeats count here: they arrive every 5 s, so a segment's last line is about when it stopped. If off + X falls after a segment's last line, that segment gives no snapshot at X. A state is never carried into a gap, as the recorder's own rule says. The gap of a few seconds between one segment and the next (a refresh every 30 minutes) can therefore lose a few snapshots, which show as no snapshot.
  - Times are always Betfair's pt, the same clock as marketTime. The Air's `received_at` is never used, because nothing verifies it.
- **Data age.** The age is off + X minus the pt of the last non-heartbeat line (image or delta) at or before off + X, in the segment that holds the state.
  - A snapshot is usable only if its age is at most 180 s, it was taken outside a 503 ("data possibly stale") window, and it carries no non-finite number.
  - Otherwise the market counts as no snapshot (or invalid) at X. An older state is never used instead.
  - Why heartbeats do not count for age: under 180 s conflation, the delta published at T carries every change up to T, so the state is the exchange as of T, minus the 1–180 s delay. A heartbeat only shows that the connection is alive.
  - Why not the market's own last change: a quiet market is not stale.
  - *Inference:* that a delta leaving a market out means the market did not change follows from Betfair's conflation behaviour. It has not been checked on a live stream.
  - In the T6 live test the largest gap between non-heartbeat lines was 180.013 s (`reports/betfair-live-tests-2026-10-13.json:135-138`). That run had only 4 such lines (1 image and 3 deltas). So a few milliseconds in each 180 s may fall just past the limit, but how often is not known from that sample.
- **Non-finite numbers.** A non-finite number on any runner (removed runners included) makes the market invalid until its next image. Images come at the start of each segment, so this can last up to 30 minutes. That is stricter than `book.py`, so it can only remove snapshots. The "invalid" count covers both 503 windows and non-finite numbers, so it is an upper bound on how many this rule removes.

## Columns

The script prints these per racing day and pooled. Every column counts markets usable at −300 s, and every rate is out of those markets, rounded down to 0.01%.

| Column | Counts the usable markets that are |
| --- | --- |
| usable | in the population, with a usable snapshot at −300 s (the denominator) |
| primary | qualifying at −300 s |
| flagged at −460 s | qualifying in the usable snapshot at −460 s (the last state at or before off −460 s). A proxy for Pilot 2's delayed pre-filter, which also sees virtual prices |
| persistence | primary and flagged at −460 s. Labelled "persistence in delayed data, not live recall" |
| persistence from two batches | persistence, where the −460 s and −300 s snapshots come from different delayed batches |
| window | qualifying in some usable state from −600 s to −300 s: the state at −600 s, or the state after any line published after −600 s and up to −300 s |
| price lever | with an ACTIVE runner whose 6.0 ≤ b ≤ 10.0 and l is 3 or 4 ticks above |
| width lever | with an ACTIVE runner whose 1.50 ≤ b ≤ 5.90 and l is 5 to 7 ticks above |

- The table also shows "recorded": the markets in the recordings read, with their off on that date.
- In `--json`, every column appears per racing day and pooled, with rates out of the usable markets. The table prints primary, persistence, window and the two levers per day; flagged and two-batch counts appear only in its pooled persistence line, where persistence and the two-batch count are shown as shares of the markets flagged at −460 s. That share is how often a delayed flag still holds at −300 s.
- The −460 s and −300 s snapshots are 160 s apart, but delayed batches come every 180 s, so both can be the same batch seen twice. The two-batch count leaves those out.

It also prints:
- why markets were not usable at −300 s (no snapshot, invalid, out of population, not open or in play, under 5 ACTIVE), over the same dates as the pooled row;
- the revised-marketTime line, over the same dates;
- the sessions and segments it read and skipped, and how many sessions it read from a partial receipt (still running, or crashed);
- the sha256 of the script's own bytes.

It never prints a market id, selection id, event name, venue, price or time of day.

**Until Gate A can be read** it prints only recorded and usable counts, racing days, the not-usable reasons, the revised-marketTime count, the gate's conditions and the inputs. It prints nothing about which markets qualified, so nobody can watch the rate build up through its output.

## Gate A

Gate A is fixed by the recordings, not by when the script is run:

- **Its dates.** Walking the UTC dates of the off in order, Gate A pools every date up to and including the first date D on which the running totals reach 10 racing days and 300 markets usable at −300 s. Dates after D never change it. They are printed per day for description only and are not pooled.
- **When it can be read.** Only once D's recording has ended: a session started on a later UTC date exists, or the latest session's final `receipt.json` says complete with a stop reason that ends the recording day (`end_at_reached`, `max_duration_reached` or `max_seconds_reached`, as `recording_day.DONE_STOPS`). A session stopped for live work (`stop_requested`) or for a reconnect restart has not ended the day, because the daily wrapper resumes recording after it. Each session's receipt is read once, so the data and this check come from the same read.
- So every run of the deciding code ([amendment 1](#amendment-1-2026-11-04)) after D's recording ends gives the same PASS or FAIL.

- **PASS.** The primary count is at least 1.4% of the markets usable at −300 s, pooled. The test is exact: primary × 1000 ≥ 14 × usable. Yu then decides whether Pilot 2 is built. The count starts nothing on its own.
- **FAIL.** Below 1.4%. No Pilot 2 code is built unless Yu records a decision to go on anyway. Yu then chooses between:
  - stopping LP here, at a cost of £0; or
  - the lab bringing the levers with their counts (deciding from −600 to −300 s, lays above 6.0 at a smaller stake, wider spreads, or other markets).
- **NOT_ENOUGH_DAYS.** Fewer than 10 racing days. Wait for more recorded days, then run it again.
- **NOT_ENOUGH_MARKETS.** 10 racing days or more, but fewer than 300 usable markets so far. Wait, then run it again.
- **RECORDING_OPEN.** D is known, but its recording has not ended: the latest session is still recording, crashed, or was stopped for live work or a restart. Run it again after 23:00 London, or once a later day's session exists. A file or symlink with a session's name is not a session and is ignored here.
- The threshold is 1.4%, about 0.5 qualifying markets a racing day. That is roughly five times the 2015-16 rate of 0.25–0.3% of markets (design, expected_rate).
  - That comparison is approximate. lp-v1 excluded markets whose schedule was revised and divided by indexed markets. This count keeps revised markets and divides by markets usable at −300 s.

### Yu's choices (2026-11-03)

Yu took the lab's three suggestions ("Lets go with what you recommeded"):

1. **The first decisive result decides.** The lab's suggestion was the first run after the 10th racing day's recording has closed. The script builds that in: Gate A pools a fixed set of dates and is read only after the last of them has ended, so the result does not depend on when anyone runs it. It cannot be re-run until it passes. The decisive run is one of the deciding code (amendment 1's), on the Air, with `--root` set to the recorder's recordings directory (the command below). A run on a copy, a subset or test data decides nothing.
2. **At least 300 usable markets,** pooled, as well as 10 racing days. A short day, such as one where the Air slept, still counts as a racing day, but 10 thin days cannot decide the gate. In 2025 there were about 36.5 GB/IE WIN markets a day (`docs/betfair-pilot-1-plan.md:190`), but that counts all markets, not markets usable at −300 s in these recordings. lp-v1 replayed about 31 a date in 2015-16. So 300 usable markets may take a few days more than 10 (*inference*).
3. **Qualifying counts stay hidden until Gate A can be read.**

Record the first PASS or FAIL with the run's script sha256 and the date it pools through.

### Amendment 1 (2026-11-04)

- **What was missed.** When this page was registered, the Air's recordings root already held four delayed-key test sessions from before the recorder rollout. They started on 2026-10-13, 2026-10-19 (two) and 2026-10-22, and each is complete, stopped at its `max_seconds` cap. The lab knew of the 2026-10-13 and 2026-10-22 recordings from the committed live-test reports (T6 and T3), and this page cites T6's echoed conflation, but it did not check whether they, and the two 2026-10-19 tests, still sat in the recordings root the count reads. Under the rule as merged at 9e69b73, any of them that passed the session checks would be read. A racing day is any UTC date with one market usable at −300 s, so these short runs, on three UTC dates, could count as 3 of the 10 racing days.
- **What has been seen.** On 2026-11-04 the Air's Claude ran the counter as merged at 9e69b73 once on that root, which held only these four sessions (the Air's note of 2026-11-04). With at most three dates it could print only NOT_ENOUGH_DAYS, with the qualifying counts withheld, and its output was not shared with the lab. No qualifying count has been printed for this registration.
- **Yu's choice.** Yu was asked: "Should the count use only recordings made after the counting rule was fixed (from 3 Nov, 23:05 UTC), leaving out the four short October test recordings?" Yu chose "Only new recordings" (2026-11-04).
- **The rule.** A session counts only if the UTC start in its name is at or after 2026-11-03 23:05:00 (`FIRST_START`), just after this page's first merge (9e69b73, stamped 23:04:55Z). An earlier session is not read at all, not even its receipt. It is counted under the skip code `recorded_before_registration` and adds no market, no date and no ended recording day. Every later session counts, including any recorded before amendment 1 merges; no run has printed a qualifying count for any of them.
- **Unchanged.** Everything else, including the markets, the runner rule, the columns, Gate A's 10 racing days, 300 usable markets and 1.4%, and Yu's three choices. The output schema is now `lp-rate-count/v2`, because the sessions line has the new code.
- **Which run decides.** A run whose printed sha256 is `a8c5a0ec99df9ab40b6edfaf0a153f09eb530dbba2d3e3c2505e3ae58ba6aace`: `scripts/lp_rate_count.py` as amended here, on main from the commit that lands amendment 1. `tests/test_lp_rate_count.py` checks that this line names the script's current sha256, so the script cannot change without a new amendment here. A run of 9e69b73, or of any other version, decides nothing.

## What it cannot show

- **Delay.** The data is delayed by 1–180 s and conflated to 180 s, so a "−300 s" state can describe the exchange as early as about −660 s (*inference*: up to 180 s of age plus up to 180 s of delay).
- **Depth.** It sees the best 3 levels only.
- **Virtual prices.** There are none. Pilot 2 decides on a live book that has them, and virtual prices narrow spreads, which closes some 3–4 tick spreads (`docs/lp-study.md:125`). The design therefore reads the count as an upper bound, not live recall. *Inference:* virtual prices could also bring a few wider spreads down into 3–4 ticks, which the width lever shows.
- **Coverage.** The recorder runs 07:00–23:00 London. Explicit-mode subscriptions can leave markets out (the first 150 by start time; `betangel_research/betfair/recorder.py:664-711`). So "recorded" is not every market.
- **Fills and money.** It says nothing about whether a quote would fill or pay.

## Who runs it, and how

- Yu runs it, or the Air's Claude with Yu's OK, on the Air. The recordings never leave the Air. Only the printed counts are shared, and only with Yu's OK.
- Run it from a checkout of main that includes amendment 1, not from Pilot 1's registered checkout or the recording_day pinned worktree. Those stay at their own commits.
- The printed script sha256 must be the one named under [Which run decides](#amendment-1-2026-11-04). A Gate A result is read only from a run whose sha256 matches.
- Running it early does no harm. Until Gate A can be read it prints NOT_ENOUGH_DAYS, NOT_ENOUGH_MARKETS or RECORDING_OPEN and withholds the qualifying counts. An open segment is never read, and the sessions line shows how many sessions were read from a partial receipt.
- The command (Python 3.10 or later):

  ```sh
  cd <that checkout> && python3 -I -B scripts/lp_rate_count.py \
    --root "$HOME/Library/Application Support/betangel-research/betfair/profiles/default/recordings"
  ```

  - Add `--json` for one JSON object instead of the table.
  - If the Air's config sets `recorder.root`, pass that directory instead.
- What it does and does not do:
  - It uses the standard library only.
  - It needs no network, credentials or login, and writes nothing.
  - It refuses a root inside a Git worktree, because recordings stay outside Git.
  - It exits 0 with a result, or 2 with a fixed code on any refusal or failure.

## What it changes elsewhere

- Nothing under `betangel_research/` changes. The script sits in `scripts/`, outside `receipts.code_hashes` (`betangel_research/betfair/receipts.py:121-125`). So Pilot 1's code identity, contract and registration do not change, and Pilot 1 does not re-register.
- Scripts may not import `betangel_research.betfair` (`tests/test_betfair_boundaries.py`). The script's line reader, best-offer fold and CLASSIC ladder are therefore small copies. Tests check them against `stream.messages`, `book.MarketBookCache` and `ticks.LADDER_CLASSIC`. The copies do not follow a later change to those modules by themselves; the tests are what catch one.
- Two dated lines are added in [backtest plan](backtest-plan.md) §13: the registration (2026-11-03) and amendment 1 (2026-11-04).
