# Pilot 1 plan: first live micro-stake pilot

Status: Proposed, together with [RS-19](betfair-pilot-policy.md) and its [engineering note](betfair-pilot-engineering.md). Yu set the money numbers on 2026-10-21; nothing is approved yet. The current rules stay in force until Yu's go-live consent (RS-19, consent 2).

For Yu and lab-Claude. Author: Nuance, 2026-10-19, revised 2026-10-21, against betangel-research main 416702f. Astra reviewed the design with the earlier policy draft in four rounds, and its findings (#1-#12, R2-1 to R2-9, R3-1 to R3-5, R4-1, R4-2) were closed there. The 2026-10-21 revision follows Yu's choice of principles over rules: RS-19 is now one page, its detail moved to the engineering note with the section numbers kept, and the drawdown episodes became the −£150 pause. Some revised parts touch areas those findings covered: the pause replaces the drawdown stops and their ACK rule (R3-4); the ledger's W rule now uses a checked current-order read; the three consents replace the old approval steps and sentences. Those parts await Astra's review.

- "RS-19" means [docs/betfair-pilot-policy.md](betfair-pilot-policy.md); "the note" means [docs/betfair-pilot-engineering.md](betfair-pilot-engineering.md); "contract p1" means [research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json).
- RS-19 holds the policy and Yu's money numbers; the note holds the engineering detail (RS-19 wins on any difference); contract p1 holds the frozen rule and analysis values. This plan points to them and keeps only its own content: the goal, why this rule, the statistics, the reason for each value, the tasks, the first-live steps and the timeline.
- *Estimate* and *Inference* mark numbers that have not been measured.
- File:line citations refer to the tree at main 416702f.

## Goal
- Find out, with real money at micro stakes, whether one rule fixed in advance takes prices that beat Betfair SP after commission (one rate, c = 2%, frozen in contract p1; note §10). Net CLV against BSP decides. Profit is reported but never decides.
- Show that an attended automated live path is safe. Measure the real CLV spread, fill rate and exclusion rates. Those numbers set the sample size for Pilot 2.
- Honest prior: no mispricing is known. P1 tests whether a price five minutes before the off beats BSP, and it mainly measures the live path. It will most likely fail. Backing at the best back price gives up about one tick of spread. At BSP 3 the price has to beat BSP by 1.36% just to break even after 2% commission (*Inference*: arithmetic from the NCLV formula below).
- Lean first version (Astra's recommendation, adopted): Yu attends every session, settlement uses the existing typed settle-market, and the efficacy result is one terminal artifact. The watchdog (P1-11), auto-settle (P1-09) and rich reports stay a later option for Yu (note §16).

## The rule: P1-FAV-T5 (chosen in advance, not fitted to data)
- In short: back the clear favourite in a GB or IE horse-racing WIN market with at least 5 runners, about five minutes before the off, at its best back price, £2 flat, when that price is 1.50 to 4.00 and the book is deep and tight. Any unmatched part is cancelled at once. No retry, re-entry, lay or second runner.
- The note §4 holds the exact predicates, the decision window and the send deadline. The note §6 holds the capacity, which uses Yu's numbers: 495 lifetime PLACE records (£990 of stake, so even a total loss stays inside £1,000), up to 25 a UTC day and 5 on each PT4 session day.
- The price is already on the ladder, so nothing is snapped (validate.py:204-205).
- Why this rule:
  - It is the simplest rule there is: back the favourite.
  - It asks one plain question: is a price five minutes before the off better than BSP?
  - Favourites have the deepest books, so we assume most orders fill near the decision price. This is an assumption; Pilot 1 measures it.
  - Short prices keep variance low.
- Alternative considered: a small-race variant. Same rule at T−10 min, limited to markets whose total matched is below a threshold fixed before the freeze. Slower and noisier.

## Where the shared rules live
| Topic | Where |
|---|---|
| Principles, Yu's money numbers, who does what, the three consents, how it ends | RS-19 |
| What it amends, with the exact wording | note §1 |
| Who starts, stops and changes it; sessions and their end margin | note §3 |
| Reservation before fetching, live-key evidence, the P1 predicates and window, the decision record, binding | note §4 |
| The first-byte gate, including the fresh journal-head check | note §5 |
| Lifetime and day capacity, durable stops, carryover across registrations and to a replacement contract | note §6 |
| Registration receipt and identity | note §7 |
| Command matrix, cleanup, the remainder cancel during a KILL, an UNKNOWN place outcome and the handoff to Yu, settle points, the settlement day and token | note §8 |
| Caps, refusals, latches and logins | note §9 |
| The ledger's one equity equation, the commission rate, cleared-figure checks, the −£150 pause and what an ACK covers | note §10 |
| Final execution record, when an L row counts, its finalization table, exact C/L agreement and the NCLV price, unmatched bets pending to the deadline, BSP cutoff, capture after sessions, measurement classes | note §11 |
| Interim allowlist, private risk view, data lock, publication | note §12 |
| The three consents in detail (freeze, build, the go-live gate, go live, REGISTER, PT4); the gate's test receipts; what PT1-PT4 and the PT4 receipt cover; adversarial tests | note §14 |
| Endings | note §15 |

## Sample and verdict (frozen in contract p1; #8-#10; R2-5, R2-6, R2-9)
- Decision order: PLACE records are numbered by the journal sequence of their reservations (note §4). That order never changes.
- Classes: each PLACE record gets exactly one measurement class from note §11, in its order of precedence. Only eligible bets enter the primary. Every other class is an exclusion, counted and reported.
- Primary sample: the first 400 eligible bets in decision order. A bet counts only once every earlier PLACE record has its final class. Placing stops there (note §15).
- Late completion: a PLACE record made before placing ended may still complete N if it is final by the data lock (note §12). Its decision came before any outcome was known, and its place in the order is fixed. So an ending at the cap with 395 eligible and ten pending can still reach 400.
- Surplus: PLACE records after the 400th eligible one, in decision order. They are finalised, counted and reported, but stay outside the primary and the validity gates.
- Analysis population for the gates: if N is reached, the PLACE records up to and including the 400th eligible one; otherwise every PLACE record. PT4 records reported apart (below) are in neither.
- PT4 (note §14): its PLACE records (from every PT4 session, if PT4 repeats) belong to the sample if the first session after the PT4 receipt runs under PT4's registration receipt. Otherwise they are reported apart, still use their slots, and the sample starts after them. A contract change after PT4 means a replacement contract with carryover (note §6). This rule is frozen before PT4's first bet.
- Primary estimand: the mean NCLV over eligible bets: matched, in markets that were not void, with no post-decision removal, a resolved measurement and a frozen BSP. It is conditional on filling.
  - NCLV = (1 + (P − 1) × (1 − c)) / BSP − 1. P is the final execution record's price (note §11: L's priceMatched when C and L agree exactly, else that of the one source used), BSP the frozen value (note §11), and c the one commission rate contract p1 freezes for the ledger too (note §10): 2%, so 1 − c = 0.98.
  - Partial fills are eligible and unweighted. NCLV depends on the price, not the size.
  - A positive mean is a commission-adjusted price advantage over BSP, assuming 1/BSP is a calibrated win probability for this population. It is not proof of positive expected profit. It is also a stricter test than beating a BSP back bet that pays the same commission.
- Validity gates, checked once, at the data lock, on the analysis population:
  - fill rate = bets with final matched size above 0 ÷ sent bets whose final matched size is resolved; at least 80%;
  - missing_bsp ÷ (missing_bsp + eligible); at most 2%;
  - measurement_unresolved ÷ sent bets (every PLACE record except definitely_unsent); at most 1%;
  - a zero denominator means there is no eligible bet. That gate is then not evaluated, and the result is INCONCLUSIVE: insufficient_eligible_sample unless INVALID applies.
- Order of results: INVALID first (a failed gate or a protocol violation). Then INCONCLUSIVE: insufficient_eligible_sample, when fewer than 400 eligible bets exist at the data lock. Then the test below.
- Clustered SE. Cluster = the UTC date of the decision time. G = the number of dates with at least one sample bet; days with attempts can be more. Mean m = Σx_i / N. CR1: SE² = G/(G−1) × Σ_g [Σ_{i in g} (x_i − m)]² / N².
- Critical value: t = the 97.5% point of Student's t with G − 1 degrees of freedom (one-sided α = 0.025). That is about 2.131 at G = 16, 2.120 at G = 17 and 2.021 at G = 41.
- PASS if m ≥ t × SE. FAIL if m + t × SE < 0. Anything else is INCONCLUSIVE.
- If G < 15, the result is INCONCLUSIVE: too_few_clusters. With Yu's day limit of 25, 400 eligible bets span at least 16 dates. That holds even when more days have attempts: if all five PT4 bets are excluded and sixteen days then give 25 eligible bets each, G = 16 over 17 attempt-days. So the guard only catches errors. If SE is zero or undefined, the result is INCONCLUSIVE: se_undefined.
- CR1 with t(G−1) is a common minimum correction. It does not give exact coverage with few clusters (Cameron and Miller 2015, §VI).
- Fewer than N eligible bets at the data lock gives INCONCLUSIVE: insufficient_eligible_sample, unless INVALID applies. That covers an ending at the lifetime cap, Yu ending the pilot in writing, or the final date 2027-06-30. An ending for a breach of RS-19 is a protocol violation, so INVALID. There is never an early PASS or a smaller-N test, and the cap is never raised to rescue completion.
- 400 eligible bets from 495 PLACE records need at least 80.8% eligible yield. An 80% fill rate alone gives only 396, so a weak fill rate ends INCONCLUSIVE by design. The historical 49.5% share of markets with any non-runner (from the P-003 review, which is not in this repository) does not estimate post-decision removals; Pilot 1 measures them.
- Power, as an IID illustration only: with t ≈ 2 and 80% power, the smallest detectable mean is (2 + 0.84) × SD / √400. That is about 0.71% at SD 0.05, 1.42% at 0.10 and 2.84% at 0.20.
  - The SD is an assumption, not a measurement (Nuance's staking research of 2026-10-19, which is not in this repository). Bets on the same day may move together, which widens these figures.
  - N = 400 is a pilot choice. It cannot promise to detect a small edge. A 2% raw CLV is only +0.63% net at BSP 3 (*Inference*: the same arithmetic).
- Reported at the end, never deciding:
  - the stake-weighted mean, Σ s_i·x_i / Σ s_i over the primary bets (s_i = final matched size), and the count of every class (attrition);
  - the decision-price series: NCLV at the decision price for every PLACE record in the same decision range, sent or not, filled or not, with its own exclusions (void, post-decision removal, missing BSP). It shows fill selection; it does not remove it from the primary;
  - surplus bets; raw CLV by price band and country; BSP calibration by band; profit after commission and the running total, as plain tables.
- Afterwards: scaling up (a bigger stake, or staking in proportion to the bankroll) comes once a strategy has shown an edge, in a later pilot with its own consents (RS-19, Money). Nuance recommends that a PASS first leads to one replication contract at the same flat stake, with no Kelly yet, because one pass at α = 0.025 can still be luck; whether that is needed is Yu's decision. A FAIL ends this rule. Either way, the measured SD sets N for Pilot 2.

## Values and why
Money numbers are Yu's (RS-19, Money): the first four rows are those and the £ caps that follow from them, and only Yu changes them. Every other number was chosen by the principles before the freeze, with Astra's review; this is its reason. After the freeze, a value in contract p1 changes only through a replacement contract with the build and go-live consents again (note §6, §14); a config value only through a new registration receipt, Astra's acceptance and the go-live consent again (note §7); a value held only in the note with Astra's review, plus Yu's yes if it weakens a protection (RS-19, Where the rest lives).

| Value | Number | Why |
|---|---|---|
| Stake; bets a day; bets in total; open at once; pause; website loss limit | £2; 25 (5 until the first check); 495; £20; −£150 and each further −£150; £50 a day | Yu's numbers, set 2026-10-21 |
| max_daily_loss | £50 | the code's own stop at Yu's worst day (25 × £2) |
| Liability and market exposure caps | £2 each | follow from one £2 BACK per market |
| External exposure tolerance | £0 | no manual or Bet Angel bet may be open during a session (RS-19); any outside exposure halts |
| customer_strategy_ref | p1-fav-t5 | names the version on every order, so a kill on an untrusted journal still finds pilot bets (note §7) |
| Commission rate c | 2% | Betfair's rate on Yu's account; one rate for the ledger and the metric |
| Market | GB or IE horse-racing WIN, at least 5 active runners | the deepest, most regular markets we can reach; small fields have thin books and odd favourites |
| Decision time | off − 300 s | the rule's question is about five minutes before the off |
| Decision window | 15 s | long enough for one fetch after reserving, short enough to stay "five minutes before" |
| Send deadline | off − 240 s | the order goes while the price is still a T−5 price; a later send asks a different question |
| Best back range | 1.50 to 4.00 | short enough to keep variance low; set in advance, not fitted to data |
| Depth and spread | at least £10 at the best back; best lay within 2 ticks | our £2 should fill at the decision price, in a book that has settled |
| No new reservation before the session end | 6 min | a decision (5 min before the off) plus a margin never runs past the end |
| Orders a minute, an hour | 2, 12 | above what one bet per race needs; they only catch a runaway loop |
| max_minutes_before_off | 10 | covers T−5 with room; nothing earlier is ever needed |
| max_price_deviation_ticks (V13) | 2 | the send book may move a little from the decision, never far |
| max_book_age_s | 2.0 s | the send book must be fresh |
| Wall/monotonic drift at the first byte | 2 s | more means a sleep or clock jump, which is a reason to skip |
| Wait for a kill outcome before the handoff | 30 s | just under the kill's last re-sweep at T0 + 35 s; after it, Yu takes over |
| No decision due before a settle point | 3 min | settling never crowds a decision |
| N | 400 eligible bets | about the most that fits inside 495 records (£990) at an 81% eligible yield; it detects only a large net edge (about 1.4% at an assumed SD of 0.10, power above), so a small real edge will most likely come out INCONCLUSIVE |
| One-sided α | 0.025 | the usual 5% two-sided bar, applied to the side that matters |
| Minimum clusters G | 15 | CR1 needs enough days; 25 a day gives at least 16 |
| Fill rate; missing BSP; measurement_unresolved | at least 80%; at most 2%; at most 1% | below or above these, the bets we measured are not a fair picture of the rule, or our measurement is the story |
| Finalization deadline | off + 7 days | long enough for settlement and cleared rows, short enough to finish |
| BSP cutoff | off + 24 h | BSP is known minutes after the off; a day allows for a late CLOSED read |
| First pilot-capture after a session | the same evening | current-order rows leave once markets settle (F77) |
| Final date | 2027-06-30 | time for 400 bets at a realistic pace (Timeline), with a firm end |
| PT4's bets join the sample | if the first session after the PT4 receipt runs under PT4's registration receipt | then the same code and config placed them, so they belong to the same experiment |

## Architecture (the Air only)
- Everything runs on the Air from a clean checkout matching the registration receipt. The live key and credentials stay in Keychain. The journal, raw responses, ledger and terminal artifact stay in the state root, outside Git. Nothing goes to the lab.
- pilot.py is a new module, stdlib-only and pure: no clock, file or network. It holds the P1 predicates, the decision record, the measurement classes, NCLV, the ledger fold with the pause, and the CR1 analysis. A boundary test keeps it from importing gateway, api, transport, auth or credentials.
- Three new CLI commands: `pilot-register`, `pilot-run` and the read-only `pilot-capture`. Credentials are read once per session; today cli.py:562 reads them on every command. cli.py stays the only module that imports the gateway (tests/test_betfair_boundaries.py:280-286).
- Pilot records are `note` events, so the database schema does not change (journal.py:121). INTENT_SOURCES gains "pilot" (journal.py:97). SETTLE_BASES is unchanged in v1 (journal.py:84). The note §6 sets the fold rules.

## First-live tests and what Yu does in person
Every test needs Yu's explicit consent, with Yu present at the terminal (facts.md:272; order-path:615). Yu types the live confirmation lines. Nuance may run the read-only tests (T1, T2, T5 and the T10 reads). T9, settle-market, pilot-register and pilot-run need a real tty (A11; arming.py:38), so they run in Yu's own terminal; an agent never supplies a pty. Already done on 2026-10-13 with the delayed key: T1, T4, T6, T7 and part of T8 (facts.md:293).
1. Yu bought the £499 key; it was activated on 2026-10-20. The key needs a KYC-verified, funded account, and read-only use is not allowed (F46, facts.md:118). Yu funds the account close to the budget, sets the deposit and loss limits on the website, and signs Bet Angel out of the Air.
2. T0 Keychain dialogs; T1 live login; T2 keepAlive; T3 whether the token survives a website login, then set coexistence; T5 isMarketDataDelayed is false on the live key (A18).
3. T9 live-probe, in Yu's own terminal: Yu types `PROBE <market> <selection> LAY 1.01 1.00`. It places a fixed LAY £1.00 @ 1.01 (worst loss £0.01), cancels it at once and sets A19 (order-path:604-611). It is a separate typed probe, not a pilot order.
4. T10: after the probe's market settles, how long it stays CLOSED and whether bets leave listCurrentOrders (F36/F77; order-path:613). The capture and settlement rhythm depends on it.
5. PT1 (note §14) uses the account's existing cleared bets if listClearedOrders returns any (*Inference*). Otherwise its matched-bet checks move to PT4, as the note says.
6. In person, in RS-19's order: the build consent; the go-live consent; `REGISTER PILOT <contract12> <code12>` (note §3); each day's arming line; every SETTLE MARKET line; pilot-capture after each session; the PT4 checks (note §14); checking one winning market's commission against the website, in PT4 if one wins, else at the first later winner (note §10); "go on" after a pause (PILOT ACK) and any RESUME LIVE; and, whenever the executor prints a handoff, an own-scope kill or a cancel on the website (note §8).

## Tasks (dependency order)
"Needs" refers to RS-19's consents. — = nothing new. build = can be written now, merged only after the build consent. build+ = started and merged after the build consent. go-live = after the go-live consent and registration.
| id | owner | title | needs | estimate |
|---|---|---|---|---|
| P1-01 | Nuance | RS-19, the engineering note and this plan; redlines (AGENTS.md:9-10; order-path §2, §15 and the note §1 lines); contract p1 text with the exact C/L agreement rule and finalization table, the commission rate, the pause and the PT4 rule; registration receipt format | — | 1-2 days |
| P1-02 | Astra | Adversarial review of RS-19, the note and the p1 method | — | 2-4 days per round |
| P1-03 | Yu (consent, confirmation lines; T0, T3 and T9 in Yu's own terminal); Nuance runs the read-only tests | First-live tests T0-T3, T5, T9, T10; receipts per facts.md:274-276 | — | 2 sittings + settle wait |
| P1-04 | Nuance | PT1 as the note §14 defines it, and its receipt with C and L as recorded (evidence under the exact rule; it chooses no tolerance); a new fact row (F78). Delayed key first if it returns SP data (*Inference*); the live recheck is part of PT4 | — | 1-2 days + 1 race afternoon |
| P1-05 | Nuance | Contract p1 JSON; pure pilot.py (predicates, record, classes, NCLV, ledger fold with the pause and the pinned examples, CR1 analysis, analysis population); offline tests incl. dead heat, void, fractional averages, partial fills, zero denominators; import-boundary test | build | 4-5 days |
| P1-06 | Nuance | Gateway: source "pilot", PA1-PA4 and exact identity for placement only; the note §4 protocol (reserve before fetch, one live-key catalogue row and decision book, one classification with atomic capacity, SKIP(interrupted)); R13 binding; the note §5 gate with the journal-head check; durable stops and carryover; orders.pilot config keys (unknown keys refused, config.py:111-112) | build+ | 2-3 weeks with review |
| P1-07 | Astra | The adversarial tests listed in the note §14 | build | 3-4 days |
| P1-08 | Nuance | `pilot-register`, attended `pilot-run` and `pilot-capture`: session credentials, per-action open and close, immediate remainder cancel with the KILL cases and handoff (note §8), scheduler, end margin, settle points, in-session capture, stop handling | build+ | 1 week |
| P1-09 | deferred | Auto-settle (E2): a later option for Yu, with its own review and approval | later | 3-5 days if chosen |
| P1-10 | Nuance | Final execution record (C and L, the finalization table, exact agreement), BSP capture, measurement classes, ledger and the pause, data lock, terminal artifact, report projection test; cancel-race and reduction tests | build+ | 4-5 days |
| P1-11 | deferred | Watchdog and PA5: a later option for Yu, with its own review and approval | later | 3-4 days if chosen |
| P1-12 | Astra | Code review rounds on P1-05 to P1-08 and P1-10; acceptance of the exact code commit and config sha256 | — | 2-3 weeks elapsed |
| P1-13 | Nuance | PT2 and PT3, as the note §14 defines them | build+ | 3-4 days |
| P1-14 | Yu present; Nuance supports | PT4 and its receipt, as the note §14 defines them; repeated if no bet matches and settles | go-live | 1 racing day |
| P1-15 | Yu runs sessions; Nuance supports; Astra recomputes | Run to N = 400 or an ending; data lock; terminal analysis; verdict | go-live and the PT4 receipt | see Timeline |

## Decided
- Yu, 2026-10-21: the money numbers on RS-19, all as recommended: £2 a bet; at most 25 bets a day (5 until the first check); at most 495 in total; at most £20 open at once; a pause at −£150 and at each further −£150; a Betfair website loss limit of £50 a day. Yu kept the daily count for Pilot 1 because the test needs at least 15 different days; scaling up waits until a strategy has shown an edge.
- Chosen by the principles (the reasons for their numbers are in "Values and why"):
  1. Consents: the three on RS-19 (build, go live, each session).
  2. First rule: P1-FAV-T5, the simplest rule with one plain question. Considered instead: the small-race variant, or a T−2 version (deeper book, smaller SD).
  3. Build: the lean attended version first. Auto-settle (P1-09) and the watchdog (P1-11) wait (note §16).
  4. Sample: N = 400 eligible bets within 495 PLACE records, final date 2027-06-30; a shortfall is INCONCLUSIVE.
  5. Settlement: typed SETTLE MARKET inside pilot-run at settle points (note §8).
  6. Capture: pilot-capture the same evening, then daily (note §11).
  7. PT4: its bets join the sample if the first session after the PT4 receipt runs under PT4's registration receipt.
  8. Accounting: dead heats, voids, removals and unknowns stay at worst case for all of Pilot 1; a cleared-figure gap above 1p is a review stop from the first bet (note §10).
  9. Funds: an R11 refusal ends the session with a review stop (note §9).
  10. Sessions: at most one arming line a racing day, ending at UNTIL and before 00:00Z, with no new reservation in the last 6 min and Yu present throughout.
  11. Keychain: if T0 shows dialogs, Yu approves the read once per session; v1 is attended anyway.
  12. Betfair loss limit: Yu's £50 a day. Its day boundary and whether it counts open liability are unverified (*Inference*; order-path:626 says only that the API cannot read the limits and that LOSS_LIMIT_EXCEEDED fails and HALTs). If it trips, the pilot stops, which is the safe direction.
  13. Account: no manual or Bet Angel bets during a session, with tolerance at £0.
  14. Compliance: send the G1 question to Betfair before go-live. Going ahead without an answer would be Yu's recorded decision (RS-19, consent 2).
  15. Reports: interim counts and codes only; the terminal artifact stays on the Air; a reports/ summary only with fields Yu approves.

## Timeline (*Inference*)
- Now to early November: the key is active (2026-10-20); RS-19 through the freeze, Astra's check and Yu's build consent; T0-T10; PT1.
- November to early December: after the build consent, build P1-05 to P1-08 and P1-10, with Astra's review rounds. Deferring the watchdog and auto-settle removes the earlier draft's 6-9 author days of that work (Astra's estimate). That is gross removed work, not a net saving after the added gates, capture and tests; review time dominates. For comparison, the P-003 review (not in this repository) put the simpler manual live path (L4) alone at 4-8+ weeks.
- December: PT2 and PT3, Astra's acceptance, the go-live consent, then PT4.
- First pilot bet: early-to-mid December 2026 at the earliest; mid-to-late December is realistic, about 8-10 weeks from 2026-10-19. Both are estimates, not review-verified delivery dates.
- Capacity, from Astra's arithmetic. These are illustrations, not completion guarantees:

| Assumptions | Sessions or days with PLACE records |
|---|---|
| Five PLACE records on day 1, then up to 25 a day; every one eligible | 1 + ceil((400 − 5)/25) = 17 |
| Five on day 1, then ten fully matched £2 bets a day, settled before midnight; every bet eligible | 1 + ceil((400 − 5)/10) = 41 |
| Same caps, an assumed 85% eligible yield (*Estimate*) | about 20 / 48, from 1 + ceil((400/0.85 − 5)/cap) |
| Four sessions a week, at 20-48 sessions | about 5-12 weeks |

- Ten a day is the fully matched £2 scenario, not a bound. £20 of total exposure holds ten fully matched £2 bets, but a partial fill whose remainder is cancelled holds less, so more markets can be open (lifecycle.py:277-285; risk.py:68-74, :107-112). R10 may allow fewer. The settlement day and token rules are in the note §8.
- R10, scarce qualifying markets, partial fills, failed attempts, missing observations and Yu's attendance all change the rate. Qualifying bets per session are not measured. In 2025 there were about 36.5 GB/IE WIN markets a day (P-003 RS-21 counts, not in this repository). An attended session sees only part of the card, and the share that passes the filters is unknown.
- From a mid-to-late December start, 5-12 weeks is roughly mid-January to late March. February to April is a planning allowance for slower qualification or missed sessions, not a direct result of those numbers.
- The final date 2027-06-30 ends placing. The data lock then waits up to 7 days more for pending classes (note §12).
- A fully unmatched bet has no L row, so its measurement stays pending until its 7-day deadline (note §11); the ledger already counts it at £0 from its EXECUTION_COMPLETE read (note §10). N can therefore be found up to about a week after the 400th eligible bet is placed. PLACE records made in that week are surplus: they use slots and stake, but stay outside the primary.
