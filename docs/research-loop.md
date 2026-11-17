# Research lane: the data split, the study ledger, rule files and close-v1

Status: PR A (#64) and PR B (#65) of three stacked PRs, merged on Yu's word of 2026-11-12 ("Yes merge all"). early-price-v1, a study on the BASIC files, with the split file version 2, came in a pull request of its own (#67), which lab-Claude merged on 2026-11-12 under Yu's word of that day ([who merges and who starts a pass](#who-merges-and-who-starts-a-pass)). When that pull request was written (2026-11-12), no pass had run: nothing here had read a data file or written a ledger entry. The first half of this page covers PR A: frozen rule files, a data split and a ledger of every pass. The middle, from [close-v1](#close-v1-resting-orders-on-the-favourite) on, covers PR B: the first study that uses them, and its pass runner. [The split, version 2](#the-split-version-2) and [early-price-v1](#early-price-v1-early-prices-against-bsp) cover the proposed PR. inplay-v1 is planned for PR C and is not built. close-early-v1, early resting LAYs on the explore dates only, came in four pull requests and a speed-up from 2026-11-16 (#70 its pins, #71 its parser and walk, #73 its runner, #74 the speed-up, and the fourth its frozen files); [the split, version 3](#the-split-version-3) and [close-early-v1](#close-early-v1-early-resting-lays-explore-dates-only), near the end of this page, cover them. When the fourth was written (2026-11-17), no pass of close-early-v1 had run. early-price-2015-16-v1, the drift check, runs early-price-v1's measurement unchanged on the 2015-16 BASIC files and names split file version 3. It came in a pull request of its own (2026-11-17), which changes `early.py` and `early_study.py`; [early-price-2015-16-v1](#early-price-2015-16-v1-the-drift-check-on-the-2015-16-basic-year), the last section of this page, covers it. When that pull request was written (2026-11-17), no pass of the drift check had run.

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

## Who merges and who starts a pass

Until 2026-11-12 every merge in this lane waited for Yu's "merge", and every pass over real data for Yu's OK for that pass. On 2026-11-12 that changed, for this lane only.

- **What lab-Claude put to Yu** (lab session, 2026-11-12): "If you'd like me to merge my own offline research PRs after an independent review, and run the night data passes without asking each time, say "research is yours". That covers no live code and no money."
- **Yu's answer, exact:** "ho lah follow what you recc and suggest :)"
- **lab-Claude's reading** (lab-Claude's, not Yu's words): yes to that offer as worded. Yu did not use the phrase that was offered, so this is a reading, and Yu can correct it at any time.
- **What it covers.** lab-Claude may merge its own pull requests in this lane (offline research code, registrations, split files, this page) once a reviewer other than the builder has read them and the whole suite passes. lab-Claude may start a pass at night under the operator steps below without asking first. It tells Yu what ran and what came out.
- **What it does not cover.** Any live code, the order path, a money number, a policy page, the lab's own rule file, or another author's pull request. Those wait for Yu as before.
- **The registrations.** close-v1's and early-price-v1's registrations say each pass needs Yu's OK for that pass. lab-Claude reads Yu's answer above as that OK for night passes of these studies. The registered pass of a study still follows its blind pass on a later night, after lab-Claude has read the blind tables.
- **Unchanged.** Passes run at night only, one at a time per ledger, never while a live command is running on the Mac, and never by a subagent. Outputs stay on the lab.

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

- **The three kinds.** A `look` is the entry for a blind pass. A blind pass is planned to show counts and how spread out the values are (SDs and ranges), with no average and no result. That is the runner's job: the ledger does not check what a pass shows. A `registered` entry is the one test on the holdout. `explore` is a pass over explore labels only; close-early-v1's explore pass writes it ([close-early-v1](#close-early-v1-early-resting-lays-explore-dates-only)), and so does the early-price runner for `early-price-2015-16-v1` ([the drift check](#early-price-2015-16-v1-the-drift-check-on-the-2015-16-basic-year)).
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
- **The files.** Three came with PR A, and two with close-early-v1.

  | File | Lineage | Runner | Times (pull) | Orders |
  |---|---|---|---|---|
  | `close-v1-back.json` | `close` | favourite | −600 (−120), −300 (−120), −120 (−10) | one £2 BACK at the best lay price, `lapse` |
  | `close-v1-lay.json` | `close` | favourite | the same | one £2 LAY at the best back price, `lapse` |
  | `inplay-v1-lay.json` | `inplay` | each active runner | −600 (kept) | eight £2 LAYs at 1.01, 1.02, 1.03, 1.05, 1.10, 1.20, 1.50 and 2.00, `keep` |
  | `close-early-v1-fav.json` | `close` | favourite | −14,400, −7,200, −3,600, −1,800 (each −600) | one £2 LAY at the best back price, `lapse` |
  | `close-early-v1-band.json` | `close` | each active runner | the same | one £2 LAY at the best back price on each runner that passes, `lapse` |

  - The two close-v1 files ask for an open market, at least 5 active runners, a best back from 1.50 to 4.00 with at least £10 shown, and a spread of 1 or 2 ticks. These are movement-v1's gates (a test compares the constants) plus `market_open`. Their `market` block asks for GB and IE WIN markets with BSP.
  - One thing to know: the BACK rests on the lay side of the book, but only the size on the back side is gated. Its queue can be far smaller than the LAY's.
  - The in-play file asks for an open market with at least 2 active runners, in markets that turn in play and keep orders.
  - The two close-early-v1 files are the only files with a parent: the favourite file was changed from `close-v1-lay.json`, and the band file from the favourite file ([close-early-v1](#close-early-v1-early-resting-lays-explore-dates-only)).
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
  | `--study close-v1\|close-early-v1` | the study, of one of two kinds (below); anything else is refused before a file is read. The registration's own id must be this one (`study:not_the_registrations`) |
  | `--pass blind\|registered\|explore` | blind first; then close-v1's registered pass, or a new id's one explore pass. A pass that is not the study's own is refused before a file is read (`pass:not_supported`) |
  | `--registration-sha256 HEX` | must equal the sha256 of the registration file |
  | `--registration PATH` | optional; the default is `research/studies/<study>.json`. The split file is read beside the `studies/` folder and the rule files in `rules/` beside it; each must be the bytes the registration names |
  | `--archive-root DIR`, `--out DIR`, `--code-commit HEX` | the data root; a new output directory; the commit the code was checked out at |
  | `--ledger DIR`, `--ledger-head none\|SEQ:SHA256` | the ledger directory and its last head; both required |
  | `--blind-receipt PATH` | every pass but the blind one: the blind pass's `receipt.json` |

- **Two kinds of study.** close-v1 runs `blind`, then `registered`, and reads both labels. A new id runs `blind`, then one `explore` pass, and reads `PRO-D` alone: `close-early-v1`, and its registered fallbacks `close-early-fav-v1` and `close-early-band-v1`, which have no file. A new id's registered pass is refused, and so is a second explore pass of the same study (`explore:already_run`). Its ledger lines are a `look` and an `explore` line, each with m 0 and alpha "0" ([close-early-v1](#close-early-v1-early-resting-lays-explore-dates-only)).

- **Before it reads a record** it refuses: a wrong hash; a registration that is not in a folder named `studies` (`registration:not_in_a_studies_folder`); a registration, rule file or split it cannot parse; an `--out` that exists, lies in a Git worktree or a data tree, or lies inside the ledger directory, however that directory is spelled (this Mac's file system ignores letter case, so the folders are compared by identity, not by name); a blind receipt of another study, registration, split, commit, set of sources (the package's code files, by hash) or input; a ledger with a broken chain, a head other than the one typed, or a spent budget.
- **The blind receipt leads to the look.** Every receipt names two things: `ledger_head`, the ledger's head after the pass, and `ledger_entry`, the pass's own ledger line. For a registered pass the ledger must hold the line the blind receipt names as its own, and that line must be a look of the same run. A blind pass that is run again (a repeat) writes a receipt that names the same look, so either receipt works.
- **The read.** As lp-v1: the old month gate, one market id once, every file's bytes checked against its manifest. Rows hold no market or runner id and stay in memory.
- **Then, in this order:** the tables are built in memory; the ledger gets its one line; the files are written. A pass that stops during the read leaves no ledger line and no file. If the ledger line is written and the files then fail, or the code faults there, the line stands, the printed result names the new head (`ledger_head`), and running the pass again is a repeat. If only the lock file could not be removed after the line was written, the files are still written and the result says `ledger_lock_left: true`.
- **Outputs.** `summary.json` and `summary.md` (totals only) and `receipt.json`, in a new mode-700 directory. The same registration, input and code give the same summary bytes. The receipt names the hashes, the commit, the new ledger head, how many looks and tests the lineage has had, when the pass started and ended, and two totals: `bytes_walked` (file bytes the replay read) and `bytes_drained` (the rest of those files, hashed but not parsed).
- **What it prints.** One JSON line of counts, codes and hashes. Exit 0 when the pass completed, 2 when it was refused or failed. A fault in the code prints `internal:` and the error's class name only: a traceback could show a market id.

### Operator steps for a pass (on the lab, after the PR merges)

1. **Who starts it.** lab-Claude itself or Yu, after Yu's OK for that pass. Never a subagent or another agent. The lab's rule 1 says no agent runs Python that imports the Betfair package outside the test suite. A pass is such Python, though it is offline and reads only the historical files. So it runs only on Yu's word. Since 2026-11-12 lab-Claude reads Yu's word as standing for night passes ([who merges and who starts a pass](#who-merges-and-who-starts-a-pass)).
2. **When.** Between 23:00 and 06:45 London. The recorder runs 07:00 to 23:00 and must not be slowed. Start the pass at low priority under a plain shell guard that ends it at 06:45. A pass ended during the read has written nothing. lab-Claude checks the result in the morning and tells Yu.
3. **Ask first.** Before starting, ask Yu whether a session will be armed or an order will rest before the pass ends. If yes, do not start. Under the standing word of 2026-11-12, lab-Claude tells Yu which night it plans where it can, and checks at the start that no live command is running on the Mac; if one is, it does not start.
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

## The split, version 2

- **What it is.** [research/partitions-v2.json](../research/partitions-v2.json) is the later split file that [the split](#the-split) says the BASIC rows wait for. It gives the 2023 and 2024 labels a budget, so a pass may name them. Nothing is run or registered by the file alone.
- **What changed from version 1.** Three edits and nothing else:
  - `BASIC-D` (2023, explore) gets `explore_budget` 10: at most 10 explore or registered passes per lineage.
  - `BASIC-V` (2024, holdout) gets `alpha_lifetime` "0.10". At 0.02 a test, that allows five tests on 2024 in total, all lineages together.
  - A fifth `prior_exposure` sentence, `p0-basic-2023-25`: P0's run of 2026-10-17 and the index build read every 2023-25 BASIC file mechanically, results included, and released only counts and digests.
- **What did not change.** Every date row, the salt, the half-years, `max_looks` 2, the PRO budgets, `BASIC-T` (sealed), `SEALED` (never) and the four old sentences. So every date gets the same label and the same half under both files. A test checks every date from 2015 to 2027. The `schema` text inside the file is still `betangel.partitions/v1`: "v2" is the file's name, not a new format, and `partition.py` is unchanged.
- **Version 1 stays.** Its bytes and its pinned hash do not move. close-v1 keeps naming version 1. A study names one split file by its hash, and the ledger checks the bytes it is given against that hash.
- **Half-years.** The file holds one pair of halves, and they are the PRO year. A 2024 date has no half in either file. A BASIC study that needs halves of 2024 writes them in its own registration.
- **Two split files, one ledger.** Each ledger entry stores the hash of the split its pass named, so one ledger file can hold entries that name version 1 and entries that name version 2. What is spent is counted over every entry, whatever file it named: looks by study id, explore passes by lineage and label, tests and alpha by label. Labels and lineages decide, not files.
  - close-v1 (lineage `close`, labels `PRO-D` and `PRO-V`) and early-price-v1 (lineage `early-price`, labels `BASIC-D` and `BASIC-V`) share no label and no lineage. Neither spends anything of the other's.
  - A later `PRO-V` test that names version 2 adds its alpha to the `PRO-V` tests that named version 1. Both files say 0.10, so it is one sum against one limit.
  - A pass that names version 1 cannot use a BASIC label: `ledger:entry:partition_no_budget`.
  - Limit, stated plainly: the ledger reads the size of a budget from the file the pass names. A later file with a larger budget would raise it. The controls are the merged file and its pinned hash.
- **One head for both studies.** Both studies use the same ledger directory. After a pass of either study appends its line, the next pass of either study must be typed with that new head, or it refuses before its read (`ledger:head_not_expected`). So after every pass the new head goes into one shared note, and the next pass, of whichever study, takes it from there. A registered pass still anchors on its own blind pass's entry.
- **One pass at a time, across studies.** A pass that appends while another pass is reading makes the other fail at its end (`ledger:head_moved`), after its whole read. A BASIC pass started during a close-v1 pass would cost close-v1 its night.
- **Later BASIC studies.** The ledger blocks a second test only within the lineage `early-price`. A new BASIC idea under another lineage would get a fresh test on 2024. So, as a rule of this page: no other BASIC study registers until a split file carries an `early-price-v1` exposure sentence, saying what that study released of 2023 and 2024.
- **What merging it fixes.** As for version 1: the two BASIC budgets and the new sentence cannot change later without a third split file. The numbers are placeholders from the lab's design notes and involve no money.

## early-price-v1: early prices against BSP

- **The question.** On GB and IE horse-racing WIN markets, is the last traded price some time before a race a fair guide to the Betfair Starting Price? And do earlier price moves keep going, or come back, by 10 minutes before the off?
- **Words.** LTP = last traded price: the price of the latest trade the file shows. BSP = Betfair Starting Price, the price SP bets settle at. "The off" = the first-published scheduled start. T-10 = 10 minutes before it. "Drift" = a price gets longer; "steam" = it gets shorter. "Gap" = the distance between the best price to back and the best price to lay. A "value" = one runner at one time with a score. SD = standard deviation: how far single values spread. SE = standard error: how far a mean moves by luck. CR1 = the way this page works out an SE when the values of one race date move together: each race date counts as one group.
- **What it cannot show.** A last traded price is not a price anyone could have taken: the files have no quotes and no sizes. So this study can close an idea or flag one. It can never show an edge. Every label is printed with: "A last traded price is not a price anyone could have taken. This can flag or kill; it cannot show an edge."
- **Where the rules are.** [research/studies/early-price-v1.json](../research/studies/early-price-v1.json) is the registration. It names [research/partitions-v2.json](../research/partitions-v2.json) by hash. Both hashes are pinned in `tests/test_backtest_early.py`. The code is two new modules, `early.py` (the rules, pure) and `early_study.py` (the pass runner), tested on synthetic markets only. Race results are never used. Nothing is ordered, so there is no rule file.

### The files and the years

- **BASIC files.** They hold each runner's LTP, sent only when it changes (about once a minute), the market's definitions, removals and the final BSP. No quotes, no sizes, no volume.
- **2023 (`BASIC-D`)** is for looking around. **2024 (`BASIC-V`)** is locked and tested once. 2025 is never opened, nor is the 2015-16 BASIC year, nor any PRO file: their records get a code from the index alone (`month_not_registered`, `plan_not_registered`). A record with a 2026 month stops the whole pass before anything of it is used (`input:<archive sha256>:stale_gate`), as in every study.
- **Half-years of 2024.** The split file's half-years are the PRO year's, so this registration holds its own: H1 is 2024-01-01 to 2024-06-30, H2 2024-07-01 to 2024-12-31.
- **What a pass reads.** Every index record. A record that reaches the view at some time is opened once and read up to the latest such time, at most T-10. Nothing after that time is applied: the reader parses one message beyond it to find where to stop, and the decompressor reads a little ahead. The rest of the file is hashed against its manifest, not parsed. About 27,000 files; a pass should take 6 to 12 minutes. That is a guess from P0's 12-minute run, not a measurement.

### The cells

- **Times.** 4 hours, 2 hours, 1 hour, 30 minutes and 10 minutes before the off (−14,400, −7,200, −3,600, −1,800 and −600 s).
- **Bands**, by the LTP at the time: A is 1.50 to under 4.00, B is 4.00 to under 10.00.
- **Codes.** Each market gets one code per time: the first reason it is not looked at (the old gates of close-v1, a race date with no registered label, `bspMarket` false, a market not open or in play, a price entry with a bad runner id), else `viewed`. Each runner then gets one code: `no_ltp`, `ltp_out_of_bands`, `ltp_never_changed`, `ltp_before_last_removal`, `ltp_stale`, else `priced`. The codes add up to the records and the runners.
- **Ten level cells**: one per time and band. Members: the runners priced there. They are described, never tested. Each time needs its own fresh price, so the cells hold different runners and are not comparable across times.
- **Twelve move cells**: two starts (4 hours and 1 hour) × DRIFT, FLAT or STEAM × two bands. Every move cell is entered at T-10: its members are the runners priced at T-10 whose price moved that way from the start to T-30. The move is `ln(LTP at T-30 / LTP at the start)`: DRIFT at +0.05 or more, STEAM at −0.05 or less, else FLAT. Price up is DRIFT.
- **When a move has no cell.** The first of eight reasons applies: the window cannot be read; a runner was removed in the window; the market had a code at the start; no usable start price; a stale start price; then the same three at T-30. A suspension in the window does not drop the move. The member keeps its cell and is flagged, and the blind pass counts the flags.

### The price rules, in plain words

- **A price counts only if its value changed recently.** The file adds an entry when a value differs in type or form, and a snapshot that leaves a price out adds an empty one. So an entry is not always a new price. A *change* is an entry whose value differs from the last usable one. A first sight, a reset followed by the same price, or `5` then `5.0` is not a change.
- **The age of a price** is the time since its last change. A later trade at the same price sends nothing, so the age is an upper limit on the time since the last trade.
- **Fresh.** The age must be at most `min(3,600 s, half the time left to the off)`: 60, 60, 30, 15 and 5 minutes at the five times. So the price last changed between 5 and 4 hours before the off, 3 and 2 hours, 90 and 60 minutes, 45 and 30 minutes, or 15 and 10 minutes. The windows do not overlap, so no two times read the same change.
- **After the last removal.** If a runner was withdrawn before the time, the price must have changed after that. A withdrawal shifts every price, because the withdrawn horse's chance is shared out among the rest. So an older price is a price for another field.
- **The T-10 price must have changed after the prices the move was measured from.** It last changed between T-15 and T-10, and the T-30 price at or before T-30, so the T-10 price is a later change. The registration is refused unless that holds. Why: otherwise the price we score could be the very price the move ended on, and the move would be scored against itself.
- **That does not make the two prices independent.** In a still market trades jump between the two sides of the gap, and the file shows changes only, so published prices take turns: high, low, high. So for each member the pass counts k, the changes after the T-30 price up to the T-10 price: 1, 2, or 3 and more. The blind pass prints the counts. A `signal_candidate` is printed with the share at k = 1.

### The score

- **x = LTP / BSP − 1** for each scored runner, exactly. Above 0, a back at that price would have beaten BSP; below 0, a lay would. BACK reads x and LAY reads −x, so a positive figure is good for that side. Tables print 100 × x as "points of stake".
- **If the LTP is a fair price, the mean of x is 0** in any cell built only from facts known at the time. A market whose BSPs add up to more than 100% shifts it up by that much; table R6 prints that sum.
- **Net, held to the result**, for the labels only: commission of 2% on a bet held to the result, with a free fill at the LTP, which nobody gets. In band A that is about 0.7 to 1.5 points for a BACK.
- **Classes.** Every priced runner ends in one class from facts after the time: `outcome_unknown`, `void_own_runner_removed`, `post_decision_removal`, `result_revised_after_close`, `missing_bsp`, else `scored`. Only scored values enter a statistic. The class never decides membership.
- **Two BSP ends and the 99% switch**, as close-v1. The switch counts the scored values of the two level cells at T-10, both labels together.
- **Statistics.** SEs are clustered by race date (CR1). Under 100 values a group gives counts only. A test, a bound or a selection needs 200 values on 15 race dates.

### Selection, the test and the labels

- **Selection on `BASIC-D` only.** A move cell is eligible if it has 200 scored values on 15 race dates on each label, a clustered SE on `BASIC-D`, and a predicted detectable effect of at most 2.5 points: the smallest true effect the 2024 test would catch 4 times in 5. Each side's primary is the eligible cell with the highest mean / SE: of x for BACK, of −x for LAY. A tie goes to the cell listed first: start, then DRIFT, FLAT, STEAM, then band. Level cells are never chosen. The rule gets 2023's values, two counts per cell from 2024 and the switch, nothing else.
- **Why mean / SE.** The 12 cells differ a lot in size. In a simulation from the design notes, with no true effect and SEs guessed at 0.35 to 1.7 points across the cells, the highest raw mean picked one of the four noisiest cells about 69% of the time; mean / SE about 33%. The SEs were guesses, not data.
- **The test, on `BASIC-V`, once.** Per side, one-sided: is the side's mean above 0? Holm over two tests at alpha 0.02. A side with no primary still counts in the two.
- **The labels**, per side, the first that applies. No label says "edge".
  1. `kill_free_fill`: over both years pooled, the primary has 200 values on 15 race dates and the upper bound of mean net (held) is below 0. "This one cell, on this side, for a bet held to the result: even a free fill at the last traded price would not pay the commission. It says nothing about the other cells, about closing before the off, or about an order that waits on one side of the gap."
  2. `underpowered`: no primary; or no SE on 2024; or the 2024 test could not see 2.5 points. "This says nothing either way." This comes before labels 3 and 4: a primary whose 2024 test could not see 2.5 points is `underpowered` even when its test rejects under Holm, and R4 prints the rejection beside the label.
  3. `cost_clearing_candidate`: label 4 holds, and the lower bound of mean net on 2024 is above half the gap, guessed as 1.0 point in band A and 2.0 in band B. Its text is label 4's plus "The allowance is a guess at half the gap. The gap was not measured."
  4. `signal_candidate`: the test rejects under Holm, and the mean is above 0 in each half of 2024, each with 100 values. "The pattern showed in 2023 and again in 2024. A steady habit in which side trades last would show the same. It is not a price to bet at. The next step is a check against quotes."
  5. `unresolved`. "This says nothing either way."
- **Printed with every label:** the sentence above on what it cannot show; "Before this study, nothing of 2024 had been evaluated. Its blind pass showed counts and spreads."; the BSP limit ("If BSP in these files is rounded and not cut, the true BSP can lie up to 0.005 below the written value, outside both ends (0.3 points at 1.50)."); the removal sentences ("Values in markets with a later removal are not scored. Any lean from that is unmeasured; the size of the class is printed. Removals are classed, never re-priced by a reduction factor."); the side, cell and band; n, race dates and the detectable effect on 2024; the share of members not scored; the share at k = 1; gross x and net at both BSP ends; the band's allowance; the checks, yes or no.
- **The prediction, written before any pass** (in the registration, word for word): "Signed by lab-Claude on 2026-11-12, before any pass: the band-A level cells at T-30 and T-10 lie within 0.5 points of 0 on BASIC-V; each primary's gross mean for its side on BASIC-V is between -0.5 and +1.5 points; the labels are kill_free_fill or unresolved, about even; about one chance in five of a signal_candidate and under one in twenty of a cost_clearing_candidate."

### Honest power

Every figure here is a guess that the blind pass replaces.

- In band A the moved cells can likely see an effect of 1.4 to 1.9 points; in band B 2.4 to 3.4. An effect that would matter is 2.5 points (1.5 of commission plus 1.0 for half the gap). So band A can likely see one, by a small margin, but not one that only just clears the commission. Most of band B's moved cells cannot pass the 2.5 ceiling.
- The blind pass measures this (B8) before any alpha is spent. Operator step 11 below tells the operator to stop and tell Yu before the registered pass if no cell can see 2.5 points. A person does that; no code enforces it.
- The likely result is `kill_free_fill` or `unresolved`, about even. About one chance in five of a `signal_candidate`.
- In the design notes' simulation (no true effect, commission of 1.1 points, the best of 12 cells picked on 2023), the kill came 99% of the time at a yearly SE of 0.30 points, 89% at 0.38, 50% at 0.50 and 18% at 0.66.

### What each pass releases and withholds

- **Blind first.** Counts and spreads only. Labels are shown apart only for counts.

  | Table | What it gives |
  |---|---|
  | B1 | records by month and label; records with no off time or no registered label; files opened |
  | B2 | market codes per time; each column adds up to the record count |
  | B3 | per time and label: markets viewed, markets with a priced runner, runner codes (`no_ltp` split three ways), priced runners per band |
  | B4 | classes of priced runners per time, band and label |
  | B5 | the age of the price in seconds, as quantiles, per time and band (an age is not an outcome) |
  | B6 | per band, start and label: undefined moves by reason, flagged members (only members that keep a cell are flagged), and per move cell its members, scored values, race dates and members by k |
  | B7 | the SD and three ranges of x for each move cell, both labels together; only for a cell with 100 scored values on each label |
  | B8 | each move cell's detectable effect on 2024, from a clustered SE over both years; empty when B7's SD is |
  | B9 | whole-hundredth BSPs and the switch; times with no `bspMarket` value; the compressed bytes of the files opened |

- **What the blind pass withholds.** Every mean, median, sign, interval, test, label and selection of x. Any figure by half-year. The names DRIFT and STEAM: the two moved cells of a start are shown as `moved_larger` and `moved_smaller`, by size, because more drift than steam would give the rough sign of the early level cells. Any spread by label: a pooled SD beside each year's own would give the gap between the two years' means. Any spread of a move cell with fewer than 100 scored values on either label, in both passes: beside 2023's own mean and SD (R2), the pooled SD would give the figures of the few 2024 values, down to a single one. Every spread of a level cell. The earlier start's spreads in a band when the two starts hold nearly the same values (fewer than 100 apart). A test checks every key and the text.
- **Known residual release.** The 12 SDs show which move cells are noisier. B6 shows how often prices move and how much the two moved cells differ in size, not which is which.
- **Registered second, once.** Everything above with the cells named, plus:

  | Table | What it adds |
  |---|---|
  | R1 | level cells per label: n, race dates, mean x, SD and interval at both BSP ends, and each class's size. 2023 is "exploratory", 2024 "not tested, unadjusted" |
  | R2 | move cells on 2023, the same figures, "exploratory". On 2024: counts only |
  | R3 | the selection: each cell's three checks, mean / SE for each side, the detectable effect, and the two primaries |
  | R4 | per side: the test, Holm, the kill bound, the lower bound of net, the checks, the label and what is printed with it |
  | R5 | the primaries on 2024 by half-year: n, race dates and "mean above 0", yes or no; no mean |
  | R6 | per label, quantiles of the sum of 1 / BSP per market |

- **Only the two primaries print a 2024 move-cell mean**, in R4. This was lab-Claude's recommendation, which Yu left standing on 2026-11-12: printing all twelve cannot be undone, and opening the other ten later would cost a new test. The level cells' 2024 means are printed.
- **Part and rest.** The selection and the labels are decided first, on the full values. Then a group's mean is held back when it, with the means given beside it, would give away the mean of fewer than 100 values. Inside each band's level cell at T-10 the move cells of one start share no runner, so they are checked together. Across the two starts on 2023, if some cells of one start and some cells of the other cover the same runners but for 1 to 99 (in one and not the other), the start checked second is held back: the two sums would give the sum of those few runners. The start checked first is the one that holds the BACK primary, else the one with the LAY primary, else the 4-hour start. On 2024 only the primaries print a mean, so the same check runs between the two primaries when they share a band: BACK is checked first, and LAY is held back. A held-back cell shows its counts and `rest_not_given`, and its SE and mean / SE are held back in R3 too. A primary can print with no mean. Then its label, counts, checks and Holm result (rejected or not) still print, but not its p-value: with the number of race dates the p-value gives mean / SE, and the SE could be worked out from figures that do print. And then neither side prints a Holm-adjusted p-value, which can carry the other side's p-value.

### Limits, stated plainly

- A kill covers one cell, on one side, for bets held to the result. It does not close "drift and steam" as a family. It says nothing about an order that waits on one side of the gap.
- A signal may be only the side the last trade printed on. The file does not say which side of the gap a trade was on.
- Removals are classed, never re-priced by the reduction factor.
- The freshness rule has a cost. A runner that trades for five minutes at one unchanged price sends no new value, so it looks stale at T-10. Coarser price steps make this more likely in band B. The blind pass counts it (B3) and shows the ages (B5).
- BSP precision: the files seem to cut BSP to two decimals. That was seen on 2017 PRO, not checked on BASIC 2023-24.
- Not measured: the gap; the size and side of any trade; whether any order would have filled; the number of trades behind a price; race results.
- The blind pass hides the names DRIFT and STEAM by listing the two by size. If the two cells of a start hold exactly the same number of scored values, the tie goes to the registered order, and a reader could tell which is which. On real sizes an exact tie is very unlikely.
- Two small releases stay, by design. In the registered pass a move cell's pooled SD (B7) beside its 2023 mean and SD (R2) puts an upper limit on how far its 2024 mean lies from 2023's. With two years of about the same size, the limit is about 1.4 times the cell's SD, so it shows a sign only if a mean were larger than the spread of single values, which is not expected. And the two-starts rule for spreads compares whole starts, so two single cells of different starts that differ by under 100 values can both print an SD. That shows the spread of those few values, not where they lie.
- A test noisier than planned is not read. If the 2024 test of a primary could not see 2.5 points, the label is `underpowered` even when the test rejects. This is a choice made before any pass: x has a long tail, and a rejection from a wide SE can rest on a few values. R4 still prints the rejection.

### The runner

- **Command.** `python3.13 -B -m betangel_research.betfair.backtest.early_study`, with close-v1's arguments:

  | Argument | Meaning |
  |---|---|
  | `--study early-price-v1\|early-price-2015-16-v1` | one of the two studies it runs (the second is [the drift check](#early-price-2015-16-v1-the-drift-check-on-the-2015-16-basic-year)); anything else is refused before a file is read. The registration's own id must be this one (`study:not_the_registrations`) |
  | `--pass blind\|registered\|explore` | blind first; then early-price-v1's registered pass, or the drift check's one explore pass. A pass that is not the study's own is refused before a file is read (`pass:not_supported`) |
  | `--registration-sha256 HEX`, `--registration PATH` | the hash must match; the default file is `research/studies/<study>.json`, and the split file is read beside its `studies/` folder |
  | `--archive-root DIR`, `--out DIR`, `--code-commit HEX` | the BASIC data root; a new output directory; the commit the code was checked out at |
  | `--ledger DIR`, `--ledger-head none\|SEQ:SHA256` | the ledger directory and its newest head; both required |
  | `--blind-receipt PATH` | every pass but the blind one (early-price-v1's registered pass, the drift check's explore pass): the blind pass's `receipt.json` |

- **Before it reads a record** it refuses, in close-v1's order: another study or pass name; a wrong hash; a registration not in a `studies` folder; a split file that is not the bytes named; a bad commit, data root or `--out`; a blind receipt of another kind, study, registration, split, commit, set of sources or input; then the ledger: `--out` inside it, a head other than the newest, a blind receipt whose look it does not hold, a spent budget.
- **The ledger.** The blind pass is a `look` of lineage `early-price` on `BASIC-D` and `BASIC-V`. The registered pass is the lineage's one test on `BASIC-V`, with family size 2, and spends 0.02 of 2024's 0.10. It also counts once against `BASIC-D`'s explore budget of 10.
- **Then, as close-v1:** the tables are built in memory; the ledger gets its one line; the files are written. A repeat writes no line and must give the same summary bytes. Of the bytes read, the summary gives one total: the compressed bytes of the files opened. How that total splits into bytes read and bytes drained depends on how far the Python version's bz2 reader reads ahead (8 KiB on 3.13, 128 KiB on 3.15), so that split is in the receipt only and cannot make a repeat under another Python differ.
- **A wrong data root spends no look.** If no record of the root reaches the view at any time (for example a PRO root given by mistake), the pass fails with `input:no_registered_market` before the ledger gets a line.
- **Outputs.** `summary.json`, `summary.md` (totals only) and `receipt.json` (kind `betangel_early_receipt`, close-v1's keys), in a new mode-700 directory. No market id, selection id, name, venue, date or time of a market, and no per-market row reaches them or the printed line; a test searches every output for the synthetic ids.
- **What it prints.** One JSON line of counts, codes and hashes. Exit 0 when the pass completed, 2 when it was refused or failed. A fault in the code prints `internal:` and the error's class name only.

### Operator steps for an early-price-v1 pass (after the PR merges)

`<LAB RUNS>` below stands for the full path of `~/.local/state/lab-runs`.

1. **Who starts it.** lab-Claude or Yu, after Yu's OK for that pass. Never a subagent or another agent. A pass is Python that imports the Betfair package, so it runs on Yu's word. Since 2026-11-12 lab-Claude reads that word as standing for night passes ([who merges and who starts a pass](#who-merges-and-who-starts-a-pass)).
2. **One pass at a time per ledger, across studies.** Never start it while any pass of any study is running on the same ledger.
3. **When.** Between 23:00 and 06:45 London, at low priority, under close-v1's guard (its step 2), because the recorder runs 07:00 to 23:00 and must not be slowed. The blind and registered passes run on different nights, so the blind tables are read first. Yu left this to lab-Claude's recommendation on 2026-11-12; a daytime pass with the recorder off would be a new choice for Yu.
4. **Ask first.** Ask Yu whether a session will be armed or an order will rest in the next 15 minutes. If yes, do not start. Under the standing word of 2026-11-12, lab-Claude checks at the start that no live command is running on the Mac; if one is, it does not start.
5. **A clean checkout** at the merged commit, in a new directory of its own, as close-v1's step 4. Note the commit and pin the registration: `shasum -a 256 research/studies/early-price-v1.json`.
6. **The data root.** The BASIC root, already ingested and indexed, in a root of its own. A root in which no record reaches the view (for example the PRO root, typed by mistake) fails with `input:no_registered_market` and spends no look. Any other mistake that lets the pass complete does spend one, and this study has two looks in all.
7. **The ledger and its head.** Use the same ledger directory as close-v1 (`<LAB RUNS>/ledger`). After any pass of any study, the next pass of any study is typed with the ledger's newest head: `none` if the ledger has no line yet, else the `ledger_head` the last pass printed (`SEQ:SHA256`). Keep that head in one shared note for both studies.
8. **Where outputs go.** Make the parent once: `mkdir -m 700 -p ~/.local/state/lab-runs/early-price-v1`. The runner makes `blind` and then `registered` inside it. Write and remove the run marker as close-v1's step 7 says.
9. **The commands**, from the clean checkout (`<BASIC ROOT>` is the data root of step 6):

   ```sh
   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.early_study --study early-price-v1 --pass blind \
     --registration-sha256 <sha256 from step 5> --archive-root <BASIC ROOT> --code-commit <commit from step 5> \
     --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
     --out <LAB RUNS>/early-price-v1/blind

   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.early_study --study early-price-v1 --pass registered \
     --registration-sha256 <the same sha256> --archive-root <BASIC ROOT> --code-commit <the same commit> \
     --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
     --blind-receipt <LAB RUNS>/early-price-v1/blind/receipt.json \
     --out <LAB RUNS>/early-price-v1/registered
   ```

10. **The input check.** After the blind pass, compare the receipt's `input_sha256` with P0's recorded value (`6e5a8275…27c2`, [backtest plan](backtest-plan.md) §10.1). If it differs, an archive was added or the index was rebuilt, and the exposure sentence `p0-basic-2023-25` may not describe this input: tell Yu before the registered pass.
11. **Read before the registered pass.** Read B3, B5, B6 and B8. Stop and tell Yu if, on either label, fewer than half the markets viewed at 4 hours have a priced runner (B3 gives both counts per label), or if no move cell shows a B8 of at most 2.5 points. The registered pass spends the test even if no cell is eligible. A changed registration is a new hash and costs the second look.
12. **If a pass stops at the very end**, or prints `failed` with a `ledger_head` beside the failure, its ledger line stands. Do as close-v1's step 9 says under "If a pass stops at the very end": the head is the last line of `study-ledger-v1.jsonl` (its `seq`, a colon, its `entry_sha256`); a person checks that no pass is running and removes a leftover lock file or `.partial` folder; then the same pass is run again with that head. It is a repeat: it adds no ledger line and writes the files.
13. **Afterwards.** The outputs stay on the lab. Nothing enters Git or `reports/` without Yu's OK on the exact fields.

## What it changes elsewhere

- PR A adds three modules to `betangel_research/betfair/backtest/` and PR B three more. The early-price-v1 PR adds two more, `early.py` and `early_study.py`. No existing source file changes. The engine certificate covers every module in that directory, so its key moves again with each PR. Any later `run-backtest` needs `certify-engine` at the new sources. The boundary test's count of certified files goes from 25 to 28 (PR A), to 31 (PR B) and to 33 (early-price-v1).
- Each runner is its own module, not a subcommand of the backtest CLI: that CLI's `__main__.py` is a certified source and stays byte-identical.
- movement-v1 and lp-v1 are untouched. As before, a re-run of either study's registered pass works only from a clean clone at its own commit ([lp study](lp-study.md), operator step 9).
- **close-v1 after this merge.** The package sources change, so close-v1's registered pass runs only from the checkout its blind pass ran at. From a later commit it refuses (`blind_receipt:another_code`). If close-v1's blind pass has run and its registered pass has not, Yu is told before this PR merges.
- **Later BASIC studies.** No other BASIC study registers until a split file carries an `early-price-v1` exposure sentence (the rule in [the split, version 2](#the-split-version-2)). Version 3 carries one ([the split, version 3](#the-split-version-3)).
- The `ltp_to_bsp_movement_non_executable` metric that P0's contract declares and never built is done here as a lane study, not in P0.
- One dated line each in [backtest plan](backtest-plan.md) §13 for PR A and for early-price-v1.
- **close-early-v1** adds no module. Its pull requests changed `resting.py` and `resting_study.py` and added data files, tests and the two sections below, so the count of certified files stays 33. The sources' hash moved with #71, #73 and #74, so the engine certificate's key moved too: a later P0 `run-backtest` needs `certify-engine` at these sources. One dated line in [backtest plan](backtest-plan.md) §13.
- **early-price-2015-16-v1** (the drift check) adds no module either. Like close-early-v1's #71, #73 and #74, it changes existing source files: `early.py` and `early_study.py`. In `early.py` the parser takes a second study id (two constants, the id check, the plan's study taken from the file, one docstring sentence). In `early_study.py` a table says which study runs which pass; a registration whose id is not `--study` is refused; the drift check gets a third pass, `explore`, with its own ledger line, one explore pass per study, the locked label coded so that its files are never opened, and the tables E1 to E3; and three small edits let the shared Markdown serve a third pass with the same bytes for the first two. Why: one measurement, run on another year, with no new copy of the code ([what changed in the code](#what-changed-in-the-code)). No measurement function changes, and early-price-v1's outputs are pinned byte for byte by a test committed before any source line moved. The count of certified files stays 33. The sources' hash moves, so the engine certificate's key moves: a later P0 `run-backtest` needs `certify-engine` at these sources. A repeat of an early-price-v1, close-v1 or close-early-v1 pass works only from its own commit. When the pull request was written, no study had a blind pass waiting for its second pass; lab-Claude checks that again before it merges ([the drift check's operator steps](#operator-steps-for-the-drift-check-after-this-pull-request-merges), step 1). One dated line in [backtest plan](backtest-plan.md) §13.

## The split, version 3

- **What it is.** [research/partitions-v3.json](../research/partitions-v3.json): version 2 plus three `prior_exposure` sentences, and nothing else. Its sha256 is pinned in `tests/test_backtest_close_early.py`, and again in `tests/test_backtest_early_2015.py`. close-early-v1 and early-price-2015-16-v1 name it. Nothing is run or registered by the file alone.
- **The three sentences, by kind.** The file holds their words; none holds a figure, a label or a cell of a result.
  - `close-v1`: what close-v1 read of the 2015-16 PRO year (`PRO-V` dates included), that it spent the `close` lineage's one test on `PRO-V`, and the kinds of figure it released on the lab.
  - `early-price-v1`: what early-price-v1 read of the 2023 and 2024 BASIC files, that it spent the `early-price` lineage's one test on `BASIC-V`, the kinds of figure it released on the lab, and what its designer also knew.
  - `p0-basic-2015-16`: what P0's run of 2026-10-17 and the index build read of the 2015-16 BASIC files; P0's outcome metrics stay on the lab.
- **Why they may be in Git.** Yu answered lab-Claude's question of 2026-11-16 on what may enter Git; that the answer covers these three sentences is lab-Claude's reading, and the record is in the lab's notes.
- **What did not change.** Every date row, the salt, the half-years, `max_looks` 2, every budget and role, and the five old sentences. So every date gets the same label and the same half under versions 1, 2 and 3; a test checks every date from 2015 to 2027. The `schema` text is still `betangel.partitions/v1`: "v3" is the file's name.
- **Versions 1 and 2 stay.** Their bytes and pinned hashes do not move. close-v1 keeps naming version 1 and early-price-v1 version 2. One ledger holds the lines of all three: what is spent is counted by study id, lineage and label, not by file ([the split, version 2](#the-split-version-2)).
- **The `early-price-v1` sentence.** [The split, version 2](#the-split-version-2) asks that no other BASIC study registers until a split file carries one. Version 3 is such a file. early-price-2015-16-v1, a BASIC study, names version 3, so it meets that rule.
- **BASIC files of the PRO year.** A label belongs to a race date, not to a file. So `PRO-D` and `PRO-V` apply to the BASIC files of 2015-05-01 to 2016-04-30 as they do to the PRO files of those dates. The names say PRO; the files can be BASIC. early-price-2015-16-v1 reads BASIC files under these two labels.
- **What merging it fixes for good.** The three sentences. A wrong one costs a version 4.
- **Owed.** The next split file owes a `close-early-v1` sentence: what close-early-v1 read and released. It is written once the study's passes have run. It owes an `early-price-2015-16-v1` sentence on the same terms.

## close-early-v1: early resting LAYs, explore dates only

- **The question.** We leave a £2 LAY at the best back price 4 hours, 2 hours, 1 hour or 30 minutes before a race and take it back 10 minutes before the off. Do the orders that would really fill beat BSP once each is closed there, after the 2% commission? On the favourite priced 1.50 to 4.00, and on every runner priced 4.00 to 10.00, on GB and IE horse-racing WIN markets of the 2015-16 PRO files.
- **Only a LAY.** At this stake a BACK cannot be closed at BSP: a BSP lay needs £10 of liability (the registration's `measurement.deciding_net`). So the closing lines read `net_closed`, the order closed at BSP.
- **What kind of study it is.** A registered measurement on the explore dates (`PRO-D`) only: a blind look, then one explore pass on a later night. Nothing is tested and no alpha is spent. It can end a cell; it cannot confirm one. Neither pass opens a file of a `PRO-V` date. The registration's line to print with every word: "PRO 2015-16, explore dates only: a kill gate and a prior, never confirmation. Every fill is simulated."
- **Lineage `close`.** The favourite order is close-v1's own LAY at other times. close-v1 spent the lineage's one test on `PRO-V` ([the split, version 3](#the-split-version-3)), so no study of this lineage can test there again, under this id or another. The new id has two looks of its own; its explore pass counts once against `PRO-D`'s explore budget for `close`.
- **What was known.** The year is not fresh, and neither is the idea. The registration's `exposure` says, by kind, what earlier studies read and released and what this study's designer knew. So it is a description with its closing lines written first, not a test.
- **Where the rules are.** The registration [research/studies/close-early-v1.json](../research/studies/close-early-v1.json), the rule files [research/rules/close-early-v1-fav.json](../research/rules/close-early-v1-fav.json) and [research/rules/close-early-v1-band.json](../research/rules/close-early-v1-band.json), and [research/partitions-v3.json](../research/partitions-v3.json). The four hashes are pinned in `tests/test_backtest_close_early.py`, the rule files' also in `tests/test_backtest_lane.py`. Every field of the registration that the design does not set is close-v1's, word for word, and a test checks it. The code is close-v1's: `resting.py` and `resting_study.py` run two kinds of study ([the runner](#the-runner)).

### Cells and orders

- **Two rule files, four times each: eight cells.** A cell is one rule file at one time. Each cell is its own what-if: its orders are our only orders in that market. An order is never re-priced.

  | Placed | Pulled | Favourite cell | Band cell |
  |---|---|---|---|
  | off − 14,400 s (4 h) | off − 600 s | `close-early-v1-fav@-14400` | `close-early-v1-band@-14400` |
  | off − 7,200 s (2 h) | off − 600 s | `close-early-v1-fav@-7200` | `close-early-v1-band@-7200` |
  | off − 3,600 s (1 h) | off − 600 s | `close-early-v1-fav@-3600` | `close-early-v1-band@-3600` |
  | off − 1,800 s (30 min) | off − 600 s | `close-early-v1-fav@-1800` | `close-early-v1-band@-1800` |

- **The two files.**

  | | `close-early-v1-fav.json` | `close-early-v1-band.json` |
  |---|---|---|
  | Runner | `favourite`: the only runner with the lowest best back | `each_active`: every active runner, each judged alone |
  | Best back | 1.50 to 4.00 | there is one (`no_best_back`), and it is 4.00 to 10.00 |
  | Market | open, at least 5 active runners; GB and IE WIN markets with BSP | the same |
  | Size shown at the best back | at least £10 | the same |
  | Best lay | there is one (`no_best_lay`), and the gap is 1 to 20 ticks (`gap_not_1_to_20_ticks`) | the same |
  | Order | one £2 LAY at the best back price, `lapse` | one such order on each runner that passes |
  | Parent | `close-v1-lay.json` | `close-early-v1-fav.json` |

- **The gap gate.** close-v1 asked for 1 or 2 ticks; hours out that would keep only rare tight books. 20 ticks is the line lp-v1 and close-v1 registered for a book too wide to be a market. lp-v1 rested LAYs at gaps of 3 to 20 ticks, so the gate was widened by someone who could know lp-v1's results; the registration says so.
- **Codes.** As close-v1, in the order of the registration's `population.codes`. A band cell gives a market `issued` when at least one runner got an order, else the first runner's code; table B8 gives every runner's own code. A missing quote has its own code (`no_best_back`, `no_best_lay`), apart from a price out of range.
- **On the hour.** Offs sit on round minutes, so the 1-, 2- and 4-hour orders of a race due on the hour are placed exactly on a whole hour. Table B7 counts the orders placed in the after-the-hour seconds.

### The two switches

- **The hour switch** (`data.traded_volume.hour_rises`: `not_counted`). In a batch whose time lies in the first 60 seconds after a whole UTC hour, Monday to Friday, no rise of traded volume counts as a trade. Such a rise fills nothing by `trade_through` or `volume_at_price`, then or later; `quote_cross` is still judged. Some volume updates in those seconds are currency restatements ([lp study](lp-study.md)), and an order that rests for hours meets several whole hours. True trades in those seconds are lost with them, so fills are understated, never overstated. A fourth trade count, `rises_left_out_in_the_hour_seconds`, counts what the switch removed, and table B4 counts the trade fills in the second and the third minute after the hour, to check that 60 seconds is enough. close-v1's registration has no such key, and close-v1 is untouched.
- **The labels read** (`data.partitions.read`: `explore_only`). Both passes read `PRO-D` alone. A `PRO-V` record is coded `partition_not_registered` from its index line, at every cell its earlier gates let through, and its file is never opened. The one place the runner opens a file also refuses a record whose label is not read (`replay:label_not_read`). Every pass still parses every index line, and index lines hold BSP and results. So the promise is: no `PRO-V` file is opened, and nothing of `PRO-V` is printed but index record counts. Both ledger lines name `PRO-D` alone. The registration's `measurement.bsp_ends` is close-v1's text, word for word: it takes the share of whole-hundredth BSPs over every scored order of both labels. No `PRO-V` order is scored here, so that share is taken over `PRO-D`'s scored orders alone.

### What each pass releases and withholds

- **Night 1, the blind look.** Counts and spreads only, on `PRO-D`.

  | Table | What it gives |
  |---|---|
  | B1 | records by month and label (of `PRO-V`, index record counts only); orders by month |
  | B2 | per cell, the codes; `issued` counts the markets with an order |
  | B3 | per cell, the classes of placed orders (the share of withdrawals) |
  | B4 | per cell and fill rule: scored orders, fills, the fill rate, fills by kind, within 1 s and 2 s, in the after-the-hour seconds, in the second and the third minute after the hour, in a batch that left the market not OPEN, in the batch that turned it in play, orders ended by the pull and by the lapse; the fills of orders not scored |
  | B5 | per cell and fill rule: the SD and three ranges of gross g over filled orders, none for a rule flagged `filled_is_all_placed`; the size shown ahead of the order and the seconds to a fill, as quantiles |
  | B7 | the machinery's counts: whole-hundredth BSPs and whether the two BSP ends decide apart, the four trade counts, orders placed in the after-the-hour seconds, scored orders with a batch that began with no market status, null values missing by reason |
  | B8 | band cells: the runners of the markets whose market rows held, by their first failing row, and the runners ordered on |
  | B9 | per cell: orders placed, scored orders and pessimistic fills by the gap at placing: 1-2, 3-5, 6-10 and 11-20 ticks |

  - **There is no B6.** close-v1's detectable effect describes a test, and none is run.
  - **Withheld.** Every mean, median, raw quantile, sign, interval, test, label and selection of an outcome. Any SD of a net. The SD over unfilled orders, and the SD over all scored orders, also where close-v1 let it stand in for a rule's filled SD. Any outcome statistic by gap, price or half-year. A test checks every key and the text.
- **Night 2, the explore pass, once.** Every blind table again, number for number, and:

  | Table | What it adds, headed exploratory with its number of cells c and `sqrt(2 ln c)` |
  |---|---|
  | E1 | per cell, at both BSP ends: close-v1's four figures with n, race dates, SD and 95% interval; the mid at placing against BSP; each class's size |
  | E2 | the same by half-year, a half's figure only if the other half's is given |
  | E3 | quantiles of the sum of 1 / BSP per market with a scored order |

  - A cell whose pessimistic rule has fewer than 200 fills on 15 race dates prints counts only in E1 and E2.
  - Nothing is tested, bounded, selected or labelled by the code. No p-value, and no outcome figure by gap, or by price within a cell (B9 gives counts only).
  - The mean g of all orders placed and the mid at placing are not fill figures: across a wide gap both look good by construction. No closing line reads them.

### The closing lines, written before any pass

As the registration words them (`verdicts.rule`): lab-Claude reads one word per cell from the explore pass's `summary.json`, the first that applies. The figure is the pessimistic model's filled `net_closed` on the explore label, with its CR1 95% interval.

1. **dropped**: a stop rule of `blind_pass.rule` fired for the cell.
2. **not given**: the figure is not given at `bsp_plus_step` or at `bsp_as_written`.
3. **ended**: at `bsp_plus_step`, the kill end, the interval's upper end is below 0; a figure flagged `filled_is_all_placed` may read ended.
4. **kept as a lead**: the figure is not flagged `filled_is_all_placed`; at `bsp_as_written`, the claim end, the interval's lower end is above 0; and its means in H1 and in H2 are both given and above 0, each on `half_year_min_fills` (100) or more pessimistic fills.
5. **unclear**.

- A mean alone never ends a cell and never makes a lead. If the switch of `measurement.bsp_ends` says the two ends do not decide apart, the as-written figure is read wherever an end is named. Nothing else enters a word: not another model, not `net_held`, not the placed mean, not the mid, not the gap at t, not another cell.
- **Two departures from close-v1, on purpose.** Ended reads the pessimistic model where close-v1's kill read the optimistic one, and a lead reads `net_closed` where close-v1's claim read `net_held`. The pessimistic model leans against the order more the longer it rests, so ended means ended for an order at the back of the queue. Every word is reported with the optimistic and central figures beside it where they are given, the fill rate and the share of orders not scored.
- **What a word leads to.** Ended or dropped closes that cell; when every cell of a rule file is ended or dropped, that rule file's early variant is closed. A lead is reported to Yu and starts nothing: what a lead may earn is Yu's to say. Unclear and not given start nothing. No word leads to a live rule, a money number, a policy page, a second explore pass or a result in Git.
- **The prediction** (in the registration, word for word; signed by lab-Claude on 2026-11-16, before any pass): "(1) The four cells at 30 minutes and at 1 hour pass the stop rules; a 4-hour cell is dropped with about four chances in ten. (2) No cell reads "kept as a lead"; about one chance in eight that one does. (3) At most two cells read "ended", most likely none or one; every other cell that is read is "unclear". (4) In every cell that prints a gap, the filled orders score below the unfilled ones."

### Stop rules after the blind look

lab-Claude reads the blind tables in daylight and writes its reading down before night 2. Nothing in the registration or the code may change between the two passes. As the registration words them (`blind_pass.rule`):

| Rule | Fires when | Then |
|---|---|---|
| S0 | the receipt's `input_sha256` is not the one close-v1's passes named | stop and tell Yu |
| S1 | a code column does not add up to the records; classes do not add up to orders; any fill of kind `trade_through` or `volume_at_price` lies in the after-the-hour seconds; or, over the cells of one rule file under the pessimistic model, the `trade_through` fills in the second minute after the hour number 40 or more and are more than twice those in the third minute | stop and tell Yu: a fault, not a finding |
| S2 | the markets with an order at the cell's time (`issued`) are fewer than half of those at −1,800 s in the same rule file | that cell is dropped |
| S3 | the pessimistic model has fewer than 200 fills on 15 race dates | that cell is dropped; the explore pass enforces this by code and gives that cell's counts only |
| S4 | the cells at −3,600, −7,200 and −14,400 s of a rule file are all dropped | that rule file's early variants end there |
| S5 | every cell is dropped | the explore pass is not run |

- No code enforces S0, S1, S2, S4 or S5: the written reading does. A dropped cell's figures, if the explore pass prints them, are not read.
- A stop is not a reason for a second look. The second look exists for a registration the blind tables show wrong; that is a new hash, and it goes to Yu first.

### The runner and the ledger, for this study

- `--study close-early-v1`, `--pass blind`, then on a later night `--pass explore` with `--blind-receipt`. The runner refuses this study's registered pass, a second explore pass (`explore:already_run`, before the read), this file under another id and another study's file under this one (`study:not_the_registrations`).
- The explore pass needs this study's blind receipt: the same registration, split file, commit, backtest sources and input, and the look in this ledger.
- The ledger gets a `look` line for night 1 and an `explore` line for night 2, each of lineage `close`, naming `PRO-D` alone, with m 0 and alpha "0". The ledger refuses an explore line that names `PRO-V`, and a registered line with this registration's alpha "0.0".
- **A receipt's `ledger_counts` does not count explore lines.** It counts the lineage's looks and registered lines only. So after night 2 the receipt does not show the explore line: the ledger's last line does.

### Operator steps for close-early-v1 (after this pull request merges)

`<LAB RUNS>` below stands for the full path of `~/.local/state/lab-runs`, and `<PRO ROOT>` for the PRO data root close-v1 read ([lp study](lp-study.md), operator step 2).

**Before either night.** Never start a close-v1 or an early-price-v1 pass from this main (below). lab-Claude checks that nothing was added to the PRO root since close-v1's passes, so that S0 can hold.

**Night 1, the blind look.**

1. **Who and when.** lab-Claude itself starts it; never a subagent or another agent. Between 23:00 and 06:45 London, at low priority, under the 06:45 guard. Nothing live: no live command running, no armed session, no resting order, nothing heavy, and no other pass on this ledger. Where it can, lab-Claude tells Yu which night ([who merges and who starts a pass](#who-merges-and-who-starts-a-pass)).
2. **A start script of its own.** The lab's guard for close-v1 (`~/agenttool/notes/research/2026-11-11-research-loop/ops/run-close-pass.sh`) has close-v1's commit and hash written in and takes `blind` or `registered` only. lab-Claude writes a copy for this study that takes `blind` or `explore`. The copy names the clean checkout of step 4, its commit, the registration's hash `45a180b9d86fb3d16b0e21fdfc26663bcb0ee49239bb5e8ec4e8a5b60738a299`, `--study close-early-v1` and outputs under `<LAB RUNS>/close-early-v1/`; for `explore` it passes `--blind-receipt <LAB RUNS>/close-early-v1/blind/receipt.json`. A subagent may not start it.
3. **The clock.** The guard reads the Mac's local time. Before the night, check that `date` prints London time and that the recorder is booked for 07:00.
4. **A clean checkout** of the merged commit in a new directory of its own. Never the live clone, and never at the top of `~`, `~/Library` or `~/.config` (the lab's rule 2a). Note the commit (`git rev-parse HEAD`). Check `shasum -a 256 research/studies/close-early-v1.json`: it must print `45a180b9d86fb3d16b0e21fdfc26663bcb0ee49239bb5e8ec4e8a5b60738a299`, the hash pinned in `tests/test_backtest_close_early.py`.
5. **The ledger and the outputs.** The shared ledger directory `<LAB RUNS>/ledger` and its newest head, from the shared head note (`none` only if the ledger has no line yet). Make the parent once: `mkdir -m 700 -p ~/.local/state/lab-runs/close-early-v1`. Do not write the run marker by hand: the start script of step 2 refuses if `~/.local/state/lab-runs/ACTIVE` or `~/.local/state/lab-runs/HOLD` exists, writes `ACTIVE` before the pass and removes it after, whatever the result.
6. **Run** it with the start script of step 2, never by hand. The script runs this command from the clean checkout, under `nice -n 10`, and ends it at 06:45 local:

   ```sh
   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.resting_study --study close-early-v1 --pass blind \
     --registration-sha256 <sha256 from step 4> --archive-root <PRO ROOT> --code-commit <commit from step 4> \
     --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
     --out <LAB RUNS>/close-early-v1/blind
   ```

7. **Afterwards.** Copy the new head into the shared note. In daylight, write the reading down in a totals-only note: S0 to S5, cell by cell (for S0, close-v1's value is the `input_sha256` in `<LAB RUNS>/close-v1/blind/receipt.json`); which cells are dropped; for each cell what B4 and B5 say of the size of its standard error; the fill counts; the gap ranges; the queue; the share of withdrawals; how long the pass took.

**Night 2, the explore pass, a later night.**

8. Steps 1, 3 and 5 again, with the same checkout, untouched. Then once, with the same start script:

   ```sh
   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.resting_study --study close-early-v1 --pass explore \
     --registration-sha256 <the same sha256> --archive-root <PRO ROOT> --code-commit <the same commit> \
     --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
     --blind-receipt <LAB RUNS>/close-early-v1/blind/receipt.json \
     --out <LAB RUNS>/close-early-v1/explore
   ```

9. **Checks before reading any mean.** Copy the new head. Every value of the blind `summary.json` must appear, equal, at the same key in the explore one: the lab's comparer (`~/agenttool/notes/research/2026-11-12-next-lineage/ops/compare-blind-registered.py`) fits once it knows this study's name and the folder `explore`; lab-Claude makes that copy before night 2. The ledger's last line must be of kind `explore` and name `PRO-D` alone; the receipt's `ledger_counts` will not show it (above). If a check fails, stop and tell Yu.
10. **The reading.** Apply the closing lines clause by clause. Write a totals-only results note: each cell's word and the clause that gave it, the figure at both BSP ends and their middle, the central and front figures, fills and fill rate, the gap, the share of withdrawals, the half-years, and the prediction read against the result.
11. **Tell Yu** what ran and what came out. The outputs stay on the lab. Nothing enters Git, a pull-request body or `reports/` without Yu's OK on the exact fields. If a pass stops at the very end, its ledger line stands: do as close-v1's step 9 says under "If a pass stops at the very end".

- **If one night cannot hold all eight cells.** lab-Claude's timing check of 2026-11-17, an estimate, put a night under the 6-hour line in all but the worst case, so the study is one study, not split by family. If the 06:45 guard ends night 1 during the read, nothing is written and no look is spent; the study is then split by family into `close-early-fav-v1` and `close-early-band-v1`, which the parser already takes. That needs a data pull request, not a source one.

### What it cannot show

- **Delay.** The order is in the book at once; a home connection is slower.
- **Our place in the queue.** Three fill rules stand in for it. None credits size cancelled ahead of us, so over hours the pessimistic rule leans low.
- **Part fills and cross-matched prices.** A fill is all or nothing, and the files' ladders leave cross-matched prices out.
- **Our own effect** on a thin early book.
- **Withdrawals.** An order in a race with a later withdrawal is not scored, on a fact not known when placing. The share is printed beside every word.
- **The close itself.** An equal back at exactly BSP is assumed. A £2 lay at a price L risks £2(L − 1) until it is closed; that is for any later live text.
- **How it picks runners.** By best back, not by last trade. Hours out "the favourite" is the runner with the lowest quote among those that have one, and the gap gate reads a lay quote of any size.
- **Today, and a confirmation.** Every count is from 2015-16. The dates were explored, not locked, and the year is not fresh.

### What merging this fixes for good

- The registration: its cells, gates, the two switches, the closing lines, the stop rules, the prediction and every field copied from close-v1. Any change is a new sha256, and after night 1 it costs the study's second look.
- The two rule files, and the three sentences of [the split, version 3](#the-split-version-3).

### What it changes for close-v1, and what is owed

- **A close-v1 pass started from this main would spend close-v1's second look.** The sources are not those of close-v1's passes, so such a pass would not be a repeat, and it would read the locked dates again. Never start one.
- **Nor an early-price-v1 pass.** Its receipts check the same backtest sources, so a pass of it from this main would not be a repeat either. Never start one.
- **A receipt's `ledger_counts` does not count explore lines.**
- **The next split file owes a `close-early-v1` sentence.**

## early-price-2015-16-v1: the drift check on the 2015-16 BASIC year

- **The question.** On the explore dates of the 2015-16 BASIC year (`PRO-D`), does the last traded price 10 minutes before the off of band-B runners (4.00 to under 10.00) whose price drifted since 4 hours before the off sit below BSP, as the registration's line defines it? The study runs early-price-v1's measurement, cells and rules, unchanged, on the BASIC files of 2015-05 to 2016-04. early-price-v1 never opened them, and [its section](#the-files-and-the-years) stays true of it; this study does open them. The score is early-price-v1's: x = LTP / BSP − 1 per runner. x below 0 means the last trade was below BSP, the side a LAY wants.
- **What kind of study it is.** A registered offline check, not a backtest contract and not a test: a description with its closing line written first. There is no registered pass, no test, no p-value, no selection and no label. No alpha is spent, and the `early-price` lineage's one test on `PRO-V` stays unspent. It spends one of the new id's two looks and one of `PRO-D`'s 20 explore passes for the lineage `early-price`. early-price-v1's ledger lines name `BASIC-D` and `BASIC-V`, so they count nothing against `PRO-D`.
- **Lineage `early-price`, a new id.** The lineage is kept, so the ledger shows it is the same idea, counts its explore passes under one name and goes on refusing a second test of the lineage on 2024. The new id gets two looks of its own.
- **What it cannot show.** Anything about today's market: the files are of another decade. What price could have been had: BASIC files hold last traded prices, with no quotes and no sizes. Whether the price goes on drifting or the last trade only prints on the low side of the gap: the files do not say which side of the gap a trade was on. And the year is not fresh (below). So it can weaken or support an idea; it cannot confirm one and it cannot show an edge. Printed above E1 (`verdicts.printed_with`): "A last traded price is not a price anyone could have taken. The 2015-16 year is another decade and is not fresh. This can weaken or support an idea; it cannot confirm one and it cannot show an edge."
- **The labels, in plain words.** A label belongs to a race date, not to a file ([the split, version 3](#the-split-version-3)). `PRO-D` and `PRO-V` are the split file's labels for the race dates of 2015-05-01 to 2016-04-30, so a BASIC file of such a date carries the label a PRO file of it carries. `PRO-D` is the explore label. `PRO-V` is the locked label: the registration's key for it is `test` because the parser fixes the key's name, and nothing is tested.
- **What was known.** The year is not fresh on any date, `PRO-V` dates included: P0 read these BASIC files, and movement-v1, lp-v1 and close-v1 read the PRO files of the same races. The idea is not fresh either: the two cells were named, and the numbers of the line set, after early-price-v1's results on 2023 and 2024 were read. The registration's `exposure` cites version 3's eight `prior_exposure` ids, with one line for each, by kind.
- **Where the rules are.** The registration [research/studies/early-price-2015-16-v1.json](../research/studies/early-price-2015-16-v1.json) (sha256 `1d57d8248195dd7ff7a17a061e7bb87746b9ef302c2cad0e5787a63ecc4bca43`) names [research/partitions-v3.json](../research/partitions-v3.json) (sha256 `fd35425aab3717099f0be2ce497505ec57fc37549cd24d470e3d6961ff792b65`). Both hashes are pinned in `tests/test_backtest_early_2015.py`. Every field the design does not set is early-price-v1's, byte for byte: the cells, the timing, the population, the statistics, the uncertainty, the diagnostics, what is not measured, and the measurement but for its part-and-rest text. A test checks that. The blocks `family`, `selection` and `verdicts`, the field `registered_pass.test_label_move_means` and `data.halves` are kept because the parser takes one exact key set; each says in its own text that nothing reads it. The code is early-price-v1's: `early.py` and `early_study.py` now run two studies ([what changed in the code](#what-changed-in-the-code)).
- **Who starts its passes.** The registration's `decided_by` relies on the standing word of 2026-11-12 ([who merges and who starts a pass](#who-merges-and-who-starts-a-pass)) for merging this pull request, once a reviewer other than the builder has read it and the whole suite passes, and for starting its two night passes under the operator steps below. Its `blind_pass.rule` puts the explore pass on a later night than the blind look, after lab-Claude has read the blind tables. The standing word does not cover live code, the order path, a money number, a policy page, the lab's rule file, another author's pull request, a daytime pass, or putting any result into Git.
- **What Yu said yes to.** The registration quotes it.
  - On 2026-11-14 lab-Claude asked Yu: "Is a LAY thinkable at all? If no, I drop the drift flag. If yes, I check it offline in small steps before asking for anything live." Item 1 of Yu's three-part reply that day was "yes lay is ok". Items 2 and 3 answered other questions of lab-Claude's and are not relied on. lab-Claude's reading, not Yu's words: a LAY is thinkable, so lab-Claude may check a LAY idea offline in small steps. It does not cover a live LAY, any live rule, any stake, liability or other money number, a closing bet at the starting price, or the policy pages; each of those stays Yu's.
  - On 2026-11-16 lab-Claude asked Yu whether, for this check, its two cell names, the direction it tests, its five threshold numbers and the sentence that these were set after reading the earlier results may enter Git. Yu's reply of that day ended "yes to both on git." That this covers these fields is lab-Claude's reading; the record is in the lab's notes. Yu chose none of the numbers: the cells, the passes and the line are lab-Claude's.

### The two named cells

- **`B from -14400: DRIFT`.** The band-B runners priced 10 minutes before the off whose price drifted between 4 hours and 30 minutes before the off: ln(LTP at T-30 / LTP at T-4 h) of +0.05 or more, early-price-v1's move rule ([the cells](#the-cells)). The line reads this cell alone.
- **`A from -14400: DRIFT`.** The same cell in band A (1.50 to under 4.00). It is exploratory: it gets fixed words, and no outcome.
- Both were named before any pass, and after early-price-v1's results were read.

### What each night releases and withholds

- **Night 1, the blind look.** early-price-v1's blind pass, with no change to its tables, over both labels. It opens the files of both labels.

  | Table | `PRO-D` (explore dates) | `PRO-V` (locked dates) |
  |---|---|---|
  | Files opened | yes | yes |
  | B1 records by month | counts | counts |
  | B2 market codes per time | one table for all records, not by label | |
  | B3 markets viewed, markets with a priced runner, runner codes, priced runners per band | counts | counts |
  | B4 classes of priced runners | counts | counts |
  | B5 the age of the price (seconds, quantiles) | both labels together | |
  | B6 undefined moves by reason; per move cell its members, scored values, race dates and k | counts | counts |
  | B7 the SD and three ranges of x per move cell | both labels together only | |
  | B8 the detectable effect on the locked label | printed; it describes a test that is not run, and is not read | |
  | B9 whole-hundredth BSPs and the switch; `bspMarket` with no value; bytes | both labels together | |

  - **Withheld,** as early-price-v1's blind pass withholds them: every mean, median, sign, interval and quantile of x; any spread by label; every spread of a level cell; the names DRIFT and STEAM (the two moved cells of a start show as `moved_larger` and `moved_smaller`); a move cell's spread when either label holds fewer than 100 of its scored values; the earlier start's spreads in a band when the two starts' scored members differ by fewer than 100. That last rule can hide the spread of the very cells the line reads. Their counts still print, and nothing in the line needs B7.
  - **B8 still prints.** The blind tables are early-price-v1's, so B8 prints too, and its heading still says "both years", which here means both labels. It describes a test this study does not run. Nothing is decided by it.
- **Night 2, the explore pass, once.** New runner code, no new measurement code. It opens explore-date files only.

  | Table | `PRO-D` (explore dates) | `PRO-V` (locked dates) |
  |---|---|---|
  | Files opened | yes | none |
  | B1 records by month | counts | index record counts only (the blind look gave them) |
  | B2 market codes per time | one table; a `PRO-V` record is coded `partition_not_registered` at every time where none of the first thirteen codes (the history gates and the field) applies | |
  | B3, B4, B6 | counts, with DRIFT and STEAM named | every count is 0 |
  | B5 ages | `PRO-D` alone | |
  | B7, B8 | no spread and no detectable effect: every cell shows its counts and `rest_not_given` or `counts_only` | |
  | B9 | `PRO-D` alone; the switch printed here decides nothing | |
  | E1 the two level cells at 10 minutes (`A@-600`, `B@-600`): members, each class's size, n, race dates, mean x, SD and 95% interval at both BSP ends | headed exploratory; c = 2 and `sqrt(2 ln c)` | nothing |
  | E2 the six move cells from the 4-hour start (DRIFT, FLAT and STEAM in bands A and B): the same figures | headed exploratory; c = 6 and `sqrt(2 ln c)`; under part and rest | nothing |
  | E3 quantiles of the sum of 1 / BSP per market with a scored value | printed | nothing |

  - **Withheld by the explore pass:** every figure of the move cells from the 1-hour start and of the level cells at the other times but their counts; every mean, SD, range or interval of `PRO-V`; every test, p-value, bound, selection, label or half-year figure; every net.
  - **Why eight cells.** The question needs these and no others; each further mean is one more thing to be tempted by.
  - **Part and rest** (the registration's `measurement.part_and_rest`). Nothing is selected or tested, so no figure is decided first. Inside each band's level cell at T-10 on `PRO-D`, the move cells of one start share no runner. The 4-hour start is checked first, and in it DRIFT, FLAT and STEAM (`early.given_move_cells` with no primary). A cell is given only if it, and what is left of its band's level cell, hold 100 scored values or none; else it shows its counts and `rest_not_given`. Nothing is pooled over the two labels.
  - **Printed above E1:** `verdicts.printed_with`, `exposure.printed_with_every_label`, `measurement.bsp_ends.limit` and `measurement.removals`. Every interval is descriptive.

### The freeze between the two nights

- Nothing in the registration or the code may change between the two passes.
- The explore pass needs this study's blind receipt. The runner refuses a receipt of another study, registration, split file, commit, set of backtest sources or input, and one whose look this ledger does not hold (the existing `blind_receipt:*` codes).
- Both passes run from the same clean checkout at the same commit, on two nights, so the blind tables are read first.
- The second look stays possible, as for every study. It is for a changed registration, after the blind look shows a rule wrong: a new hash, put to Yu first. A stop is not a reason for one.

### The drift check's stop rules (S1 to S4)

After the blind look lab-Claude reads the tables in daylight and writes its reading down before night 2. As `blind_pass.rule` words them, lab-Claude stops, tells Yu, and runs nothing more until Yu answers, if any of these holds.

| Rule | Fires when |
|---|---|
| S1 | on either label, fewer than half the markets viewed at 4 hours before the off have a priced runner (B3) |
| S2 | on either label, the markets viewed at 4 hours before the off are fewer than half the markets viewed at 10 minutes before the off (B3) |
| S3 | on `PRO-D`, in the group `B from -14400`, either moved cell has fewer than 1,500 scored values, or scored values on fewer than 120 race dates (B6); the blind look does not say which moved cell is DRIFT, so both must pass |
| S4 | the blind receipt's `input_sha256` is not P0's recorded value (`6e5a8275…27c2`, [backtest plan](backtest-plan.md) §10.1); then an archive was added or the index was rebuilt, and the exposure lines may not describe this input |

- S2 closes a hole in S1: if the old files start late, most markets are not viewed at 4 hours at all, and S1 would look only at the few that are.
- No code enforces S1 to S4: lab-Claude does, in writing. A wrong data root spends no look: if no record of the root reaches the view, the pass fails with `input:no_registered_market` before the ledger gets a line.

### The line, written before any pass

As the registration words it (`verdicts.rule`), lab-Claude reads one outcome from the explore pass's `summary.json`.

- **The cell:** `B from -14400: DRIFT` on `PRO-D`, as table E2 gives it.
- **The figures:** at each BSP end, as fractions of the stake to 6 places (0.012 is 1.2 points): the cell's `mean`, `ci_low` and `ci_high` of x, and the `mean` of x of `B from -14400: FLAT`.
- **At one end** the cell **shows** when `mean <= -0.012` and `ci_high < 0`; it is **absent** when `mean >= -0.006` and `ci_low > -0.018`.
- **The outcome** is the first of these that applies.
  1. **Unclear:** at either end the cell's mean or interval is not given (fewer than 100 values, fewer than 15 race dates, or `rest_not_given`).
  2. **The drift shows:** at both ends the cell shows, and its mean is at least 0.009 below FLAT's mean.
  3. **Unclear, a level and not a move:** at both ends the cell shows, and at either end FLAT's mean is not given or the cell's mean is less than 0.009 below it.
  4. **The drift is absent:** at both ends the cell is absent.
  5. **Unclear.**
- **The two ends.** If the blind look's B9 says the two BSP ends do not decide apart, the BSP as written is read alone wherever the line says both ends. The switch read is the blind look's, because it counts both labels together; the one the explore pass prints decides nothing.
- **Nothing else enters the outcome:** not STEAM, not the band's level, not band A, not a part of the year, not a count by k, and no figure worked out afterwards.
- **Beside the cell, deciding nothing:** `B from -14400: FLAT` and `B from -14400: STEAM`; the level cell `B@-600`; the mean of band B at T-10 without the cell, which lab-Claude works out from E1 and E2 as (N_L × m_L − N_C × m_C) / (N_L − N_C), with N the values and m the means (arithmetic on printed figures, with no interval, and no table of the pass); and E3, since a market whose BSPs add up to more than 100% lifts every x by about that much.
- **Band A, in fixed words that are no outcome.** `A from -14400: DRIFT` is reported in the same way, headed exploratory: **seen again** when, at both ends, `mean <= -0.015` and `ci_high < 0`; **not seen at that size** when, at both ends, `ci_low > -0.015`; else **unclear**. Nothing is built on band A's words; they go to Yu with the report.
- **What an outcome leads to.**

  | Outcome | Leads to |
  |---|---|
  | The drift shows | lab-Claude may register the next small step: a facts pass at 10 minutes before the off on the 2015-16 PRO files that uses no BSP and no result: how far the offered lay price sits above the last traded price, and on which side of the gap the last trade printed. Its closing line goes into its own registration first. |
  | The drift is absent | no further work on the 2015-16 year for this cell |
  | Unclear | reported to Yu as unclear, with its reason; lab-Claude starts no further step on that year for this cell |
  | A stop after the blind look (S1 to S4) | Yu is told, and nothing more runs until Yu answers |

- **No outcome leads to** a live rule, a live LAY or any change to the order path; a money number or a change to a policy page; a test on `PRO-V`; a second explore pass, a changed line, or another cell made "the" cell after the fact; a reading of any 2024 cell that early-price-v1 gave as counts only; or any result in Git, a pull-request body or `reports/` without Yu's OK on the exact fields.
- **Where the numbers come from.** lab-Claude set them with early-price-v1's results in view. The arithmetic behind them, and how likely each outcome is, are in a note on the lab and not in Git.
- **The prediction** (in the registration, word for word; signed by lab-Claude on 2026-11-17, before the build and before any pass): "Signed by lab-Claude on 2026-11-17, before the build and before any pass. (a) The blind look passes S1 to S3, with about three chances in four; the likeliest stop is the files not reaching back four hours. (b) The mean x of B from -14400: DRIFT on PRO-D at the BSP as written lies below -0.006, and more likely than not at or below -0.012. (c) Shows about one chance in two, absent about one in ten, unclear about two in five. (d) Band A most likely reads unclear (about six in ten), seen again about three in ten, not seen at that size about one in ten."

### Known small releases

As the registration's `exposure.blind_pass_releases` says them:

- **The blind look.** The 12 SDs show which move cells are noisier; B6 shows how often prices move and by how much the two moved cells differ in size. It lists the two moved cells unnamed, but lab-Claude knows from early-price-v1's registered pass which of the two was the larger on 2023 and 2024, so the listing hides less here than it did there. Whether that holds in 2015-16 is not known.
- **The two passes together.** A move cell's pooled SD (B7) beside its `PRO-D` mean and SD (E2) puts an upper limit on how far its `PRO-V` mean lies from its `PRO-D` mean: about 1.2 times the cell's SD, with 65% of the dates on `PRO-D`. That shows a sign only if a mean were larger than the spread of single values. early-price-v1 names the same kind of release ([limits, stated plainly](#limits-stated-plainly)).
- **Printed, not read.** Every pass parses every index line, and every index line holds BSP and results ([what is enforced, and what is not](#what-is-enforced-and-what-is-not)). So the promise for `PRO-V` in the explore pass is: no file is opened, and nothing is printed but index record counts.

### The runner and the ledger for the drift check

- **Command.** early-price-v1's runner (`python3.13 -B -m betangel_research.betfair.backtest.early_study`) with `--study early-price-2015-16-v1`: `--pass blind`, then on a later night `--pass explore` with `--blind-receipt`. Give no `--registration`: the runner reads `research/studies/early-price-2015-16-v1.json` and `research/partitions-v3.json` beside it.
- **Refused before any record is read:** this study's registered pass and early-price-v1's explore pass (`pass:not_supported`); this file under early-price-v1's id, and early-price-v1's file under this one (`study:not_the_registrations`); a second explore pass of this study that is not a repeat (`explore:already_run`). A repeat of the same run appends nothing and must give the same summary bytes.
- **The ledger.** Night 1 is a `look` of lineage `early-price` naming `PRO-D` and `PRO-V`, with version 3's sha256, m 0 and alpha "0"; it uses one of this id's two looks. Night 2 is an `explore` line naming `PRO-D` alone, with m 0 and alpha "0"; it counts once against `PRO-D`'s explore budget for the lineage `early-price`. The ledger refuses an explore line that names `PRO-V` (`ledger:entry:explore_touches_another_role`), and a registered line with this registration's alpha "0.0" (`ledger:entry:m_or_alpha_malformed`).
- **A receipt's `ledger_counts`** counts the lineage's looks and registered lines, early-price-v1's included, and no explore line. After night 2 the ledger's last line shows the explore line; the receipt does not.
- **A `PRO-V` record in the explore pass** is coded from its index line: at every time it keeps a code of the history gates and the field if one applies, and gets `partition_not_registered` at every other time. So it has a code at every time and its file is never opened. It counts in B1 and B2 only. The explore pass's `summary.md` says so in one header line.
- **Outputs** as early-price-v1's: `summary.json`, `summary.md` (totals only) and `receipt.json`, in a new mode-700 directory.

### What changed in the code

- **`early.py`**, a few lines: two constants (`DRIFT_CHECK_ID`, `STUDY_IDS`); the registration's id must be one of the two, else `registration:id:not_supported`; the plan's study is the file's own id; one docstring sentence. Nothing from `race_label` on changes: not the codes, the fold, the price rules, the cells, the classes, the scores, the blind spreads, the selection, the verdicts or part and rest. The key sets, the lineage check and the imports are as they were.
- **`early_study.py`.** `PASSES_OF` says which study runs which passes: early-price-v1 `blind` and `registered`, the drift check `blind` and `explore`. A registration whose id is not `--study` is refused. Every pass but the blind one needs the blind receipt. The explore pass has its own ledger line and the one-explore-pass check. `_read_only` recodes a record of another label from its index facts, in the explore pass only. `_explored` and `_explored_lines` give E1 to E3 from existing functions. Three small edits to the shared Markdown (the withheld line prints when the summary has that key; the `moved_larger` sentence prints in the blind pass only; B9 names the labels it counts) give early-price-v1's two passes the same bytes as before. The command line's choices and help name both studies.
- **early-price-v1's outputs are pinned.** The first commit of the pull request, a test only, on the untouched sources of 76a9595, pins the sha256 of `summary.json` and of `summary.md` of both end-to-end passes of the synthetic `shared()` run (`EarlyPriceV1IsUnchanged` in `tests/test_backtest_early.py`; the same on two runs and under python3.13 and python3.15), beside the class's four hand-built table constants from close-early-v1's pull request 1. No later commit changes them, nor close-v1's `CloseV1IsUnchanged` constants, nor the hashes of early-price-v1's registration and of split files 1 and 2.
- **Tests.** `DriftCheckRun` in `tests/test_backtest_early.py` runs the mechanism on early-price-v1's synthetic archive with the id changed: each study runs its own passes only; blind then explore, end to end; no locked-date file opened by the explore pass; every locked-label count 0 but B1's; the explore label's counts equal to the blind look's; E1 and E2 hold their cells only; no test, selection or label; the receipt's checks; one explore pass, and a repeat with the same bytes; counts only under the real gates. `tests/test_backtest_early_2015.py` checks the real files: version 3 and the registration in their one byte form with their pinned hashes; the parse under version 3, and the refusal under version 2's bytes; the same measurement as early-price-v1; the quoted words and the signed prediction; what the ledger takes of this study; and, on a small synthetic root, that the blind look opens the two 2015-16 BASIC files and the explore pass the explore-date file only. early-price-v1, over the same root, opens its own year's file and neither 2015-16 file.

### Operator steps for the drift check (after this pull request merges)

`<LAB RUNS>` below stands for the full path of `~/.local/state/lab-runs`, and `<BASIC ROOT>` for the BASIC data root early-price-v1 read (its step 6).

**Before either night.**

1. **Review and merge.** A reviewer who is not a builder reads the pull request. The whole suite passes under python3.13 and python3.15. lab-Claude checks whether any study has a blind pass waiting for its second pass; such a second pass then runs from the checkout its blind pass ran at, never from this main. Then lab-Claude merges under the standing word. No result figure goes into the pull-request body.
2. **Which two nights.** lab-Claude tells Yu which two nights it plans. Never a night on which another pass of any study runs on this ledger: one pass at a time per ledger, across studies. Never start a close-v1, early-price-v1 or close-early-v1 pass from this main ([what merging this fixes, and what is owed](#what-merging-this-fixes-and-what-is-owed)).

**Night 1, the blind look.**

3. **Who and when.** lab-Claude itself starts it; never a subagent or another agent. Between 23:00 and 06:45 London (check with `TZ=Europe/London date`), at low priority, under the 06:45 guard. Nothing live: no live command running, no armed session, no resting order, nothing heavy, and no other pass on this ledger. If any of these is not so, do not start.
4. **A start script of its own.** The lab's guards have a study's commit and hash written in: early-price-v1's (`~/agenttool/notes/research/2026-11-12-next-lineage/ops/run-early-pass.sh`) runs this runner on the BASIC root but takes `blind` or `registered` only, and close-early-v1's copy of close-v1's guard takes `blind` or `explore`. lab-Claude writes a copy for this study that takes `blind` or `explore`. The copy names the clean checkout of step 6, its commit, the registration's hash `1d57d8248195dd7ff7a17a061e7bb87746b9ef302c2cad0e5787a63ecc4bca43`, `--study early-price-2015-16-v1`, `<BASIC ROOT>` and outputs under `<LAB RUNS>/early-price-2015-16-v1/`; for `explore` it passes `--blind-receipt <LAB RUNS>/early-price-2015-16-v1/blind/receipt.json`. Like close-early-v1's copy, it refuses if `<LAB RUNS>/ACTIVE` or `<LAB RUNS>/HOLD` exists, writes the run marker `<LAB RUNS>/ACTIVE` before the pass and removes it after, whatever the result, runs the pass under `nice -n 10` and ends it at 06:45 local. A subagent may not start it.
5. **The clock.** The guard reads the Mac's local time. Before the night, check that `date` prints London time and that the recorder is booked for 07:00.
6. **A clean checkout** of the merged commit in a new directory of its own. Never the live clone, and never at the top of `~`, `~/Library` or `~/.config` (the lab's rule 2a). Note the commit (`git rev-parse HEAD`). Run `shasum -a 256 research/studies/early-price-2015-16-v1.json`: it must print `1d57d8248195dd7ff7a17a061e7bb87746b9ef302c2cad0e5787a63ecc4bca43`, the hash pinned in `tests/test_backtest_early_2015.py` (`REGISTRATION_SHA256`).
7. **The data root.** `<BASIC ROOT>`, already ingested and indexed, in a root of its own. A root in which no record reaches the view fails with `input:no_registered_market` and spends no look; any other mistake that lets the pass complete spends one.
8. **The ledger and the outputs.** The shared ledger directory `<LAB RUNS>/ledger`: mode 700 and writable, and the ledger file writable. Its newest head comes from the shared head note, and it must equal the last line of `study-ledger-v1.jsonl` (its `seq`, a colon, its `entry_sha256`); `none` only if the ledger has no line yet. Make the output parent once: `mkdir -m 700 -p <LAB RUNS>/early-price-2015-16-v1`. Do not write the run marker by hand: the start script does.
9. **Run** it with the start script of step 4, never by hand. The script runs this command from the clean checkout:

   ```sh
   PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
     python3.13 -B -m betangel_research.betfair.backtest.early_study --study early-price-2015-16-v1 --pass blind \
     --registration-sha256 1d57d8248195dd7ff7a17a061e7bb87746b9ef302c2cad0e5787a63ecc4bca43 \
     --archive-root <BASIC ROOT> --code-commit <commit from step 6> \
     --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
     --out <LAB RUNS>/early-price-2015-16-v1/blind
   ```

10. **Afterwards.** Copy `ledger_head` from the printed line into the shared head note. In daylight, read the blind tables and write the reading down in a totals-only note on the lab, before night 2:
    - S4: the receipt's `input_sha256` against P0's value. S1 and S2 from B3. S3 from B6.
    - What the look says about the old files: B2's `anchor_not_known_by_decision` at -14400 (do the files reach back 4 hours?); B3's stale and never-changed prices and B5's ages (how often is the price sent?); B4's `missing_bsp` and `outcome_unknown` (is BSP there?); B9's whole-hundredth BSPs and the switch; B1 by month (is it a whole year?).
    - If any of S1 to S4 holds: tell Yu and stop. Run nothing more.
    - Do not use B7 or B8 to change anything. Nothing in the registration or the code may change now.

**Night 2, the explore pass, a later night.**

11. Steps 3, 5 and 8 again, with the same checkout, the same commit and no file touched. Then once, with the same start script:

    ```sh
    PATH=/opt/homebrew/bin:$PATH BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 \
      python3.13 -B -m betangel_research.betfair.backtest.early_study --study early-price-2015-16-v1 --pass explore \
      --registration-sha256 1d57d8248195dd7ff7a17a061e7bb87746b9ef302c2cad0e5787a63ecc4bca43 \
      --archive-root <BASIC ROOT> --code-commit <the same commit> \
      --ledger <LAB RUNS>/ledger --ledger-head <the newest head> \
      --blind-receipt <LAB RUNS>/early-price-2015-16-v1/blind/receipt.json \
      --out <LAB RUNS>/early-price-2015-16-v1/explore
    ```

12. **Checks before reading any mean.** Copy the new head into the shared head note. The explore pass's `PRO-D` counts in B3, B4 and B6 equal the blind look's, with the two unnamed moved cells matched to DRIFT and STEAM. Every `PRO-V` count is 0 but B1's (and B2's `partition_not_registered`). The ledger's last line is of kind `explore` and names `PRO-D` alone; the receipt's `ledger_counts` will not show it. If one of these fails, stop and tell Yu.
13. **The reading.** Apply the line clause by clause, from `summary.json`, at both BSP ends. Write a totals-only results note on the lab: the outcome and the clause that gave it; the cell's figures at both ends; FLAT, STEAM, the level and the rest of the band beside it; E3; band A's words; the prediction read against the result.
14. **Tell Yu** what ran and what came out, in plain words, with what the outcome leads to. The outputs stay on the lab. Nothing enters Git, a pull-request body or `reports/` without Yu's OK on the exact fields.
15. **If a pass stops at the very end**, or prints `failed` with a `ledger_head` beside the failure, its ledger line stands. Do as close-v1's step 9 says under "If a pass stops at the very end": the head is the last line of `study-ledger-v1.jsonl`; a person checks that no pass is running and removes a leftover lock file or `.partial` folder; then the same pass is run again with that head. It is a repeat: it adds no ledger line and writes the files.

### What merging this fixes, and what is owed

- **Fixed for good:** the registration, with its two cells, its line and five numbers, the stop rules, the prediction and every field copied from early-price-v1. Any change is a new sha256, and after night 1 it costs the study's second look.
- **A pass of another study from this main is not a repeat.** The backtest sources change, so a close-v1, early-price-v1 or close-early-v1 pass started from this main would not repeat a pass of an earlier commit: a blind pass would spend a look, and a second pass would refuse (`blind_receipt:another_code`). close-early-v1's passes run from its own clean checkout (its step 4). Never start one of them from this main.
- **The next split file owes** an `early-price-2015-16-v1` sentence, written once the study's passes have run, beside the `close-early-v1` one ([the split, version 3](#the-split-version-3)).
- **Still owed after early-price-v1:** two page corrections promised after its registered pass, to its [honest power](#honest-power) paragraph and to the words "not which is which" in its known residual release and its limits. This pull request leaves early-price-v1's section as it was.
