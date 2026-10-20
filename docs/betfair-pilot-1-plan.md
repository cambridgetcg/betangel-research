# Pilot 1 plan: first live micro-stake pilot

Status: Proposed, together with the [RS-19 policy](betfair-pilot-policy.md). Nothing here is approved. The current rules stay in force until the approvals in Policy §14.

For Yu and lab-Claude. Author: Nuance, 2026-10-19, against betangel-research main 416702f. Astra reviewed this plan with the policy in four rounds. Round 4 accepted the design with two nits (R4-1, R4-2); both are applied here and await Astra's check. The earlier findings (#1-#12, R2-1 to R2-9, R3-1 to R3-5) stay closed.

- "Policy" means [docs/betfair-pilot-policy.md](betfair-pilot-policy.md); "contract p1" means [research/pilots/p1-fav-t5.json](../research/pilots/p1-fav-t5.json).
- The policy is the single source for every shared rule. This plan points to its sections and keeps only its own content: the goal, why this rule, the statistics, the tasks, the first-live steps, Yu's decisions and the timeline.
- *Estimate* and *Inference* mark numbers that have not been measured.
- A value marked "(Yu to confirm)" is Nuance's recommendation; Yu confirms or changes it before the build approval (Policy §14 step 1). The policy's appendix lists them all.
- File:line citations refer to the tree at main 416702f.

## Goal
- Find out, with real money at micro stakes, whether one rule fixed in advance takes prices that beat Betfair SP after commission (one rate, c = 2% (Yu to confirm), frozen in contract p1; Policy §10). Net CLV against BSP decides. Profit is reported but never decides.
- Show that an attended automated live path is safe. Measure the real CLV spread, fill rate and exclusion rates. Those numbers set the sample size for Pilot 2.
- Honest prior: this rule will most likely fail. Backing at the best back price gives up about one tick of spread. At BSP 3 the price has to beat BSP by 1.36% just to break even after 2% commission (*Inference*: arithmetic from the NCLV formula below).
- Lean first version (Astra's recommendation, adopted): Yu attends every session, settlement uses the existing typed settle-market, and the efficacy result is one terminal artifact. The watchdog (P1-11), auto-settle (P1-09) and rich reports stay a later option for Yu (Policy §16).

## The rule: P1-FAV-T5 (chosen in advance, not fitted to data)
- In short: back the clear favourite in a GB or IE horse-racing WIN market with at least 5 runners, about five minutes before the off, at its best back price, £2 flat, when that price is 1.50 to 4.00 and the book is deep and tight. Any unmatched part is cancelled at once. No retry, re-entry, lay or second runner.
- Policy §4 holds the exact predicates, the decision window and the send deadline. Policy §6 holds the capacity: 495 (Yu to confirm) lifetime PLACE records (£990 of stake, so even a total loss stays under £1,000), up to 25 (Yu to confirm) a UTC day and 5 (Yu to confirm) on each PT4 session day.
- The price is already on the ladder, so nothing is snapped (validate.py:204-205).
- Why this rule:
  - It is the simplest family-5 rule (back the favourite).
  - It also asks family 2's question: is a price 5 minutes before the off better than BSP?
  - Favourites have the deepest books, so we assume most orders fill near the decision price. This is an assumption; Pilot 1 measures it.
  - Short prices keep variance low.
- Alternative for Yu: the family-2 small-race variant. Same rule at T-10 min, limited to markets whose total matched is below a threshold fixed before the freeze. Slower and noisier.

## Where the shared rules live
| Topic | Policy |
|---|---|
| Who starts, stops and changes it; sessions and their end margin | §3 |
| Reservation before fetching, live-key evidence, the P1 predicates and window, the decision record, binding | §4 |
| The first-byte gate, including the fresh journal-head check | §5 |
| Lifetime and day capacity, durable stops, carryover across registrations and to a replacement contract | §6 |
| Registration receipt and identity | §7 |
| Command matrix, cleanup, the remainder cancel during a KILL, an UNKNOWN place outcome and the handoff to Yu, settle points, the settlement day and token | §8 |
| Caps, refusals, latches and logins | §9 |
| The ledger's one equity equation, the commission rate, cleared-figure checks, drawdown stops, what an ACK covers and when a stop re-arms | §10 |
| Final execution record, when an L row counts, its finalization table, exact C/L agreement and the NCLV price, unmatched bets pending to the deadline, BSP cutoff, capture after sessions, measurement classes | §11 |
| Interim allowlist, private risk view, data lock, publication | §12 |
| Approvals and the order of steps (freeze, build, gate, activation, REGISTER, PT4); the gate's test receipts; what PT1-PT4 and the PT4 receipt cover; adversarial tests | §14 |
| Endings | §15 |

## Sample and verdict (frozen in contract p1; #8-#10; R2-5, R2-6, R2-9)
- Decision order: PLACE records are numbered by the journal sequence of their reservations (Policy §4). That order never changes.
- Classes: each PLACE record gets exactly one measurement class from Policy §11, in its order of precedence. Only eligible bets enter the primary. Every other class is an exclusion, counted and reported.
- Primary sample: the first 400 (Yu to confirm) eligible bets in decision order. A bet counts only once every earlier PLACE record has its final class. Placing stops there (Policy §15).
- Late completion: a PLACE record made before placing ended may still complete N if it is final by the data lock (Policy §12). Its decision came before any outcome was known, and its place in the order is fixed. So an ending at the cap with 395 eligible and ten pending can still reach 400.
- Surplus: PLACE records after the 400th eligible one, in decision order. They are finalised, counted and reported, but stay outside the primary and the validity gates.
- Analysis population for the gates: if N is reached, the PLACE records up to and including the 400th eligible one; otherwise every PLACE record. PT4 records reported apart (below) are in neither.
- PT4 (Policy §14 step 5): its PLACE records (from every PT4 session, if PT4 repeats) belong to the sample if the first session after the PT4 receipt runs under PT4's registration receipt (recommended; Yu to confirm). Otherwise they are reported apart, still use their slots, and the sample starts after them. A contract change after PT4 means a replacement contract with carryover (Policy §6). This rule is frozen before PT4's first bet.
- Primary estimand: the mean NCLV over eligible bets: matched, in markets that were not void, with no post-decision removal, a resolved measurement and a frozen BSP. It is conditional on filling.
  - NCLV = (1 + (P − 1) × (1 − c)) / BSP − 1. P is the final execution record's price (Policy §11: L's priceMatched when C and L agree exactly, else that of the one source used), BSP the frozen value (Policy §11), and c the one commission rate contract p1 freezes for the ledger too (Policy §10): 2% (Yu to confirm), so 1 − c = 0.98.
  - Partial fills are eligible and unweighted. NCLV depends on the price, not the size.
  - A positive mean is a commission-adjusted price advantage over BSP, assuming 1/BSP is a calibrated win probability for this population. It is not proof of positive expected profit. It is also a stricter test than beating a BSP back bet that pays the same commission.
- Validity gates, checked once, at the data lock, on the analysis population:
  - fill rate = bets with final matched size above 0 ÷ sent bets whose final matched size is resolved; at least 80%;
  - missing_bsp ÷ (missing_bsp + eligible); at most 2%;
  - measurement_unresolved ÷ sent bets (every PLACE record except definitely_unsent); at most 1% (Yu to confirm);
  - a zero denominator means there is no eligible bet. That gate is then not evaluated, and the result is INCONCLUSIVE: insufficient_eligible_sample unless INVALID applies.
- Order of results: INVALID first (a failed gate or a protocol violation). Then INCONCLUSIVE: insufficient_eligible_sample, when fewer than 400 eligible bets exist at the data lock. Then the test below.
- Clustered SE. Cluster = the UTC date of the decision time. G = the number of dates with at least one sample bet; days with attempts can be more. Mean m = Σx_i / N. CR1: SE² = G/(G−1) × Σ_g [Σ_{i in g} (x_i − m)]² / N².
- Critical value: t = the 97.5% point of Student's t with G − 1 degrees of freedom (one-sided α = 0.025). That is about 2.131 at G = 16, 2.120 at G = 17 and 2.021 at G = 41.
- PASS if m ≥ t × SE. FAIL if m + t × SE < 0. Anything else is INCONCLUSIVE.
- If G < 15 (Yu to confirm), the result is INCONCLUSIVE: too_few_clusters. With a day limit of 25 (Yu to confirm), 400 eligible bets span at least 16 dates. That holds even when more days have attempts: if all five PT4 bets are excluded and sixteen days then give 25 eligible bets each, G = 16 over 17 attempt-days. So the guard only catches errors. If SE is zero or undefined, the result is INCONCLUSIVE: se_undefined.
- CR1 with t(G−1) is a common minimum correction. It does not give exact coverage with few clusters (Cameron and Miller 2015, §VI).
- Fewer than N eligible bets at the data lock gives INCONCLUSIVE: insufficient_eligible_sample, unless INVALID applies. That covers an ending at the lifetime cap, a terminal safety stop, Yu abandoning it, or the final date 2027-06-30 (Yu to confirm). There is never an early PASS or a smaller-N test, and the cap is never raised to rescue completion.
- 400 eligible bets from 495 PLACE records need at least 80.8% eligible yield. An 80% fill rate alone gives only 396, so a weak fill rate ends INCONCLUSIVE by design. The historical 49.5% share of markets with any non-runner (from the P-003 review, which is not in this repository) does not estimate post-decision removals; Pilot 1 measures them.
- Power, as an IID illustration only: with t ≈ 2 and 80% power, the smallest detectable mean is (2 + 0.84) × SD / √400. That is about 0.71% at SD 0.05, 1.42% at 0.10 and 2.84% at 0.20.
  - The SD is an assumption, not a measurement (Nuance's staking research of 2026-10-19, which is not in this repository). Bets on the same day may move together, which widens these figures.
  - N = 400 is a pilot choice. It cannot promise to detect a small edge. A 2% raw CLV is only +0.63% net at BSP 3 (*Inference*: the same arithmetic).
- Reported at the end, never deciding:
  - the stake-weighted mean, Σ s_i·x_i / Σ s_i over the primary bets (s_i = final matched size), and the count of every class (attrition);
  - the decision-price series: NCLV at the decision price for every PLACE record in the same decision range, sent or not, filled or not, with its own exclusions (void, post-decision removal, missing BSP). It shows fill selection; it does not remove it from the primary;
  - surplus bets; raw CLV by price band and country; BSP calibration by band; profit after commission and the drawdown path, as plain tables.
- Afterwards: a PASS leads to a replication contract at the same flat stake, with no Kelly yet. A FAIL ends this rule. Either way, the measured SD sets N for Pilot 2.

## Architecture (the Air only)
- Everything runs on the Air from a clean checkout matching the registration receipt. The live key and credentials stay in Keychain. The journal, raw responses, ledger and terminal artifact stay in the state root, outside Git. Nothing goes to the lab.
- pilot.py is a new module, stdlib-only and pure: no clock, file or network. It holds the P1 predicates, the decision record, the measurement classes, NCLV, the ledger fold and the CR1 analysis. A boundary test keeps it from importing gateway, api, transport, auth or credentials.
- Three new CLI commands: `pilot-register`, `pilot-run` and the read-only `pilot-capture`. Credentials are read once per session; today cli.py:562 reads them on every command. cli.py stays the only module that imports the gateway (tests/test_betfair_boundaries.py:280-286).
- Pilot records are `note` events, so the database schema does not change (journal.py:121). INTENT_SOURCES gains "pilot" (journal.py:97). SETTLE_BASES is unchanged in v1 (journal.py:84). Policy §6 sets the fold rules.

## First-live tests and what Yu does in person
Every test needs Yu's explicit consent, with Yu present at the terminal (facts.md:272; order-path:615). Yu types the live confirmation lines. Nuance may run the read-only tests (T1, T2, T5 and the T10 reads). T9, settle-market, pilot-register and pilot-run need a real tty (A11; arming.py:38), so they run in Yu's own terminal; an agent never supplies a pty. Already done on 2026-10-13 with the delayed key: T1, T4, T6, T7 and part of T8 (facts.md:293).
1. Yu buys and activates the £499 key. The key needs a KYC-verified, funded account, and read-only use is not allowed (F46, facts.md:118). Yu funds the account close to the budget, sets the deposit and loss limits on the website, and signs Bet Angel out of the Air.
2. T0 Keychain dialogs; T1 live login; T2 keepAlive; T3 whether the token survives a website login, then set coexistence; T5 isMarketDataDelayed is false on the live key (A18).
3. T9 live-probe, in Yu's own terminal: Yu types `PROBE <market> <selection> LAY 1.01 1.00`. It places a fixed LAY £1.00 @ 1.01 (worst loss £0.01), cancels it at once and sets A19 (order-path:604-611). It is a separate typed probe, not a pilot order.
4. T10: after the probe's market settles, how long it stays CLOSED and whether bets leave listCurrentOrders (F36/F77; order-path:613). The capture and settlement rhythm depends on it.
5. PT1 (Policy §14 step 2) uses the account's existing cleared bets if listClearedOrders returns any (*Inference*). Otherwise its matched-bet checks move to PT4, as the policy says.
6. In person, in the Policy §14 order: the build approval; the activation approval; `REGISTER PILOT <contract12> <code12>` (Policy §3); each day's arming line; every SETTLE MARKET line; pilot-capture after each session; the PT4 checks (Policy §14 step 5); checking one winning market's commission against the website, in PT4 if one wins, else at the first later winner (Policy §10); any PILOT ACK or RESUME LIVE; and, whenever the executor prints a handoff, an own-scope kill or a cancel on the website (Policy §8).

## Tasks (dependency order)
"Needs" refers to Policy §14. — = nothing new. build = can be written now, merged only after the build approval (step 1). build+ = started and merged after the build approval. activation = after steps 2 to 4.
| id | owner | title | needs | estimate |
|---|---|---|---|---|
| P1-01 | Nuance | RS-19 and this plan; redlines (AGENTS.md:9-10; order-path §2, §15 and the Policy §1 lines); contract p1 text with the exact C/L agreement rule and finalization table, the commission rate and the PT4 rule; registration receipt format | — | 1-2 days |
| P1-02 | Astra | Adversarial review of RS-19 and the p1 method | — | 2-4 days per round |
| P1-03 | Yu (consent, confirmation lines; T0, T3 and T9 in Yu's own terminal); Nuance runs the read-only tests | First-live tests T0-T3, T5, T9, T10; receipts per facts.md:274-276 | — | 2 sittings + settle wait |
| P1-04 | Nuance | PT1 as Policy §14 step 2 defines it, and its receipt with C and L as recorded (evidence under the exact rule; it chooses no tolerance); a new fact row (F78). Delayed key first if it returns SP data (*Inference*); the live recheck is part of PT4 | — | 1-2 days + 1 race afternoon |
| P1-05 | Nuance | Contract p1 JSON; pure pilot.py (predicates, record, classes, NCLV, ledger fold with the pinned examples, CR1 analysis, analysis population); offline tests incl. dead heat, void, fractional averages, partial fills, zero denominators; import-boundary test | build | 4-5 days |
| P1-06 | Nuance | Gateway: source "pilot", PA1-PA4 and exact identity for placement only; the Policy §4 protocol (reserve before fetch, one live-key catalogue row and decision book, one classification with atomic capacity, SKIP(interrupted)); R13 binding; the Policy §5 gate with the journal-head check; durable stops and carryover; orders.pilot config keys (unknown keys refused, config.py:111-112) | build+ | 2-3 weeks with review |
| P1-07 | Astra | The adversarial tests listed in Policy §14 | build | 3-4 days |
| P1-08 | Nuance | `pilot-register`, attended `pilot-run` and `pilot-capture`: session credentials, per-action open and close, immediate remainder cancel with the KILL cases and handoff (Policy §8), scheduler, end margin, settle points, in-session capture, stop handling | build+ | 1 week |
| P1-09 | deferred | Auto-settle (E2): a later option for Yu, with its own review and approval | later | 3-5 days if chosen |
| P1-10 | Nuance | Final execution record (C and L, the finalization table, exact agreement), BSP capture, measurement classes, ledger and drawdown stops, data lock, terminal artifact, report projection test; cancel-race and reduction tests | build+ | 4-5 days |
| P1-11 | deferred | Watchdog and PA5: a later option for Yu, with its own review and approval | later | 3-4 days if chosen |
| P1-12 | Astra | Code review rounds on P1-05 to P1-08 and P1-10; acceptance of the exact code commit and config sha256 | — | 2-3 weeks elapsed |
| P1-13 | Nuance | PT2 and PT3, as Policy §14 step 2 defines them | build+ | 3-4 days |
| P1-14 | Yu present; Nuance supports | PT4 and its receipt, as Policy §14 step 5 defines them; repeated if no bet matches and settles | activation | 1 racing day |
| P1-15 | Yu runs sessions; Nuance supports; Astra recomputes | Run to N = 400 or an ending; data lock; terminal analysis; verdict | activation and the PT4 receipt | see Timeline |

## Yu's decisions (Nuance's recommendation first)
1. Approvals in two steps (Policy §14, §17): the build approval once Astra accepts RS-19 and contract p1; the activation approval after the pre-first-order gate, naming the code commit and config sha256 Astra accepted. Recommended: yes.
2. First rule: P1-FAV-T5. Alternatives: the family-2 small-race variant, or a T-2 version (deeper book, smaller SD).
3. Build: the lean attended version first. Later option: auto-settle (P1-09) and the watchdog (P1-11), each with its own review and approval.
4. Sample: N = 400 (Yu to confirm), 495 (Yu to confirm) lifetime PLACE records, final date 2027-06-30 (Yu to confirm); a shortfall is INCONCLUSIVE. Alternative: N = 800, which makes the £1,000 stop reachable.
5. Stake and caps: £2 flat; up to 25 (Yu to confirm) a day and 5 (Yu to confirm) on each PT4 session day; £50 (Yu to confirm) daily loss; £20 (Yu to confirm) total exposure. Alternative: £1 flat.
6. Decision window: 15 s (Yu to confirm) from off − 300 s; send deadline off − 240 s (Policy §4). Alternative: a wider window, which reaches more markets but blurs "five minutes before".
7. Settlement: typed SETTLE MARKET inside pilot-run at settle points (Policy §8). Alternative: evening only, before 00:00Z.
8. Capture: pilot-capture on the Policy §11 schedule (the same evening (Yu to confirm), then daily). Alternative: next morning, if T10 shows CLOSED lasts long enough.
9. PT4: its bets count if the first session after the PT4 receipt runs under PT4's registration receipt. Alternative: always report PT4 apart, which spends every PT4 PLACE record (up to 5 (Yu to confirm) a PT4 session) of the 495 outside the sample.
10. Drawdown (Policy §10): peak to trough on the one equity equation; £300 (Yu to confirm) session and review stop at once, £600 (Yu to confirm) HALT, £1,000 (Yu to confirm) end. An ACK covers one threshold for the peak named by that stop-id; it re-arms only after a new peak. An ACK of an older stop never disarms a threshold for a newer peak. £1,000 never re-arms. Alternative: measured from the start.
11. Accounting (Policy §10): dead heats, voids, removals and unknowns stay at worst case for all of Pilot 1; a cleared-figure gap above 1p is a review stop from the first bet. Alternative: HALT on the first gap.
12. Funds: an R11 refusal ends the session with a review stop. Alternative: refusal only, as today.
13. Sessions: one arming line per racing day, ending at UNTIL and before 00:00Z, with no new reservation in the last 6 min (Yu to confirm) and Yu present throughout.
14. Keychain: if T0 shows dialogs, Yu approves the read once per session; v1 is attended anyway.
15. Betfair loss limit: £50 (Yu to confirm) a day on the website. Its day boundary and whether it counts open liability are unverified (*Inference*; order-path:626 says only that the API cannot read the limits and that LOSS_LIMIT_EXCEEDED fails and HALTs). If it trips, the pilot stops, which is the safe direction.
16. Account: no manual or Bet Angel bets during a session, with tolerance at £0.
17. Compliance: send the G1 question to Betfair before go-live. Alternative: proceed on the order-path §14 reading (order-path:621-623).
18. Reports: interim counts and codes only; the terminal artifact stays on the Air; a reports/ summary only with fields Yu approves. Alternative: an approved counts-and-verdict summary goes into reports/.

## Timeline (*Inference*)
- Now to early November: buy and activate the key; RS-19 and this plan through Yu's build approval; T0-T10 once the key is active; PT1.
- November to early December: after the build approval, build P1-05 to P1-08 and P1-10, with Astra's review rounds. Deferring the watchdog and auto-settle removes the earlier draft's 6-9 author days of that work (Astra's estimate). That is gross removed work, not a net saving after the added gates, capture and tests; review time dominates. For comparison, the P-003 review (not in this repository) put the simpler manual live path (L4) alone at 4-8+ weeks.
- December: PT2 and PT3, Astra's acceptance, the activation approval, then PT4.
- First pilot bet: early-to-mid December 2026 at the earliest; mid-to-late December is realistic, about 8-10 weeks from now. Both are estimates, not review-verified delivery dates.
- Capacity, from Astra's arithmetic. These are illustrations, not completion guarantees:

| Assumptions | Sessions or days with PLACE records |
|---|---|
| Five PLACE records on day 1, then up to 25 a day; every one eligible | 1 + ceil((400 − 5)/25) = 17 |
| Five on day 1, then ten fully matched £2 bets a day, settled before midnight; every bet eligible | 1 + ceil((400 − 5)/10) = 41 |
| Same caps, an assumed 85% eligible yield (*Estimate*) | about 20 / 48, from 1 + ceil((400/0.85 − 5)/cap) |
| Four sessions a week, at 20-48 sessions | about 5-12 weeks |

- Ten a day is the fully matched £2 scenario, not a bound. £20 of total exposure holds ten fully matched £2 bets, but a partial fill whose remainder is cancelled holds less, so more markets can be open (lifecycle.py:277-285; risk.py:68-74, :107-112). R10 may allow fewer. The settlement day and token rules are in Policy §8.
- R10, scarce qualifying markets, partial fills, failed attempts, missing observations and Yu's attendance all change the rate. Qualifying bets per session are not measured. In 2025 there were about 36.5 GB/IE WIN markets a day (P-003 RS-21 counts, not in this repository). An attended session sees only part of the card, and the share that passes the filters is unknown.
- From a mid-to-late December start, 5-12 weeks is roughly mid-January to late March. February to April is a planning allowance for slower qualification or missed sessions, not a direct result of those numbers.
- The final date 2027-06-30 (Yu to confirm) ends placing. The data lock then waits up to 7 days (Yu to confirm) more for pending classes (Policy §12).
- A fully unmatched bet has no L row, so it stays pending until its 7-day (Yu to confirm) deadline (Policy §11). N can therefore be found up to about a week after the 400th eligible bet is placed. PLACE records made in that week are surplus: they use slots and stake, but stay outside the primary.
