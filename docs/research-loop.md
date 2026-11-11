# Research lane: the data split, the study ledger and rule files

Status: proposed (PR A of three stacked PRs), not merged. Nothing here has read a data file, run a study or written a ledger entry. This page covers the three parts of PR A: frozen rule files, a data split and a ledger of every pass. The two studies planned to use them (close-v1 and inplay-v1) come in later PRs and get their own sections here.

For Yu, Nuance, Astra and the lab operator. Author: lab-Claude, 2026-11-11, against betangel-research main b9099ff.

- The code is three new modules in `betangel_research/betfair/backtest/`: `partition.py` (the split), `study_ledger.py` (the ledger) and `rulefile.py` (rule files). They are tested by `tests/test_backtest_lane.py` on synthetic values only.
- The data files are [research/partitions-v1.json](../research/partitions-v1.json) and the three files in [research/rules/](../research/rules/). The files hold the rules; this page explains them and does not override them.
- A "pass" is one run of a study over real data. A "lineage" is one idea (for example `close`); several study ids can belong to it.
- A "rule file" is not a strategy card. That name belongs to research/strategy-cards.json ([edge framework](edge-framework.md)).
- An "order" on this page is a simulated intent inside the backtest package. Nothing here can reach an exchange.
- "Alpha" is the chance a test is allowed to take of calling noise a result. 0.02 a test is planned.
- "Family size" (`m`) is how many claims one test covers.
- A "salt" is a fixed text mixed into a hash. Another salt would give another split, so the salt is written in the split file.

## What the lane is

- A way to test an idea on history without fooling ourselves. Three things make it:
  1. **A split.** Each race date gets a label before anything new is read. Some dates are for looking around. Others are locked away for one test.
  2. **A ledger.** One file on the lab that gets one line per pass. It counts looks and tests, and refuses a second test of the same lineage on the locked dates. That holds only while the same ledger file is used and a runner calls it. No runner does yet. The other limits are under "What it cannot do" below.
  3. **Rule files.** A strategy is a small frozen JSON file. One fixed function reads it and says what to order. No code is written per idea.
- What exists now is the three modules and four data files. There is no runner yet, so no command uses them. They change no existing source file.

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
- **The older words stay.** [Backtest plan](backtest-plan.md) §3 still calls 2015-05..2016-04 the engineering corpus, 2023-01..2025-12 the research corpus and 2026-01..2026-08 the holdout. The lane adds labels inside the first two. It also labels every 2026 date `SEALED` (role `never`). That covers §3's holdout, and also 2026-09 and 2026-10 to 2026-12, which §3 calls exposed and prospective. So no lane pass can use a 2026 date, the lab's own recordings included, until a new split file says otherwise. §3's roles and the month gate are unchanged.

## What is enforced, and what is not

- **Built now.**
  - `Split.label` is the only place that knows the rule. The split file's sha256 is pinned in `tests/test_backtest_lane.py`, with the counts above.
  - `parse_split` takes one byte form only (sorted keys, indent 2, ASCII, one final newline) and refuses an unknown key, ranges that overlap or are out of order, a label used twice, a bad salt or a bad residue.
  - The ledger refuses a pass that names a label that is unknown, `sealed`, `never` or without a budget, or whose split hash is not the hash of the split bytes it was given. It parses those bytes itself. So the budgets it applies are the ones in the file whose hash the entry names, and a split object built by hand is refused (`ledger:split_malformed`).
- **Not built yet.** The runner that gives each record its label, keeps every tally apart by label and never opens a file whose label is not registered. It comes with close-v1. Until then the only gate that runs on data is the old month gate, which seals 2026-01 onward and the exposed months.
- **Limits, stated plainly.**
  - The planned passes read both partitions in one run, and every index record already holds BSP and results. So the split is kept by code paths and tests, not by separate files.
  - A later split file with another salt is refused by nothing in the ledger. The controls are the merged file, its pinned hash, the count tests, and the split hash written into every ledger entry.
  - The 2015-16 year is not fresh. `prior_exposure` says what earlier studies already read from all of it, `PRO-V` dates included.

## The ledger

- **File.** `<ledger dir>/study-ledger-v1.jsonl`. One entry per line, as canonical JSON (one fixed byte form). It holds hashes, whole-number counts and codes: no float, no id, no statistic. Entries are chained: each holds the hash of the one before, so a careless edit, deletion or reorder is seen. The chain has no secret, so it does not show a careful rewrite (see "What it cannot do").
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

- **The three kinds.** A `look` is a blind pass. It shows counts and how spread out the values are (SDs and ranges), with no average and no result. A `registered` entry is the one test on the holdout. `explore` is a pass over explore labels only; the format has it, and no runner planned so far writes it.
- **One entry per pass.** The ledger has two calls for a runner. `check` comes before the read and refuses a spent budget. `record` comes after the read, before any output is written, and appends the one entry. So a pass that stops during the read has written nothing. Both calls take the split file's bytes, not a parsed split.
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
  - Only a lane runner writes to it, and none exists yet. movement-v1, lp-v1 and P0 runs do not call it, so they read `PRO-V` dates and leave no entry.
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

- **The language.** Operators `>=`, `<=`, `==`. One feature against one constant. `when` is a plain AND: no OR, no tick offsets. A feature with no value fails its row. A row's `code` may not be one of `decide`'s own six codes. It can still clash with a code a runner adds, such as `issued`. The runner must refuse that, and none exists yet.
  - `favourite`: the only runner with the lowest `best_back` among priced runners. A tie gives `favourite_tie`; none priced gives `no_priced_active_runner`. This is `movement.favourite`'s rule.
  - `each_active`: every runner in the view (`no_active_runner` when it holds none). Rows on a runner feature are applied per runner: a runner that fails gets no order, and when every runner fails the code is the first runner's.
  - `price`: `{"ref": "best_back"}`, `{"ref": "best_lay"}` or `{"ref": "fixed", "value": "1.01"}`. `side`: `BACK` or `LAY`. `stake`: GBP of backer's stake.
  - `persist`: `lapse` or `keep`. `decide` only hands the pull time and `persist` back; the runner that will walk the orders acts on them. They mean this. The pull time alone says when an order is taken back. `persist` says what the in-play turn does to an order still resting then: `lapse` ends it, `keep` leaves it in the market. So `lapse` with no pull time is an order that rests until the market turns in play. `keep` in a rule file where any time has a pull time could never matter, and the parser refuses it. `keep` is for simulation only: the repository's live surface is LAPSE only.
- **`rulefile.decide(view, rule)`** gives orders and no code, or no order and one code. In this order: `time_not_in_rule`; `ladder_malformed`; the code of the first failing market row (`market_open`, `active_runners`); the selector's code; then the code of the runner's first failing row, or `no_reference_price` when an order's price has no value. It is pure: it reads no file and no clock, draws nothing and keeps no state. `rulefile.py` imports only the standard library, and a test checks that.
- **What `decide` does not check.** The `market` block is parsed and kept, but checking a market against it is the caller's job. So is checking a price against the price ladder, and building the view from data. No code here builds a view.
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
- **Nothing here is wired to anything live.** No other module in the package imports these three (a search on 2026-11-11 found none; no test pins it). They import no gateway, client or network code, and the boundary tests check that. Calling `decide` from live code would need Yu to change AGENTS.md, which keeps simulated order intents inside the backtest package. This PR does not ask for that.

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

## What it changes elsewhere

- Three modules are added to `betangel_research/betfair/backtest/`, and no existing source file changes. The engine certificate covers every module in that directory, so its key moves again. Any later `run-backtest` needs `certify-engine` at the new sources. The boundary test's count of certified files goes from 25 to 28.
- movement-v1 and lp-v1 are untouched. As before, a re-run of either study's registered pass works only from a clean clone at its own commit ([lp study](lp-study.md), operator step 9).
- One dated line in [backtest plan](backtest-plan.md) §13.
