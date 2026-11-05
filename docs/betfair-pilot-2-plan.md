# Pilot 2 plan: P2-LP, attended two-sided quotes

Status: proposed, together with the [Pilot 2 page](betfair-pilot-2-policy.md) and its [engineering note](betfair-pilot-2-engineering.md). Nothing is approved. The current rules stay in force until Yu's go-live consent on the Pilot 2 page.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, from the lab's Pilot 2 design of 2026-11-03.

- "The page" means [docs/betfair-pilot-2-policy.md](betfair-pilot-2-policy.md). "The note" means [docs/betfair-pilot-2-engineering.md](betfair-pilot-2-engineering.md). "Contract p2-lp" means [research/pilots/p2-lp.json](../research/pilots/p2-lp.json), a draft until the build consent.
- The page holds the policy and the money. The note holds the engineering detail, and the page wins on any difference. This plan keeps only its own content: the goal, what has been decided, the reason for each value, the expected rate, the stages and the timeline.
- *Estimate* and *Inference* mark numbers that have not been measured.
- **Yu's words** are quoted with their date and where they were said. "To lab-Claude" means Yu's own answer in the lab's session. "Relayed" means Yu said it in chat with the Air's Claude, which passed it on through the Air–lab mailbox. Yu confirms relayed answers, or corrects them, by merging this plan or asking for a change.

## Goal
- Find out, with real money at micro stakes, whether an attended executor can post two passive £2 orders on one runner, rest them about four minutes, pull them 60 s before the off, and stop in an orderly way under every fault, with Yu's −£20 day and −£100 pilot limits never passed.
- Measure live fills: how often each leg fills, how often both fill (a round trip), how often a leg is matched at once (a taker fill), and how long fills take.
- Report each decision's value at BSP after 2%, and the settled profit. Money never decides.
- **Honest prior.**
  - No mispricing is known. The evidence leans to "we are picked off" ([lp-study](lp-study.md), Predictions). In lp-v1's positive control at off −10 min, the mid moved against queued orders by 0.54 ticks at +10 s and 1.08 ticks by BSP ([lp-study](lp-study.md), Results; descriptive only).
  - Pilot 2's legs improve the price rather than join it. Whether that is less adverse is unmeasured.
  - Per decision, expect a few pence negative (*Inference*). A one-tick round trip, BACK 5.1 over LAY 5.0, is worth about +4p at BSP 5.0. A lone LAY at 5.0 that BSP moves one tick against is worth about −7p. Realised money is luck, dominated by any lone LAY whose runner wins: about −£8 at 5.0 and −£10 at 6.0.
  - Decisions will be rare (Expected rate, below). The verdict will be about operations. It is never a claim of an edge.
- **Why it is worth testing.** Offline history cannot answer it. lp-v1's registered pass was underpowered at every decision time, and its fill model has no queue, no latency and no virtual prices, so its modelled fills of 89–98% per main leg are optimistic ([lp-study](lp-study.md), Results and What it cannot show). Only live fills can measure that optimism. A £2 order moves nothing, can wait and can skip freely, and any larger liquidity step needs these operations facts first.

## Decided
- **Yu, 2026-11-01:** asked whether providing liquidity in thin, wide-spread markets can pay. That started lp-v1 ([lp-study](lp-study.md)).
- **Yu, 2026-11-03, to lab-Claude,** after lp-v1:
  - "Automated pilot only": the first step is an automated pilot, with no hand trial first.
  - Money, from an option offered for a hand trial, carried over to this pilot: £2 each side; lays only up to 6.0, so the worst race is −£10; at most 5 races a day; a day stop at −£20; a pilot stop at −£100; inside the £1,000 overall budget and "NOT part of Pilot 1's counts". Yu confirms them for the automated pilot at the build consent.
  - "Let it run": a lone matched leg rides to the result after the unmatched side is cancelled before the off, and is scored against BSP.
  - "Yes, PRs only": the lab may draft the page, the plan and the executor code as PRs; Astra reviews; Yu merges; nothing goes live without Yu's three consents.
- **Yu, 2026-11-03, to lab-Claude:** "Count first". Before any Pilot 2 code, count for free how often today's markets show the spreads Pilot 2 would quote in. Registered in [docs/lp-rate-count.md](lp-rate-count.md), with Yu's choices on which run decides, and amended once on 2026-11-04 on Yu's choice "Only new recordings".
- **Yu, 2026-11-04, relayed:** "We can start now ah😂 no need record 10 days. And we can start small." This is the decision to go on without waiting for Gate A that [Gate A's FAIL branch](lp-rate-count.md#gate-a) allows for. The count still runs as registered (Stage 0, below).
- **Yu, 2026-11-04, relayed:** "2.是", keeping Pilot 1 first. PR4 waits for Astra's acceptance of Pilot 1's go-live commit, and PR5 waits for Pilot 1's PT4.
- **Yu, 2026-11-05, relayed:** "可以 let lab go ahead. 得", the go-ahead for the lab to draft this PR (stage PR1). It is not the build consent. The Air's Claude's proposals for the open questions remain proposals (Open for Yu, below).

## Before Pilot 2: attended manual drills
Yu chose to try a few live orders by hand first ("Yea lets go for the drill, all good", relayed, 2026-11-04; "用寬鬆版", relayed, 2026-11-05). They are not part of Pilot 2.
- **The shape.** Yu types every line through the existing typed `place` and `cancel`, under Pilot 1's config on the Air. It is one order per race, alternating BACK and LAY, at most 2 ticks from the price V13 compares it with (the best back for a BACK, the best lay for a LAY), with a lay at no more than 3.0. That is at most 5 races and −£10 in all, then a disarm. The worst race is −£2.
- **Outside Pilot 2.** There is no contract, ledger or verdict. The drills come out of the same account, so they count against the overall budget (page, Money). Every drill order must be cancelled, lapsed, or matched and settled before any Pilot 1 session, and Pilot 1's config must be back to its exact bytes after the disarm.
- **What they already showed, from reading the code before any order:**
  - A typed live `place` takes its own recording admission and is refused while one is held (cli.py:1518-1527; admission.py:667-670). So typed drills rest only one order at a time. The executor holds one admission for its session (note §7).
  - Contract p1 and Pilot 1's config set V13 at 2 ticks. A 4-tick spread's inside legs are 3 ticks from their reference, so config-p2.json's V13 of 3 is a Pilot 2-only relaxation (page, What it relaxes).
- Their receipts stay on the Air. Nothing from them enters Git without Yu's OK.

## Stage 0 and Gate A
- The count is registered in [docs/lp-rate-count.md](lp-rate-count.md) (PR #56, amended in PR #57), not here. It reads only the Air's delayed-key recordings from sessions started at or after 2026-11-03 23:05 UTC, and prints counts only.
- Gate A no longer decides whether Pilot 2 is built. On 2026-11-04 Yu chose to go on without waiting for it (Decided).
- The count still runs as registered, with Yu's OK for each run, once 10 racing days and 300 usable markets are recorded. Its pooled rate is the best estimate of how many decisions to expect. It is an upper bound: delayed, conflated data with no virtual prices.
- **Gate B** stays optional and is not proposed now (note §16).

## Values and why
Money numbers are Yu's (the page, Money). Every other number is chosen by RS-19's principles before the freeze, with Astra's review, and this is its reason. After the freeze, a value in contract p2-lp changes only through a new contract with the build and go-live consents again.

| Value | Number | Why |
|---|---|---|
| Market | GB or IE horse-racing WIN, at least 5 active runners | Pilot 1's predicates, so the scheduler and checks are shared |
| Pre-filter read | one delayed-key book between off −540 s and −460 s | it flags markets before the planned open at −450 s, so live-key reservations are likely to lead to a decision (F46) |
| Decision time and window | off −300 s, observed by −285 s | Pilot 1's scheduler, window, end margin and send deadline then apply unchanged |
| Send deadline | off −240 s | as Pilot 1; the legs still rest about four minutes |
| Best back band | 1.50 to 5.90 | b + 1 tick = 6.00, Yu's lay cap; below 1.50, too few ticks of spread to quote inside |
| Spread | exactly 3 or 4 ticks | below 3 the two inside legs land on one price (V8 refuses); wider needs V13 above 3 |
| Leg prices | BACK at best lay − 1 tick, LAY at best back + 1 tick | inside the spread on both sides, so neither should match at once |
| BACK first | always | if the second send fails, the leg left resting can lose £2, not £10 |
| Cancel point | off −60 s, read-cancel-read | a failed cancel still leaves the kill's re-sweeps before the off; it avoids the last minute, where late fills are most likely to be informed |
| One race in flight | always | any failure is bounded by one race's £10 |
| SKIP streak | 5 live-key SKIPs in a row end the session | limits live-key reads that end in no bet (F46) |
| V13 | 3 ticks | admits a 4-tick spread's legs and nothing wider |
| Orders a minute, an hour, a day | 2, 12, 10 | two legs fit in one minute; 5 decisions × 2 legs = 10 |
| max_book_age_s; max_minutes_before_off | 2.0 s; 10 | as Pilot 1, with room for the −540 s read |
| Wall/monotonic drift | 2 s | more means a sleep or clock jump, which is a reason to pull |
| Wait for a kill outcome before the handoff | 30 s | as Pilot 1 |
| Suspended-market poll | every 5 s | often enough to send one more cancel if the market reopens before the off |
| Finalization deadline; BSP cutoff | off + 7 days; off + 24 h | as Pilot 1 |
| Verdict | operations only: PASS at 10 PLACE records with no failure | each safety fact can fail on one event; money can answer nothing at this N |
| P2-PT4 | at most 5 decisions a session until a matched, settled LAY is checked | tests the lay bookkeeping (profit sign and basis, F37) before any LAY enters the ledger's settled part |
| Final date | 2027-06-30 (*proposed*) | the same horizon as RS-19; a later date is the cheapest way to more decisions |

## Expected rate (*Inference*)
- **The base rate.** lp-v1's registered pass on 2015-05..2016-04 PRO found 63 eligible decisions at off −5 min, on 49 race dates, out of 12,208 records (0.5%) in twelve months ([lp-study](lp-study.md), Results). Its rule was wider: any runner priced 1.50 to 10.0 with a spread of 3 to 20 ticks.
- Pilot 2's rule is narrower (1.50 to 5.90, 3 or 4 ticks), so it keeps only part of those 63. The breakdown by band and spread was not released, so 63 a year is an upper bound.
- **Live is lower still.** Virtual prices close some spreads. An attended session covers part of the card. The pre-filter misses some markets, and some delayed flags fail on the live book. One race in flight skips markets that come close together. Today's books may differ from 2015-16, in either direction.
- **So** expect well under one decision a session, and plausibly only a few decisions by 2027-06-30. Then neither the 5-a-day nor the 10-in-all cap binds, and the verdict is INCOMPLETE. Stage 0's count gives today's rate before the build ends.

## Stages
| Stage | What | Gate | Size |
|---|---|---|---|
| PR1 (this PR) | The page, the note, this plan, the draft contract p2-lp and the G1 follow-up. Docs only, plus a small test of the draft contract. Pilot 1's documents are unchanged | Astra's review; Yu merges | M |
| PR2 (done) | The Stage 0 counter, [scripts/lp_rate_count.py](../scripts/lp_rate_count.py) (#56; amendment 1 in #57) | merged | S |
| PR3 (optional) | Gate B: a counts-only table over Pilot 1's stored decision books | only with Yu's yes, and merged before Pilot 1's first PLACE record | S |
| PR4 | The pure module pilot_lp.py: the contract loader, predicates, leg prices, passive check, two-leg record, fold, ledger, classes, value, verdict and tables | the build consent; merged only after Astra accepts Pilot 1's go-live commit | L |
| PR5 | The gateway path for `pilot_lp`: two sends under one reservation, the passive check, read-cancel-read, the suspended-market rule, virtual prices on Pilot 2's books only | merged only after Pilot 1's PT4 receipt, and after the cross-version check | L, highest scrutiny |
| PR6 | The executor and the `plp-*` commands: pre-filter, rest loop, pull, auto-kill, arming refusals | after PR5 | L |
| PR7 | Capture, the data lock and the terminal report | after PR6 | M |
| PR8 | P2-PT2's dry day and P2-PT3's probe drill mode | after PR7 | M |

Each code PR is reviewed by Codex and Astra and merged by Yu, and the full offline suite passes, including every Pilot 1 test unchanged.

## Open for Yu, at the build consent
None of these needs an answer before the build consent. The Air's Claude's proposals of 2026-11-04, made under Yu's "全部ok, follow what you think is sensible" (relayed), are shown as proposals, not as Yu's words.

| Question | The Air's Claude's proposal |
|---|---|
| Overall budget: raise it, or keep £1,000 with the account balance as the joint stop (page, Money) | raise it to £1,100 |
| Day stop: a hard ceiling (a race only if losing its full £10 still leaves the day at or above −£20), or stop once −£20 is reached | hard ceiling |
| Confirm the 2026-11-03 money numbers for the automated pilot, with "5 a day" counting PLACE decisions | confirmed as given |
| A lifetime cap of 10 decisions | yes |
| config-p2.json's £ caps (note §9) | as derived |
| Drill money: at most £1 a race and at most 8 drill races | yes |
| Final date | 2027-06-30 |
| If decisions are too rare: stop, or the lab brings the levers with their counts (deciding from −600 to −300 s, a later final date, lays above 6.0 at a smaller stake, other markets) | the levers; Yu decides then |
| Gate B (PR3) | yes, but only if it does not delay Pilot 1 |
| Reviews: Astra on the Air accepts; the lab's tool-free gpt-6-astra may pre-review, but that is not Astra's acceptance | as stated |

Also for Yu, before go-live: send the [G1 follow-up](betfair-pilot-2-g1-question.md) to Betfair, or record a decision to go ahead without an answer; check Basic 2% in My Account; and confirm the £50 website loss limit is set.

## Timeline (*Inference*)
- **November 2026:** this PR and Astra's review, after Pilot 1's review queue. The attended manual drills. Recording starts on the Air when Yu loads the recorder.
- **December:** the build consent when Yu is ready. PR4 is drafted. Pilot 1's go-live and PT4 go ahead untouched; Pilot 1's plan expects its first bet in mid-to-late December.
- **January to February 2027:** PR4 merges once Astra has accepted Pilot 1's go-live commit. After Pilot 1's PT4 receipt, PR5 to PR8 follow with Codex and Astra rounds, about 4–6 weeks, with review dominating.
- **February to March:** P2-PT2, P2-PT3, the G1 answer or Yu's recorded decision, the commission check, Astra's acceptance, Yu's go-live consent, registration and P2-PT4.
- **March or April at the earliest, to 2027-06-30:** Pilot 2 sessions on days without Pilot 1.
- Skipping Gate A does not bring this forward: the dates are set by the Pilot 1-first order and by review time. If Pilot 1 slips, Pilot 2 slips by the same amount.

## Risks
- **Too few decisions.** The likeliest outcome is a handful of decisions and an INCOMPLETE verdict.
- **Adverse selection is expected, not a tail risk.** A lone LAY filled ahead of a shortening price loses most often, and one such fill can wipe out several round trips.
- **F46.** Pre-filter false positives still mean some live-key reads with no bet. Betfair publishes no read-to-bet ratio, so the £499 key both pilots need is at some risk. The delayed pre-filter, the SKIP-streak cap and the G1 follow-up reduce it, but do not remove it.
- **G1** may not cover resting automated lays. Going live without an answer would be Yu's recorded decision.
- **Taker fills.** A leg meant to rest can still take liquidity if the book moves between its send book and its arrival. They are counted as taker fills.
- **Cancel races.** A leg filling between the read and the cancel gives an UNKNOWN by design (lifecycle.py:283-297). The second read resolves it. A residual UNKNOWN blocks all placing (R3) and triggers auto-kill.
- **No dead-man.** A crash, sleep or network cut leaves the legs resting until the off, bounded at that race's £10.
- **The shared journal.** The design relies on Pilot 1's unchanged code ignoring `pilot_lp` records (note §7). This has been read on main but not yet pinned by a test. The cross-version check pins it before PR5 merges.
- **Auto-kill stops Pilot 1 too,** until Yu types RESUME LIVE. That is safe, but it costs Pilot 1 sessions.
- **Shared order-path code.** PR5 touches gateway.py and risk.py, the only order path. Hence the merge order, Pilot 1's pinned checkout, Pilot 1's unchanged tests and the cross-version check.
- **Two configs and two worktrees in one profile.** Every mismatch refuses (PA1–PA3 fail closed), so the cost is lost sessions, not money.
- **The budget overlap** (page, Money). If the account balance is the joint bound, whichever pilot runs later can be starved by R11.
- **Commission.** The 2026 rate is unconfirmed; 2% is assumed until Yu checks it.
