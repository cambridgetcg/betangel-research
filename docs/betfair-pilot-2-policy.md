# Pilot 2 (P2-LP): attended two-sided quotes

Status: proposed, for Astra's review. Nothing on this page is approved, and no Pilot 2 live order is allowed before Yu's go-live consent here. [RS-19](betfair-pilot-policy.md), Pilot 1's other documents and AGENTS.md are unchanged by this commit. The lines they would gain at Pilot 2's go-live are listed under [Changes to Pilot 1's documents](#changes-to-pilot-1s-documents-at-pilot-2s-go-live) and are not in force.

For Yu, Astra and the Air's Claude. Author: lab-Claude, 2026-11-05, against betangel-research main c86764a, after Yu's go-ahead to draft it (relayed by the Air's Claude, 2026-11-05: "可以 let lab go ahead. 得"). The design is the lab's Pilot 2 design of 2026-11-03. The [plan](betfair-pilot-2-plan.md) records what Yu has decided and what is still open.

- **Naming.** In [Pilot 1's plan](betfair-pilot-1-plan.md) (Goal and Sample), "Pilot 2" means whatever follows Pilot 1. On this page and its companions, Pilot 2 is this liquidity-provision pilot, P2-LP.
- **Companions.** The [engineering note](betfair-pilot-2-engineering.md) says how the code keeps these promises. The [plan](betfair-pilot-2-plan.md) gives the reasons for each value, the stages and the timeline. The draft contract is [research/pilots/p2-lp.json](../research/pilots/p2-lp.json), and the [G1 follow-up](betfair-pilot-2-g1-question.md) is the question for Yu to send to Betfair. This page wins on any difference.

## What it allows
One registered Pilot 2 contract may place small live orders through the existing order gateway. It runs on Yu's Air, only in sessions Yu starts and watches. In one race it sends one £2 BACK and one £2 LAY on one runner, both priced inside the spread so they wait to be filled. It cancels whatever is still unmatched 60 seconds before the off.

It runs in Pilot 1's Betfair profile and live journal, because A15 allows one profile per account. Inside them it is a separate lineage, with its own contract, records, counts, ledger, stops, config file and checkout. Nothing of Pilot 2 enters Pilot 1's 495 bets, its −£150 pause or its running total.

## Five principles
These are RS-19's five principles. Each says what it means here. Where a detail is unclear, follow the principle and write down the reasoning.

**Transparency.** Every decision, PLACE or SKIP, is written down with its reason before anything is sent. Each session ends with a short plain summary that includes how many live-key reads led to a decision. *Why:* the same as RS-19. Yu should be able to see what the code did without having to trust it.

**Truth.** The contract is fixed before the first order and identified by its hash. It is judged once, at the end, over every PLACE record, by an operations verdict fixed in advance: did every order rest, get pulled on time, and stop in an orderly way under every fault, with the day and pilot stops never passed? Value at BSP and profit are reported and never decide. Anything we cannot verify counts at its worst case. *Why:* with at most 10 decisions, money can answer nothing, but each safety fact can fail on a single event.

**Understanding.** It tests operations and live fills: whether an attended executor can post two passive £2 orders, rest them, pull them in time and stop safely, and how often each fills. No mispricing is known. The evidence leans to "we are picked off" ([lp-study](lp-study.md), Predictions): makers are about break-even after commission, and in lp-v1 the mid moved against queued orders after they filled. The honest expectation is a few pence negative per decision, very few decisions, and an answer about operations only. *Why:* the same as RS-19. Any larger liquidity step needs these operations facts first, and offline history cannot show queue position, virtual prices or latency.

**Simplicity.** One market type and one decision per race. One £2 BACK, then one £2 LAY, both LIMIT and LAPSE, before the off. They rest until a timed whole cancel at off −60 s, and a matched part rides to the result. Nothing is hedged, re-priced, replaced or retried. Every departure from RS-19's simplicity is listed under [What it relaxes](#what-it-relaxes). *Why:* every extra moving part is one more way to lose money by accident.

**Non-forcing.** Skipping is the default. Any doubt means skip, and nothing is ever resent. Day and lifetime numbers are ceilings, never targets. A stop waits for Yu. *Why:* the same as RS-19.

## Money
Yu chose the first five numbers on 2026-11-03, in a question that offered them for a hand trial; they carry over to this automated pilot. At the build consent Yu confirms them, together with each row marked *proposed*. Only Yu changes a money number.

| | Number | Whose | What it means |
|---|---|---|---|
| Stake | £2 a side: one £2 BACK and one £2 LAY on one runner | Yu, 2026-11-03 | A BACK can lose £2. A LAY at price L can lose £2 × (L − 1) |
| Highest lay price | 6.0 | Yu, 2026-11-03 | One LAY can lose at most £10 |
| Decisions a day | at most 5 a UTC day | Yu, 2026-11-03 | A ceiling, never a target. *Proposed reading:* it counts PLACE decisions, and SKIPs do not count |
| Day stop | −£20 | Yu, 2026-11-03 | *Proposed reading:* a hard ceiling. A race is allowed only if losing its full £10 still leaves the day at or above −£20, so the day ends after about £10 lost or still at risk. The other reading, "stop once −£20 is reached", would allow a −£30 day |
| Pilot stop | −£100 | Yu, 2026-11-03 | A race is allowed only if losing its full £10 still leaves the pilot at or above −£100 |
| Worst race | −£10 | follows from Yu's numbers | The LAY fully matched and its runner wins. The BACK loses only when its runner loses, so the two never add up |
| Decisions in total | at most 10 | *proposed* | 10 × £10 = £100, so −£100 holds by count whatever the ledger code does, as Pilot 1's 495 × £2 does |
| Open at once | one race with resting orders; at most £20 of worst case open | *proposed* | Any failure is bounded by that one race's £10 |
| Pause | none | *proposed* | The hard stops and the 10-decision cap replace a look-and-go-on pause. Yu may add one |
| Drill money (P2-PT3) | per drill race, a fixed probe: LAY £1.00 @ 1.01 plus BACK £1.00 @ 1000; at most 8 drill races | *proposed* | At most £1 a race and £8 in all. In practice nothing should match. Outside Pilot 2's ledger, inside the overall budget |
| Betfair website loss limit | £50 a day, unchanged | Yu, 2026-10-21 | A backstop outside the code |
| Overall budget | open | Yu decides | Pilot 1's worst case of −£990 (RS-19, Money), plus Pilot 2's −£100 and the drills' −£8, is −£1,098, which is over Yu's £1,000 |

- **The budget.** Yu decides at the build consent whether to raise the overall budget or to keep £1,000, with the account balance and no top-ups as the joint stop. In that second case, whichever pilot runs later may be starved of funds. Yu has said a higher maximum loss is acceptable ("可以上調max loss", relayed by the Air's Claude, 2026-11-04). The Air's Claude proposes £1,100. Neither is Yu's written yes. The attended manual drills of November 2026 ([plan](betfair-pilot-2-plan.md), Before Pilot 2) come out of the same account.
- **Separate from Pilot 1.** Pilot 2's ledger starts at £0. Pilot 1's slots, pause and running total are untouched, and Pilot 2's records never count in them. The option Yu chose on 2026-11-03 said so: "NOT part of Pilot 1's counts".
- **Losses pay no commission.** Commission is Basic 2% on each market's net, checked per market and never per bet, because both orders of one decision are in one market.

## Who does what
- Only Yu starts a session, in person at the Air terminal, and stays for the whole session. There is at most one pilot session a UTC day across both pilots. Pilot 2 never runs on a day with a Pilot 1 session.
- Pilot 2 arms only when no Pilot 1 bet, manual bet or Bet Angel bet of ours is open or unsettled. The Air is on mains power with the lid open, and the Betfair phone app is logged in as the stop outside the Air.
- Anyone on the Air can stop it: Ctrl-C, the KILL file, or Pilot 2's own-scope kill. From anywhere else, use the Betfair website or phone app. While orders rest, a typed `cancel` waits for the gateway lock and fails, so the session's start banner names the stops that work.
- Every line the code asks a person to type belongs to Yu, as on RS-19: register, arm, settle a market, resume and an account-wide kill. Agents and scripts never type one and never supply a terminal for one.
- Agents build, test, review and summarise. The lab may write Pilot 2's pages and code as PRs, Astra reviews and Yu merges. Yu chose "Yes, PRs only" on 2026-11-03.
- Only Yu approves a new contract or changes who may type a line.

## What it relaxes
Each item needs Yu's yes, asked in plain words, at the build consent (RS-19, Where the rest lives).

Against RS-19's rules for Pilot 1:
1. **Lays.** RS-19 allows only BACK orders.
2. **Two resting orders in one race.** RS-19 allows one bet per race.
3. **A timed cancel.** The unmatched part is cancelled at off −60 s, not straight after the place.
4. **A worst race of −£10**, set by the lay's liability, not −£2.
5. **V13 up to 3 ticks.** The send book may differ from the decision by 3 ticks, not 2. This is in Pilot 2's config only.
6. **The gateway lock is held while orders rest**, about four minutes a decision. A typed `cancel` fails during the rest. The KILL file, Pilot 2's kill and the website or app still work.
7. **The executor runs an auto-kill** after a failed cancel.
8. **A second cancel** after a cancel refused while the market was suspended.
9. **The executor sends the drill probes** in P2-PT3.

Pilot 1 protections it weakens:

10. RS-19's "No other strategy, agent, script or scheduler places live orders" and AGENTS.md's one-contract exception stop holding. There is a second automated order source on the account.
11. Pilot 1's journal holds a second lineage under its own record name. By design, that does not trigger Pilot 1's fail-closed "another contract in the journal" check, and no Pilot 1 code changes.
12. A kill from Pilot 1's checkout finds Pilot 2's orders only through a readable journal. Pilot 2's handoff therefore names Pilot 2's kill, which sweeps both pilots' order refs from code constants, and the website.
13. RS-19's "The worst possible total is −£990, inside Yu's £1,000 trading budget" stops holding unless Yu raises the budget.
14. Pilot 2's auto-kill journals a HALT that also stops Pilot 1 until Yu types RESUME LIVE. That is the safe direction, but it costs sessions.
15. Pilot 2's live-key reads that end in a SKIP add read-only-looking traffic on the £499 key both pilots need (F46).

Not weakened: Pilot 1's contract, config, caps, its one BACK per race, its immediate remainder cancel, pause, slots, PT4, verdict, data lock, handoffs and code.

## Three consents
1. **Build.**
   - Yu confirms the Money table: each *proposed* row, the day stop's reading and the overall budget. They are written into the contract, which is then frozen.
   - Astra reviews this page, the engineering note, the plan and the frozen contract, and accepts them.
   - Yu then approves this page as committed, together with the changes to Pilot 1's documents listed below, and the frozen contract by its raw-bytes sha256. Yu gives a plain-words yes to each item under What it relaxes.
   - This consent allows no live order. Gate A, the free Stage 0 count, is not a condition: Yu chose on 2026-11-04 to go on without waiting for it ([plan](betfair-pilot-2-plan.md), Decided).
   - Pilot 1 goes first (Yu, 2026-11-04). No Pilot 2 code merges into betangel_research/ before Astra has accepted Pilot 1's go-live commit. Pilot 2's order path merges only after Pilot 1's PT4 receipt exists. Every Pilot 1 test passes unchanged at every Pilot 2 commit.
2. **Go live.**
   - These must be done first:
     - the code's tests, all passing, including the cross-version check that Pilot 1's registered code folds a journal with Pilot 2 records to the identical Pilot 1 state;
     - P2-PT2, a delayed-key dry day with no order, and P2-PT3, the probe drills, each with a receipt;
     - Betfair's answer to the [G1 follow-up](betfair-pilot-2-g1-question.md), or Yu's recorded decision to go ahead without one;
     - Yu's check of Basic 2% commission in My Account and of the £50 website loss limit;
     - Astra's acceptance of the exact code commit and Pilot 2's config sha256.
   - Yu then approves going live, naming the code commit, the config sha256 and the contract sha256, and only then registers the contract.
   - Any change to one of those needs this consent again.
3. **Each session.**
   - Yu arms it.
   - Until Yu has checked a matched, settled LAY against the website, each session has at most 5 decisions (P2-PT4). Yu checks each of them: matched sizes, prices, result, per-market commission, and the lay's profit sign and basis. These decisions count in the verdict, because it judges the code on every order, and they are shown apart in the tables.

## How it ends
- Placing stops at the first of these:
  - 10 PLACE records;
  - the final date (*proposed*: 2027-06-30);
  - no room left in the pilot stop, with nothing pending;
  - Yu ends the pilot in writing (stopping one session does not end it);
  - something this page forbids happens.
- Stopping early is always allowed. It gives INCOMPLETE ("no failure in k decisions"), never a pass.
- After placing stops, open orders are cancelled or settled as usual. The verdict and the tables run once on the Air, after the data lock, and Astra recomputes them there. Nothing enters Git without Yu's approval of the exact fields.
- The lines added to Pilot 1's documents at Pilot 2's go-live are removed when Pilot 2 ends, except where Pilot 1 still needs them.

## Changes to Pilot 1's documents at Pilot 2's go-live
These are not applied in this commit. They are applied in the go-live commit that Yu approves, and are listed now so that Astra and Yu review them with this page.

- **RS-19, What it allows.** "No other strategy, agent, script or scheduler places live orders" gains "except the Pilot 2 executor under docs/betfair-pilot-2-policy.md".
- **RS-19, Who does what.** "At most once a day" is read across both pilots: at most one pilot session a UTC day, Pilot 1 or Pilot 2. "No manual or Bet Angel bet is open" becomes "No manual, Bet Angel or Pilot 2 bet is open" (stronger).
- **AGENTS.md.** The RS-19 exception covers the contracts Yu registered under RS-19 and under this page, one per pilot, each only in sessions Yu armed and attends. The live surface gains "a £2 BACK and LAY pair, LIMIT and LAPSE, resting until a timed whole cancel of the executor's own bets, sent by the Pilot 2 executor". Each pilot's lines revert when it ends.
- **docs/betfair-order-path.md.**
  - The order-source row: intent source `pilot_lp` is accepted live only through Pilot 2's path, and `api` stays paper-only.
  - The kill row: Pilot 2's kill sweeps the `p1-fav-t5`, `p2-lp` and `bar-manual` refs.
  - The live-probe section: the fixed two-sided drill probe is sent by the Pilot 2 executor in P2-PT3 only.
  - §15: a pointer to this page.
- **docs/betfair-pilot-engineering.md §2 and §7.** Pointer lines only, describing behaviour that already exists, with no Pilot 1 code change: the live journal also holds Pilot 2's records; Pilot 1's fold never reads them as its own; a Pilot 2 intent during an armed Pilot 1 session sets a review stop; and an open or unsettled Pilot 2 bet halts a Pilot 1 open.

## Where the rest lives
- **The rule, the money values, the ledger, the classes and the verdict** are in the contract, [research/pilots/p2-lp.json](../research/pilots/p2-lp.json). It stays a draft until the build consent freezes it, and after that it changes only as a new contract with Yu's consents again.
- **How the code keeps these promises** is in the [engineering note](betfair-pilot-2-engineering.md). Astra reviews every change to it. A change that removes or weakens a protection also needs Yu's yes, asked in plain words.
- **Breaking** the engineering note or the contract counts as breaking this page.
