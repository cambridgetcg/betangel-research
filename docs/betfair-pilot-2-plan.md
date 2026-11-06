# Pilot 2 plan: P2-LP, attended two-sided quotes

Status: proposed, together with the [Pilot 2 page](betfair-pilot-2-policy.md) and its [engineering note](betfair-pilot-2-engineering.md). Nothing is approved. The current rules stay in force until Yu's go-live consent on the Pilot 2 page.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, from the lab's Pilot 2 design of 2026-11-03.

- "The page" means [docs/betfair-pilot-2-policy.md](betfair-pilot-2-policy.md). "The note" means [docs/betfair-pilot-2-engineering.md](betfair-pilot-2-engineering.md). "Contract p2-lp" means [research/pilots/p2-lp.json](../research/pilots/p2-lp.json), a draft until the build consent.
- The page holds the policy and the money. The note holds the engineering detail, and the page wins on any difference. This plan keeps only its own content: the goal, what has been decided, the reason for each value, the expected rate, the stages and the timeline.
- *Estimate* and *Inference* mark numbers that have not been measured.
- **Yu's words** are quoted with their date and where they were said. "To lab-Claude" means Yu's own answer in the lab's session. "Relayed" means Yu said it in chat with the Air's Claude, which passed it on through the Air–lab mailbox. By merging this plan, Yu confirms only that the quoted words are accurate, or asks for a correction. Merging decides nothing that is still open, approves no page and accepts no proposal.

## Goal
- Find out, with real money at micro stakes, whether an attended executor can do all of this:
  - post two passive £2 orders on one runner;
  - rest them about four minutes;
  - pull them 60 s before the off;
  - stop in an orderly way under every fault, never passing Yu's −£20 day and −£100 pilot limits.
- See whether and how live fills happen: each leg filling, both filling (a round trip), a leg matched at once (a taker fill), and how long fills take. With the few decisions expected, these are counts, not rates.
- Report each decision's value at BSP after 2%, and the settled profit. Money never decides.
- **Honest prior.**
  - No mispricing is known.
  - The research round's judgement, which is not evidence, leans to "we are picked off" ([lp-study](lp-study.md), Predictions). lp-v1 could neither support nor rule that out.
  - Its only released statistics are descriptive. In lp-v1's positive control at off −10 min, the mid moved against queued orders by 0.54 ticks at +10 s and 1.08 ticks by BSP. That is partly by construction, drift is not excluded, and it says nothing about orders one tick inside the spread ([lp-study](lp-study.md), Results).
  - Pilot 2's legs improve the price rather than join it. Whether that is less adverse is unmeasured.
  - *Inference:* per decision, expect a few pence negative. A one-tick round trip, BACK 5.1 over LAY 5.0, is worth about +4p at BSP 5.0. A lone LAY at 5.0 that BSP moves one tick against is worth about −7p. Realised money is luck, dominated by any lone LAY whose runner wins: about −£8 at 5.0 and −£10 at 6.0.
  - Decisions will be rare (Expected rate, below). The verdict will be about operations. It is never a claim of an edge.
- **Why it is worth testing.**
  - Offline history cannot answer it. lp-v1's registered pass was underpowered at every decision time. Its fill model has no queue, no latency and no virtual prices, so its modelled fills of 89–98% per main leg are optimistic ([lp-study](lp-study.md), Results and What it cannot show).
  - Only live fills can test that optimism, and a handful of decisions will show only whether it is badly wrong.
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
  - **Backup:** Yu sets up an encrypted local backup of the lab Mac, covering the state root and the Keychain, before the first order.

  Yu also approved rules for the lab Mac, kept in the lab's own instructions, not in the repo. Those that bear on Pilot 2 are on the page (Who does what).
  - What follows for Pilot 2 is on the page (What it allows, Who does what, How it ends), in the note (§2, §8, §15) and below (Before Pilot 2, Stage 0 and Gate A, Timeline, Risks).
  - Not decided, and open for Yu: where Astra's acceptance and the data-lock recomputation run, and how Astra reaches the lab Mac's journal, since the lab's Astra is tool-free (Open for Yu, below); whether the Air's helper scripts (arm-live.py, disarm-live.py, the config-field helper and livekey-check.py) come to the lab Mac; and which first-live gate items, beyond the T0, T1, T2, T5 and T9 redone on the lab Mac, Yu and Astra choose to repeat.
- **Yu, 2026-11-06, to lab-Claude,** in a multiple-choice prompt, after Yu wrote "Lets go for LP trading :) Or anything to do before that?" and lab-Claude listed what comes first:
  - **"Count first (Recommended)",** to "Should LP's code wait for the 10-day price count (Gate A)?": Pilot 2's code is built only after Gate A passes, as registered in [docs/lp-rate-count.md](lp-rate-count.md). The option said this costs little time, because Pilot 2's code cannot merge before Pilot 1's go-live anyway.
  - **"Pilot 1 first (Recommended)",** to "Should Pilot 1 still go live before LP?": the relayed "2.是" above, now in Yu's own answer.
  - **"Yes, without asking",** to "May I run the kill on this Mac without asking you first?": lab-Claude may run an own-scope kill on the lab Mac whenever something looks wrong, without asking first, and tells Yu straight after. The option told Yu that the kill never places a bet, cancels all our open orders, manual ones included, and stops Pilot 1 until Yu resumes it. This changes one of Yu's lab rules (page, Who does what).

## Before Pilot 2: attended manual drills
Yu chose to try a few live orders by hand first: "Yea lets go for the drill, all good" (relayed, 2026-11-04) and "用寬鬆版" (relayed, 2026-11-05). They are not part of Pilot 2.
- **The shape.**
  - Yu types every line through the existing typed `place` and `cancel`, under Pilot 1's config on the lab Mac, at its own keyboard, armed for the drills and disarmed after.
  - They run on the lab Mac only after Yu's encrypted backup of it (Decided) and its attended first-live tests, each with Yu present and a receipt: T0, T1, T2, T5 and the T9 live probe.
  - Each race gets one £1 order, alternating BACK and LAY. Each order is at most 2 ticks from the price V13 compares it with: the best back for a BACK, the best lay for a LAY.
  - A lay is at no more than 3.0. Pilot 1's £2 liability cap then allows the £1 stake, and the worst race is −£2.
  - At most 5 races and −£10 in all, then the disarm.
- **Outside Pilot 2.**
  - There is no contract, ledger or verdict.
  - The drills come out of the same account, so they count against the overall budget (page, Money).
  - Every drill order must be cancelled, lapsed, or matched and settled before any Pilot 1 session.
  - Arming changes Pilot 1's config, so after the disarm it must be back to exactly the bytes Pilot 1's go-live consent will name. That is the lab Mac's own config, written with init-config and Yu's values, with a new config_sha256 for Astra to accept; the Air's config is not carried over. Whether the Air's arm and disarm helpers come to the lab Mac is open (Decided).
- **What reading the code showed, before any order:**
  - A typed live `place` takes its own recording admission and is refused while one is held (cli.py:1518-1527; admission.py:667-670). Release is refused while any order is still open (admission.py:410-435), so typed drills rest only one order at a time. The executor holds one admission for its whole session (note §7).
  - Contract p1 and Pilot 1's config set V13 at 2 ticks. A 4-tick spread's inside BACK sits 3 ticks above the best back, so config-p2.json's V13 of 3 is a Pilot 2-only relaxation (page, What it relaxes).
  - A typed live `place` needs A19 (arming.py:53, :477-478), and only the live probe sets it (gateway.py:2673-2674). The lab Mac's journal is fresh (note §2), so the drills wait for the lab Mac's own T9.
- Their receipts stay on the lab Mac, and no agent reads them (Yu's lab rule, 2026-11-05). Nothing from them enters Git without Yu's OK.

## Stage 0 and Gate A
- The count is registered in [docs/lp-rate-count.md](lp-rate-count.md) (PR #56, amended in PR #57), not here.
  - As registered, it reads only the Air's delayed-key recordings from sessions started at or after 2026-11-03 23:05 UTC. Yu chose on 2026-11-05 to start the count again from the lab Mac's recordings (Decided). Amendment 2, which a separate PR adds, names the lab Mac before any count is read, and Gate A then counts only the lab Mac's recordings root. Recordings are never copied between Macs.
  - It prints counts only.
  - Each run needs Yu's OK.
  - It can be read once 10 racing days and 300 usable markets are recorded.
- Gate A decides whether Pilot 2's code is built, as registered. Yu confirmed this on 2026-11-06 ("Count first (Recommended)", Decided).
- Either way, its pooled rate is the best estimate of how many decisions to expect. *Inference:* it is mostly an upper bound, because it uses delayed, conflated data with no virtual prices, and virtual prices close some spreads. They could also bring a few wider spreads into 3–4 ticks ([docs/lp-rate-count.md](lp-rate-count.md), What it cannot show).
- **Gate B** is optional and needs Yu's yes (Open for Yu; note §16).

## Values and why
Money numbers are Yu's (the page, Money). Every other number is chosen by RS-19's principles before the freeze, with Astra's review, and this is its reason. After the freeze, a value in contract p2-lp changes only through a new contract with the build and go-live consents again.

| Value | Number | Why |
|---|---|---|
| Market | GB or IE horse-racing WIN, at least 5 active runners | Pilot 1's predicates, so the scheduler and checks are shared |
| Pre-filter read | one delayed-key book between off −540 s and −460 s | it flags markets before the planned open at −450 s, so live-key reservations are likely to lead to a decision (F46) |
| Decision time and window | off −300 s, observed by −285 s | Pilot 1's scheduler, window, end margin and send deadline then apply unchanged |
| Send deadline | off −240 s | as Pilot 1; the legs still rest about four minutes |
| Best back band | 1.50 to 5.90 | b + 1 tick = 6.00, Yu's lay cap; 1.50 is where lp-v1's band and Pilot 1's start, fixed in advance, not fitted to data |
| Spread | exactly 3 or 4 ticks | at 2 ticks the two inside legs land on one price, and at 1 they cross (V8 refuses both); wider needs V13 above 3 |
| Leg prices | BACK at best lay − 1 tick, LAY at best back + 1 tick | inside the spread on both sides, so neither should match at once |
| BACK first | always | if the second send fails, the leg left resting can lose £2, not £10 |
| Cancel point | off −60 s, by the record's off, read-cancel-read | a failed cancel still leaves the kill's re-sweeps before the off; *Inference:* it avoids the last minute, where late fills are most likely to be informed |
| One race in flight | always | any failure is bounded by one race's £10 |
| SKIP streak | 5 live-key SKIPs in a row end the session | limits live-key reads that end in no bet (F46) |
| V13 | 3 ticks | admits a 4-tick spread's inside BACK and nothing wider |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 decisions × 2 legs = 10 |
| max_book_age_s; max_minutes_before_off | 2.0 s; 10 | as Pilot 1 (V9b checks the second at each leg's send) |
| Wall/monotonic drift | 2 s | more means a sleep or clock jump, which is a reason to pull |
| Wait for a kill outcome before the handoff | 30 s | as Pilot 1 |
| Suspended-market poll | every 5 s | often enough to send one more cancel if the market reopens before the off |
| Finalization deadline; BSP cutoff | off + 7 days; off + 24 h | as Pilot 1 |
| Verdict | operations only: PASS at 10 PLACE records with no failure | each safety fact can fail on one event; money can answer nothing at this N |
| P2-PT4 | at most 5 decisions a session until a matched, settled LAY is checked | tests the lay bookkeeping (profit sign and basis, F37) before any LAY enters the ledger's settled part. It equals the 5-a-day cap, so its protection is Yu's check of each decision and the receipt the settled ledger needs; a lower number is Yu's choice at the build consent |
| Final date | 2027-06-30 (*proposed*) | the same horizon as RS-19; a later date is the cheapest way to more decisions |

## Expected rate (*Inference*)
- **The base rate.** lp-v1's registered pass on 2015-05..2016-04 PRO data covered 12,208 records over twelve months ([lp-study](lp-study.md), Results). At off −5 min it issued 94 decisions. 63 of them were eligible, on 49 race dates (0.5% of the records). Its rule was wider than Pilot 2's: any runner priced 1.50 to 10.0 with a spread of 3 to 20 ticks.
- **Why 94 is the closer figure.** Eligibility also excludes decisions on facts known only later, such as a runner removed after the decision (lp-study, What it cannot show). A live executor cannot exclude those. So the closer comparison is the 94 issued.
- **Pilot 2's narrower rule** (1.50 to 5.90, 3 or 4 ticks) would issue fewer. The breakdown by band and spread was not released, and lp-v1's checks differ from Pilot 2's. So the most that can be said is: on 2015-16 history, fewer than about 94 a year, and even that is not a strict bound.
- **Live is lower still.**
  - Virtual prices close some spreads.
  - An attended session covers part of the card.
  - The pre-filter misses some markets, and some delayed flags fail on the live book.
  - One race in flight skips markets that come close together.
  - Today's books may differ from 2015-16, in either direction.
- **So expect** well under one decision a session, and plausibly only a few decisions by 2027-06-30. Then neither the 5-a-day nor the 10-in-all cap binds, and the verdict is INCOMPLETE. Stage 0's count gives today's rate.

## Stages
| Stage | What | Gate | Size |
|---|---|---|---|
| PR1 (this PR) | The page, the note, this plan, the draft contract p2-lp and the G1 follow-up. Docs only, plus a small test of the draft contract. Pilot 1's documents are unchanged | Astra's review; Yu merges | M |
| PR2 (done) | The Stage 0 counter, [scripts/lp_rate_count.py](../scripts/lp_rate_count.py) (#56; amendment 1 in #57) | merged | S |
| PR3 (optional) | Gate B: a counts-only table over Pilot 1's stored decision books | only with Yu's yes, and merged before Pilot 1's first PLACE record | S |
| PR4 | The pure module pilot_lp.py: the contract loader, predicates, leg prices, passive check, two-leg record, fold, ledger, classes, value, verdict and tables | the build consent and Gate A passed (Yu, 2026-11-06); merged only after Astra accepts Pilot 1's go-live commit | L |
| PR5 | The gateway path for `pilot_lp`: two sends under one reservation, the passive check, read-cancel-read, the suspended-market rule, virtual prices on Pilot 2's books only, the in-session halts, and the drill probe's path (R13 to a drill record and its exemptions, for the fixed probe shape only) | merged only after Pilot 1's PT4 receipt, after the cross-version check, and after Gate B if PR3 exists | L, highest scrutiny |
| PR6 | The executor and the `plp-*` commands: pre-filter, rest loop, pull, auto-kill, arming refusals | after PR5 | L |
| PR7 | Capture, the data lock and the terminal report | after PR6 | M |
| PR8 | P2-PT2's dry day, P2-PT3's stand-in transport for the fault drills, and the probe-session mode | after PR7 | M |

Each code PR is reviewed by Codex and Astra and merged by Yu. The full offline suite passes, including every Pilot 1 test unchanged.

## Open for Yu, at the build consent
None of these needs an answer before the build consent, except Gate B, which needs Yu's answer in time for PR3 to merge before Pilot 1's first PLACE record. The Air's Claude made its proposals on 2026-11-04, under Yu's "全部ok, follow what you think is sensible" (relayed). They are shown as proposals, not as Yu's words.

| Question | The Air's Claude's proposal |
|---|---|
| Overall budget: raise it, or keep £1,000 with the account balance as the joint stop (page, Money) | raise it to £1,100 (made before the manual drills' −£10 was counted; covering −£1,108 needs a budget of at least £1,108) |
| Day stop: a hard ceiling (a race only if losing its full £10 still leaves the day at or above −£20), or stop once −£20 is reached | hard ceiling |
| Confirm the 2026-11-03 money numbers for the automated pilot, with "5 a day" counting PLACE decisions | confirmed as given |
| A lifetime cap of 10 decisions | yes |
| One race with resting orders at a time; at most £20 of worst case open | (not asked; the design's proposal) |
| No pause: the hard stops and the 10-decision cap replace it | (not asked; the design's proposal) |
| config-p2.json's £ caps (note §9) | as derived |
| Drill probes in the probe sessions: at most £1 a race and at most 8 drill races | yes |
| P2-PT4's decisions counting in the verdict (page, What it relaxes) | (not asked; the design's proposal) |
| Final date | 2027-06-30 |
| If decisions are too rare: stop, or the lab brings the levers with their counts (deciding from −600 to −300 s, a later final date, lays above 6.0 at a smaller stake, other markets) | the levers; Yu decides then |
| Gate B (PR3) | yes, but only if Gate A passes and only if it does not delay Pilot 1 |
| Reviews: Astra on the Air accepts; the lab's tool-free gpt-6-astra may pre-review, but that is not Astra's acceptance. Since the move (Decided, 2026-11-05), also: where Astra's acceptance and the data-lock recomputation run, and how Astra reaches the lab Mac's journal | as stated, proposed before the move; where Astra runs is open for Yu |

Also for Yu, before go-live:
- send the [G1 follow-up](betfair-pilot-2-g1-question.md) to Betfair, or record a decision to go ahead without an answer;
- check Basic 2% in My Account;
- confirm the £50 website loss limit is set.

## Timeline (*Inference*)
- **November 2026:** this PR and Astra's review, after Pilot 1's review queue. The move to the lab Mac and its attended first-live tests (Decided). Then the attended manual drills, after the lab Mac's T9 probe. Recording starts on the lab Mac when Yu loads the recorder there.
- **Once 10 racing days and 300 usable markets are recorded:** Gate A can be read. If it fails, no Pilot 2 code is built (Yu, 2026-11-06); Yu then chooses between stopping at £0 and the levers with their counts.
- **December:** the build consent when Yu is ready. PR4 is drafted once Gate A has passed and Yu has given the build consent. Pilot 1's go-live and PT4 go ahead untouched; Pilot 1's plan expects its first bet in mid-to-late December.
- **January to February 2027:** PR4 merges once Astra has accepted Pilot 1's go-live commit. After Pilot 1's PT4 receipt, PR5 to PR8 follow with Codex and Astra rounds, about 4–6 weeks, with review dominating.
- **February to March:** P2-PT2, P2-PT3, the G1 answer or Yu's recorded decision, the commission check, Astra's acceptance, Yu's go-live consent and registration. Then about 8 probe sessions, each on a day with no Pilot 1 session, and P2-PT4.
- **March or April at the earliest, to 2027-06-30:** Pilot 2 sessions on days without Pilot 1.
- *Inference:* waiting for Gate A, as Yu chose on 2026-11-06, does not move these dates, which are set by the Pilot 1-first order and by review time, as long as the lab Mac records its 10 racing days before the build consent. If Pilot 1 slips, Pilot 2 slips by the same amount.

## Risks
- **Too few decisions.** The likeliest outcome is a handful of decisions and an INCOMPLETE verdict.
- **Adverse selection is expected, not a tail risk.** The costly case is a lone LAY filled just before its runner shortens. It wins £2 if the runner loses but loses up to £10 if it wins, and informed fills make the second more likely. One such fill can wipe out several round trips.
- **F46.** Pre-filter false positives still mean some live-key reads with no bet. Betfair's read-only article gives no ratio of reads to bets (F46), so the £499 key both pilots need is at some risk. The delayed pre-filter, the SKIP-streak cap and the G1 follow-up reduce it, but do not remove it.
- **G1** may not cover resting automated lays. Going live without an answer would be Yu's recorded decision.
- **Taker fills.** A leg meant to rest can still take liquidity if the book moves between its send book and its arrival. They are counted as taker fills.
- **Cancel races.** A leg filling between the read and the cancel gives an UNKNOWN by design (lifecycle.py:283-297). The second read resolves it. A residual UNKNOWN blocks all placing (R3) and triggers auto-kill.
- **No dead-man.** A crash, sleep or network cut leaves the legs resting until the off, bounded at that race's £10.
- **The shared journal.** The design relies on Pilot 1's unchanged code ignoring `pilot_lp` records (note §7). This has been read on main but not yet pinned by a test. The cross-version check pins it before PR5 merges.
- **Journal-wide effects.** Both pilots fold one journal, so:
  - a Pilot 2 UNKNOWN blocks Pilot 1's placing too (R3), and counts toward the three-UNKNOWN HALT (journal.py:932-933);
  - R10 books a loss on the UTC day its settle is typed. A Pilot 2 settle typed on a Pilot 1 day uses Pilot 1's £50 of room, which is the safe direction. The reverse can end a Pilot 2 session early.
- **Auto-kill stops Pilot 1 too,** until Yu types RESUME LIVE. That is safe, but it costs Pilot 1 sessions.
- **Shared order-path code.** PR5 touches gateway.py and risk.py, the only order path. After it merges, any Pilot 1 fix taken from main carries Pilot 2's code (page, What it relaxes, item 18). Hence the merge order, Pilot 1's pinned checkout, Pilot 1's unchanged tests and the cross-version check.
- **Two configs and two worktrees in one profile.** Every mismatch refuses (PA1–PA3 fail closed), so the cost is lost sessions, not money.
- **One Mac only.** Every exclusion (the KILL file, `.gateway.lock`, accounts.json, the journal and its caps, the login ledger) is per state root, so per Mac (note §2). Nothing in the code stops the Air and the lab Mac trading one account at once; only the move's order and Yu's rule that the Air runs no live command do.
- **A busy lab Mac.** Pilot 2's legs rest, and its rest loop must pull them at off −60 s (note §8). *Inference:* heavy work could delay that loop. The drift check sees a sleep or a clock jump, not a stalled process, and case 7 acts only once the loop runs again. Yu's lab rule keeps heavy work off the lab Mac while a session is armed or an order rests. The bound is still that race's £10.
- **The budget overlap** (page, Money). If the account balance is the joint bound, whichever pilot runs later can be starved by R11.
- **Commission.** The 2026 rate is unconfirmed; 2% is assumed until Yu checks it.
