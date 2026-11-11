# Research lane: the data split, the study ledger, rule files and close-v1

Status: proposed (PR A and PR B of three stacked PRs), not merged. No pass has run: nothing here has read a data file or written a ledger entry. The first half of this page covers PR A: frozen rule files, a data split and a ledger of every pass. The second half, from [close-v1](#close-v1-resting-orders-on-the-favourite) on, covers PR B: the first study that uses them, and its pass runner. A second study, inplay-v1, is planned for PR C and is not built.

For Yu, Nuance, Astra and the lab operator. Author: lab-Claude, 2026-11-11, against betangel-research main b9099ff.

- PR A's code is three modules in `betangel_research/betfair/backtest/`: `partition.py` (the split), `study_ledger.py` (the ledger) and `rulefile.py` (rule files). They are tested by `tests/test_backtest_lane.py` on synthetic values only.
- PR B adds three more: `fills.py` (the fill rules), `resting.py` (the study's logic) and `resting_study.py` (the pass runner). They are tested by `tests/test_backtest_resting.py` on synthetic markets only.
- The data files are [research/partitions-v1.json](../research/partitions-v1.json), the three files in [research/rules/](../research/rules/) and the registration [research/studies/close-v1.json](../research/studies/close-v1.json). The files hold the rules; this page explains them and does not override them.
- A "pass" is one run of a study over real data. A "lineage" is one idea (for example `close`); several study ids can belong to it.
- A "rule file" is not a strategy card. That name belongs to research/strategy-cards.json ([edge framework](edge-framework.md)).
- An "order" on this page is a simulated intent inside the backtest package. Nothing here can reach an exchange.
- "Alpha" is the chance a test is allowed to take of calling noise a result. 0.02 a test is planned.
- "Family size" (`m`) is how many claims one test covers.
- A "salt" is a fixed text mixed into a hash. Another salt would give another split, so the salt is written in the split file.

## What the lane is

- A way to test an idea on history without fooling ourselves. Three things make it:
  1. **A split.** Each race date gets a label before anything new is read. Some dates are for looking around. Others are locked away for one test.
  2. **A ledger.** One file on the lab that gets one line per pass. It counts looks and tests, and refuses a second test of the same lineage on the locked dates. That holds only while the same ledger file is used and a runner calls it. close-v1's runner does. The other limits are under "What it cannot do" below.
  3. **Rule files.** A strategy is a small frozen JSON file. One fixed function reads it and says what to order. No code is written per idea.
- PR A is the three modules and four data files. PR B adds the first runner that uses them. Neither changes an existing source file.

## The split

- **Race date.** The Europe/London calendar date of a market's first-published scheduled off, as ISO text (`movement.race_date`). It is the cluster key of both earlier studies (the unit their statistics group by). A market with no first-published off has no race date, so it has no label.
- **The rule.** The split file holds an ordered list of date ranges. A race date gets the label of the range that holds it. A date in no range has no label, and no pass may use it.

  | Race dates | Rule | Label | Role | Budget in the file |
  |---|---|---|---|---|
  | 2015-05-01 to 2016-04-30 | salted hash, below | `PRO-D` or `PRO-V` | explore / holdout | `explore_budget` 20 / `alpha_lifetime` "0.10" |
  | 2023-01-01 to 2023-12-31 | fixed | `BASIC-D` | explore | none |
  | 2024-01-01 to 2024-12-31 | fixed | `BASIC-V` | holdout | none |
  | 2025-01-01 to 2025-12-31 | fixed | `BASIC-T` | sealed | none |
  | 2026-01-01 to 2026-12-31 | fixed | `SEALED` | never | none |

- **The hash.** `u = int(sha256((salt + "|" + race_date).encode("ascii")).hexdigest()[:8], 16)`. The label is `PRO-V` when `u % 3 == 0`, else `PRO-D`. The salt is the text `betangel-research/partitions-v1/PRO-2015-05..2016-04`. Nothing is drawn at run time: the same date always gets the same label.
- **The counts.** Of the 366 calendar dates, 238 are `PRO-D` and 128 are `PRO-V` (35.0%). 2015-05-01 is `PRO-D`, 2015-05-02 is `PRO-V`, 2015-12-25 is `PRO-D`. A test recomputes every date.
- **Half-years.** H1 is 2015-05-01 to 2015-10-31: 120 `PRO-D` dates and 64 `PRO-V`. H2 is 2015-11-01 to 2016-04-30: 118 and 64.
- **Calendar make-up of `PRO-V`.** It is uneven, as a hash split is. Each month holds 7 to 14 `PRO-V` dates: 14 in 2015-11, 8 in 2015-12 and 7 in 2016-01. By weekday it holds 24 of the 52 Tuesdays and 15 of the 52 Wednesdays. No second salt was drawn because of this: choosing a salt by how its split looks would be a choice made after looking.
- **Roles.** `explore`: look around. `holdout`: locked away, tested once per lineage (within the ledger's limits, below). `sealed` and `never`: no pass may name them. The code treats these two alike; the words say what is meant. `sealed` is shut until a later split file opens it. `never` is not meant for any lane pass. The label named `SEALED` has the role `never`, and `BASIC-T` has the role `sealed`. A label with no budget cannot be named either, so no pass can use the BASIC rows until a later split file gives them budgets.
- **Dates with no label.** Everything before 2015-05-01, 2016-05-01 to 2022-12-31, and everything after 2026-12-31. A new split file, merged on Yu's word, is the only way to give them one.
- **Also in the file.** `max_looks` (2 blind passes per study) and `prior_exposure`: one sentence for each earlier read of the same year (`p0`, `movement-v1`, `lp-v1`, `suite-counts`). A later study's registration cites those ids.
- **The older words stay.** [Backtest plan](backtest-plan.md) §3 still calls 2015-05..2016-04 the engineering corpus, 2023-01..2025-12 the research corpus and 2026-01..2026-08 the holdout. The lane adds labels inside the first two. It also labels every 2026 date `SEALED` (role `never`). That covers §3's holdout, and also 2026-09 and 2026-10 to 2026-12, which §3 calls exposed and prospective. So no lane pass may use a 2026 date, the lab's own recordings included, until a new split file says otherwise. What the code does today: the ledger refuses a pass that names `SEALED`, and close-v1's runner never opens a file whose race date has a label its registration does not name (next section). §3's roles and the month gate are unchanged.

## What is enforced, and what is not

- **Built now.**
  - `Split.label` is the only place that knows the rule. The split file's sha256 is pinned in `tests/test_backtest_lane.py`, with the counts above.
  - `parse_split` takes one byte form only (sorted keys, indent 2, ASCII, one final newline) and refuses an unknown key, ranges that overlap or are out of order, a label used twice, a bad salt or a bad residue.
  - The ledger refuses a pass that names a label that is unknown, `sealed`, `never` or without a budget, or whose split hash is not the hash of the split bytes it was given. It parses those bytes itself. So the budgets it applies are the ones in the file whose hash the entry names, and a split object built by hand is refused (`ledger:split_malformed`).
- **Built with close-v1 (PR B).** Its runner gives each record its label, keeps its value rows apart by label and never opens a file whose label is not registered (code `partition_not_registered`). The selection is handed the explore label's rows and two counts per cell from the holdout, nothing else. The old month gate still runs first; it seals 2026-01 onward and the exposed months. movement-v1, lp-v1 and P0 know nothing of labels.
- **Limits, stated plainly.**
  - A pass reads both partitions in one run, and every index record already holds BSP and results. So the split is kept by code paths and tests, not by separate files.
  - A later split file with another salt is refused by nothing in the ledger. The controls are the merged file, its pinned hash, the count tests, and the split hash written into every ledger entry.
  - The 2015-16 year is not fresh. `prior_exposure` says what earlier studies already read from all of it, `PRO-V` dates included.

## The ledger

- **File.** `<ledger dir>/study-ledger-v1.jsonl`. One entry per line, as canonical JSON (one fixed byte form). It holds hashes, whole-number counts, names and codes (the study id, the lineage, the commit, a UTC time, alpha as decimal text): no float, no market or runner id, no statistic. Entries are chained: each holds the hash of the one before, so a careless edit, deletion or reorder is seen. The chain has no secret, so it does not show a careful rewrite (see "What it cannot do").
- **Where it lives.** The code takes an explicit directory and has no default. Operator convention: `~/.local/state/lab-runs/ledger/`, mode 700, on the lab, not in Git. The directory must exist, and gets the checks a study's `--out` gets: outside the repository and every Git worktree, no data-tree name in its path, apart from the data root. A directory that holds a P0 trial ledger is refused (`ledger:trial_ledger_present`).
- **The 18 keys.** No value is ever null.

  | Key | Value |
  |---|---|
  | `ledger_schema` | 1 |
  | `seq`, `prev_sha256`, `entry_sha256` | the chain: `seq` counts from 0; the hash covers every other key |
  | `kind` | `look`, `explore` or `registered` |
  | `study`, `lineage` | the study id, and the idea it belongs to |
  | `partitions`, `partitions_sha256` | the sorted labels the pass touched, and the sha256 of the split file |
  | `registration_sha256`, `input_sha256`, `source_descriptor_sha256`, `code_commit` | what was run, on what, with which code |
  | `m`, `alpha` | family size (1 to 1,000) and alpha spent (decimal text); 0 and "0" unless `registered` |
  | `tables_sha256` | sha256 of the pass's `summary.json` bytes |
  | `result` | `completed`, or `engine_check_failed` (a `look` only) |
  | `recorded_at` | UTC text from the caller's clock |

- **The three kinds.** A `look` is the entry for a blind pass. A blind pass is planned to show counts and how spread out the values are (SDs and ranges), with no average and no result. That is the runner's job: the ledger does not check what a pass shows. A `registered` entry is the one test on the holdout. `explore` is a pass over explore labels only; the format has it, and no runner planned so far writes it.
- **One entry per pass.** The ledger has two calls for a runner. `check` comes before the read and refuses a spent budget. `record` is meant to be called after the read and before any output is written; it appends the one entry. Called in that order, a pass that stops during the read has written nothing. The order is the runner's duty: the ledger cannot tell when it is called. Both calls take the split file's bytes, not a parsed split.
- **A repeat.** A run is known by its study, kind, registration, input, sources and commit. The same run again appends nothing and counts against no budget. Its tables hash and result must equal the stored ones, else `ledger:repeat_differs`.
- **Budgets.** Checked before the read, so a refusal costs no night, and again at the append.
  - Looks: at most `max_looks` (2) per study id. Else `ledger:looks_spent`. A failed engine check (a sanity check of the simulator, defined with inplay-v1) still costs its look.
  - Explore: at most `explore_budget` (20 on `PRO-D`) `explore` or `registered` entries per lineage. Else `ledger:explore_budget_spent`.
  - One test: no earlier `registered` entry for the same lineage on the same holdout label, whatever its study id. Else `ledger:lineage_already_tested`.
  - Alpha: the alpha of every `registered` entry on a holdout label, plus this one, may not pass `alpha_lifetime` (0.10 on `PRO-V`). Else `ledger:alpha_spent`. The sum is exact. At the planned 0.02 a test, 0.10 allows five tests on `PRO-V` in total, all lineages together.
- **The head is required.** To open the ledger the caller must give the last known head: `none` when the file must not exist yet, else `SEQ:SHA256` of the last entry. A wrong head refuses (`ledger:head_not_expected`). So a new or mistyped directory refuses unless the operator says `none`, which is right exactly once. The operator copies the new head into the results note after every pass.
- **A broken chain.** Opening the ledger refuses with `ledger:<reason>`, so every pass stops before the read. Nothing repairs it. A person keeps the bad file unchanged and moves it aside, and the next PR body records its sha256, its last good head and its counts per study.
- **The lock file** is `trial-ledger-v1.jsonl.lock` in the ledger directory. The name is the P0 trial ledger's, because its lock helper is reused unchanged. A leftover lock refuses every pass (`ledger:locked`) until a person has checked that no pass is running and removes it.
- **What it cannot do.**
  - The refusals hold inside one ledger file only. A new directory starts every budget from zero. Keeping the file, and giving its last head, is the operator's duty.
  - A cut at a line boundary, or a rewrite with every later hash recomputed, leaves a valid chain. Only the required head, or a head kept elsewhere, shows it.
  - Nothing is synced to disk, so a power loss can lose the last line.
  - Looks are counted per study id. A new study id in the same lineage gets two fresh looks; the explore and one-test budgets are per lineage and do not reset.
  - The ledger knows an idea only by its lineage text. The same idea under another lineage name gets a new test, until `alpha_lifetime` is spent.
  - Only a lane runner writes to it. close-v1's is the first. movement-v1, lp-v1 and P0 runs do not call it, so they read `PRO-V` dates and leave no entry.
  - One pass at a time per ledger. A pass that started from a head another pass has since moved is refused at the end, after its read (`ledger:head_moved`), and has written nothing.
  - It does not test before the read that it can write. Opening and `check` only read. If the ledger directory or the ledger file cannot be written, the pass is refused at the end (`ledger:lock_failed` or `ledger:not_writable`). It has written nothing and must be run again. So before a pass the operator checks that the directory is mode 700 and that the file, once it exists, can be written.

## Rule files and `decide`

- **A rule file** has schema `betangel.rule/v1` and exactly these keys. Decimal values are text with two decimals (`"1.50"`) and are read exactly; counts are whole numbers. The parser takes one byte form only, as for the split file.

  | Key | Meaning |
  |---|---|
  | `schema`, `id`, `lineage`, `status` | `status` must be `frozen` |
  | `parent_sha256` | sha256 of the rule file this one was changed from, or null |
  | `claim`, `counter_claim` | one sentence each: what we hope, and the dull story that would also fit |
  | `market` | which markets: `countries`, `event_type_id`, `market_type`, `betting_type`, `number_of_winners`, and three flags (`bsp_market`, `turn_in_play_enabled`, `persistence_enabled`), each true or null (null = not asked) |
  | `times` | `{at_s, pull_s}` rows: seconds before the first-published off (−86,400 to −1), and when the order is taken back (later than `at_s`, at most −1), or null for an order that is not taken back before the off |
  | `select` | `favourite` or `each_active` |
  | `when` | `{feature, op, value, code}` rows; all must hold |
  | `intents` | `{side, price, stake, persist}` rows: what to order |

- **The view.** A rule reads six features and nothing else. Whoever builds the view fills them from messages published at or before the decision time t. The view has no field for BSP, a result, the last traded price, traded volume or anything after t.

  | Feature | Meaning |
  |---|---|
  | `market_open` | 1 when the market is OPEN and not in play at t, else 0 |
  | `active_runners` | runners ACTIVE and listed at t |
  | `best_back`, `best_back_size` | the runner's best price to back at t, and the size shown there (GBP of backer's stake) |
  | `best_lay` | the runner's best price to lay at t |
  | `spread_ticks` | price steps (ticks) from `best_back` to `best_lay`; no value when either is off the price ladder |

- **The language.** Operators `>=`, `<=`, `==`. One feature against one constant. `when` is a plain AND: no OR, no tick offsets. A feature with no value fails its row. A row's `code` may not be one of `decide`'s own six codes. It could still clash with a code a runner adds, such as `issued`. close-v1's registration parser refuses that: a row may use none of the runner's own codes, and every row's code must be in the registration's code list.
  - `favourite`: the only runner with the lowest `best_back` among priced runners. A tie gives `favourite_tie`; none priced gives `no_priced_active_runner`. This is `movement.favourite`'s rule.
  - `each_active`: every runner in the view (`no_active_runner` when it holds none). Rows on a runner feature are applied per runner: a runner that fails gets no order, and when every runner fails the code is the first runner's.
  - `price`: `{"ref": "best_back"}`, `{"ref": "best_lay"}` or `{"ref": "fixed", "value": "1.01"}`. `side`: `BACK` or `LAY`. `stake`: GBP of backer's stake.
  - `persist`: `lapse` or `keep`. `decide` only hands the pull time and `persist` back; the runner that will walk the orders acts on them. They mean this. The pull time alone says when an order is taken back. `persist` says what the in-play turn does to an order still resting then: `lapse` ends it, `keep` leaves it in the market. So `lapse` with no pull time is an order that rests until the market turns in play. `keep` in a rule file where any time has a pull time could never matter, and the parser refuses it. `keep` is for simulation only: the repository's live surface is LAPSE only.
- **`rulefile.decide(view, rule)`** gives orders and no code, or no order and one code. In this order: `time_not_in_rule`; `ladder_malformed`; the code of the first failing market row (`market_open`, `active_runners`); the selector's code; then the code of the runner's first failing row, or `no_reference_price` when an order's price has no value. It is pure: it reads no file and no clock, draws nothing and keeps no state. `rulefile.py` imports only the standard library, and a test checks that.
- **What `decide` does not check.** The `market` block is parsed and kept, but checking a market against it is the caller's job. So is checking a price against the price ladder, and building the view from data. `rulefile.py` builds no view; close-v1's `resting.view_of` does.
- **The three files.**

  | File | Lineage | Runner | Times (pull) | Orders |
  |---|---|---|---|---|
  | `close-v1-back.json` | `close` | favourite | −600 (−120), −300 (−120), −120 (−10) | one £2 BACK at the best lay price, `lapse` |
  | `close-v1-lay.json` | `close` | favourite | the same | one £2 LAY at the best back price, `lapse` |
  | `inplay-v1-lay.json` | `inplay` | each active runner | −600 (kept) | eight £2 LAYs at 1.01, 1.02, 1.03, 1.05, 1.10, 1.20, 1.50 and 2.00, `keep` |

  - The two close files ask for an open market, at least 5 active runners, a best back from 1.50 to 4.00 with at least £10 shown, and a spread of 1 or 2 ticks. These are movement-v1's gates (a test compares the constants) plus `market_open`. Their `market` block asks for GB and IE WIN markets with BSP.
  - One thing to know: the BACK rests on the lay side of the book, but only the size on the back side is gated. Its queue can be far smaller than the LAY's.
  - The in-play file asks for an open market with at least 2 active runners, in markets that turn in play and keep orders.
  - Each file's sha256 is pinned in the tests. A change to a file is a new rule file.

## What live and offline would share

- Only two things: the schema and `decide`.
- The tested unit is a cell, not a rule file. A cell is one rule file at one time, or one price of the in-play file. Each of these files holds three or eight separate what-ifs, and no live lane would run them as written. A live rule file would be a new file with one time and one order. Its `parent_sha256` would be the hash of the grid file it came from (the file with several times or prices).
- The `market` gates are each lane's own job.
- **Nothing here is wired to anything live.** The only modules that import these three are close-v1's own, in the same backtest package: `fills.py`, `resting.py` and `resting_study.py`. Nothing outside `betangel_research/betfair/backtest/` imports them (a search on 2026-11-11 found none). None of the six imports a gateway, a client or network code; the boundary tests and `RestingBoundaries` in `tests/test_backtest_resting.py` check that. Calling `decide` from live code would need Yu to change AGENTS.md, which keeps simulated order intents inside the backtest package. These PRs do not ask for that.

## What merging this fixes for good

The split file's hash is written into every ledger entry, and each later registration will name it. So these cannot change later without a new split file:

- the label rows (dates, labels and roles), and that dates after 2026-12-31 have no label;
- the salt, and so which dates are `PRO-V`;
- `alpha_lifetime` 0.10 on `PRO-V`;
- `explore_budget` 20 on `PRO-D`;
- `max_looks` 2;
- the four `prior_exposure` sentences.

Not fixed here: the alpha one study spends. 0.02 is planned, and each registration sets it. At 0.02, `alpha_lifetime` 0.10 allows five registered tests on `PRO-V` in total, all lineages together.

The numbers are placeholders from the lab's design notes. They involve no money. The ledger's 18 keys are fixed too, but by its first entry, not by the merge.

## close-v1: resting orders on the favourite

- **The question.** We leave a £2 order on the favourite a few minutes before the race and take it back before the off. Does it get a better price than the Betfair Starting Price (BSP, the price SP bets settle at)? It counts only if that holds for the orders that would really have been filled.
- **Why "really filled" matters.** A resting order is filled when someone else wants out at our price. That happens more often when the price is about to move against us. So the orders that fill can be worse than the orders that do not. The gap between the two is the new thing this study measures.
- **What it cannot show.** PRO 2015-16: a kill gate and a prior, never confirmation. A bad result can close the idea. A good result is only a reason to try something live at small stakes. The level of g was known from the whole year. Only the gap is new: movement-v1 already showed how the favourite's price sits against BSP, on every date, the locked dates included.
- **Where the rules are.** [research/studies/close-v1.json](../research/studies/close-v1.json) is the registration: the file that fixes every rule before any pass. Its sha256 is pinned in `tests/test_backtest_resting.py`. A change to it is a new registration.

### Cells and orders

- **Two rule files, six cells.** `close-v1-back.json` puts a £2 BACK at the best lay price. `close-v1-lay.json` puts a £2 LAY at the best back price. Each has three times. A "cell" is one rule file at one time.

  | Placed | Pulled | BACK cell | LAY cell |
  |---|---|---|---|
  | off − 600 s | off − 120 s | `close-v1-back@-600` | `close-v1-lay@-600` |
  | off − 300 s | off − 120 s | `close-v1-back@-300` | `close-v1-lay@-300` |
  | off − 120 s | off − 10 s | `close-v1-back@-120` | `close-v1-lay@-120` |

- **Each cell is its own what-if.** Its order is our only order in that market. It is never re-priced. "Off" is the first-published scheduled start.
- **Codes.** Per cell, every market gets one code: the first reason it got no order, or `issued`. The codes add up to the number of records. First come reasons from the index record alone (wrong country or market type, no label for its race date, no BSP market). The index record is the one-line summary of a market that the index step wrote earlier. A market with such a code at every cell is never opened. Then come reasons from the book at the time: a ladder that cannot be read, the rule file's own rows, a price off the price ladder.
- **Classes.** Every order placed ends in one class, set by facts that do not depend on a fill: `outcome_unknown`, `void_own_runner_removed`, `post_decision_removal`, `result_revised_after_close`, `missing_bsp`, `stream_malformed_after_placement`, else `scored`. Only `scored` orders enter an outcome statistic (an SD, a mean, a test, a label). The others stay in the counts, with their fills. The queue sizes and the traded-volume counts are over every order placed.
- **Nothing is dropped for what happened later.** A suspension, an early off or a changed schedule after the order is placed does not drop it. lp-v1 dropped those.
- **Removals are classed, not re-priced.** When another horse is withdrawn, Betfair cuts matched prices by a reduction factor. This study does not model that. The order is classed `post_decision_removal` and is not scored. Withdrawals come when the best-informed people act, so leaving them out flatters the result by an unknown amount. The size of each class is printed per cell and label (table B3), and again with each cell's T5 figures.

### The three fill rules

- **Why three.** History does not show our place in the queue. So every order is judged three times. The rules differ only in the queue: how much of the size shown at our price stood ahead of us.

  | Rule | Queue ahead of us |
  |---|---|
  | `optimistic` | none: front of the queue |
  | `central` | half the size shown |
  | `pessimistic` | all the size shown (lp-v1's rule for joining a price) |

- **When an order fills.** Messages with the same publish time are one batch. After each batch, the first of these that holds fills the order, in full, at its own price:
  1. `trade_through`: a trade at a price beyond ours (below a LAY, above a BACK), of any size.
  2. `quote_cross`: the other side now shows at least £2 at or through our price.
  3. `volume_at_price`: enough has traded at our price since we joined: our queue plus our £2.
- **What counts as a trade.** A rise in traded volume counts only in a batch that began with the market OPEN, and only if that batch also carries a ladder entry at that price for that runner (lp-v1's rule). So the batch that suspends the market still credits its trades, and the batch that reopens it does not.
- **The cross test differs from lp-v1's.** lp-v1 asked for our queue plus our stake on the other side. Here the stake alone is enough. An opposing order can come to rest at our price only after everyone ahead of us is matched or gone, so asking for the queue again would count it twice. This is reasoning, not something checked on real files.
- **No latency.** The order is in the book from the batch at the decision time, with no delay. A home connection would be slower. The tables count the fills within 1 s and 2 s of placing, which a delay could change, and give one line without the first-second fills.
- **Lapse.** If the market turns in play before the pull time, the order ends there. The batch that itself turns the market in play is still judged, because the order was resting when it began. A fill in that batch is counted apart (table B4): it can come from a quote that exists only after the turn.
- **A market with no status.** If a message takes the market's status away (a definition with no status in it), later batches do not count as open and the order cannot fill. It stays `scored`. Table B7 counts such orders, so they cannot hide among the unfilled ones. Whether real files ever do this is not known. A full snapshot with no definition at all would also take the status away, but then the order's result cannot be read and it is classed `outcome_unknown`; the 274 snapshots in the [data audit](data-audit.md)'s sample all carried a definition.

### The score

- **g.** A BACK at price P scores `g = P / BSP − 1`. A LAY at L scores `g = 1 − L / BSP`. It is per £1 of backer's stake, and positive is good for us. Tables print `100 × g` as "points of stake". These are not lp-v1's points.
- **Two nets** ("net" = after the 2% commission):
  - `net_closed`: as if the order were closed at BSP. Commission is paid on a gain only. It is the kinder of the two.
  - `net_held`: as if the order were held to the result. Commission is then paid on the winnings, not on the gain against BSP. In this price band that is roughly 0.7 to 1.5 points lower.
- **Which net decides.** A kill reads `net_closed`: an idea that fails on the kinder net also fails when held. A claim reads `net_held`. The rule files have no closing order, and a £2 BACK cannot be closed at BSP, because a BSP lay needs £10 of liability.
- **Two BSP ends.** The files seem to cut BSP to two decimals. So the true BSP lies between the written value and that plus 0.01. Every figure is worked out at both ends. The "kill end" is the one that favours the order; the "claim end" is the other. So a kill and a claim each have to hold at the end that is hardest for them.
- **The 99% switch.** That reading holds only if BSPs really are whole hundredths. If at least 99% of scored orders' BSPs are, kills read the kill end and claims the claim end. If not, the written value decides alone. Both ends are printed either way.
- **Four figures per cell**, over scored orders:
  1. the mean over filled orders, per fill rule: g and both nets;
  2. the mean g over all orders placed, filled or not;
  3. the value per order placed: `net_held` when filled, 0 when not;
  4. the gap: mean g of filled orders minus mean g of unfilled ones.
- **Part and rest.** A figure for part of a group is given only if the part and the rest each hold 100 orders. Otherwise two printed means would give away a handful of orders. If fewer than 100 orders are unfilled under a rule, its "filled" figures are the all-placed figures, flagged `filled_is_all_placed`.
- **The same idea, across tables.** A figure that is held back keeps its counts and shows `rest_not_given`. The verdicts and the selection are always decided first, on the full values.
  - *Across the three fill rules.* Every pessimistic fill is also a central fill, and every central fill an optimistic one. Two means over groups a few orders apart would give those few orders away. So in each group the rules are taken in the order pessimistic, optimistic, central. A rule's figures are given only if no set of fewer than 100 orders could then be told apart by the figures given.
  - *Across half-years.* A half-year's figure is given only if the other half-year's is, and only if both are over the same kind of group (both over fills, or both over all orders placed).
  - *Across the two partition labels.* A kill's figures are over `PRO-D` and `PRO-V` together. They are given only if `PRO-D` and `PRO-V` each hold 100 of those orders, and each one's own optimistic figures are given over the same orders. The kill verdict is printed either way.
  - *The null.* If 1 to 99 of a claim's orders have no null value, the mean over the other orders is not printed.
  - *Not covered.* The blind pass gives one SD per fill rule. Two SDs over groups a few orders apart can bound how far those orders lie from the mean, but not on which side. The registered pass repeats those pooled SDs beside each label's own mean and SD. If exactly one order separates two such groups, its g can be worked out to one of two values. This is a known residual release, and the registration says so.
- **Sizes.** Under 100 values a group gives counts only. A test, a bound or a selection needs 200 values on 15 race dates. An interval is the range the true mean plausibly lies in; it needs 15 race dates. Intervals group orders by race date (the method is called CR1), because orders on one day move together.

### Selection, the null and the labels

- **Selection on `PRO-D`.** For each rule file, one "primary" cell is chosen by a fixed rule: among its cells with 200 central-rule fills on 15 race dates on `PRO-D`, and 200 pessimistic-rule fills on 15 race dates on `PRO-V` (counts only), the one with the highest mean `net_held` on `PRO-D`. A tie goes to the later time. No such cell means no primary for that rule file.
- **The family.** Two primaries, one per rule file. A p-value is the chance of a mean this high if the true mean were 0. Alpha 0.02 in all, with Holm's correction for two tests (the smaller p-value must pass at 0.01). Each test is one-sided: it asks only whether the mean is above 0. A rule file with no primary still counts as one of the two.
- **The random-time null.** A check for fooling ourselves. For each order a random moment between placing and pulling is drawn by a fixed hash. The middle of the favourite's quote at that moment is scored against BSP as if it were our price. If that "result" is at least half as big as the real one, and clearly not zero, the claim is blocked (`claim_blocked_by_placebo`). It is also blocked when the null cannot be worked out. The null can block a claim. It never kills. It is read, and printed in table T9, only for a primary whose other three claim conditions hold. The table says yes or no to "at least half as big" even when the mean beside it is held back (part and rest, above).
- **The labels**, per primary, the first that applies:
  1. `kill_optimistic`: on `PRO-D` and `PRO-V` together, under the front-of-queue rule, with 200 fills on 15 race dates, the upper bound of mean `net_closed` (one-sided, 97.5%) is below 0. Dead even with the best luck.
  2. `underpowered`: the pessimistic rule has under 200 fills or 15 race dates on `PRO-V`.
  3. `edge_candidate`: on `PRO-V`, on `net_held`: the pessimistic mean passes its test; the mean is above 0 under all three rules; it is above 0 in each half-year, with 100 fills in each; and the null does not block.
  4. `measurement_pilot_candidate`: on `PRO-V`, the pessimistic lower bound of mean `net_held` (one-sided, 98%) is above −1.5 points. If live fills behaved as this simulation does, a small live test would then cost little: 600 fills at £2 would lose at most about £18 on average. That is a bound on a simulated figure. It leaves out latency and removals.
  5. `unresolved`.
- **Printed with every label:** "PRO 2015-16: a kill gate and a prior, never confirmation"; "The level of g was known from the whole year. Only the gap is new."; both nets; both BSP ends; the pessimistic fill rate; any `filled_is_all_placed` flag.
- **The prediction, written before any pass** (it is in the registration, signed by lab-Claude, 2026-11-11): no primary is an `edge_candidate`. The pessimistic mean `net_held` on `PRO-V` is below 0 for both rule files. The labels are `measurement_pilot_candidate` or `unresolved`, and `unresolved` is the likelier because the held net decides. A `kill_optimistic` ends that side of the idea.

### The two passes

- **Blind first.** It gives counts and spreads only. People read it. Then nothing in the registration or the code may change.

  | Table | What it gives |
  |---|---|
  | B1 | records by month and label; records with no off time; records with no registered label |
  | B2, B3 | codes per cell; classes per cell and label |
  | B4 | per cell, label and rule: scored orders, fills, fills by kind, fills within 1 s and 2 s, fills just after a whole hour (when some volume updates are not trades), fills in a batch that left the market not OPEN (a suspension, a close, or a batch with no status), fills in the batch that turned the market in play, orders ended by the pull and by the lapse, the fill rate; fills of the orders that are not scored |
  | B5 | both labels together: how spread out g is over filled orders (SD and three ranges), at the written BSP; the SD over all scored orders; the size shown ahead of the order and the seconds to a fill, as quantiles (a quantile is the value below which that share of the orders lies) |
  | B6 | the smallest effect the test on `PRO-V` could detect, "under independence; clustering makes it larger" |
  | B7 | checks of the machinery: whole-hundredth BSPs, trades counted and not, scored orders whose market lost its status, null values missing by reason, flags with no value |

- **What the blind pass withholds.** Every average, middle value, raw quantile, sign, interval, test, label and selection of an outcome. Any SD of a net. The SD over unfilled orders. Anything by price band or half-year. A test checks every key and the text.
- **Registered second, once.** Everything above, plus:

  | Table | What it adds |
  |---|---|
  | T5 | the four figures per cell and label, at both BSP ends, with the mid at the decision time and each class's size |
  | T7 | the same by half-year: every cell on `PRO-D`, the primaries only on `PRO-V` |
  | T8 | the selection: which cells passed the gates and which were chosen |
  | T9 | per primary: the test, Holm, both bounds, the gap, the null, the label |
  | T10 | the filled figures without first-second fills; quantiles of the sum of 1 / BSP per market (how far the BSP book is from 100%) |

- **Only the primaries are tested.** A p-value, a test or a label is printed only for a primary on `PRO-V`, in table T9. Other `PRO-V` rows are headed "not tested, unadjusted". A primary's own `PRO-V` rows in T5, T7 and T10 are headed "primary: tested in T9 only; this row is descriptive". `PRO-D` rows are headed "exploratory" and carry no p. Each such table prints its number of cells c and `sqrt(2 ln c)`: about how large the best of c pure-noise results looks.
- **No table by price band.** One was planned (T6) and cut.
- **Not measured.** Latency. Partial fills (a fill is all or nothing). Cross-matched prices, which the files leave out. Our real queue place. Removals (classed, never re-priced). A closing order and its rounding.

### The runner

- **Command.** `python3.13 -B -m betangel_research.betfair.backtest.resting_study`, with these arguments:

  | Argument | Meaning |
  |---|---|
  | `--study close-v1` | the only study it runs; anything else is refused before a file is read |
  | `--pass blind\|registered` | blind first |
  | `--registration-sha256 HEX` | must equal the sha256 of the registration file |
  | `--registration PATH` | optional; the default is `research/studies/close-v1.json`. The split file is read beside the `studies/` folder and the rule files in `rules/` beside it; each must be the bytes the registration names |
  | `--archive-root DIR`, `--out DIR`, `--code-commit HEX` | the data root; a new output directory; the commit the code was checked out at |
  | `--ledger DIR`, `--ledger-head none\|SEQ:SHA256` | the ledger directory and its last head; both required |
  | `--blind-receipt PATH` | registered pass only: the blind pass's `receipt.json` |

- **Before it reads a record** it refuses: a wrong hash; a registration that is not in a folder named `studies` (`registration:not_in_a_studies_folder`); a registration, rule file or split it cannot parse; an `--out` that exists, lies in a Git worktree or a data tree, or lies inside the ledger directory, however that directory is spelled (this Mac's file system ignores letter case, so the folders are compared by identity, not by name); a blind receipt of another study, registration, split, commit, set of sources (the package's code files, by hash) or input; a ledger with a broken chain, a head other than the one typed, or a spent budget.
- **The blind receipt leads to the look.** Every receipt names two things: `ledger_head`, the ledger's head after the pass, and `ledger_entry`, the pass's own ledger line. For a registered pass the ledger must hold the line the blind receipt names as its own, and that line must be a look of the same run. A blind pass that is run again (a repeat) writes a receipt that names the same look, so either receipt works.
- **The read.** As lp-v1: the old month gate, one market id once, every file's bytes checked against its manifest. Rows hold no market or runner id and stay in memory.
- **Then, in this order:** the tables are built in memory; the ledger gets its one line; the files are written. A pass that stops during the read leaves no ledger line and no file. If the ledger line is written and the files then fail, or the code faults there, the line stands, the printed result names the new head (`ledger_head`), and running the pass again is a repeat. If only the lock file could not be removed after the line was written, the files are still written and the result says `ledger_lock_left: true`.
- **Outputs.** `summary.json` and `summary.md` (totals only) and `receipt.json`, in a new mode-700 directory. The same registration, input and code give the same summary bytes. The receipt names the hashes, the commit, the new ledger head, how many looks and tests the lineage has had, when the pass started and ended, and two totals: `bytes_walked` (file bytes the replay read) and `bytes_drained` (the rest of those files, hashed but not parsed).
- **What it prints.** One JSON line of counts, codes and hashes. Exit 0 when the pass completed, 2 when it was refused or failed. A fault in the code prints `internal:` and the error's class name only: a traceback could show a market id.

### Operator steps for a pass (on the lab, after the PR merges)

1. **Who starts it.** lab-Claude itself or Yu, after Yu's OK for that pass. Never a subagent or another agent. The lab's rule 1 says no agent runs Python that imports the Betfair package outside the test suite. A pass is such Python, though it is offline and reads only the historical files. So it runs only on Yu's word, pass by pass.
2. **When.** Between 23:00 and 06:45 London. The recorder runs 07:00 to 23:00 and must not be slowed. Start the pass at low priority under a plain shell guard that ends it at 06:45. A pass ended during the read has written nothing. lab-Claude checks the result in the morning and tells Yu.
3. **Ask first.** Before starting, ask Yu whether a session will be armed or an order will rest before the pass ends. If yes, do not start.
4. **A clean clone.** Check out the merged commit in a new directory of its own. Never the live clone, and never at the top of the home folder, of `~/Library` or of `~/.config` (the lab's rule 2a). Note the commit (`git rev-parse HEAD`) and pin the registration (`shasum -a 256 research/studies/close-v1.json`).
5. **The data root.** The PRO root, already ingested and indexed, in a root of its own ([lp study](lp-study.md), operator step 2).
6. **The ledger.** Make its directory once: `mkdir -m 700 -p ~/.local/state/lab-runs/ledger`. Run one pass at a time per ledger. Before each pass check that the directory is mode 700 and can be written, and that the ledger file, once it exists, can be written: the runner finds out only at the end of the read. Pass `--ledger` and the last head (`none` for the very first pass). Copy the new head from the printed line into the results note after every pass.
7. **The run marker.** Write `~/.local/state/lab-runs/ACTIVE` before the pass and remove it after, whatever the result. No code reads it; it is a sign to the other jobs on the lab.
8. **Where outputs go.** Make the parent folder once: `mkdir -m 700 -p ~/.local/state/lab-runs/close-v1`. The runner makes the pass's own directory inside it (`blind`, then `registered`). It refuses if the parent is missing or the directory exists. Never inside the ledger directory. The runner's path check knows three data-tree names only, so keeping outputs and the ledger under `lab-runs` is the control.
9. **Blind, read, registered.** Run the blind pass. People read its `summary.md`. Then run the registered pass once, with `--blind-receipt` and the head the blind pass printed. A pass is expected to take about 3 hours; this is a guess from lp-v1's 2 h 36 min, not a measurement. Both commands, run from the clean clone. `<LAB RUNS>` stands for the full path of `~/.local/state/lab-runs`, and `<PRO ROOT>` for the data root of step 5:

   ```sh
   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.resting_study --study close-v1 --pass blind \
     --registration-sha256 <sha256 from step 4> --archive-root <PRO ROOT> --code-commit <commit from step 4> \
     --ledger <LAB RUNS>/ledger --ledger-head <none, or the last head> \
     --out <LAB RUNS>/close-v1/blind

   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.resting_study --study close-v1 --pass registered \
     --registration-sha256 <the same sha256> --archive-root <PRO ROOT> --code-commit <the same commit> \
     --ledger <LAB RUNS>/ledger --ledger-head <the head the blind pass printed> \
     --blind-receipt <LAB RUNS>/close-v1/blind/receipt.json \
     --out <LAB RUNS>/close-v1/registered
   ```

   - **Read the counts before the registered pass.** The registered pass spends the lineage's one test on `PRO-V`, and 0.02 of its alpha, even if no cell passes the selection's gates. So first read B4 in the blind `summary.md`. A rule file needs a cell with 200 central fills on 15 race dates on `PRO-D` and 200 pessimistic fills on 15 race dates on `PRO-V`. B4 gives the fill counts. It does not give their race dates, but 200 fills on fewer than 15 dates would be very unusual. If neither rule file has such a cell, the registered pass can test nothing: tell Yu before running it.
   - **If a pass stops at the very end.** The 06:45 guard can end a pass after its ledger line is written and before the result is printed. Then the head is not on the screen. It is in the last line of `study-ledger-v1.jsonl` in the ledger directory: that line's `seq`, a colon, and its `entry_sha256`. Such a stop can also leave the lock file (`trial-ledger-v1.jsonl.lock`) in the ledger directory, or a folder named like `--out` with `.partial` on the end. A person checks that no pass is running and removes them. The same goes for a result that says `ledger_lock_left: true`. Then run the same pass again with that head. It is a repeat: it adds no ledger line and writes the files.
10. **Afterwards.** The outputs stay on the lab. Nothing enters Git or `reports/` without Yu's OK on the exact fields. A stop between the two passes is written into the next PR body.
11. **After PR C merges** (planned), the package sources differ from PR B's. A close-v1 registered pass must then run from a clean clone at PR B's commit if its blind pass ran there. From a later commit it refuses (`blind_receipt:another_code`).
- **Planned, not built:** the blind receipt's two byte totals are meant to size inplay-v1, which must read past the off. If the estimate is over 7 hours it will not run at night, and the choice is Yu's.

## What it changes elsewhere

- PR A adds three modules to `betangel_research/betfair/backtest/` and PR B three more. No existing source file changes. The engine certificate covers every module in that directory, so its key moves again with each PR. Any later `run-backtest` needs `certify-engine` at the new sources. The boundary test's count of certified files goes from 25 to 28 (PR A) and to 31 (PR B).
- The runner is its own module, not a subcommand of the backtest CLI: that CLI's `__main__.py` is a certified source and stays byte-identical.
- movement-v1 and lp-v1 are untouched. As before, a re-run of either study's registered pass works only from a clean clone at its own commit ([lp study](lp-study.md), operator step 9).
- One dated line in [backtest plan](backtest-plan.md) §13.
