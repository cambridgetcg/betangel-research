# Pilot 2 engineering note: how the P2-LP executor would keep the page's promises

Status: proposed, for Astra's review. Nothing here is built. It describes the code that stages PR4 to PR8 of the [plan](betfair-pilot-2-plan.md) would add, against main c86764a. The [Pilot 2 page](betfair-pilot-2-policy.md) wins on any difference, and RS-19 and its [engineering note](betfair-pilot-engineering.md) ("eng") stay as they are.

For Astra, Yu and the Air's Claude. Author: lab-Claude, 2026-11-05, from the lab's Pilot 2 design of 2026-11-03.

- The sections follow eng's numbering. A section says only what differs. Where it is silent, eng applies to Pilot 2 as written, with "pilot" read as "pilot_lp" and contract p1 read as contract p2-lp.
- File:line citations are to main c86764a. Its betangel_research/ is byte-identical to 35da293, where the design read them.
- *Inference* marks what has not been checked against Betfair or run.

## 1. What it amends
Nothing, until Pilot 2's go-live. The lines Pilot 1's documents would gain then are listed on the page, under Changes to Pilot 1's documents.

## 2. Scope: one lineage inside Pilot 1's profile
- **Identity.**
  - Contract `research/pilots/p2-lp.json`, schema `betangel.pilot-contract-lp/v1`, rule P2-LP. It has its own loader. Pilot 1's loader refuses this schema, and Pilot 2's refuses Pilot 1's.
  - customer_strategy_ref `p2-lp`; intent source `pilot_lp`; journal records are note events under the key `pilot_lp`.
  - Profile `default`, the same as Pilot 1. A15 refuses a second profile on the same account for place, cancel, reconcile and settle (arming.py:42, :46-62, :385-406, :460-468; F76), and the Keychain credentials are per profile (arming.py:235-239). A separate profile would make every Pilot 2 placement, cancel and settle fail.
  - Its own config file, `STATE_ROOT/profiles/default/config-p2.json`, read only by Pilot 2's commands. Pilot 1's config.json and its exact-equality PA3 check (pilot_gate.py:221-257) do not change.
  - Its own worktree at its own registered commit.
- **Shared with Pilot 1, on purpose.** One Keychain credential set, one live journal and hash chain, one KILL file, one `.gateway.lock` (paths.py:73-74), and one recording admission and live-owner lock (paths.py:90-98). Every R-rule folds the whole journal, so each pilot's R6, R9, R10 and R12 see the other's orders. R12 at £0 does not misfire, because both pilots' orders are in the journal.
- **What the executor may send:**
  - for one PLACE record, before the send deadline: a BACK of exactly £2.00 at the best lay minus one tick, then a LAY of exactly £2.00 at the best back plus one tick, never above 6.00. Both are LIMIT and LAPSE, one instruction per request, each with its own customer order ref;
  - whole cancels of its own legs only, named by this open's bet ids;
  - in its probe sessions only, the fixed drill probe (§14).
- **The BACK goes first.** If the second send fails, the leg left resting can lose £2, not £10.
- **The one exception is the kill.** Pilot 2's own-scope kill, and so its auto-kill, cancels every open order of ours, Pilot 1's and manual ones included, as Pilot 1's kill does (gateway.py:3088-3095).

## 3. Who starts, stops and changes it
- Commands: `plp-register`, `plp-run`, `plp-capture`, `plp-end-session` and `plp-kill`, all in cli.py, which stays the only module that imports the gateway.
- One pilot session per UTC day across both pilots. `plp-run` refuses to arm:
  - on a UTC day with a Pilot 1 session;
  - while any Pilot 1, manual or Bet Angel bet of ours is open or unsettled;
  - on battery power.
- During a session the same checks run again at each open. A Pilot 1, manual or Bet Angel bet of ours that is open or unsettled HALTs it, as manual_bet_open_during_session does for Pilot 1 (pilot_gate.py:318-334; gateway.py:1422-1425). Sends are refused while any Pilot 1 session's window is open (pilot_gate.py:337-343).
- Pilot 1's code is unchanged, so nothing on Pilot 1's side refuses a Pilot 1 session after a Pilot 2 session on the same UTC day. That half of "one pilot session a day" is Yu's rule.
- It holds a caffeinate assertion tied to its pid. Yu attends, and the lid stays open.
- Typed lines are Yu's, as in eng §3.

## 4. The decision
- **Market:** Pilot 1's predicates (eng §4): GB or IE, event type 7, WIN, ODDS, one winner, CLASSIC ladder, at least 5 ACTIVE runners, off = the fresh catalogue row's marketStartTime.
- **Pre-filter, for timing only and never as evidence.**
  - For each market on Pilot 1's scheduler list (pilot_run.py:1359-1385), one delayed-key listMarketBook read with EX_BEST_OFFERS and virtual prices on, between off −540 s and off −460 s. That is before the planned open at off −450 s, which is −300 s less OPEN_LEAD (pilot_run.py:48).
  - A market is flagged only if isMarketDataDelayed is true and some ACTIVE runner meets the runner rule below. A book that is not delayed is discarded, so the pre-filter can never become a bet-free live-key read (F46).
  - Unflagged markets are never opened or reserved.
- **Reservation and evidence.** Reserve at or after off −300 s, exactly as Pilot 1 does. Then fetch one fresh live-key catalogue row and one decision book, with isMarketDataDelayed false and virtual prices on. Pilot 1's decision book has none (api.py:740-745), and its default stays off. Classify once and write one PLACE or SKIP record. There is no second fetch. The book must be observed between off −300 s and off −285 s, else SKIP(window).
- **Runner.**
  - In band: ACTIVE; best back b and best lay l both on the CLASSIC ladder; b < l; 1.50 ≤ b ≤ 5.90.
  - Eligible: l is exactly 3 or 4 CLASSIC ticks above b.
  - Choice: the lowest b, then the lowest l, then the lowest selection id. A tie is never a SKIP.
  - No depth predicate: both legs rest on new, empty levels.
  - New SKIP codes: `no_runner_in_band` and `spread_out_of_band`.
  - Why 3–4 ticks: at 2 ticks the two inside legs land on one price, and at 1 tick they cross; V8 refuses both (validate.py:257-268). A wider spread puts the inside BACK more than 3 ticks from the best back, which V13 at 3 refuses (validate.py:230-239, :289-290).
- **Legs.**
  - Each leg is sent on its own fresh send book (A18, virtual prices on). It must pass A1–A19, Pilot 2's PA checks against Pilot 2's receipt, V0–V14 and V9b with V13 at most 3, R1–R13 with R13 per leg against the decision record, and the first-byte gate (eng §5).
  - Both legs are sent before the send deadline, off −240 s.
  - **Passive check, per leg.** It only refuses, never re-decides. On that leg's send book, best back < leg price < best lay. Our own resting BACK counts: it is the LAY's best lay at l − 1, and b + 1 is still inside the spread. Otherwise `leg_not_passive`: that leg is not sent, and the market is latched.
  - **Incomplete pair.** If the BACK was sent and the LAY is not (not passive, refused, late or FAILED), the BACK is whole-cancelled at once, with KILL cases 1–4, as Pilot 1's remainder cancel does (pilot_run.py:1749-1793). The decision is recorded as `pair_incomplete`. If the BACK was not sent, nothing is sent.
  - **An unknown place outcome** on either leg triggers auto-kill (§8). An UNKNOWN LAY may be resting with £10 of liability, and an UNKNOWN BACK is neither sent nor unsent.

## 5. The first-byte gate
Unchanged, and applied to each leg.

## 6. Capacity, durable stops and carryover
- One decision per market for the contract's life. R13 enforces it, as in eng §6. Every reservation uses up its market, SKIP included.
- **One race in flight:** no reservation while any Pilot 2 leg is EXECUTABLE or unresolved.
- **Slots:** at most 5 PLACE records per UTC day, and 10 for the contract's life. The 5 is Yu's 2026-11-03 number; the 10 is proposed. Both are Yu's to confirm at the build consent.
- **Room:** a PLACE is allowed only if (the day's worst-case equity − £10) ≥ −£20 and (the pilot's worst-case equity − £10) ≥ −£100. This is checked before the reservation and again at classification. The gateway's R10 at £20 checks the day once more (risk.py:327-348, :378-379).
- **SKIP streak:** five live-key SKIPs in a row end the session (F46).
- Stops are folded from the journal, as in eng §6. Carryover is keyed by contract p2-lp's sha256.

## 7. Registration and identity
- `plp-register` writes Pilot 2's receipt in `STATE_ROOT/profiles/default/pilot-lp/`. It binds the contract sha256, the code commit and tree, the code-hash digest and config-p2.json's sha256. Pilot 2's daily arming token is keyed by config-p2.json's sha256 (arming.py:92-96), so arming one pilot never arms the other.
- **What Pilot 1's unchanged code does with Pilot 2's records.** Read on main; to be pinned by the cross-version check before PR5 merges (§14).
  - Pilot 1's fold handles only notes keyed `pilot` (pilot.py:1941). So its "another contract" refusals (pilot.py:2010-2016; pilot_run.py:758-762; pilot_gate.py:291-306) never fire on `pilot_lp` notes.
  - An intent whose source is not `pilot` sets a review stop if a Pilot 1 session is armed, and is otherwise ignored (pilot.py:2376-2383).
  - An open or unsettled bet whose source is not `pilot` halts a Pilot 1 placement open as `manual_bet_open_during_session` (pilot_gate.py:318-334; gateway.py:1422-1425). So an unsettled Pilot 2 bet stops Pilot 1 rather than mixing with it.
  - Pilot 1's own-scope kill sweeps Pilot 2's legs through journal bet ids and the refs it has seen (gateway.py:3095). With an untrusted journal it sweeps by Pilot 1's config refs only (gateway.py:3088-3091).
  - Journal payloads are validated on append only (journal.py:199-219, :296), so Pilot 1's code can read `pilot_lp` intents (*Inference*).
- **Recording admission.** The executor holds one recording admission for its whole session, as `pilot-run` does (pilot_run.py:1005-1015, :453-468), so both legs rest under it. A typed live `place` instead takes a fresh admission and is refused while one is held (cli.py:1518-1527; admission.py:667-670), and release is refused while any order is still open (admission.py:410-435). That is why the attended manual drills of November 2026 rest one order at a time ([plan](betfair-pilot-2-plan.md), Before Pilot 2).
- **Code identity.** code_hashes covers every betangel_research/*.py and betangel_research/betfair/*.py file (receipts.py:121-125). Pilot 2's code therefore changes the digest of any later Pilot 1 checkout taken from main. Hence the merge order in §14.
- **Fallback.** If Astra rules the separate record name unsafe, an explicit tolerance in Pilot 1's fold replaces it. It would merge after Pilot 1's PT4, with Pilot 1 re-registering (eng §14) and a fresh yes from Yu.

## 8. Commands, cleanup and settlement
- **Rest.** Both legs rest inside the same open, which keeps `.gateway.lock`. There is no Betfair call during the rest. Local checks run every 1 s, and the first case that applies wins:
  1. The journal is unreadable: the handoff at once (KILL case 1).
  2. A kill event after our submits: if HALT `kill_incomplete` follows, our own pull (case 3); with no outcome, no cancel and no Betfair read for up to 30 s, then the handoff (case 4).
  3. A KILL file with no kill event: our own pull at once (case 2).
  4. Any other event appended by another process, such as a HALT or a stop: pull.
  5. The stop flag (SIGINT, SIGTERM or SIGHUP; pilot_run.py:141): pull. Pilot 1 prints a handoff after a submit (pilot_run.py:1913-1982); Pilot 2 pulls instead.
  6. Wall and monotonic clocks more than 2 s apart (a sleep or a clock jump): pull.
  7. The wall clock more than one poll past the cancel point, for example after a SIGSTOP, which the drift check cannot see: pull at once. At or after the off, treat it as a failed cancel.
  8. Off −60 s, by the PLACE record's off: the scheduled cancel.
- **A pull** reads both legs by bet id, whole-cancels each leg still EXECUTABLE (the LAY first, one instruction per request), reads both legs again, and journals a session stop. The scheduled cancel does the same but lets the session go on.
  - A leg that filled while resting is resolved by these reads instead of being left UNKNOWN (lifecycle.py:283-297).
  - Matched parts ride to the result. Nothing is hedged, exited, re-priced, replaced or retried.
  - Why −60 s: a failed cancel still leaves the kill's re-sweeps at T0 + 20 s and + 35 s (killswitch.py:43) before the off. *Inference:* it also avoids the last minute, where late fills are most likely to be informed ([lp-study](lp-study.md), What it cannot show).
- **Failed cancel** (refused, an error, a timeout, an UNKNOWN the second read does not resolve, or a leg still EXECUTABLE): auto-kill.
  1. Write the KILL file.
  2. Close the open, which releases the lock.
  3. Run Pilot 2's own-scope kill as a child process. It takes no lock, sweeps refs and bet ids, and re-sweeps at T0 + 20 s and + 35 s. Meanwhile the executor makes no Betfair call.
  4. Print the handoff with the kill's result, and end the session. The kill journals a HALT, so Yu types RESUME LIVE before either pilot places again.
- **Suspended market at the cancel point.** *Inference, to check against Betfair's documentation:* cancels are refused while a market is suspended, and nothing can match then. The code is `cancel_market_suspended`, with no auto-kill. The executor reads the market status and our legs by bet id every 5 s. If the market reopens before the off, it sends one more whole cancel. If that is refused, or the market turns in play, LAPSE acts. The handoff is printed meanwhile, and these cases are counted apart.
- **Process gone** (SIGKILL, a crash, power loss, or a sleep with no wake): nothing on the Air cancels. The legs rest until the off, at most about 4–5 minutes, and lapse. A leg can still match until then, bounded at £10. Yu runs `plp-end-session`, which prints both handoffs, then Pilot 2's kill or the phone app. Crash recovery hands off any leg still EXECUTABLE, and arming refuses until it is resolved, as Pilot 1's does (pilot_run.py:232-261, :897-899).
- **Network drop:** the cancel fails, so auto-kill runs. The kill fails too, giving `kill_incomplete` and a HALT. The handoff then says to cancel in the Betfair phone app over mobile data now, or LAPSE acts at the off.
- **Login failure with legs resting:** auto-kill. The emergency login skips the success cap but not the failure cap.
- **The lock.** During the rest a typed `cancel` waits order_deadline_s + 5 s and then fails with `lock_busy` (eng §8). The arming banner names the stops that work: Ctrl-C, the KILL file, Pilot 2's kill (lock-free), and the website or app.
- **Dead-man: none, by choice.** Yu attends and is the dead-man, LAPSE is the backstop, and the £10 race bound holds. The Heartbeat API stays forbidden (A9; F58). The watchdog stays deferred (eng §16).
- **Settlement** is Yu's typed `settle-market`, one market at a time, as in eng §8. R10 books a loss on the UTC day the settle is typed.
- **Pilot 2's kill** sweeps `p1-fav-t5`, `p2-lp` and `bar-manual` from code constants plus the journal, so it finds both pilots' orders even when the journal cannot be read.

## 9. Caps in config-p2.json
Proposed values. The £ ones are derived from Yu's money numbers and are Yu's to set at the build consent. All are inside constants.py's ceilings.

| Cap | Value | Why |
|---|---|---|
| max_stake_per_order | £2 | one £2 leg |
| max_liability_per_order | £10 | R5 passes a £2 LAY at exactly 6.00 and refuses 6.20 (risk.py:296-299 uses ">") |
| max_market_exposure | £10 | no hedge credit, so a race's worst case is max(£10, £2) |
| max_total_exposure | £20 | equals the day stop |
| max_daily_loss | £20 | R10 checks the day stop again, counting open liability |
| external_exposure_tolerance | £0 | R12 halts on any exposure the shared journal cannot explain |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 decisions × 2 legs = 10 a day |
| Instructions per request | 1 | as Pilot 1 |
| max_price_deviation_ticks (V13) | 3 | admits a 4-tick spread's legs and nothing wider; Pilot 1's stays 2 |
| max_book_age_s | 2.0 s | as Pilot 1 |
| max_minutes_before_off | 10 | as Pilot 1; V9b checks it at each leg's send |

Pilot 1's config.json is unchanged.

## 10. The ledger
For safety only. It is separate from the R-rules and can only bring a stop earlier, as in eng §10.
- **W, per decision:** minus risk.worst_case_market_loss over its legs, with no hedge credit (risk.py:136-154), using checked current-order reads. Before those reads it is the contract bound, −£10. Pinned examples:
  - a pending lone LAY at 6.0: −£10;
  - an unsettled round trip, BACK 5.1 and LAY 5.0: −£8;
  - both legs checked with £0 matched: £0;
  - nothing sent: £0.
- A contradiction moves the decision back to −£10 and sets a review stop.
- **S, settled and verified,** needs all of: Yu's typed settle-market; both legs final; one WINNER and no removal; the per-market commission check; and a P2-PT4 receipt showing a matched, settled LAY (F37).
- **Commission** is 2% on each market's net (pnl.py:24-83). Both legs are in one market, so a per-bet check would raise false gaps whenever both fill.
- **No pause.** The hard day and pilot stops and the 10-decision cap replace one.

## 11. Final execution, classes and value
- **Per leg,** Pilot 1's final execution record is unchanged: C and L reads per bet id, exact Decimal agreement, deadline off + 7 days (eng §11). C rows also keep placedDate and matchedDate (*Inference*: listCurrentOrders returns them). BSP is frozen by Pilot 1's rule.
- **Classes per decision,** the first that applies: protocol_violation; definitely_unsent (no leg sent); void; post_decision_removal, including a non-runner removal that cancels our unmatched orders (*Inference, to check against Betfair's Exchange Horse Racing Rules:* a reduction factor of 2.5% or more does so); measurement_unresolved (any sent leg); missing_bsp; eligible.
- A decision with no fill is eligible and worth 0. Pilot 1's unmatched class and its 80% fill-rate gate do not apply: they would make any liquidity pilot INVALID by design.
- **Value at BSP, reported and never deciding.** With final matched sizes mB at price B and mL at price L: if the runner wins, net = mB(B − 1) − mL(L − 1); if it loses, net = mL − mB. 2% comes off whichever net is positive. Value = p·net_win + (1 − p)·net_lose, with p = 1/BSP.

## 12. Verdict, tables and reporting
- **The verdict,** fixed in advance and judged once at the end over every PLACE record, P2-PT4 included:
  - INVALID: any protocol_violation, including the day's or the pilot's worst-case equity ever going below −£20 or −£100.
  - FAIL, if any of these happens: a leg ends measurement_unresolved; a sent leg is still EXECUTABLE at the off, so LAPSE removes it (cancels refused because the market was suspended are counted apart); more than one PLACE record needed an auto-kill or a handoff to Yu, suspended-market cases apart.
  - PASS: 10 PLACE records with none of the above.
  - INCOMPLETE: placing ended earlier with none of the above, reported as "no failure in k decisions", never as PASS.
- **Descriptive tables,** registered before the first PLACE record, computed once after the data lock and never deciding. Groups under 30 values are listed without intervals; with the few decisions expected, D1–D4 will be short lists, not estimates. P2-PT4 decisions are shown apart.
  - D1, fills: per leg none, partial or full; inventory state (no fill, LAY only, BACK only, round trip, partial, pair_incomplete); legs matched in their own place report (taker fills); shown beside lp-v1's modelled fills at off −5 min and labelled not like for like.
  - D2, time to fill: matchedDate − placedDate.
  - D3, adverse selection with no read after the decision: per sent leg, m = the move from the decision-book mid to BSP against the leg, in points; adverse selection = mean m over filled legs minus mean m over unfilled sent legs on the same side.
  - D4, value at BSP by inventory state and by price band (1.50–2.98, 3.00–4.90, 5.0–5.9).
  - D5, the funnel: pre-filter flags, reservations, PLACE and SKIP by code, leg_not_passive, pair_incomplete, live-key reads per PLACE, cancels on time, auto-kills, handoffs, cancel_market_suspended and UNKNOWNs.
  - D6, settled profit after commission and the running total.
- **Interim:** counts and codes only, on Pilot 1's allowlist (eng §12), plus legs sent, legs refused as not passive, inventory-state counts and pre-filter flags. No value, price-band or profit figure before the end. Yu's private risk view shows the ledger and the room left.
- **Data lock and publication** as in eng §12. Nothing enters Git without Yu's approval of the exact fields.

## 13. Still forbidden
eng §13's bans stay, except that this executor may send lays. Still: more than one instruction per request in the executor's own sends and cancels (the kill's existing sweep, killswitch.py:182-185, is the exception), a partial cancel, Heartbeat, PERSIST, BSP orders, a second decision in a market, a replace, any action on Pilot 1's or manual bets other than the kill's sweep of all our refs (§2), and agents typing any line.

## 14. Stages, gates and tests
- **Merge order (Yu, 2026-11-04, relayed: "2.是").**
  - No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit.
  - The order path (PR5) merges only after Pilot 1's PT4 receipt exists, so the shared placement path is first proven live on Pilot 1's simpler shape, and after Gate B if PR3 exists.
  - Every Pilot 1 test passes unchanged at every Pilot 2 commit, and contract p1's sha256 (4dbccb5a…) is unchanged.
- **The cross-version check,** a harness run before PR5 merges: Pilot 1's registered commit folds a journal written by Pilot 2's code to the identical Pilot 1 state, with the expected review stop and HALT.
- **PR5's reservation pre-check** models the pair at the contract bound, a LAY £2 @ 6.0 plus a BACK £2. Today's evaluate_pilot_reservation models one BACK (risk.py:544-566).
- **Adversarial tests,** by stage:
  - PR4: each predicate left out in turn; edges at 1.49/1.50/5.90/6.00; 2-, 3-, 4- and 5-tick spreads; the tie order; the passive check with our own BACK resting; room at −£10.00 and −£10.01; the 11th PLACE refused; the pinned ledger examples; the verdict truth table; restart determinism.
  - PR5: eng §14's list rerun for two legs; the LAY refused, not passive, late or FAILED after the BACK was sent, so the BACK is cancelled at once; an UNKNOWN place outcome on either leg triggering one auto-kill; a Pilot 1, manual or Bet Angel bet open at a Pilot 2 open HALTing it; sends refused while a Pilot 1 session's window is open; probes bound by R13 to their drill record; the spread narrowing between decision and send; R5 at 6.00 and 6.20; a cancel naming another open's, Pilot 1's or a manual bet id refused; a fill racing the cancel resolved through reconcile, with R3 blocking placing meanwhile; arming Pilot 2 never arms Pilot 1.
  - PR6: Ctrl-C while idle, mid-send and while resting; a KILL file during the rest; case 4's 30 s wait; a wake more than one poll past the cancel point pulling at once, and at or after the off counting as a failed cancel; a network error at the cancel triggering one auto-kill; no Betfair call during the rest; the pre-filter refusing a non-delayed book; arming refused on battery, on a Pilot 1 day, or with a Pilot 1 bet open; 5 live SKIPs ending the session.
  - PR7: exact C/L agreement per leg; partial fills; the lay's profit sign and basis; commission per market; the verdict identical with and without the tables.
  - PR8: the probe shape pinned (any other price, size or side refused); a dry day that sends nothing; the stand-in transport asserting zero order calls in P2-PT3.
- **P2-PT2:** a delayed-key dry day of the scheduler, pre-filter and predicates, with no gateway and no order. It produces a counts receipt and is a pipeline test, not a rate estimate.
- **P2-PT3, before go-live, with no live order:** the eight fault drills, each with a receipt: the scheduled cancel; Ctrl-C; the KILL file; Pilot 2's kill; a network cut at the cancel point; SIGKILL, then Yu's kill; SIGSTOP past the cancel point; a sleep or a clock jump. They run against a stand-in transport that sends nothing, in a scratch STATE_ROOT with its own journal and KILL file, no Keychain and no network, and the kill in them goes to the stand-in too, so nothing touches Pilot 1's live state. A leg fully matched before the cancel, and a market suspended at the cancel point, are tested offline only.
- **The probe sessions,** the first armed sessions after registration: one drill race each, with Yu injecting one of the eight faults on real resting orders, using only the fixed probe (LAY £1.00 @ 1.01 plus BACK £1.00 @ 1000), each with a receipt. Most faults end the session, so this takes up to 8 sessions, each on a day with no Pilot 1 session; one race is two orders, inside the order caps. The probe is exempt from V13, the passive check and the runner rule. R13 binds each probe to its drill record instead of a decision record, a drill race uses up its market, and its orders are outside the ledger and the verdict.
- **No PLACE record** is made until every probe drill has a passing receipt. A failed drill stops Pilot 2 until Astra has reviewed it and Yu decides again.
- **P2-PT4:** the armed sessions after the probe sessions, at most 5 decisions each, until a P2-PT4 receipt shows a matched, settled LAY checked on the website.

## 15. How it ends
As on the page. Then the data lock (eng §12). The verdict and the tables run once on the Air, in the terminal, and Astra recomputes them there.

## 16. Deferred
- A dead-man or watchdog.
- Mark-outs during the rest.
- Gate B: a counts-only table over Pilot 1's stored −300 s decision books. It would change Pilot 1's interim reporting rule (eng §12), so it needs Yu's plain-words yes, Astra's review, and a merge before Pilot 1's first PLACE record.
