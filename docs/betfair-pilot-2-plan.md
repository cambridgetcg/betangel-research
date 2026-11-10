# Pilot 2 plan: P2-LP, a plain capped bot for two-sided quotes

Status: proposed, together with the [Pilot 2 page](betfair-pilot-2-policy.md) and its [engineering note](betfair-pilot-2-engineering.md). Nothing is approved. The current rules stay in force until Yu's go-live consent on the Pilot 2 page.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, from the lab's Pilot 2 design of 2026-11-03. Rewritten on 2026-11-09 for the plain capped bot Yu chose that day (Decided). Revised on 2026-11-10 after lab-Claude's review round of the drafted code, for Yu's answers that day and the lab Mac's first live tests (Decided).

- "The page" means [docs/betfair-pilot-2-policy.md](betfair-pilot-2-policy.md). "The note" means [docs/betfair-pilot-2-engineering.md](betfair-pilot-2-engineering.md). There is no contract file: the draft contract research/pilots/p2-lp.json was retired on 2026-11-09, and the page's tables and the code's `p2_lp` constants are the only copies of the rule's numbers.
- The page holds the policy and the money. The note holds the engineering detail, and the page wins on any difference. This plan keeps only its own content: the goal, what has been decided, the reason for each value, the expected rate, the stages and the timeline.
- *Estimate* and *Inference* mark numbers that have not been measured.
- **Yu's words** are quoted with their date and where they were said. "To lab-Claude" means Yu's own answer in the lab's session. "Relayed" means Yu said it in chat with the Air's Claude, which passed it on through the Air–lab mailbox. By merging this plan, Yu confirms only that the quoted words are accurate, or asks for a correction. Merging decides nothing that is still open, approves no page and accepts no proposal.

## Goal
- Find out, with real money at micro stakes, whether a plain capped bot can do all of this:
  - post two passive £2 orders on one runner, about ten minutes before the off;
  - rest them about five minutes;
  - pull them five minutes before the off;
  - stop in an orderly way under every fault, never passing Yu's −£20 day and −£100 pilot limits.
- See whether and how live fills happen: each leg filling, both filling (a round trip), and a leg matched at once (a taker fill). `p2-summary` counts the last as a leg matched in its own place report ("in the place report BACK b, LAY l"); no fill time is recorded. With the few races expected, these are counts, not rates.
- Report counts and P&L: the journal's counts and Betfair's cleared profit after commission, checked by Yu on the website. There is no verdict, and money never decides.
- **Honest prior.**
  - No mispricing is known.
  - The research round's judgement, which is not evidence, leans to "we are picked off" ([lp-study](lp-study.md), Predictions). lp-v1 could neither support nor rule that out.
  - Its only released statistics are descriptive. In lp-v1's positive control at off −10 min, the mid moved against queued orders by 0.54 ticks at +10 s and 1.08 ticks by BSP. That is partly by construction, drift is not excluded, and it says nothing about orders one tick inside the spread ([lp-study](lp-study.md), Results).
  - Pilot 2's legs improve the price rather than join it. Whether that is less adverse is unmeasured.
  - *Inference:* per race, expect a few pence negative. A one-tick round trip, BACK 5.1 over LAY 5.0, is worth about +4p at BSP 5.0. A lone LAY at 5.0 that BSP moves one tick against is worth about −7p. Realised money is luck, dominated by any lone LAY whose runner wins: about −£8 at 5.0 and −£10 at 6.0.
  - Races will be rare (Expected rate, below). The answer will be about operations. It is never a claim of an edge.
- **Why it is worth testing.**
  - Offline history cannot answer it. lp-v1's registered pass was underpowered at every decision time. Its fill model has no queue, no latency and no virtual prices, so its modelled fills of 89–98% per main leg are optimistic ([lp-study](lp-study.md), Results and What it cannot show).
  - *Inference:* lp-v1's legs also rested 590 s, to off −10 s. Pilot 2's rest about 230–290 s and stop at off −300 s. So lp-v1's 77 round trips to 8 lone legs at −10 min is not a forecast: expect fewer fills and a larger share of lone legs.
  - Only live fills can test that optimism, and a handful of races will show only whether it is badly wrong.
  - A £2 order moves nothing, can wait and can skip freely. Any larger liquidity step needs these operations facts first.

## Decided
- **Yu, 2026-11-01:** asked whether providing liquidity in thin, wide-spread markets can pay. That started lp-v1 ([lp-study](lp-study.md)).
- **Yu, 2026-11-03, to lab-Claude,** after lp-v1:
  - **"Automated pilot only":** the first step is an automated pilot, with no hand trial first. The attended manual drills below were added on 2026-11-04.
  - **Money:** chosen from an option offered for a hand trial, and carried over to this pilot:
    - £2 each side;
    - lays only up to 6.0, so the worst race is −£10;
    - at most 5 races a day;
    - a day stop at −£20 and a pilot stop at −£100;
    - inside the £1,000 overall budget, and "NOT part of Pilot 1's counts".

    Yu confirms these for the automated pilot at the build consent.
  - **"Let it run":** a lone matched leg rides to the result after the unmatched side is cancelled before the off, and is scored against BSP.
  - **"Yes, PRs only":** the lab may draft the page, the plan and the executor code as PRs. Astra reviews, Yu merges, and nothing goes live without Yu's three consents.
- **Yu, 2026-11-03, to lab-Claude: "Count first".** Before any Pilot 2 code, count for free how often today's markets show the spreads Pilot 2 would quote in. Build the pilot only if at least 1.4% of usable markets qualify over 10 or more racing days. This is registered in [docs/lp-rate-count.md](lp-rate-count.md), with Yu's choices on which run decides, and was amended once on 2026-11-04 on Yu's choice "Only new recordings".
- **Yu, 2026-11-04, relayed, said but not decided:** "We can start now ah😂 no need record 10 days. And we can start small."
  - The Air's Claude read this as a decision to go on without waiting for Gate A, and asked Yu to confirm that reading.
  - On 2026-11-06 Yu chose to wait for Gate A (below), so that reading does not hold.
- **Yu, 2026-11-04, relayed: "2.是",** keeping Pilot 1 first. PR4 waits for Astra's acceptance of Pilot 1's go-live commit, and PR5 waits for Pilot 1's PT4. Yu confirmed this to lab-Claude on 2026-11-06 (below).
- **Yu, 2026-11-05, relayed: "可以 let lab go ahead. 得".** This is the go-ahead for the lab to draft this PR (stage PR1: documents and one small test of the draft contract). It is not the build consent. The Air's Claude's proposals for the open questions remain proposals (Open for Yu, below).
- **Yu, 2026-11-05, to lab-Claude: "Lets run live trading here actually".** Yu typed this on the lab Mac (the MacBook Pro). In a multiple-choice prompt the same day, Yu chose:
  - **"All of it":** the credentials, Pilot 1, Pilot 2, the manual £1 drills and the delayed-key price recorder all move from the Air to the lab Mac. The Air stops live work, so only one Mac trades.
  - **"This account (user)":** the credentials live in the lab Mac's existing macOS account `user`, not in a separate trading user. Yu was told that every agent on the lab Mac, and the Air's Claude over SSH, could then reach them.
  - **"Move it here too",** for the recorder and Gate A: the Gate A count starts again from the lab Mac's recordings, and a written amendment names the lab Mac before any count is read (Stage 0 and Gate A, below). Recordings are never copied between Macs.
  - **"A note: means this Mac",** for contract p1: where it says the analysis and Yu's private risk view are "on the Air" (p1-fav-t5.json:188, :190), Yu decided this means the Mac Pilot 1 trades from, now the lab Mac. Contract p1's bytes and sha256 (4dbccb5a…) stay unchanged.
  - **Backup:** Yu sets up an encrypted local backup of the lab Mac, covering the state root and the Keychain, before the first order. On 2026-11-10 Yu put the one-pound probe ahead of it. The lab suggested that it still comes before the manual drills, and Yu did not change that (below).

  Yu also approved rules for the lab Mac, kept in the lab's own instructions, not in the repo. Those that bear on Pilot 2 are on the page (Who does what).
  - What follows for Pilot 2 is on the page (What it allows, Who does what, How it ends), in the note (§2, §8, §15) and below (Before Pilot 2, Stage 0 and Gate A, Timeline, Risks).
  - Not decided, and open for Yu: where Astra's acceptance and the data-lock recomputation run, and how Astra reaches the lab Mac's journal, since the lab's Astra is tool-free (Open for Yu, below; on 2026-11-10 Yu confirmed that Astra stays); whether the Air's helper scripts (arm-live.py, disarm-live.py, config-field.py, configlock.py and pilot-config.py, as the Air's Claude listed them on 2026-11-06) come to the lab Mac; and which first-live gate items, beyond the T0, T1, T2, T5 and T9 redone on the lab Mac, Yu and Astra choose to repeat (the lab's review suggests T3 again on the lab Mac, the phone app included, before the first Pilot 2 session).
- **Yu, 2026-11-06, to lab-Claude,** in a multiple-choice prompt, after Yu wrote "Lets go for LP trading :) Or anything to do before that?" and lab-Claude listed what comes first:
  - **"Count first (Recommended)",** to "Should LP's code wait for the 10-day price count (Gate A)?": Pilot 2's code is built only after Gate A passes, as registered in [docs/lp-rate-count.md](lp-rate-count.md). The option said this costs little time, because Pilot 2's code cannot merge before Pilot 1's go-live anyway.
  - **"Pilot 1 first (Recommended)",** to "Should Pilot 1 still go live before LP?": the relayed "2.是" above, now in Yu's own answer.
  - **"Yes, without asking",** to "May I run the kill on this Mac without asking you first?": lab-Claude may run an own-scope kill on the lab Mac whenever something looks wrong, without asking first, and tells Yu straight after. The option told Yu that the kill never places a bet, cancels all our open orders, manual ones included, and stops Pilot 1 until Yu resumes it. This changes one of Yu's lab rules (page, Who does what).
- **Yu, 2026-11-08, to lab-Claude: "Lets actually create a liquidity provider protocol for betfair utilising the API key from the current developing progress."** In a multiple-choice prompt the same day, Yu chose:
  - **"Draft now, gates unchanged (Recommended)",** to "Does 'actually create' mean I should draft the code now?": the lab drafts PR4 to PR8 now, as PRs that nobody merges yet. The option said that Gate A, Pilot 1 first, Yu's build consent, Astra's review and Yu's three consents still decide when anything merges or trades. So the 2026-11-06 choice above now governs merging, not drafting: if Gate A fails, none of the drafted code merges, and Yu chooses as before between stopping at £0 and the levers. Where [docs/lp-rate-count.md](lp-rate-count.md) says Pilot 2's code is "built" only after Gate A, that is now read as merged.
  - **"P2-LP as drafted in #58 (Recommended)":** the code implements exactly this plan's page, note and draft contract, not a wider quoting engine.
  - **"All of PR4–PR8, stacked (Recommended)":** each stage is its own PR on the one before it, and each is reviewed and passes its tests before the next is built on it.
- **Yu, 2026-11-09, to lab-Claude: "Let the liquidity scraper design be simple. Refer to existing architecture and design for working ones. Lets do some research."**
  - The lab read this as being about the Pilot 2 bot, and the build spec it had drafted for PR4 to PR8 as too heavy. The PR4 work in progress was held, unreviewed and unpushed.
  - The research the same day (the lab's notes "2026-11-09-lp-simple", outside the repo) read flumine, traders' tools, Betfair's API, the evidence, Pilot 1's own order path and market-making practice. It found that every working design is a small strategy on top of a large platform that does the safety: a market feed, a risk gate, an order tracker that reconciles, a clock and a kill. For Pilot 2, Pilot 1's gateway, risk rules, journal, reconcile and kill are that platform. It proposed keeping the shape and the money and cutting the special cases around them.
- **Yu, 2026-11-09, to lab-Claude,** in a multiple-choice prompt after that research:
  - **"Plain capped bot (Recommended)",** to "How simple should Pilot 2 be?". The option said: "Small quote module on Pilot 1's existing caps, journal, reconcile and kill. No registered verdict, ledger, BSP value, tables or live drills. Results are counts and P&L plus your website check. About 3–5k lines over 2 PRs (my estimate, checked before building). Needs the Pilot 2 page rewritten and RS-19/AGENTS.md lines changed: Astra reviews, you approve." The other options were "Trimmed pilot" ("The research's pick. Keeps PR #58's registered-pilot frame (contract freeze, registration, operations verdict) but cuts auto-kill, live drills, ledger and tables. About 13–16k lines over 3 PRs.") and "Full plan as drafted" ("PR4–PR8 as specified: about 27–30k lines over 5 PRs.").
  - **"−10 min, pulled at −5 min (Recommended)",** to "When should it quote?". The option said: "More qualifying races in lp-v1 (85 a year against 63), fewer instant fills, and about 5 minutes to kill by hand if a cancel fails. Books are thinner earlier, and Gate A needs an amendment to count at −10 min before any count is read." The other option was "−5 min, pulled at −1 min" ("As drafted and as Gate A is registered. Busier books, but only about 1 minute of slack if the cancel fails, and more of the informed last-minute flow.").
  - What follows:
    - The page, the note and this plan are rewritten for the plain bot, and the draft contract is retired.
    - Cut, against the page as drafted: the verdict, the ledger, value at BSP, the tables, the drills and probe sessions, registration and the auto-kill (page, Cut from the page as first drafted). Each cut protection needs Yu's yes in plain words at the build consent; choosing the option is not that yes.
    - The decision moves to about off −590 s and the pull to off −300 s (Values and why). Gate A's amendment 3 counts at −590 s before any count is read (Stage 0 and Gate A).
    - The 2026-11-08 choices "P2-LP as drafted in #58" and "All of PR4–PR8, stacked" are replaced by this one. The "3–5k lines" estimate was checked against the code before building: about 980 source and 2,560 test lines (*Estimate*; Stages).
    - Unchanged: Yu's money numbers, "Let it run" (a lone matched leg still rides to the result, but its scoring against BSP is cut with value at BSP), the Heartbeat ban, Gate A, Pilot 1 first, Yu's build consent, Astra's review and Yu's consents. Drafting moves no gate.
    - The option's "about 5 minutes to kill by hand if a cancel fails" is about 3 to 5 minutes in the drafted code (Values and why, Pull). Whether the −5 min choice stands is listed for Yu (Open for Yu).
- **Yu, 2026-11-10, to lab-Claude: "Oh, lets go for the probe without backup. I still havent made the order, am shopping for it now."** Yu had chosen on 2026-11-05 an encrypted backup of the lab Mac before the first order (above). This put the one-pound probe ahead of it; the backup disk is not bought yet. The backup still comes before the manual drills: that was the lab's suggestion, which Yu did not change (Before Pilot 2).
- **Yu, 2026-11-10, to lab-Claude: "Its ok😂 Lets keep always allow".** At the probe macOS asked once for the Mac password, and Yu clicked Always Allow, so `/usr/bin/security` now reads the live key with no dialog. The lab recommended restoring the prompt; Yu decided to keep it.
  - So no Keychain click stands between this macOS account and a live command. The typed lines, the daily token and the armed flag remain.
  - lab-Claude's own-scope kill is now expected to run with nobody at the screen. *Unverified:* not yet tested.
- **On the lab Mac, 2026-11-10** (lab-Claude's session; facts, not decisions):
  - The certificate, the key and five Keychain items are on the lab Mac. Its config.json exists, with Bet Angel not running on the account, the eight acknowledgements and the UK egress attestation; it was armed only for the probe and disarmed after.
  - The first login test, T1, passed on the delayed key.
  - The one-pound probe, T9, passed on the lab Mac: placed, found by ref, cancelled in full, nothing matched. Its `ref_roundtrip_verified` sets A19 in the lab Mac's fresh journal.
  - The delayed-key recorder runs on the lab Mac since that day, pinned at main c86764a: a rehearsal recording first, then the daily job, which Yu loaded and which was recording the same afternoon. So Gate A's recording days begin on the lab Mac on 2026-11-10, a short first day (Stage 0 and Gate A).
  - The review round of the drafted code and its fixes are lab-Claude's. Nothing has merged.
- **Yu, 2026-11-10, to lab-Claude: "lets go with what you recommended and go for the next steps for live testing. We can start preparing recorder here too."** This answered lab-Claude's message of the same day. Yu's yes decides four of its recommendations, quoted here in the lab's words:
  - **The consent order:** "merge the switch-on change first, create and fully edit the Pilot 2 config, then consent on that merged commit and the config hash the suite prints". The page's go-live order follows it (page, Three consents).
  - **Stopping a running session:** "I first only create the KILL file, because the bot then cancels its own two orders within a second. I run kill only if the bot is gone" (page, Who does what).
  - **Astra:** "I read your 'No need codex review' as ending GitHub Codex reviews only, with Astra still the outside reviewer at the consents". Where Astra runs is still open (Open for Yu).
  - **The gates:** "keep both for now": Gate A and Pilot 1 first both stay. The manual £1 drills give live experience first, and the count runs in the background. "When both are in, you'll have real numbers for deciding whether the bot still has to wait for Pilot 1."
  - Not covered by this yes, because the message stated no recommendation for them or gave them only as fixes: the rows added to Open for Yu on 2026-11-10 (below). Each stays a proposal of the lab's.
  - "We can start preparing recorder here too": the recorder rollout above.

## Before Pilot 2: attended manual drills
Yu chose to try a few live orders by hand first: "Yea lets go for the drill, all good" (relayed, 2026-11-04) and "用寬鬆版" (relayed, 2026-11-05). They are not part of Pilot 2. On 2026-11-10 Yu also said yes to the lab's view that the drills give live experience first while the count runs in the background (Decided).
- **The shape.**
  - Yu types every line through the existing typed `place` and `cancel`, under Pilot 1's config on the lab Mac, at its own keyboard, armed for the drills and disarmed after.
  - They run on the lab Mac only after its attended first-live tests, each with Yu present and a receipt (T0, T1, T2, T5 and the T9 live probe), and after Yu's encrypted backup of it (Decided; the backup's place before the drills is the lab's suggestion, which Yu did not change). T1, on the delayed key, and the T9 probe passed on 2026-11-10. Yu ran the probe before the backup (Decided, 2026-11-10).
  - Each race gets one £1 order, alternating BACK and LAY. Each order is at most 2 ticks from the price V13 compares it with: the best back for a BACK, the best lay for a LAY.
  - A lay is at no more than 3.0. Pilot 1's £2 liability cap then allows the £1 stake, and the worst race is −£2.
  - At most 5 races and −£10 in all, then the disarm.
- **Outside Pilot 2.**
  - There is no contract, ledger or verdict.
  - The drills come out of the same account, so they count against the overall budget (page, Money).
  - Every drill order must be cancelled, lapsed, or matched and settled before any Pilot 1 session.
  - Arming changes Pilot 1's config, so after the disarm it must be back to exactly the bytes Pilot 1's go-live consent will name. That is the lab Mac's own config, written with init-config and Yu's values, with a new config_sha256 for Astra to accept; the Air's config is not carried over. Whether the Air's arm and disarm helpers come to the lab Mac is open (Decided).
- **What reading the code showed, before any order:**
  - A typed live `place` takes its own recording admission and is refused while one is held (cli.py:1518-1527; admission.py:667-670). Release is refused while any order is still open (admission.py:410-435), so typed drills rest only one order at a time. The bot holds one admission for its whole session (note §7).
  - Contract p1 and Pilot 1's config set V13 at 2 ticks. A 4-tick spread's inside BACK sits 3 ticks above the best back, so config-p2.json's V13 of 3 is a Pilot 2-only relaxation (page, What it relaxes).
  - A typed live `place` needs A19 (arming.py:53, :477-478), and only the live probe sets it (gateway.py:2673-2674). The lab Mac's journal is fresh (note §2), so the drills waited for the lab Mac's own T9, which set it on 2026-11-10.
- Their receipts stay on the lab Mac, and no agent reads them (Yu's lab rule, 2026-11-05). Nothing from them enters Git without Yu's OK.

## Stage 0 and Gate A
- The count is registered in [docs/lp-rate-count.md](lp-rate-count.md) (PR #56, amended in PR #57), not here.
  - As registered, it reads only the Air's delayed-key recordings from sessions started at or after 2026-11-03 23:05 UTC. Yu chose on 2026-11-05 to start the count again from the lab Mac's recordings (Decided). Amendment 2, which a separate PR adds, names the lab Mac before any count is read, and Gate A then counts only the lab Mac's recordings root. Recordings are never copied between Macs.
  - The recorder runs on the lab Mac since 2026-11-10, pinned at main c86764a: a rehearsal recording, then the daily job Yu loaded that day. The recording days begin there on 2026-11-10, a short first day (Decided).
  - Amendment 3, another separate PR, follows Yu's "−10 min" choice of 2026-11-09 (Decided). The count's decision snapshot moves from off −300 s to off −590 s, and its pre-filter proxy from −460 s to −780 s, the end of the bot's screen. 10 racing days, 300 usable markets and 1.4% stay; changing them is Yu's call. It must merge before any lab count is read, and none has been.
  - It prints counts only.
  - Each run needs Yu's OK.
  - It can be read once 10 racing days and 300 usable markets are recorded.
- Gate A decides whether Pilot 2's code merges. Yu confirmed this on 2026-11-06 ("Count first (Recommended)"), on 2026-11-08 allowed the code to be drafted first, and on 2026-11-10 said yes to keeping it for now (Decided).
- Either way, its pooled rate is an upper estimate of how many races to expect. The bot opens only markets its screen flagged, so the count's persistence column (markets flagged at the screen's time and still qualifying at −590 s), which is never larger, is the nearer one. *Inference:* the pooled rate is mostly an upper bound for a second reason too: it uses delayed, conflated data with no virtual prices, and virtual prices close some spreads. They could also bring a few wider spreads into 3–4 ticks ([docs/lp-rate-count.md](lp-rate-count.md), What it cannot show). A delayed state at −590 s can describe the exchange as early as about −950 s.
- **Gate B is withdrawn.** It would have counted spreads in Pilot 1's stored −300 s decision books, and the bot now decides at −590 s. Yu never chose it.

## Values and why
Money numbers are Yu's (the page, Money). Every other number is chosen by RS-19's principles, with Astra's review, and this is its reason. The page's Values and Money tables and the `p2_lp` constants are pinned to each other by a test, so a value changes only through a code change, Astra's review and Yu's consents again.

| Value | Number | Why |
|---|---|---|
| Market | GB or IE horse-racing WIN, ODDS, due to turn in play, at least 5 active runners | Pilot 1's predicates, plus `turnInPlayEnabled`, so LAPSE acts at the off |
| Screen | one delayed-key row and book between off −840 s and −780 s | it flags markets before the open, so live-key opens are likely to lead to a race (F46) |
| Open | off −740 s | 150 s before the decision, as Pilot 1's open lead: the lock wait, the reconcile budget, the login and the anchor |
| Decision window | off −590 s to −575 s | Yu's "−10 min" (2026-11-09); 15 s, as Pilot 1's window |
| LAY's book | observed by off −532 s, so the LAY leaves by about −530 s | about a minute after the BACK at most; the legs then rest about four and a half minutes |
| LAY's price check | the LAY goes only while the best back is still one tick below its price; else the BACK is pulled (`p2_book_moved`) | *proposed:* the price was set on the BACK's book, and nothing is re-priced (Open for Yu) |
| Best back band | 1.50 to 5.90 | b + 1 tick = 6.00, Yu's lay cap; 1.50 is where lp-v1's band and Pilot 1's start, fixed in advance, not fitted to data |
| Spread | exactly 3 or 4 ticks | at 2 ticks the two inside legs land on one price, and at 1 they cross (V8 refuses both); wider needs V13 above 3 |
| Leg prices | BACK at best lay − 1 tick, LAY at best back + 1 tick | inside the spread on both sides, so neither should match at once |
| BACK first | always | if the second send fails, the leg left resting can lose £2, not £10 |
| Funds and order room | before a BACK: funds for £2 plus the LAY's whole liability, and room for two orders in each order cap | *proposed:* so that R6 and R11 do not refuse the LAY alone once the BACK is placed |
| Pull | off −300 s, by the send book's off; a whole cancel of each unmatched leg, the LAY first | Yu's "pulled at −5 min": if the pull fails, about 3 to 5 minutes are left to cancel by hand or with the kill, because a cancel whose outcome is unknown is reconciled for up to 40 s before the next cancel or the handoff; *Inference:* it also avoids the last minutes, where late fills are most likely to be informed |
| One race at a time; nothing else of ours open | always | any failure is bounded by one race's £10 |
| Session ends after a matched leg | always | the next session waits for Yu's settle |
| Skip streak | 5 opens in a row that end with no order sent (a refused BACK, a failed send-book read, or the decision window missed after the open) end the session | limits live-key reads that end in no bet (F46) |
| V13 | 3 ticks | admits a 4-tick spread's inside BACK and nothing wider |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 races × 2 legs = 10 |
| max_book_age_s; max_minutes_before_off | 2.0 s; 10 | as Pilot 1; V9b checks the second at each leg's send, and −590 s is inside it |
| Rest checks; late wake | every 1 s; 5 s | a check more than 5 s late means a stalled or sleeping Mac, which is a reason to pull |
| Wall/monotonic drift | 2 s | Pilot 1's tolerance; more means a sleep or clock jump, which is a reason to pull |
| First sessions | `RACES 1` until one race has had both legs placed, with Yu watching both legs on the website | puts Yu's eyes on the first live rest and pull, in place of the probe sessions; a race whose BACK was not placed, or whose LAY was refused, has neither |
| Final date | 2027-06-30 (*proposed*) | the same horizon as RS-19; a later date is the cheapest way to more races |

## Expected rate (*Inference*)
- **The base rate.** lp-v1's registered pass on 2015-05..2016-04 PRO data covered 12,208 records over twelve months ([lp-study](lp-study.md), Results). At off −10 min it issued 119 decisions. 85 of them were eligible, on 66 race dates (0.7% of the records). At off −5 min the figures were 94, 63 and 49. Its rule was wider than Pilot 2's: any runner priced 1.50 to 10.0 with a spread of 3 to 20 ticks.
- **Why the issued figure is the closer one.** Eligibility also excludes decisions on facts known only later, such as a runner removed after the decision (lp-study, What it cannot show). A live bot cannot exclude those. So the closer comparison is the 119 issued.
- **Pilot 2's narrower rule** (1.50 to 5.90, 3 or 4 ticks) would issue fewer. The breakdown by band and spread was not released, and lp-v1's checks differ from Pilot 2's. So the most that can be said is: on 2015-16 history, fewer than about 119 a year, and even that is not a strict bound.
  - Gate A's registration estimates more closely, from the design's cells: about 0.33–0.43% of markets at −600 s under Pilot 2's rule ([docs/lp-rate-count.md](lp-rate-count.md), Gate A, as amendment 3 words it). Its 1.4% threshold is about three to four times that, so if today's markets were like 2015-16's, a FAIL would be the expected result. This too is not a strict bound.
- **Live is lower still.**
  - Virtual prices close some spreads.
  - An attended session covers part of the card.
  - The screen misses some markets, and some delayed flags fail on the live book.
  - One race at a time skips markets that come close together, and the session ends after any matched leg.
  - Today's books may differ from 2015-16, in either direction.
- **So expect** well under one race a session, and plausibly only a few races by 2027-06-30. Then neither the 5-a-day nor the 10-in-all cap binds. Stage 0's count at −590 s gives an upper estimate of today's rate, with its persistence count as the nearer one.

## Stages
| Stage | What | Gate | Source lines | Test lines |
|---|---|---|---|---|
| PR-D (#58, this PR) | The page, the note, this plan and the G1 follow-up, rewritten for the plain bot; Pilot 1's document lines listed, not applied; the draft contract retired; the docs test | Astra's review; Yu merges | 0 | about −60 |
| Stage 0 counter (done) | [scripts/lp_rate_count.py](../scripts/lp_rate_count.py) (#56; amendment 1 in #57) | merged | | |
| PR-G (#60) | Gate A's amendment 3: the count at off −590 s | merges before any lab count is read | about 30 | about 120 changed |
| PR-1 (#61) | `p2_lp.py`, `load_config_p2`, `config_p2_path`, the example config-p2, the `p2_lp` boundary entry, the `p2_lp` tests and the two pure Pilot 1 untouched checks (an open Pilot 2 leg is a Pilot 1 manual ref; the ARM record is no pilot note) | merged only after Gate A has passed, the build consent, and Astra's acceptance of Pilot 1's go-live commit | about 320, plus 45 of JSON | about 780 |
| PR-2 (#62) | The gateway, api, journal and cli changes, `p2_run.py`, the `p2_run` boundary entry, the gateway, run and cli tests, and the rest of the Pilot 1 untouched tests (the sha256 pins, the fold, the P2 HALT, the open-leg halt and ARM refusal) | merged only after Pilot 1's PT4 receipt, the build consent and Astra's review; going live is Yu's consent | about 630 | about 1,780 |
| PR-L, the go-live PR | `IN_FORCE` True; the lines the page lists for RS-19, AGENTS.md, order-path and Pilot 1's engineering note, applied by their quoted text; the page's and the note's status lines; the four switched-off tests changed (tests/test_betfair_p2_lp.py, test_betfair_p2_gateway.py, test_betfair_p2_run.py, and the docs test's "are not in force"). Drafted by lab-Claude, reviewed by lab-Claude's rounds and by Astra | merged by Yu after PR-2, before the go-live consent, which names the merged commit (page, Three consents) | | |
| **Total** | | | **about 980 (690–1,350)** | **about 2,560 (1,800–3,400)** |

- PR-G, PR-1 and PR-2 are drafted ahead of their gates (Yu, 2026-11-08; Decided), so the Gate column says only when each may merge. They are opened now as draft PRs, as Yu chose ("PRs that nobody merges yet", 2026-11-08). Yu's OK on their edits to existing test files and on the api.py change is asked before they merge (Open for Yu). When PR-L may be drafted is open (Open for Yu).
- PR-1 is rebased onto main once PR-G is there, and then gains a test that reads scripts/lp_rate_count.py's runner rule and times (by `ast`, no import) and pins them to `p2_lp`'s constants, so a later change on either side cannot leave Gate A counting another rule. It cannot pass before PR-G is on main.
- **The merge order,** in one place: #59 (the move to the lab Mac, in Pilot 1's documents); then #60 (PR-G, stacked on it); then #58 (PR-D), rebased, with one conflict in docs/backtest-plan.md §12 (keep both sides' bullets in date order) and the line numbers the page cites read again; then the Gate A read, the build consent and Astra's acceptance of Pilot 1's go-live commit; then #61 (PR-1), rebased, with the Gate A pin test; Pilot 1's PT4; #62 (PR-2), rebased, with the untouched pins made again; then PR-L. A review is recorded against the head it read. After a rebase the reviewer re-points it with `git range-diff`, and reads again wherever the range-diff is not empty.
- Each code PR is reviewed by lab-Claude's own review rounds and by Astra, and merged by Yu. Yu, 2026-11-09: "No need codex review in the future. All yours". On 2026-11-10 Yu confirmed that this ended GitHub's Codex reviews only, with Astra still the outside reviewer at the consents (Decided). The full offline suite passes, including every Pilot 1 test unchanged.
- **Still without an owner or a written place** (the lab's proposals; none is Yu's decision):
  - the lab's runbook for the recorder on the lab Mac, which exists outside Git and is not yet a page here (the rollout itself, done on 2026-11-10 and pinned at main c86764a, is Yu's: Decided);
  - docs/betfair-integration.md:125 and two printed lines in cli.py (:677 and :913 at main c86764a) still say to answer Allow and never Always Allow for the live key. They need a separate PR that reflects Yu's 2026-11-10 choice to keep Always Allow (Decided);
  - a map of the checkouts on the lab Mac (Pilot 1's registered checkout, Pilot 2's, the recorder's), with the rule that no agent pulls or switches them;
  - how Astra accepts config-p2.json, which it cannot read: the lab proposes that Yu shows the file's difference from the committed example, which holds no secret, together with the hash `init-config --p2` prints;
  - how an agent tells whether a session is armed without reading the state root: the lab proposes the process check (a `p2-run` or `pilot-run` process exists).
- *Estimate,* ±30%. The executor is the least certain figure: pilot_run.py is 2,820 lines. These figures assume stop-flag-only signals, no owed writes, one ticket per session and no end-session command. If Astra wants more, add 50%.
- For comparison, the PR4 to PR8 plan as drafted came to about 27,000–30,000 lines, and the research's "Trimmed pilot" to about 13,000–16,000.

## Open for Yu, at the build consent
None of these needs an answer before the build consent. The three edits to existing files need Yu's OK before the PRs carrying them merge. The Air's Claude made its proposals on 2026-11-04, under Yu's "全部ok, follow what you think is sensible" (relayed), for the pilot as then drafted. The lab's proposals are from its build spec of 2026-11-09 and, for the rows added on 2026-11-10, from its review round of the drafted code. They are shown as proposals, not as Yu's words.

| Question | Proposal |
|---|---|
| Overall budget: raise it, or keep £1,000 with the account balance as the joint stop (page, Money) | The Air's Claude: raise it to £1,100, which now covers −£1,100 exactly |
| Day stop: a hard ceiling (a race only if losing its full £10 still leaves the day at or above −£20), or stop once −£20 is reached. With one session a UTC day that ends after any matched leg, the worst real day is −£10; −£20 is the gateway's ceiling, reached only through a settle typed that day, or with several sessions a day | The Air's Claude: hard ceiling |
| Confirm the 2026-11-03 money numbers for the bot, with "5 a day" counting races from their first submit | The Air's Claude: "confirmed as given", with "at most 5 PLACE decisions a UTC day (SKIPs not counted)"; the lab: races from their first submit |
| A lifetime cap of 10 races | The Air's Claude: "Lifetime cap of 10 decisions: yes"; the lab: 10 races |
| The strict reading: the session ends after any matched leg, and nothing else of ours may be open or unsettled when a BACK goes, so at most £10 is open | the lab: yes |
| One pilot session a UTC day, as the page says and the code enforces; or several sessions, at most 5 races a day, with the page and RS-19's lines reworded. With several, R10 at £20 becomes the stop that binds, and settling between sessions joins the loop | the lab: one a day |
| No pause: the hard stops and the 10-race cap replace it | the lab: yes |
| Pilot 1's ARM refusing a UTC day that already has a P2 ARM record (it does not today; note §3) | the lab: add it in Pilot 1's go-live commit, if Yu and Astra want it |
| config-p2.json's £ caps (note §9) | The Air's Claude: as derived |
| config-p2's total-exposure cap (R9) at £10 instead of £20. Today "at most £10 open at once" rests on the one-race-at-a-time check alone; £10 would make the gateway's own cap a second hold on it and would block nothing the bot does. It changes the page's £ caps row | the lab: yes; not changed in the draft |
| The LAY refused when the best back has fallen since the BACK's book (`p2_book_moved`; page, One race): the BACK is pulled and the race still counts. The other way is to send the LAY at the planned price, as before. How often a one-tick drop happens shows only live | the lab: yes; built this way in the draft |
| Each item under What it relaxes and each cut (page), including dropping P2-PT2 and the probe sessions, the exception path's one best-effort pull, and item 17 (no pull after a kill event) | the lab: yes, each in plain words |
| Whether the bot, on a kill event, should wait up to 30 s and pull its own legs if that kill ends incomplete, as Pilot 1's executor does. Today it never cancels after a kill event (page, What it relaxes, item 17) | the lab: list it as a relaxation; not built |
| The pull's unknown cancel reports can HALT both pilots (note §8): accept it, or have Astra weigh one cancel request carrying both bet ids, which adds 1 to the count, not 2. A status read of both legs just before the pull could only decide whether to send each cancel, since live refresh of open orders was removed in v1 (order-path), so a matched leg it skipped would stay EXECUTABLE with nothing matched in the journal | open, for Astra and Yu |
| After a failed pull, about 3 to 5 minutes are left before the off, not about 5, because a cancel whose outcome is unknown is reconciled for up to 40 s before the next cancel or the handoff. Does the −5 min pull stand (Decided, 2026-11-09)? | the lab: Yu's −5 min choice stands |
| Whether lab-Claude's kill exception (2026-11-06) covers the own-scope kill run from Pilot 2's checkout, by its absolute path, which sweeps `p2-lp`. The stop order for a running session is decided (Decided, 2026-11-10); this is not | open |
| What "the bot is gone" means in that stop order (page, Who does what). The order is decided; the reading is not | the lab: no `p2-run` process; or one that has not ended within about two minutes of the KILL file; or one `ps` shows stopped |
| A `p2_lp` that cannot be imported: today the kill from Pilot 2's checkout then ends exit 3 before its sweep, with nothing cancelled, and every gateway command there fails (note §3). Importing `p2_lp` only in the gateway's lp paths, with the literal `p2-lp` standing in for the kill, would let that kill sweep. It adds a cancel path, so it was withdrawn from the 2026-11-10 review round's fixes | the lab: yes, as its own change with Astra's review |
| A check of the Mac's clock against Betfair's. Every timed step compares Betfair's start time with the Mac's clock, and only drift within a race is caught. Refuse when the two differ by more than a set number of seconds, with a new *proposed* row in the Values table (the review suggested 10 s), or rely on Yu's check | the lab: not built; until then, "Set time automatically" being on joins Yu's pre-session checks |
| Virtual prices at the screen too. The delayed screen reads prices without virtual bets and the decision with them, so some flagged races are refused at the decision after a live login with no order, and count toward the skip streak. A change needs one attended delayed-key read | the lab: leave it, and read the refusal counts from the first sessions |
| A minimum shown amount at both best prices: one £1–£2 order can set the touch and so our prices. If yes, Yu names the amount, and Gate A's count needs the same rule before it is read | the lab: no, not for this 10-race pilot: Betfair's display already rolls up amounts under £1, and each race is capped at £10 |
| A failed first read after the ARM line (the delayed login or the day's market list): nothing was sent, but the day's one session is used. Keep that, or let Yu arm again that day | the lab: keep |
| Between Astra's acceptance and the go-live consent: set `orders.live.armed` back to false, and true again after the consent (the hash then comes back the same), or leave the file armed and rely on the P2 ARM line only Yu types | the lab: set it back to false; the same hash returns |
| When lab-Claude may draft the go-live PR (PR-L; its branch would hold `IN_FORCE` True): now, as a draft nobody merges, or after PR-2 has merged | the lab: after PR-2 has merged |
| The two boundary-table entries in tests/test_betfair_backtest_boundaries.py, against the fallback of no new module (the pure code inside gateway.py and the executor inside cli.py: more lines, harder to review) | the lab: the two entries |
| api.py's `virtualise` option, so the passive check sees virtual prices | the lab: yes |
| tests/test_lp_rate_count.py changing in PR-G | the lab: yes; amendment 3 needs it |
| Final date | The Air's Claude: 2027-06-30 |
| If races are too rare: stop, or the lab brings the levers with their counts (a later final date, lays above 6.0 at a smaller stake, other markets) | The Air's Claude: the levers; Yu decides then |
| Where Astra's acceptance runs now that all live work is on the lab Mac, and how Astra reaches the lab Mac's journal. Astra stays the outside reviewer (Decided, 2026-11-10). Before the move it ran on the Air; the lab's tool-free gpt-6-astra may pre-review, but that is not Astra's acceptance | open |

Also for Yu, before go-live:
- send the [G1 follow-up](betfair-pilot-2-g1-question.md) to Betfair, or record a decision to go ahead without an answer;
- check Basic 2% in My Account;
- confirm the £50 website loss limit is set;
- (*proposed*, the lab's) confirm in the Betfair account's settings that Best Price Execution is on and MatchMe is off. Both are Betfair's defaults, and a UK customer can change either (Betfair's Exchange Rules).

## Timeline (*Inference*)
The estimates written in October and early November predate the lab Mac's first live tests of 2026-11-10 and Yu's answers that day (Decided). The dates below are revised from them.
- **November 2026:** done on the lab Mac on 2026-11-10: the credentials and its config.json, T1 on the delayed key, and the one-pound probe T9. Recording on the lab Mac started on 2026-11-10 (a rehearsal, then the daily job). Next: Yu's encrypted backup (before the drills, as the lab suggested), then the attended manual drills, with the count running in the background (Yu, 2026-11-10). Astra's review of this rewrite, after Pilot 1's review queue. PR-G merges before any count is read. PR-1 and PR-2 stay draft PRs; nothing merges before its gate.
- **Once 10 racing days and 300 usable markets are recorded:** Gate A can be read. *Estimate:* at the earliest nine racing days after the first recorded day, if every racing day is recorded and 300 usable markets are reached by then. If it fails, none of the drafted Pilot 2 code merges (Yu, 2026-11-06 and 2026-11-08); Yu then chooses between stopping at £0 and the levers with their counts.
- **December:** the build consent when Yu is ready, only after Gate A has passed, with fewer relaxations to approve than the plan as drafted. Pilot 1's go-live and PT4 go ahead untouched; Pilot 1's plan expects its first bet in mid-to-late December.
- **January to February 2027:** PR-1 merges once Astra has accepted Pilot 1's go-live commit. After Pilot 1's PT4 receipt, PR-2 follows with lab-Claude's and Astra's review rounds, perhaps 2–4 weeks, with review dominating.
- **February to March:** the G1 answer or Yu's recorded decision, the account checks, the go-live PR merged, config-p2.json made at its commit, Astra's acceptance of that commit and config-p2's hash, and Yu's go-live consent. Then sessions at `RACES 1` until one race has had both legs placed.
- **Late February or March at the earliest, to 2027-06-30:** Pilot 2 sessions on days without Pilot 1.
- *Inference:* waiting for Gate A, as Yu chose on 2026-11-06 and kept on 2026-11-10, does not move these dates, which are set by the Pilot 1-first order and by review time, as long as the lab Mac records its 10 racing days before the build consent. If Pilot 1 slips, Pilot 2 slips by the same amount. When the drills and the count are both in, Yu has real numbers for deciding whether the bot still waits for Pilot 1 (Decided, 2026-11-10). Against the plan as drafted, the plain bot saves perhaps 3–6 weeks, mostly in review time and the probe-session days.

## Risks
- **Too few races.** The likeliest outcome is a handful of races.
- **Adverse selection is expected, not a tail risk.** The costly case is a lone LAY filled just before its runner shortens. It wins £2 if the runner loses but loses up to £10 if it wins, and informed fills make the second more likely. One such fill can wipe out several round trips.
- **Thinner books earlier.** At −10 min books are thinner than at −5 min, so a spread can change between the screen and the send, and fills may be fewer.
- **F46.** Screen false positives still mean some live-key reads with no bet. Betfair's read-only article gives no ratio of reads to bets (F46), so the £499 key both pilots need is at some risk. The delayed screen, the skip streak and the G1 follow-up reduce it, but do not remove it.
- **G1** may not cover resting automated lays. Going live without an answer would be Yu's recorded decision.
- **Taker fills.** A leg meant to rest can still take liquidity if the book moves between its send book and its arrival, or by matching an offer under £1 that Betfair's display rolls up (note §4). `p2-summary` counts the legs matched in their own place report, by side; it records no fill time and cannot tell the two causes apart.
- **Cancel races and the HALT.** A leg that fills during the rest still looks unmatched, so its cancel report is UNKNOWN by design (lifecycle.py:283-297). The in-line reconcile resolves it, but it counts toward the three-UNKNOWN HALT, which stops both pilots until Yu types RESUME LIVE (note §8). A round trip leaves the count at 2, so the next unknown outcome of any order HALTs at once. A residual UNKNOWN blocks all placing (R3) and ends the session with a handoff.
- **No auto-kill.** A failed pull ends in a handoff, with about 3 to 5 minutes left before the off for Yu to cancel in the phone app or run the kill. Since Yu kept Always Allow for the live key on 2026-11-10 (Decided), the kill's Keychain reads are expected to need no click; not yet tested.
- **The Keychain.** Since the same choice, no Keychain click stands between the lab Mac's macOS account and a live command. The typed lines, the daily token and the armed flag remain.
- **No dead-man.** A crash, sleep or network cut leaves the legs resting until the off, bounded at that race's £10.
- **No registration.** The banner shows the commit and the code refuses a dirty tree, but only Yu's comparison of the banner's commit and cfg12 with the merged commit and config-p2's hash that Astra accepted ties the running code to the reviewed code.
- **The shared journal.** The design relies on Pilot 1's unchanged code ignoring Pilot 2's orders and ARM records (note §7). This has been read on main but not yet pinned by a test on main. The untouched tests pin it: the two pure checks in PR-1, the rest in PR-2.
- **Journal-wide effects.** Both pilots fold one journal, so:
  - a Pilot 2 UNKNOWN blocks Pilot 1's placing too (R3), and counts toward the three-UNKNOWN HALT (journal.py:932-933);
  - R10 books a loss on the UTC day its settle is typed. A Pilot 2 settle typed on a Pilot 1 day uses Pilot 1's £50 of room, which is the safe direction. The reverse: a settle typed on a day that books more than £10 refuses that day's P2 ARM before the line (`p2_pair_precheck:daily_loss`).
- **Shared order-path code.** PR-2 touches gateway.py, api.py, journal.py and cli.py, and with them the only order path; risk.py does not change. After it merges, any Pilot 1 fix taken from main carries Pilot 2's code (page, What it relaxes, item 16). Hence the merge order, Pilot 1's pinned checkout, Pilot 1's unchanged tests and the untouched tests.
- **Two configs and two checkouts in one profile.** Every mismatch refuses (`config_problems`, the token keyed by each config's hash, and Pilot 1's PA1–PA3), so the cost is lost sessions, not money.
- **One Mac only.** Every exclusion (the KILL file, `.gateway.lock`, accounts.json, the journal and its caps, the login ledger) is per state root, so per Mac (note §2). Nothing in the code stops the Air and the lab Mac trading one account at once; only the move's order and Yu's rule that the Air runs no live command do.
- **A busy lab Mac.** Pilot 2's legs rest, and its rest loop must pull them at off −300 s (note §8). *Inference:* heavy work could delay that loop. The drift and late-wake checks act once the loop runs again. Yu's lab rule keeps heavy work off the lab Mac while a session is armed or an order rests. The bound is still that race's £10.
- **The budget overlap** (page, Money). If the account balance is the joint bound, whichever pilot runs later can be starved by R11, and then each BACK is skipped for funds (page, Money).
- **Commission.** The 2026 rate is unconfirmed; 2% is assumed until Yu checks it.
