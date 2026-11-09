# Pilot 2 (P2-LP): a plain capped bot for two-sided quotes

Status: proposed, for Astra's review. Nothing on this page is approved, and no Pilot 2 live order is allowed before Yu's go-live consent here. [RS-19](betfair-pilot-policy.md), Pilot 1's other documents and AGENTS.md are unchanged by this commit. The lines they would gain at Pilot 2's go-live are listed under [Changes to Pilot 1's documents](#changes-to-pilot-1s-documents-at-pilot-2s-go-live) and are not in force. The code ships switched off (`p2_lp.IN_FORCE` False); only the go-live commit, which also applies those lines, sets it True.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, after Yu's go-ahead to draft it (relayed by the Air's Claude, 2026-11-05: "可以 let lab go ahead. 得"). Rewritten on 2026-11-09 as the plain capped bot Yu chose that day ("Plain capped bot (Recommended)" and "−10 min, pulled at −5 min (Recommended)"; [plan](betfair-pilot-2-plan.md), Decided).

- **Naming.** In [Pilot 1's plan](betfair-pilot-1-plan.md) (Goal and Sample), "Pilot 2" means whatever follows Pilot 1. On this page and its companions, Pilot 2 is this liquidity-provision pilot, P2-LP.
- **Companions.** The [engineering note](betfair-pilot-2-engineering.md) says how the code keeps these promises. The [plan](betfair-pilot-2-plan.md) gives what Yu decided, the reasons for each value, the stages and the timeline. The [G1 follow-up](betfair-pilot-2-g1-question.md) is the question for Yu to send to Betfair. This page wins on any difference.
- **No contract file.** The Money and Values tables below and the constants in `p2_lp.py` are the only copies of the rule's numbers. A test pins them to each other.

## What it allows
One plain capped bot may place small live orders through Pilot 1's existing order gateway. Its command is `p2-run`, its config file `config-p2.json`, its order ref `p2-lp` and its intent source `pilot_lp`. It runs on the lab Mac (the MacBook Pro), only in sessions Yu arms and watches. Yu chose on 2026-11-05 to move all live work there from the Air, so only one Mac trades ([plan](betfair-pilot-2-plan.md), Decided).

In one race it sends one £2 BACK at the best lay − 1 tick, then one £2 LAY at the best back + 1 tick, on one runner whose spread is 3 or 4 ticks. Both sit inside the spread, so they wait to be filled. It decides at about off −590 s (10 minutes) and pulls (cancels) whatever is still unmatched at off −300 s (5 minutes). A matched part rides to the result.

It runs in Pilot 1's Betfair profile and live journal, because A15 allows one profile per account. It uses Pilot 1's existing gateway, risk caps, journal, reconcile and kill, with its own config's numbers. It has no contract, records, ledger or stops of its own: its counts come from the journal's order records with ref `p2-lp`, and its only record of its own is one typed ARM record a session. Nothing of Pilot 2 enters Pilot 1's 495 bets, its −£150 pause or its running total.

## Five principles
These are RS-19's five principles. Each says what it means here. Where a detail is unclear, follow the principle and write down the reasoning.

**Transparency.** Every order is journaled before it is sent, with its intent, risk decision and submit, and its outcome after. SKIPs, screens and refused legs are printed in the session with their codes, not journaled. Each session ends with a short printed summary, and `p2-summary` prints the counts at any time. *Why:* the same as RS-19. Yu should be able to see what the code did without having to trust it.

**Truth.** There is no verdict. The results are counts and P&L: the journal's counts, Betfair's cleared profit and commission, and Yu's own check on the website. A disagreement between them blocks the next ARM until Astra has looked; that is Yu's procedure, not code. Anything we cannot verify counts at its worst case in the gateway's day stop. *Why:* with at most 10 races, money can answer nothing, and Yu chose no registered verdict on 2026-11-09.

**Understanding.** It tests operations: whether a plain bot can post two passive £2 orders, rest them, pull them in time and stop safely. It also shows whether and how live fills happen; with the few races expected, those are counts, not rates. No mispricing is known. The research round's judgement, which is not evidence, leans to "we are picked off" ([lp-study](lp-study.md), Predictions). lp-v1 could neither support nor rule that out, and its only released statistics, a move against queued orders after they filled, are partly by construction and say nothing about orders inside the spread, which is where Pilot 2's sit ([lp-study](lp-study.md), Results). *Inference:* the honest expectation is a few pence negative per race, very few races, and an answer about operations only. *Why:* the same as RS-19. Any larger liquidity step needs these operations facts first, and offline history cannot show queue position, virtual prices or latency.

**Simplicity.** One market type and one race at a time. One £2 BACK, then one £2 LAY, both LIMIT and LAPSE, before the off. They rest until a timed whole cancel at off −300 s, and a matched part rides to the result. Nothing is hedged, re-priced, replaced or retried. Every departure from RS-19's simplicity is listed under [What it relaxes](#what-it-relaxes). *Why:* every extra moving part is one more way to lose money by accident.

**Non-forcing.** Skipping is the default. Any doubt means skip, and nothing is ever resent. Day and lifetime numbers are ceilings, never targets. A stop waits for Yu. *Why:* the same as RS-19.

## One race
1. **Screen**, off −840 to −780 s, on the delayed key only. One fresh catalogue row and one delayed book. A market that fails the screen is skipped with no live-key read (F46).
2. **Open**, off −740 s. One gateway open as `place`, with config-p2.json: the lock, the login, the startup reconcile and A1–A19.
3. **BACK**, on the gateway's own fresh send book, observed at off −590 to −575 s. The runner rule and the passive check run on that same book, which the gateway's checks then judge.
4. **LAY**, only if the BACK was placed, on a fresh send book observed by off −532 s. Our own resting BACK counts in that book.
5. **Rest** until off −300 s. No Betfair call; local checks every second.
6. **Pull** at off −300 s: a whole cancel of each leg still unmatched, the LAY first.
7. Close the open. Then the next market, or the end of the session if any leg matched.

| Value | Constant | Number | Unit | Whose | What it means |
|---|---|---|---|---|---|
| Best back, from | `BAND_H[0]` | 150 | hundredths | *proposed* | 1.50, where lp-v1's band and Pilot 1's start |
| Best back, to | `BAND_H[1]` | 590 | hundredths | *proposed* | 5.90, so the LAY at best back + 1 tick is at most 6.0, Yu's lay cap |
| Spread, from | `SPREAD_TICKS[0]` | 3 | ticks | *proposed* | At 2 ticks the two inside legs land on one price, and at 1 tick they cross |
| Spread, to | `SPREAD_TICKS[1]` | 4 | ticks | *proposed* | A wider spread puts the BACK more than 3 ticks from the best back, which V13 at 3 refuses |
| Active runners | `MIN_ACTIVE` | 5 | count | *proposed* | At least 5 ACTIVE runners, as Pilot 1 |
| Screen, from | `SCREEN_S[0]` | -840 | seconds | *proposed* | Off −14 minutes |
| Screen, to | `SCREEN_S[1]` | -780 | seconds | *proposed* | Before the open, with time to spare |
| Open | `OPEN_S` | -740 | seconds | *proposed* | 150 s before the decision, as Pilot 1's open lead |
| Decide, from | `DECIDE_S[0]` | -590 | seconds | Yu, 2026-11-09 ("−10 min") | The BACK's send book is observed in this window |
| Decide, to | `DECIDE_S[1]` | -575 | seconds | *proposed* | A 15 s window, as Pilot 1's |
| LAY book by | `LAY_BOOK_BY_S` | -532 | seconds | *proposed* | With the 2 s book age, the LAY leaves by about off −530 s |
| Pull | `PULL_S` | -300 | seconds | Yu, 2026-11-09 ("pulled at −5 min") | If the pull fails, about 5 minutes are left to cancel by hand |
| Rest check | `SLICE_S` | 1 | seconds | *proposed* | Local checks every second, with no Betfair call |
| Late wake | `LATE_WAKE_S` | 5 | seconds | *proposed* | A check more than 5 s late (a stalled or sleeping Mac) pulls at once |
| Skip streak | `SKIP_STREAK` | 5 | count | *proposed* | 5 refused BACKs in a row end the session. This limits live-key reads that end with no order (F46) |

## Money
Yu chose the first five numbers on 2026-11-03, in a question that offered them for a hand trial. At the build consent Yu confirms them for this bot, together with each row marked *proposed* and each *proposed reading*. Only Yu changes a money number.

| Value | Constant | Number | Unit | Whose | What it means |
|---|---|---|---|---|---|
| Stake, each leg | `STAKE_P` | 200 | pence | Yu, 2026-11-03, to confirm | £2 a side: one £2 BACK and one £2 LAY on one runner. A BACK can lose £2. A LAY at price L can lose £2 × (L − 1) |
| Highest lay price | `MAX_LAY_H` | 600 | hundredths | Yu, 2026-11-03, to confirm | 6.0. One LAY can lose at most £10 |
| Races a day | `RACES_DAY` | 5 | count | Yu, 2026-11-03, to confirm | At most 5 a UTC day. A ceiling, never a target. *Proposed reading:* a race counts from its first submit; a market screened, skipped or refused before any submit does not count |
| Day stop | — | 2000 | pence | Yu, 2026-11-03, to confirm | −£20, as the gateway's R10 at £20 in config-p2.json. R10 counts settled losses, matched bets not yet settled and open orders. settle-market books each bet at its worst case. *Proposed reading:* a hard ceiling. A BACK goes only if the race's full £10 still fits, so the worst possible day is −£20. The other reading, "stop once −£20 is reached", would allow a −£30 day |
| Pilot stop | — | 10000 | pence | Yu, 2026-11-03, to confirm | −£100. It holds by count: 10 races × £10 |
| Worst race | `PAIR_WORST_P` | 1000 | pence | follows from Yu's numbers | −£10: the LAY fully matched and its runner wins. The BACK loses only when its runner loses, so the two never add up |
| Races in total | `RACES_TOTAL` | 10 | count | *proposed* | 10 × £10 = £100, so −£100 holds by count, as Pilot 1's 495 × £2 does |
| Open at once | — | 1000 | pence | *proposed* | One race at a time, and nothing else of ours open or unsettled when a BACK goes, so at most £10 of worst case is ever open. The session ends after any race with a matched leg |
| Pause | — | none | — | *proposed* | The hard stops and the 10-race cap replace a look-and-go-on pause. Yu may add one |
| £ caps in config-p2.json | — | — | — | *proposed*, derived from the rows above | Stake £2, liability £10, market £10, total £20, daily loss £20, outside exposure £0: the gateway's own copies of these limits ([note](betfair-pilot-2-engineering.md) §9). Pilot 1's config is unchanged |
| Betfair website loss limit | — | 5000 | pence | Yu, 2026-10-21 | £50 a day, unchanged. A backstop outside the code |
| Overall budget | — | open | — | Yu decides | Pilot 1's worst case of −£990 (RS-19, Money), plus Pilot 2's −£100 and the November manual drills' −£10, is −£1,100, which is over Yu's £1,000 |

- **The budget.** At the build consent Yu decides between two options:
  - raise the overall budget;
  - keep £1,000, with the account balance and no top-ups as the joint stop, so that whichever pilot runs later may be starved of funds.

  Yu has said a higher maximum loss is acceptable ("可以上調max loss", relayed by the Air's Claude, 2026-11-04). The Air's Claude proposed £1,100, which now covers −£1,100 exactly. Neither is Yu's written yes. The November manual drills are described in the [plan](betfair-pilot-2-plan.md), Before Pilot 2.
- **Separate from Pilot 1.** Pilot 2 has no ledger. Pilot 1's slots, pause and running total are untouched, and Pilot 2's orders never count in them. The option Yu chose on 2026-11-03 said so: "NOT part of Pilot 1's counts".
- **Losses pay no commission.** Commission is Basic 2% on each market's net, checked per market and never per bet, because both orders of one race are in one market.

## Arming a session
- Yu types `p2-run --mode live --confirm-live live-<cfg12>-<YYYYMMDD> --until HHMM --races N [--bet-angel-paused]` at the lab Mac's own keyboard. The flag is needed while Bet Angel's coexistence is unverified (A12). The token is keyed by config-p2.json's sha256, so arming one pilot never arms the other.
- **Refused before any line,** with nothing journaled:
  - not live, or not the default profile; config-p2.json missing or not exactly as the note §9 pins it;
  - `SSH_CONNECTION`, `SSH_CLIENT` or `SSH_TTY` set (friction only);
  - static arming fails (A2, A11, A13);
  - the checkout's commit is unknown or its tree is dirty;
  - the KILL file, a HALT, an open order, an unresolved market, a login-ledger problem, or Pilot 1's armed-session file;
  - a Pilot 1 session today or its window open, or an earlier P2 ARM record today;
  - any bet of ours open or unsettled;
  - 10 races in total; N outside 1 to the smaller of 5 and the races left; N other than 1 while no race has run;
  - UNTIL less than 20 minutes ahead, or after 23:59Z;
  - the login cap.
- **The banner** shows config-p2.json's sha256 (cfg12), the commit, today's and total races, N, and the stops: the phone app, Ctrl-C, the KILL file and the own-scope `kill`. It warns that a typed `cancel` fails while legs rest, that the session ends after any matched leg, and that nothing heavy runs on the lab Mac while armed or an order rests. It asks for mains power with the lid open.
- **Then Yu types at /dev/tty:** `P2 ARM <cfg12> <YYYYMMDD> UNTIL <HHMM>Z RACES <N>`.
- **After the line:** the live-owner lock and recording admission, the checks again (and the commit must still be the one the banner showed), the credentials, caffeinate, and then the ARM record in the journal, appended under `.gateway.lock`, with the anchor published.
- **What it binds.** Every leg's gateway check needs that exact ARM record: this config's sha256, this session, today, before UNTIL, and within its race count. Only `p2-run` builds a gateway that can send for Pilot 2, and only after the line. This is friction, not cryptography; the rule that agents never arm is this page's.
- **Later lines are the existing ones:** `SETTLE MARKET`, `ACCEPT ABSENT`, `RESUME LIVE` and `RELEASE RECORDING GENERATION <N>`.

## How each fault ends
The full state machine is in the [note](betfair-pilot-2-engineering.md) §8. In short:
- **Before the BACK:** a refusal or an error moves to the next market or ends the session. Nothing has been sent.
- **The BACK refused or failed:** the next market. 5 refused BACKs in a row end the session.
- **The BACK's outcome unknown:** the gateway reconciles it in line. If it is not found, the handoff, and the session ends.
- **A stop between the legs** (Ctrl-C, the KILL file, another process's journal event): pull the BACK, end. A kill event or an unreadable journal: the handoff, end. The bot never races the kill.
- **The LAY refused or failed:** pull the BACK at once. The LAY's outcome unknown: reconcile; if not found, pull the BACK, hand off the LAY, end.
- **During the rest:** a stop, the KILL file, another process's event, a clock jump or a late wake pulls at once and ends the session. A kill event or an unreadable journal: the handoff for both legs, end.
- **At the pull:** nothing matched, the next market. A leg matched, the session ends with the settle line. A failed or unknown cancel, a suspended market or a leg still part unmatched: the handoff, end. LAPSE acts at the turn in play.
- **An exception after a send:** one best-effort pull (none if the pull itself raised), then the handoff for each leg not shown complete, and the lock is released.
- **The process gone** (SIGKILL, power loss, a sleep through the off): nothing runs. The race is bounded at £10 and LAPSE. The next ARM refuses until every leg is resolved.
- **The handoff** names each leg (bet id or ref, side, £2 at its price, runner, market), then the stops in order: the Betfair phone app, the website, and the own-scope `kill` run from Pilot 2's checkout, by its absolute path. With the pull at −300 s, about 5 minutes are left before the off.

## Who does what
- Only Yu starts a session, in person at the lab Mac's own keyboard, never over SSH, and stays for the whole session. There is at most one pilot session a UTC day across both pilots.
  - Pilot 2's code refuses to arm over SSH (friction only), while Pilot 1's armed-session file exists, on a day with a Pilot 1 session or an earlier Pilot 2 session, and refuses to send while a Pilot 1 session's window is open.
  - Pilot 1's code is unchanged and does not know about Pilot 2's days. So not starting Pilot 1 after a Pilot 2 session on the same day is Yu's rule on Pilot 1's side.
- Pilot 2 arms only when no Pilot 1 bet, manual bet or Bet Angel bet of ours is open or unsettled, and each BACK checks this again. The lab Mac is on mains power with the lid open, and the Betfair phone app is logged in as the stop away from the lab Mac. Nothing heavy (backtests, model studies, large downloads) runs on the lab Mac while a session is armed or an order rests (Yu's lab rule, 2026-11-05).
- Anyone at the lab Mac can stop it with Ctrl-C, the KILL file or the existing own-scope `kill`. Run from Pilot 2's checkout, the kill also sweeps `p2-lp` once config-p2.json exists; run from Pilot 1's checkout, it finds Pilot 2's legs only through a readable journal (What it relaxes, item 12). Since 2026-11-06, lab-Claude may run an own-scope kill whenever something looks wrong, without asking first, and tells Yu straight after (Yu: "Yes, without asking"; [plan](betfair-pilot-2-plan.md), Decided). Whether that covers the kill run from Pilot 2's checkout is open for Yu (plan, Open for Yu). It never runs the account-wide kill, deletes the KILL file or resumes. Any other agent may only create the KILL file. For the existing `kill`, the KILL file stops new sends at once, but its cancels wait for its Keychain reads: if macOS asks, each read waits up to 120 s for someone at the lab Mac to click Allow (order-path §9, step 7; whether macOS asks is unverified, F73). The Betfair website and phone app stop it from anywhere.
  - From the Air over SSH, creating the lab Mac's KILL file stops new sends but itself cancels nothing; a running bot pulls its own resting legs when it sees the file ([note](betfair-pilot-2-engineering.md) §8). A kill run on the Air engages only the Air's KILL file, so the lab Mac can keep sending.
  - There is no auto-kill. The own-scope kill cancels every open order of ours, Pilot 1's and manual ones included, as Pilot 1's kill does.
  - The gateway lock is held for about 7.5 minutes a race, from the open to the pull. Meanwhile a typed `cancel` waits for the lock and fails. The banner names the stops that work.
  - Yu settles after the session: `settle-market` for each market with a matched leg, after its result. The next ARM refuses until then.
- Every line the code asks a person to type belongs to Yu, as on RS-19: the P2 ARM line, settle a market, accept an absent order, resume, release a recording generation and an account-wide kill. Yu types each one at the lab Mac's own keyboard, never over SSH (Yu's lab rule, 2026-11-05). Agents and scripts never type one and never supply a terminal for one.
- Agents build, test, review and summarise. The lab may write Pilot 2's pages and code as PRs, Astra reviews and Yu merges. Yu chose "Yes, PRs only" on 2026-11-03. Agents relay only the counts and codes `p2-summary` prints.
- Yu's lab rules of 2026-11-05 also bind: agents never arm; lab-Claude runs only the commands the repo's documents give to an agent, each time with Yu's OK, except the own-scope kill above (Yu, 2026-11-06); other agents on the lab Mac, and the Air's Claude over SSH, run no live command but may create the KILL file; the Air runs no live command; no agent reads the state root, recordings, receipts or certificate directory; and nobody runs `git init` or clones a repo into ~, ~/Library or ~/.config, which would make every live command refuse, while the kill would still engage the KILL file but cancel nothing (arming.py:62, :278; config.py:246, :294-295; cli.py:2079).
- Only Yu changes a money number or who may type a line.

## What it relaxes
Each item needs Yu's yes, asked in plain words, at the build consent (RS-19, Where the rest lives).

Against RS-19's rules for Pilot 1:
1. **Lays.** RS-19 allows only BACK orders.
2. **Two resting orders in one race.** RS-19 allows one bet per race.
3. **A timed cancel.** The unmatched part is cancelled at off −300 s, not straight after the place.
4. **A worst race of −£10**, set by the lay's liability, not −£2.
5. **V13 up to 3 ticks.** Each leg may be up to 3 ticks from the send book's best price on its side, not 2, because a 4-tick spread's inside BACK sits 3 ticks above the best back. This is in config-p2.json only.
6. **The gateway lock is held while orders rest**, about 7.5 minutes a race. A typed `cancel` fails during the rest. The KILL file, the own-scope kill and the website or app still work.
7. **One P2 ARM line a session** instead of a typed line per order, and no decision record: the ARM record and the gateway's checks bind every leg.
8. **No registration.** No frozen contract, registration receipt, PA1–PA4 or code-hash digest. The banner shows the commit and a clean tree is required. Yu's go-live consent names the commit and config-p2.json's sha256.
9. **No dry day and no probe sessions.** The fault drills run only in the offline test suite. The first live session has 1 race, with Yu watching both legs on the website.

Pilot 1 protections it weakens:

10. RS-19's "No other strategy, agent, script or scheduler places live orders" and AGENTS.md's one-contract exception stop holding. There is a second automated order source on the account.
11. Pilot 1's journal holds Pilot 2's orders and ARM records. Pilot 2 adds no record type: its orders are core intents with source `pilot_lp` and ref `p2-lp`, and its ARM record is the core kind `operator_confirm`, which every fold ignores. So Pilot 1's fail-closed "another contract in the journal" check does not fire, and no Pilot 1 code changes.
12. A kill from Pilot 1's checkout finds Pilot 2's legs only through a readable journal. Pilot 2's handoff therefore names the phone app and the website first, then the own-scope `kill` run from Pilot 2's checkout, by its absolute path, which sweeps `p2-lp` even when the journal cannot be read. It keeps the warning that a kill from Pilot 1's checkout misses Pilot 2's legs when the journal is unreadable.
13. RS-19's "The worst possible total is −£990, inside Yu's £1,000 trading budget" stops holding unless Yu raises the budget.
14. **Pilot 2's faults can stop Pilot 1.** There is no auto-kill and no in-session halt. But a Pilot 2 UNKNOWN counts toward the three-UNKNOWN HALT, which stops Pilot 1 too until Yu types RESUME LIVE. That includes the pull's cancel of a leg that matched, or part matched, during the rest, so a round trip's pull counts twice. A Pilot 2 order with an unknown outcome blocks Pilot 1's placing too (R3). A Pilot 2 settle typed on a Pilot 1 day uses that day's room for Pilot 1. These are the safe direction, but they cost sessions.
15. Pilot 2's live-key reads that end with no order add read-only-looking traffic on the £499 key both pilots need (F46).
16. **Pilot 1's code identity.** Pilot 1 runs from its own registered checkout. But once Pilot 2's order path is on main, any later Pilot 1 fix taken from main carries Pilot 2's code too, and needs Astra's re-acceptance and Yu's go-live consent over it.

Not weakened: Pilot 1's contract, config, caps, its one BACK per race, its immediate remainder cancel, pause, slots, PT4, verdict, data lock and handoffs.

## Cut from the page as first drafted
On 2026-11-09 Yu chose "Plain capped bot (Recommended)" ([plan](betfair-pilot-2-plan.md), Decided). Against this page as drafted from 2026-11-05 to 2026-11-08, these are cut:
- the operations verdict;
- the ledger, its worst-case equity room checks and its classes;
- value at BSP;
- the tables (D1–D6) and the capture;
- the drills: P2-PT2's dry day, the stand-in transport and the probe sessions on real orders;
- registration: the frozen contract `research/pilots/p2-lp.json`, its receipt, PA1–PA4 and the code-hash digest;
- the auto-kill, the suspended-market poll and the second cancel;
- Pilot 2's own records, fold, stops and its own kill;
- journaled SKIPs and screens, which are printed in the session instead;
- the end-session command: `p2-summary` and Yu's existing `recording-release` cover a crash;
- the battery check: caffeinate stays, and mains power with the lid open is Yu's procedure.

The [note](betfair-pilot-2-engineering.md) §16 lists everything left out, including what the 2026-11-09 designs proposed and this page never had: a fault acknowledgement line and a process pin.

Each cut protection needs Yu's yes in plain words at the build consent. Writing it here decides nothing.

## Three consents
1. **Build.**
   - Yu confirms the Money table: Yu's 2026-11-03 numbers for this bot, each *proposed* row (the £ caps included), the two *proposed readings* (how a race counts, and the day stop's reading) and the overall budget. There is no contract to freeze: the numbers are on this page and in the `p2_lp` constants, and a test pins them to each other.
   - Yu gives a plain-words yes to each item under What it relaxes and each cut above.
   - Gate A: on 2026-11-06 Yu chose to wait for it ("Count first (Recommended)"; [plan](betfair-pilot-2-plan.md), Decided). Pilot 2's code merges only after Gate A, the free Stage 0 count, passes. On 2026-11-08 Yu chose to have the code drafted before then, as PRs nobody merges ("Draft now, gates unchanged (Recommended)"; plan, Decided). Drafting moves no gate. Because the bot now decides at off −590 s, Gate A counts at that time too: amendment 3 of [docs/lp-rate-count.md](lp-rate-count.md), a separate PR, must merge before any count is read. The Air's Claude had read Yu's "We can start now ah😂 no need record 10 days" (2026-11-04) as going on without Gate A; that reading does not hold.
   - Astra reviews this page, the engineering note and the plan, and accepts them.
   - Yu then approves this page as committed, together with the changes to Pilot 1's documents listed below.
   - This consent allows no live order. The code ships switched off: while `p2_lp.IN_FORCE` is False, `p2-run` refuses (`p2_not_in_force`) before any Keychain, journal or git read, and the gateway refuses the Pilot 2 context. `p2-summary` and `init-config --p2` still work. Yu creates and arms config-p2.json only after the go-live consent.
   - Pilot 1 goes first (Yu, 2026-11-04, relayed; confirmed by Yu to lab-Claude, 2026-11-06). No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit. Pilot 2's order path merges only after Pilot 1's PT4 receipt exists. Every Pilot 1 test passes unchanged at every Pilot 2 commit.
2. **Go live.**
   - These must be done first, none with a live order:
     - the code's tests, all passing, including the check that Pilot 1's fold of a journal with Pilot 2's orders and ARM records equals its fold without them;
     - P2-PT3, the fault drills, run only inside the offline test suite, against its fakes, with no network, no Keychain call and no real state root, so they cannot touch Pilot 1's live state;
     - Betfair's answer to the [G1 follow-up](betfair-pilot-2-g1-question.md), or Yu's recorded decision to go ahead without one;
     - Yu's check of Basic 2% commission in My Account and of the £50 website loss limit;
     - Astra's acceptance of the exact code commit and config-p2.json's sha256.
   - Yu then approves going live, naming the code commit and config-p2.json's sha256. The changes to Pilot 1's documents come into force in that commit, which also sets `p2_lp.IN_FORCE` True.
   - Any change to one of those needs this consent again.
3. **Each session.**
   - Yu arms it with the P2 ARM line.
   - The first session is `RACES 1`, with Yu watching both legs on the website through the pull.
   - Yu checks each race with a matched leg on the website: matched sizes, prices, result, per-market commission, and the lay's profit sign and basis. A disagreement with `p2-summary` blocks the next ARM until Astra has looked; that is Yu's procedure, not code.

## How it ends
- Placing stops at the first of these:
  - 10 races, each counted from its first submit;
  - the final date (*proposed*: 2027-06-30), which is Yu's rule, not code;
  - Yu ends the pilot in writing (stopping one session does not end it);
  - something this page forbids happens.
- Stopping early is always allowed.
- There is no verdict and there are no tables. `p2-summary` prints the counts from the journal and, with `--cleared`, Betfair's profit and commission per race and in total. Yu checks them on the website.
- After placing stops, open orders are cancelled or settled as usual. Nothing enters Git without Yu's approval of the exact fields.
- The lines added to Pilot 1's documents at Pilot 2's go-live are removed when Pilot 2 ends, except where Pilot 1 still needs them.

## Changes to Pilot 1's documents at Pilot 2's go-live
These are not applied in this commit. They are applied in the go-live commit that Yu approves, the same commit that sets `p2_lp.IN_FORCE` True. They are listed now so that Astra and Yu review them with this page. Line numbers are at main c86764a.

- **RS-19 ([docs/betfair-pilot-policy.md](betfair-pilot-policy.md)).**
  - What it allows (:6): "No other strategy, agent, script or scheduler places live orders" gains ", except the Pilot 2 bot under docs/betfair-pilot-2-policy.md, in sessions Yu arms and attends". "gain this one exception" becomes "gain these two exceptions".
  - Money (:28) gains: "Pilot 2 adds at most −£100 (10 races × £10); the overall budget is as Yu sets it at Pilot 2's build consent."
  - Who does what (:40): "at most once a day" gains ", counting Pilot 2 sessions (one pilot session a UTC day across both pilots)".
  - Who does what (:44): "No manual or Bet Angel bet is open" becomes "No manual, Bet Angel or Pilot 2 bet is open or unsettled". This is stronger.
  - How it ends (:75): "…at 416702f" gains ", keeping Pilot 2's lines while Pilot 2 runs".
- **AGENTS.md.**
  - :9: "No other strategy, agent, script or scheduler may." becomes "No other strategy, agent, script or scheduler may, except the Pilot 2 bot in docs/betfair-pilot-2-policy.md (`p2-run`, config-p2.json, ref p2-lp), only in sessions Yu armed with the typed P2 ARM line at the lab Mac and attends, and only after Yu's go-live consent there."
  - :10: after "…after Yu's go-live consent there)," insert "or a £2 BACK and £2 LAY pair, LIMIT and LAPSE, that the Pilot 2 bot sends under that page and cancels at off −300 s if unmatched,".
  - Each pilot's lines revert when it ends.
- **[docs/betfair-order-path.md](betfair-order-path.md)** (one clause each, in its Cantonese):
  - :5 and :13: "Pilot 2 例外：bot 嘅單由 Yu 每個 session 喺 `/dev/tty` 打一次 `P2 ARM` 行代替逐張打；只喺 docs/betfair-pilot-2-policy.md 之下、go-live 同意之後。"
  - :26: "`pilot_lp`：只限 live、只經 config-p2 gateway 嘅 `lp_place`；`place()` 用會拒；`api` 照舊只限 paper。"
  - :145: "有 config-p2.json 嘅 profile，sweep 加 `p2-lp`（journal 讀唔到都照掃）。"
  - After :156, three rows: `p2-run` (the `pilot-arm` set at ARM, then A1–A19 for each open as `place`); its pull (the `cancel` set, its own legs only); and `p2-summary` (offline; `--cleared` on the delayed key).
  - :239 gains `P2 ARM <cfg12> <YYYYMMDD> UNTIL <HHMM>Z RACES <N>`. :243 and §15 (:646) gain the same exception.
- **[docs/betfair-pilot-engineering.md](betfair-pilot-engineering.md) §2 and §7.** Pointer lines only, describing behaviour that already exists, with no Pilot 1 code change:
  - the live journal also holds Pilot 2's orders (ref `p2-lp`, source `pilot_lp`) and ARM records;
  - Pilot 1's fold never reads them as its own;
  - a Pilot 2 intent during an armed Pilot 1 session sets a review stop;
  - an open or unsettled Pilot 2 bet halts a Pilot 1 open.

## Where the rest lives
- **The rule's numbers and the money** are on this page, in the Values and Money tables, and in the `p2_lp` constants. config-p2.json's caps are pinned by `config_problems` ([note](betfair-pilot-2-engineering.md) §9). Changing any of them needs a code change, Astra's review and Yu's consents again.
- **How the code keeps these promises** is in the [engineering note](betfair-pilot-2-engineering.md). Astra reviews every change to it. A change that removes or weakens a protection also needs Yu's yes, asked in plain words.
- **Breaking** the engineering note counts as breaking this page.
