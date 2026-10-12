# Amendment A3: v1 live narrowing, round-3 fixes and the Astra review (binding, 2026-10-10, revision 2)

**Precedence.** This amendment overrides `final.txt` and the earlier `brief.md` amendments wherever they
conflict. Everything not changed here stays as specified. Sources: `final.txt` §9–§13, `brief.md`
("Audit fixes and spec amendments"), `research/betfair-facts.json`, the sealed round-3 findings
(`audit3/sealed-r3.json`, 19 confirmed, numbered R3#0–R3#18 below in file order), the rounds 1–2
list (`audit3/already-fixed-46.json`, numbered 1–46), Astra's review `astra-review-bc2db951.md`
(C1–C4) and S0 review `astra-s0-review-6e540b09.md`. Yu's decision D1–D5 (2026-10-10) is binding.

**Revision 2** answers a three-lens critique (live money, Betfair operations, simplicity; 43 items,
K1–K43, each accepted or rejected in §10). Main changes from revision 1: the kill's A13 reads the
Keychain before the journal head and an anchor failure no longer stops the sweep; the kill always
re-sweeps at engage + 20 s and + 35 s; `send_by`/`send_gate` apply to placeOrders only; settle-market
is typed before the lock, reads nothing it does not need, and no longer trips the same-day startup
reconcile; more live code is deleted (`_cancel_after_kill`, `kill_file_only`, live partial cancel,
the unreported-cancel_all check, `_realized_loss` fallbacks, the kill's registry write).

**Guiding rule.** Fewer live states, fewer live commands, fewer branches. When in doubt, delete live
code instead of guarding it. Never more permissive on live orders. Paper keeps everything (D5).

**Line numbers** refer to the frozen tree (`v1n/base`, identical to `v1n/tree` when this was written).

**Dated edits after revision 2 (2026-10-11).** Every edit since revision 2 is marked "2026-10-11"
(or, for one later follow-up in A18, "2026-10-12") where it applies; the rest of revision 2's text is
unchanged.
(1) **A20 is decided.** Yu decided on 2026-10-11 to keep the conservative behaviour (collab record
`report_94134b37-5c9d-4a98-b1de-9bae35a45a37`). A20 in §8 is rewritten as decided, and the places
that called it open (§3.1, §7, K17) now record the decision.
(2) **Re-review fixes R1 and R2.** Astra's re-review `report_ef3fb90d-3ad9-4150-9b50-4796f91a4c32` was
request_changes with two new P2 regressions and accepted C1–C4 as closed. **R1**: every Keychain anchor
write goes through `credentials.publish_anchor`, which serializes publications and reads the journal head
inside its lock. The kill now publishes the anchor only in `close()`, after its sweep (§4 step 6, A18).
**R2**: a paper market stepped CLOSED keeps a compact closed marker in `paper_state` (schema_version 2)
after its bulky state is dropped (§1 `paper-step` row, R3#12 row).
Where a note and the revision-2 text differ, the note wins. The details are in
`docs/betfair-order-path.md` §7, §9 and §11 and in brief.md "Re-review fixes (2026-10-11)".

---

## 1. Scope table: every CLI command × mode

"Kept" means unchanged in behaviour except for the cross-cutting fixes listed in the last column.
Cross-cutting live fixes: **C1** no hedge credit in R8–R10; **C2** send-by deadline and first-byte
gate (placeOrders only); **X5** exit code 5 for "sent, outcome not as requested"; **SIG** signals
deferred after the first send (R3#16); **H** halts journaled inside the triggering event (R3#13).

| Command | Paper | Live | Notes |
|---|---|---|---|
| `init-config`, `doctor`, `keychain-setup`, `login-reset` | n/a | n/a | Kept (offline). |
| `event-types`, `catalogue`, `book`, `orders-current`, `orders-cleared`, `funds`, `account`, `record`, `verify-recording` | n/a | n/a | Kept (read-only, D1). |
| `place` | Kept | **Kept, changed**: LIMIT + LAPSE only (already enforced by the fixed params, V6 and `_params_match`); funds read before the book; C1, C2, X5, SIG, H. No holder cancel after a kill (step 16 deleted, §4). | |
| `cancel` | Kept (reductions kept) | **Kept, changed**: own bets only (`validate_cancel`, unchanged); **full cancels only** (`--size-reduction` refused, table below); no send-by and no first-byte gate; the capability is minted right before the send (§5b C2); X5, SIG, H; help text fixed (R3#18). | |
| `replace` | **Kept** (paper replace, R3 paper fixes) | **Removed.** Refused before any Keychain read, journal open, lock, login or network. | see refusal below |
| `live-probe` | n/a (`probe_is_live_only`) | **Kept, changed**: place + cancel only. `--replace-step` flag **deleted** from the parser (argparse exit 2 "unrecognized arguments"; nothing runs). C1, C2 (its place), X5, SIG. | T11 dropped. |
| `paper-step` | **Changed**: refuses a book of another market (R3#6); paper state carried in the journal (R3#12). 2026-10-11 (re-review R2): a market the paper state has seen CLOSED keeps a closed marker; later paper orders on it are refused, and later books are ignored except a CLOSED one at or after its close (`paper_market_closed`). | n/a | |
| `paper-replay` | **Changed**: C3, C4, R3#7, R3#8, R3#9. | n/a | |
| `reconcile` | Kept (`paper_has_no_exchange_to_reconcile`) | **Kept, narrowed**: resolves UNKNOWN / ABSENT_UNCONFIRMED only, under the D3 evidence rule (§3.2). No open-order refresh, no close-from-cleared, no settlement, no `stopped_for_kill`. | |
| `reconcile --accept REF` | Kept | Kept (ABSENT_UNCONFIRMED → ABSENT_ACCEPTED after typed `ACCEPT ABSENT <ref>`). | |
| `settle-market` (**new**) | n/a: the command has **no `--mode` flag** (argparse exit 2 on `--mode`); paper markets settle through `paper-step` with a CLOSED book | **New, live only**: the operator settles one CLOSED market by hand (§3.1); the line is typed before the lock. | D3 |
| `journal`, `status` | Kept | Kept; live `status` shows exposure without hedge credit (`exposure_basis: "worst_case_no_hedge_credit"`). | C1 |
| `kill` (own scope) | Kept (file + journal HALT) | **Changed (D4)**: never touches `.gateway.lock`; static arming A4 + the state-root part of A13 only; sweep of our bets (strategy refs ∪ journal betIds; config ref only when the journal is untrusted, which forces exit 3); re-sweeps always at engage + 20 s and + 35 s; journals no order moves; no account-registry write; progress lines; signals → exit 3 (R3#17). `kill_cancels_own_orders` must be true. | §4 |
| `kill --scope account --confirm-account-wide` | n/a | Kept, lock-free (§4 step 9); no "unreported cancel_all" check; a rejected concurrent cancel-all (F31) ends exit 3. | |
| `resume` | Kept | Kept. `--confirm-live` given without `--mode live` is refused (R3#18). | |

**Removed live paths: exact refusal and operator message**

| Path | Where refused | Code / exit | Message (stderr, one line) |
|---|---|---|---|
| `replace --mode live …` | first statement of `cli.cmd_replace` (before argument parsing of prices, `_coexistence`, static arming, `_existing_journal`, Keychain, lock, login) | exit 2, `live_replace_removed_v1` | `Betfair order refused: live_replace_removed_v1: live replace is not available in v1; nothing was sent. To move a bet, cancel it (cancel --mode live --market M --bet-id B) and place a new one (place --mode live ...).` |
| `OrderGateway.replace(...)` with `mode == "live"` (programmatic) | first statement after `_check_source` | `OrderRefused("live_replace_removed_v1")` | — |
| `OrderGateway.open(command="replace")` live | first statement of `_open_live` | `OrderRefused("live_replace_removed_v1")` | — (defence in depth; one line) |
| `RiskGuard.evaluate_replace` with `mode == "live"` | first statement | `OrderRefused("live_replace_removed_v1")` | — (its live-only branches are then dead and deleted, §2) |
| `live-probe --replace-step` | argparse (flag deleted) | exit 2 | argparse's `unrecognized arguments: --replace-step`; HELP for live-probe says "the replace step was removed with live replace in v1" |
| `cancel --mode live … --size-reduction X` | first statement of `cli.cmd_cancel` when the mode is live (before `_gateway`, Keychain, lock, login) | exit 2, `live_partial_cancel_removed_v1` | `Betfair order refused: live_partial_cancel_removed_v1: live cancels are full cancels in v1; nothing was sent. To reduce a bet, cancel it and place a new one.` |
| `OrderGateway.cancel(..., size_reductions=[non-None …])` live | after `_check_source` | `OrderRefused("live_partial_cancel_removed_v1")` | — (live cancel params always carry `"sizeReduction": null`; wire shape and `_params_match` unchanged) |
| `settle-market --mode …` | argparse (no such flag) | exit 2 | argparse's `unrecognized arguments` |
| `orders.live.kill_cancels_own_orders: false` | `config.load_config` | `ConfigError("orders.live.kill_cancels_own_orders_must_be_true")` | kill: `kill_incomplete_config_unreadable`, exit 3 with the website / Bet Angel advice; other commands: their usual config input error |
| any live capability for a replace body | `RiskGuard.issue` (`"replace"` removed from `ACTION_VERBS`) | `OrderRefused("capability_body_mismatch")` | structural: no live replace capability can exist |

**New exit code 5 (X5, R3#15/R3#16).** `5` = "an order request was sent; the command did not end as
asked; do not resend; run `status` / `reconcile --mode live`". Exit codes are now 0, 2, 3, 4, 5
(spec §12 amended). Paper exit codes are unchanged.

---

## 2. D2 removal list (live replace)

Delete outright (no dormant code). Paper replace keeps `_replace_core`'s paper branch,
`risk.evaluate_replace` (paper only, refuses live), `paper.PaperExchange.replace`,
`lifecycle.classify_replace_report`, `schemas.validate_replace_report`, the
`REPLACE_PENDING`/`REPLACED`/provisional-child states and the journal's provisional-child checks.

**`betangel_research/betfair/gateway.py`**

| Line(s) | Item | Action |
|---|---|---|
| 47 | `REPLACE_METHOD` | delete (the string `replaceOrders` then appears nowhere in the package) |
| 53 | `PROBE_REPLACE_PRICE_H` | delete |
| 64, 66, 68, 70 | `"replace"` in `STARTUP_RECONCILE_COMMANDS`, `RESOLVE_ON_OPEN_COMMANDS`, `RISK_COMMANDS`, `PRECONFIRM_COMMANDS` | remove `"replace"`; add `"settle-market"` to `RISK_COMMANDS` and `PRECONFIRM_COMMANDS` (not to `STARTUP_RECONCILE_COMMANDS`, §3.1) |
| 33 | import `replace_echo_line` | delete |
| 39–40 | import `validate_replace_report` | delete |
| 205–242 | `normalise_replace_report` | delete (live only) |
| 738–749 | `_replace_lines` | delete |
| 751–785 | `preconfirm`: `replaces`, `replace_step` params, `"replace"` branch (770–773), `replace_step` in the probe line (775) | delete; add the `"settle-market"` branch (§3.1) |
| 1224–1240 | `replace()`: live branch 1230–1234 | replace with `if self.mode == "live": raise OrderRefused("live_replace_removed_v1")` (before `_require_open`) |
| 1242–1258 | `_replace_live` | delete |
| 1260–1277 | `_refresh_parents` | delete |
| 1279–1420 | `_replace_core`: `probe`, `funds`, `funds_checked_at` params; live prepare/issue 1312–1324; `_clocks_disagree_since` block 1351–1359; exception branch of `journal_report` 1366–1385 (only `_send` raises); `normalise_replace_report` call 1386–1387; live send branch 1404–1411 incl. the `unresolved_replace` halt (1406–1407) and its `_cancel_after_kill` call (1410–1411) | delete; `_replace_core` becomes paper-only (rename not required; docstring says "paper only") |
| 1500–1558 | `probe()`: `replace_step` param and branch 1517–1532 (`target_ref`, `out["replace"]`, `replace_error`), `"replace_step"` in the `probe_result` payload (1553) | delete; cancel only the probe ref |
| 526 | `_open_live` | add first statement `if command == "replace": raise OrderRefused("live_replace_removed_v1")` |

**Other modules**

| File:line | Item | Action |
|---|---|---|
| `presence.py:46–53` | `replace_echo_line` | delete |
| `presence.py:56–60` | `probe_echo_line(..., replace_step)` | drop the param and the `" REPLACE 1.02"` suffix |
| `risk.py:42` | `PROBE_REPLACE_PRICE_H` | delete |
| `risk.py:46` | `"replace"` in `ACTION_VERBS` | delete (issue() refuses any replace decision) |
| `risk.py:488–500` | `evaluate_replace`: first statement `if self.mode == "live": raise OrderRefused("live_replace_removed_v1")`; delete its live-only parts (the `now_monotonic_ns` live default 495–497, the `probe` param and V13 probe exemption 524–533); its calls to the shared helpers `_r2`, `_refs_required` and `_funds_rules` stay (all three return at once outside live and are still used by `evaluate_place`) | live replace evaluation cannot exist |
| `risk.py:616` | `if decision["action"] in ("place", "replace")` | `if decision["action"] == "place"` |
| `risk.py:712–719` | `_params_match` replace branch | delete |
| `arming.py:50` | `"replace": CHECK_IDS` | keep (paper `open()` validates the command name); live is refused in `_open_live` |
| `cli.py:57` | `HELP["replace"]` | `"Paper only: replace own paper bets at a new price; live replace was removed in v1"` |
| `cli.py:273–280` | `replace` parser | keep (paper) |
| `cli.py:284` | `--replace-step` | delete |
| `cli.py:884–899` | `cmd_replace` | first statement refuses live (table in §1); drop the live `confirm`/`preconfirm` |
| `cli.py:902–914` | `cmd_live_probe` | drop `replace_step` |
| `reconcile.py:323–363` | `_child_candidate`, `_child_cleared_candidates` | delete |
| `reconcile.py:417–552` | `reconcile_unknown`: `parent` param, `is_child` branches 502–513 and 530–546 | delete |
| `reconcile.py:705–738` | `refresh_open_orders` (only other caller was `_refresh_parents`; D3 also drops it from `reconcile`) | delete |
| `killswitch.py:87` | docstring "replace children included" | reword |
| `validate.py:27` | `"source_id_key"` in `KILL_TARGET_LEGS` | delete (D4, §4) |

**Tests deleted or rewritten for D2:** see §6 (gateway: `test_replace_live`,
`test_replace_needs_a_replace_gateway`, `test_replace_refreshes_the_parent_before_minting_the_child`,
`test_replace_kill_rechecked_after_submit_journal`,
`AuditRegressionTests.test_lost_replace_child_without_our_refs_is_found_by_key`; reconcile:
`test_replace_unknown_unique_child_inferred`, `test_replace_unknown_ambiguous_keeps_halt`,
`test_stuck_replace_child_can_be_accepted`; killswitch: `test_kill_covers_replace_child_without_strategy_ref`;
presence: replace echo tests; probe tests lose `--replace-step`). `tests/test_betfair_support_orders.py`'s
property harness skips the `replace` op when `mode == "live"` (its `_issue` would now be refused).

## 2b. Other live code deleted by this revision

| File:line | Item | Action | Why |
|---|---|---|---|
| `gateway.py:1109–1118`, call `1043–1044` | `_cancel_after_kill` (spec §9.3 step 16) | delete | the lock-free kill re-sweeps over the in-flight bound; the holder's duplicate cancel only produced BET_TAKEN_OR_LAPSED → UNKNOWN (§4) |
| `gateway.py:840–842`, call `1041` | `_after_outcome`, `CONSECUTIVE_UNKNOWN_HALT` stays as the threshold | delete `_after_outcome` | the halt rides in the report payload (R3#13) |
| `gateway.py:1752–1755`, `cli.py:1000–1005` | `kill_file_only` branches | delete | D4 exit rule; `kill_cancels_own_orders` must be true (`config.py:397` validates `is True`) |
| `gateway.py:1811–1818` | `_cancel_all`'s "unreported cancel_all" journal scan | delete | lock-free it is check-then-act; Betfair rejects a concurrent cancel-all itself (F31); a crashed kill-account no longer blocks every later one |
| `gateway.py:564–574` | `register_account` / `set_account_fingerprint` | skip for `KILL_COMMANDS` | the kill has no A15; an unlocked read-modify-write of `accounts.json` could lose another profile's registration (F76) |
| `gateway.py:248–259, 57, 539–542` | `kill_lock_wait_s`, `KILL_LOCK_MARGIN_S`, the kill branch of `deadline_s` | delete | kill never takes the lock (§4) |
| `gateway.py:509–524, 356, 528–529` | `_kill_arrived`, `_kill_watch` | delete | §4 "What a concurrent lock holder does" |
| `cli.py:1006–1007` | `_journal_view` in `cmd_kill` | delete (static arming gets `journal_anchor={}`, `journal=None`, `journal_state=None`) | the kill's static A13 checks the state root only (§4 step 5) |
| `killswitch.py:26` and `sweep` | `RECENT_SUBMIT_S`, `last_submit_wall_ns` param, the `unresolved` scan, the wall-clock read | delete | re-sweeps are unconditional and measured from engage (§4 step 8) |
| `risk.py:326–333` | `_realized_loss`'s two recompute fallbacks | delete; a missing or invalid `settled_matched_liability_today_pence` / `unsettled_matched_liability_today_pence` returns the daily cap (R10 refuses) | the fold always supplies both; fail closed instead of recomputing (and the SETTLED fallback undercounted an EXECUTABLE → SETTLED remainder) |
| `cli.py:871–881` | live `--size-reduction` | refused (table in §1); live params always `"sizeReduction": null` | D1 needs full cancels only |

---

## 3. D3 design: no automatic live settlement

### 3.1 New command `settle-market`

```
python3 -m betangel_research.betfair settle-market --market ID --confirm-live TOKEN --bet-angel-paused
```

Live only: the parser has no `--mode`. `--bet-angel-paused` is **required** while
`coexistence.bet_angel == "unverified"` (the default until first-live-test T3 sets
`token_survival_verified` or `not_running_on_account`); without it `_live_gateway` → `_coexistence`
refuses first with exit 2 `bet_angel_coexistence_unverified`. HELP and docs say so.

**Arming** (`arming.COMMAND_CHECKS["settle-market"]` = the `reconcile-accept` set): A1, A2, A4, A5,
A8, A10, A11, A12, A13, A14 (login only; `can_bet` not required → add to `LOGIN_ONLY_COMMANDS`),
A15. Not A7: allowed while halted or killed (it sends no order). Normal (non-emergency) login. In
`RISK_COMMANDS` (open refuses unless the Keychain anchor was written first) and `PRECONFIRM_COMMANDS`
(typed before the lock). **Not** in `STARTUP_RECONCILE_COMMANDS` and not in `RESOLVE_ON_OPEN_COMMANDS`:
settle books the worst case of each whole order whatever the exchange shows, and foreign bets are not
in the journal either way, so a startup pass would protect no money (§10 K26/K35).

**Flow.**

1. **Typed before the lock** (`gateway.preconfirm("settle-market", market_id=M)`, run by
   `cmd_settle_market`'s `confirm` callback after static arming, before `open()`): fold the journal
   (`self.journal.fold`, read-only), apply **P0** below to that fold (refuse before prompting), build the
   listing (one row per order of ours in M: ref, betId, side, selection, price, size, matched, remaining,
   state, `settled_worst_loss_pence`) plus `L_M` (no credit) and `Σ w`, and show the banner with
   `self.confirmer.confirm_line(f"SETTLE MARKET {M}", banner=banner)`. Keep `(line, shown_sha256)`,
   `shown_sha256` = canonical sha256 of the listing. A mistyped line raises
   `OrderRefused("operator_echo_mismatch")` as every other prompt (no journal write).

   ```
   MODE=LIVE: SETTLE MARKET BY HAND. No order is sent.
   Do this only after the market has settled: check this market's settled bets on the Betfair website
   (or in Bet Angel) first.
   After you type, settle-market checks that Betfair shows the market CLOSED and none of our bets in it
   as current; if not, it refuses and nothing is settled.
   Our orders in this market (journal):
     <ref> bet <betId|none> <BACK|LAY> sel <id> @ <price> size <s> matched <m> remaining <r> <state> worst-case loss <w>
     ...
   Worst-case exposure counted now in this market (no hedge credit): <L_M as £x.xx>
   Booked as today's (UTC) realised loss for the daily cap: <Σ w as £x.xx> (each order's worst case
   added up; it can exceed the line above)
   Orders the kill cancelled, or whose cancel report was lost, are still counted here and are booked at
   worst case.
   Betfair's settled P&L is not used: see orders-cleared or the Betfair website.
   Settling stops counting these orders as open exposure. It cannot be undone.
   ```
   (`confirm_line` prints the `Type exactly: SETTLE MARKET <M>` line itself; the banner does not repeat it.)
2. `settle_market(M, *, confirm_token)` after `open()` (under `.gateway.lock`, which no prompt now
   holds): `_require_open("settle-market")`, `_require_armed()`, `token_matches` (else
   `NotArmed("confirm_token_mismatch")`); `basis = self.journal.anchor()` then `state = self._state()`.
3. **P0 journal** on `state`: `ours` = orders with `market_id == M` and state ∉ {RECORDED, REFUSED}.
   - none → `OrderRefused("settle_market_no_orders_in_market")`;
   - any order in `UNRESOLVED_STATES` (SUBMITTING, UNKNOWN, ABSENT_UNCONFIRMED, CANCEL_PENDING,
     REPLACE_PENDING) or a non-terminal provisional child → `settle_market_unresolved_orders`
     (`detail.refs`); the operator runs `reconcile --mode live` first;
   - none in {EXECUTABLE, EXECUTION_COMPLETE} → `settle_market_nothing_to_settle`;
   - the listing rebuilt from `state` has another sha256 than the typed one →
     `settle_market_journal_changed` (nothing settled; run the command again).
4. **P2 market CLOSED**: `_market_status(M)` (new, gateway; one `read.list_market_book([M],
   price_data=())`, returns the status only): read error → `settle_market_book_read_failed`
   (`detail.code`); no row → `settle_market_book_missing` ("Betfair no longer returns this market; see
   docs: its worst case stays counted"); more than one row or `status != "CLOSED"` →
   `settle_market_not_closed` (`detail.status`).
5. **P3 none of our bets current in M**, two legs, both must succeed and return no item:
   `read.iter_current_orders(customer_strategy_refs=<config ref ∪ strategy_refs_seen, batched>,
   market_ids=[M], order_projection="ALL")` and `read.iter_current_orders(bet_ids=<journal betIds of
   ours in M, batched ≤ MAX_IDS_PER_REQUEST>, order_projection="ALL")`. Read error →
   `settle_market_current_read_failed`; any item → `settle_market_bets_still_current` (`detail.count`,
   `detail.bet_ids`; message: "check these bets' status on the Betfair website or in Bet Angel and
   settle again once Betfair has settled them"). **Premise, inferred (new F77):** listCurrentOrders
   stops listing a bet once its market is settled; first-live-test T10 is extended to observe it.
6. **Journal** one `settle` event via `_append` (not `_append_moves`, so a fold refusal raises and
   nothing is settled) with `expect_head = basis`: payload `{"basis": "operator_settled_worst_case",
   "echo": "SETTLE MARKET <M>", "shown_sha256": ..., "market_status": "CLOSED", "commission_p": 0,
   "transitions": [...]}`, `market_id` column = M. A moved head (a concurrent kill's events included)
   → `settle_market_journal_changed` (nothing settled; retry). No separate `operator_confirm` event: the
   typed line rides in `settle` (`_check_payload` accepts `echo`, `shown_sha256`, `basis`,
   `market_status` on `settle`). Transitions, reason `operator_settle_market`, fields
   `{"settled_profit_own_p": null, "settled_profit_betfair_p": null}`:
   - EXECUTION_COMPLETE → SETTLED;
   - EXECUTABLE → SETTLED (**new transition**, `lifecycle.TRANSITIONS["EXECUTABLE"]` gains
     `"SETTLED"`; `size_remaining_p` is kept as recorded: we do not know whether it matched).
   Then `_clear_reconciled_market_halts()` (existing).
7. **Result** (exit 0): `{"mode": "live", "status": "settled", "market_id", "orders": [_order_view...],
   "worst_case_exposure_before_pence", "booked_realized_loss_pence", "journal_seq"}`. Refusals exit 2
   (`Betfair order refused: settle_market_*`).

No `listClearedOrders` read, no runner results and no `informational` section: D3 does not ask for
Betfair's P&L, and `orders-cleared` already shows it (§10 K33).

**What exposure becomes.** Every settled order is terminal: `exposure_basis` none, `_adverse_size` 0,
`exact_contrib` 0, so `L_M` → 0 and the market leaves R8/R9. **Realised P&L for R10 is the worst
case**: `journal._settled_profit` falls back to `-settled_worst_loss_pence(order)` where (new, in
`journal.py` beside `matched_worst_loss_pence`, 534–542)

```
settled_worst_loss_pence(o) = matched_worst_loss_pence(o) + (o["size_remaining_p"] if BACK
                              else ceil(o["size_remaining_p"] * (o["price_h"] - 100) / 100))
```

used both in `_settled_profit` (545–553) and for `settled_liability` (state() 715–718). Paper SETTLED
orders always have remaining 0, so paper is unchanged. Live P&L is informational only: no code path
writes `settled_profit_*` for live any more. Effects, stated in docs §8 and the banner:
- `Σ w` (each order's worst case added up) can exceed `L_M` (the market's no-credit worst case counted
  before), e.g. a BACK and a LAY on the same runner; so `daily_used = realised + Σ L_m` can **rise**
  when a market is settled. `Σ w` is kept on purpose (§8 A19).
- `max_daily_loss` therefore caps the day's settled worst cases (with the £20 default, about ten £2
  BACKs settled per UTC day whatever their results).
- The loss is booked on the settle event's UTC day: settle a market on its own UTC day.
- An unmatched bet that lapses counts at its worst case until you settle the market and is then booked
  as a loss for that day's cap; cancelling it yourself before the off frees it only if the cancel
  report reached the journal. **Orders the kill cancelled, or whose cancel report was lost, stay
  EXECUTABLE at worst case** (the kill journals no moves; a later manual cancel gets
  BET_TAKEN_OR_LAPSED → UNKNOWN → withheld EXECUTABLE) and are booked at worst case when you settle.
- Settle soon after the market is CLOSED: how long listMarketBook keeps returning a CLOSED market is
  unverified (F77). A market Betfair no longer returns cannot be settled in v1 and keeps its worst case
  counted indefinitely (§8 A20; decided by Yu on 2026-10-11: kept as is).

The startup reconcile's `exchange_realized_today_pence` (exchange floor) stays: R10 already uses the
worse of journal and exchange, so it can only tighten.

**Same-day startup reconcile after a settle (no false contradiction).** Without refresh, a resting
order that matched later keeps a stale `size_matched_p`, and settle-market moves it to SETTLED with it;
the next live open that UTC day reads a cleared SETTLED row whose `sizeSettled` exceeds it. In
`reconcile.startup_reconcile` (661–665) the comparison bound for a journal order in SETTLED becomes
`size_matched_p + size_remaining_p` (the size whose worst case was booked; live SETTLED now only comes
from settle-market): `bound = 0 if state in _ZERO_MATCHED_STATES else size_matched_p + (size_remaining_p
if state == "SETTLED" else 0)`. A `sizeSettled` above that bound is still a contradiction. Regression
test `test_settle_market_then_place_same_day_arms` (§6).

### 3.2 UNKNOWN resolution and the live evidence rule

**Rule (binding).** In live mode, exchange evidence (current-order rows, cleared rows) may establish
that a bet exists (record its betId) and raise the known matched size; it never lowers an order's
counted worst case and never moves an order to a terminal or zero-exposure state. Only three things
lower live exposure: (a) a report of an order request this process sent (place / cancel report,
`lifecycle.classify_*`), (b) `reconcile --accept` (ABSENT_UNCONFIRMED → ABSENT_ACCEPTED, typed),
(c) `settle-market` (typed). Since `reconcile.py` is live-only (paper never calls it), the rule is
implemented in `reconcile.py` itself:

- **`found_move(item, order)`** (165–203) keeps its terms and size-identity checks. Then let
  `known = size_cancelled_p + size_lapsed_p + size_voided_p` of the journal order and `seen` = the same
  sum on the exchange row. If `seen > known` (the exchange reports cancelled, lapsed or voided size
  the journal did not know), return `("EXECUTABLE", fields)` with the journal's cancelled/lapsed/voided
  sizes, `size_matched_p = max(journal, exchange)`, the exchange average when matched > 0,
  `size_remaining_p = size_p − size_matched_p − known` (**≥ 0**; it is 0 when the journal's matched size
  exceeds the exchange's, e.g. Betfair voided part of a matched bet — that is a valid `matched_exact`
  EXECUTABLE record, never an assert or a None), and `error_code =
  "exchange_reduction_withheld_until_settle"`. Otherwise return the exchange state (EXECUTABLE, or
  EXECUTION_COMPLETE when matched + known == size). The `LAPSED` return (199–201) is deleted.
- **Cleared rows** (`cleared_move`, 258–320) are replaced by `_cleared_bet_id(order, hits)`: rows
  (any of the four statuses, read by the unchanged `cleared_hits`) that name exactly one valid betId,
  equal to the order's betId when it has one, give `("EXECUTABLE", {"bet_id": id, "error_code":
  "cleared_row_bet_exists_worst_case"})` (sizes untouched; `transition()` keeps the unaccounted size
  as remaining). Inconsistent rows → `_still_unknown("cleared_rows_inconsistent")`; no rows →
  continue to the absent path as today. No SETTLED, VOIDED or LAPSED conclusion exists any more.
- **Unchanged:** polling window and monotonic pacing, read error never "not found", ref-independent
  search incl. the sourceIdKey leg (only halts), `matched_size_known_never_absent`,
  ABSENT_UNCONFIRMED only after the full clean pass, `--accept` needing a clean startup and a final
  clean pass.

**Live lifecycle after D2/D3** (shared `TRANSITIONS` table keeps paper's edges; these live writers
are gone, so the edges are unreachable in live — no fold-level guard is added, see §8 A10):

| Removed for live (no writer) | Why |
|---|---|
| EXECUTABLE → REPLACE_PENDING; REPLACE_PENDING → *; REPLACED → *; provisional child intents | D2 |
| EXECUTION_COMPLETE / REPLACED → VOIDED | `settle_from_cleared` deleted |
| EXECUTION_COMPLETE → SETTLED from cleared rows | replaced by `settle-market` (operator) |
| UNKNOWN / ABSENT_UNCONFIRMED → LAPSED | evidence rule |
| UNKNOWN / ABSENT_UNCONFIRMED → EXECUTION_COMPLETE with any reduction the journal did not know | evidence rule (lands in EXECUTABLE) |
| EXECUTABLE → EXECUTABLE/EXECUTION_COMPLETE/LAPSED by refresh | `refresh_open_orders` deleted |
| ABSENT_UNCONFIRMED → UNKNOWN by a kill-sweep cancel | kill journals no moves (§4) |
| CANCEL_PENDING → EXECUTABLE with a partly reduced remainder from a live partial cancel | live partial cancel removed (§1) |

Live edges that remain: RECORDED → REFUSED | SUBMITTING; SUBMITTING → EXECUTABLE | EXECUTION_COMPLETE |
FAILED | UNKNOWN | REFUSED; EXECUTABLE → CANCEL_PENDING | SETTLED (operator); CANCEL_PENDING →
EXECUTABLE | EXECUTION_COMPLETE | UNKNOWN; UNKNOWN → EXECUTABLE | EXECUTION_COMPLETE | ABSENT_UNCONFIRMED
| UNKNOWN; ABSENT_UNCONFIRMED → EXECUTABLE | EXECUTION_COMPLETE | ABSENT_ACCEPTED; EXECUTION_COMPLETE →
SETTLED (operator). Added globally: **EXECUTABLE → SETTLED** (paper never writes it).

### 3.3 `reconcile` (live) after D3

`gateway.reconcile()` (1569–1635) keeps: `_require_open`, `_require_armed`, `--accept` → `_accept`,
paper early return, `_resolve_unknowns(budget_s=OPEN_RECONCILE_BUDGET_S, states=("UNKNOWN",
"ABSENT_UNCONFIRMED"))`, `_clear_reconciled_market_halts()`. Deleted: both `stopped_for_kill`
returns (1597–1598, 1620–1621), refresh (1599–1608), close-from-cleared (1609–1619), settlement
(1622–1633), `_settle_since` (1637–1650). Result keys: `mode, status ("reconciled"), reconciled[...],
market_halts_cleared, journal_seq`. In `reconcile.py` delete `close_from_cleared` (741–757),
`_runner_results` (760–773; settle-market reads the status only, through the new gateway
`_market_status`), `_hundredths` (776–783), `_pnl_mismatch` (786–802), `settle_from_cleared`
(805–889) and the now-unused imports (`compare_profit`, `order_pnl_pence`, `matched_worst_loss_pence`,
46–48). Update the module docstring (1–37).

---

## 4. D4 design: kill without the gateway lock

**Kill flow (both scopes), step by step**

1. `cmd_kill`: `paths.ensure()` (failure noted, never fatal) — unchanged.
2. `killswitch.engage(paths, reason)`: create `STATE_ROOT/KILL` with `O_CREAT|O_EXCL|O_NOFOLLOW`
   0600, `full_fsync` the file, `fsync` the directory. No lock, config, login or read — unchanged
   (`killswitch.py:35–74`). **T0** = `clock.monotonic_ns()` taken right after `engage` returns (also
   when the file was already there); it is passed to `gateway.kill(..., engaged_monotonic_ns=T0)`.
   stderr at once: `Betfair kill: KILL file engaged; no gateway sends a new order from now on. If this
   kill does not end with "kill complete", cancel open bets on the Betfair website or in Bet Angel.`
3. Journal `kill` + `halt` into an existing non-empty live journal — unchanged (`cli.py:981–994`).
   **Verified, not assumed** (claim corrected in revision 2): `journal.append` runs `BEGIN IMMEDIATE`
   (`journal.py:418`), reads the head inside that transaction (421), catches the fold up inside it
   (430, `_committed_folder`) and commits before returning; the connection has `busy_timeout=30000`
   (264) and `timeout=30` (247). `fold()` does not seed `_fold_cache`, so **each connection's first
   append folds the whole journal inside its write transaction**: a kill append can wait for another
   connection's whole-journal fold plus commit, up to the 30 s busy timeout. Appends from several
   processes still cannot fork the chain. Nothing on the kill path depends on append latency any more:
   the cancel capability is minted after the appends (step 8). Test: §6 journal.
4. Config: `orders.live.kill_cancels_own_orders` must be `true` (`config.py:397` validates it;
   `false` → `ConfigError`, the kill ends `kill_incomplete_config_unreadable`, exit 3 with advice).
   The `kill_file_only` status and both branches are deleted (§2b).
5. **Static arming**: A4 (account scope adds A11) and **only the state-root part of A13**
   (`state_root_invalid`): for `kill`/`kill-account`, `arming._static_check("A13")` reads no Keychain
   anchor and no journal head. `cmd_kill` no longer calls `_journal_view`. (Every other command's static
   A13 now reads the Keychain anchor **first** and the journal head after it — `static_arming` calls
   `journal.anchor()` itself after `anchor_read` whenever a journal object is given — so a concurrent
   appender can only leave journal ≥ Keychain and no false `journal_anchor_mismatch` exists, §8 A18.)
6. **Open without the lock.** `_open_live` for `command in KILL_COMMANDS` skips
   `file_lock(paths.gateway_lock, ...)` (549–550) entirely; `_lock` stays `None`; nothing ever waits on
   `.gateway.lock`. Then: chain verify; the **one** anchor evaluation of the kill,
   `_keychain_anchor_ok` (Keychain read first, then the journal head — no false mismatch against a
   concurrent holder); emergency login; **no account-registry write** (`register_account` and
   `set_account_fingerprint` skipped for KILL_COMMANDS: the kill has no A15); fold or stand-in; anchor
   write. (2026-10-11, re-review R1: the kill no longer writes the anchor here. It publishes it once, in
   `close()` after the whole sweep, through `credentials.publish_anchor`, and waits at most
   `killswitch.JOURNAL_BUSY_TIMEOUT_S` (2 s) for another publisher. If it cannot publish, it prints a
   stderr warning; no cancel is stopped or delayed.) **Untrusted journal:** a broken chain, an unfoldable journal or a failed anchor check never
   stops the sweep. The gateway sets one flag `_journal_untrusted` (replacing `_chain_broken` and
   `_journal_unfoldable`), sweeps with the stand-in state (`_unfoldable_state()`: no orders, no
   `strategy_refs_seen`, so the config strategy ref only — bets that are certainly ours), journals a
   `note` `journal_untrusted_sweep_uses_config_strategy_ref` when the journal is writable, writes **no**
   Keychain anchor (`_write_anchor` already refuses on the flag), and the kill ends `kill_incomplete`
   (exit 3, advice) whatever the sweep finds, because our bets cannot be fully enumerated (D4).
7. **Session.** Emergency login (`EMERGENCY_COMMANDS`): skips the successful-login cap, never the
   persisted ban or the consecutive-failure cap; the login-ledger lock (≤ `LEDGER_LOCK_TIMEOUT_S` 10 s)
   is held only for the reservation write (`auth.py:425–439`), never across the HTTP login. No A12 /
   `--bet-angel-paused` is needed. Progress on stderr before each phase:
   `Betfair kill: reading credentials from the Keychain; if macOS asks, click Allow (not Always Allow)`
   (F73, unverified: an item created with `-T ""` may prompt on every read; each read waits up to
   120 s), then `Betfair kill: logging in (emergency session)`. **F67 line only while
   `config["coexistence"]["bet_angel"] == "unverified"`**: `Betfair kill: F67 (inferred, unverified):
   if a new login ends other sessions, Bet Angel and any running gateway may be logged out; check Bet
   Angel after the kill.` The kill JSON carries `"session": {"login": "emergency", "coexistence":
   <config value>}`. Login failure → `kill_incomplete_no_session`, exit 3 (unchanged); when the login
   ledger refused (ban or consecutive-failure cap), the exit-3 line adds `; the login ledger refused
   (<code>): check why logins failed, then login-reset`.
8. **Sweep** (`gateway.kill` → `killswitch.sweep`):
   - Target set = **our bets only**: leg `strategy_ref` (`listCurrentOrders(customerStrategyRefs=<config
     ref ∪ strategy_refs_seen>, orderProjection="EXECUTABLE")`) ∪ leg `bet_id`
     (`listCurrentOrders(betIds=<journal non-terminal betIds>, orderProjection="EXECUTABLE")`); an
     untrusted journal leaves the config ref only. The `source_id_key` leg (`killswitch.py:131–140`) is
     **deleted** from the kill, and `"source_id_key"` from `validate.KILL_TARGET_LEGS`: a bet without
     our strategy ref and unknown to our journal is never cancelled except by the explicit
     account-wide scope. (`placed_with_key` stays for `startup_reconcile` and `reconcile_unknown`,
     where it only halts.)
   - Every cancel goes through `_sweep_cancel` with the **no-moves stand-in state**
     (`fold_events([])` with `halted=True`), so the kill journals `cancel_intent` (with `kill_targets`),
     `submit` {action cancel, transitions []} and `report` {report, transactions, transitions []}:
     **the kill never moves an order**; the journal keeps counting our bets at their worst case until
     an order report, `reconcile --accept` or `settle-market` says otherwise. Cancels carry **no
     send-by and no first-byte gate**, and their capability is minted inside `_send`, after the
     `cancel_intent` and `submit` appends, immediately before `send_order` (§5b C2): a slow journal or
     a slow reconnect never expires a kill cancel.
   - **Re-sweeps, unconditional.** Passes at T0 + `RESWEEP_OFFSETS_S = (20, 35)` s on the monotonic
     clock (a pass whose time has already passed runs at once; no wait exceeds 35 s). `RECENT_SUBMIT_S`,
     `last_submit_wall_ns`, the unresolved-order scan and every wall-clock read are deleted.
     **Why this covers the longest in-flight order:** with C2, no gateway writes the first byte of a
     placeOrders request after the KILL file exists (the gate runs immediately before
     `conn.endheaders(payload)`, which writes the whole small request), so every place request's bytes
     were handed to the kernel by T0. Betfair may take the order call's 5 s timeout
     (`ORDER_TIMEOUT_S`, F28) plus 15 s for the order to appear (`ORDER_VISIBILITY_S`, F28) plus an
     in-play bet delay of up to 12 s (new `constants.IN_PLAY_DELAY_MAX_S = 12`, tagged F42: V9 refuses
     in-play only on a book up to `max_book_age_s` old, so the market can turn in-play in between), i.e.
     32 s; the 35 s pass leaves a 3 s margin for transit. A module-level check in `killswitch.py`
     enforces `RESWEEP_OFFSETS_S[-1] >= ORDER_TIMEOUT_S + ORDER_VISIBILITY_S + IN_PLAY_DELAY_MAX_S + 3`
     and `RESWEEP_OFFSETS_S == tuple(sorted(RESWEEP_OFFSETS_S))`. **Residual (documented, printed on
     exit 0):** a request whose bytes TCP retransmits late (`_drop()` closes, it does not abort), or a
     process descheduled between the gate and the write, can still reach Betfair after the last pass.
   - Progress on stderr: `Betfair kill: pass N of 3; next pass in S s (orders sent just before the kill
     can still appear; bound 35 s after the KILL file)` and `Betfair kill: final check` (R3#17).
   - Verify pass (`orderProjection="EXECUTABLE"`); `kill_complete` iff no remaining EXECUTABLE bet of
     ours, no read failure and a trusted journal. On exit 0, stderr: `Betfair kill complete: none of our
     bets is current on the final check. A request delayed in the network can still arrive later: if an
     order was being sent when you killed, check Bet Angel or the website.`
   - **Worst-case duration (computed from code constants, not measured).** Keychain: 3 reads (4 with a
     certificate passphrase) × up to 120 s each, only while a macOS dialog is left unanswered; ledger
     lock ≤ 10 s; login ≤ about 2 × `http.timeout_s`; each read ≤ 3 attempts × `timeout_s` 10 s + 1 s +
     4 s backoff ≈ 35 s plus one relogin; 4 passes × 2 legs (≤ 250 refs and ≤ 250 betIds) = 8 reads ≈
     280 s; each cancel ≤ `order_deadline_s` (10 s, ceiling 12 s); the re-sweep waits overlap with the
     reads. Defaults, dialogs answered, every read failing twice: about 6 minutes; with three
     unanswered dialogs: about 12 minutes. Typical (reads succeed first time): about 40 s, set by the
     35 s pass. The start-of-kill advice line tells the operator they may cancel on the website at once.
9. **Account-wide** (`--scope account --confirm-account-wide`, typed phrase): runs after the own sweep,
   still lock-free. The journal scan for an unreported `cancel_all` is **deleted** (§2b). Per F31
   (documented), a concurrent account-wide cancel is rejected or answered with `ERROR_IN_MATCHER`:
   that kill's `account_wide.status != "SUCCESS"`, so it ends `kill_incomplete`, exit 3 with the advice,
   and the operator re-runs `kill` or checks the website. It is not "harmless"; it is fail-closed.
10. **Exit:** 0 only if `status == kill_complete` and the KILL file is engaged; otherwise exit 3 with
    `Betfair kill incomplete: <reason>; <file state>; cancel open bets on the Betfair website or in
    Bet Angel now` and HALT `kill_incomplete`. SIGINT/SIGTERM/SIGHUP or KeyboardInterrupt anywhere
    after step 2 → `kill_incomplete_interrupted`, JSON + exit 3 + the same advice (R3#17). The kill
    does **not** defer signals after a send (the operator keeps Ctrl-C).

**KILL re-check right before each place submit (send path).** Place and probe pass `send_by` and a
`send_gate` to `JsonRpcClient.send_order` → `HttpTransport.request` (cancels pass neither). Inside
`_send`'s `_signals_blocked()` section, the transport evaluates the gate **after the connection is
established and immediately before `conn.endheaders(payload)`** (`transport.py:470`;
`putrequest`/`putheader` only buffer, so no request byte has been written): the gate returns
`"kill_engaged_before_send"` when `kill_engaged(paths)` is True (fail-closed), else
`"book_stale_before_send"` when `_book_age_ok(book)` fails, else None. A refusal raises
`TransportError(phase="send", code=..., may_have_arrived=False)` with zero bytes written →
FAILED(`transport_not_sent`), exception code journaled. The existing step-10 check after the submit
journal (`gateway.py:996–1002`) stays.

**What a concurrent lock holder does** (it is never waited for or signalled):
place/probe before its decision → R1 `halted`; between decision and submit → compare-and-append fails
(`journal_changed_since_decision`, the kill's `kill`/`halt` moved the head); after the submit journal →
step 10 REFUSED or the first-byte gate FAILED; bytes already sent → its report is journaled and the
kill's re-sweeps cancel the bet. **Step 16 (`_cancel_after_kill`) is deleted**: its cancel duplicated
the kill's and its BET_TAKEN_OR_LAPSED left an UNKNOWN that blocked R3/A17. A manual `touch KILL` only
stops new sends; `kill` cancels (docs). **A place or live-probe holder stops reconciling once the
KILL file exists**: `_resolve_unknowns` and `_reconcile_refs` return early when
`self._command in RESOLVE_ON_OPEN_COMMANDS and kill_engaged(self.paths)` (A7 guaranteed no KILL at
their open, so any KILL is new; the orders stay UNKNOWN at worst case). This one condition keeps such a
holder from competing with the kill for the 3-request concurrency budget (F18) for up to
`OPEN_RECONCILE_BUDGET_S`. A holder `cancel`, `reconcile` or `settle-market` simply finishes (allowed
while killed). Documented overlap: (a) F18's 3-request concurrency limit is per account and shared with
Bet Angel and that holder, so a kill read can draw TOO_MANY_REQUESTS (3 attempts, then exit 3);
(b) if F67 is false (tied to T3), the kill's emergency login can end the holder's session and the
holder's relogin can end the kill's: the kill's reads then fail and it exits 3. The rest of the
"give way to a kill" machinery is **deleted**: `_kill_watch` (356, 528–529), `_kill_arrived`
(509–524), `reconcile_unknown(abort=)` (456–461, 479–480), `reconcile_abandoned_for_kill`,
`_reconcile_refs`'s `reconcile_deferred_for_kill` note (1059–1061), `reconcile()`'s `stopped_for_kill`.

---

## 5. Mapping of every round-3 finding

| # | Sev | Finding (short) | Disposition |
|---|---|---|---|
| R3#0 | major | Kill lock wait shorter than a slow/flaky holder; `kill_incomplete_no_session`, sweep never runs | **fixed by D4** (kill never takes the lock) **and A18** (the kill's static A13 reads no anchor; its one anchor check reads the Keychain first; an anchor failure degrades to the untrusted-journal sweep, never to no sweep). Revision 1 called this "moot"; the A13 read order was a second route to the same harm. Regression tests required (§6 killswitch). |
| R3#1 | minor | Replace child stuck UNKNOWN (loose candidate) | **moot_by_D2** (live replace deleted; paper replace reports are complete, paper never reconciles). |
| R3#2 | major | Replace partial match discards the new betId; child UNKNOWN forever | **moot_by_D2**. Regression test: live replace refused before lock/login/network. |
| R3#3 | major | `cleared_move` VOIDED equality vs stale journal; stuck exposure / stuck UNKNOWN | **moot_by_D3**: `cleared_move` and `close_from_cleared` deleted; any cleared row now only establishes the bet (UNKNOWN → EXECUTABLE worst case), and `settle-market` closes it; the same-day startup reconcile bounds a SETTLED order by matched + remaining (§3.1), so the lockout does not move to after settle-market. Regression tests: VOIDED/SETTLED/LAPSED rows never move a live order to terminal; the UNKNOWN is not stuck; settle then same-day open is not halted. |
| R3#4 | minor | Rule 4 / dead heat false `pnl_basis_mismatch` halt | **moot_by_D3** (`settle_from_cleared`, `_pnl_mismatch` deleted; P&L informational). |
| R3#5 | minor | `priceReduced` ignored (Rule 4) | **moot_by_D3** (same). F37 code_handling updated. |
| R3#6 | major | `paper-step` accepts another market's book | **fix**: `OrderGateway.advance` refuses `book["market_id"] != market_id` with `OrderRefused("paper_book_market_mismatch")` before any fold, `paper.advance`, settle or `_keep_paper_book`; `PaperExchange.advance` asserts `prev_book` and `book` share a market id (ValueError). Test. |
| R3#7 | minor | An empty segment (`lines 0`) disables the replay gap guard | **fix**: rewrite `_receipt_windows` (paper.py 690–711): a listed segment with `lines == 0` / null `first_pt` is an empty window (skipped); a path is "unknown" only when no receipt lists it. All paths known → windows; all unknown → None with honest labels (`recording_gaps_unknown_no_receipt` / `recording_gaps_unknown_segment_not_in_receipt`); mixed → `ValueError("recordings_mix_known_and_unknown_gap_windows")`. Test. |
| R3#8 | minor | Gap check uses the union of windows, not the activation book's window/market | **fix**: windows carry the segment's `market_ids` (recorder.py:269). For an order on M, the covering windows are those whose `market_ids` contain M; activation is refused (`paper_book_in_recording_gap`) unless `t_a` and the activation book's timeline pt fall in the **same** covering window; a passive-fill interval needs `prev_pt` and `pt` in the same covering window. Both repros as tests. Residual (a market dropped mid-segment by an explicit-mode refresh) bounded by `max_snapshot_age_s`; documented. |
| R3#9 | minor | Passive-fill activation order lost across processes | **fix**: `export_state` adds `"activation": {ref: seq}` and `"sequence": n`; `_load_state` accepts them (absent → empty, old states still load); `advance` sorts by the persisted sequence. Test with a process boundary. |
| R3#10 | minor | S0 procedure omits three doc lines + brief Status | **fix** (§7): `merge_gate.s0_commit` and brief "S0 patch and merge gate" list `docs/betfair-integration.md` (the `.gitignore` *.crt line and the "未改" line), `docs/betfair-order-path.md` "未做" S0 sentence and the brief Status line as rewritten in the S0 commit; `s0_state_problems` (s0_applied true) refuses `未套用`/`未改`/`S0 會補上` wording in `docs/betfair-*.md` and brief Status. |
| R3#11 | minor | Fact tiers: F61 unrecorded reader-proxy side; F74 local check; (F62 refuted) | **fix (2),(3)**: F61 drops the unrecorded conflict claim → `partially_unverified`, value says "a conflicting copy was seen during research but its URL was not recorded; not relied upon"; F74 retrieval lists `local_python_check` too and the summaries (report md:19, handoff `limitations[1]`) count it ("2 local checks: F69, F74's SQLite part"). **(1) no change** (spec-mandated, refuted). |
| R3#12 | major | Paper fills and `paper-state.json` written non-atomically | **fix**: the paper state rides in the same journal event as the fills. Every paper event that changes paper exchange state (place/cancel/replace `report`, `advance` report/lapse/`settle`, and a `note` `{"reason": "paper_advance_state"}` when an advance changes state without moves) carries payload `"paper_state": {"schema_version": 1, "queues", "books", "exchange"}` (2026-10-11, re-review R2: schema_version 2 adds `"closed": {"markets", "floor_ms"}`, the closed-market markers; version 1 is still read). `open()` loads the latest `paper_state` from the journal; `paper-state.json` is read only when no journal event carries one (legacy) and is no longer written (`_save_paper_state` 434–440 deleted with its calls at 1049, 1219, 1419, 1493 and in `close()` 653–654). SIGHUP handled like SIGTERM (`_SignalGuard`, `_signals_blocked`). Test: report journaled, process object dropped without `close()`, reopen → takes and high-water marks intact (no double fill). |
| R3#13 | minor | HALTs journaled after the state change can be lost | **fix, one mechanism**: (1) `_report_halts` returns reasons that ride in the same `report` event as `"halts": [...]`, and `risk_decision` carries `detail.halt` the same way; the fold applies payload `halts` (global, with the event's seq); `_check_payload` validates the list. (2) `consecutive_unknown_outcomes` joins the same list: before journaling a live report whose moves send any order to UNKNOWN, the gateway computes `count = fold_before["consecutive_unknown"] + 1` from the fold it already took before the send (`submitting` in place, the post-submit fold in cancel; only `report` events with moves change the count, and the kill's reports have none) and adds the halt when `count >= CONSECUTIVE_UNKNOWN_HALT` (3) — on **every** such report, not only the third, so a 4th UNKNOWN after a resume halts again (resume does not reset the count). `_after_outcome` is deleted; the fold gets no mode-specific rule. (3) `pnl_basis_mismatch`: moot_by_D3; `unresolved_replace`: moot_by_D2. SIGHUP added (R3#12). |
| R3#14 | minor | RECORDED orphan (provisional child) counted forever | **moot_by_D2** for live; **fix** for paper (same code): `_stale_to_unknown` (448–455) also journals every RECORDED order as REFUSED(`crash_before_submit`) at `open()` (safe: submit is journaled before any byte; gateways exclude each other by lock; the kill never journals intents). |
| R3#15 | major* | Live exit 0 / `"accepted"` after an exchange-rejected or unknown outcome | **fix** (X5): `_order_view` adds `size_remaining`, `size_cancelled`. Live only, after printing the JSON: place/probe → exit 5 if any order is FAILED, UNKNOWN or ABSENT_UNCONFIRMED; cancel → exit 5 if any targeted order is UNKNOWN or CANCEL_PENDING or carries an `error_code`; live-probe → exit 5 if the probe bet did not end with nothing remaining (message names the bet and says to cancel it). stderr per order: `Betfair order sent; outcome not as requested: <ref> <state> <error_code>; do not resend; run status or reconcile --mode live`. FAILED instead prints `Betfair order not live: <ref> FAILED <error_code>; nothing is live for this order; a fresh place is safe` (transport_not_sent: zero bytes; rejected: Betfair refused it). `decision` keeps its spec meaning (risk decision); docs say so. *severity disputed (major/minor); fixed either way. |
| R3#16 | major | Ctrl-C/SIGTERM during a send prints the same as "nothing sent"; skips steps 15–16 | **fix**: `OrderGateway(on_send=...)`; `_send` calls it before masking; the CLI's hook sets `_SignalGuard.deferring = True`, so a signal after the first send is recorded, step 15 runs (step 16 is deleted, §4), the gateway closes, the JSON is printed, then exit 5 with `Betfair stopped after an order request was sent (<SIG>); result above; do not resend`. A signal before any send stays exit 4. Kill passes no hook (§4 step 10). |
| R3#17 | minor | Signal during kill exits 4 with no JSON/advice; silent waits | **fix**: `cmd_kill` catches `(SignalStop, KeyboardInterrupt)` around both the pre-gateway phase and the gateway block → `kill_incomplete_interrupted`, JSON, exit 3, advice, HALT `kill_incomplete` when the journal is writable; progress lines per phase (§4 steps 2, 7, 8). |
| R3#18 | minor | Help text: cancel promises typing; resume omits `--mode live`; paper resume swallows the token | **fix**: `HELP["cancel"]` = "Cancel own bets by betId. Paper by default; live needs --mode live and A4, A5, A8, A10, A12, A13 plus a login; allowed while halted or killed; live cancels are full cancels (no --size-reduction); sent at once with no typed confirmation"; `HELP["resume"]` = "...; live needs --mode live --confirm-live TOKEN, then typing RESUME LIVE"; `HELP["settle-market"]` = "Live only: settle one CLOSED market by hand after checking the website; books each order's worst case; needs --confirm-live TOKEN and --bet-angel-paused until T3"; module docstring fixed; `main` refuses `--confirm-live` whenever the resolved mode is paper: exit 2 `Betfair input error: confirm_live_needs_mode_live`. |

**Rounds 1–2 fixes whose code is now deleted (superseded):** #25 (kill_lock_wait_s,
`reconcile_unknown(abort=)`, `_kill_arrived` — superseded by D4); #29 (`settle_from_cleared` VOIDED
handling — D3); #30 (`cleared_move` LAPSED/CANCELLED cumulative reading — D3); the round-2 brief
bullets "§9.8 step 3 kill 等 lock", "§9.7 / settlement" (VOIDED equality, LAPSED/CANCELLED
cumulative, settle needs CLOSED, response-amount tolerance stays for `found_move`), the round-1
"§9.7 child search sourceIdKey leg", and the unit-round items "stuck replace child" (U5) and live
"replace mixed outcomes" (U4 — the lifecycle classifier stays for paper). Partially touched but kept:
#8 (re-sweep waits monotonic — kept; the wall-clock read is now gone entirely), #31
(`matched_size_known_never_absent` — kept), #36/#37 (`pence`, `found_move` voided identity — kept),
#38/#40 (503 and gap handling — extended by C3, R3#7, R3#8), #6 (`accept_startup_not_clean` — kept for
`--accept`; settle-market no longer uses it).

### 5b. Astra C1–C4 (all must-fix)

**C1 (P1) — no hedge credit in live v1.** `risk._exact_cp` gains `credit`; without credit a matched
portion contributes `min(0, pnl)`. `worst_case_market_loss(..., hedge_credit=False)` and
`exposure_by_market(..., hedge_credit=False)` pass it through: **the default is no credit**, so a call
site that forgets the argument is conservative. `hedge_credit=True` is passed explicitly only by:
`RiskGuard.__init__` (`self.hedge_credit = mode == "paper"`, used by `_exposure(...,
hedge_credit=None)` for R8–R10 in `_exposure_rules` and for `evaluate_cancel`'s informational worst
case), R12's `before` figure in `evaluate_place` (476: `before = self._exposure(...,
hedge_credit=True)`: crediting only lowers our explained exposure, so `external` stays at least as
large — R12 is not loosened), and paper `status` / paper `_journal_summary` (`hedge_credit=(mode ==
"paper")`). Live `status`, live `_journal_summary` (cli.py:659) and the `settle-market` banner use the
default. No edit at `evaluate_replace` (risk.py:540): it is paper-only now and uses
`self.hedge_credit`. Why this is complete: without positive credit a matched BACK counts its stake and
a matched LAY its liability at `max(avg, limit)` — both independent of `averagePriceMatched` changes,
market version, Rule 4 reductions (they only lower a LAY's price) and non-runner voids (they only remove
exposure); EXECUTION_COMPLETE positions need no refresh. D1 stays usable: a single order is
unaffected; a hedge is accepted when the sum of both legs' losses fits the caps (default caps: BACK £2
+ LAY £2 @ 3.50 → 500p ≤ 2000p). Astra's repro becomes a refusal (`market_exposure`, worst case 500p >
cap 200p). Boundary test: `hedge_credit=True` appears only at the call sites listed here.

**C2 (P2) — absolute send-by through api/transport, placeOrders only.**
`JsonRpcClient.send_order(capability, prepared, *, send_by_monotonic_ns=None, send_gate=None)`:
**refuses a placeOrders body unless both are given, and refuses either one for any other method**
(`OrderRefused("capability_send_bound_required")` / `("send_bound_not_for_this_method")`, raised
before the capability is consumed and burning it, so FAILED_NOT_SENT). For placeOrders it reads
`capability.expires_monotonic_ns` before consuming, `send_by = min(expiry, caller bound)`, and passes
`send_by_monotonic_ns` and `send_gate` to `HttpTransport.request(..., send_by_monotonic_ns=None,
send_gate=None)` (the transport refuses them on idempotent reads with `invalid_request`). In
`_attempt`, after `_connection()` and `metered.arm()`, immediately before `conn.endheaders(payload)`:
`monotonic_ns() >= send_by` → `"send_window_expired"`; else `send_gate()` (an exception counts as
`"send_gate_failed"`); a refusal raises `TransportError(phase="send", code, may_have_arrived=False)` —
definitely unsent (asserted `metered.bytes_written == 0`), connection dropped. Gateway place/probe:
`send_by = book["monotonic_ns"] + max_book_age_s` and the gate of §4. `_place_live` reads funds
**before** the book (`_funds()` moves above `_self_fetch`; no new code), so the book-age window covers
only the decision, the journal appends and the connect. **Cancels, sweep cancels and `cancel_all`
pass neither** (transport behaviour unchanged; a late cancel of our own bet adds no risk), and their
capability is minted inside `_send` (a `mint` callable called in the same `try` as `send_order`, right
after the `submit` append): a refusal there is journaled by `journal_report` as FAILED_NOT_SENT
(`outcome_from_exception` already maps `capability_*` to it), so CANCEL_PENDING → EXECUTABLE
`cancel_not_processed`. `_clocks_disagree_since` (707–716) and its pre-send block (1003–1008) are
deleted (subsumed: the gate's `_book_age_ok` checks both clocks at the first byte). Outcome:
FAILED(`transport_not_sent`) with the code in the journaled exception payload.
`tests/test_betfair_support*.py` fakes (`FakeHttpTransport.request`, `RoutingTransport.request`)
accept and enforce both keywords on the fake clock before recording a request as sent. Both Astra cases
become tests.

**C3 (P2) — 503 invalidates every cached market and blocks fills across recovery.**
`book.snapshots_from_recording` (512–556): on the message that **enters** the unreliable window
(`cache.stream_unreliable` and the previous message was healthy), yield an invalid
(`stream_unreliable`) snapshot for **every** market in `cache.markets()` that has a definition, not
only `changed`, and add them all to `pending`; later 503 messages yield their changed markets as today.
`MarketBookCache.snapshot(..., stamp=None)` lets a yielded snapshot of a market the message did not
change carry that message's `pt`, `observed_at` and `monotonic_ns` (used for the 503 entry and the
recovery catch-up). Recovery stays: the first healthy message re-yields every pending market (usable)
at its pt. Fills are then blocked across recovery by the existing paper rules (no passive fill when
the previous snapshot is not usable; the high-water mark is raised on the recovery snapshot, so volume
traded during the 503 window is never counted). Tests: global 503 without `mc`, 503 on another market
only, and Astra's end-to-end replay (0 fills; `passive_fill` absent).

**C4 (P2) — replay provenance hashes every consumed dependency.** `run_paper_replay` records
`inputs.dependencies`: per recording `{"recording": name, "receipt_json": {"present", "sha256"},
"receipt_partial_json": {"present", "sha256"}, "receipt_used": "receipt.json"|"receipt.partial.json"|null,
"recv_sidecar": {"name", "present", "sha256"}}` (sidecar = `book._recv_rows(path)`), and
`inputs.resolved`: `{"key_type", "key_type_source": "explicit"|"receipt"|"inferred_historical",
"windows": [[first_pt, last_pt, [market_ids]], ...] | null, "windows_status":
"known"|"unknown_no_receipt"|"unknown_segment_not_in_receipt"}`. The ledger row keeps exactly
`LEDGER_KEYS`; `source_sha256 = canonical_sha256({"recordings": [...], "orders": orders_sha,
"dependencies": [...], "resolved": {...}})`. Astra's two runs (with and without the receipt) must now
differ in `source_sha256`.

---

## 6. Test plan

All tests go into existing `tests/test_betfair_*.py` modules (no new files). Offline, fakes only,
temp state roots. Each R3 major that is fixed or made moot has a regression test (marked **[R]**).

**test_betfair_gateway.py**
- Delete: `test_replace_live`, `test_replace_needs_a_replace_gateway`,
  `test_replace_refreshes_the_parent_before_minting_the_child`,
  `test_replace_kill_rechecked_after_submit_journal`, `test_kill_waits_longer_for_the_lock_than_an_order`,
  `test_cancel_or_reconcile_under_an_old_kill_yields_only_to_a_new_one`,
  `AuditRegressionTests.test_lost_replace_child_without_our_refs_is_found_by_key`; any assertion that a
  holder cancels its own bet after a kill (step 16).
- Change: `test_unknown_outcome_gives_the_global_lock_to_a_kill` → the kill sweeps while the UNKNOWN
  holder still holds the lock; `test_three_consecutive_unknown_outcomes_halt` → halt rides in the third
  report's payload, no separate `halt` event, and a 4th UNKNOWN after `resume` halts again;
  `test_sleep_after_the_decision_refuses_the_send` → FAILED `transport_not_sent`, code
  `book_stale_before_send`, zero request bytes; `test_probe_sets_ref_roundtrip_marker` → no replace
  step; `test_signals_masked_around_send` → also asserts the `on_send` hook ran first;
  `NormaliserTests` → drop replace-normaliser assertions; the unfoldable-journal kill tests → note
  `journal_untrusted_sweep_uses_config_strategy_ref`, status `kill_incomplete`.
- Add: `test_live_replace_refused_before_lock_login_or_network` **[R#2]** (gateway, `open` and
  `RiskGuard.evaluate_replace`); `test_send_window_expires_during_a_slow_connect` (C2b, Astra case: 0
  bytes, FAILED); `test_both_clocks_advanced_after_submit_refuses_at_first_byte` (C2a);
  `test_kill_file_checked_at_the_first_byte`; `test_place_reads_funds_before_the_book`;
  `test_live_exposure_gives_no_hedge_credit` (C1 end-to-end: matched BACK 2 @ 3.5, exchange average 2.0
  and version bumped, LAY 2 @ 3.5 refused `market_exposure` with cap 200p);
  `test_report_halt_rides_in_the_report_event` (R3#13); `test_recorded_orphans_refused_at_open`
  (R3#14, paper replace child + live plain intent); `test_live_partial_cancel_refused`;
  `test_cancel_capability_minted_after_the_submit_append` (a slow `submit` append does not expire it;
  a mint refusal is journaled FAILED_NOT_SENT → EXECUTABLE `cancel_not_processed`);
  `test_place_holder_stops_reconciling_when_the_kill_file_appears`;
  `PaperGatewayTests.test_paper_step_refuses_a_book_of_another_market` **[R#6]**;
  `PaperGatewayTests.test_paper_state_survives_death_after_the_report` **[R#12]** (takes and traded
  baseline survive; the repro's OVERFILL and DOUBLE-COUNTED cases both stay at the control values);
  `test_settle_market_*` may live here or in reconcile (below).

**test_betfair_reconcile.py**
- Delete: `test_replace_unknown_unique_child_inferred`, `test_replace_unknown_ambiguous_keeps_halt`,
  `test_stuck_replace_child_can_be_accepted`, `test_settle_own_pnl_and_mismatch_halts`,
  `test_refresh_open_orders`, `test_executable_bet_gone_from_current_orders_is_closed_and_settled`,
  `test_settlement_reads_back_to_the_submit_time`, `test_settle_mismatch_halts_through_the_gateway`,
  `test_multi_fill_average_rounding_is_not_a_pnl_mismatch`, `test_replaced_bets_settle_their_matched_part`.
- Change to the §3.2 rule: `test_cleared_lapsed`, `test_cleared_rows_never_turn_a_matched_part_into_zero`,
  `test_voided_fragment_is_part_of_the_size_identity`, `test_known_cancel_is_never_counted_twice_as_a_lapse`,
  `test_partial_void_never_zeroes_a_standing_match`, `test_float_artefact_sizes_are_their_penny`
  (where it used `cleared_move`), `test_reconcile_clears_market_halt_only_when_resolved` (use a
  `found_order_inconsistent` market halt instead of a replace child).
- Add: `test_unknown_found_lapsed_keeps_worst_case_executable`;
  `test_unknown_found_partly_cancelled_keeps_worst_case`;
  `test_unknown_found_partly_voided_matched_bet_is_executable_with_nothing_remaining` (journal matched
  2.00, exchange matched 1.00 + voided 1.00 → EXECUTABLE, remaining 0, `matched_exact`);
  `test_cleared_rows_only_establish_the_bet` **[R#3]** (VOIDED with a stale journal size, SETTLED,
  LAPSED: each → EXECUTABLE with betId, never LAPSED/VOIDED/SETTLED; the order is no longer UNKNOWN,
  so A17/R3 do not stay blocked); `test_live_reconcile_writes_no_settle_or_close_moves` (cleared
  SETTLED/VOIDED rows present: no `settle` event, exposure unchanged);
  `test_startup_bounds_a_settled_order_by_matched_plus_remaining` (SETTLED, matched 0, remaining 2.00,
  row `sizeSettled` 2.00 → no contradiction; 2.01 → contradiction). **settle-market**:
  `test_settle_market_has_no_mode_flag` (argparse exit 2), `test_settle_market_line_is_typed_before_the_lock`
  (the confirmer runs while `.gateway.lock` is free), `test_settle_market_refused_unless_closed`
  (`settle_market_book_read_failed`, `settle_market_book_missing`, `settle_market_not_closed`),
  `test_settle_market_refused_while_our_bets_are_current`, `test_settle_market_refused_with_unresolved_orders`,
  `test_settle_market_read_failures_refuse`, `test_settle_market_shows_orders_and_worst_case_then_settles`
  (banner text, one `settle` event carrying `echo` and `shown_sha256`, EXECUTABLE and EXECUTION_COMPLETE →
  SETTLED, `L_M` = 0, realised booked = Σ worst case incl. the EXECUTABLE remainder, no
  listClearedOrders call), `test_settle_market_refused_when_the_journal_moved_after_typing` (listing sha
  and compare-and-append), `test_settle_market_then_place_same_day_arms` **[R#3]** (EXECUTABLE matched 0
  → settle-market → same-day place open whose startup reconcile sees a SETTLED row `sizeSettled` 2.00:
  no `exchange_journal_contradiction` halt, A17 passes).

**test_betfair_killswitch.py**
- Delete: `test_kill_covers_replace_child_without_strategy_ref`.
- Change: `test_target_set_union_refs_betids_sourceid` → `test_target_set_is_our_refs_and_journal_bet_ids_only`
  (a bet visible only through sourceIdKey is neither targeted nor cancelled); `test_resweep_after_recent_submit`
  and `test_resweep_waits_are_bounded_on_the_monotonic_clock` → `test_resweeps_run_from_the_kill_file_and_cover_the_bound`
  (passes at T0 + 20 s and + 35 s with no journal submit at all; the bound constants; no wall-clock read);
  `test_account_wide_needs_flag_phrase_runs_once` → a second account-wide cancel is sent (no journal
  scan) and the fake's FAILURE / ERROR_IN_MATCHER makes it `kill_incomplete`, exit 3; the
  `kill_file_only` CLI case (line 363) → config error, exit 3 with advice.
- Add: `test_kill_never_takes_the_gateway_lock` (the test holds `.gateway.lock` with `file_lock` for
  the whole kill: kill_complete, exit 0, resting bet cancelled, lock still held by the "holder");
  `test_kill_completes_its_sweep_while_a_flaky_holder_keeps_the_lock` **[R#0]** (thread holder: live
  place whose every read times out once then succeeds, scaled fake clock as in
  `audit3/repro/verify/killwait/concurrent_kill_vs_flaky_holder.py`; kill exit 0 within the bound,
  earlier resting bet cancelled, holder's place refused — R1, compare-and-append, step 10 or
  `kill_engaged_before_send` — and zero new place requests after the KILL file);
  `test_kill_sweeps_when_a_holder_reanchors_between_reads` **[R#0]** (the keychain_runner fake appends
  to the journal and rewrites the anchor between the kill's Keychain read and its journal-head read: no
  mismatch, sweep runs, exit 0; and `kill_a13_race.py`'s order through `static_arming` for another
  command gives `[]`); `test_kill_with_an_untrusted_journal_sweeps_the_config_ref_and_exits_3`
  (unfoldable, broken chain, Keychain anchor ahead of the journal: sweep on the config ref only, no
  anchor written, exit 3 with advice); `test_kill_journals_no_order_moves`;
  `test_resweep_covers_the_longest_in_flight_order` (a bet that appears at T0 + 32 s is cancelled by
  the 35 s pass); `test_kill_cancel_is_sent_over_a_slow_reconnect` (connect takes 8 s: cancel sent,
  bet cancelled); `test_kill_writes_no_account_registry`;
  `test_kill_cancels_own_orders_false_is_a_config_error`;
  `test_signal_during_kill_is_exit_3_with_advice` (R3#17: during login, during a re-sweep wait, and
  KeyboardInterrupt in `load_config`); `test_kill_prints_progress_advice_and_f67_only_when_unverified`;
  `test_kill_login_refused_by_the_ledger_names_login_reset`.

**test_betfair_cli.py**
- Change: `test_sigterm_during_a_live_order_still_closes_and_logs_out` → signal during the send:
  JSON printed, exit 5, logout and close still happen **[R#16]**; `test_help_lists_subcommands_with_limits`
  (settle-market live only; replace paper-only; live-probe without replace step; cancel full only).
- Add: `test_live_replace_refused_with_message_and_no_side_effects` **[R#2]** (exact stderr line,
  exit 2, zero Keychain reads, no live journal file, no `.gateway.lock` opened, zero transport calls);
  `test_live_partial_cancel_refused_with_message_and_no_side_effects`;
  `test_live_probe_has_no_replace_step_flag`; `test_live_cancel_rejected_by_exchange_exits_5` and
  `test_live_place_failed_exits_5` **[R#15]** (FAILED line says nothing is live, a fresh place is safe);
  `test_live_probe_cancel_rejected_exits_5`; `test_signal_before_send_is_exit_4`;
  `test_confirm_live_with_paper_mode_refused` (R3#18); `test_help_texts_state_live_limits` (R3#18);
  `test_settle_market_cli_end_to_end` (incl. `bet_angel_coexistence_unverified` without the flag).

**test_betfair_transport.py / test_betfair_api.py**: `test_send_by_expiry_after_connect_sends_nothing`,
`test_send_gate_refusal_is_definitely_unsent`, `test_send_by_and_gate_refused_on_reads`;
`test_send_order_passes_capability_expiry_as_send_by` (min with the caller's bound);
`test_place_body_without_send_bound_is_refused` and `test_cancel_body_with_send_bound_is_refused`
(structural, capability burned, nothing sent).

**test_betfair_arming.py**: `settle-market` check set; live `open(command="replace")` refused;
`test_a13_reads_the_keychain_before_the_journal_head`; `test_kill_static_a13_checks_the_state_root_only`.

**test_betfair_book.py**: `test_global_503_without_mc_invalidates_every_cached_market`,
`test_503_on_another_market_invalidates_this_one_too`, `test_recovery_reyields_every_market_at_the_healthy_pt`;
adjust `test_status_503_flags_cache_until_healthy_message` /
`test_change_under_a_503_is_not_lost_when_the_market_goes_quiet` if their snapshot lists change.

**test_betfair_paper.py**: `test_no_passive_fill_across_a_503_without_mc` (C3, Astra case);
`test_provenance_hashes_receipts_sidecars_and_assumptions` (C4, Astra's two runs differ; dependency
roles and presence listed); `test_empty_segment_keeps_the_gap_guard` (R3#7);
`test_mixed_receipt_and_no_receipt_inputs_refused`; `test_gap_window_is_per_market_and_same_window`
(R3#8, both repros); `test_activation_order_survives_a_process_boundary` (R3#9); adjust
`test_paper_replay_ledger_row_exact_keys` and `test_recording_gap_is_never_forward_filled` to the new
helpers.

**test_betfair_journal.py**: `test_concurrent_appends_from_two_connections_keep_one_chain` (D4
verification: two `OrderJournal` connections, two threads, interleaved appends — one with order moves,
one kill-like with none — `verify()` ok, seqs contiguous, fold ok; **plus a fresh connection's first
append on a journal of several thousand events racing the other connection**, chain still one);
`test_settled_worst_case_counts_the_remaining_size`; `test_payload_halts_fold_atomically`;
`test_settle_payload_accepts_echo_and_shown_sha256`; `test_paper_state_payload_round_trips`.

**test_betfair_lifecycle.py**: change `test_transition_table_complete` (EXECUTABLE → SETTLED); add
`test_executable_to_settled_keeps_remaining_and_counts_nothing`.

**test_betfair_risk.py**: add `test_live_guard_gives_no_hedge_credit`, `test_r12_uses_the_credited_figure`,
`test_paper_keeps_hedge_credit`, `test_hedge_credit_defaults_to_none`,
`test_realized_loss_without_fold_fields_counts_the_cap_used` (replaces the "bare" fallback assertion:
still `daily_loss`); remove any probe-replace exemption assertion. Paper replace tests stay.

**test_betfair_boundaries.py**: `replaceOrders` appears nowhere under `betangel_research/betfair`;
`placeOrders`/`cancelOrders` only in `gateway.py`; `risk.ACTION_VERBS` has no `"replace"`;
`hedge_credit=True` only at the §5b C1 call sites; `send_gate`/`send_by_monotonic_ns` passed only from
the place path.

**test_betfair_presence.py**: delete the `replace_echo_line` tests and the probe `replace_step` variant.

**test_betfair_config.py** (or the existing config tests' module): `kill_cancels_own_orders` false refused.

**test_betfair_support*.py**: fakes accept/enforce `send_by_monotonic_ns`/`send_gate`; the
keychain_runner fake gets an optional hook between reads; property harness skips `replace` in live;
nothing else.

**test_betfair_reports_no_account_data.py / test_betfair_facts_doc.py**: §7 checks (no
`dockerignore_receipt_refresh`; `historical_receipts`; receipt in `protected_sha256`; S0 doc wording
check; brief diff block == `s0_patch`; state diagram includes EXECUTABLE → SETTLED; F77 present and
labelled inferred).

**Finish:** `cd tree && BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests`
must pass in full (142 pre-existing tests included), then the 300 × 200 property run, then brief
integration steps 4–6 on the private tree.

---

## 7. Docs, report, handoff and brief

Cantonese prose with English technical terms and evidence labels, as the existing files.

- **docs/betfair-order-path.md**: §1 Modes gets the §1 scope table (live surface); §2 matrix: no live
  `replace` row, `live-probe` without replace step, new `settle-market` row (live only, typed before
  the lock, `--bet-angel-paused` until T3), live `cancel` full only, `kill` "no lock"; §3 choke point:
  first-byte gate on placeOrders only (KILL, book age on both clocks, send-by = min(book freshness,
  capability expiry)), cancels ungated with the capability minted right before the send, funds read
  before the book, `_clocks_disagree_since` gone; §4 presence: no replace line, the settle banner;
  §5 diagram: EXECUTABLE → SETTLED, a "live after v1 narrowing" note with the §3.2 tables; §6 R8:
  "live: matched portions count only their losses (no hedge credit, the default); R12 uses the credited
  figure"; §8 reconciliation: the evidence rule, no refresh/close/settle, new "Settling a market by
  hand" subsection (§3.1 verbatim banner, preconditions, refusal codes, the P3 premise labelled 推斷
  (inferred, F77), "settle soon after CLOSED; a market Betfair no longer returns stays counted", Σ w vs
  L_M, the daily-cap effect, settle on the market's own UTC day, kill-cancelled bets stay counted until
  settled); §9 kill: §4 flow, legs, untrusted journal → config ref + exit 3, no moves, re-sweeps from the
  KILL file and why (F28 + F42), the TCP-retransmission residual, the worst-case duration, progress
  lines, F67 line only while unverified, F18 overlap, F31 account-wide behaviour, `login-reset` on a
  ledger refusal, "touch KILL only stops new sends; run kill to cancel", signals → exit 3; exit-code table
  adds 5; §11 PAPER_ASSUMPTIONS only if `paper.py`'s tuple text changes (keep verbatim sync); §13
  live-probe: no `--replace-step`, T11 dropped, T10 extended (F77); §16 "What is implemented"/"未做":
  live replace, live partial cancel and automatic live settlement removed by design.
- **docs/betfair-integration.md**: paper-replay provenance (C4), 503 handling (C3), gap windows per
  market (R3#7/#8), F67 note in the kill paragraph; the `orders.live` config row says
  `kill_cancels_own_orders` must be true. Lines 104 and 391 stay true until S0 lands; they are added to
  the S0 commit list (R3#10).
- **docs/betfair-facts.md + research/betfair-facts.json**: F32/F33 code_handling "paper only; live
  replace removed in v1; T11 dropped"; F36 "cleared rows only establish that a bet exists in live";
  F37 "live P&L informational; settle-market books the worst case"; F28 and F42 code_handling add "the
  kill's re-sweep bound (`IN_PLAY_DELAY_MAX_S`)"; F31 code_handling adds "a rejected concurrent
  cancel-all ends the kill kill_incomplete"; F61/F74 per R3#11; F67 code_handling adds "kill logs in a
  separate emergency session and prints the F67 warning while coexistence is unverified"; **new F77**
  (area reconcile, `evidence_type: "inferred"`, `verification_status: "inferred"`, no new source
  claimed): "After a market closes, listMarketBook returns it as CLOSED for an unverified period, and
  listCurrentOrders stops listing a bet once its market is settled", code_handling "settle-market P2/P3;
  first-live-test T10 extended". Fact counts move from 76 to 77 wherever stated.
- **reports/betfair-setup-2026-10-10.{md,json}**: limitations add the narrowed live surface (no live
  replace, no live partial cancel, no automatic settlement, no hedge credit, settle by hand, F77
  inferred); S0 paragraph (md:64) drops "所以 direct-collection receipt 要更新" and says the historical
  as-deployed receipt stays byte-identical; fact counts per R3#11 and F77; `code_sha256` refreshed; test
  counts from the rerun; `protected_sha256` copies the handoff's.
- **reports/direct-collection-2026-10-10.json**: **untouched** (verified byte-identical to base:
  sha256 `19a06dae12ec57a99712ddfc95943ce897e42baaa1c1466a4b157f983ab368f0`). Add it to
  `protected_sha256` (handoff and report) so the merge gate refuses any edit.
- **handoff.json**: delete `merge_gate.dockerignore_receipt_refresh`; add
  `merge_gate.historical_receipts`: "reports/direct-collection-2026-10-10.json is the as-deployed
  receipt of the 2026-10-10 Fly build; it stays byte-identical. S0 changes the local .dockerignore
  only; an actual later build or deploy records its own new receipt." `s0_commit` adds the R3#10
  rewrite list. `s0_patch`: the AGENTS.md bullet describes the narrowed live surface:

  > - `betangel_research/betfair` is a Betfair Exchange client: read-only market data, a stream recorder, and one order gateway that defaults to paper mode. Its live surface is limited to LIMIT orders with LAPSE persistence that the operator types at a terminal, cancelling our own bets, the kill switch, a one-pound live probe, reconciling unknown order outcomes and settling a closed market by hand; there is no live replace and no automatic live settlement. Live orders need every arming condition in docs/betfair-order-path.md. Betfair credentials live only in the macOS Keychain; the client certificate (default `~/.config/betangel-research/betfair/`) and live order state (`~/Library/Application Support/betangel-research/betfair/`) live outside every Git worktree.

  (Certificate under `~/.config/...`, live state under `~/Library/Application Support/...`, as Astra
  confirmed.) New `s0_expected_sha256_after["AGENTS.md"]` =
  `b5021ba872f6e6e97be0a0d07a3b610f757719e47d144a9f2a87d274011261b6` (computed in
  `v1n/repro/amend/` from base AGENTS.md; the method reproduces the old value `01209a87…`; the
  regenerated patch is `v1n/repro/amend/s0_new.patch`, differing from the old one in that single `+`
  line; re-verify with `patch -p1` on a scratch copy; the other three targets are unchanged). The bullet
  does not mention partial cancels, so revision 2 leaves it and the hash unchanged. `readme_commit` lines
  unchanged. `reviews` gains: round-3 audit (19 confirmed; mapping in brief), Yu's D1–D5 narrowing,
  Astra review C1–C4 and S0 correction, and the revision-2 critique (summaries, not independent
  verification). `tests`, `artifact_sha256`, `completed_at` from the rerun.
- **brief.md**: new section "第三輪 audit、v1 live 收窄同 Astra review（2026-10-10）" summarising
  §1–§5b as amendments to `final.txt` (live surface table, evidence rule, settle-market, kill without
  lock, C1–C4, exit 5, paper fixes, the §8 decisions incl. A20, open for Yu then and decided on 2026-10-11); the "`.dockerignore`
  改動係必須嘅…deploy receipt 要更新" bullet becomes "`.dockerignore` 改動係必須嘅（後面嘅規則覆蓋
  `!betangel_research/**`）。`reports/direct-collection-2026-10-10.json` 係 2026-10-10 as-deployed 嘅歷史
  收據，一個 byte 都唔改；S0 之後真正 build/deploy 先另寫新收據。"; the diff block regenerated
  byte-identical to `s0_patch`; the round-2 bullets superseded by D3/D4 marked as such; Status line
  updated (S0 still not applied).
- **AGENTS.md, README.md, .gitignore, .dockerignore**: not edited (S0 stays a proposal).

---

## 8. Ambiguities resolved (safer on live money, smaller in live code)

- **A1** The D3 evidence rule applies to every live evidence move, and `refresh_open_orders` is deleted
  rather than restricted: otherwise UNKNOWN → EXECUTABLE (withheld) → refresh → LAPSED would zero an
  UNKNOWN in two steps. Cost: lapsed-unmatched bets count until `settle-market`.
- **A2** `settle-market` books the worst case as realised loss; Betfair's profit is neither read nor
  journaled by it (`orders-cleared` shows it).
- **A3** EXECUTABLE → SETTLED is added instead of inventing matched or lapsed sizes for a stale journal;
  the same-day startup reconcile bounds a SETTLED order by matched + remaining (the booked size).
- **A4** The settle line is typed before the lock (preconfirm), like every other typed line; the listing
  hash binds it to the fold taken under the lock, and the single `settle` event is compare-and-appended
  on that fold's head. No prompt ever holds `.gateway.lock`.
- **A5** The kill drops the sourceIdKey leg (bets without our strategy ref are never cancelled outside
  the account-wide scope); startup/reconcile keep it because it only halts. An untrusted journal forces
  `kill_incomplete`; no verify-only sourceIdKey leg is added.
- **A6** The kill journals no order moves (no race with a holder; worst case kept).
- **A7** Account-wide kill is lock-free with no journal scan; a concurrent cancel-all is rejected by
  Betfair (F31) and that kill exits 3. Not "harmless": fail-closed.
- **A8** Holders no longer "give way"; nothing waits for them. The one survivor is a single condition:
  a place/live-probe holder stops reconciling once the KILL file exists.
- **A9** Exit 5 is live-only; paper exit codes are unchanged.
- **A10** No fold-level guard for removed live states: no live writer remains (boundary and reconcile
  tests prove it); paper still needs those states.
- **A11** R12 keeps the credited (smaller) explained exposure, passed explicitly, so it is not loosened
  by C1; every other call site defaults to no credit.
- **A12** Step 16 (`_cancel_after_kill`) is deleted; a manual `touch KILL` only stops new sends.
- **A13** `--replace-step` is deleted from the parser (argparse refusal) instead of a refusal branch;
  likewise `settle-market` has no `--mode`.
- **A14** Replay inputs that mix known and unknown gap windows are refused.
- **A15** The paper state lives in the journal; `paper-state.json` is legacy read-only.
- **A16** The kill keeps Ctrl-C live (no signal deferral); it ends exit 3 with advice.
- **A17** The startup exchange floor for realised P&L stays (it only tightens R10).
- **A18** A13 reads the Keychain anchor before the journal head everywhere. The kill's static A13 checks
  the state root only; its one anchor check (`_keychain_anchor_ok`, under `_open_live`) never stops the
  sweep: a failure marks the journal untrusted (config strategy ref only, no anchor write, exit 3). The
  kill still writes the anchor when the journal is trusted (cmd_kill's "no events without an anchor"
  invariant). (2026-10-11, re-review R1: the kill writes it in `close()`, after the sweep, not in
  `open()`. Every anchor write of every command goes through `credentials.publish_anchor`. It holds a
  per-profile lock (`paths.anchor_lock`) and reads the journal head inside that lock, so the Keychain
  anchor never moves backwards. Appends never take this lock. 2026-10-12, follow-up: inside the lock it
  also reads the Keychain anchor and writes only a head that extends it under the A13 rule, so a journal
  restored in place to an older copy during a live session is never anchored; a lock path replaced during
  the Keychain write makes it publish again under a fresh lock. Only a risk-adding command's open() waits
  the full 30 s for another publisher; every other wait is `killswitch.JOURNAL_BUSY_TIMEOUT_S`.)
- **A19** Settled realised loss is Σ of per-order worst cases (≥ the market's no-credit `L_M`): it reuses
  the per-order realised path with no market-level field, and the larger figure is the conservative one.
- **A20** **Decided by Yu on 2026-10-11: keep the conservative behaviour** (collab record
  `report_94134b37-5c9d-4a98-b1de-9bae35a45a37`). A market that listMarketBook no longer returns (F77,
  retention unverified) cannot be settled in v1. It keeps its worst case in Σ L_m indefinitely, which
  permanently reduces R9/R10 headroom. v1 adds no override. The operating rule is to settle promptly
  after CLOSED. A "forget-market" command may be considered later, but only after the first-live
  T-tests, with a fresh design and review. It will never be part of v1. (Revision 2 listed A20 as an
  open decision for Yu.)
- **A21** P3's premise (listCurrentOrders stops listing settled bets) is inferred (F77); if Betfair keeps
  listing finished bets of a CLOSED market, settle-market refuses (never more permissive) and T10 tells us.
- **A22** `kill_cancels_own_orders` stays as a config key (old configs still load) but must be true.
- **A23** Live cancels are full cancels; paper keeps reductions.
- **A24** Send-by and the first-byte gate are for placeOrders only and are structurally required there.
- **A25** Kill re-sweeps are unconditional and measured from the KILL file, not from journaled submits.
- **A26** `_realized_loss` fails closed (cap counted as used) when the fold's liability fields are absent.
- **A27** No PENDING / `orderProjection=ALL` verify read: we place synchronously (no async flag), the
  in-play delay is spent inside the placeOrders call, and PENDING is not in the fact register.

## 9. Suggested implementation order

1. D2 deletions + boundary tests. 2. D3 (`reconcile.py`, `reconcile()`, `settle-market` with preconfirm,
journal worst case, lifecycle edge, startup SETTLED bound). 3. D4 (kill open without lock, static A13
state-root only, A13 read order, untrusted journal, legs, no moves, unconditional re-sweeps and bound
check, registry skip, account-wide scan removal, CLI progress and signals). 4. C1 (default no credit).
5. C2 (api structural rule, transport, fakes, gateway gate, funds before book, cancel mint in `_send`).
6. §2b deletions (`_cancel_after_kill`, `kill_file_only`, live partial cancel, `_realized_loss`
fallbacks, `_after_outcome`). 7. Paper: R3#6/#9/#12/#14, C3, C4, R3#7/#8. 8. CLI UX: R3#15/#16/#18,
help. 9. Docs, facts (F77), report, handoff, brief (S0). 10. Full suite, property run, brief steps
4–6. Expected net live-code change: several hundred lines deleted, well under half that added
(settle-market, send gate, journal halts/worst case).

---

## 10. Critique log (revision 2)

K1–K13 are the live-money lens, K14–K29 Betfair operations, K30–K43 simplicity, in the critique's
order. Verified offline before deciding: `repro/simplicity-critic/kill_a13_race.py` (journal-first
→ `['journal_anchor_mismatch']`, Keychain-first → `[]`) and `settled_contradiction.py`
(`exchange_matched_size_exceeds_journal`) both reproduce on the tree; the fold-cache, `_cancel_all`,
registry, `kill_cancels_own_orders`, `_realized_loss` and F28/F31/F42/F67/F73 claims match the code and
the fact register.

| # | Sev | Item | Decision | Reason |
|---|---|---|---|---|
| K1 | blocking | Kill A13 reads the journal before the Keychain in three places | **Accept** (§4 steps 5–6, A18) | Reproduced; the kill's static A13 now reads no anchor, its one check is Keychain-first, and A13 is Keychain-first for every command. |
| K2 | important | Send-by on cancels can make a kill cancel FAILED_NOT_SENT | **Accept** (§5b C2, §4 step 8) | A late cancel of our own bet adds no risk; send-by and the gate are now placeOrders only. |
| K3 | important | settle-market with stale matched size → same-day contradiction HALT | **Accept, option (a)** (§3.1) | Bound SETTLED by matched + remaining, the size whose worst case was booked; option (b) would need the cleared read K33 deletes. |
| K4 | important | `kill_file_only` exits 0 with no sweep | **Accept** (§4 step 4, §2b) | Contradicts D4's exit rule; branches deleted, config must say true. |
| K5 | minor | Unfoldable journal can let the kill exit 0 | **Accept; optional sourceIdKey verify rejected** (§4 step 6, A5) | An untrusted journal forces exit 3; a forced exit 3 already covers the gap without a new read leg. |
| K6 | minor | Re-sweep bound ignores F42's in-play delay; PENDING not read | **Accept the delay; reject PENDING** (§4 step 8, A27) | `IN_PLAY_DELAY_MAX_S` joins the bound; synchronous placement spends the delay inside the call, and PENDING is unregistered. |
| K7 | minor | Settle prompt held `.gateway.lock` for up to 120 s | **Accept, typed before the lock** (§3.1 step 1, A4) | Restores the spec's "every typed line before the lock"; the listing hash and compare-and-append keep it safe. |
| K8 | minor | Append wait claim wrong; cancel capability can expire behind a whole-journal fold | **Accept; reject seeding `_fold_cache`** (§4 step 3, §5b C2) | Claim corrected; the cancel mint moves after the appends, so append latency no longer matters and no cache change is needed. |
| K9 | minor | "remaining > 0 by construction" is false after a partial void | **Accept** (§3.2) | Remaining ≥ 0; remaining 0 with matched > 0 is a valid `matched_exact` record; test added. |
| K10 | minor | Kill-cancelled bets stay counted; docs implied cancelling frees the cap | **Accept** (§3.1, banner, §7) | Conservative behaviour, now stated in docs and in the settle banner. |
| K11 | minor | "Concurrent account-wide cancels are harmless"; unreported check blocks forever | **Accept: delete the check, correct the text** (§4 step 9, §2b) | One branch fewer; F31 makes the second cancel fail closed (exit 3); no permanent block after a crash. |
| K12 | minor | Lock-free kill does an unlocked registry write | **Accept: skip for the kill** (§4 step 6, §2b) | The kill has no A15 and does not need it; the docstring's lock premise stays true. |
| K13 | minor | send_order does not require the gate on placeOrders; risk.py:616 dormant replace | **Accept** (§5b C2, §2) | Structural rule both ways plus tests; `== "place"`. |
| K14 | important | Re-sweep bound does not encode F28; mixes local writes with arrival | **Accept** (§4 step 8) | Bound is now from the KILL file: ORDER_TIMEOUT_S + ORDER_VISIBILITY_S + IN_PLAY_DELAY_MAX_S + 3 = 35 s, checked at import; residual documented and printed. |
| K15 | important | Same A13 race as K1 (cmd_kill and `_open_live`) | **Accept** (see K1) | Same fix; A18 rewritten. |
| K16 | important | Same SETTLED contradiction as K3 | **Accept** (see K3) | Same fix and regression test. |
| K17 | important | P2 "no row" is permanent and misnamed; CLOSED retention unverified | **Accept; way-out left to Yu** (§3.1 step 4, A20, F77); 2026-10-11: Yu kept the conservative behaviour, no override in v1 | Split into read_failed / missing / not_closed, retention labelled unverified with a T10 observation; adding an override would add live code, so it was an open decision (A20, decided 2026-10-11). |
| K18 | important | Lock-free overlap: F67 session thrash, F18 concurrency | **Partly accept** (§4 holder paragraph) | One condition stops place/probe holders reconciling after a KILL; no relogin guard in the read client; both effects documented and tied to T3. |
| K19 | important | Kill duration bound and operator messages incomplete | **Accept** (§4 steps 2, 7, 8) | Computed worst case, per-phase progress, advice at start, Keychain dialog hint, `login-reset` named. |
| K20 | minor | P3 premise presented as fact | **Accept** (§3.1 step 5, F77, A21) | Labelled inferred, T10 extended, refusal text says what to check. |
| K21 | minor | Account-wide text contradicts F31 | **Accept** (§4 step 9, A7) | Rewritten from F31; fail-closed outcome stated. |
| K22 | minor | `--bet-angel-paused` shown optional | **Accept** (§3.1 synopsis, HELP) | `_coexistence` refuses without it until T3. |
| K23 | minor | F67 warning unconditional | **Accept** (§4 step 7) | Printed only while coexistence is unverified; JSON carries the config value. |
| K24 | minor | Invented website menu path in the banner | **Accept** (§3.1 banner) | Neutral wording. |
| K25 | minor | Σ w can exceed L_M; daily-cap and date effects unstated | **Accept the docs; keep Σ w** (§3.1, A19) | Effects documented; Σ w stays because it is the more conservative figure and needs no market-level field. |
| K26 | minor | P1 passes trivially for older markets | **Accept: drop P1** (§3.1) | It protects no money; settle-market leaves `STARTUP_RECONCILE_COMMANDS`. |
| K27 | minor | Settle prompt under the lock (same as K7) | **Accept** (see K7) | Same fix. |
| K28 | minor | Place send-by window spends the funds RTT | **Accept** (§5b C2) | Funds before the book (a reorder); FAILED text says nothing is live and a fresh place is safe. |
| K29 | minor | Registry docstring premise broken (same as K12) | **Accept** (see K12) | Kill no longer registers. |
| K30 | blocking | SETTLED contradiction (same as K3) | **Accept** (see K3) | Same fix; the suggested `size_p` bound was not used because matched + remaining is tighter and still covers the booked size. |
| K31 | important | Drop the anchor part of A13 from the kill; skip anchor check and write | **Partly accept** (§4 steps 5–6, A18) | Static anchor reads are dropped and an anchor failure degrades to a sweep, never no sweep; the one Keychain-first check stays because a mismatched journal cannot be trusted to list only our bets, and the anchor write stays when the journal is trusted. |
| K32 | important | Hedge credit defaulted on | **Accept** (§5b C1) | No credit by default; explicit opt-in only at listed sites, checked by a boundary test. |
| K33 | important | settle-market informational cleared reads | **Accept: delete step 6** (§3.1) | D3 does not need Betfair's P&L; fewer live reads and failure paths. |
| K34 | important | Send-by on cancels (same as K2) | **Accept** (see K2) | Same fix. |
| K35 | minor | settle-market over-built: P1, two events, `--mode` | **Accept all three** (§3.1) | No P1, one `settle` event carrying the echo, no `--mode`; duplicate "Type exactly" line removed. |
| K36 | minor | `_cancel_after_kill` duplicates the kill's cancel | **Accept: delete** (§4, §2b, A12) | The re-sweeps cover it; the duplicate created an UNKNOWN; R3#16 now covers step 15 only. |
| K37 | minor | Re-sweep trigger logic and wall-clock arithmetic | **Accept: unconditional, from the KILL file** (§4 step 8, A25) | Deletes RECENT_SUBMIT_S, the wall clock and the unresolved scan; costs about 35 s per kill. |
| K38 | minor | Account-wide "harmless" contradicts F31 (same as K21) | **Accept** (see K21/K11) | Text corrected; fake-transport test for a rejected cancel-all. |
| K39 | minor | `kill_cancels_own_orders` false (same as K4) | **Accept** (see K4) | Config refuses false. |
| K40 | minor | Consecutive-UNKNOWN halt could read as `== 3`; two mechanisms | **Accept** (§5 R3#13) | `>= 3` on every UNKNOWN report, carried in payload `halts` (one mechanism); resume-then-4th test. |
| K41 | minor | evaluate_replace keeps dormant live branches; line-540 edit | **Accept** (§2, §5b C1) | Live refusal as first statement, live-only parts deleted, edit dropped. |
| K42 | minor | `_realized_loss` SETTLED fallback undercounts | **Accept: delete, fail closed** (§2b, A26) | The fold always supplies both fields; missing fields now count the cap as used (the existing "bare state" assertion still sees `daily_loss`). |
| K43 | minor | Live partial cancel keeps reachable branches | **Accept** (§1, A23) | D1 needs full cancels only; refused before any side effect; paper keeps reductions. |
