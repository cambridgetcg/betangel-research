# Pilot 2 engineering note: how the plain capped bot would keep the page's promises

Status: proposed, for Astra's review. Nothing here is merged. It describes the code that PR-1 and PR-2 of the [plan](betfair-pilot-2-plan.md) would add, against main c86764a. The [Pilot 2 page](betfair-pilot-2-policy.md) wins on any difference, and RS-19 and its [engineering note](betfair-pilot-engineering.md) ("eng") stay as they are.

For Astra, Yu and the Air's Claude. Author: lab-Claude, 2026-11-05; rewritten on 2026-11-09 for the plain capped bot Yu chose that day, from the lab's build spec of the same day.

- The sections follow eng's numbering. A section says only what differs. Where it is silent, eng applies to Pilot 2 as written, with "pilot" read as "pilot_lp".
- File:line citations are to main c86764a.
- *Inference* marks what has not been checked against Betfair or run. *Unverified* marks what nobody could confirm.

## 1. What it amends
Nothing, until Pilot 2's go-live. The lines Pilot 1's documents would gain then are listed on the page, under Changes to Pilot 1's documents.

## 2. Scope: inside Pilot 1's profile
- **Identity.**
  - No contract. Two new modules: `p2_lp.py`, pure (the constants, the rule, the counts, the ARM line and the summary), and `p2_run.py`, the executor. `p2_lp.py` imports only `pilot` and `ticks`, never the gateway or `pilot_gate`. `p2_run.py` never imports the gateway; cli.py hands it the factories. The names keep them, and their tests `tests/test_betfair_p2_*.py`, out of Pilot 1's `pilot*` file families.
  - customer_strategy_ref `p2-lp`; intent source `pilot_lp`, live only and accepted only through the gateway's new `lp_place`. The public `place()` and its typed line stay byte-identical (tests/test_betfair_pilot_gateway.py:2287-2385 guard that).
  - No record type of its own. Its orders are core intents, and its one ARM record a session is the core kind `operator_confirm` (journal.py:80-82), which the fold ignores (§7).
  - Profile `default`, the same as Pilot 1. A15 refuses a second profile on the same account for place, cancel, reconcile and settle (arming.py:42, :46-62, :385-406, :460-468; F76), and the Keychain credentials are per profile (arming.py:235-239).
  - Its own config file, `<profile>/config-p2.json`, read only by `load_config_p2`. `load_config` still refuses that path (`config_override_not_allowed`, config.py:139-140), so it can never load into a manual command. Pilot 1's config.json, its sha256 and PA3 do not change.
  - Its own checkout. There is no registration: the banner shows the commit, and ARM refuses a dirty tree.
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
  - `p2-summary`, offline counts, with `--cleared` for Betfair's figures (§10);
  - `init-config --p2`, which writes research/betfair-config-p2.example.json to config-p2.json with O_EXCL (as cli.py:889-905);
  - the existing `kill`, which adds `p2-lp` to its sweep only when config-p2.json exists. That check sits in its own try, and an OSError counts as present, because any exception in that block ends the kill `kill_incomplete` before a cancel (cli.py:2152-2164).
- There is no register, end-session, ack or separate kill command for Pilot 2.
- **One pilot session per UTC day across both pilots.** ARM refuses on a UTC day with a Pilot 1 `session_start` or a P2 ARM record, while a Pilot 1 window is open, or while Pilot 1's armed-session file exists (pilot_run.py:350). Each leg checks the Pilot 1 day and the ARM record again (§6). Sends are refused while a Pilot 1 session's window is open (gateway.py:745, :1942-1944).
- Pilot 1's code is unchanged, so nothing on Pilot 1's side refuses a Pilot 1 session after a Pilot 2 session on the same UTC day. That half of "one pilot session a day" is Yu's rule.
- **Caffeinate.** `/usr/bin/caffeinate -i -s -w <pid>`, started at the ARM and ended at the session's end. It is process-scoped and changes no setting. Mains power and the open lid are Yu's procedure; there is no battery check in code.
- Typed lines are Yu's, as in eng §3.

## 4. The decision
- **Markets.** One delayed catalogue call: GB and IE, event type 7, WIN, with MARKET_START_TIME and MARKET_DESCRIPTION. Each row must have bettingType ODDS and `turnInPlayEnabled` true (`schedule_row_ok`). A market whose screen window has passed is skipped (`missed_window`). A market whose off −300 s is after UNTIL is left out, as the gateway would refuse it (`p2_until_before_pull`).
- **Screen,** off −840 to −780 s, on the delayed session only.
  - One fresh catalogue row by market id with Pilot 1's row projections, stored with `store_catalogue_row` as Pilot 1 does (api.py:777-787; gateway.py:1846). Every send book takes its `market_time` and the country from this row, not from a cache up to 6 h old (config.py:46).
  - One delayed `market_snapshot`. `screen` needs `isMarketDataDelayed` true (else `not_delayed`), then `book_problems` and `select_runner`.
  - The off moved, a read error or a screen code: the next market, with no live-key read (F46).
- **Before the open:** the stop flag or the KILL file ends the session; the login cap moves to the next market; a broken admission ends the session.
- **Open,** off −740 s: the gateway command `place` with config-p2.json and the Pilot 2 context. The lock, the login, the startup reconcile, UNKNOWNs resolved within 120 s (gateway.py:104, :774-781), A1–A19, the Pilot 1 window guard (gateway.py:745) and the anchor. If `open_report()["armed"]` is not True, close and end (gateway.py:827-829): dynamic failures do not raise (gateway.py:785-788, :810-820).
- **Book problems** (`book_problems`): OPEN, not in play, at least 5 ACTIVE runners; on a live send book also `isMarketDataDelayed` false and `betDelay` 0. The V-rules check neither the bet delay nor the ACTIVE count (validate.py:104-140, :280).
- **Runner** (`select_runner`, on the send book):
  - In band: ACTIVE; best back b and best lay l both on the CLASSIC ladder; b < l; 1.50 ≤ b ≤ 5.90.
  - Eligible: l is exactly 3 or 4 CLASSIC ticks above b.
  - Choice: the lowest b, then the lowest l, then the lowest selection id. A tie is never a SKIP.
  - No depth predicate: both legs rest on new, empty levels.
  - Codes: `no_runner_in_band` and `spread_out_of_band`.
  - Why 3–4 ticks: at 2 ticks the two inside legs land on one price, and at 1 tick they cross; V8 refuses both (validate.py:257-268). A wider spread puts the inside BACK more than 3 ticks from the best back, which V13 at 3 refuses (validate.py:286-290).
- **Leg prices** (`leg_prices`): BACK at l − 1 tick, LAY at b + 1 tick. It raises unless LAY < BACK and LAY ≤ 6.00.
- **Passive check** (`passive_check`), per leg, on that leg's own send book: best back < leg price < best lay, strictly. Our own resting BACK counts: it is the LAY's best lay at l − 1, and b + 1 is still inside the spread. Otherwise `leg_not_passive`, and that leg is not sent.
- **BACK** (`choose_back`): the country from the row SCREEN stored must be GB or IE (`p2_country`; a missing country refuses). The book's `observed_at` must lie within off −590 to −575 s, by its `market_time` (`p2_window`). Then `book_problems`, `select_runner`, `leg_prices` and the passive check. The instruction is £2, LIMIT, LAPSE, handicap 0.
- **LAY** (`choose_lay`): the book observed by off −532 s (`p2_send_deadline`; with the 2 s book age, gateway.py:1032-1034, it leaves by about −530 s), `book_problems` on the live book, the runner ACTIVE, and the passive check for the LAY on this fresh book. The selection and price come from the placed BACK.
- **Virtual prices.** Pilot 2's send books are fetched with virtual prices on. Every other request body is unchanged. This needs a `virtualise` option in api.py, which needs Yu's OK (plan, Open for Yu). Without it, the passive check sees non-virtual prices only. *Unverified:* the field name, against Betfair's PriceProjection documentation.
- **Incomplete pair.** If the BACK was placed and the LAY is not (refused, FAILED, or raised before it was sent), the BACK is pulled at once (§8). If the BACK was not placed, nothing more is sent.

## 5. The first-byte gate
The plain manual first-byte gate, unchanged, for each leg. `lp_place` runs `_place_live` (gateway.py:1283-1307) with a Pilot 2 side set:
- It keeps the token check, `_require_armed`, the manual-during-session refusal and the resolved-at-open refusal (gateway.py:1284-1289).
- It skips V0 and `_take_confirmations` (gateway.py:1290-1292, :1297), because there is no per-order typed line.
- It keeps the funds check first, then fetches the book itself, at most twice (gateway.py:1301-1304).
- A `choose_*` code refuses, with only the funds check journaled.
- Then `_live_arming("place", book)` and `_place_core` with source `pilot_lp` and its one new hook, `lp_check` (§6).
- With no Pilot 2 side, `_place_live` is byte for byte today's.

## 6. Counts and gateway checks
- **No Pilot 2 fold.** Every count comes from the core fold's order records with ref `p2-lp` (journal.py:758-777). `races()` gives the markets with a `p2-lp` order that has a `submitted_at`, so a race counts from its first submit.
- **The check runs on the submit's own basis.** `_lp_check` runs inside `_place_core`, right after the session guard (gateway.py:1942-1944), on the basis read of the submit's compare-and-append. Anything journaled after the check makes the submit refuse (gateway.py:1212-1254). It reads the cached fold, so no full refold runs inside the 2 s book window.
- **Refusal codes,** in this order (`leg_refusal`):
  - the ARM record: `p2_arm_missing`, `p2_arm_other_config`, `p2_arm_other_session`, `p2_arm_not_today`, `p2_arm_expired`, `p2_arm_already_today`;
  - `p2_pilot_1_day`;
  - BACK: `p2_market_used` (any `p2-lp` intent in the market, refused ones included), `p2_until_before_pull`, `p2_races_total`, `p2_races_today`, `p2_races_session`, `p2_other_bet_open`;
  - LAY: `p2_lay_without_back`, `p2_lay_already_sent` (from the basis fold, never an in-memory flag), `p2_lay_not_pair` (selection or price), `p2_other_bet_open`;
  - any leg: `p2_leg_shape` (£2, LAPSE, the side).
- **The pair pre-check,** for the BACK only: the existing `evaluate_pilot_reservation(..., size_p=1000)` (risk.py:544-566), giving `p2_pair_precheck:<rule>`. It models £10 more, so R10 never refuses the LAY alone. risk.py does not change.
- **No leg is lost.** The refs go into the open's set right after `_new_refs` (gateway.py:1948), before any intent, because the submit, the send and the report can each raise after the order went out (gateway.py:2033, :2108, :1172-1174; journal.py:314). `lp_legs()` reads every leg from the fold by those refs; if the fold raises, each ref is still named.

| Limit | Enforced by |
|---|---|
| £2 BACK at l − 1, £2 LAY at b + 1, one runner | `choose_*` with `leg_prices`; `p2_leg_shape`; R5's £2 stake cap (risk.py:292-295) |
| best back 1.50–5.90, spread 3–4 ticks | `select_runner` on the gateway's own fresh send book; V13 at 3 ticks |
| BACK first; LAY only if the BACK was placed | the BACK placed in this open; `p2_lay_without_back` on the basis read; V7 at 1 instruction (validate.py:310); a restart can never send an earlier LAY |
| each leg strictly inside the spread on a fresh book | the passive check inside `choose_*`, on the book `_place_core` judges; V8 keeps LAY < BACK; V10 and A18 at 2 s |
| lay at most 6.0; worst race −£10 | `leg_prices`; R5's liability cap of £10, so 6.0 passes and 6.2 is refused (risk.py:296-299); R8's market cap of £10, with no hedge credit (risk.py:136-154, :367-380) |
| 5 races a day, 10 in all | `races()` on the basis fold; R6 at 10 orders a day as a backstop (risk.py:301-325) |
| day stop −£20 | R10 at £20 (risk.py:327-348, :378-379) and the pair pre-check. settle-market books each bet at its worst case (gateway.py:3017), so one fully matched race at 6.0 books £12 and ends the day |
| one race in flight; nothing else of ours open or unsettled | one process, one open, `.gateway.lock`; `p2_other_bet_open` (every ref but this open's legs; pilot_gate.py:318-334); R3 (risk.py:286-290); the session ends after any matched leg |
| no Pilot 2 race on a Pilot 1 day | the Pilot 1 day at ARM and per leg; the armed-session file at ARM; the window guards; Pilot 1 cannot arm while Pilot 2 holds the live-owner lock |
| GB/IE horse-racing WIN, LIMIT, LAPSE | the catalogue filter, then `p2_country` on the stored row (the snapshot has no country, schemas.py:18-22); V9 with config-p2's rules (validate.py:104-124); the request body is LIMIT LAPSE (gateway.py:2011-2017) |
| decide −590 to −575 s, LAY by −530 s, pull at −300 s | the `choose_*` windows on the send book's own time; V9b at 10 minutes (validate.py:124-132); the rest loop (§8) |
| a lone matched leg rides | no hedge, replace or retry code exists |
| no Heartbeat; screen on the delayed key; live key only in a placing session (F46) | A9 and the ban test (tests/test_betfair_boundaries.py:19, :220-227); the screen uses the delayed session; live-key reads happen only in armed `place` opens; the skip streak |

R10 books a settled race at its worst case, so two matched races can use up the day before −£20 is really lost. That is stricter, never looser.

## 7. The ARM record
- **The record.** `operator_confirm` with `{"echo", "typed_before_lock": true, "p2_arm": {session_id, config_sha256, utc_day, until_ns, races, code_commit}}`. Its payload is not shape-checked and the fold ignores it, so a linear scan finds it.
- **After Yu's line,** in this order:
  1. the stop flag;
  2. `live_ownership` with owner `p2-session:<hex8>` (admission.py:628), which holds the live-owner lock and the recording admission for the whole session;
  3. the checks again, on a fresh view;
  4. the session credentials;
  5. caffeinate;
  6. the ARM record, compare-and-appended on the view's head while holding `.gateway.lock` (released before the first open, as Pilot 1 appends `session_start`, pilot_run.py:1074-1088), then `publish_anchor`;
  7. the delayed read session.
- **What each leg checks.** That exact record: this config's sha256, this session, today, before UNTIL, and no earlier P2 ARM record today (§6). Only `p2-run` builds a gateway with the Pilot 2 context, and only after the line. This is friction, not cryptography (betfair-order-path.md:243).
- **What Pilot 1's unchanged code does with Pilot 2's records.** Read on main; pinned by the untouched tests before PR-1 merges (§14).
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
IDLE     armed, no lock; markets from one delayed catalogue call (§4)
SCREEN   off -840..-780 s, delayed session: a fresh row, stored; one delayed book
  |-- off moved, read error, screen code ----------------------------> next market (no live-key read)
PRE-OPEN stop flag -> END | KILL file -> END | login cap -> next market | admission broken -> END
OPEN     off -740 s: gateway "place" with config-p2.json: lock, login, startup reconcile, A1-A19, anchor
  |-- raised (not armed, Pilot 1 window, lock busy, login error, signal) -> END, nothing sent
  |-- open report not armed (a foreign order journals a HALT and fails A17) -> close, END
WAIT     until off -590 s, 1 s slices: stop, KILL file, any journal event after our head, drift -> close, END
BACK     lp_place("BACK"), book observed off -590..-575 s
  |-- refused with a code, or by a V-, R- or A-rule -> close, next market; 5 in a row -> END
  |-- raised with nothing submitted: not armed or the Pilot 1 window -> close, END; book fetch failed -> next market
  |-- FAILED (rejected, or not sent) -> close, next market (a race once its submit is journaled)
  |-- UNKNOWN -> in-line reconcile (<= 40 s): found -> placed | else -> close, HANDOFF(BACK), END
BETWEEN  stop flag, KILL file with no kill event, a foreign event -> PULL(BACK), END
         a kill event, or the journal unreadable -> HANDOFF, close, END (never race the kill)
LAY      lp_place("LAY"), book observed by off -532 s
  |-- refused, FAILED, or raised with no LAY submitted -> PULL(BACK) at once -> BACK matched ? END : next market
  |-- raised with the LAY submitted -> EXCEPTION
  |-- UNKNOWN -> reconcile: found -> REST | else -> PULL(BACK), HANDOFF(LAY), END
REST     until off -300 s, 1 s slices, no Betfair call
  |-- stop flag, KILL file with no kill event, a foreign event, drift, a wake > 5 s late -> PULL now, END
  |-- a kill event, or the journal unreadable -> HANDOFF both legs, close, END
PULL     first the kill check: a kill event, or the journal unreadable -> HANDOFF, close, END
         then a whole cancel of each EXECUTABLE leg, LAY first (in-line reconcile); then read every leg
  |-- complete, nothing matched -> close, next market
  |-- complete, a leg matched -> close, END (prints the settle-market line; ARM refuses until settled)
  |-- a cancel raised, an UNKNOWN left, MARKET_SUSPENDED, a part still EXECUTABLE -> HANDOFF, close, END
EXCEPTION after a submit: if it did not come from the PULL, one best-effort PULL (kill check first, no retry);
         HANDOFF for each leg not shown complete (by ref and market when no bet id is known);
         the lock is released; END, exit 5
GONE     SIGKILL, power loss, a sleep through the off: nothing runs; bounded at £10 and LAPSE;
         the next ARM refuses while a leg is open or unresolved
```
- **Network loss.** A send not sent is FAILED; one after bytes is UNKNOWN (above). Nothing is called during the rest. A cancel exception leaves `cancel_outcome_unknown` or `cancel_not_processed` (gateway.py:2297-2310), which means a handoff.
- **Signals.** Before an open's first send, the gateway guard raises SignalStop (cli.py:209-213). After it, signals are deferred (cli.py:215-218) and the next slice turns them into a pull. A second Ctrl-C during the pull is deferred too.
- **KILL file.** R1 and the first-byte gate refuse any send (risk.py:259-283; gateway.py:1045-1046). Cancels stay allowed (risk.py:623-631), so with no kill event the bot pulls its own legs. With a kill event it hands off, because a duplicate cancel only leaves an UNKNOWN (gateway.py:2156-2157).
- **Partial fill at the pull.** A whole cancel short of the remainder is UNKNOWN `cancel_size_short_refresh_needed` (lifecycle.py:283-288), then reconciled in line.
- **The pull and the three-UNKNOWN HALT.** The rest makes no Betfair call, so a leg that matched still looks EXECUTABLE and is cancelled. Fully matched gives BET_TAKEN_OR_LAPSED, UNKNOWN (lifecycle.py:296-297); part matched gives `cancel_size_short` (:286-288). Each such report counts toward the HALT once `unknown_before + 1 ≥ 3` (gateway.py:1190-1192, :2294, :2310, :2323), and only a clean report with moves resets the count; a reconcile does not (journal.py:932-933). A round trip adds 2. So one earlier UNKNOWN since the last clean report makes the BACK's cancel HALT the shared journal, and Pilot 1 is blocked until RESUME LIVE. The safe direction; it costs sessions.
- **A suspended market at the pull** leaves the leg EXECUTABLE (lifecycle.py:298-300, :324-329): the handoff. Nothing matches while it is suspended, and LAPSE acts at the turn in play (`turnInPlayEnabled` was screened). *Unverified:* whether Betfair refuses cancels on a suspended market.
- **The handoff** (`handoff_lines`): each leg by bet id or ref, side, £2 at its price, runner and market. Then the Betfair phone app, then the website, then the own-scope `kill` run from Pilot 2's checkout, by its absolute path. A kill from Pilot 1's checkout finds `p2-lp` legs only through `strategy_refs_seen` and journal bet ids (gateway.py:3095; killswitch.py:134-158), and an untrusted journal drops both (gateway.py:3088-3091). With the pull at −300 s, about 5 minutes are left before the off.
- **The lock.** During the rest a typed `cancel` waits order_deadline_s + 5 s and then fails with `lock_busy` (eng §8). The banner names the stops that work.
- **Dead-man: none, by choice.** Yu attends and is the dead-man, LAPSE is the backstop, and the £10 race bound holds. The Heartbeat API stays forbidden (A9; F58).
- **Settlement** is Yu's typed `settle-market`, one market at a time, as in eng §8. R10 books a loss on the UTC day the settle is typed. The session ends after any matched leg, and the next ARM refuses until it is settled.

## 9. Caps in config-p2.json
`config_problems` pins exact values, so loosening one needs a code change and Astra's review. The £ ones are derived from Yu's money numbers and are Yu's to set at the build consent. All are inside constants.py's ceilings.

| Cap | Value | Why |
|---|---|---|
| max_stake_per_order | £2 | one £2 leg |
| max_liability_per_order | £10 | R5 passes a £2 LAY at exactly 6.00 and refuses 6.20 (risk.py:296-299 uses ">") |
| max_market_exposure | £10 | no hedge credit, so a race's worst case is max(£10, £2) |
| max_total_exposure | £20 | equals the day stop |
| max_daily_loss | £20 | R10 checks the day stop, counting open liability |
| external_exposure_tolerance | £0 | R12 halts on any exposure the shared journal cannot explain |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 races × 2 legs = 10 a day |
| Instructions per request | 1 | as Pilot 1 |
| max_price_deviation_ticks (V13) | 3 | admits a 4-tick spread's legs and nothing wider; Pilot 1's stays 2 |
| max_book_age_s | 2.0 s | as Pilot 1 |
| max_minutes_before_off | 10 | V9b checks it at each leg's send |

- It also pins: `orders.customer_strategy_ref` `p2-lp` with no `orders.pilot` (so the order ref is `p2-lp`, config.py:436-441); rules WIN and event type "7"; `heartbeat_api_enabled` false; `read.app_key_slot` "delayed".
- Yu sets `orders.live.armed`, the acknowledgements and the attestations by hand.
- Pilot 1's config.json is unchanged.

## 10. Results: counts and P&L
This replaces the ledger, the classes, the verdict and the tables.
- **`p2-summary`, offline,** from a read-only journal read, for `p2-lp` orders, per UTC day and in total:
  - sessions (ARM records), races and races left;
  - legs sent, placed and refused, by side, with the refusal codes;
  - legs matched in part or in full, round trips, lone BACKs and lone LAYs;
  - legs cancelled at the pull, lapsed or unresolved;
  - open or unsettled legs, with the handoff text;
  - the worst case booked at settle.
- **`p2-run`'s end** also prints that session's screens, flags and skip codes. They are printed, not journaled.
- **`--cleared`** uses the delayed read key: cleared orders, SETTLED, from the first P2 ARM, ref `p2-lp`, grouped by MARKET (api.py:844-884). It gives Betfair's profit and commission per race and in total. A journal market not cleared, or a cleared market not in the journal, prints "check the website". *Unverified:* whether commission is filled when grouped by MARKET (F37).
- A disagreement with Yu's website check blocks the next ARM until Astra has looked; that is Yu's procedure. Agents relay counts and codes only.

## 11. Final execution, classes and value
None. Cut on Yu's choice of 2026-11-09 (§16; page, Cut from the page as first drafted).

## 12. Verdict, tables and reporting
No verdict and no tables. Reporting is §10's counts and Betfair's figures. Nothing enters Git without Yu's approval of the exact fields.

## 13. Still forbidden
eng §13's bans stay, except that this bot may send lays. Still: more than one instruction per request in the bot's own sends and cancels (the kill's existing sweep, killswitch.py:182-185, is the exception), a partial cancel, Heartbeat, PERSIST, BSP orders, a second race in a market, a replace, a retry or resend, a second pull, any action on Pilot 1's or manual bets other than the kill's sweep of all our refs (§2), and agents typing any line.

## 14. Stages, gates and tests
- **Merge order (Yu, 2026-11-04, relayed: "2.是"; confirmed by Yu to lab-Claude on 2026-11-06).**
  - No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit.
  - The order path (PR-2) merges only after Pilot 1's PT4 receipt exists, so the shared placement path is first proven live on Pilot 1's simpler shape.
  - Gate A's amendment 3 (PR-G) merges before any lab count is read, and the code merges only after Gate A passes.
  - Every Pilot 1 test passes unchanged at every Pilot 2 commit, and contract p1's sha256 (4dbccb5a…) is unchanged.
- **Pilot 1 untouched** (tests/test_betfair_p2_untouched.py, in PR-1):
  - the sha256 at c86764a of pilot.py, pilot_run.py, pilot_gate.py, p1-fav-t5.json and Pilot 1's test files, listed by name;
  - Pilot 1's `pilot.fold` over a journal with Pilot 2's intents, submits, reports and ARM records, but no HALT, kill or resume, equals its fold without them; and a Pilot 2 HALT stops Pilot 1;
  - an open Pilot 2 leg halts a Pilot 1 open and refuses Pilot 1's ARM.
- **Tests,** offline, with the existing fakes, `block_network` and zero `/usr/bin/security` calls:
  - tests/test_betfair_p2_lp.py: the pure rule (band edges, leg prices, the passive check, the screen, the windows, each refusal code in order, the counts, the ARM line, `config_problems`, the handoff, the summary, its imports), and the constants against this page's Values and Money tables;
  - tests/test_betfair_p2_gateway.py: the Pilot 2 context in paper and with Pilot 1's config, `place()` refused on it, the ARM record, the request body, the check on the submit's basis, the pair rules, R5, R8 and R10 with settle-market, the race counts, own-leg cancels, the kill's extra ref, and the three-UNKNOWN HALT after a round trip;
  - tests/test_betfair_p2_run.py: one test per §8 edge, the ARM refusals, the first session at 1 race, caffeinate, the ARM record under the lock, and the owner lock blocking `pilot-run` and a manual `place`;
  - tests/test_betfair_p2_cli.py: `init-config --p2`, `load_config` refusing config-p2.json, the kill's refs with and without it, and `p2-summary`.
- **Existing test files that change:** tests/test_betfair_backtest_boundaries.py (two data lines for `p2_lp` and `p2_run`) and tests/test_lp_rate_count.py (PR-G). Each, and the api.py change, needs Yu's OK before its PR is opened. tests/test_betfair_pilot_2_docs.py, which exists only on this PR's branch, changes in this PR.
- **P2-PT3,** the fault drills, are tests/test_betfair_p2_run.py, inside the offline suite only. No drill runs against the real state root, the Keychain or Betfair, because the journal anchor's Keychain item is keyed by its service and the profile alone (credentials.py:37, :131, :150), so a scratch run that reached it would overwrite Pilot 1's anchor.
- **Go-live prerequisite.** A19 needs a `ref_roundtrip_verified` event in the lab Mac's journal before any Pilot 2 open arms (arming.py:53, :477-478). Pilot 1 going first on the lab Mac probably covers it.

## 15. How it ends
As on the page. `p2-summary` prints the counts and Betfair's figures; there is no data lock and no recomputation.

## 16. Left out
Not needed for real-money safety, Yu's limits, Pilot 1's isolation or the lab rules. Each cut protection needs Yu's yes in plain words at the build consent.
- **Registration and identity:** the frozen contract, registration, PA1–PA4, the code-hash digest and the process pin. The commit is shown and a clean tree required instead.
- **Journal records and stops:** a Pilot 2 fold and notes, a fault acknowledgement line, Pilot 2 stops, a session-end record, and journaled SKIPs and screens.
- **Results machinery:** the ledger, classes, value at BSP, the verdict, the tables, capture and attestations, and own P&L arithmetic. Betfair's cleared figures and Yu's website check replace them.
- **Failure extras:** the auto-kill child, the suspended-market poll and second cancel, and any retry of a failed pull.
- **Drills and dry runs:** the probe sessions, a stand-in transport, a dry mode and dry day (P2-PT2), and an end-session command. `p2-summary` and `recording-release` cover a crash.
- **Commands and code:** a separate Pilot 2 kill, a new arming row, any risk.py change, and the battery check (caffeinate stays).
- **Policy rows not set in code:** the final date (2027-06-30, proposed) and pausing.
- **Not adopted:** the Heartbeat API (banned), the order stream, cancel by market, and two-instruction requests.
- **Gate B:** withdrawn. It counted spreads in Pilot 1's −300 s decision books, and the bot now decides at −590 s.
- **Deferred:** a dead-man or watchdog, and mark-outs during the rest.
- **Unverified:** the `virtualise` field name; whether Betfair refuses cancels on a suspended market; whether commission is filled when grouped by MARKET; F73.
