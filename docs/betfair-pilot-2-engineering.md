# Pilot 2 engineering note: how the plain capped bot would keep the page's promises

Status: proposed, for Astra's review. None of the code it describes is merged. It describes the code that PR-1 and PR-2 of the [plan](betfair-pilot-2-plan.md) would add, as read at main c86764a; each is rebased onto main before it merges (plan, Stages). The [Pilot 2 page](betfair-pilot-2-policy.md) wins on any difference, and RS-19 and its [engineering note](betfair-pilot-engineering.md) ("eng") stay as they are.

For Astra, Yu and the Air's Claude. Author: lab-Claude, 2026-11-05; rewritten on 2026-11-09 for the plain capped bot Yu chose that day, from the lab's build spec of the same day; revised on 2026-11-10 after lab-Claude's review round of the drafted code and its fixes; rebased onto main b9099ff later that day, after #59, #63 and #60 merged, and brought into line with those merges.

- The sections follow eng's numbering. A section says only what differs. Where it is silent, eng applies to Pilot 2 as written, with "pilot" read as "pilot_lp".
- File:line citations are to main c86764a.
- *Inference* marks what has not been checked against Betfair or run. *Unverified* marks what nobody could confirm.

## 1. What it amends
Nothing, until Pilot 2's go-live. The lines Pilot 1's documents would gain then are listed on the page, under Changes to Pilot 1's documents.
- **Switched off until then.** The code ships with `p2_lp.IN_FORCE` False. Only the go-live commit, which also applies the page's AGENTS.md, RS-19 and order-path lines, sets it True. It is a PR of its own (plan, Stages: PR-L), which Yu merges before the go-live consent names it (page, Three consents).
- No test ties `IN_FORCE` True to those document lines, and none shows that `p2-run` loads the order path before the banner's git read (§7). The go-live PR's reviewers check both by reading it.
- While it is False, `config_problems` returns `p2_not_in_force` first, so the gateway refuses a Pilot 2 context (`p2_gateway_needs_config_p2`). `p2-run` refuses before the SSH check and any Keychain, journal or git read: `config_missing` while config-p2.json is absent (`load_config_p2`), else `p2_not_in_force` at the top of `p2_run.run`. The command line's own checks come before them: `--mode live` and the profile before `config_missing`, and `--bet-angel-paused` (while config-p2's coexistence is unverified) before `p2_not_in_force`. `p2-summary --mode live` (offline, read only, once config-p2.json exists) and `init-config --p2` still work.

## 2. Scope: inside Pilot 1's profile
- **Identity.**
  - No contract. Two new modules: `p2_lp.py`, pure (the constants, the rule, the counts, the ARM line and the summary), and `p2_run.py`, the executor. `p2_lp.py` imports only `pilot` and `ticks`, never the gateway or `pilot_gate`. `p2_run.py` never imports the gateway; cli.py hands it the factories. It does build a `RiskGuard` (it imports risk) for ARM's check of the day's room (§7). gateway.py imports `p2_lp`, so in Pilot 2's checkout every gateway command, the kill included, needs it to import (§3). The names keep them, and their tests `tests/test_betfair_p2_*.py`, out of Pilot 1's `pilot*` file families.
  - customer_strategy_ref `p2-lp`; intent source `pilot_lp`, live only and accepted only through the gateway's new `lp_place`. The public `place()` and its typed line stay byte-identical (tests/test_betfair_pilot_gateway.py:2287-2385 guard that).
  - No record type of its own. Its orders are core intents, and its one ARM record a session is the core kind `operator_confirm` (journal.py:80-82), which the fold ignores (§7).
  - Profile `default`, the same as Pilot 1. A15 refuses a second profile on the same account for place, cancel, reconcile and settle (arming.py:42, :46-62, :385-406, :460-468; F76), and the Keychain credentials are per profile (arming.py:235-239).
  - Its own config file, `<profile>/config-p2.json`, read only by `load_config_p2`. `load_config` still refuses that path (`config_override_not_allowed`, config.py:139-140), so it can never load into a manual command. Pilot 1's config.json, its sha256 and PA3 do not change.
  - Its own checkout, put at exactly the go-live commit as merged on main, with a clean tree, and never pulled afterwards (page, Three consents). There is no registration: the banner shows the commit, and ARM refuses a dirty tree. No document yet names the checkout's path (plan, Stages).
- **Shared with Pilot 1, on purpose.** One Keychain credential set, one live journal and hash chain, one KILL file, one `.gateway.lock` (paths.py:73-74), and one recording admission and live-owner lock (paths.py:90-98). Every R-rule folds the whole journal, so each pilot's R6, R9, R10 and R12 see the other's orders. R12 at £0 does not misfire, because both pilots' orders are in the journal.
- **One Mac.** All of these exist once per Mac, under the state root, as do accounts.json (A15) and the login ledger (paths.py:1-5, :73-81, :103-106). Nothing in the code stops two Macs trading one account. The sharing holds because Yu chose on 2026-11-05 to move the credentials, Pilot 1, Pilot 2, the manual £1 drills and the recorder from the Air to the lab Mac, and the Air stops live work ([plan](betfair-pilot-2-plan.md), Decided). The lab Mac starts a fresh journal; the Air's state root and anchor are never copied to it (arming.py:136-153; betfair-order-path.md:403-411).
- **What the bot may send:**
  - per race, before the LAY's deadline: a BACK of exactly £2.00 at the best lay − 1 tick, then a LAY of exactly £2.00 at the best back + 1 tick, never above 6.00. Both are LIMIT and LAPSE, one instruction per request, each with its own customer order ref;
  - whole cancels of its own legs only: on a Pilot 2 gateway, `cancel()` refuses any bet id that is not a leg of this open (`p2_cancel_not_own_leg`). The pull uses `source="cli_manual"`, which works in any open (gateway.py:2227-2249).
- **The BACK goes first.** If the second send fails, the leg left resting can lose £2, not £10.
- **The one exception is the kill.** The existing own-scope `kill`, with `p2-lp` added when config-p2.json exists, cancels every open order of ours, Pilot 1's and manual ones included (gateway.py:3088-3095).

## 3. Who starts, stops and changes it
- **Commands,** all in cli.py, which stays the only module that imports the gateway:
  - `p2-run`, the session (page, Arming a session);
  - `p2-summary --mode live`, offline counts, with `--cleared` for Betfair's figures (§10);
  - `init-config --p2`, which writes research/betfair-config-p2.example.json to config-p2.json with O_EXCL (as cli.py:889-905), and prints config-p2's hash, for a new file or one that exists (§9);
  - the existing `kill`, which adds `p2-lp` to its sweep only when config-p2.json exists. The check sits inside the kill's own try (cli.py `_p2_configured`): only a missing file (by `lstat`, so a dangling link counts as present) leaves `p2-lp` out, and any other error counts as present, so no problem in that check can end the kill before its sweep. The kill imports `p2_lp.STRATEGY_REF` inside that try. So a `p2_lp` that cannot be imported ends the kill exit 3 (`kill_incomplete_no_session`) after the KILL file and its kill and halt events, with nothing swept; a running bot then hands off (§8). Importing `p2_lp` only in the gateway's lp paths, with the literal `p2-lp` standing in for the kill, would let that kill sweep. It was drafted in the review round and withdrawn, because it adds a cancel path, which a review fix may not (plan, Open for Yu).
- There is no register, end-session, ack or separate kill command for Pilot 2.
- **One pilot session per UTC day across both pilots.** ARM refuses on a UTC day with a Pilot 1 `session_start` or a P2 ARM record, while a Pilot 1 window is open, or while Pilot 1's armed-session file exists (pilot_run.py:350). Each leg checks the Pilot 1 day and the ARM record again (§6). Sends are refused while a Pilot 1 session's window is open (gateway.py:745, :1942-1944). Pilot 1's own ARM does not look for P2 ARM records (it checks only its own `session_start`, pilot_run.py:891-892), so today the rule holds from Pilot 2's side only; a matching check in Pilot 1 is a go-live question for Yu and Astra (plan, Open for Yu).
- Pilot 1's code is unchanged, so nothing on Pilot 1's side refuses a Pilot 1 session after a Pilot 2 session on the same UTC day. That half of "one pilot session a day" is Yu's rule.
- **Caffeinate.** `/usr/bin/caffeinate -i -s -w <pid>`, started at the ARM and ended at the session's end. It is process-scoped and changes no setting. Mains power and the open lid are Yu's procedure; there is no battery check in code.
- **Stopping a running session.** Yu chose on 2026-11-10 that lab-Claude's first stop is the KILL file alone, and the kill only if the bot is gone. What "gone" means is the page's *proposed* reading (page, Who does what; §8, KILL file).
- Typed lines are Yu's, as in eng §3.

## 4. The decision
- **Markets.** One delayed catalogue call: GB and IE, event type 7, WIN, with MARKET_START_TIME and MARKET_DESCRIPTION. Each row must have bettingType ODDS and `turnInPlayEnabled` true (`schedule_row_ok`). A market whose screen window has passed, before the wait for it or after it (a stall or sleep of the Mac), is skipped (`missed_window`). A market whose off −300 s is after UNTIL is left out, as the gateway would refuse it (`p2_until_before_pull`).
- **Screen,** off −840 to −780 s, on the delayed session only.
  - One fresh catalogue row by market id with Pilot 1's row projections, stored with `store_catalogue_row` as Pilot 1 does (api.py:777-787; gateway.py:1846). Every send book takes its `market_time` and the country from this row, not from a cache up to 6 h old (config.py:46).
  - One delayed `market_snapshot`. `screen` needs `isMarketDataDelayed` true (else `not_delayed`), then `book_problems` and `select_runner`.
  - The off moved, a read error or a screen code: the next market, with no live-key read (F46).
  - The screen's delayed book has no virtual prices, the same kind Gate A counts, so some flagged markets are refused `spread_out_of_band` on the live virtual book and count toward the skip streak, and a market whose raw spread is 5 or more ticks is never opened; the first sessions' counts of that refusal after a flag show how often (virtual prices at the screen too are open for Yu: plan, Open for Yu).
- **Before the open:** the stop flag or the KILL file ends the session; the login cap moves to the next market; a broken admission ends the session. The time is checked again after the wait for the open, and again when the open's WAIT returns: once off −575 s has passed (a stall, a sleep or a slow open), the BACK could only be refused, so the race moves to the next market (`missed_window`) before any live-key read, or before the BACK's funds and book reads. A `missed_window` after the open counts in the skip streak, as a refused BACK and a failed send-book read do: each followed a live-key login and reads with no order (F46). Skips before the open make no live-key read and do not count.
- **Open,** off −740 s: the gateway command `place` with config-p2.json and the Pilot 2 context. The lock, the login, the startup reconcile, UNKNOWNs resolved within 120 s (gateway.py:104, :774-781), A1–A19, the Pilot 1 window guard (gateway.py:745) and the anchor. If `open_report()["armed"]` is not True, close and end (gateway.py:827-829): dynamic failures do not raise (gateway.py:785-788, :810-820).
- **Book problems** (`book_problems`): OPEN, not in play, at least 5 ACTIVE runners; on a live send book also `isMarketDataDelayed` false and `betDelay` 0. The V-rules check neither the bet delay nor the ACTIVE count (validate.py:104-140, :280).
- **Runner** (`select_runner`, on the send book):
  - In band: ACTIVE; best back b and best lay l both on the CLASSIC ladder; b < l; 1.50 ≤ b ≤ 5.90.
  - Eligible: l is exactly 3 or 4 CLASSIC ticks above b.
  - Choice: the lowest b, then the lowest l, then the lowest selection id. A tie is never a SKIP.
  - No depth predicate: both legs rest on new, empty levels. The best prices that set them have no size rule either, so one £1–£2 order can set the touch and so our prices. Betfair's display rolls amounts under £1 into the next price (Passive check, below). A minimum shown amount at both best prices is open for Yu (plan, Open for Yu).
  - Codes: `no_runner_in_band` and `spread_out_of_band`.
  - Why 3–4 ticks: at 2 ticks the two inside legs land on one price, and at 1 tick they cross; V8 refuses both (validate.py:257-268). A wider spread puts the inside BACK more than 3 ticks from the best back, which V13 at 3 refuses (validate.py:286-290).
- **Leg prices** (`leg_prices`): BACK at l − 1 tick, LAY at b + 1 tick. It raises unless LAY < BACK and LAY ≤ 6.00.
- **Passive check** (`passive_check`), per leg, on that leg's own send book: best back < leg price < best lay, strictly. Our own resting BACK counts: it is the LAY's best lay at l − 1, and b + 1 is still inside the spread. Otherwise `leg_not_passive`, and that leg is not sent.
  - The book is Betfair's displayed view: virtual prices on, and Betfair's default roll-up of amounts under the minimum stake into the next price (Betting Type Definitions, DD/2687465, ExBestOffersOverrides.rollupModel: "If unspecified defaults to STAKE rollup model with rollupLimit of minimum stake in the specified currency"; support article 115003878512: "The default across the website and API is £1"). The send book asks for no override.
  - So an offer under £1 inside the displayed spread is not shown, and a leg that passes can still match under £1 at once, at its own price. That stays inside every cap, and like any matched part it ends the session. *Unverified:* documented, not yet observed by us; no facts-register row yet.
- **BACK** (`choose_back`): the country from the row SCREEN stored must be GB or IE (`p2_country`; a missing country refuses). The book's `observed_at` must lie within off −590 to −575 s, by its `market_time` (`p2_window`). Then `book_problems`, `select_runner`, `leg_prices` and the passive check. The instruction is £2, LIMIT, LAPSE, handicap 0.
  - GB or IE is Betfair's `event.countryCode`, which Betfair sets to GB "when the correct country code cannot be determined" (Betting Type Definitions, DD/2687465, Event). The catalogue filter, the stored row and `p2_country` all rest on it, as Pilot 1's rule does. Not seen to happen for a race meeting; no facts-register row yet.
- **LAY** (`choose_lay`): the book observed by off −532 s (`p2_send_deadline`; with the 2 s book age, gateway.py:1032-1034, it leaves by about −530 s), `book_problems` on the live book, the runner ACTIVE, and the passive check for the LAY on this fresh book. The selection and price come from the placed BACK. Then the best back on this book must still be the LAY's price less one tick, both on the ladder, else `p2_book_moved`: the price was taken from the BACK's book, and a best back that fell since leaves it more than one tick above. It only refuses: the price is never moved, and the BACK is then pulled (§8). *Proposed:* built in the draft, for Yu at the build consent (plan, Open for Yu).
- **Virtual prices.** Pilot 2's send books are fetched with virtual prices on. Every other request body is unchanged. This needs a `virtualise` option in api.py, which needs Yu's OK (plan, Open for Yu). Without it, the passive check sees non-virtual prices only. The field is documented: Betting Type Definitions (DD/2687465), PriceProjection `virtualise`, boolean, default false, "Indicates if the returned prices should include virtual prices". The code sends JSON true; Betfair's examples send the string. Not yet observed live, and no facts-register row yet.
- **Incomplete pair.** If the BACK was placed and the LAY is not (refused, FAILED, or raised before it was sent), the BACK is pulled at once (§8). A LAY that raised with nothing of it sent then ends the session (`lay_raised:<code>`), unless only its book read failed. If the BACK was not placed, nothing more is sent.

## 5. The first-byte gate
The plain manual first-byte gate, unchanged, for each leg. `lp_place` runs `_place_live` (gateway.py:1283-1307) with a Pilot 2 side set:
- It keeps the token check, `_require_armed`, the manual-during-session refusal and the resolved-at-open refusal (gateway.py:1284-1289).
- It skips V0 and `_take_confirmations` (gateway.py:1290-1292, :1297), because there is no per-order typed line.
- It keeps the funds check first, then fetches the book itself, at most twice (gateway.py:1301-1304).
- A `choose_*` code refuses, with only the funds check journaled.
- Then `_live_arming("place", book)` and `_place_core` with source `pilot_lp` and its one new hook, `lp_check` (§6), which also gets the funds `_place_live` read.
- With no Pilot 2 side, `_place_live` is byte for byte today's.

## 6. Counts and gateway checks
- **No Pilot 2 fold.** Every count comes from the core fold's order records with ref `p2-lp` (journal.py:758-777). `races()` gives the markets with a `p2-lp` order that has a `submitted_at`, so a race counts from its first submit.
- **The check runs on the submit's own basis.** `_lp_check` runs inside `_place_core`, right after the session guard (gateway.py:1942-1944), on the basis read of the submit's compare-and-append. Anything journaled after the check makes the submit refuse (gateway.py:1212-1254). It reads the cached fold, so no full refold runs inside the 2 s book window.
- **Refusal codes,** in this order (`leg_refusal`):
  - the ARM record: `p2_arm_missing`, `p2_arm_other_config`, `p2_arm_other_session`, `p2_arm_not_today`, `p2_arm_expired`, `p2_arm_already_today`;
  - `p2_pilot_1_day`;
  - BACK: `p2_market_used` (any `p2-lp` intent in the market, refused ones included), `p2_until_before_pull`, `p2_races_total`, `p2_races_today`, `p2_races_session`, `p2_other_bet_open`;
  - LAY: `p2_lay_without_back`, `p2_lay_already_sent` (from the basis fold, never an in-memory flag), `p2_lay_not_pair` (selection or price), `p2_other_bet_open`;
  - any leg: `p2_leg_shape` (£2, LAPSE, the side);
  - then, for a BACK only, the pair pre-check (below): `p2_pair_precheck:<rule>`.
- **The pair pre-check,** for the BACK only, giving `p2_pair_precheck:<rule>`:
  - the existing `evaluate_pilot_reservation(..., size_p=1000)` (risk.py:544-566): R1, R3, R6 and R8–R10 for £10 more, so R10 never refuses the LAY alone;
  - then `p2_lp.pair_room`, which covers what that leaves out: R6's minute, hour and day windows with room for two orders (`rate_minute`, `rate_hour`, `rate_day`), and R11's funds for both legs on the BACK's own funds read: £2 plus the LAY's whole liability, rounded up to a penny, within the available funds less the reserve (`funds`). A missing balance figure, a failed funds read or a LAY price off the ladder refuses `funds`.
  - So R6, R10 and R11 do not refuse the LAY alone unless the account or the journal changes between the legs. risk.py does not change. A test pins `pair_room` to risk.py's R6 for two orders and R11 for both legs.
- **The guard's config.** A Pilot 2 gateway is refused (`p2_gateway_needs_config_p2`) unless its config is config-p2 exactly and its risk guard was built on that same config (by sha256).
- **No leg is lost.** The refs go into the open's set right after `_new_refs` (gateway.py:1948), before any intent, because the submit, the send and the report can each raise after the order went out (gateway.py:2033, :2108, :1172-1174; journal.py:314). `lp_legs()` reads every leg from the fold by those refs; if the fold raises, each ref is still named.

| Limit | Enforced by |
|---|---|
| £2 BACK at l − 1, £2 LAY at b + 1, one runner | `choose_*` with `leg_prices`; `p2_leg_shape`; R5's £2 stake cap (risk.py:292-295) |
| best back 1.50–5.90, spread 3–4 ticks | `select_runner` on the gateway's own fresh send book; V13 at 3 ticks |
| BACK first; LAY only if the BACK was placed | the BACK placed in this open and still resting or matched; `p2_lay_without_back` on the basis read; V7 at 1 instruction (validate.py:310); a restart can never send an earlier LAY. This is judged from the journal, and no order-status read runs between the legs: a BACK cancelled outside the bot, or matched, in the seconds between the legs is not seen, and a LAY sent then may rest alone until the pull, inside the £10 race |
| each leg strictly inside the spread on a fresh book | the passive check inside `choose_*`, on the book `_place_core` judges; for the LAY, `p2_book_moved` when the best back has fallen since the BACK's book; V8 keeps LAY < BACK; V10 and A18 at 2 s |
| lay at most 6.0; worst race −£10 | `leg_prices`; R5's liability cap of £10, so 6.0 passes and 6.2 is refused (risk.py:296-299); R8's market cap of £10, with no hedge credit (risk.py:136-154, :367-380) |
| funds for both legs; room for two orders | the pair pre-check's `pair_room`, on the BACK's own funds read; R6 and R11 again at each leg |
| 5 races a day, 10 in all | `races()` on the basis fold; R6 at 10 orders a day as a backstop (risk.py:301-325), and the pair pre-check asks R6 for both legs' room |
| day stop −£20 | R10 at £20 (risk.py:327-348, :378-379) and the pair pre-check; ARM refuses `p2_pair_precheck:daily_loss` when today's booking leaves no room for £10 (§7). settle-market books each bet at its worst case (gateway.py:3017), so one fully matched race at 6.0 books £12 and ends the day. With one session a UTC day that ends after any matched leg, the worst real day is −£10 (page, Money) |
| one race in flight; nothing else of ours open or unsettled | one process, one open, `.gateway.lock`; `p2_other_bet_open` (for a BACK, every ref of ours, this open's own included; for a LAY, every ref but this open's legs; pilot_gate.py:318-334); R3 (risk.py:286-290); the session ends after any matched leg |
| no Pilot 2 race on a Pilot 1 day | the Pilot 1 day at ARM and per leg; the armed-session file at ARM; the window guards; Pilot 1 cannot arm while Pilot 2 holds the live-owner lock |
| GB/IE horse-racing WIN, LIMIT, LAPSE | the catalogue filter, then `p2_country` on the stored row (the snapshot has no country, schemas.py:18-22), both on Betfair's `event.countryCode` (§4); V9 with config-p2's rules (validate.py:104-124); the request body is LIMIT LAPSE (gateway.py:2011-2017) |
| decide −590 to −575 s, LAY by −530 s, pull at −300 s | the `choose_*` windows on the send book's own time; V9b at 10 minutes (validate.py:124-132); the rest loop (§8) |
| a lone matched leg rides | no hedge, replace or retry code exists |
| no Heartbeat; screen on the delayed key; live key only in a placing session (F46) | A9 and the ban test (tests/test_betfair_boundaries.py:19, :220-227); the screen uses the delayed session; live-key reads happen only in armed `place` opens; the skip streak (a refused BACK, a failed send-book read, or the window missed after the open) |

R10 books a settled race at its worst case, so two matched races can use up the day before −£20 is really lost. That is stricter, never looser.

## 7. The ARM record
- **The record.** `operator_confirm` with `{"echo", "typed_before_lock": true, "p2_arm": {session_id, config_sha256, utc_day, until_ns, races, code_commit}}`. Its payload is not shape-checked and the fold ignores it, so a linear scan finds it.
- **Before Yu's line,** `arm_problems` runs, read only (page, Arming a session). Besides config-p2's pins it runs config-p2's static arming for `place` (A1–A13: the set each race's open runs at off −740 s; it reads Keychain item presence and the anchor, no secret, and makes no Betfair call), keeping A7, which also refuses a journal whose last event is ahead of the wall clock. It also runs the day's room: the BACK's own pair pre-check on ARM's fold, for `daily_loss` alone (`p2_pair_precheck:daily_loss`), since the minute and hour windows can clear before a race and the UTC day cannot. N must be 1 until one race has had both legs placed (`p2_lp.pair_placed`: a `p2-lp` LAY with a bet id). A check that raises instead of answering (the static set opens the journal and reads its anchor outside its own try) refuses `p2_arm_check_failed:static` or `:daily_loss`, so after the line the refusal releases the admission like any other. A refused ARM writes no record.
- **After Yu's line,** in this order:
  1. the stop flag;
  2. `live_ownership` with owner `p2-session:<hex8>` (admission.py:628), which holds the live-owner lock and the recording admission for the whole session;
  3. the checks again, the same as before the line, on a fresh view, whose commit must be the one the banner showed (`p2_code_changed`); `p2-run` loads the order path (gateway.py) before the banner's git read, so the commit recorded is the code that places;
  4. the session credentials;
  5. caffeinate;
  6. the ARM record, compare-and-appended on the view's head while holding `.gateway.lock` (released before the first open, as Pilot 1 appends `session_start`, pilot_run.py:1074-1088), then `publish_anchor`;
  7. the delayed read session.
- **What each leg checks.** That exact record: this config's sha256, this session, today, before UNTIL, and no earlier P2 ARM record today (§6). Only `p2-run` builds a gateway with the Pilot 2 context, and only after the line. This is friction, not cryptography (betfair-order-path.md:243).
- **What Pilot 1's unchanged code does with Pilot 2's records.** Read on main; pinned by the untouched tests before PR-2 merges (§14).
  - Pilot 1's fold handles only notes keyed `pilot` (pilot.py:1941). Pilot 2 writes no note of its own, so its "another contract" refusals (pilot.py:2010-2016; pilot_run.py:758-762; pilot_gate.py:291-306) never fire.
  - An intent whose source is not `pilot` sets a review stop if a Pilot 1 session is armed, and is otherwise ignored (pilot.py:2376-2383).
  - An open or unsettled bet whose source is not `pilot` halts a Pilot 1 placement open as `manual_bet_open_during_session` (pilot_gate.py:318-334; gateway.py:1422-1425), and refuses Pilot 1's ARM (pilot_run.py:909-910).
  - Pilot 1's own-scope kill sweeps Pilot 2's legs through journal bet ids and the refs it has seen (gateway.py:3095). With an untrusted journal it sweeps by Pilot 1's config refs only (gateway.py:3088-3091).
  - A core HALT, kill or resume from Pilot 2 is a core event, so it stops Pilot 1 too (pilot.py:2410, :2514-2528).
  - Journal payloads are validated on append only (journal.py:199-219, :296), so Pilot 1's code can read `pilot_lp` intents (*Inference*).
- **Recording admission.** The bot holds one admission for its whole session, as `pilot-run` does (pilot_run.py:1005-1015, :453-468), so both legs rest under it. A typed live `place` takes a fresh admission and is refused while one is held (cli.py:1518-1527; admission.py:667-670). Release is refused while any order is still open (admission.py:410-435). A crash leaves the admission held; Yu's existing `recording-release` clears it.
- **Code identity.** code_hashes covers every betangel_research/*.py and betangel_research/betfair/*.py file (receipts.py:121-125). Pilot 2's code therefore changes the digest of any later Pilot 1 checkout taken from main. Hence the merge order in §14.

## 8. Rest, pull, handoffs and settlement
**One race:**
```
IDLE     armed, no lock; markets from one delayed catalogue call (§4); 1 s slices to off -840 s
  |-- the day's list unreadable (markets_read_failed) or empty -> END: nothing sent, no second ARM that UTC day
  |-- stop flag -> END | KILL file -> END
  |-- off -780 s passed, checked before the wait and again after it (a stall or sleep) -> next market (missed_window)
SCREEN   off -840..-780 s, delayed session: a fresh row, stored; one delayed book
  |-- off moved, read error, screen code ----------------------------> next market (no live-key read)
PRE-OPEN stop flag -> END | KILL file -> END | login cap -> next market | admission broken -> END
  |-- off -575 s passed, checked again after the wait (a stall or sleep) -> next market (missed_window)
OPEN     off -740 s: gateway "place" with config-p2.json: lock, login, startup reconcile, A1-A19, anchor
  |-- raised (not armed, Pilot 1 window, lock busy, login error, signal) -> END, nothing sent
  |-- open report not armed (a foreign order journals a HALT and fails A17) -> close, END
WAIT     until off -590 s, 1 s slices: stop, KILL file, a kill event, an unreadable journal, any journal event after our head, drift, a late wake -> close, END
  |-- off -575 s passed when it returns (a slow open), checked again -> close, next market (missed_window, in the streak)
BACK     lp_place("BACK"), book observed off -590..-575 s
  |-- a stop during its funds or book read: the guard's SignalStop, or p2_stop_before_send at its pair check
  |       (before any intent) -> close, END (stopped), no race
  |-- refused with a code (the pair pre-check's included), or by a V-, R- or A-rule -> close, next market
  |-- raised with nothing submitted: not armed or the Pilot 1 window -> close, END; book fetch failed -> next market
  |-- 5 opens in a row with no order sent (refused, book fetch failed, missed_window after the open) -> END (skip_streak)
  |-- FAILED (rejected, or not sent) -> close, next market (a race once its submit is journaled)
  |-- UNKNOWN -> in-line reconcile (<= 40 s): found -> placed | else -> close, HANDOFF(BACK), END
BETWEEN  stop flag, KILL file with no kill event, a foreign event, drift -> PULL(BACK), END
         a kill event, or the journal unreadable -> HANDOFF, close, END (never race the kill)
LAY      lp_place("LAY"), book observed by off -532 s; a stop since BETWEEN's look refuses it at its pair check
         (before any intent) or, if later, at the first byte (p2_stop_before_send)
  |-- refused (p2_book_moved included) or FAILED -> PULL(BACK) at once -> BACK matched or a stop ? END : next market
  |-- raised with no LAY submitted -> PULL(BACK) at once -> BACK matched or a stop ? END
  |       : book fetch failed ? next market : END (lay_raised:<code>)
  |-- raised with the LAY submitted -> EXCEPTION
  |-- UNKNOWN -> reconcile: found -> REST | else -> PULL(BACK), HANDOFF(LAY), END
REST     until off -300 s, 1 s slices, no Betfair call; first one line: "<market>: BACK and LAY placed; resting, pull due HH:MM:SSZ"
  |-- stop flag, KILL file with no kill event, a foreign event, drift, a wake > 5 s late -> PULL now, END
  |-- a kill event, or the journal unreadable -> HANDOFF both legs, close, END
PULL     first the kill check: a kill event, or the journal unreadable -> HANDOFF, close, END
         then a whole cancel of each EXECUTABLE leg, LAY first (in-line reconcile); then read every leg
  |-- complete, nothing matched -> close, next market
  |-- complete, a leg matched -> close, END (prints the settle-market line, to type on the race's own UTC day; ARM refuses until settled)
  |-- a cancel raised, an UNKNOWN left, MARKET_SUSPENDED, a part still EXECUTABLE -> HANDOFF, close, END
  |-- a leg Betfair cancelled during the rest (a non-runner): its cancel UNKNOWN -> HANDOFF for that leg, close, END
EXCEPTION after a submit: if it did not come from the PULL, one best-effort PULL (kill check first, no retry);
         HANDOFF for each leg not shown complete (by ref and market when no bet id is known);
         the lock is released; END, exit 5
GONE     SIGKILL, SIGSTOP, power loss, a sleep through the off: nothing runs; bounded at £10 and LAPSE;
         the next ARM refuses while a leg is open or unresolved
```
- **Printed codes.** Each market's line carries its end and, in brackets, its skip or LAY code, as `<market>: <end> (<note>)`: a skip's code, `BACK failed <code>`, or `LAY not placed <code>` or `LAY unresolved <code>`. A LAY that is not placed is counted as `lay:<code>`. Codes only: an exception prints its type's name, never its text.
- **The day's first read.** The ARM record comes before the delayed session's first login and the day's list (§7), so a failed first read, or an empty list, uses the day's one session with nothing sent. Whether a failed first read should use the day is open for Yu (plan, Open for Yu; the lab: keep).
- **Network loss.** A send not sent is FAILED; one after bytes is UNKNOWN (above). Nothing is called during the rest. A cancel exception leaves `cancel_outcome_unknown` or `cancel_not_processed` (gateway.py:2297-2310), which means a handoff.
- **Signals.** Before an open's first send, the gateway guard raises SignalStop (cli.py:209-213). After it, signals are deferred (cli.py:215-218) and the next slice turns them into a pull.
  - A deferred signal, one a mask is still holding (`signal.sigpending`), or the stop flag refuses a leg twice over (`p2_stop_before_send`): first at its pair check (`_lp_check`, after its funds and book reads, before any intent), then at its first byte (checked after the KILL file). So a stop between the legs, or during a leg's reads, does not let the leg go, and nothing of it is journaled; a BACK refused at its pair check is no race and the session ends `stopped`. What is left is a signal that lands after the gate has answered: the LAY goes and is pulled at the next slice.
  - A stop in the milliseconds between the pair check and the first byte (a Ctrl-C held by the send's own mask, or Ctrl-\ or Ctrl-Z) refuses the leg at its first byte. The leg is FAILED `transport_not_sent`, and a BACK refused there counts as a race, as any FAILED BACK does. A test runs the held Ctrl-C for each leg; for Ctrl-\ and Ctrl-Z in that window this is *Inference*.
  - A stop signal held by a mask the process started with, so never delivered, counts as delivered: the first leg's check sets the stop flag, and the session ends there instead of refusing leg after leg.
  - Ctrl-\ (SIGQUIT) and Ctrl-Z (SIGTSTP) set the same stop flag as Ctrl-C (`p2_run.StopFlag`, a subclass; pilot_run.py is unchanged), so the next slice pulls and the end names the signal. Inside the BACK's reads all three keys end the session with nothing journaled of the BACK (tests/test_betfair_p2_run.py, Stops). They reach the whole foreground process group, so the same key also stops or ends caffeinate; after Ctrl-Z the end's wait for caffeinate may run to its 5 s (*Inference*, not tested). SIGKILL and SIGSTOP cannot be caught (GONE).
  - A second Ctrl-C during the pull is deferred too.
- **Clock drift** (wall and monotonic time apart by more than 2 s) is measured from one baseline a race, taken after the screen-window check, so a wall-clock step in the wait for the open, in the open or between the legs is seen at the next look (WAIT, BETWEEN or REST), not only one inside a single wait. Nothing compares the Mac's clock with Betfair's: every window compares Betfair's start time with the Mac's wall clock, so a clock that is steadily wrong moves every step with it. A check against Betfair is open for Yu (plan, Open for Yu).
- **After a sleep.** The pull's first cancel, the LAY's, can go down a connection that died in the sleep. The transport reuses a kept-alive connection by monotonic idle time, which stops while the Mac sleeps (transport.py:420-422), a cancel gets one attempt (:487), and bytes written before a lost connection make its outcome unknown (:568-570). Expect the handoff, with the LAY possibly still open, and cancel it by hand. The connection is reused only within 170 s of awake time (`http.idle_reconnect_s`) and the rest is about 290 s, so this needs a sleep that starts in the first 170 s of the rest, against the banner's lid-open rule. *Inference:* whether the socket is dead after a wake cannot be shown offline.
- **KILL file.** R1 and the first-byte gate refuse any send (risk.py:259-283; gateway.py:1045-1046). Cancels stay allowed (risk.py:623-631), so with no kill event the bot pulls its own legs at the next slice, on the session it holds, with no Keychain read. With a kill event it hands off, because a duplicate cancel only leaves an UNKNOWN (gateway.py:2156-2157).
  - The kill journals its kill and halt events right after the KILL file, before its config, Keychain and login (cli.py:2101-2102), so a kill normally reaches the bot as a kill event. If that append fails (no live journal, an empty one, or one busy or unreadable), the bot sees the KILL file alone and pulls, and its cancels may overlap the kill's sweep: an UNKNOWN for reconcile, and never new exposure, since a cancel adds none.
  - The bot never reads `kill_incomplete`: once it has seen the kill event it has handed off and ended, even if that kill then ends incomplete. Pilot 1's executor waits up to 30 s and cancels in that case (eng §8). The page lists the difference (What it relaxes, item 17), and the wait-and-pull is not built (plan, Open for Yu).
  - So for a running session, Yu chose on 2026-11-10 that lab-Claude's first stop is the KILL file alone, and the kill only if the bot is gone (page, Who does what, with its *proposed* reading of "gone"). Tests pin the kill event's handoff with no cancel, and the KILL file's pull (tests/test_betfair_p2_run.py, Stops).
- **Partial fill at the pull.** A whole cancel short of the remainder is UNKNOWN `cancel_size_short_refresh_needed` (lifecycle.py:283-288), then reconciled in line.
- **The pull and the three-UNKNOWN HALT.** The rest makes no Betfair call, so a leg that matched still looks EXECUTABLE and is cancelled. Fully matched gives BET_TAKEN_OR_LAPSED, UNKNOWN (lifecycle.py:296-297); part matched gives `cancel_size_short` (:286-288). Each such report counts toward the HALT once `unknown_before + 1 ≥ 3` (gateway.py:1190-1192, :2294, :2310, :2323), and only a clean report with moves resets the count; a reconcile does not (journal.py:932-933). A round trip adds 2. So one earlier UNKNOWN since the last clean report makes the BACK's cancel HALT the shared journal, and Pilot 1 is blocked until RESUME LIVE. A race whose legs both matched in the rest leaves the count at 2 (a lone matched BACK leaves 1; a lone matched LAY 0, because the BACK's clean cancel follows). The next unknown outcome of any order, Pilot 1's or a typed one, then HALTs at once. The next clean order report resets the count; settle-market and RESUME LIVE do not. Those counts after a round trip are the review's probe; the gateway test pins the HALT after one earlier UNKNOWN. The safe direction; it costs sessions.
- **A suspended market at the pull.** Nothing matches while it is suspended, and LAPSE acts at the turn in play (`turnInPlayEnabled` was screened). *Unverified:* whether Betfair refuses cancels on a suspended market, and the shape of its cancel report there. `Race.test_suspended_at_pull` (tests/test_betfair_p2_run.py) runs three shapes. Each ends with both legs EXECUTABLE after the in-line reconcile, one cancel each, the handoff, `pull_incomplete` and exit 5:
  - MARKET_SUSPENDED on the execution report, with no instruction reports: no HALT, and nothing added to the UNKNOWN count;
  - instruction reports with ERROR_IN_ORDER: each cancel report is UNKNOWN, so two toward the three-UNKNOWN HALT (count 2), no HALT;
  - instruction reports that carry MARKET_SUSPENDED themselves: the error table has that code for the execution report only, so on an instruction it is `unrecognized`, a HALT category. Each cancel report journals a HALT (halt reasons `unrecognized` twice; count 2), and Pilot 1 waits for RESUME LIVE too.

  The review's probe also found an instruction-level MARKET_NOT_OPEN_FOR_BETTING as quiet as the first shape, and instruction reports with no code counted as the second; the test does not run those two. The error table does not change here: it is shared with Pilot 1's place path, and adding the code would remove a HALT. The first suspension met live shows which shape Betfair sends. The handoff's last line covers the case.
- **A runner withdrawn while the legs rest** (a late non-runner, our own runner removed, or the race abandoned). *Unverified:* Betfair's rule (by the lab's reading of the Exchange Rules, unmatched lays are cancelled when a non-runner's reduction factor is 2.5% or more, and both legs when our runner is removed or the race abandoned; no facts-register row), and how Betfair lists a bet it cancelled itself. The rest makes no Betfair call, so the bot is not told. At the pull the gone leg's cancel comes back BET_TAKEN_OR_LAPSED, UNKNOWN (lifecycle.py:296-297), and the in-line reconcile cannot show the bet gone under the live evidence rule. `Race.test_leg_cancelled_by_the_exchange_in_the_rest` cancels the LAY at off −400 s and runs three listings:
  - not listed: the LAY ends ABSENT_UNCONFIRMED (BET_TAKEN_OR_LAPSED), and the BACK's cancel goes 15 s late, at off −285 s; the next ARM refuses `order_absent_unconfirmed`, `market_unresolved` and `p2_other_bet_open`;
  - listed as cancelled: the LAY stays EXECUTABLE (`exchange_reduction_withheld_until_settle`), and the BACK's cancel goes on time; the next ARM refuses `order_executable` and `p2_other_bet_open`;
  - only a cleared row: the LAY stays EXECUTABLE (`cleared_row_bet_exists_worst_case`), and the BACK's cancel goes at off −285 s; the next ARM refuses `order_executable` and `p2_other_bet_open`.

  In each: two cancels, the LAY first; the BACK cancelled in full; nothing of the LAY matched; `pull_incomplete`, exit 5, the recording admission kept; the handoff for the LAY only, although nothing of it is open; no HALT, and the UNKNOWN count back at 0, because the BACK's clean cancel resets it. The ways out are Yu's typed lines, not run in that test: for a leg left ABSENT_UNCONFIRMED, `reconcile --mode live --accept <ref> --confirm-live <token>` and ACCEPT ABSENT; for a leg kept EXECUTABLE, `settle-market` after the result, which books its worst case with nothing matched (ACCEPT is refused for it, and a typed cancel changes nothing); then `recording-release` and RELEASE RECORDING GENERATION N. Both legs gone (our runner removed, the race abandoned) is not tested here; the review's probe gave two UNKNOWNs and no HALT.
- **The handoff** (`handoff_lines`): each leg by bet id or ref, side, £2 at its price, runner and market. Then the Betfair phone app, then the website, then the own-scope `kill` run from Pilot 2's checkout, by its absolute path. A kill from Pilot 1's checkout finds `p2-lp` legs only through `strategy_refs_seen` and journal bet ids (gateway.py:3095; killswitch.py:134-158), and an untrusted journal drops both (gateway.py:3088-3091). Its last line: "If a cancel is refused because the market is suspended, cancel again as soon as it reopens; anything unmatched lapses at the off." With the pull at −300 s, about 3 to 5 minutes are left before the off: the handoff prints after both cancels, and each cancel whose outcome is unknown is reconciled in line for up to 40 s (reconcile.py:53, :371; §9 pins the reconcile values this assumes) before the next cancel or the handoff. With both cancels unknown, the review measured the handoff at off −218 s on the fake clock, before real time-outs add to it.
- **The lock.** During the rest a typed `cancel` waits order_deadline_s + 5 s and then fails with `lock_busy` (eng §8). The banner names the stops that work.
- **Dead-man: none, by choice.** Yu attends and is the dead-man, LAPSE is the backstop, and the £10 race bound holds. The Heartbeat API stays forbidden (A9; F58).
- **After the off.** A late match is not void: if Betfair turns a market in play after the official off without suspending it at the off, bets matched in between stand (Exchange Rules, Part B 1.4, for a market scheduled to turn in play; no facts-register row yet). The bound is the £10 cap and LAPSE at the turn.
- **Settlement** is Yu's typed `settle-market`, one market at a time, as in eng §8, as soon as the market shows CLOSED: a market Betfair no longer returns cannot be settled in v1 (betfair-order-path.md §8). R10 books a loss on the UTC day the settle is typed, so Yu settles on the race's own UTC day. A booking of more than £10 on a later day (a fully matched pair books £2 × the LAY's price, so any LAY above 5.0) makes that day's ARM refuse `p2_pair_precheck:daily_loss` (§7). The printed settle line says so. The session ends after any matched leg, and the next ARM refuses until it is settled.

## 9. Caps in config-p2.json
`config_problems` pins exact values, so loosening one needs a code change and Astra's review. While `IN_FORCE` is False it also returns `p2_not_in_force` first (§1). The £ ones are derived from Yu's money numbers and are Yu's to set at the build consent. All are inside constants.py's ceilings. tests/test_betfair_p2_lp.py states every pin as a literal, and tests/test_betfair_pilot_2_docs.py ties the table below to the committed example config.

| Cap | Value | Why |
|---|---|---|
| max_stake_per_order | £2 | one £2 leg |
| max_liability_per_order | £10 | R5 passes a £2 LAY at exactly 6.00 and refuses 6.20 (risk.py:296-299 uses ">") |
| max_market_exposure | £10 | no hedge credit, so a race's worst case is max(£10, £2) |
| max_total_exposure | £20 | equals the day stop; £10 instead is open for Yu (plan, Open for Yu) |
| max_daily_loss | £20 | R10 checks the day stop, counting open liability |
| external_exposure_tolerance | £0 | R12 halts on any exposure the shared journal cannot explain |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 races × 2 legs = 10 a day |
| max_transactions_per_hour | 300 | R7's cap; the pair pre-check does not model it, so it is pinned |
| Instructions per request | 1 | as Pilot 1 |
| max_price_deviation_ticks (V13) | 3 | admits a 4-tick spread's legs and nothing wider; Pilot 1's stays 2 |
| max_book_age_s | 2.0 s | as Pilot 1 |
| max_minutes_before_off | 10 | V9b checks it at each leg's send |
| orders.reconcile poll_interval_s, window_s, grace_s, min_successful_polls | 1.0 s, 15 s, 5 s, 3 | the in-line reconcile's 40 s that the state machine's time budget assumes (§8); wider values would let it block for many minutes |
| http timeout_s, order_deadline_s | 10 s, 10 s | each send and read within the same budget |
| read retry_backoff_s | 1, 4, 16 s | each read's retries within the same budget |

- It also pins: `orders.customer_strategy_ref` `p2-lp` with no `orders.pilot` (so the order ref is `p2-lp`, config.py:436-441); rules WIN and event type "7"; `heartbeat_api_enabled` false; `read.app_key_slot` "delayed".
- **Yu's hand edits,** all made before Astra's acceptance (page, Three consents): `credentials.key_path` and `credentials.key_passphrase_in_keychain` as in config.json; `coexistence.bet_angel` as in config.json; the eight acknowledgements; `operator_attests_uk_egress`; and `orders.live.armed`, last. Whether armed goes back to false until the consent is open (plan, Open for Yu).
- **The hash.** The token, the banner, the ARM record and the go-live consent use `config_sha256` of the validated config merged over the checkout's DEFAULTS (config.py:132-151, :452-454; on this branch `load_config_p2` uses the same merge). `init-config --p2` prints it, also for a file that exists (status `config_exists`). It covers every hand-set field, armed included, and it moves with a default the file leaves out, so it is read from a checkout at the commit the consent names. It is not `shasum` of the file.
- Pilot 1's config.json is unchanged.

## 10. Results: counts and P&L
This replaces the ledger, the classes, the verdict and the tables.
- **`p2-summary --mode live`, offline,** from a read-only journal read, for `p2-lp` orders, per UTC day and in total:
  - sessions (ARM records), races and races left;
  - legs sent, placed and refused, by side, with the refusal codes: the orders' error codes, so `risk_refused` for any R- or V-rule refusal; the rule itself is named only in the session's print;
  - legs matched in part or in full, and, by side, the legs matched in their own place report (the plan's taker fills, printed as "in the place report BACK b, LAY l"). A leg first found matched by a reconcile or at the pull is not counted there, and no fill time is recorded;
  - round trips, lone BACKs and lone LAYs;
  - legs cancelled at the pull, lapsed or unresolved;
  - legs that may still rest (unresolved or EXECUTABLE), with the handoff text. A market whose open legs are matched, with nothing resting, gets one line instead: nothing to cancel, and `settle-market --market <market>` after its result (the line names no token or flag; the page's first-session list gives the whole command);
  - the worst case booked at settle;
  - for each market with a matched leg, settled or not, each leg's matched size and average matched price (the journal's, in hundredths, rounded against us), and what was cancelled or lapsed, with no bet id and no ref, for Yu's website check. For a leg filled at several prices, the average can differ in the last digit from the website's.
- **`p2-run`'s end** also prints that session's screens, flags and skip codes, and its codes include `lay:<code>`. Each market's line carries its code at once (§8). They are printed, not journaled.
- **`--cleared`** uses the delayed read key, and refuses (`p2_cleared_needs_delayed_key`) before any Keychain read if config-p2's `read.app_key_slot` is not "delayed". It logs in, so it needs `--bet-angel-paused` while config-p2's coexistence is unverified. It reads cleared orders, SETTLED, from the first P2 ARM, ref `p2-lp`, grouped by MARKET (api.py:844-884), and gives Betfair's profit and commission per race and in total. A figure within a millionth of a pound of a penny is read as that penny, as reconcile reads Betfair's doubles. A journal market not cleared, or a cleared market not in the journal, prints "check the website".
  - Commission is documented at MARKET grouping (Betting Type Definitions, ClearedOrderSummary.commission; Betting Enums, GroupBy MARKET), not yet observed (F37).
  - Betfair's cleared history is about 90 days (F35); whether a from date reaches further is unverified. Yu runs `p2-summary --mode live --cleared` once each race has settled and keeps that figure. A market settled more than 90 days earlier may print "no cleared figure", and Yu then compares it with the figure kept earlier.
- A disagreement with Yu's website check blocks the next ARM until Astra has looked; that is Yu's procedure. Agents relay counts and codes only, never the matched sizes and prices, which are for Yu's website check.

## 11. Final execution, classes and value
None. Cut on Yu's choice of 2026-11-09 (§16; page, Cut from the page as first drafted).

## 12. Verdict, tables and reporting
No verdict and no tables. Reporting is §10's counts and Betfair's figures. Nothing enters Git without Yu's approval of the exact fields.

## 13. Still forbidden
eng §13's bans stay, except that this bot may send lays. Still: more than one instruction per request in the bot's own sends and cancels (the kill's existing sweep, killswitch.py:182-185, is the exception), a partial cancel, Heartbeat, PERSIST, BSP orders, a second race in a market, a replace, a retry or resend, a second pull, any action on Pilot 1's or manual bets other than the kill's sweep of all our refs (§2), and agents typing any line.

## 14. Stages, gates and tests
- **Merge order (Yu, 2026-11-04, relayed: "2.是"; confirmed by Yu to lab-Claude on 2026-11-06; kept on 2026-11-10).** The order of every PR is in the plan (Stages).
  - No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit.
  - The order path (PR-2) merges only after Pilot 1's PT4 receipt exists, so the shared placement path is first proven live on Pilot 1's simpler shape.
  - Gate A's amendments 3 and 4 (PR-G, #60) merged on 2026-11-10, before any lab count was read, and the code merges only after Gate A passes.
  - Every Pilot 1 test passes unchanged at every Pilot 2 commit, and contract p1's sha256 (4dbccb5a…) is unchanged.
- **Pilot 1 untouched** (tests/test_betfair_p2_untouched.py). PR-1 has its two pure checks: an open or unsettled Pilot 2 leg is a Pilot 1 manual ref, and the P2 ARM record is no pilot note. PR-2 adds the rest, which need its journal and gateway:
  - the sha256 at c86764a of pilot.py, pilot_run.py, pilot_gate.py, p1-fav-t5.json and all 18 of Pilot 1's test and support files, 22 names in all (pilot_run.py as PR #59 left it, and tests/test_betfair_pilot_gateway.py as PR #63 left it, sha256 059d46a3…, where c86764a's was 8ac842f0…; both PRs merged on 2026-11-10, and PR-2's pins of the two are made again when it is rebased); the sha256 at c86764a of the core modules Pilot 2 relies on unchanged (risk.py, reconcile.py, arming.py, lifecycle.py, validate.py, killswitch.py) and of the four other test support modules, which PR #59 does not change; and a check, by pattern, that those names are every Pilot 1 test file, every test support module and everything under research/pilots/;
  - Pilot 1's `pilot.fold` over a journal with Pilot 2's intents, submits, reports and ARM records, but no HALT, kill or resume, equals its fold without them; and a Pilot 2 HALT stops Pilot 1;
  - an open Pilot 2 leg halts a Pilot 1 open and refuses Pilot 1's ARM.
- **Tests,** offline, with the existing fakes, `block_network` and zero `/usr/bin/security` calls:
  - tests/test_betfair_p2_lp.py: the pure rule (band edges, leg prices, the passive check, the screen, the windows, each refusal code in order, `p2_book_moved`, the counts, the ARM line, `config_problems`, the handoff, the summary, its imports); the pair's room (`pair_room`) against risk.py's R6 for two orders and R11 for both legs; the first placed pair (`pair_placed`); every config-p2 pin stated as a literal and each loosened alone; `_pence` against reconcile's rule for Betfair's doubles; the summary's matched-leg lines and the legs matched in their own place report; and the constants against this page's Values and Money tables;
  - tests/test_betfair_p2_gateway.py: the Pilot 2 context in paper and with Pilot 1's config, a guard built on another config, `place()` refused on it, a pending stop refusing either leg before any intent, the ARM record, the request body, the check on the submit's basis, the pair rules, the LAY refused when the best back fell, the pair pre-check's funds and R6 room for both legs, R5, R8 and R10 with settle-market, R12 at the LAY, Pilot 1's own matched, unsettled bet refusing a Pilot 2 BACK, the race counts, own-leg cancels, the kill's extra ref, and the three-UNKNOWN HALT after a round trip;
  - tests/test_betfair_p2_run.py: a test for most §8 edges (among them each stop key inside the BACK's reads, a Ctrl-C held by each leg's send mask, and a stop held by the inherited mask), the ARM refusals (config-p2's static arming, the day's room, a check that raises and SSH among them), N at 1 until one race has had both legs placed, caffeinate, the ARM record under the lock, and the owner lock blocking `pilot-run` and a manual `place`. Edges with no test yet, as its docstring lists them: a list row that cannot be read, or the screen's own row missing; the handoff when the legs cannot be listed at all, and a pull that raises inside the EXCEPTION edge; a stop between the Keychain read and the ARM record; an admission refusal at the owner lock; and a terminal that is lost;
  - the switched-off default (§1): `IN_FORCE` False in the committed code, `config_problems` and the gateway refusing, and `p2-run` refusing with no Keychain, journal or git read; the tests of the live path patch it True;
  - tests/test_betfair_p2_cli.py: `init-config --p2`, `load_config` refusing config-p2.json, the kill's refs with and without it, the kill ending exit 3 before its sweep when `p2_lp` cannot be imported, and `--bet-angel-paused` for `p2-run` and `p2-summary --cleared` while coexistence is unverified. `p2-summary` is tested in tests/test_betfair_p2_run.py.
- **Existing test files that change:** tests/test_betfair_backtest_boundaries.py (two data lines for `p2_lp` and `p2_run`), which, like the api.py change, needs Yu's OK before the PR carrying it merges (plan, Open for Yu); and tests/test_lp_rate_count.py, changed in PR-G (#60); Yu gave the OK for that change on 2026-11-10 ("OK, merge it (Recommended)"), and it merged that day. tests/test_betfair_pilot_2_docs.py is added by #58, which merges before PR-1 (plan, Stages); PR-1 then adds the tests that tie the page's £ caps row and this note's §9 table to the committed example config, so by the time PR-1 merges it is an existing test file that PR-1 changes, and it is listed for Yu's OK with the others (plan, Open for Yu).
- **P2-PT3,** the fault drills, are tests/test_betfair_p2_run.py, inside the offline suite only. No drill runs against the real state root, the Keychain or Betfair, because the journal anchor's Keychain item is keyed by its service and the profile alone (credentials.py:37, :131, :150), so a scratch run that reached it would overwrite Pilot 1's anchor.
- **Go-live prerequisite.** A19 needs a `ref_roundtrip_verified` event in the lab Mac's journal before any Pilot 2 open arms (arming.py:53, :477-478). The lab Mac's one-pound probe (T9) passed on 2026-11-10 and set it in the lab Mac's fresh journal.

## 15. How it ends
As on the page. `p2-summary --mode live` prints the counts, each matched leg's size and average price, and Betfair's figures; there is no data lock and no recomputation.

## 16. Left out
Not needed for real-money safety, Yu's limits, Pilot 1's isolation or the lab rules. Each cut protection needs Yu's yes in plain words at the build consent.
- **Registration and identity:** the frozen contract, registration, PA1–PA4, the code-hash digest and the process pin. The commit is shown and a clean tree required instead.
- **Journal records and stops:** a Pilot 2 fold and notes, a fault acknowledgement line, Pilot 2 stops, a session-end record, and journaled SKIPs and screens.
- **Results machinery:** the ledger, classes, value at BSP, the verdict, the tables, capture and attestations, and own P&L arithmetic. Betfair's cleared figures and Yu's website check replace them.
- **Failure extras:** the auto-kill child, the suspended-market poll and second cancel, any retry of a failed pull, and a wait-and-pull after a kill that ends incomplete (page, What it relaxes, item 17).
- **Drills and dry runs:** the probe sessions, a stand-in transport, a dry mode and dry day (P2-PT2), and an end-session command. `p2-summary` and `recording-release` cover a crash.
- **Commands and code:** a separate Pilot 2 kill, a new arming row, any risk.py change, and the battery check (caffeinate stays).
- **Policy rows not set in code:** the final date (2027-06-30, proposed) and pausing.
- **Not adopted:** the Heartbeat API (banned), the order stream, cancel by market, and two-instruction requests.
- **Gate B:** withdrawn. It counted spreads in Pilot 1's −300 s decision books, and the bot now decides at −590 s.
- **Deferred:** a dead-man or watchdog, and mark-outs during the rest.
- **Unverified:** whether Betfair refuses cancels on a suspended market, and which cancel report it sends there (§8); Betfair's rule for a non-runner's unmatched lays, and how Betfair lists a bet it cancelled itself (§8); whether commission is filled when grouped by MARKET (documented, not yet observed; F37); the £1 roll-up and the GB country default, documented but not yet observed (§4); F73 on the lab Mac: macOS asked once at the T9 probe, Yu chose Always Allow for the live key on 2026-11-10, and a kill from lab-Claude's shell with nobody at the screen is not yet tested.
