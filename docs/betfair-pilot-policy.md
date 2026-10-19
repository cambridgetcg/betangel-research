# RS-19 live-automation policy (pilot executor)

Status: Proposed. Takes effect only through the approvals in §14; until then AGENTS.md and docs/betfair-order-path.md apply as written at 416702f.

This policy amends the lines listed in §1. Nuance wrote it on 2026-10-19 against main 416702f. Astra reviewed it in four rounds. Round 4 accepted the design with two nits (R4-1, R4-2); both are applied here and await Astra's check. It is the single source for every rule that the [Pilot 1 plan](betfair-pilot-1-plan.md) shares; the plan points here and does not restate them. Contract p1 is [research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json) and holds the same values.

- A value marked "(Yu to confirm)" is Nuance's recommendation, written in so that the text is concrete. Yu confirms or changes each one before the build approval; the freeze commit (§14 step 0) drops the markers, and the commit Yu approves is that freeze commit. The appendix lists them.
- File:line citations refer to the tree at main 416702f. "order-path" is docs/betfair-order-path.md and "facts.md" is docs/betfair-facts.md; the other files are under betangel_research/betfair/.
- Ids in parentheses after a heading, such as (Astra #4) or (R3-2), name the review findings that the section closes.
- The first live pilot order can come only at §14 step 5.

## 1. What it amends
Each clause below says that the exception applies only under this policy and only after the §14 approvals; before them, nothing changes in behaviour.
- AGENTS.md:9, append: "Exception (RS-19): the registered pilot executor in docs/betfair-pilot-policy.md may compute live decisions for one contract that Yu registered, and submit them through the order gateway, only in sessions Yu armed and attends. No other strategy, agent, script or scheduler may. The exception applies only under that policy and only after its §14 approvals; before them, nothing in this rule changes."
- AGENTS.md:10: "LIMIT orders with LAPSE persistence that the operator types at a terminal" becomes "…types at a terminal, or that the registered pilot executor sends in a session Yu armed and attends (RS-19; only under docs/betfair-pilot-policy.md and after its §14 approvals)". "there is no live replace and no automatic live settlement" stays as it is.
- order-path §15 (:630): add the RS-19 exception and a pointer to this policy. `source="api"` stays paper-only.
- These lines get a pilot clause, because they stop being true for pilot orders: :5 and :13 (every order is retyped); :26 (the only live source is cli_manual); :182 and :185 (the operator confirms each order before open(), and the book is fetched after that); :227 (the typed lines gain REGISTER PILOT, PILOT ARM and PILOT ACK); :231 (a person sees every order); :651 ("no live programmatic API"). Its §2 command table (:135-146) gains the pilot rows in §8.
- Unchanged in order-path: :16, :421, :440 and :649 in §16. Nothing settles automatically. Only the three listed things lower exposure.

## 2. Scope: the lean first version
- One executor: `python3 -m betangel_research.betfair pilot-run --mode live`, on Yu's Air, from a clean checkout that matches the registration receipt (§7).
- One contract at a time, identified by the sha256 of its frozen JSON. Pilot 1 is contract p1 ([research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json)), rule P1-FAV-T5. The intent source is "pilot". customer_strategy_ref is the fixed config value p1-fav-t5 (Yu to confirm).
- Sessions are attended. Yu watches the Air terminal for the whole session. There is no notifier and no watchdog: the executor only stops and prints. The Air stays awake and on power. If Yu steps away, they end the session first. There is no unattended running.
- Settlement is the existing typed `settle-market`, one market at a time, by Yu (§8). Nothing settles automatically.
- The executor may send only: a BACK, LIMIT, LAPSE order of exactly £2.00, one instruction per request, for a PLACE record made under §4, before the §5 send deadline, with at most one decision per market for the contract's whole life (§6); and a whole cancel of the unmatched remainder of its own pilot order, straight after the place report (during a KILL, §8 says when).
- Exceptions to current rules, and nothing else. E1: for source "pilot" only, the per-order echo (gateway.py:1158) is replaced by the session ARM line plus the §4 decision record. E3: source "pilot" is accepted in live mode (gateway.py:859-863), only when PA1-PA4 and R13 pass; "api" stays paper-only. E2 (automatic settlement) is not in this version (§16).
- New gateway checks: PA1 contract sha256; PA2 code identity; PA3 exact config sha256 and an armed, unexpired session (all §7); PA4 no pilot stop (§6). R13 is the decision protocol of §4 and §6. There is no PA5.
- Everything else on the place path is unchanged: A1-A19, V0-V14 and V9b, R1-R12, funds before a fresh self-fetched live-key send book (A18), the body-bound capability, the journal written with F_FULLFSYNC before the send, no resend. Three consecutive UNKNOWNs is a HALT threshold. It never permits placing while one order is unresolved; R3 refuses that. The caps never rise to buy speed.

## 3. Who starts, stops and changes it
- Only Yu starts it, in person at the Air terminal. Yu types `REGISTER PILOT <contract12> <code12>` once per registration (§14 step 4), and `PILOT ARM <contract12> <YYYYMMDD> UNTIL <HHMM>Z` each racing day.
- A session ends at the first of: UNTIL; 23:59:59Z, because the confirm token is per UTC day (arming.py:78-82); Yu stopping it; any KILL, HALT, session stop or review stop. No reservation (§4) starts within 6 min (Yu to confirm) of the end.
- Anyone on the Air may stop it: the KILL file or own-scope `kill`, which take no lock. Ctrl-C stops the loop, but once a send has started the CLI defers it (gateway.py:387-388; order-path:190). The KILL file and any journaled stop reach the first-byte gate (§5). A KILL file alone stops new sends but sweeps nothing (killswitch.py:57-90); only a kill invocation attempts a sweep. Away from the Air, use the Betfair website: a kill run elsewhere cannot see the Air's state root.
- Only Yu may resume, accept, acknowledge a stop, settle, change a cap, edit the contract, config or checkout, or register.
- Agents and scripts never start pilot-run live, and never type the REGISTER, ARM, SETTLE, ACCEPT, ACK or RESUME lines. The typed lines are friction, not proof that a person is there (presence.py:3-9; order-path:231). This policy is what forbids it.
- Nuance may support first-live tests that Yu explicitly authorizes, under the existing consent and presence rules (facts.md:272). Those rules do not need Yu to type every outer test command.

## 4. The decision: reservation, evidence and record (Astra #1, #4; R2-2)
One protocol, in this order, inside one pilot placement open (open() to close(), under .gateway.lock):
1. Reserve before fetching. At or after off − 300 s, using the scheduler's off time, the gateway appends a reservation for (contract sha256, market), with a compare-and-append on the journal head (journal.py:17-20, :483-490). It carries the session id and a new open id. A second reservation for the same pair is refused, so two runners cannot both decide one market. The scheduler may find candidates in any catalogue, cached or delayed. That list sets timing only; it is never evidence.
2. Fetch once. After reserving, the gateway itself fetches, with the live key, one fresh, uncached listMarketCatalogue row with the MARKET_DESCRIPTION, EVENT_TYPE, MARKET_START_TIME and EVENT projections, then one decision book with isMarketDataDelayed false. In that row, off is marketStartTime (the same time the send book reads as description.marketTime, api.py:956) and country is event.countryCode; the event's openDate is never the off. The normal snapshot has no country (api.py:46, :950-962), catalogue_row() may serve a row up to 6 h old (api.py:633-653; config.py:46), and the read slot defaults to delayed (config.py:44); none of them is used. A caller-supplied book, catalogue or country is refused. Raw responses go to the state root and their digests are journaled, whatever the outcome. There is no second fetch.
3. Classify once. The gateway checks the P1 predicates below on that evidence and appends one decision record, PLACE or SKIP with a code, by compare-and-append. A PLACE takes its capacity in the same append (§6). If the head moved, the gateway re-checks capacity, stops and drawdown on the new head with the same evidence and appends again. It never fetches again.
4. Interrupted means SKIP. Only the open that made a reservation may classify it. A reservation whose open ended without a record (crash, kill, lost lock) is SKIP(interrupted) in every fold. It is never refetched or re-decided.
- The P1 predicates. This list governs; contract p1 holds the same values. "off" is marketStartTime in the fresh catalogue row (step 2).
  - market: country GB or IE; event type 7; market type WIN; ODDS; one winner; CLASSIC ladder; at least 5 ACTIVE runners in the decision book;
  - window: the decision book was observed from off − 300 s to off − 300 s + 15 s (Yu to confirm). Outside it: SKIP(window);
  - selection: the only ACTIVE runner with the lowest best back price (a tie is SKIP); best back from 1.50 to 4.00; at least £10 available at it; best lay no more than 2 ticks above it;
  - order: BACK, LIMIT at exactly that best back price, LAPSE, stake 200p.
- The record holds: contract sha256; registration receipt sha256; session id; open id; reservation seq; market and selection ids; side; limit price in hundredths; stake; LAPSE; country and off from the fresh row; the decision book's time on both clocks and its isMarketDataDelayed flag; the send deadline (off − 240 s); digests of the row and the book; PLACE or SKIP with a code; for a PLACE, its lifetime slot and day count.
- Two books, two jobs. The decision book alone decides the selection, depth and spread. The send book is the gateway's later fresh book (A18). On it the gateway runs V0-V14 and V9b as today and V13 (no more than 2 ticks of movement). It checks that the selection is still ACTIVE, and refuses if the send book's market_time differs from the record's off. It never re-decides from the send book.
- Binding. R13 checks the body against the PLACE record from this same open, field by field. The record's sha256 goes into the risk decision, and the capability binds it together with the body sha256 (gateway.py:1234-1246). Missing, contradictory or reused evidence refuses: a record from another open, session or contract; a digest that does not match; a body that differs.

## 5. The first-byte gate (Astra #2; R2-1)
- After a PLACE, the place path runs as today: funds, send book, V-rules, R1-R13, intent, risk decision, then the submit, compare-and-appended on the head the risk decision saw (gateway.py:1104-1116, :1241-1246). PA4 is checked at open, at classification and at the risk decision.
- Right before the first request byte, the gate (gateway.py:915-934) refuses unless all six hold:
  1. no KILL file (as today);
  2. the send book is within max_book_age_s on both clocks (as today);
  3. a fresh read of the journal head (journal.py:437-442) is exactly the head our own submit created. Any later event from any writer refuses: HALT, kill, kill_incomplete, a pilot stop or anything else. An unreadable head, or one that does not follow from our submit, also refuses;
  4. the wall clock is before the send deadline and before session expiry (the earlier of UNTIL and 23:59:59Z);
  5. the monotonic time since the decision book fits inside the same remaining window;
  6. the wall and monotonic clocks have not drifted apart by more than 2 s (Yu to confirm) since the decision (as order-path:424 does).
- Why check 3. Holding .gateway.lock does not freeze the journal. kill takes no lock (gateway.py:2156). If it cannot create the KILL file, it still journals the kill and the HALT (gateway.py:2170-2180, :2138-2148). The CLI can also append HALT kill_incomplete without the lock (cli.py:1408-1416). So a stop can be committed while no KILL file exists. Check 3 sees it. The lock, the body binding, the clocks and the KILL check all stay; none of them replaces check 3.
- What is left. A stop committed after check 3's read and before the first byte cannot be seen. If that stop is a kill invocation, the kill attempts re-sweeps at T0 + 20 s and T0 + 35 s (gateway.py:2157-2158; killswitch.py:228-237). They are attempts, not a guarantee: a network or login failure can stop them. A file-only KILL, a HALT or a pilot stop sweeps nothing. In every such case, including a send whose outcome is UNKNOWN, the executor's remainder cancel or its handoff to Yu (§8) follows. If neither the KILL file nor the HALT can be written, nothing on the Air can stop a send: use the Betfair website.
- A long open() cannot stretch the window. The 120 s reconcile budget (gateway.py:102), the lock wait, the Keychain anchor and the login all count against it. The executor opens early enough to reserve at off − 300 s.
- Any refusal here is FAILED(transport_not_sent): no byte is written, the market stays reserved and the slot stays used (§6). So the end of a session, a KILL, a HALT or a due stop voids every placement not yet sent, not just the next loop. The one exception is the race above: a placement whose checks passed just before the stop was committed can still be sent.

## 6. Capacity, durable stops and carryover (Astra #4)
- The journal is the authority. Pilot records are note events. A note enforces nothing by itself, so the pilot fold and compare-and-append rules enforce everything in §4 and here.
- R13, not R8, enforces one decision per market. R8 frees up once exposure is released (risk.py:374-375), and the journal is unique per order ref, not per market (journal.py:125).
- Every reservation uses up its market, SKIP included. No reservation is ever released: not after REFUSED, FAILED, UNKNOWN, cancel, settle, restart or RESUME.
- A PLACE record takes one of the 495 (Yu to confirm) lifetime slots and one day slot in its own append, before anything can be sent. If either is full, the record is SKIP(cap). Day limits: 5 (Yu to confirm) PLACE records on each PT4 session day (§14 step 5), then 25 (Yu to confirm) per UTC day. They count PLACE records, not R6's submitted instructions (risk.py:301-324). A slot whose request was definitely not sent stays used.
- Pilot stops are folded from the journal, so a restart cannot lose them. Session stop: ends today's session; the next needs a new ARM line. Review stop: no placement until Yu writes a review and types `PILOT ACK <contract12> <stop-id>`; for a drawdown stop, §10 says what the ACK covers. Contract ended: final. `resume` and RESUME LIVE clear a gateway HALT only. They cannot clear a review stop or reopen an ended contract.
- Carryover. Reservations, slots, day counts, stops and the ledger's equity history and peak (§10) are keyed by contract sha256. A new registration of the same contract (for example after a code fix) keeps all of them. A replacement contract (for example after PT4 forces a contract change) names its predecessor's sha256 and inherits all of them: the slots left (495 minus those used), every reservation, any active stop, and the equity history from the first registration. So the £1,000 bound holds across both.

## 7. Registration receipt and identity (Astra #5)
- The contract JSON in Git holds the rule, caps, N, metric, exclusions, analysis, stops, final date and strategy ref. It names no commit, so committing it cannot change what it names.
- `pilot-register` writes a registration receipt outside the contract, in the state root, and journals its sha256. The receipt binds: the contract sha256; the code commit and tree; the receipts.code_hashes digest (receipts.py:121-125); and the full config sha256 (the daily token uses its first 12 hex, arming.py:82).
- Every pilot placement open checks all four for exact equality, and that the tree is clean. An unknown or unreadable identity, or a dirty tree, refuses. Caps inside bounds is not enough. Cleanup commands keep today's checks (§8), so drift never blocks cleanup.
- customer_strategy_ref stays p1-fav-t5 (Yu to confirm) until every pilot order is settled, even after the contract ends. A kill on an untrusted journal then still sweeps pilot bets (gateway.py:390, :2189-2194).
- code_hashes covers only betangel_research/*.py and betangel_research/betfair/*.py. It catches drift. It does not prove Yu is present, and it does not stop a hostile process (order-path:7).
- A code or config change needs a new receipt, Astra's acceptance and a new activation approval (§14, §17) naming the new code commit and config sha256. Config holds more than caps (for example orders.customer_strategy_ref, config.py:57; order_deadline_s and the login caps, config.py:41-43). §6 carries the state over.

## 8. Commands, cleanup and settlement (Astra #6; R2-8)
| Command | Who | Gates |
|---|---|---|
| pilot-register | Yu, typed line, after §14 step 3 | A2, A11, A13; writes the receipt |
| PILOT ARM | Yu, typed line, once per racing day | A2, A11, A13; starts one session (§3) |
| pilot-run: decide and place | executor, in an armed session | A1-A19 on every placement open, PA1-PA4, R1-R13, §4-§6, the §5 gate |
| pilot-run: remainder cancel | executor | existing cancel set (A4, A5, A8, A10, A12-A15, login only); runs during HALT and KILL, as ordinary cancels may (gateway.py:1371-1395; risk.py:561-571); no PA gate; whole cancel of a pilot order only; during a KILL, the rule below the table |
| pilot-run: SETTLE MARKET | Yu, typed inside pilot-run, at a settle point | the gates of the settle-market row, plus the settle-point rule below the table |
| pilot-capture (read only) | executor in the session, between placement opens; Yu, typed, after it (§11) | read session only; needs no arming line; sends nothing; never changes order state or exposure; Yu's run refuses while a session is armed, so it never appends during a send (§5 check 3) |
| cancel; own-scope kill | anyone on the Air | unchanged (order-path:141, :144); kill takes no lock; cancel waits order_deadline_s + 5 s for the lock (15 s at today's default, config.py:43; gateway.py:690), then exits lock_busy |
| reconcile; reconcile --accept | anyone; accept is Yu only, typed | unchanged |
| settle-market, own process | Yu, typed, outside a session only | unchanged: A1, A2, A11 plus the login set, no A7, today's token, P0/P2/P3, worst case only (arming.py:58; gateway.py:2086-2134) |
| PILOT ACK | Yu, typed line | A2, A11, A13; clears one review stop |
| resume; kill --scope account | Yu, typed | unchanged; resume cannot reopen an ended contract |

- The remainder cancel during a KILL (R3-1). A KILL file sweeps nothing. A kill invocation journals kill and halt when it can, but it journals an outcome only when it ends incomplete (HALT kill_incomplete: gateway.py:2214-2216; cli.py:1405-1411). A complete kill journals none. So, for a sent pilot order whose place report shows an executable remainder, the executor reads only the journal, not Betfair, and takes the first case that applies:
  1. It cannot read the journal: it prints the handoff at once.
  2. No kill event after its submit (a file-only KILL, or a kill that could not journal): it sends its own whole cancel at once.
  3. A kill event after its submit, then HALT kill_incomplete: it sends its own whole cancel at once.
  4. A kill event after its submit, with no outcome: a sweep may be running. It sends no cancel and makes no Betfair read, because either would compete with the sweep (gateway.py:1300-1301, :1310-1312; F18). It waits up to 30 s (Yu to confirm) from when it first sees that kill event. If HALT kill_incomplete appears, it acts as in case 3. If not, it prints the handoff at once. A complete kill and a kill process that died look the same here, which is why the wait is bounded. The kill's last re-sweep is at T0 + 35 s, so the handoff can come while a healthy kill is still running. Following it is still safe.
- An UNKNOWN place outcome. A cancel needs a bet id (gateway.py:1371, :1382-1383), and the place path stops reconciling once a KILL file exists (gateway.py:1309-1322; order-path:194). So if a sent pilot order is still UNKNOWN when its place path ends, the executor prints the handoff at once, whatever stopped it. A file-only KILL never sweeps it.
- The handoff says, in the terminal: pilot order <bet id, or its order ref if UNKNOWN> (BACK £2.00 on <selection> in <market>) may still have an unmatched part; run own-scope kill on the Air, or cancel it on the Betfair website, now. The executor prints the same handoff if its own cancel is refused or fails. A kill that could not journal, or one that starts just after case 2 or 3, can overlap the executor's cancel. That may leave an UNKNOWN for reconcile (gateway.py:1300-1301), and the executor's reads can crowd that kill's reads (F18), so the kill may end kill_incomplete. Neither adds exposure: a cancel of our own bet only reduces it (gateway.py:1394; risk.py:565). So kill_incomplete is never the only way out.
- A1-A19 and PA1-PA4 apply to placement only (arming.py:51-62). Startup reconciliation runs for place, live-probe, reconcile and cancel, not for settle-market or kill (gateway.py:107-114).
- A settle point inside pilot-run needs: no pilot place or cancel in flight; no pilot order with an executable unmatched remainder; no unresolved pilot order (schemas.py:51); and no decision due within 3 min (Yu to confirm). Fully matched EXECUTION_COMPLETE orders in CLOSED markets are allowed. They are nonterminal and still counted (schemas.py:46-52; risk.py:109-112), and settling them is what releases their exposure.
- A second process must not settle during a session. It would take .gateway.lock, and a place or cancel waits for it only order_deadline_s + 5 s.
- Settlement counts in R10 on the UTC day it is typed, and needs that day's token (journal.py:852-857; risk.py:327-348). Settling after 00:00Z books the worst case on the new day: ten of yesterday's £2 markets use £20 of today's £50 (Yu to confirm).
- Settle while CLOSED is still visible. A market no longer returned cannot be settled in v1 and stays at worst case (F77, facts.md:166). T10 measures how long CLOSED lasts.
- The lock is held from open() to close() (order-path:183; gateway.py:695), through the 120 s reconcile budget and any reconcile after an UNKNOWN (order-path:408). So a manual cancel can fail during a place. The KILL file or kill is the reliable stop.
- If a cleanup step's own gates fail (login lockout, no tty, a new UTC day), the order stays at worst case. The executor stops and prints this. Yu cancels on the website if needed, and settles later with that day's token. No tty or token check is waived.

## 9. Caps, refusals and logins (Astra #12)
- Config caps, all inside constants.py:197-217 and the contract: stake £2.00, liability £2.00, market exposure £2.00, total exposure £20 (Yu to confirm); max_daily_loss £50 (Yu to confirm); orders 2 a minute (Yu to confirm), 12 an hour (Yu to confirm), and up to 25 a day (Yu to confirm) subject to every other gate (R10 counts open and unsettled liability, risk.py:327-348, :378); max_minutes_before_off 10 (Yu to confirm), max_price_deviation_ticks 2 (Yu to confirm), max_book_age_s 2.0 (Yu to confirm), external_exposure_tolerance £0.00 (Yu to confirm).
- A refusal is not a HALT. R11 (funds) refuses without a HALT; R12 (unexplained exposure) HALTs (risk.py:401-430; gateway.py:1213-1219). LOSS_LIMIT_EXCEEDED and INSUFFICIENT_FUNDS from Betfair FAIL and HALT (order-path:626).
- The pilot adds one latch. These end only that market: any SKIP; V13; V9 market_not_open or market_inplay; V9b; the R6 minute or hour rate; FAILED(transport_not_sent) from §5, unless its cause ends more. These end the session: the R6 day cap, the §6 day limit, R10, lock_busy, a remainder cancel that failed or left an executable remainder. These set a review stop: any other V9 code (validate.py:97-121); R11; any PA or R13 refusal; NotArmed at open; earlier_unknown_resolved_at_open (gateway.py:1149-1150, :1179-1188); a cleared-figure gap (§10); a failed §11 consistency check.
- An UNKNOWN stops placing until reconcile resolves it. If it stays unresolved, the session stops, the terminal prints the §8 handoff, and Yu decides.
- Logins: the 30-per-10-min cap is a local setting (config.py:41), far below Betfair's own limit (auth.py:11). Every open logs in (gateway.py:703). Two failed or unknown logins stop every caller, kill included, until login-reset (config.py:42; auth.py:433-434). The executor ends the session at the first login failure, and Yu decides on the reset.

## 10. The ledger, equity and drawdown stops (Astra #3; R2-3)
- The ledger is a pure fold of journaled pilot records, kept on the Air. It is separate from the R-rules, which keep their own worst case, and it is never fed back into them. It can only bring a stop earlier.
- One equation: equity = the sum of one value per PLACE record. Each record is in exactly one part:
  - W, worst case, the default: minus the final matched stake if the final execution record (§11) gives it, else minus £2.00. A resolved zero match is £0. A record with no submit event, or FAILED(transport_not_sent), is a resolved zero match: nothing was sent (order-path:183).
  - S, settled and verified, only when all hold: Yu's typed settle-market for the market succeeded; the final execution record is resolved; the CLOSED read shows one WINNER, our runner WINNER or LOSER, no void and no runner removed after the decision; Betfair's per-bet profit is present and the commission check below passed; and the PT4 receipt (§14 step 5) exists.
- S values. LOSER: minus the final matched stake. WINNER: gross = final matched size × (P − 1), P at full precision, floored to the penny; commission = c × the positive market gross, rounded up to the penny, where c is the one rate frozen in contract p1, 2% (Yu to confirm), which the plan's metric also uses (one bet per market; pnl.py:32-34, :79-83; no discount assumed); net = gross − commission. The value is the lower of our net and Betfair's per-bet profit minus that commission.
- Like-for-like checks. Our per-bet gross is compared with Betfair's per-bet profit, as compare_profit does (pnl.py:87-93), on the sign and basis PT1 or PT4 verified (F37 is unverified, facts.md:187). Our commission is compared with Betfair's market commission where the API returns it; F37 doubts that it does. Where it does not, the commission check passes only after Yu has matched our commission with the website's, to the penny, for a winning pilot market (in PT4 or by a later written check). A net figure is never compared with a gross one.
- A gap over 1p in either check keeps the bet in W and sets a review stop, from the first bet, PT4 included. A missing Betfair figure keeps the bet in W until it arrives; if it never arrives, the bet stays in W.
- These stay in W for the whole contract: a dead heat (pnl.order_pnl_pence pays the full win, pnl.py:24-35; market_realized refuses it, pnl.py:53-56); a void; any runner removed after the decision; an unknown result, price or size; any settlement we cannot verify. Five ordinary PT4 bets do not validate them. Learning their basis needs a later approved contract; it cannot change Pilot 1.
- Pinned examples (each is a test). Pending: £2.00 fully matched, not settled: −£2.00. Ordinary winner: £2.00 matched at 3.00, settled, Betfair profit 400p, commission 8p verified: +£3.92. Unresolved settlement: the same bet with Betfair's profit missing, or 3p apart: −£2.00, and the 3p gap sets a review stop. Never sent: a PLACE refused by V13 before its submit, or FAILED(transport_not_sent): £0. 485 settled £2 losers plus ten pending £2 bets: equity −£990, never −£1,010.
- Drawdown = peak equity minus current equity, from the first registration; the peak starts at £0 (§6 carries both over). One pure function computes it at every PLACE classification, counting the new bet at −£2.00, and after every ledger change. Each threshold is an episode on this peak-to-trough series. A PLACE whose −£2.00 would reach an armed threshold becomes SKIP(drawdown), and the stop is journaled at once:
  - £300 (Yu to confirm): a session stop and a review stop, at once; continuing needs Yu's written review and PILOT ACK;
  - £600 (Yu to confirm): HALT and a review stop; continuing needs Yu's written review, PILOT ACK and RESUME LIVE;
  - £1,000 (Yu to confirm): kill, and the contract ends. It never re-arms. The 495 cap makes this unreachable, but the check stays.
- What an ACK covers (R3-4). The stop-id names the threshold and the peak it was measured from. The ACK disarms that threshold for that peak only. Placing may then go on until the next threshold: after a £300 ACK, until the £600 HALT. A disarmed threshold re-arms only when equity sets a new peak (above every earlier one); it then fires when a PLACE would take equity that far below the new peak. Equity and the peak are never reset, caps are never edited, and a higher threshold is never waived. The fold rebuilds peaks, stops and ACKs from the journal, so a restart gives the same answer.
- Pinned example (a test, with a restart): peak £0, settled equity −£298, nothing pending. The next PLACE would make the drawdown £300: SKIP(drawdown), a session stop and a review stop. Yu reviews and ACKs. In the next session the next PLACE, which again makes the drawdown £300 from the £0 peak, is not a drawdown SKIP; after a restart, the same. The £600 stop stays armed. If equity later sets a new peak of +£4, the £300 stop re-arms and fires when a PLACE would take equity to −£296.
- Outside the code: a Betfair website loss limit of £50 (Yu to confirm) a day, and a balance kept close to the budget, with no top-ups. The £499 key and running costs are not trading drawdown.

## 11. Final execution, BSP and measurement classes (Astra #7; R2-4, R2-5)
- The place report's average is floored to hundredths for risk (gateway.py:195; reconcile.py:128-134). That stays for risk; the metric never uses it. More can match before the cancel lands (lifecycle.py:283-288).
- Each PLACE gets one final execution record from two sources. C, the current row: listCurrentOrders once the bet is EXECUTION_COMPLETE (after the remainder cancel, a full match or the lapse at the off), read before its market settles, because settled bets leave that list (F77); sizeMatched and averagePriceMatched kept at full precision. L, the cleared row: listClearedOrders after Betfair settles; sizeSettled, priceMatched and priceReduced. An L row counts as read only once a PT1 or PT4 receipt has verified those fields against the website (F37). An L row read before that is logged, and counts from that receipt if the receipt comes before the deadline. If a receipt finds that the fields mean something else, no later session is armed until a replacement contract (§6) fixes the method.
- The place and cancel reports are earlier snapshots. They are checked only for consistency: the final matched size is at least the place report's, and matched + cancelled + lapsed + voided size equals £2.00. Those four sizes come from C when C is read, not from the cancel report: a kill's sweep cancels on a no-orders stand-in, so our own cancel report may not exist (gateway.py:2195-2199). A failed check makes the size unresolved and sets a review stop (§9). They are never compared with final totals as if taken at the same instant.
- Agreement is exact (R3-3). C's and L's sizes and prices are the numbers as written in the raw JSON, parsed with Decimal (never through float). They agree only if they are exactly equal, with no rounding. Contract p1 freezes this rule. A rounding rule may replace it only if Betfair's documentation justifies one, never because of the digits seen in a few fills, and only before contract p1 is frozen or in a later contract. PT1 and PT4 record C and L as evidence under this rule; they cannot choose a tolerance. A difference that looks like rounding (C 3.444, L 3.44) is a mismatch.
- Finalization (R3-2). A bet already classed void or post_decision_removal is not price-compared: a documented removal comes first, even when C and L differ (class 4). Otherwise this table decides. The deadline is off + 7 days (Yu to confirm). "L unavailable" means only that no L row counted as read by the deadline; nothing declares it earlier.

| Read by the deadline | Final when | Final size and P (the NCLV price) |
|---|---|---|
| C and L: sizes and prices exactly equal, priceReduced false | when L is read | L's sizeSettled and priceMatched |
| C and L: anything else | when L is read | none: measurement_unresolved |
| C only | at the deadline; pending until then | C's sizeMatched and averagePriceMatched |
| L only | when L is read | L's sizeSettled and priceMatched if priceReduced is false; else measurement_unresolved |
| neither | at the deadline | none: measurement_unresolved |

- Canonical observation (R4-2). When a source has more than one qualifying observation, the canonical one is the earliest by durable capture sequence in the journal (never by loading order). For C, that is the earliest read showing EXECUTION_COMPLETE. For L, it is the earliest logged L row; it becomes usable from the PT receipt, and its place in the sequence does not change. Later observations of the same source are logged as corrections and never replace the canonical one. Pinned: two C reads (3.44, then 3.440000000000001) before L 3.44 gives agreement on the first C; two L rows logged before the receipt, loaded in either order, give the same result.
- Pending is waiting, not failure. An L read before the deadline turns a C-only record into a C-and-L one. Once a record is final, later reads and corrections (C, L, CLOSED or BSP) are logged and never change it. Unresolved never means unmatched. The record never lowers exposure. A bet with no matched part has no L row: L is the SETTLED view, and lapsed or cancelled bets sit in other views (F35, F36, facts.md:164-165; api.py:713-714). So it stays C-only and pending until the deadline, and §10's W keeps it at −£2.00 until then. That is conservative: a drawdown stop can come earlier, and N is found later.
- Pinned examples (each is a test). C now, then an L tomorrow that contradicts it: final when L is read, measurement_unresolved. C now, no L by the deadline: final at the deadline from C. PT fills all at whole-number prices, then C 3.44 and L 3.06: unresolved. C 3.444 and L 3.44: unresolved. C and L both 3.44: P is L's 3.44.
- BSP comes from raw listMarketBook with SP_TRADED (api.py:571-606); the normalised snapshots drop sp (api.py:935-939). The frozen value is actualSP from the first raw read that shows the market CLOSED, if that read is within off + 24 h (Yu to confirm) and valid: finite and above 1, our runner WINNER or LOSER in it, and bspReconciled true. Otherwise the bet has no BSP. NaN, Infinity and a missing value count as none. Reads before CLOSED, and corrections after it, are logged and never used.
- Capture and custody. In a session the executor reads C after each remainder cancel, and the CLOSED book and L between decisions. After the session Yu runs pilot-capture the same evening (Yu to confirm), then once a day until every record is final or past its deadline. pilot-capture is read only and gives no placement authority. Raw responses stay in the state root until the terminal analysis has been recomputed.
- Measurement classes. Each PLACE record ends in exactly one class; the first that applies wins:
  1. protocol_violation: it broke this policy or the contract;
  2. definitely_unsent: no submit event, or FAILED(transport_not_sent);
  3. void: the market was voided, or our runner was removed;
  4. post_decision_removal: another runner ACTIVE in the decision book was later removed (seen in the CLOSED read or in L). It applies whether or not a reduction factor changed our price, and it is decided before any price comparison;
  5. measurement_unresolved: the final matched size, or a matched bet's price, is unresolved;
  6. unmatched: the final matched size is 0;
  7. missing_bsp: no frozen BSP;
  8. eligible.
- A class is final once its execution record is final under the table and the BSP rule has decided, and at off + 7 days (Yu to confirm) at the latest. Anything still unknown then is measurement_unresolved. Later reads never change a final class.

## 12. Reporting and the data lock (Astra #11; R2-6)
- This is a fixed-analysis, no-adaptation rule. It is not blinding.
- Every view except Yu's private risk view (status output, agent summaries) shows only operational counts and codes, in the terminal: reservations; decisions; SKIPs by code; PLACE records; sends; refusals by rule; fills (full, partial, none); cancels; UNKNOWNs; settles; classes still pending; missing BSP; stops; sessions. No NCLV, profit, price-band or calibration figure.
- Yu's private risk view shows balance, exposure, the ledger and drawdown. It is for safety, stays on the Air, and is never copied into Git or agent reports. Seeing it is planned; it changes nothing in the analysis.
- The data lock. Placing ends at the first ending in §15; after that only the remainder cancel, cancel, kill, reconcile, settle-market and pilot-capture run. The analysis waits until every PLACE record made before placing ended has its final class (§11), or has passed off + 7 days (Yu to confirm). Then contract p1's frozen analysis runs once, on the Air, in the terminal, and Astra recomputes it independently there. There is no earlier efficacy figure and no provisional PASS. The interim counts stay available.
- Any unplanned look at outcomes is logged. It cannot change the rule, inclusion, N or the threshold. Safety stops still apply, and never give an early PASS.
- Resuming after a stop needs objective conditions only: the cause fixed or explained in writing, the journal reconciled, no unresolved order, no open ledger gap, and Yu's acknowledgement. Results are never a reason to pause or resume. Departures are reported.
- Nothing from the pilot enters Git or reports/ unless Yu approves the exact fields and a test checks their projection (tests/test_betfair_reports_no_account_data.py). Neither order-path:615 (a narrow first-live-test list) nor T&C 19.3 grants more.

## 13. Still forbidden
- Orders: in-play orders and lays; PERSIST or MARKET_ON_CLOSE; MOC, LOC and BSP orders; betTargetType, minFillSize, timeInForce and async; replace, partial cancel, more than one instruction per request, a second decision in a market.
- Reach: the Heartbeat API, any account other than Yu's, any market outside the contract.
- Changing caps, stake, rule, N or thresholds without a new contract and Yu's new written approvals.
- Agents arming, resuming, accepting, acknowledging, settling, or editing the contract, config or checkout.
- An account-wide kill without Yu.
- Bet Angel or manual bets on the account: none may be open or unsettled while a session runs, even ones placed before it. R12 at £0 HALTs on them (risk.py:420-430).

## 14. Approvals and the order of steps (R2-7)
The steps run in this order. None is skipped, and no receipt is written for a step that did not happen.
0. Freeze (PR29-1). After Yu confirms or changes every value marked "(Yu to confirm)", Nuance makes one freeze commit: every dependent value updated; in contract p1, `status` set to "frozen", `frozen_on` set to that UTC date and `values_yu_to_confirm` emptied; every prose marker removed from this policy and the plan. Astra checks that final artifact before step 1. The contract's identity is the SHA-256 of the frozen file's raw bytes, exactly as committed; build approval, activation and registration all name that same digest. `pilot-register` refuses a contract whose `status` is not "frozen", whose `frozen_on` is null, or whose `values_yu_to_confirm` is not empty.
1. Build approval. Astra accepts this policy and the frozen contract p1 (step 0). Yu types the §17 build sentence, naming the freeze commit and the frozen file's raw-bytes SHA-256. This policy, the §1 redlines and contract p1 are merged. Nuance may then build and merge the pilot code. This step allows no live pilot order: with no registration receipt, PA1-PA3 refuse every pilot placement.
2. The pre-first-order gate. All of these exist (the tests may be done earlier, some before step 1; the gate is that all exist before step 3): receipts for T0, T1 (live key), T2, T3, T5, T9 and T10, where T10 shows how long CLOSED stays visible; PT1 (BSP fields, bspReconciled, CLOSED retention, BSP corrections, C and L as recorded (evidence under §11's exact rule, choosing no tolerance), and the sign and basis of cleared profit from existing cleared bets if any; with no matched or cleared bet, those matched-bet checks move to PT4); PT2 (a delayed-key dry day: the scheduler and the pure P1 predicates run on delayed data and record what they would decide, with no gateway and no order; it tests scheduling and recording only, not the §4 protocol, live timing or fills); PT3 (sleep, network-cut, SIGKILL, SIGSTOP and clock-jump drills); the adversarial tests below, all passing; Astra's acceptance of the exact code commit and config sha256; and Betfair's answer to G1, or Yu's recorded decision to go ahead without one.
3. Activation approval. Yu types the §17 activation sentence, naming that code commit and config sha256.
4. Registration. Yu runs `pilot-register` and types the REGISTER line.
5. PT4, the first armed session: at most 5 (Yu to confirm) PLACE records. Yu checks each bet against the website for matched size, price, result and net profit. PT4 also covers T7 with a real position, how long exposure takes to release after a settle (R12), a live recheck of PT1's BSP fields, and any PT1 checks that moved here. The PT4 receipt records all of this, including C and L as recorded under §11's exact rule and whether L's fields are verified (§11); it chooses no tolerance. If PT4 ends with no bet that matched and settled, the receipt says so, and the next armed session is PT4 again, under the same limits. Whether PT4's bets join the sample is frozen in contract p1 before its first bet. No later session is armed, and no bet enters ledger part S (§10), until a PT4 receipt with at least one matched, settled bet is written.
6. Later sessions.
- A later code or config change repeats steps 2 to 4 for what changed (§7). A contract change needs a replacement contract (§6) and steps 1 to 4.
- Adversarial tests, each a negative case:
  - decision: each P1 predicate left out in turn; a caller-supplied country, catalogue or book; a cached catalogue row; a row missing a needed projection; the event's openDate used as the off; a delayed-key decision book; a book outside the window; a second fetch; a send book whose off differs; a reused or edited record; a record from another open; a body that differs;
  - reservation: two runners on one market; two runners racing for the last slot; a crash after the reservation and before the record (SKIP(interrupted), no refetch); a crash after a PLACE and before the submit; a second decision after cancel, settle, restart or RESUME;
  - first byte: KILL-file creation fails but the HALT is committed before the gate; kill_incomplete appended without the lock; a pilot stop appended by another process; Yu's pilot-capture during a session (refused); a delayed open; sleep; clock jumps; session expiry between approval and send;
  - commands: RESUME on an ended contract; a changed config, code or contract; a dirty tree; cleanup under drifted config; cleanup during HALT; a file-only KILL after a partial fill (the executor cancels the remainder at once); a kill process that dies after its kill event and before its outcome (no executor cancel or Betfair read during the 30 s (Yu to confirm) wait, then the handoff); a kill ending kill_incomplete (the executor cancels); an unreadable journal during a KILL (the handoff); an UNKNOWN place outcome under a file-only KILL (the handoff); a failed remainder cancel (session stop); an in-session settle with an EXECUTION_COMPLETE order (allowed) and with an executable remainder (refused); a second-process settle during a session;
  - measurement: a cancel race (a partial place report, then a larger final match); a lapse at the off with no cancel report; a failed consistency check (unresolved, review stop); fractional averages; a legitimate reduction with C and L both present and priceReduced true (post_decision_removal, not unresolved); an unexplained C/L disagreement (unresolved); an L row read before any PT receipt verifies L (it counts from the receipt); a fully unmatched bet (C-only, pending to the deadline, W at −£2.00); the §11 pinned examples; a BSP corrected after CLOSED; a valid in-play BSP with no CLOSED read (missing_bsp); NaN, Infinity and missing BSP;
  - ledger: the §10 pinned examples, including the drawdown ACK example with a restart; an ACK of an older stop after equity set a new peak (the new peak's threshold stays armed); a dead heat; a void; a removal; a missing Betfair figure; a 2p gap.

## 15. How the exception ends
- It covers only the contract sha256 named in Yu's activation approval, run from the code and config in its registration receipt.
- Placing ends at the first of: N reached (contract p1's primary sample is complete: its first 400 (Yu to confirm) eligible bets in decision order are final, and so is every earlier PLACE record); the lifetime cap; the £1,000 (Yu to confirm) stop; the final date 2027-06-30 (Yu to confirm); any breach of this policy; Yu withdrawing it in writing. The analysis's validity gates never end placing early. A protocol violation ends it at once.
- After that there is no new reservation or placement. The executor may finish the remainder cancel of an order already sent. Cancel, kill, reconcile, pilot-capture and typed settle-market stay available until the data lock (§12) has passed and no pilot order remains. Then AGENTS.md:9-10 apply exactly as at 416702f.
- The verdict rules, including what an ending before N gives, are frozen in contract p1.

## 16. Deferred, for Yu to choose later
- A watchdog with phase-aware deadlines and a notifier (P1-11, PA5), for unattended sessions. It cannot beat sleep, a network cut or the login lockout on the same Air.
- Automatic worst-case settlement (E2, P1-09). It would need its own exception, matrix rows and T10 evidence.
- Richer reports: price bands, calibration, weekly views.
- Each needs a policy amendment, Astra's review and Yu's written approval.

## 17. Approval sentences (typed by Yu)
- Build approval (§14 step 1): "I, Yu, approve RS-19 as written in betangel-research commit <policy-commit>, and the frozen pilot contract p1 whose raw file bytes have sha256 <64-hex>, with the caps and stops filled in that commit, so that the pilot code can be built, reviewed and merged. This approval allows no live pilot order."
- Activation approval (§14 step 3): "I, Yu, activate RS-19 as written in commit <policy-commit> for pilot contract p1 with sha256 <64-hex>, run only from code commit <code-commit> and config sha256 <64-hex> that Astra accepted. The registered pilot executor may place live orders only within it, and only in sessions I arm and attend at the Air terminal. This approval ends when that contract ends."

## Appendix: values marked (Yu to confirm)
Each is Nuance's recommendation. The last three are set in the plan and contract p1, not in this policy. Contract p1 lists the values it holds under `values_yu_to_confirm`; the policy-only operating values (end margin, drift limit, kill wait, settle margin, website loss limit and capture timing) are not in it.

| Value | Recommended | Where |
|---|---|---|
| customer_strategy_ref | p1-fav-t5 | §2, §7 |
| No new reservation before session end | 6 min | §3 |
| Decision window length after off − 300 s | 15 s | §4 |
| Wall/monotonic drift limit at the first byte | 2 s | §5 |
| Lifetime PLACE records | 495 | §6, §10 |
| PLACE records on each PT4 session day | 5 | §6, §14 |
| PLACE records per UTC day | 25 | §6 |
| Wait for a kill outcome before the handoff | 30 s | §8, §14 |
| No decision due before a settle point | 3 min | §8 |
| Total exposure | £20 | §9 |
| max_daily_loss | £50 | §8, §9 |
| Orders per minute, hour and day | 2, 12, 25 | §9 |
| max_minutes_before_off | 10 | §9 |
| max_price_deviation_ticks | 2 | §9 |
| max_book_age_s | 2.0 | §9 |
| external_exposure_tolerance | £0.00 | §9 |
| Commission rate c | 2% | §10 |
| Drawdown thresholds | £300, £600, £1,000 | §10, §15 |
| Betfair website loss limit | £50 a day | §10 |
| Finalization deadline | off + 7 days | §11, §12 |
| BSP cutoff | off + 24 h | §11 |
| First pilot-capture after a session | the same evening | §11 |
| N (eligible bets in the primary sample) | 400 | §15 |
| Final date | 2027-06-30 | §15 |
| PT4's bets join the sample | yes, if the first session after the PT4 receipt runs under PT4's registration receipt | plan; contract p1 |
| Minimum clusters G | 15 | plan; contract p1 |
| measurement_unresolved share | at most 1% | plan; contract p1 |
