# Pilot 2 (P2-LP): a plain capped bot for two-sided quotes

Status: proposed, for Astra's review. Nothing on this page is approved, and no Pilot 2 live order is allowed before Yu's go-live consent here. [RS-19](betfair-pilot-policy.md), Pilot 1's other documents and AGENTS.md are unchanged by this commit. The lines they would gain at Pilot 2's go-live are listed under [Changes to Pilot 1's documents](#changes-to-pilot-1s-documents-at-pilot-2s-go-live) and are not in force. The code ships switched off (`p2_lp.IN_FORCE` False); only the go-live commit, which also applies those lines, sets it True.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, after Yu's go-ahead to draft it (relayed by the Air's Claude, 2026-11-05: "可以 let lab go ahead. 得"). Rewritten on 2026-11-09 as the plain capped bot Yu chose that day ("Plain capped bot (Recommended)" and "−10 min, pulled at −5 min (Recommended)"; [plan](betfair-pilot-2-plan.md), Decided). Revised on 2026-11-10 after lab-Claude's review round of the drafted code, and for Yu's answers that day (plan, Decided). Rebased onto main b9099ff later on 2026-11-10, after #59, #63 and #60 merged that evening, and brought into line with those merges (plan, Decided).

- **Naming.** In [Pilot 1's plan](betfair-pilot-1-plan.md) (Goal and Sample), "Pilot 2" means whatever follows Pilot 1. On this page and its companions, Pilot 2 is this liquidity-provision pilot, P2-LP.
- **Companions.** The [engineering note](betfair-pilot-2-engineering.md) says how the code keeps these promises. The [plan](betfair-pilot-2-plan.md) gives what Yu decided, the reasons for each value, the stages and the timeline. The [G1 follow-up](betfair-pilot-2-g1-question.md) is the question for Yu to send to Betfair. This page wins on any difference.
- **No contract file.** The Money and Values tables below and the constants in `p2_lp.py` are the only copies of the rule's numbers. A test pins them to each other. config-p2.json's caps are pinned in the code; a test states every pin as a literal, and another ties the Money table's £ caps row and the note's §9 table to the committed example config.

## What it allows
One plain capped bot may place small live orders through Pilot 1's existing order gateway. Its command is `p2-run`, its config file `config-p2.json`, its order ref `p2-lp` and its intent source `pilot_lp`. It runs on the lab Mac (the MacBook Pro), only in sessions Yu arms and watches. Yu chose on 2026-11-05 to move all live work there from the Air, so only one Mac trades ([plan](betfair-pilot-2-plan.md), Decided).

In one race it sends one £2 BACK at the best lay − 1 tick, then one £2 LAY at the best back + 1 tick, on one runner whose spread is 3 or 4 ticks. The LAY's price is taken from the BACK's book, and the LAY is sent only while that price is still the best back + 1 tick on the LAY's own fresh book (*proposed*; One race, step 4). Both sit inside the spread, so they wait to be filled. It decides at about off −590 s (10 minutes) and pulls (cancels) whatever is still unmatched at off −300 s (5 minutes). A matched part rides to the result.

It runs in Pilot 1's Betfair profile and live journal, because A15 allows one profile per account. It uses Pilot 1's existing gateway, risk caps, journal, reconcile and kill, with its own config's numbers. It has no contract, records, ledger or stops of its own: its counts come from the journal's order records with ref `p2-lp`, and its only record of its own is one typed ARM record a session. Nothing of Pilot 2 enters Pilot 1's 495 bets, its −£150 pause or its running total.

## Five principles
These are RS-19's five principles. Each says what it means here. Where a detail is unclear, follow the principle and write down the reasoning.

**Transparency.** Every order is journaled before it is sent, with its intent, risk decision and submit, and its outcome after. SKIPs, screens and refused legs are printed in the session with their codes, not journaled. Each session ends with a short printed summary, and `p2-summary` (run with `--mode live`; it reads the journal only) prints the counts at any time. *Why:* the same as RS-19. Yu should be able to see what the code did without having to trust it.

**Truth.** There is no verdict. The results are counts and P&L: the journal's counts, Betfair's cleared profit and commission, and Yu's own check on the website. A disagreement between them blocks the next ARM until Astra has looked; that is Yu's procedure, not code. Anything we cannot verify counts at its worst case in the gateway's day stop. *Why:* with at most 10 races, money can answer nothing, and Yu chose no registered verdict on 2026-11-09.

**Understanding.** It tests operations: whether a plain bot can post two passive £2 orders, rest them, pull them in time and stop safely. It also shows whether and how live fills happen; with the few races expected, those are counts, not rates. No mispricing is known. The research round's judgement, which is not evidence, leans to "we are picked off" ([lp-study](lp-study.md), Predictions). lp-v1 could neither support nor rule that out, and its only released statistics, a move against queued orders after they filled, are partly by construction and say nothing about orders inside the spread, which is where Pilot 2's sit ([lp-study](lp-study.md), Results). *Inference:* the honest expectation is a few pence negative per race, very few races, and an answer about operations only. *Why:* the same as RS-19. Any larger liquidity step needs these operations facts first, and offline history cannot show queue position, virtual prices or latency.

**Simplicity.** One market type and one race at a time. One £2 BACK, then one £2 LAY, both LIMIT and LAPSE, before the off. They rest until a timed whole cancel at off −300 s, and a matched part rides to the result. Nothing is hedged, re-priced, replaced or retried. Every departure from RS-19's simplicity is listed under [What it relaxes](#what-it-relaxes). *Why:* every extra moving part is one more way to lose money by accident.

**Non-forcing.** Skipping is the default. Any doubt means skip, and nothing is ever resent. Day and lifetime numbers are ceilings, never targets. A stop waits for Yu. *Why:* the same as RS-19.

## One race
1. **Screen**, off −840 to −780 s, on the delayed key only. One fresh catalogue row and one delayed book. A market that fails the screen is skipped with no live-key read (F46).
2. **Open**, off −740 s. One gateway open as `place`, with config-p2.json: the lock, the login, the startup reconcile and A1–A19.
3. **BACK**, on the gateway's own fresh send book, observed at off −590 to −575 s. The runner rule and the passive check run on that same book, which the gateway's checks then judge. A BACK goes only with funds and order room for both legs (Money).
4. **LAY**, only if the BACK was placed, on a fresh send book observed by off −532 s. Our own resting BACK counts in that book. The best back on that book must still be one tick below the LAY's price. If it has fallen since the BACK's book, the LAY is not sent (`p2_book_moved`), the BACK is pulled at once, nothing is re-priced, and the race still counts. *Proposed:* built this way in the draft, for Yu at the build consent ([plan](betfair-pilot-2-plan.md), Open for Yu).
5. **Rest** until off −300 s. No Betfair call; local checks every second. One line is printed when both legs rest: the market and the time the pull is due.
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
| Pull | `PULL_S` | -300 | seconds | Yu, 2026-11-09 ("pulled at −5 min") | If the pull fails, about 3 to 5 minutes are left to cancel by hand: a cancel whose outcome is unknown is reconciled for up to 40 s before the next cancel or the handoff |
| Rest check | `SLICE_S` | 1 | seconds | *proposed* | Local checks every second, with no Betfair call |
| Late wake | `LATE_WAKE_S` | 5 | seconds | *proposed* | A check more than 5 s late (a stalled or sleeping Mac) pulls at once |
| Skip streak | `SKIP_STREAK` | 5 | count | *proposed* | 5 opens in a row that end with no order sent (a refused BACK, a failed send-book read, or the decision window missed after the open) end the session. This limits live-key reads that end with no order (F46) |

## Money
Yu chose the first five numbers on 2026-11-03, in a question that offered them for a hand trial. At the build consent Yu confirms them for this bot, together with each row marked *proposed* and each *proposed reading*. Only Yu changes a money number.

| Value | Constant | Number | Unit | Whose | What it means |
|---|---|---|---|---|---|
| Stake, each leg | `STAKE_P` | 200 | pence | Yu, 2026-11-03, to confirm | £2 a side: one £2 BACK and one £2 LAY on one runner. A BACK can lose £2. A LAY at price L can lose £2 × (L − 1) |
| Highest lay price | `MAX_LAY_H` | 600 | hundredths | Yu, 2026-11-03, to confirm | 6.0. One LAY can lose at most £10 |
| Races a day | `RACES_DAY` | 5 | count | Yu, 2026-11-03, to confirm | At most 5 a UTC day. A ceiling, never a target. *Proposed reading:* a race counts from its first submit; a market screened, skipped or refused before any submit does not count |
| Day stop | — | 2000 | pence | Yu, 2026-11-03, to confirm | −£20, as the gateway's R10 at £20 in config-p2.json. R10 counts settled losses, matched bets not yet settled and open orders. settle-market books each bet at its worst case. *Proposed reading:* a hard ceiling. A BACK goes only if the race's full £10 still fits, so the worst possible day is −£20. The other reading, "stop once −£20 is reached", would allow a −£30 day. With one session a UTC day that ends after any race with a matched leg, at most one Pilot 2 race a day can lose, so the worst real day is −£10. −£20 is the gateway's ceiling; it is reached only through a settle typed that day, or if Yu later allows several sessions a day (plan, Open for Yu) |
| Pilot stop | — | 10000 | pence | Yu, 2026-11-03, to confirm | −£100. It holds by count: 10 races × £10 |
| Worst race | `PAIR_WORST_P` | 1000 | pence | follows from Yu's numbers | −£10: the LAY fully matched and its runner wins. The BACK loses only when its runner loses, so the two never add up |
| Races in total | `RACES_TOTAL` | 10 | count | *proposed* | 10 × £10 = £100, so −£100 holds by count, as Pilot 1's 495 × £2 does |
| Open at once | — | 1000 | pence | *proposed* | One race at a time, and nothing else of ours open or unsettled when a BACK goes, so at most £10 of worst case is ever open. The session ends after any race with a matched leg |
| Pause | — | none | — | *proposed* | The hard stops and the 10-race cap replace a look-and-go-on pause. Yu may add one |
| £ caps in config-p2.json | — | — | — | *proposed*, derived from the rows above | Stake £2, liability £10, market £10, total £20, daily loss £20, outside exposure £0: the gateway's own copies of these limits ([note](betfair-pilot-2-engineering.md) §9). A total of £10 instead is open for Yu (plan, Open for Yu). Pilot 1's config is unchanged |
| Betfair website loss limit | — | 5000 | pence | Yu, 2026-10-21 | £50 a day, unchanged. A backstop outside the code |
| Overall budget | — | open | — | Yu decides | Pilot 1's worst case of −£990 (RS-19, Money), plus Pilot 2's −£100 and the November manual drills' −£10, is −£1,100, which is over Yu's £1,000 |

- **Funds and order room for both legs.** *Proposed.* A BACK goes only if the account's available funds, less config-p2's funds reserve (£0 in the example), cover £2 plus the LAY's whole liability (£3.02 with the LAY at 1.51, £12.00 at 6.0), and the order caps for a minute, an hour and a day each have room for two orders. Otherwise the race is skipped before any order (`p2_pair_precheck:funds`, or `:rate_minute`, `:rate_hour` or `:rate_day`). It does not count as a race, and it counts toward the skip streak.
- **The budget.** At the build consent Yu decides between two options:
  - raise the overall budget;
  - keep £1,000, with the account balance and no top-ups as the joint stop, so that whichever pilot runs later may be starved of funds (each BACK is then skipped for funds, above).

  Yu has said a higher maximum loss is acceptable ("可以上調max loss", relayed by the Air's Claude, 2026-11-04). The Air's Claude proposed £1,100, which now covers −£1,100 exactly. Neither is Yu's written yes. The November manual drills are described in the [plan](betfair-pilot-2-plan.md), Before Pilot 2.
- **Separate from Pilot 1.** Pilot 2 has no ledger. Pilot 1's slots, pause and running total are untouched, and Pilot 2's orders never count in them. The option Yu chose on 2026-11-03 said so: "NOT part of Pilot 1's counts".
- **Losses pay no commission.** Commission is Basic 2% on each market's net, checked per market and never per bet, because both orders of one race are in one market.

## Arming a session
- Yu types `p2-run --mode live --confirm-live live-<cfg12>-<YYYYMMDD> --until HHMM --races N [--bet-angel-paused]` at the lab Mac's own keyboard. The flag is needed while config-p2.json's Bet Angel coexistence is unverified (A12). The token is keyed by config-p2's hash, so arming one pilot never arms the other.
  - That hash is the one `init-config --p2` prints, run from a checkout at the commit the go-live consent names. It is the suite's hash of the validated config merged over that checkout's defaults, not `shasum` of the file.
  - Yu's other typed commands (`settle-market`, `reconcile`, `resume`, `recording-release`) load config.json and take its token, `live-<first 12 of config.json's hash>-<YYYYMMDD>`, whose inputs `status` prints.
- **Refused before any line,** with nothing journaled:
  - not live, or not the default profile; config-p2.json missing (`config_missing`) or not exactly as the note §9 pins it;
  - `SSH_CONNECTION`, `SSH_CLIENT` or `SSH_TTY` set (friction only);
  - config-p2's static arming for `place` fails (A1–A13: armed, the acknowledgements, the egress attestation, the certificate files, the Keychain items present; A7 also refuses a journal whose last event is ahead of the wall clock). No secret is read;
  - the checkout's commit is unknown or its tree is dirty;
  - the KILL file, a HALT, an open order, an unresolved market, a login-ledger problem, or Pilot 1's armed-session file;
  - a Pilot 1 session today or its window open, or an earlier P2 ARM record today;
  - any bet of ours open or unsettled;
  - today's booked loss leaves no room for one race's £10 (`p2_pair_precheck:daily_loss`);
  - the static arming or the day's-room check raises instead of answering (`p2_arm_check_failed:static` or `:daily_loss`). After the line this refusal also releases the recording admission;
  - 10 races in total; N outside 1 to the smaller of 5 and the races left; N other than 1 until one race has had both legs placed;
  - UNTIL less than 20 minutes ahead, or after 23:59Z;
  - the login cap.
- **The banner** shows config-p2's hash (cfg12), the commit, today's and total races, N, and the stops in this order: at the lab Mac, Ctrl-C or the KILL file (the bot cancels its own legs); away from it, the Betfair phone app (the bot's pull then ends in a handoff, and each leg needs ACCEPT ABSENT or settle-market); then the own-scope `kill` from Pilot 2's checkout (the bot then cancels nothing itself and prints the handoff). It warns that a typed `cancel` fails while legs rest, that the session ends after any matched leg, and that nothing heavy runs on the lab Mac while armed or an order rests. It asks for mains power with the lid open.
- **Then Yu types at /dev/tty:** `P2 ARM <cfg12> <YYYYMMDD> UNTIL <HHMM>Z RACES <N>`.
- **After the line:** the live-owner lock and recording admission, the checks again (and the commit must still be the one the banner showed), the credentials, caffeinate, and then the ARM record in the journal, appended under `.gateway.lock`, with the anchor published. A refused ARM writes no record.
- **What it binds.** Every leg's gateway check needs that exact ARM record: this config's hash, this session, today, before UNTIL, and within its race count. Only `p2-run` builds a gateway that can send for Pilot 2, and only after the line. This is friction, not cryptography; the rule that agents never arm is this page's.
- **Later lines are the existing ones:** `SETTLE MARKET`, `ACCEPT ABSENT`, `RESUME LIVE` and `RELEASE RECORDING GENERATION <N>`.
- **The first session, in order** (*proposed*):
  1. From Pilot 2's checkout, `init-config --p2` prints config-p2's hash (status `config_exists`). cfg12 is its first 12 characters.
  2. Yu types the `p2-run` line above, with `--races 1`.
  3. Before the P2 ARM line, Yu compares the banner's cfg12 and commit with the hash and commit the go-live consent names. If either differs, Ctrl-C.
  4. During the session, the stops in the banner's order.
  5. After a matched leg, as soon as its market shows CLOSED, on the race's own UTC day: from Pilot 2's checkout, `settle-market --market <market> --confirm-live <config.json's token>`, with `--bet-angel-paused` while config.json's coexistence is unverified, and with config.json armed (A1). A market Betfair no longer returns cannot be settled in v1, and an unsettled leg refuses every later ARM ([order-path](betfair-order-path.md) §8).
  6. `p2-summary --mode live`, then Yu's website check.

## How each fault ends
The full state machine is in the [note](betfair-pilot-2-engineering.md) §8. In short:
- **Before the BACK:** a refusal or an error moves to the next market or ends the session. Nothing has been sent. If the day's market list cannot be read (`markets_read_failed`) or lists no market, the session ends: nothing was sent, and no second ARM is allowed that UTC day (whether a failed first read should use the day is open for Yu; plan, Open for Yu).
- **The BACK refused or failed:** the next market. 5 opens in a row that end with no order sent (a refused BACK, a failed send-book read, or the decision window missed after the open) end the session. Each skip's code is printed with its market.
- **A stop during a leg's send** (Ctrl-C, Ctrl-\ or Ctrl-Z while the leg's funds and book are read): the leg stops before its intent is journaled, so nothing of it is sent. A BACK stopped this way is no race, and the session ends; after a LAY stopped this way the BACK is pulled and the session ends. A stop in the milliseconds after the leg's last check reaches its first byte instead (`p2_stop_before_send`): nothing is sent, but the leg is journaled as FAILED, and a BACK refused there counts as a race.
- **The BACK's outcome unknown:** the gateway reconciles it in line. If it is not found, the handoff, and the session ends.
- **A stop between the legs** (Ctrl-C, or Ctrl-\ or Ctrl-Z, which act the same; the KILL file; another process's journal event): pull the BACK, end. A kill event or an unreadable journal: the handoff, end. The bot never races the kill.
- **The LAY refused or failed:** pull the BACK at once. That includes a best back that has fallen since the BACK (`p2_book_moved`). A LAY that raises with nothing of it sent: pull the BACK; the session ends (`lay_raised:<code>`) unless only its book read failed. The LAY's code is printed with its market and counted as `lay:<code>`. The LAY's outcome unknown: reconcile; if not found, pull the BACK, hand off the LAY, end.
- **During the rest:** a stop, the KILL file, another process's event, a clock jump or a late wake pulls at once and ends the session. A kill event or an unreadable journal: the handoff for both legs, end. After a sleep, the pull's first cancel, the LAY's, can go down a connection that died in the sleep: expect the handoff, with the LAY possibly still open, and cancel it by hand.
- **A runner withdrawn while the legs rest** (a late non-runner, our own runner removed, or the race abandoned). *Unverified:* Betfair's rule and how Betfair then lists such a bet (no facts-register row yet). By the lab's reading of Betfair's Exchange Rules, Betfair cancels unmatched lays when a non-runner's reduction factor is 2.5% or more, and cancels both legs when our runner is removed or the race abandoned. The bot is not told, because the rest makes no Betfair call. At the pull, the cancel of a leg Betfair already cancelled fails, and that leg is handed off although nothing of it is open. The BACK is still pulled, on time or 15 s later. The session ends with exit 5 and no HALT. Yu checks the website first. The way out, typed by Yu at the lab Mac:
  - a leg Betfair no longer lists: `reconcile --mode live --accept <ref> --confirm-live <token>` and the line ACCEPT ABSENT;
  - a leg kept at its worst case: `settle-market` after the result (the line SETTLE MARKET), which books that leg's worst case, up to £10, with nothing matched;
  - then `recording-release` and the line RELEASE RECORDING GENERATION N.

  Until then neither pilot places (What it relaxes, item 14).
- **At the pull:** nothing matched, the next market. A leg matched, the session ends with the settle line. A failed or unknown cancel, a suspended market or a leg still part unmatched: the handoff, end. A suspended market may also add two UNKNOWNs toward the three-UNKNOWN HALT, or HALT at once if Betfair's cancel report carries MARKET_SUSPENDED on each instruction; which shape Betfair sends is unverified. LAPSE acts at the turn in play.
- **An exception after a send:** one best-effort pull (none if the pull itself raised), then the handoff for each leg not shown complete, and the lock is released.
- **The process gone** (SIGKILL, SIGSTOP, power loss, a sleep through the off): nothing runs. The race is bounded at £10 and LAPSE. The next ARM refuses until every leg is resolved.
- **The handoff** names each leg (bet id or ref, side, £2 at its price, runner, market), then the stops in order: the Betfair phone app, the website, and the own-scope `kill` run from Pilot 2's checkout, by its absolute path. Its last line: if a cancel is refused because the market is suspended, cancel again as soon as it reopens; anything unmatched lapses at the off. With the pull at −300 s, about 3 to 5 minutes are left before the off, because each cancel whose outcome is unknown is reconciled for up to 40 s before the next cancel or the handoff.

## Who does what
- Only Yu starts a session, in person at the lab Mac's own keyboard, never over SSH, and stays for the whole session. There is at most one pilot session a UTC day across both pilots.
  - Pilot 2's code refuses to arm over SSH (friction only), while Pilot 1's armed-session file exists, on a day with a Pilot 1 session or an earlier Pilot 2 session, and refuses to send while a Pilot 1 session's window is open.
  - Pilot 1's code is unchanged and does not know about Pilot 2's days. So not starting Pilot 1 after a Pilot 2 session on the same day is Yu's rule on Pilot 1's side.
- Pilot 2 arms only when no Pilot 1 bet, manual bet or Bet Angel bet of ours is open or unsettled, and each BACK checks this again. The lab Mac is on mains power with the lid open, and the Betfair phone app is logged in as the stop away from the lab Mac. Nothing heavy (backtests, model studies, large downloads) runs on the lab Mac while a session is armed or an order rests (Yu's lab rule, 2026-11-05).
- Anyone at the lab Mac can stop it with Ctrl-C (Ctrl-\ and Ctrl-Z act the same), the KILL file or the existing own-scope `kill`. Run from Pilot 2's checkout, the kill also sweeps `p2-lp` once config-p2.json exists; run from Pilot 1's checkout, it finds Pilot 2's legs only through a readable journal (What it relaxes, item 12). The Betfair website and phone app stop it from anywhere.
  - **Lab-Claude's stop while `p2-run` runs** (Yu, 2026-11-10; [plan](betfair-pilot-2-plan.md), Decided): lab-Claude first only creates the KILL file, because the bot then cancels its own two orders within a second, with no Keychain read. It runs the own-scope kill only if the bot is gone. A kill journals its kill event before its Keychain reads and login, and from then on the bot cancels nothing (What it relaxes, item 17).
    - *Proposed* reading of "gone" (for Yu at the build consent; plan, Open for Yu): no `p2-run` process exists; or one exists but has not ended within about two minutes of the KILL file; or `ps` shows it stopped (SIGSTOP). A bot that sees the file sends its cancels within a second and then ends, though a cancel whose outcome is unknown is reconciled for up to 40 s. A stopped or hung process cannot pull its legs.
  - **With no bot running,** the 2026-11-06 rule stands: lab-Claude may run an own-scope kill whenever something looks wrong, without asking first, and tells Yu straight after (Yu: "Yes, without asking"; plan, Decided). Whether that rule covers the kill run from Pilot 2's checkout is open for Yu (plan, Open for Yu). It never runs the account-wide kill, deletes the KILL file or resumes. Any other agent may only create the KILL file.
  - **The kill's Keychain reads.** At the T9 probe on 2026-11-10 macOS asked once for the Mac password, and Yu clicked Always Allow and chose to keep it (plan, Decided). So `/usr/bin/security` reads the live key with no dialog, and lab-Claude's own-scope kill is expected to run with nobody at the screen. *Unverified:* not yet tested.
  - From the Air over SSH, creating the lab Mac's KILL file stops new sends but itself cancels nothing; a running bot pulls its own resting legs when it sees the file ([note](betfair-pilot-2-engineering.md) §8). A kill run on the Air engages only the Air's KILL file, so the lab Mac can keep sending.
  - There is no auto-kill. The own-scope kill cancels every open order of ours, Pilot 1's and manual ones included, as Pilot 1's kill does.
  - The gateway lock is held for about 7.5 minutes a race, from the open to the pull. Meanwhile a typed `cancel` waits for the lock and fails. The banner names the stops that work.
  - Yu settles after the session: `settle-market` for each market with a matched leg, as soon as the market shows CLOSED with its result, and on the race's own UTC day if Yu can. The booking falls on the UTC day the settle is typed, and more than £10 booked on a later day refuses that day's P2 ARM (`p2_pair_precheck:daily_loss`). The next ARM refuses until each is settled.
- Every line the code asks a person to type belongs to Yu, as on RS-19: the P2 ARM line, settle a market, accept an absent order, resume, release a recording generation and an account-wide kill. Yu types each one at the lab Mac's own keyboard, never over SSH (Yu's lab rule, 2026-11-05). Agents and scripts never type one and never supply a terminal for one.
- Agents build, test, review and summarise. The lab may write Pilot 2's pages and code as PRs, Astra reviews and Yu merges. Yu chose "Yes, PRs only" on 2026-11-03. On 2026-11-10 Yu confirmed that Astra stays the outside reviewer at the consents; only GitHub's Codex reviews ended (plan, Decided). Agents relay only the counts and codes `p2-summary` prints, not the matched sizes and prices it also prints for Yu's website check.
- Yu's lab rules of 2026-11-05 also bind: agents never arm; lab-Claude runs only the commands the repo's documents give to an agent, each time with Yu's OK, except the own-scope kill above (Yu, 2026-11-06); other agents on the lab Mac, and the Air's Claude over SSH, run no live command but may create the KILL file; the Air runs no live command; no agent reads the state root, recordings, receipts or certificate directory; and nobody runs `git init` or clones a repo into ~, ~/Library or ~/.config, which would make every live command refuse, while the kill would still engage the KILL file but cancel nothing (arming.py:62, :278; config.py:246, :294-295; cli.py:2081, at main b9099ff).
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
8. **No registration.** No frozen contract, registration receipt, PA1–PA4 or code-hash digest. The banner shows the commit and a clean tree is required. Yu's go-live consent names the merged go-live commit and config-p2's hash as `init-config --p2` prints it.
9. **No dry day and no probe sessions.** The fault drills run only in the offline test suite. Sessions are `RACES 1` until one race has had both legs placed, with Yu watching both legs on the website.

Pilot 1 protections it weakens:

10. RS-19's "No other strategy, agent, script or scheduler places live orders" and AGENTS.md's one-contract exception stop holding. There is a second automated order source on the account.
11. Pilot 1's journal holds Pilot 2's orders and ARM records. Pilot 2 adds no record type: its orders are core intents with source `pilot_lp` and ref `p2-lp`, and its ARM record is the core kind `operator_confirm`, which every fold ignores. So Pilot 1's fail-closed "another contract in the journal" check does not fire, and no Pilot 1 code changes.
12. A kill from Pilot 1's checkout finds Pilot 2's legs only through a readable journal. Pilot 2's handoff therefore names the phone app and the website first, then the own-scope `kill` run from Pilot 2's checkout, by its absolute path, which sweeps `p2-lp` even when the journal cannot be read. It keeps the warning that a kill from Pilot 1's checkout misses Pilot 2's legs when the journal is unreadable.
13. RS-19's "The worst possible total is −£990, inside Yu's £1,000 trading budget" stops holding unless Yu raises the budget.
14. **Pilot 2's faults can stop Pilot 1.** There is no auto-kill and no in-session halt. But a Pilot 2 UNKNOWN counts toward the three-UNKNOWN HALT, which stops Pilot 1 too until Yu types RESUME LIVE. That includes the pull's cancel of a leg that matched, or part matched, during the rest, so a round trip's pull counts twice. It leaves the count at 2. Until the next clean order report resets that count, the next unknown outcome of any order, Pilot 1's or a typed one, HALTs at once; settle-market and RESUME LIVE do not reset it. A suspended market at the pull may count twice too, or HALT at once (note §8). A Pilot 2 order with an unknown outcome blocks Pilot 1's placing too (R3), and so does a leg Betfair itself cancelled during the rest, until Yu accepts or settles it. A Pilot 2 settle typed on a Pilot 1 day uses that day's room for Pilot 1. These are the safe direction, but they cost sessions.
15. Pilot 2's live-key reads that end with no order add read-only-looking traffic on the £499 key both pilots need (F46).
16. **Pilot 1's code identity.** Pilot 1 runs from its own registered checkout. But once Pilot 2's order path is on main, any later Pilot 1 fix taken from main carries Pilot 2's code too, and needs Astra's re-acceptance and Yu's go-live consent over it.

Against Pilot 1's executor:

17. **No pull after a kill event.** Once a kill's event is in the journal, the bot cancels nothing of its own: it prints the handoff and ends, even when that kill then ends incomplete (for example a credential or login failure before its sweep). Pilot 1's executor waits up to 30 s and sends its own cancel when the kill ends incomplete (eng §8). So for a running session the first stop is the KILL file alone, which the bot pulls on (Who does what). Not built: on a kill event, wait up to 30 s and pull if that kill ends incomplete (plan, Open for Yu).

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
   - Yu confirms the Money table: Yu's 2026-11-03 numbers for this bot, each *proposed* row (the £ caps included), the funds line, the two *proposed readings* (how a race counts, and the day stop's reading) and the overall budget. There is no contract to freeze: the numbers are on this page and in the `p2_lp` constants, and a test pins them to each other.
   - Yu gives a plain-words yes to each item under What it relaxes and each cut above.
   - Gate A: on 2026-11-06 Yu chose to wait for it ("Count first (Recommended)"; [plan](betfair-pilot-2-plan.md), Decided). Pilot 2's code merges only after Gate A, the free Stage 0 count, passes. On 2026-11-08 Yu chose to have the code drafted before then, as PRs nobody merges ("Draft now, gates unchanged (Recommended)"; plan, Decided). Drafting moves no gate. Because the bot now decides at off −590 s, Gate A counts at that time too: amendment 3 of [docs/lp-rate-count.md](lp-rate-count.md) merged with amendment 4 in PR #60 on 2026-11-10, before any count was read. The Air's Claude had read Yu's "We can start now ah😂 no need record 10 days" (2026-11-04) as going on without Gate A; that reading does not hold. On 2026-11-10 Yu said yes to the lab's "keep both for now": Gate A and Pilot 1 first stay, the manual £1 drills give live experience first, and the count runs in the background (plan, Decided).
   - Astra reviews this page, the engineering note and the plan, and accepts them. On 2026-11-10 Yu confirmed that Astra stays the outside reviewer at the consents; where Astra runs is open (plan, Open for Yu).
   - Yu then approves this page as committed, together with the changes to Pilot 1's documents listed below.
   - This consent allows no live order. The code ships switched off: while `p2_lp.IN_FORCE` is False, `p2-run` refuses before any Keychain, journal or git read, with `config_missing` while config-p2.json is absent and `p2_not_in_force` once it exists, and the gateway refuses the Pilot 2 context. `p2-summary --mode live` (once config-p2.json exists) and `init-config --p2` still work.
   - Pilot 1 goes first (Yu, 2026-11-04, relayed; confirmed by Yu to lab-Claude, 2026-11-06; kept on 2026-11-10). No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit. Pilot 2's order path merges only after Pilot 1's PT4 receipt exists. Every Pilot 1 test passes unchanged at every Pilot 2 commit.
2. **Go live.** In this order. Steps 2, 4 and 7 follow the order Yu chose on 2026-11-10 (plan, Decided); the rest are the lab's:
   1. **The go-live PR.** Lab-Claude drafts it against main: `IN_FORCE` True, the lines listed under Changes to Pilot 1's documents applied by their quoted text, the status lines of this page and the note, and the switched-off tests changed. Lab-Claude's review rounds and Astra review it, and the full offline suite passes at its head. When it may be drafted is open (plan, Open for Yu).
   2. **Yu merges it.** main then holds the go-live commit. The merge is not the consent and allows no order. From the merge to the consent, only Yu's typed lines can start a session, and Yu types none before the consent; the AGENTS.md line the PR adds says "only after Yu's go-live consent there".
   3. **The checkout.** Pilot 2's checkout on the lab Mac is put at exactly that merged commit, with a clean tree, and nobody pulls it afterwards.
   4. **The config.** From that checkout Yu creates config-p2.json with `init-config --p2` and makes every hand edit (note §9), armed included, before Astra's acceptance, because the hash the consent names covers those fields. `init-config --p2`, run again, prints that hash. Whether Yu then sets armed back to false until the consent is open (plan, Open for Yu).
   5. **The other prerequisites,** none with a live order:
      - the code's tests, all passing, including the check that Pilot 1's fold of a journal with Pilot 2's orders and ARM records equals its fold without them;
      - P2-PT3, the fault drills, run only inside the offline test suite, against its fakes, with no network, no Keychain call and no real state root, so they cannot touch Pilot 1's live state;
      - Betfair's answer to the [G1 follow-up](betfair-pilot-2-g1-question.md), or Yu's recorded decision to go ahead without one;
      - Yu's check of Basic 2% commission in My Account and of the £50 website loss limit, and (*proposed*, the lab's) Yu's check in the Betfair account's settings that Best Price Execution is on and MatchMe is off.
   6. **Astra accepts** the merged commit and config-p2's hash.
   7. **Yu approves going live,** naming that commit and that hash in full.
   - The changes to Pilot 1's documents are applied in the go-live commit, which also sets `p2_lp.IN_FORCE` True. Any change to the commit or to config-p2's hash needs Astra's acceptance and this consent again.
3. **Each session.**
   - Yu arms it with the P2 ARM line, after comparing the banner's cfg12 and commit with the ones the go-live consent names.
   - Sessions are `RACES 1` until one race has had both legs placed, with Yu watching both legs on the website through the pull.
   - Yu checks each race with a matched leg on the website: matched sizes, prices, result, per-market commission, and the lay's profit sign and basis. A disagreement with `p2-summary` blocks the next ARM until Astra has looked; that is Yu's procedure, not code.

## How it ends
- Placing stops at the first of these:
  - 10 races, each counted from its first submit;
  - the final date (*proposed*: 2027-06-30), which is Yu's rule, not code;
  - Yu ends the pilot in writing (stopping one session does not end it);
  - something this page forbids happens.
- Stopping early is always allowed.
- There is no verdict and there are no tables. `p2-summary --mode live` prints the counts from the journal, each matched leg's size and average price for Yu's website check, and, with `--cleared`, Betfair's profit and commission per race and in total. Betfair's cleared history is about 90 days (F35), so Yu runs `--cleared` once each race has settled and keeps that figure (note §10). Yu checks them on the website.
- After placing stops, open orders are cancelled or settled as usual. Nothing enters Git without Yu's approval of the exact fields.
- The lines added to Pilot 1's documents at Pilot 2's go-live are removed when Pilot 2 ends, except where Pilot 1 still needs them.

## Changes to Pilot 1's documents at Pilot 2's go-live
These are not applied in this commit. They are applied in the go-live commit, which Yu merges before the go-live consent names it and which also sets `p2_lp.IN_FORCE` True. They are listed now so that Astra and Yu review them with this page. Each change is found by its quoted text; the line numbers were main c86764a's. At main b9099ff, after PR #59 merged on 2026-11-10, RS-19's :28, :40, :44 and :75 are :30, :42, :48 and :79 and order-path's :519 and :646 are :543 and :670; the others are unchanged.

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
  - After :156, three rows: `p2-run` (config-p2's `place` static set, A1–A13, at ARM, then A1–A19 for each open as `place`); its pull (the `cancel` set, its own legs only); and `p2-summary` (offline; `--cleared` on the delayed key).
  - :239 gains `P2 ARM <cfg12> <YYYYMMDD> UNTIL <HHMM>Z RACES <N>`. :243 and §15 (:646) gain the same exception.
  - §9 (:519), after "人手 `touch KILL` 只會停新單，唔會取消任何 bet": "（例外：行緊嘅 `p2-run` 見到 KILL file，會喺下一秒自己取消佢自己仲掛住嘅兩張單。）"
- **[docs/betfair-pilot-engineering.md](betfair-pilot-engineering.md) §2 and §7.** Pointer lines only, describing behaviour that already exists, with no Pilot 1 code change:
  - the live journal also holds Pilot 2's orders (ref `p2-lp`, source `pilot_lp`) and ARM records;
  - Pilot 1's fold never reads them as its own;
  - a Pilot 2 intent during an armed Pilot 1 session sets a review stop;
  - an open or unsettled Pilot 2 bet halts a Pilot 1 open.

## Where the rest lives
- **The rule's numbers and the money** are on this page, in the Values and Money tables, and in the `p2_lp` constants. config-p2.json's caps are pinned by `config_problems` ([note](betfair-pilot-2-engineering.md) §9); its tests state every pin as a literal, and the docs test ties the £ caps row and the note's §9 table to the committed example config. Changing any of them needs a code change, Astra's review and Yu's consents again.
- **How the code keeps these promises** is in the [engineering note](betfair-pilot-2-engineering.md). Astra reviews every change to it. A change that removes or weakens a protection also needs Yu's yes, asked in plain words.
- **Breaking** the engineering note counts as breaking this page.
