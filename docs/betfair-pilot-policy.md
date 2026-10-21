# RS-19: the pilot executor

Status: proposed. Yu set the money numbers on 2026-10-21. The page takes effect through the three consents below; until Yu's go-live consent, AGENTS.md and docs/betfair-order-path.md apply as written at 416702f. No live pilot order is allowed before the go-live consent.

## What it allows
One registered strategy version may place small live bets through the existing order gateway. It runs on Yu's Air, only in sessions Yu starts and watches. AGENTS.md:9-10 and the RS-19 lines in order-path gain this one exception, which points here. The exact AGENTS.md wording is in [the engineering note](betfair-pilot-engineering.md) §1; the order-path clauses are the ones in the commit Yu approves at the build consent. No other strategy, agent, script or scheduler places live orders.

## Five principles
Each principle says what it means here and why. Where a detail is unclear, follow the principle and write down the reasoning.

**Transparency.** Every decision, bet or skip, is written down with its reason before anything is sent. Each session ends with a short plain summary. *Why:* Yu should be able to see what the code did and why, without having to trust it.

**Truth.** A strategy version is fixed before its first bet and identified by its hash. It is judged once, at its pre-set sample size, by net CLV against BSP after commission. Profit is reported at the end and never decides. Anything we cannot verify counts as a loss. Bad results are reported the same way as good ones. *Why:* a few hundred £2 bets are mostly luck. CLV is the fairest yardstick we have, and changing the rule half-way would make the answer meaningless. Counting what we cannot verify as lost means a gap in what we know never makes the money look better than it is.

**Understanding.** Before it bets, each version states what it tests and why it is worth testing. That includes any reason to think the market misprices, or a plain statement that none is known, and what we honestly expect. A version we cannot explain does not run. *Why:* an edge nobody can explain is usually noise.

**Simplicity.** One market type, one bet per race and one fixed stake. Orders are BACK, LIMIT and LAPSE, placed before the off. Nothing else. When in doubt, leave it out. *Why:* every extra moving part is one more way to lose money by accident.

**Non-forcing.** Skipping is the default. Any doubt means skip: stale data, a missed window, an interruption or an unclear result. Nothing is ever retried or resent. Daily and lifetime numbers are ceilings, never targets. Losses are never chased. A stop waits for Yu rather than pushing on. *Why:* the market always offers another race, and a forced bet is the one most likely to be wrong.

## Money (Yu's numbers, set 2026-10-21)
Every bet is a £2 BACK and there is at most one bet per race, so the most the bets can lose is the number of bets × £2.

| | Yu's number | What it means |
|---|---|---|
| Stake | £2 a bet | The most one bet can lose is £2 |
| Bets a day | at most 25 (5 until the first check, see consent 3) | The worst possible day is −£50; the code also stops at a £50 day loss |
| Bets in total | at most 495 | The worst possible total is −£990, inside Yu's £1,000 trading budget. The one-off £499 key is separate |
| Open at once | at most £20 | About ten fully matched £2 bets wait for settlement at once. Settling frees room. This sets the pace, not the worst case |
| Pause to look | at −£150, and again at each further −£150 | Placing stops, and that day's session ends, until Yu has looked and says go on. A rule with no edge gets here by chance about 1–2 times in 100 (synthetic simulation). A serious bug gets here quickly. A rule only slightly worse than the market usually never does: judging that is the CLV verdict's job |
| Betfair website loss limit | £50 a day | A backstop that sits outside our code |

- How the pause counts. The running total starts at £0 with the first bet. A settled bet counts at its verified result. A bet still waiting for its fill, or whose fill or result we cannot verify, counts at −£2. Once a checked read shows that nothing more can match, an unsettled bet counts at the part that matched (£0 if none). The levels count from £0, not from the best point reached: after some wins, or after Yu says go on, more than £150 can be lost before the next pause. There is no other loss stop; the 495 total is what bounds the worst case. Yu accepted this on 2026-10-21: the most that can be lost is what Yu put in.
- These numbers are for Pilot 1 as a whole. A restart, a code fix or a new strategy version continues the same counts and the same running total from the first bet. Nothing starts afresh, and a new budget is Yu's decision.
- 25 and 495 also let the test finish. 495 leaves room for about one bet in five not to count (unmatched, void, no BSP) on the way to 400 counted bets. 25 a day spreads them over at least 16 days, and the test needs 15. Ask for a re-check before changing either.
- Only Yu changes a money number: every number in this table, the bet counts included, and any £ figure in the strategy version or the config.
- After a pass, one replication comes first, still at £2 a bet but allowed more than 25 bets a day (Yu, 2026-10-21). Scaling up (a bigger stake, or staking in proportion to the bankroll) comes only after that. Each is a later pilot with its own strategy version, money numbers and Yu's consents.

## Who does what
- Only Yu starts a session. Yu does it in person at the Air terminal, at most once a day, on racing days Yu chooses, and stays for the whole session. *Why:* nothing else watches it, and when it stops it may print a bet for Yu to cancel by hand.
- Anyone on the Air can stop it with the KILL file or own-scope `kill`. From anywhere else, use the Betfair website.
- Every line the code asks a person to type belongs to Yu: register, arm, settle a market, accept, go on after a pause, resume, and an account-wide kill. Agents and scripts never type one and never supply a terminal for one, even when asked in chat. *Why:* a typed line is friction, not proof that a person is there. This page is what keeps those lines Yu's.
- Agents build, test, review and summarise. Live-key tests, read-only ones included, need Yu's consent with Yu present.
- No manual or Bet Angel bet is open on the account during a session. If the code finds one, it halts.
- Only Yu approves a new strategy version or changes who may type a line.

## Three consents
1. **Build.**
   - Yu sets the money numbers (done 2026-10-21). They are written into the strategy version, which is then frozen.
   - Astra reviews this page, the engineering note and the frozen version, and accepts them.
   - Yu then approves this page as committed, together with the AGENTS.md and order-path exception lines in that commit, and the frozen version by its hash.
   - The code is then built, reviewed by Astra and merged. This consent allows no live pilot order.
2. **Go live.**
   - These must be done first:
     - the first-live tests the engineering note §14 lists (T0–T3, T5, T9, T10);
     - the pilot drills (PT1–PT3);
     - the code's tests, all passing;
     - Betfair's answer on the compliance question (G1), or Yu's recorded decision to go ahead without one;
     - Astra's acceptance of the exact code commit and config hash.
   - Yu then approves going live, naming the code commit, the config hash and the strategy version, and only then registers that version.
   - Any change to one of those needs this consent again.
3. **Each session.**
   - Yu arms it.
   - Until Yu has checked a matched, settled bet against the website, each session has at most 5 bets. Yu checks each of them: matched size, price, result and money. This tests our bookkeeping, not the strategy.

## How it ends
- Placing stops at the first of these:
  - the sample is complete;
  - the bet total is reached;
  - the final date (2027-06-30) arrives;
  - Yu ends the pilot in writing (stopping one session does not end it);
  - something this page forbids happens: a bet it does not allow, a session Yu did not start, or a money number changed without Yu.
- A missed note or summary is reported and put right. It does not end the pilot.
- Stopping before the sample is complete is always allowed. Unless bets already placed complete the sample once they settle, it leaves the answer open, never a pass or a fail. A stop for something this page forbids makes the result invalid. The bet total is never raised to finish.
- After placing stops, open bets are cancelled or settled as usual, and AGENTS.md goes back to how it read at 416702f.

## Where the rest lives
- **The strategy itself is rule-based.** The exact rule, the sample size, the analysis and the measurement are in the strategy version file ([research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json)).
  - Agents choose its other numbers (those not in the Money table and not £ amounts) by these principles, before it is frozen. Each has a one-line reason in the [Pilot 1 plan](betfair-pilot-1-plan.md). Once frozen, the version changes only as a new version with Yu's consents again.
- **How the code keeps these promises** is in the [engineering note](betfair-pilot-engineering.md). It covers one decision per race, no resend, stopping before the first byte, worst-case accounting, the pause, settlement and measurement.
  - Astra reviews every change to it.
  - A change that removes or weakens a protection there also needs Yu's yes, asked in plain words. When it is unclear whether a change weakens something, ask Yu.
  - Breaking the engineering note or the strategy version counts as breaking this page.
