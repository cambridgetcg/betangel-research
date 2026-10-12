# Betfair Exchange client setup — 10 October 2026

Prepared **2026-10-10**. Engineering setup receipt, not evidence of profitability. No credentials existed, no network call was made and no order was placed.

**寫好咗 Betfair client 同離線測試，但未接過 Betfair：冇 credentials、冇 live call、冇落單。**

Status：`tests_passed_no_credentials_no_network_calls`

機讀版本：[betfair-setup-2026-10-10.json](betfair-setup-2026-10-10.json)。

## What was built

`betangel_research/betfair/` 係一個只用標準庫嘅 Betfair Exchange client，分三部分：

- **Read path**：certificate login、keepAlive、logout；只准七個讀取 method 嘅 JSON-RPC client；weight planner 同 throttle；catalogue join。見 [接入指南](../docs/betfair-integration.md)。
- **Stream recorder**：Exchange Stream client，每個 subscription 一個 segment，raw mcm line 逐 byte 寫入，本地收到時間放 sidecar，receipt 有 hash 同 gap 清單；每個 segment 可以直接用現有 `stream.messages()` replay。
- **Order path**：預設 paper；live 要 A1–A19 全部過、每張單喺終端機打返一次；capability 綁 request body sha256，transport 拒絕冇綁定嘅非讀取 call；journal 只加唔改、有 hash chain、送出前 `F_FULLFSYNC`；未知結果先對數、永不重發；kill switch 預設只取消自己嘅單。見 [落單路徑](../docs/betfair-order-path.md)。
- **v1 live 範圍（Yu 2026-10-10 決定，[amendment](../.team-work/betfair-exchange-20261010/amendment-v1-live-scope.md)）**：live 只有 LIMIT／LAPSE 落單、整張取消自己嘅 bet、kill（唔攞 lock，固定喺 KILL file 之後 20 秒同 35 秒再掃）、live-probe（落單再取消）、對 UNKNOWN 數，同人手 `settle-market`。冇 live replace、冇 live 減 size、冇自動 live settlement（cleared rows 永遠唔會 close 或 settle 一張 live 單，settle 咗嘅 market 按 worst case 入數）、live 風險上限冇 hedge credit。Paper 保留晒。

外部事實 77 條（F01–F77）：76 條喺 2026-10-10 記錄，F77 係 v1 live 收窄時加嘅推斷（冇新來源）。73 條嚟自 Betfair 自己嘅頁面（文件、政策、資料規格同 support article，其中 3 條經 Wayback 存檔；F62 另外有一個非 Betfair 嘅二手來源），3 條嚟自 macOS man page（F68、F73、F74），1 條係本地 Python 檢查（F69）；本地檢查一共 2 個（F69 同 F74 嘅 SQLite 部分；F74 嘅 retrieval 列埋 `local_python_check`、secondary 係 `inferred`）。當中 3 條係推斷（F67、F69、F77），6 條未驗證（F30、F33、F36、F40、F56、F73）。**冇一條喺 live API 上驗證過**。等級、衝突同 first-live-test checklist 見 [fact register](../docs/betfair-facts.md)。

## Verification

| 項目 | 結果 |
|---|---|
| 全套測試（`discover -s tests`，`BETANGEL_BETFAIR_FORBID_NETWORK=1`，property 設定 300 seeds × 200 steps） | passed `889`，failed `0`，skipped `1`（要 `BETANGEL_BETFAIR_MERGE_GATE=1` 先跑嘅 merge gate 測試）；其中原有測試 142 個 |
| 全套測試 log sha256 | `974bbf505208c21b8194a62f84e5c7bb9d5386757cf29943665c587fa51f9a01` |
| 全套測試 wall time（秒，3 位小數；unittest 嘅 `Ran … in …s`） | `257.773` |
| Property test module（`test_betfair_risk_properties`，300 seeds × 200 steps，單獨再跑一次） | ran `9` 個，wall time `181.825` 秒，log sha256 `96920cd97cef7f9bdb5ff8fa457d3855adc2260a2f451583864fe65d742d8e5d` |
| 執行嘅 Python | 3.14.6 |
| Python 3.10 | 只做 syntax 檢查（`ast.parse(feature_version=(3, 10))`，67 個 betfair 檔案）；呢部 Mac 冇 3.10；`test_betfair_syntax_floor` 搵到 3.11 就額外 compile |
| Python 3.11 | 3.11.15（uv 裝嘅 `python3.11`）喺最終 tree 另外跑全套（`-v`，同一個 `BETANGEL_BETFAIR_FORBID_NETWORK=1`，property 用預設 40 × 120）：ran `890`，OK，skipped `2`（merge gate 測試，同只適用於 3.12+ tokenizer 嘅 PEP 701 自我測試）；log sha256 `dae4c97dbb5e2edc7cdf6d161c58f625d2ef1885b3417ce3f7b2f184ab67d9f1`（log 喺 `outputs/`，唔入 Git） |
| Code sha256 | 見 JSON `code_sha256`（`receipts.code_hashes`） |
| 網絡 call | 0：由離線測試設計保證（`block_network`、`BETANGEL_BETFAIR_FORBID_NETWORK=1`、假 transport），唔係量度出嚟 |
| Credentials | 冇 |
| Live 單 | 0 |
| First-live-test | 一個都未做 |
| 離線 CLI smoke | `init-config`、`doctor`、`status`、`keychain-setup`、paper `place`／`replace`／`cancel`／`paper-step`、`kill`（exit 3，kill file engage 咗）同 `resume`、synthetic recording 上嘅 `paper-replay`（兩次 report.json 一模一樣）；live `place` 被 arming 拒絕（exit 2）、冇開 live journal。全部喺臨時 `--state-root` 同 `BETANGEL_BETFAIR_FORBID_NETWORK=1` 之下，冇讀 Keychain。呢個 smoke 係喺 audit 修正之前嘅 tree 做；修正之後冇再人手 smoke，CLI 只由 `test_betfair_cli` 覆蓋；v1 live 收窄之後亦冇再人手 smoke（呢個 step 唔可以叫真嘅 `/usr/bin/security`，而 `doctor` 會查 Keychain item） |

所有測試都係離線：假 HTTP transport、假 stream socket、假 Keychain runner、假 tty、假時鐘；`block_network` 會令任何真 socket 或者 `/usr/bin/security` call 即刻失敗。

**呢啲檢查喺測試過嘅情況下睇到嘅**（integration 跑完 suite，下面每個測試 module 都存在、都過咗）：

- transport gate 拒絕冇綁定 capability 嘅非讀取 call：`test_betfair_transport`、`test_betfair_api`
- live arming 逐項檢查：`test_betfair_arming`
- 風險上限，包括生成序列：`test_betfair_risk`、`test_betfair_risk_properties`
- journal 只加唔改、hash chain：`test_betfair_journal`
- 未知結果唔當零、唔重發：`test_betfair_lifecycle`、`test_betfair_reconcile`
- kill 之後唔再接受新單：`test_betfair_killswitch`、`test_betfair_gateway`
- secret 唔寫入檔案或 exception：`test_betfair_secret`

- 2026-10-10 audit 嘅 confirmed findings（resume 同 kill 嘅 race、`.gateway.lock` 被刪、startup／accept 對數、時鐘跳、paper 過度 fill 等）各有 regression test，改動同 spec 修正列喺 [brief](../.team-work/betfair-exchange-20261010/brief.md) 嘅 "Audit fixes and spec amendments"：`test_betfair_gateway`、`test_betfair_reconcile`、`test_betfair_killswitch`、`test_betfair_journal`、`test_betfair_paths`、`test_betfair_paper`、`test_betfair_book`
- 收窄咗嘅 tree 再 review 一輪（2026-10-11）：kill 唔等被鎖住嘅 journal、一個 market 嘅 cancel 出錯唔會停 sweep、kill 查 account、冇終端機嘅 account-wide kill 照 sweep 自己嘅 bet、每個 exit 3 都寫 HALT、exit 5 每張單講實際情況、withheld 單同 kill 取消咗嘅單講清楚、emergency login 同 journal 交錯 append 嘅強制測試：`test_betfair_killswitch`、`test_betfair_cli`、`test_betfair_journal`、`test_betfair_reconcile`、`test_betfair_facts_doc`
- v1 live 收窄、第三輪 audit 同 Astra C1–C4：live replace 同 live 減 size 喺任何副作用之前拒絕、cleared rows 永遠唔會令 live 單變終結、`settle-market` 嘅前提同 banner、kill 唔攞 lock 而且固定再掃、live 冇 hedge credit、送第一個 byte 之前嘅 gate、503 同 gap window、replay provenance、exit 5 同 signal、HALT 同 event 一齊寫：`test_betfair_gateway`、`test_betfair_reconcile`、`test_betfair_killswitch`、`test_betfair_cli`、`test_betfair_risk`、`test_betfair_transport`、`test_betfair_api`、`test_betfair_journal`、`test_betfair_lifecycle`、`test_betfair_boundaries`、`test_betfair_book`、`test_betfair_paper`、`test_betfair_config`、`test_betfair_arming`
- 第四輪 review（2026-10-11，21 個 finding）：kill 遇到讀唔到或者開唔到嘅 journal、打錯嘅 `--profile` 照 engage 同 sweep，login ledger 嘅指示按原因講，lazy login 沿用 emergency flag，open 時嘅 move 用 compare-and-append、report 嘅 move 被略過就 HALT，cleared-row 單同停咗嘅 cancel 講啱嘅嘢，印出嚟嘅命令帶 `--bet-angel-paused`，之前 UNKNOWN 嘅單喺 open 時發現 live 就拒絕、reconcile 留低 unresolved 單係 exit 5，banner 講明唔送單，paper 中斷咗嘅 request 同 closed market，merge gate 要 README commit、md 數字同 JSON 一致、first-live-test 係 T0–T10：`test_betfair_killswitch`、`test_betfair_cli`、`test_betfair_gateway`、`test_betfair_auth`、`test_betfair_reconcile`、`test_betfair_reports_no_account_data`、`test_betfair_facts_doc`
- Astra re-review（2026-10-11）嘅 R1／R2：Keychain anchor 只會向前（`publish_anchor` 逐個寫、喺 lock 入面先讀 journal head；kill 只喺 sweep 之後寫，寫唔到都照 sweep），paper closed market 留低 closed 標記（同一個 process、reopen 或者死咗之後、標記上限同 floor、R2 之前嘅 paper state；`place`／`replace` 收到 CLOSED book 都 close 個 market，舊過留低 book 嘅 CLOSED book 嘅標記唔踢，冇咗 paper lock 嘅 gateway 唔寫 paper state）：`test_betfair_credentials`、`test_betfair_gateway`、`test_betfair_killswitch`、`test_betfair_boundaries`、`test_betfair_journal`

**檢查唔到嘅：** Betfair 真係咁運作（要 first-live-test）；測試冇覆蓋嘅序列同情況；paper fill 等於真實成交；任何策略有 edge。

## Reviews

Codex（Astra）嘅 blind review `report_bc2db951`（frozen state `17fa5d1c`，verdict **request_changes**，1 個 P1、3 個 P2）。四個 finding 都係保留落嚟嘅 live／paper 範圍，全部要改，全部改咗；每個都有 regression test，而且每個新測試喺還原咗修正嘅副本上面都 fail。下面係實作者嘅摘要，唔係獨立覆核：

| Finding | 問題 | 點樣改 | 測試 |
|---|---|---|---|
| C1（P1） | Hedge credit 用舊嘅平均成交價：已 match 嘅 BACK 平均價變咗（例如 Rule 4 reduction），journal 唔知，再落一張 LAY 會因為舊嘅正數 credit 而過關，真實 worst case 超過 cap | Live 風險上限冇 hedge credit：已 match 部分只計佢嘅虧損（預設）；`hedge_credit=True` 只可以喺 paper 嘅 R8–R10、R12 嘅 before 數同 paper `status`／journal summary 明確傳入；live `status` 顯示 `exposure_basis: "worst_case_no_hedge_credit"`。Astra 嘅例子而家喺 R8 被拒，冇送任何 LAY | `test_betfair_risk`、`test_betfair_gateway`、`test_betfair_boundaries` |
| C2（P2） | Book 同 capability 嘅到期時間去唔到送出嗰刻：(a) journal submit 之後兩個時鐘再行 3 秒，舊 book 照送；(b) 慢嘅 TLS 連線令 request 喺 capability 過期之後先寫出去 | placeOrders 一定要帶 `send_by = min(book 新鮮度, capability 到期)` 同 first-byte gate（KILL file、兩個時鐘嘅 book 年齡），transport 喺連好線之後、第一個 byte 之前查；被拒係 FAILED `transport_not_sent`、零 byte。其他 method 帶呢兩個參數會被拒；funds 改喺 book 之前讀；cancel 嘅 capability 喺 submit append 之後先 mint | `test_betfair_transport`、`test_betfair_api`、`test_betfair_gateway` |
| C3（P2） | 冇 `mc` 嘅全域 503 heartbeat 喺 replay 入面消失，之後嘅健康 delta 當連續歷史，paper 照 fill | 503 一開始，cache 入面每個 market 都出唔可用 snapshot（有冇 `mc`、改咗邊個 market 都一樣），恢復時再出可用嘅；跨 503 冇被動 fill，503 期間成交嘅量唔計 | `test_betfair_book`、`test_betfair_paper` |
| C4（P2） | Replay 嘅 source hash 冇包 receipt 同 sidecar：加唔加 receipt 結果唔同，但 `source_sha256` 一樣 | Report 加 `inputs.dependencies`（receipt、partial receipt、recv sidecar 嘅在唔在、sha256 同用咗邊個）同 `inputs.resolved`（key type 同來源、gap window 同狀態）；ledger 嘅 `source_sha256` 包埋兩樣 | `test_betfair_paper` |

Astra 嘅 S0 review `report_6e540b09`：歷史 as-deployed receipt `reports/direct-collection-2026-10-10.json` 一個 byte 都唔改；handoff 同 brief 入面「S0 之後要更新 receipt」嘅指示刪咗，改做 `merge_gate.historical_receipts`，receipt 亦加入 `protected_sha256`。

第三輪離線 audit 嘅 19 個 confirmed findings（R3#0–R3#18）同 Yu 嘅收窄決定，逐項處理喺 [amendment](../.team-work/betfair-exchange-20261010/amendment-v1-live-scope.md) §5 同 [brief](../.team-work/betfair-exchange-20261010/brief.md) 嘅 "v1 live scope narrowing"。

收窄咗嘅 tree 再做咗一輪離線 review（2026-10-11；live money、operator 用法、kill 嘅信心同測試覆蓋），20 個 finding：19 個改咗，每個都有 regression test，而且每個新測試喺還原咗修正嘅副本上面都 fail；一個部分拒絕。實作者嘅摘要，唔係獨立覆核；同 amendment 唔同嘅地方寫喺 [brief](../.team-work/betfair-exchange-20261010/brief.md) 嘅 "Review of the narrowed tree"：

| 範圍 | 改咗乜 |
|---|---|
| Kill 唔會漏 sweep | 一個 market 嘅 cancel 拋 exception 唔再停其他 market、再掃同最後核實（`kill_incomplete`）；開已有 schema 嘅 journal 只讀，kill 嘅 connection 最多等 2 秒，kill event 寫唔入就唔寫、照送晒 cancel（`kill_incomplete`）；之前一個卡住嘅 writer 令 kill 等 30 秒之後一個 cancel 都唔送 |
| Kill 唔會報錯 exit 0 | Login 嘅 account 唔係 journal 嗰個（只讀比較）就 `kill_incomplete`；冇終端機嘅 `--scope account` 照 sweep 自己嘅 bet、account-wide 部分 `not_sent`；CLI engage 失敗時再掃由 gateway 嘅 engage 計；每個 exit 3 都寫 HALT `kill_incomplete`；JSON `session` 用 gateway 真正嘅 login |
| Operator 見到嘅字 | Exit 5 改做「開始咗落單或取消 request、冇照要求完成」，每張單一行講實際情況：冇送出嘅 FAILED 講原因（kill 嘅就唔講 fresh place 安全）、cancel 唔講 "do not resend"、仲 live 嘅 bet 叫你再 cancel；kill 列出 `cancel_requested_bet_ids`，講明佢哋喺 status 仍然按 worst case 計直到 `settle-market`；withheld 單有自己一行；`reconcile` 每項有 `error_code`；kill complete 嗰行改講「冇 unmatched 部分」；`settle-market` 嘅建議加 `--accept`；config 錯講原因 |
| 拒絕 | Live cancel 遇到 withheld 單就拒絕：佢喺 exchange 上面仲可以有真正 live 嘅剩餘，cancel 永遠唔可以更嚴 |
| 事實同測試 | F74 嘅 retrieval 列埋 `local_python_check`、secondary `inferred`；kill 嘅 emergency login 用 login ledger 驗；兩條 connection 交錯 append 改成強制插入；`settle-market` 嘅 risk command 身份同普通 login 有測試 |

第四輪離線 review（2026-10-11；live money、unknown outcomes、kill、operator 用法、paper 同 repo-fit）有 21 個 confirmed findings（3 個 major）：全部改咗，每個都有 regression test，而且每個新測試喺還原咗修正嘅副本上面都 fail。實作者嘅摘要，唔係獨立覆核；改法同偏離 amendment 嘅地方（login ledger 嘅指示、reconcile 留低 unresolved 單係 exit 5）寫喺 [brief](../.team-work/betfair-exchange-20261010/brief.md) 嘅 "Round 4 review fixes"。

Astra（Codex）覆核咗第四輪之後嘅候選 tree：re-review `report_ef3fb90d-3ad9-4150-9b50-4796f91a4c32`（checkpoint `43bb3da9…`），verdict **request_changes**，有兩個新嘅 P2 regression，冇新嘅 P1。C1–C4 喺支援嘅 production／replay 路徑上接受為 closed：C2 接受喺 API 層執行；transport 層嘅 guard 係可選嘅 defence in depth，冇做。R1、R2 已經改咗，每個都有 regression test，等 Astra 再覆核。下面係實作者嘅摘要，詳情喺 [brief](../.team-work/betfair-exchange-20261010/brief.md) 嘅 "Re-review fixes (2026-10-11)"：

| Finding | 問題 | 點樣改 | 測試 |
|---|---|---|---|
| R1（P2） | 兩個寫 anchor 嘅 process（例如唔攞 lock 嘅 kill 同一個 holder）各自冇 lock 咁「讀 journal head，再寫 Keychain」，慢嗰個會用舊 head 蓋過新嘅：Keychain anchor 退後，一個已經過時嘅 journal snapshot（冇咗之後嘅 HALT）過到 anchor 檢查 | 所有 anchor 寫入都經 `credentials.publish_anchor`：per-profile lock，喺 lock 入面先讀 journal head，所以 anchor 唔會退後；append 唔攞呢個 lock。Kill 只喺 `close()`、sweep 之後寫一次，最多等 2 秒，寫唔到只係 warning，唔會停或延遲 cancel。2026-10-12 R1 覆核（4 個 finding，實作者摘要，Astra 未覆核）：lock 入面再讀 Keychain anchor，head 要按 A13 接得上先寫（journal 喺 session 期間被還原就 `journal_anchor_not_extended`，乜都唔寫）；寫嘅期間 lock 檔被換就喺新 lock 入面再寫（最多 3 次）；淨係 RISK 命令嘅 `open()` 等 30 秒，其他（cancel、reconcile、所有 `close()`、kill）等 2 秒 | `test_betfair_credentials`、`test_betfair_gateway`、`test_betfair_killswitch`、`test_betfair_boundaries` |
| R2（P2） | Round 4 刪 closed market 嘅 paper state 時連 CLOSED book 都刪埋：close 之前嘅 OPEN book 再被接受，知道結果之後再食同一個顯示深度，再 step CLOSED 就 settle 多一張 200p、賺 500p 嘅注 | `paper_state` `schema_version` 2 留低細嘅 closed 標記（`closed`：`markets`、`floor_ms`）；見過 CLOSED 嘅 market 唔再接 paper 單或者 CLOSED 以外嘅 book（`paper_market_closed`）；最多 256 個標記，踢走嘅由 `floor_ms` 頂住；舊 version 1 照讀。2026-10-12 R2 跟進（實作者摘要，Astra 未覆核）：`place`／`replace` 收到 CLOSED book（照樣 V9 拒絕）同 `paper-step` 忽略嘅 CLOSED book 都即刻寫標記（一條 `note`），之後 close 之前嘅 book 唔再被接受。再覆核一輪（4 個 minor finding，全部處理咗）：CLOSED book 舊過 market 留低嘅 OPEN book 嘅話，嗰個標記永遠唔踢（以前踢咗就重開個 market）；`.paper.lock` 被換咗嘅舊 gateway 唔再寫 paper state（`gateway_lock_lost`；以前會抹走新 gateway 嘅 `taken` 同標記）；report 同 handoff 嘅數字重跑咗 | `test_betfair_gateway`、`test_betfair_journal` |

**A20 已決定。** Yu 喺 2026-10-11 決定保留保守做法（collab record `report_94134b37-5c9d-4a98-b1de-9bae35a45a37`）：Betfair 唔再返嘅 market 一直按 worst case 計，market close 之後盡快 settle。之後可能會考慮一個「forget-market」，但要等 first-live T-tests 做完，再重新設計同 review；v1 一定唔會有。

## Not done

- 冇設定 credentials，冇買 £499 Live App Key，冇 live login、讀取、stream 或落單。
- 冇雲端部署；S0 套用之後 Docker image 會排除 `betangel_research/betfair/`。
- 冇自動策略；程式 API 只限 paper。
- 冇 order stream、Heartbeat API、in-play、其他 order 類型。
- v1 刻意冇 live replace、冇 live 減 size、冇自動 live settlement、live 冇 hedge credit；Betfair 唔再返嘅 CLOSED market 喺 v1 settle 唔到，佢嘅 worst case 會一直計住（Yu 2026-10-11 決定保留，amendment A20：market close 之後盡快 settle）。`settle-market` 嘅前提（F77）係推斷。

## Merge gate

Shared-file patch S0（`AGENTS.md`、`betangel_research/__init__.py`、`.gitignore`、`.dockerignore`）已經套用：Yu 同 Codex 同意之後，coordinator 做咗一個獨立 commit，四個檔案都係 handoff `s0_expected_sha256_after` 嘅 hash；merge 去 main 之前仲要過 merge gate（`BETANGEL_BETFAIR_MERGE_GATE=1`）。S0 只改本地 `.dockerignore`；[direct-collection receipt](direct-collection-2026-10-10.json) 係 2026-10-10 as-deployed 嘅歷史收據，一個 byte 都唔改（佢喺 `protected_sha256`，merge gate 會拒絕任何改動），之後真正 build／deploy 先另寫新收據。S0 嘅 AGENTS.md bullet 寫明收窄咗嘅 live 範圍。README 改動係 rebase 之後另一個最後 commit。Patch 原文、預期 hash 同受保護檔案 hash 喺 [handoff](../.team-work/betfair-exchange-20261010/handoff.json)。

## Next

First-live-test T0–T10（T11 跟 live replace 一齊剔走）每一項都要 Yu 明確同意、本人喺終端機前面。Raw response 只留喺 `~/Library/Application Support/betangel-research/betfair/profiles/<p>/live-tests/`；呢度只會加 test id、status、error code、field 名、boolean、數量、時間長度同私人收據嘅 sha256。
