# Betfair Exchange integration — 2026-10-10

**範圍。** 喺 `betangel_research/betfair/` 起一個只用標準庫嘅 Betfair Exchange client：delayed-key REST 讀取、Exchange Stream recorder、一個預設 paper 嘅落單閘門（live 要逐項 arming，每張單喺終端機打返）。Binding spec 係 coordinator 嘅 `final.txt`（§0–§18），原文一字不改 commit 咗喺呢個目錄：[`final.txt`](final.txt)（sha256 喺 `handoff.json` 嘅 `artifact_sha256`）；之後嘅修正喺下面 "Audit fixes and spec amendments"。代碼同測試入面嘅 "spec section N" 全部指呢份 spec 嘅章節號，唔係 docs 自己嘅章節號。外部事實喺 `research/betfair-facts.json`。

**授權邊界。** 冇 credentials、冇任何 live call、冇落單、冇購買（£499 Live App Key 未買）、冇雲端部署。任何 first-live-test（T0–T10；T11 後來跟 live replace 一齊剔走）都要 Yu 明確同意、本人喺終端機前面。Implementer unit 唔改 shared files（`AGENTS.md`、`README.md`、`.gitignore`、`.dockerignore`、現有 module 同測試）；改法放喺 S0，由 coordinator 喺 Yu 同 Codex 同意之後先套用。

**Worktree。** 只喺 `/Users/macair/betangel-research-betfair`（`feat/betfair-exchange`，由 `0983037` 開始）做嘢。Codex 嘅 checkout `/Users/macair/betangel-research`（`research/offline-edge-lab`）冇人讀、冇人寫。Implementer unit 唔 commit、唔改 refs 或 index；coordinator 負責 commit。

## Units and ownership

| Unit | 負責 |
|---|---|
| U1 Foundations | `constants`、`errors`、`schemas`、`config`、`paths`、`secret`、`credentials`、`capability`、`receipts`、`filters`、`transport`；example config；facts JSON；support 同基礎測試 |
| U2 Auth + REST read | `auth.py`、`api.py` 同測試 |
| U3 Stream + recorder | `esa.py`、`book.py`、`recorder.py` 同測試 |
| U4 Order core | `ticks`、`validate`、`risk`、`pnl`、`journal`、`lifecycle`、`paper` 同測試（包括 property tests） |
| U5 Gateway + CLI | `gateway`、`reconcile`、`arming`、`killswitch`、`presence`、`cli`、`betfair/__main__.py` 同測試（包括 boundary tests） |
| U6 Docs, report, handoff | `docs/betfair-{integration,order-path,facts}.md`、`reports/betfair-setup-2026-10-10.{md,json}`、呢個目錄嘅 `brief.md` 同 `handoff.json`；`test_betfair_facts_doc`、`test_betfair_reports_no_account_data` |

## Hard rules for every unit

- 測試全部離線：`test_betfair_support.block_network` patch 晒 socket、HTTP 同 `/usr/bin/security`；inject 假 transport、假 Keychain、假 tty、假時鐘。
- 唔可以喺 `~/Library/Application Support/betangel-research` 整檔案；測試用 temp dir 同 `BETANGEL_BETFAIR_FORBID_NETWORK=1`。
- 只用標準庫；plain dict 加 `validate_*`；冇 dataclass、冇 `logging`；relative import；錯誤係 `ValueError` 嘅 subclass；Python 3.10 syntax。
- spec 唔清楚時揀較安全、對 live 單較嚴嘅解讀，並喺 deviations 寫低。

## Acceptance

1. 全套測試過（原有 142 個加新測試）：`BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests`。
2. Property tests 用 300 seeds × 200 steps 單獨再跑一次，記低 module 嘅 wall time；全套測試嘅 wall time 另外記。
3. 文件、fact register、report 同呢個 handoff 一致（`test_betfair_facts_doc`、`test_betfair_reports_no_account_data`）。
4. Report 同 handoff 冇任何 account 資料：冇 betId、冇 ref、冇餘額、冇 connection id、冇 app key 或 session token 形狀嘅字串。

## Integration steps for U6 placeholders

U6 喺 U2–U5 完成之前寫文件，所以以下要喺 integration commit 補：

1. **PAPER_ASSUMPTIONS。** `docs/betfair-order-path.md` §11 逐字抄咗 `paper.py` 當時嘅 `PAPER_ASSUMPTIONS`（9 句）。U4 再改呢個 tuple 就要同步改文件；`test_betfair_facts_doc` 逐句、按次序對。狀態圖同 `lifecycle.TRANSITIONS`、R 表同 `risk.RULE_ORDER`、acknowledgement 表同 `LIVE_ACK_IDS`（同 `arming.ACK_IDS`）都係咁對。
2. **What is implemented。** 兩份文件嘅 "What is implemented" 表同 report 嘅檢查清單列出嘅 module 同測試，`test_betfair_facts_doc.missing_named_files()` 會逐個搵；report 仲係 draft 時只係 skip，第 4 步嘅 script 見到有缺就唔會改 status。有 unit 冇交付嘅項目要刪或者改寫，唔好照留。
3. **準備。** Log 放 `outputs/`（唔入 Git；呢個 worktree 未必有呢個目錄，script 會自己 `mkdir`）：

   ```sh
   cd /Users/macair/betangel-research-betfair
   mkdir -p outputs
   ```

4. **跑 suite、驗證據、填數。** 下面嘅 script 自己用 report 記錄嘅環境變數跑兩次：全套 `discover -s tests -v`（log `outputs/betfair-integration-tests.log`），同單獨跑 `test_betfair_risk_properties`（log `outputs/betfair-property-tests.log`）。佢只會喺以下全部成立先將 status 改做 `tests_passed_no_credentials_no_network_calls`：全套過晒；property module 存在、讀 `BETANGEL_BETFAIR_PROPERTY_SEEDS` 同 `..._STEPS`、單獨跑冇 failure／error／skip 而且最少跑咗一個；文件同 report 點名嘅 module 同測試全部存在。全套有 failure 就寫 `tests_failed_no_credentials_no_network_calls`。證據唔齊就乜都唔寫，印原因之後 exit 1。`reviews` 仍然要人手填。

   ```sh
   PYTHONDONTWRITEBYTECODE=1 python3 -B - <<'PY'
   import hashlib, json, os, re, subprocess, sys
   from pathlib import Path
   sys.path.insert(0, "tests")
   import test_betfair_facts_doc as doc_checks
   import test_betfair_reports_no_account_data as report_checks
   from betangel_research.betfair.receipts import SYSTEM_CLOCK, code_hashes
   ENV = {"BETANGEL_BETFAIR_FORBID_NETWORK": "1", "BETANGEL_BETFAIR_PROPERTY_SEEDS": "300",
          "BETANGEL_BETFAIR_PROPERTY_STEPS": "200", "PYTHONDONTWRITEBYTECODE": "1"}
   PROPERTY_MODULE = Path("tests/test_betfair_risk_properties.py")
   report_path = Path("reports/betfair-setup-2026-10-10.json")
   handoff_path = Path(".team-work/betfair-exchange-20261010/handoff.json")
   report = json.loads(report_path.read_text(encoding="utf-8"))
   for key in ("command", "property_command"):
       assert all(f"{k}={v}" in report["tests"][key] for k, v in ENV.items()), f"tests.{key} differs from ENV"
   sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
   def run(args, log_path):
       Path("outputs").mkdir(exist_ok=True)
       with open(log_path, "w", encoding="utf-8") as log:
           rc = subprocess.run([sys.executable, "-B", "-m", "unittest", *args], stdout=log,
                               stderr=subprocess.STDOUT, env={**os.environ, **ENV}).returncode
       text = Path(log_path).read_text(encoding="utf-8")
       ran = re.findall(r"^Ran (\d+) tests? in ([0-9.]+)s$", text, re.M)
       tail = re.findall(r"^(OK|FAILED|NO TESTS RAN)(?: \((.*)\))?$", text, re.M)
       if not ran or not tail:
           sys.exit(f"{log_path}: no unittest summary; inspect the log")
       counts = {k: int(v) for k, v in (kv.split("=") for kv in tail[-1][1].split(", ") if kv)}
       return {"rc": rc, "ran": int(ran[-1][0]), "wall": round(float(ran[-1][1]), 3), "verdict": tail[-1][0],
               "counts": counts, "log_sha256": sha(log_path)}
   suite = run(["discover", "-s", "tests", "-v"], "outputs/betfair-integration-tests.log")
   prop = run(["discover", "-s", "tests", "-p", PROPERTY_MODULE.name, "-v"], "outputs/betfair-property-tests.log")
   failed = sum(suite["counts"].get(k, 0) for k in ("failures", "errors", "unexpected successes"))
   skipped = suite["counts"].get("skipped", 0)
   passed = suite["ran"] - failed - skipped - suite["counts"].get("expected failures", 0)
   suite_ok = suite["rc"] == 0 and suite["verdict"] == "OK" and failed == 0
   problems = []
   source = PROPERTY_MODULE.read_text(encoding="utf-8") if PROPERTY_MODULE.is_file() else ""
   if not all(name in source for name in ("BETANGEL_BETFAIR_PROPERTY_SEEDS", "BETANGEL_BETFAIR_PROPERTY_STEPS")):
       problems.append("test_betfair_risk_properties is missing or does not read the seed/step variables")
   if prop["rc"] != 0 or prop["verdict"] != "OK" or prop["ran"] == 0 or prop["counts"]:
       problems.append(f"property run not clean: ran={prop['ran']} {prop['verdict']} {prop['counts']}")
   problems += [f"named but missing: {name} ({where})" for where, name in doc_checks.missing_named_files()]
   if suite_ok and problems:
       sys.exit("evidence incomplete, nothing written:\n- " + "\n- ".join(problems))
   now = SYSTEM_CLOCK.utc_now()
   report["checked_at"] = now
   report["code_sha256"] = code_hashes(Path("."))
   report["tests"].update(passed=passed, failed=failed, skipped=skipped, log_sha256=suite["log_sha256"],
                          wall_seconds=suite["wall"], property_tests_ran=prop["ran"],
                          property_wall_seconds=prop["wall"], property_log_sha256=prop["log_sha256"])
   report["status"] = ("tests_passed_no_credentials_no_network_calls" if suite_ok
                       else "tests_failed_no_credentials_no_network_calls")
   report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
   handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
   handoff["completed_at"] = now
   handoff["artifact_sha256"] = {p: sha(p) for p in report_checks.artifact_paths(Path("."))}
   handoff["tests"].update(command=report["tests"]["command"], passed=passed, failed=failed,
                           log_sha256=suite["log_sha256"])
   handoff_path.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
   print(json.dumps({"status": report["status"], "passed": passed, "failed": failed, "skipped": skipped,
                     "log_sha256": suite["log_sha256"], "wall_seconds": suite["wall"],
                     "property_tests_ran": prop["ran"], "property_wall_seconds": prop["wall"],
                     "property_log_sha256": prop["log_sha256"], "problems_ignored_because_suite_failed": problems},
                    indent=2))
   PY
   ```

5. **抄入 report md。** 將印出嚟嘅數字抄入 `reports/betfair-setup-2026-10-10.md`：`Status：` 嗰行改做新 status（md 唔可以再提其他 status）；佢下面解釋 placeholder 嗰句改做只留機讀版本連結；Verification 表嘅數字；檢查清單前面嗰句 `PENDING_INTEGRATION` 改做「integration 跑完 suite，下面每個測試 module 都存在、都過咗」。刪晒 `PENDING_INTEGRATION`。Code sha 嗰格寫「見 JSON `code_sha256`」就得。喺 `handoff.json` 人手填 `reviews`。
6. **重算 hash。** md 改完之後 `artifact_sha256` 入面 report 嘅 hash 會過時，所以要重算（唔使再跑 suite）：

   ```sh
   PYTHONDONTWRITEBYTECODE=1 python3 -B - <<'PY'
   import hashlib, json, sys
   from pathlib import Path
   sys.path.insert(0, "tests")
   import test_betfair_reports_no_account_data as report_checks
   handoff_path = Path(".team-work/betfair-exchange-20261010/handoff.json")
   handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
   handoff["artifact_sha256"] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                 for p in report_checks.artifact_paths(Path("."))}
   handoff_path.write_text(json.dumps(handoff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
   PY
   ```

7. **再跑全套**（`BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests`）。Status 一離開 draft，`test_betfair_reports_no_account_data` 就要求 md、report JSON 同 `handoff.json` 冇 `PENDING_INTEGRATION`、`reviews` 已填、handoff 嘅 `tests` 同 report 一致；`test_betfair_facts_doc` 要求點名嘅 module 同測試全部存在。之後再改任何 betfair artifact，都要重做第 6 步（merge gate 會對 `artifact_sha256`）。

## S0 patch and merge gate

S0 原文（同 `handoff.json` 嘅 `s0_patch` 一字不差，測試會對）。佢係真正嘅 unified diff，已經喺 scratch copy 用 `git apply --check` 同 `git apply` 驗證過，套用後嘅 sha256 記喺 `merge_gate.s0_expected_sha256_after`：

```diff
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -6,7 +6,8 @@
 - Record source URLs, access dates, publication dates where visible, commercial interests and limitations. Keep paraphrases short; do not archive full articles or conversations.
 - No profitable strategy has been established here. The inherited two-race pilot is descriptive and already inspected; it is not an untouched test set.
 - Keep chronology and data availability explicit. Target-race results, revised ratings and later prices cannot become earlier predictive inputs.
-- Current implementation scope includes public-source research, offline analysis and explicitly configured direct-feed collection on a small Fly deployment. There is no betting-account integration, executable betting strategy or live order path. Source policy, attribution, publication clocks and collection gaps must remain explicit.
+- Current implementation scope includes public-source research, offline analysis and explicitly configured direct-feed collection on a small Fly deployment. There is no executable betting strategy, and no strategy may submit orders automatically. Source policy, attribution, publication clocks and collection gaps must remain explicit.
+- `betangel_research/betfair` is a Betfair Exchange client: read-only market data, a stream recorder, and one order gateway that defaults to paper mode. Its live surface is limited to LIMIT orders with LAPSE persistence that the operator types at a terminal, cancelling our own bets, the kill switch, a one-pound live probe, reconciling unknown order outcomes and settling a closed market by hand; there is no live replace and no automatic live settlement. Live orders need every arming condition in docs/betfair-order-path.md. Betfair credentials live only in the macOS Keychain; the client certificate (default `~/.config/betangel-research/betfair/`) and live order state (`~/Library/Application Support/betangel-research/betfair/`) live outside every Git worktree.
 - Preserve the imported pilot as a provenance snapshot. Develop subsequent work separately; copy it to a temporary directory for reproduction checks.
 - Keep account exports, credentials and licensed raw data outside Git. Public visibility does not imply redistribution permission. Do not add a data licence on behalf of a provider.
 - Prefer Python standard library for simple analysis. Plotting has an optional pinned dependency in the pilot. Run proportionate checks and describe precisely what they establish.
--- a/betangel_research/__init__.py
+++ b/betangel_research/__init__.py
@@ -1,3 +1,4 @@
-"""Offline research only: no credentials, account clients, or order submission."""
+"""Research package. Root modules: offline analysis plus the configured public-feed collector.
+betangel_research.betfair: Keychain-gated Betfair client; its order path defaults to paper and needs explicit live arming."""
 
 __version__ = "0.1.0"
--- a/.gitignore
+++ b/.gitignore
@@ -14,3 +14,7 @@
 *.sqlite-*
 *.db
 *.db-*
+*.crt
+*.csr
+*.p12
+*.pfx
--- a/.dockerignore
+++ b/.dockerignore
@@ -10,3 +10,5 @@
 **/__pycache__
 **/*.pyc
 **/.env*
+betangel_research/betfair/
+betangel_research/betfair/**
```

- **未套 S0 唔准 merge 去 main。** 要 Yu 同 Codex 同意，coordinator 用 `git apply` 做一個獨立 commit。同一個 commit 要：將 `merge_gate.s0_applied` 同 report JSON 嘅 `s0_applied` 都設 `true`；喺 `merge_gate.s0_agreement_recorded` 寫入 `"Yu"` 同 `"Codex"`；刪走 report JSON 嘅 S0 limitation；將 report md "Merge gate" 段改做已套用（唔再寫「未套用」）；改寫只喺 S0 之前先啱嘅句子（R3#10）：`docs/betfair-integration.md` Certificate setup 講 `.gitignore` 未 ignore `*.crt` 等、「S0 會補上」嗰個 bullet，同 "What is implemented / what is not" 入面「未改」嗰個 bullet；`docs/betfair-order-path.md` §16「未做」段嘅 S0 句；呢份 brief 嘅 Status 段；更新 report JSON 嘅 `code_sha256`（`receipts.code_hashes(".")`，佢包埋 S0 改嘅 `betangel_research/__init__.py`）；之後重做 integration 第 6 步。`s0_applied` 係 true 嘅時候，預設 suite 會拒絕 `docs/betfair-*.md` 或者 brief Status 仲有「未套用」、「未改」或「S0 會補上」。
- **預設 suite 會擋嘅（永久、唔受之後改動影響）：** `s0_applied` 係 `true` 但任何一個 S0 檔案仲係原本 bytes（即係只套咗一部分）；S0 檔案已經同 post-S0 hash 一樣但 `s0_applied` 係 `false`；冇記錄雙方同意；report 同 handoff 嘅 `s0_applied` 唔一致；套咗 S0 但 `.dockerignore` 冇 betfair 排除（或者排除之後再有 `!` 規則）、`.gitignore` 冇 `*.crt`／`*.csr`／`*.p12`／`*.pfx`。
- **Merge gate（`BETANGEL_BETFAIR_MERGE_GATE=1`，merge 之前跑）先會擋嘅：** report 未 final；S0 未套或者唔係四個檔案都正好係 post-S0 hash；任何受保護檔案同 `protected_sha256` 唔同（包括 Codex 嘅歷史 deploy receipt `reports/direct-collection-2026-10-10.json`；README 只准係 `readme_commit` 嘅樣：四行 Betfair 連結（兩份 docs 加 facts doc 同 setup report）喺 collection-pipeline 連結之後、舊 Current-state 行已換走、`local_verification_line` 喺 `## Local verification` 之下出現一次）；`artifact_sha256` 同 tree 唔夾；report 嘅 `code_sha256` 同 `receipts.code_hashes` 對 tree 計出嚟嘅唔夾；handoff 仲有 `PENDING_INTEGRATION`。預設 suite 唔比較呢啲 hash，所以 merge 之後 Codex 改 `collector.py` 或 `AGENTS.md` 唔會令 suite 變紅。
- `.dockerignore` 改動係**必須**嘅（後面嘅規則覆蓋 `!betangel_research/**`）。`reports/direct-collection-2026-10-10.json` 係 2026-10-10 as-deployed 嘅歷史收據，一個 byte 都唔改；S0 之後真正 build/deploy 先另寫新收據（Astra S0 review `report_6e540b09`；`merge_gate.historical_receipts`；佢亦喺 `protected_sha256`，merge gate 會拒絕任何改動）。
- `AGENTS.md` 新 bullet 同 spec §16 原文有兩處唔同：(1) spec 將 certificate 同 live state 一齊寫成 `~/Library/Application Support/...`；certificate 預設其實喺 `~/.config/betangel-research/betfair/`，所以分開寫（Astra 確認）；(2) 佢寫明收窄咗嘅 live 範圍（LIMIT／LAPSE 落單、取消自己嘅 bet、kill、一鎊 live probe、對 UNKNOWN、人手 settle CLOSED market；冇 live replace、冇自動 live settlement），所以 `s0_expected_sha256_after` 嘅 `AGENTS.md` 跟住改咗；其他三個 S0 檔案冇變。
- `betangel_research/__main__.py` 唔使改；`docs/collection-pipeline.md` 同 `research/data-provider-options.json` 唔改，新文件會 cross-reference。
- README 改動係 rebase 之後最後一個獨立 commit；要加嘅行同要換嘅行喺 `merge_gate.readme_commit`（Start here 加四行，令 README 繼續連住每份 doc 同 report；Local verification 加一行，數字指去 setup report，唔喺 README 寫死，所以每輪重跑唔會令佢過時）。
- Merge 咗 Codex 對受保護檔案嘅改動之後：`depends_on_sha256` 嘅檔案要重跑 R1–R3（`test_betfair_recorder`）、`test_betfair_paper` 同 `test_betfair_pnl`；再更新 `protected_sha256`（handoff 同 report 兩邊）同 `depends_on_sha256`；改到 S0 檔案就要對住新嘅 pre-S0 內容重整 `s0_patch`、`s0_expected_sha256_after` 同上面嘅 diff；改到任何 `betangel_research/*.py` 就要更新 report 嘅 `code_sha256`；之後重做第 6 步。只有 merge gate run 會因為未更新而 fail。

## Audit fixes and spec amendments（2026-10-10）

Integration 之後一輪離線 audit 嘅 confirmed findings 已經改好，每個 critical／major 都有 regression test。以下係同 `final.txt` 唔同、或者 `final.txt` 冇講嘅地方；全部揀較安全、對 live 單較嚴嘅讀法：

- **§9.8 resume。** 提示顯示 journal head 同 halt reasons；打完句子之後喺 `.gateway.lock` 之下再查 kill file，`resume` 係 compare-and-append（head 變咗就拒絕），event 帶 `cleared_through_seq`，fold 只清嗰個 seq 或之前嘅 halt。提示期間唔攞 lock。
- **§9.3 / §2.3 lock 同 submit。** `file_lock` 鎖到之後要 `fstat(fd)` 同 `stat(path)` 一致，唔一致就重開；yield 一個 `HeldLock`。Place／replace 嘅 `submit` 前要 lock 仲指住同一個檔（否則 `gateway_lock_lost` + HALT），而且由 risk decision 嘅 fold 到 submit 之間 journal 只有呢張單自己嘅 event（`journal_changed_since_decision`）；`submit` 用 `append(expect_head=...)`。
- **§2.10 rule 3 digest gate。** 除咗 `order_digest == sha256(body)`，個 digest 仲要係同一個 process 一個已 consume 嘅 capability 記低、未用過嘅（`capability.take_consumed`）。
- **§9.9 startup reconcile。** Exchange bet 只按 betId 算係我哋嘅，或者 ref 對到一張仲 unresolved、未有 betId 嘅單；其他 ref match 都係 foreign。Exchange 話 EXECUTABLE 但 journal 唔係 EXECUTABLE／CANCEL_PENDING／unresolved：`contradictions`，HALT `exchange_journal_contradiction`，A17 失敗。
- **§9.7 reconcile。** 不靠 ref 搜尋同 child 搜尋（child 搜尋已被 v1 live 收窄取代，見下）加 `sourceIdKey` 腿；poll、window、最後 5 秒同硬上限用 monotonic clock，wall clock 跳超過 2 秒就保持 UNKNOWN（`clock_step_during_reconcile`）；`reconcile --accept` 要同一次 open 嘅 startup reconcile 乾淨（`accept_startup_not_clean`）。
- **§9.8 kill。** **（已被 v1 live 收窄取代，見下。）** 再掃嘅等待用 monotonic clock 量，每次最多 30 秒；journal fold 唔到時 kill 照用 exchange 腿 sweep（journal 只收唔郁單嘅 kill、halt、note、`cancel_intent`、cancel `submit`、`report`）。
- **§0.5 / A15 registry。** 另一個 profile 嘅 live journal 帶住同一個 fingerprint 都會拒絕；fingerprint 用 strip + casefold 之後嘅 username；registry 寫入被 leak guard 拒絕會變 `account_registry_write_refused`，唔會由 `open()` 拋出去。Username 仍然係 `Secret`，但唔再做 substring 掃描。
- **§6 NZ。** `api.betfair.com.au` 只可以配 `com.au`（AUS/NZ SSO host，F12）；`com` 加 `.au` 拒絕。
- **§11 errors。** `account_aping` 加 `TOO_MANY_REQUESTS`（rate_limited）、`TIMEOUT_ERROR`／`UNEXPECTED_ERROR`／`SERVICE_BUSY`（unknown_outcome）、`INVALID_INPUT_DATA`（planner_bug）。
- **§2.12 / F37 groupBy。** `GROUP_BY_VALUES` 冇 `EXCHANGE`（GroupBy enum 冇呢個值）。
- **§2.9 filters。** `motif_compatible` 要有 `EX_BEST_OFFERS`，拒絕 `EX_ALL_OFFERS`、`SP_TRADED`、`SP_PROJECTED`。
- **§13 paper-v1。** （`paper-state.json` 已被 R3#12 嘅 journal `paper_state` 取代，見下。）生效用嗰個時間最後一個 snapshot；被動 fill 只計生效之後嘅 interval；traded volume 用 high-water mark，唔會重複計；即時 fill 嘅 takes 累積、跨 snapshot 同跨 process（`paper-state.json` 嘅 `exchange`）；舊過上一個 book 嘅 book：`place`／`replace` 拒絕、`paper-step` 忽略；兩個 OPEN snapshot 之間 version 變咗唔 fill，另一隻 runner 新變 REMOVED 就 lapse。Full-depth stream ladder suspend 再開之後、未有 image 之前帶 `invalid_reason="ladder_partial_after_suspension"`。Recording gap 嘅處理見下面第二輪 audit。
- **文件。** 人手 KILL 用 `kill` 命令或者 passwd home（唔係 `$HOME`）；`handoff.reviews` 標明係 integration 摘要、未驗證；fact 摘要分清來源等級。

**第二輪 audit（2026-10-10）。** 再一輪離線 audit 嘅 22 個 confirmed findings 已經改好，每個 critical／major 都有 regression test，之後重跑咗成個 suite。同 `final.txt` 唔同或者 `final.txt` 冇講嘅地方：

- **§9.8 step 3 kill 等 lock。** **（已被 v1 live 收窄取代，見下。）** Spec 寫等 `order_deadline_s + 5`，但 §9.7 准一個 call 喺 lock 之下對數 ~40 秒，kill 就 `kill_incomplete_no_session`、sweep 冇跑。而家攞住 lock 嘅 process 一見 kill 嚟到（KILL file 出現，或者另一個 process journal 咗 `kill`；cancel／reconcile 喺已經 kill 咗嘅情況下開，只睇開咗之後嘅新 kill）就停對數（`reconcile_unknown(abort=...)`、`reconcile_abandoned_for_kill`），取消自己啱啱落嘅單，`close()` 放 lock；kill 等 `gateway.kill_lock_wait_s`（`2 × order_deadline_s + timeout_s + poll_interval_s + 5`，預設 36 秒）。`reconcile` 命令見到 kill 會 `stopped_for_kill`。
- **§9.3 step 4 resolve on open。** 只 fold 一次揀 UNKNOWN，最新嗰張先，之後每對完一張先再 fold；kill 嚟到就停。
- **§A18 / V10 book 年齡。** 兩個時鐘都要過（macOS monotonic clock 瞓覺時停）；送出前再比兩個時鐘，wall clock 行多過 monotonic 2 秒以上就 REFUSED `book_clock_disagreement_before_send`。
- **§9.7 / settlement。** **（已被 v1 live 收窄取代，見下。）** Response 金額容許 double 誤差（同一個 penny 相差 ≤ 0.000001）；`found_move` 嘅 size 等式加 `sizeVoided`（lifecycle 同 schema 一齊改）；淨係 VOIDED row 要 void size 正好等於已 match 部分，settle 仲要 market CLOSED，否則保持原狀（`void_unconfirmed`）；LAPSED／CANCELLED rows 只接受累計讀法（加埋等於成張單）；已知有 match size 嘅單永遠唔會 ABSENT（`matched_size_known_never_absent`，`--accept` 拒絕）；startup reconcile 比較 matched size（`exchange_matched_size_exceeds_journal`）。
- **§2.10 TLS。** `make_tls_context` 唔再用 `ssl.create_default_context()`（會讀 `SSLKEYLOGFILE`）；`check_tls_context` 拒絕有 key log 嘅 context。
- **§2.2 / §5 secret 掃描。** Key passphrase 唔做 substring 掃描；少過 8 個字嘅值一律唔掃；`cli._safe_text` 喺 fold whitespace 同截斷之前掃描。`kill` 遇到 `OSError`（例如 state root 權限壞咗）都係 exit 3、印 JSON 同指示，而且會先 `paths.ensure()` 修返 root。
- **§8 / §13 stream 同 paper。** （503 同 gap 規則已被 C3、R3#7、R3#8 擴充，見下。）503 期間改到嘅 market 以 `invalid_reason="stream_unreliable"` 出 snapshot（schema 加呢個值），第一個健康 message 再出可用嘅；suspend 期間收到（丟咗）atb／atl 都標 `ladder_partial_after_suspension`；paper replay 讀 receipt segment 範圍，gap 入面生效嘅單 REFUSED `paper_book_in_recording_gap`，跨 gap 冇被動 fill；report 嘅 divisor limitation 跟 config。
- **文件同 handoff。** F01 寫明 AUS/NZ 用 `.com.au`，docs 唔再引用 repo 以外嘅 "fact sheet"；F68 改 `partially_unverified`（exit 44 係推斷）並列入 open questions；scope 唔再將 F73 當 official。`final.txt` 第 2314 行引用嘅 `/private/tmp/...scratchpad/...` 係當時 session 嘅暫存位置，唔會長存、唔喺 repo，只係歷史記錄；`final.txt` 一字不改（hash 喺 `artifact_sha256`）。測試唔再假設 source tree 一定係 git checkout（`support.IN_GIT_CHECKOUT`）；syntax floor 用舊 interpreter `compile()`。

## v1 live scope narrowing (Yu decision 2026-10-10)

**第三輪 audit、v1 live 收窄同 Astra review。** 第三輪離線 audit 有 19 個 confirmed findings（R3#0–R3#18）；Codex（Astra）嘅 blind review `report_bc2db951` 係 request_changes（C1 P1，C2–C4 P2，全部要改）；Astra 嘅 S0 review `report_6e540b09` 要求歷史 receipt 一個 byte 都唔改。Yu 喺 2026-10-10 決定（D1–D5，binding）：唔再對成個 surface 做多輪 audit，改為收窄 v1 嘅 live 範圍。完整 amendment（A3 revision 2，原文一字不改；2026-10-11 之後嘅改動全部標明日期：A20 嘅決定同 re-review R1／R2，見下面 "Re-review fixes (2026-10-11)"）喺 [`amendment-v1-live-scope.md`](amendment-v1-live-scope.md)，入面有逐個命令嘅範圍表、刪咗嘅代碼清單、每個 R3 finding 嘅 mapping、測試計劃同 K1–K43 critique 嘅取捨；同 `final.txt` 或者上面嘅修正衝突嘅地方，以佢為準。原則：有疑問就刪 live 代碼，唔加 guard；更少 live 狀態、命令同分支；live 單永遠唔會因此變得更寬鬆；paper 保留晒（D5）。

- **Live 範圍（D1）。** 保留：`place`（只限 LIMIT、LAPSE）、`cancel`（只限自己嘅 bet、只可以整張取消）、`kill`、`live-probe`（落單再即刻取消）、解決 UNKNOWN 嘅 `reconcile`，同新嘅 `settle-market`；讀取命令冇改。逐個命令嘅範圍、拒絕訊息同 exit code 表喺 `docs/betfair-order-path.md` §1。
- **D2：live replace 刪咗。** CLI 喺任何 Keychain、journal、lock、login 或網絡之前拒絕（`live_replace_removed_v1`，exit 2，訊息教你 cancel 再 place）；`_replace_live`、`_refresh_parents`、replace 嘅 live 對數分支、`replaceOrders` 字串、`--replace-step` 同 T11 全部刪咗，唔係留低唔用。Paper replace 保留。Live `--size-reduction` 亦一樣喺任何副作用之前拒絕（`live_partial_cancel_removed_v1`）。
- **D3：冇自動 live settlement。** Live 證據規則：exchange 證據（current 或 cleared rows）只可以證明 bet 存在、提高已知 matched size，永遠唔降低計住嘅 worst case，亦唔會令單變終結；`refresh_open_orders`、`close_from_cleared`、`settle_from_cleared` 同 `pnl_basis_mismatch` 刪咗。新命令 `settle-market`（只限 live、冇 `--mode`、句子喺攞 lock 之前打，banner 列晒我哋喺嗰個 market 嘅單同 worst case）：market 喺 listMarketBook 係 CLOSED、我哋冇 bet 仲係 current、冇 unresolved 單，先用一條 compare-and-append 嘅 `settle` event 將 EXECUTABLE／EXECUTION_COMPLETE 搬去 SETTLED（`lifecycle` 加 EXECUTABLE → SETTLED），每張單按 worst case 當當日已實現虧損（`settled_worst_loss_pence`）。Live P&L 只係資訊。P3 嘅前提係推斷（新 F77），T10 加長觀察。同日 startup reconcile 對 SETTLED 單用 matched + remaining 做上限，所以 settle 之後唔會誤報矛盾。
- **D4：kill 唔等 lock。** 先 engage KILL file（fsync），每個 gateway 喺 placeOrders 第一個 byte 之前再查；sweep 只取消我哋嘅 bet（strategy ref ∪ journal betId；`sourceIdKey` 腿刪咗；journal 唔可信就淨係 config ref，而且一定 exit 3）；kill 唔郁 journal 入面任何單；固定喺 KILL file 之後 20 秒同 35 秒再掃（F28 + F42，新常數 `IN_PLAY_DELAY_MAX_S = 12`，import 時檢查個上限）；exit 0 只代表最後一次核實冇我哋嘅 bet，否則 exit 3 加「即刻去網站或 Bet Angel 取消」。`kill_lock_wait_s`、holder 讓路機制、`_cancel_after_kill`（spec step 16）、`kill_file_only`、account-wide 嘅 journal 掃描同 kill 嘅 registry 寫入全部刪咗。Kill 同另一個 process 同時 append journal 係安全嘅：驗過 `BEGIN IMMEDIATE` 同喺 transaction 入面讀 head，有兩條 connection、兩個 thread 嘅測試。
- **Astra C1–C4（全部改咗）。** C1：live 風險上限冇 hedge credit（預設冇；`hedge_credit=True` 只喺 paper R8–R10、R12 嘅 before 同 paper status，boundary test 會對）。C2：placeOrders 帶 `send_by = min(book freshness, capability expiry)` 同 first-byte gate 入 transport，連好線之後、第一個 byte 之前查；被拒係 FAILED `transport_not_sent`、零 byte；funds 改喺 book 之前讀；cancel 嘅 capability 喺 submit append 之後先 mint，唔受 send-by 限制。C3：503 令 cache 入面每個 market 都變唔可用，恢復前後唔 fill。C4：paper-replay 記低 receipts、sidecar 同解析出嚟嘅假設，ledger 嘅 `source_sha256` 包埋。
- **其他 live 修正。** Exit code 5（X5：送咗單但冇照要求完成，唔好重發）；送單之後 signal 延後到結果印完（R3#16），SIGHUP 同 SIGTERM 一樣處理；HALT 同觸發佢嘅 event 一齊 journal（payload `halts`，R3#13；連續 UNKNOWN 由第 3 張起每張都 HALT）；`_realized_loss` fail closed（A26）；RECORDED 孤兒單喺 open 時變 REFUSED（`crash_before_submit`，R3#14）；A13 先讀 Keychain 後讀 journal head（A18）；`--confirm-live` 冇 `--mode live` 會被拒，help 文字改正（R3#18）；kill 嘅進度、F67 警告（只限 coexistence 未驗證）同 signal 一律 exit 3（R3#17）。
- **Paper 修正（D5）。** `paper-step` 拒絕另一個 market 嘅 book（R3#6）；gap window 按 market、要喺同一個 window（R3#7、R3#8），已知同未知混埋會拒絕；同價生效次序跨 process 保留（R3#9）；paper state 放 journal 嘅 `paper_state` payload，同 fill 喺同一條 event，`paper-state.json` 只係舊 profile 讀一次、唔再寫（R3#12）。
- **事實。** 新 F77（推斷，冇新來源）；F28、F31、F32、F33、F36、F37、F42、F67、F71 嘅 code handling 跟收窄改；F61 改 `partially_unverified`（衝突嘅另一邊冇 URL 紀錄，R3#11）；F74 標明 SQLite 部分係本地檢查。一共 77 條。
- **A20（當時留畀 Yu，2026-10-11 已決定）。** Betfair 唔再返嘅 CLOSED market 喺 v1 settle 唔到，佢嘅 worst case 會一直計住、食咗 R9／R10 嘅額度；v1 冇 override。Yu 喺 2026-10-11 決定保留呢個保守做法，操作規則係 CLOSED 之後盡快 settle（見下面 "Re-review fixes (2026-10-11)"）。其他 ambiguity（A1–A27）全部揀咗對 live 錢較安全、live 代碼較少嘅一邊，列喺 amendment §8。
- **被取代嘅舊修正。** 上面標咗「已被 v1 live 收窄取代」嘅 bullet 只係歷史：第一輪 "§9.7 reconcile" 嘅 child 搜尋同 "§9.8 kill"，第二輪 "§9.8 step 3 kill 等 lock" 同 "§9.7 / settlement"（`found_move` 嘅 response 金額容差、`sizeVoided` 等式同 `matched_size_known_never_absent` 保留），由 D2–D4 取代；第一輪 "§13 paper-v1" 嘅 `paper-state.json` 同第二輪 "§8 / §13" 嘅 503 規則，由 R3#12 同 C3 取代。
- **S0。** AGENTS.md bullet 改寫成收窄咗嘅 live 範圍（新 `s0_expected_sha256_after`）；歷史 receipt 唔再更新（下面 "S0 patch and merge gate"）；S0 commit 要改寫嘅文件句子（R3#10）已經列入 `merge_gate.s0_commit`，`s0_applied` 一變 true，預設 suite 就會拒絕留低嘅「未套用」字眼。

## Review of the narrowed tree（2026-10-11）

收窄咗嘅 tree 再做咗一輪離線 review（live money、operator 用法、kill 嘅信心、測試覆蓋四個角度），有 20 個 finding。19 個改咗，每個都有 regression test，而且每個新測試喺還原咗修正嘅副本上面都 fail；一個（下面 F9 嘅 (c)）拒絕。以下同 amendment A3 唔同或者 amendment 冇講嘅地方，全部揀對 live 錢較安全、live 代碼較少嘅一邊：

- **Kill 唔等 journal（D4 補完）。** 開一個已經有 schema 嘅 journal 只係讀（WAL reader 唔等 writer），唔再 `BEGIN IMMEDIATE`；kill 嘅 connection 只等 `killswitch.JOURNAL_BUSY_TIMEOUT_S`（2 秒，`OrderJournal(busy_timeout_s=...)`）。CLI 嘅 `kill`／`halt` append 遇到 `sqlite3.Error` 同 `BetfairError` 一樣記低就繼續。Gateway 喺 live kill 入面嘅 journal 寫入係盡力而為：第一條寫唔入（鎖住、被拒）之後唔再寫任何 kill event，但照送晒每個 cancel，kill 一定 `kill_incomplete`（`journal_write_failed`）。呢個係 amendment §4 第 8–9 步「kill journal `cancel_intent`、`submit`、`report`」同 spec「submit 喺任何 byte 之前 journal」嘅唯一例外：kill 嘅 cancel 唔郁任何單（冇 CANCEL_PENDING 要 crash 之後解決），event 只係紀錄；等佢會令我哋嘅 bet 繼續 live。以前一個卡住嘅 writer 會令 kill 等 30 秒、`kill_incomplete_journal_unreadable`、一個 cancel 都冇送。
- **Sweep 唔會俾一個 market 停晒。** `_cancel_targets` 每個 chunk 接 `Exception`（唔係 `BaseException`，signal 照停），記低 `raised` 再做下一個 market、之後嘅再掃同最後核實；有 cancel 拋過就 `kill_incomplete`（`kill_cancel_raised`）。
- **Kill 查 account（只讀）。** `_open_kill` login 之後比較 session 嘅 account fingerprint 同 journal 記低嗰個；唔同就 `account_differs_from_journal`，當 journal 唔可信（config ref sweep、唔寫 anchor、exit 3，stderr 講原因）。冇寫 registry，亦冇寫 journal fingerprint（K12 不變）。
- **Kill 嘅其他 exit 3 路徑。** `--scope account --confirm-account-wide` 冇終端機（淨係 A11 失敗）照樣當 `kill` sweep 我哋嘅 bet，account-wide 部分 `not_sent`／`operator_tty_required`；CLI 自己 engage 失敗就唔傳 T0，gateway 由自己 engage 計（最後一次掃喺 KILL file 出現之後 ≥ 35 秒）；每個 exit 3（login 失敗、未 arm、config 錯、KILL file 唔見咗）都試寫 HALT `kill_incomplete`（`journal_halt_kill_incomplete`），gateway 寫咗就唔再寫；config 錯嘅原因印埋出嚟；JSON `session` 變成 `{"logged_in", "login", "coexistence"}`，`login` 由 gateway 真正用嘅 login 嚟（`OrderGateway.login_kind`）。
- **Exit 5 嘅意思改寫（X5）。** 「命令開始咗一個落單或者取消 request，但冇照要求完成；每張單一行講實際情況同下一步」，取代「已經送咗、唔好重發」：FAILED 嘅落單可以一個 byte 都冇送，cancel 可以再送。FAILED 嗰行加 journal 入面嘅原因（例如 `(kill_engaged_before_send)`），原因係 kill 就唔講「a fresh place is safe」；place result 加 `send_exception`（只有 code）。Cancel 嘅行永遠唔講 "do not resend"：bet 仲 live 就 `Betfair cancel not done: ... run cancel again now`，結果未知就叫你 reconcile 再 cancel；取消之前已經 match 咗、冇剩餘就 exit 0 加一行資訊。
- **Withheld 單講清楚（F9 (a)(b)）。** Exposure 唔變（D3／D4）。Kill exit 0 嘅 JSON 有 `cancel_requested_bet_ids`，stderr 講佢哋喺 status 仍然係 EXECUTABLE、按 worst case 計，直到 `settle-market`，唔使再 cancel。`exchange_reduction_withheld_until_settle` 有自己一行（指向 `settle-market`，唔係 status／reconcile）；落單結果係呢個 code 都 exit 5；live `reconcile` 每項加 `error_code`。**拒絕 (c)**（live cancel 遇到 withheld 單就喺送出之前拒絕）：withheld 單喺 exchange 上面仲可以有真正 live 嘅剩餘（例如 void 咗已 match 嘅部分、或者喺網站減咗 size），拒絕 cancel 會留低一張 live bet；cancel 永遠唔可以更嚴。重複 cancel 觸發 `consecutive_unknown_outcomes` HALT 係保守嘅，冇改。
- **其他。** `found_move` 用 exchange 狀態解決一張單時清走舊 error code（之前 part-match 之後嘅整張 cancel 會 exit 5）；`KILL_COMPLETE_LINE` 改講「冇 unmatched（EXECUTABLE）部分」，因為 `orders-current` 會繼續列 match 咗同取消咗嘅 bet；`settle_market_unresolved_orders` 嘅建議加 `--accept`。F74 嘅結構欄位帶住本地檢查：`retrieval` 係 list `["man_page_read", "local_python_check"]`、`secondary_evidence_type` `inferred`（R3#11(3) 完成；fact register 嘅 `scope` 同 facts doc 講明 list 嘅意思，`test_betfair_facts_doc` 要求引用 `local:` 來源嘅事實列 `local_python_check`）。測試：kill 嘅 emergency login 用 login ledger 驗（成功 login 上限用晒都照 kill）；兩條 connection 交錯 append 嘅測試改成強制插入（head 讀咗之後、insert 之前）；`settle-market` 喺 `RISK_COMMANDS`、用普通 login；`test_settle_market_has_no_mode_flag` 只用 parser。

## Round 4 review fixes（2026-10-11）

收窄咗嘅 tree 再做咗第四輪離線 review（live money、unknown outcomes、kill、operator 用法、paper 同 repo-fit 幾個角度），有 21 個 confirmed findings（3 個 major，18 個 minor；冇 split）。全部改咗，每個都有 regression test，而且每個新測試喺還原咗修正嘅副本上面都 fail（code 用 round 4 之前嘅版本、測試用而家嘅；報告同 merge gate 嘅測試換返舊 helper）。同 amendment A3 或者上面嘅修正唔同、或者佢哋冇講嘅地方，全部揀對 live 錢較安全、live 代碼較少嘅一邊：

- **Kill 遇到壞咗嘅 journal 照 sweep（major ×2）。** 以前 kill 只當 `JournalError` 係「journal 唔可信」：一行唔係 JSON（`ValueError`）、唔係 object（`AttributeError`）或者 SQLite decode 唔到（`sqlite3.Error`）會令 kill 喺 sweep 之前停；journal 根本開唔到就 `kill_incomplete_journal_unreadable`、一個 cancel 都冇送。而家 kill 路徑讀 journal 拋任何嘢都當唔可信（`_kill_journal_check`、`_fold_or_none`、`account_fingerprint`、`_journal_kill`、CLI 嘅 kill／halt append），kill 自己嘅寫入係盡力而為；開唔到嘅 journal 由 `journal.UnreadableJournal` 頂替（每次讀寫都 `journal_unreadable`，唔改、唔換、唔修個檔），照 config strategy ref sweep，最後 exit 3 `kill_incomplete_journal_unreadable`。`journal._committed_folder` 亦將 `AttributeError` 當 fold 唔到，所以 kill 嘅 safety event 照寫得入。**同 docs 舊講法唔同：** 以前文件話 journal 開唔到就唔 sweep；amendment §4 第 6 步「唔可信嘅 journal 唔會停 sweep」而家包埋呢個情況。
- **Kill 打錯 `--profile`。** 唔合法嘅 profile 名以前 exit 2、KILL file 冇 engage；而家先 engage（KILL file 喺 state root，同 profile 無關），之後 exit 3 `kill_incomplete_invalid_profile`，冇 sweep（journal、config、credentials 屬 profile）。
- **Login ledger 嘅指示（偏離 amendment §4 第 7 步原文）。** Amendment 要求 ban 同連續失敗上限都講「check why logins failed, then login-reset」，但 `login-reset` 唔會清 ban（`append_login_reset`），壞行（`login_ledger_unreadable`）以前乜都冇講。而家按原因分開講（`cli.LEDGER_ADVICE`），`login-reset` 嘅 banner 講明 ban 唔會清、修壞行會開一個 20 分鐘、連 kill 都擋嘅 ban。Amendment 原文冇改（一字不改）；以呢度為準。
- **Lazy login 沿用 emergency flag。** `SessionManager.token()` 以前永遠用 non-emergency login，kill 嘅 emergency relogin 失敗之後下一次讀取會被成功 login 上限擋（`LoginBudgetExceeded`）；而家同 `invalidate_and_relogin` 一樣沿用上一次成功 login 嘅 flag（ban 同失敗上限照守）。
- **`KILL_COMPLETE_LINE` 唔再講未驗證嘅嘢。** 「matched and cancelled bets stay listed in orders-current until Betfair settles the market」冇事實支持（F77 只講 settle 之後唔再列），刪咗；order-path doc 同一句改做推斷、指去 F77／T10。上面 "Review of the narrowed tree" 嘅「因為 `orders-current` 會繼續列 match 咗同取消咗嘅 bet」係當時嘅理由，已經唔成立。
- **Open 時嘅孤兒單同中斷 request（compare-and-append）。** `_stale_to_unknown` 以前由一次 fold 計 RECORDED → REFUSED，再冇 head 檢查咁寫：一個 `.gateway.lock` 被換咗嘅 gateway 喺中間 journal submit，就會令送緊嘅單變 REFUSED、唔再計任何 worst case，佢嘅 report move 靜靜被略過。而家兩個 open 時嘅 move 都係對住 fold 之前讀嘅 head 嘅 compare-and-append；head 郁咗就乜都唔寫、下次 open 再做。另外 live 嘅 `report` 有 move 要略過（`skipped_moves`）就喺同一條 event HALT `report_moves_skipped`（global）。冇揀「live 唔做孤兒 pass」：R3#14 嘅 test 同 paper 都要佢，條件寫入已經夠。另一個 gateway 喺 submit 之前已經 REFUSED 咗呢張單嘅話，submit 被拒（`journal_changed_since_decision`），而拒絕用嘅 `risk_decision` 唔會再因為 REFUSED → REFUSED 拋 exception（`_refuse_refs` 留低已經 REFUSED 嘅 ref）：結果係 refused、乜都冇送。
- **Betfair 只列做 cleared 嘅單（`cleared_row_bet_exists_worst_case`）。** 落單以前 exit 0（好似仲掛住），cancel 叫你「run cancel again now」，跟住做會一路 UNKNOWN 到 `consecutive_unknown_outcomes` HALT。而家兩邊都印 cleared-row 嗰行（唔係掛住、照 worst case 計直到 `settle-market`、淨係 `orders-current` 仲見到 unmatched 先再 cancel），落單 exit 5。**拒絕**「live cancel 遇到呢個 code 就喺送出之前拒絕」嗰個寫法，理由同 F9 (c) 一樣：cancel 永遠唔可以更嚴。
- **Cancel 停咗唔講 "do not resend"。** Signal 或者意外 exception 喺 cancel request 之後，以前印落單嗰句「do not resend」；而家 cancel 講「bet 可能仲 live，再 cancel」，live-probe 講「唔好再跑 probe，probe bet 可能仲 live」。`on_send` 改喺 `_send` 封鎖 signal、mint cancel capability 之後先 call，所以 mint 被拒（乜都冇送）嘅 cancel 唔會報「sent」。
- **印出嚟嘅命令照抄跑得。** `coexistence.bet_angel` 係 `unverified` 嘅時候，每一行叫你跑嘅 live 命令都帶 `--bet-angel-paused`（`ctx.paused_flag`）；coexistence 拒絕嗰行講明要加 flag、kill 唔使；冇 `--mode live` 嘅 cancel 遇到 `validation:V2` 會講佢係 paper。`live_replace_removed_v1` 同 `live_partial_cancel_removed_v1` 兩句喺讀 config 之前印，D2 要求喺任何讀取之前拒絕，所以冇改，照抄被拒時由 coexistence 嗰行補（部分修正，記低）。
- **之前嘅 UNKNOWN 喺 open 時先發現 live（major，severity 有爭議，照 major 處理）。** `place`／`live-probe` 嘅 `open()` 對到之前 UNKNOWN 嘅單係 live bet，呢次落單就喺 risk decision 之前拒絕（`earlier_unknown_resolved_at_open`，exit 2，乜都唔 journal、唔送），每張之前嘅單一行；再跑一次就照送。揀咗拒絕而唔係重新叫人打句子：更少 live 代碼、更嚴。Live `reconcile` 對完仲有 unresolved 單：JSON 加 `unresolved`，每張一行，exit 5。**偏離：** amendment §3.3 嘅 `"status": "reconciled"` 冇改；X5 嘅 exit 5 擴闊到「live reconcile 留低 unresolved 單」，因為 exit 0 會被當成成功，exit 2 嘅意思係「被拒、乜都冇送」。
- **Banner 講真話。** `reconcile --accept` 同 live `resume` 以前用 `LIVE_BANNER`（「this sends a real order」）；而家講明唔送單，accept 嘅 banner 列出張單、狀態同會唔再計嘅 worst case（journal 冇呢張單就喺打句子之前拒絕）；account-wide 嗰句講明係取消。
- **Paper（D5）。** 中斷咗嘅 paper request（submit 同 report 之間）以前變 UNKNOWN、有 match 嘅單 `--accept` 拒絕，個 market 永遠落唔到單；而家 `open()` 準確解決（`paper_interrupted_before_report`），因為 paper state 只隨 report 寫入（R3#12）。Closed market 喺每張單都終結之後離開 `paper_state`（`PaperExchange.close_market`），journal 唔再按 market 數平方增長。（連 CLOSED book 一齊刪係 regression，re-review R2 留返一個 closed 標記，見下一節。）
- **Repo-fit。** Merge gate 要求 README 係 `readme_commit` 嘅樣（以前 README 未改都過）；`status_problems` 對 report md 嘅 Verification 數字同 JSON（以前重跑第 4 步但漏咗第 5 步都綠）；`LIVE_TEST_IDS` 改做 T0–T10，所有寫到 T11 為止嘅 first-live-test 範圍改做 T0–T10（T11 跟 live replace 一齊剔走）。

## Re-review fixes (2026-10-11)

Astra（Codex）覆核咗第四輪之後嘅候選 tree（checkpoint `43bb3da9…`），report `report_ef3fb90d-3ad9-4150-9b50-4796f91a4c32`：verdict **request_changes**，有兩個新嘅 P2 regression（R1、R2），冇新嘅 P1。原本嘅 C1–C4 喺支援嘅 production／replay 路徑上接受為 closed。R1、R2 都改咗，每個都有 regression test。Astra 嘅 probe 喺改之前嘅 tree（`v1n2/base`）重現到，喺改咗嘅 tree 唔再出現。下面係實作者嘅摘要，Astra 未覆核呢啲修正：

- **R1（P2）：兩個寫 anchor 嘅 process 可以令 Keychain anchor 退後。** 以前每個寫 anchor 嘅地方都係冇 lock 咁「讀 journal head，再 `anchor_write`」，包括 gateway 嘅 `open()`／`close()`、kill 喺 `open()` 入面同 `resume`。Kill 唔攞 `.gateway.lock`，所以可以同一個 holder 一齊寫：A 讀咗 head 1 之後停住；B append event 2，再寫 anchor 2；A 返嚟寫返 anchor 1。Astra 嘅 probe 結果係 journal head 2、Keychain 寫入次序 `[2, 1]`、最後 seq 1，一個冇 event 2（嗰個 HALT）嘅舊 snapshot 過到 anchor 檢查。佢冇顯示 KILL file 被刪，亦冇顯示有單自動送出。

  **改法：** 新嘅 `credentials.publish_anchor` 係成個 package 唯一 call `anchor_write` 嘅地方（`test_betfair_boundaries` 檢查）。佢攞 per-profile 嘅 flock `paths.anchor_lock`（`profiles/<p>/orders/.journal-anchor.lock`），**喺 lock 入面先讀 journal head**，寫完先放 lock。Journal 只會變長，所以 anchor 唔會退後（呢個講法唔完整：journal 喺 session 期間被還原就唔成立，見下一點）。Append 唔攞呢個 lock：一個卡住喺 Keychain dialog 嘅寫唔會擋住 journal，kill 嘅 append 都唔會。Kill 唔再喺 `open()` 寫 anchor，只喺 `close()`、成個 sweep 之後寫一次。佢最多等另一個寫緊 anchor 嘅 process `killswitch.JOURNAL_BUSY_TIMEOUT_S`（2 秒）；寫唔到只會喺 stderr 出 warning，唔會停或者延遲任何 cancel。咁樣保留咗 kill 喺 journal／anchor 用唔到嘅時候照取消嘅能力，係 Astra 要求嘅。一般命令同 `resume` 最多等 `credentials.ANCHOR_PUBLISH_WAIT_S`（30 秒；之後收窄咗，見下一點）。新 code：`journal_anchor_publish_busy`、`journal_anchor_lock_unavailable`。CLI 嘅 warning 會印真正嘅 code。

  **代價：** 如果 process 喺 kill sweep 期間死咗，CLI 寫嘅 `kill`、`halt` 會冇 anchor 保護，直到下一個 live 命令寫 anchor；KILL file 照樣擋住所有新單。未驗證：`anchor_write` timeout 之後，Keychain 會唔會遲啲先完成嗰次寫（T0）。

  **Probe：** Astra 原本嘅 probe patch `gateway.anchor_write`，改咗之後 gateway 冇咗呢個名，所以佢喺新 tree 會 `AttributeError`。改編版 `v1n2/repro/anchor_regression_probe_threaded.py`（scratch，唔喺 repo）將 B 放喺第二條 thread，用自己嘅 journal connection。喺舊 tree 結果係 `[2, 1]`、最後 seq 1、舊 snapshot 冇 failure；喺新 tree 係 `[1, 2]`、最後 seq 2、舊 snapshot `journal_anchor_mismatch`，而且 B 等咗 A。

  **測試：** `test_betfair_credentials`、`test_betfair_gateway`（兩個 publisher 同時寫）、`test_betfair_killswitch`（anchor 寫唔到都照 sweep；kill 只喺 sweep 之後寫 anchor）、`test_betfair_boundaries`。文件喺 `docs/betfair-order-path.md` §7、§9 同 resume，amendment §4 第 6 步同 A18 有日期註記。
- **R1 覆核（2026-10-12）：4 個 confirmed findings（1 major、3 minor），全部改咗。** 呢輪係對上面 R1 修正嘅離線覆核（r1-concurrency、r1-liveness 兩個角度），唔係 Astra 嘅；第 2、4 個 finding 係同一個 defect。每個修正都有 regression test：喺 `v1n2/base` 同改之前嘅 tree 都 fail，喺改咗嘅 tree pass。Astra 未覆核呢啲修正。
  - **(major) Journal 喺 session 期間被還原，anchor 照寫。** 「Journal 只會變長」只係對同一個檔嘅內容成立。一個 live session 開住 journal 嘅時候，有人用 `sqlite3 journal-live.sqlite ".restore backup"`（SQLite backup API）還原舊 backup，所有開住嘅 connection 之後都讀到舊內容。之後呢個 session 嘅寫（holder 嘅 `open()` 喺 A13 之後、holder 嘅 `close()`、kill 嘅 `close()`）就將舊 head 寫入 Keychain：anchor 退後，冇咗 HALT 嘅舊 journal 之後過到 A13。Base 都係咁，唔係 R1 引入嘅。**改法：** `publish_anchor` 喺 lock 入面再讀 Keychain anchor，head 要按 A13 規則接得上（`arming.anchor_failures`；喺 function 入面 import，避開 arming → credentials 嘅 import cycle）先寫。接唔上就 `journal_anchor_not_extended`，乜都唔寫，anchor 留喺前面，下一次 open 就 `journal_anchor_mismatch`。Keychain anchor 讀唔到都唔寫（`journal_anchor_unreadable`／`journal_anchor_invalid`）；冇 anchor 就照寫第一個。Kill 嘅代價係 `close()`（sweep 之後）多一次 Keychain 讀取，冇 cancel 會因此遲。CLI 遇到 `journal_anchor_not_extended` 唔再講「anchor lags the journal」，改講 anchor 留咗喺前面、下一個 live 命令會拒絕（`cli._anchor_warning`）。`open()` 嘅 note 同 RISK 命令嘅拒絕照舊係 `journal_anchor_write_failed`。用 rename 換走成個檔（`mv`、Finder）嘅還原，開住嘅 session 睇唔到，還原咗嘅檔本身已經過唔到 A13。
  - **(minor ×2) 寫嘅期間 lock 檔被換咗。** `held()` 淨係喺 `anchor_write` 之前查。`security` 行緊（最多 120 秒）嘅時候 lock 檔被刪或者被換，另一個寫嘅人鎖住新檔、寫較新嘅 head，之後我哋較舊嘅寫先落地，仲報成功：anchor 退後。**改法：** 寫完再查 `held()`；lock 冇咗就喺重新攞到嘅 lock 入面再讀再寫，最多 `credentials.ANCHOR_PUBLISH_ATTEMPTS`（3）次；次次都被換就 `journal_anchor_lock_unavailable`。剩低嘅限制：中間會短暫落後（A13 接受）；次次都被換嘅話最後可能落後 head 一次寫，但係報錯，唔係報成功。冇 Keychain compare-and-swap，做唔到更好。
  - **(minor) 等 anchor lock 拖慢手動 cancel。** R1 之後所有非 kill 嘅 live 命令（包括 `cancel`、`reconcile`）喺 `open()`（cancel 送出之前）同 `close()` 各等最多 30 秒，期間攞住 `.gateway.lock`。Kill 嘅 `close()` 同 `resume` 攞 anchor lock 但冇攞 `.gateway.lock`；佢哋卡住（Keychain dialog 冇人答、Ctrl-Z）嘅時候，cancel 遲 30 秒先送，`.gateway.lock` 攞住大約 60 秒，第二個 cancel（等 `order_deadline_s + 5` = 15 秒）就 `lock_busy`，張單仲 EXECUTABLE。Base 冇呢個等待。**改法：** `OrderGateway._anchor_wait_s`：淨係 `RISK_COMMANDS` 嘅 `open()`（寫唔到就拒絕）等 `ANCHOR_PUBLISH_WAIT_S`（30 秒）；其他（`cancel`／`reconcile` 嘅 `open()`、所有 `close()`、kill）等 `killswitch.JOURNAL_BUSY_TIMEOUT_S`（2 秒）。`resume` 冇攞 `.gateway.lock`，照等 30 秒。**同 finding 建議嘅分別：** 建議只講非 RISK 命令；RISK 命令嘅 `close()` 我都改咗做 2 秒，因為 close 寫唔到只係 warning（即係建議第二句：30 秒只留喺寫唔到會拒絕命令嘅地方），而 place 之後攞住 `.gateway.lock` 等 30 秒，一樣會擋住之後嘅 cancel。
  - **測試：** `test_betfair_credentials`（`test_publish_anchor_writes_only_a_head_that_extends_the_keychain_anchor`、`test_publish_anchor_publishes_again_when_its_lock_is_replaced_during_the_write`）、`test_betfair_gateway`（`test_a_journal_rolled_back_under_the_session_is_never_anchored`，holder 嘅 `close()` 同 `open()` 兩種；`test_non_risk_commands_wait_briefly_for_another_anchor_publisher`）、`test_betfair_killswitch`（`test_kill_never_anchors_a_journal_rolled_back_during_its_sweep`）。改之前嘅 tree：5 個都係 assertion fail。`v1n2/base`：兩個 rollback 測試（gateway、kill）係 assertion fail，另外 3 個係 error，因為 base 冇 `publish_anchor` 同 anchor lock。Gateway rollback 測試淨係 `open()` 嗰一半，喺 base 同改之前嘅 tree 都會自己 fail。Full suite 884 個 test，OK（skipped 1）。
  - **Probe（scratch，唔喺 repo）：** 改咗嘅 tree 上面，finding 嘅 p1 三個 variant 都係 `journal_anchor_not_extended`，Keychain 冇郁，還原咗嘅 journal 係 `journal_anchor_mismatch`。p2 嘅寫入次序係 `[3, 2, 3]`，最後係 3。p3 壓力測試 0 個 regression。Cancel 排喺卡住嘅 resume 後面：第一個 cancel 喺 7.2 clock-s 送出（之前 45.5），第二個 cancel exit 0（之前 `lock_busy`）。卡住嘅 publisher 對 kill 冇影響（`kill_complete`）。Liveness 嘅 probe_b 每次 A 寫都換 lock：而家 A 報 `journal_anchor_lock_unavailable`（之前報成功）；原版 probe 冇接呢個 exception，所以用咗改編版。Astra 嘅 R2、C1 probe 輸出同之前一樣；佢原本嘅 R1 probe 照舊因為 `gateway.anchor_write` 冇咗而 `AttributeError`，threaded 版本照舊係 `[1, 2]`。
  - **文件：** `docs/betfair-order-path.md` §4 第 4 同 17 步、§7「Anchor 只會向前」、resume；amendment A18 加咗 2026-10-12 註記（開頭「Dated edits」都講明）。
  - **第 4–7 步（2026-10-12）：** 呢輪修正之後重跑咗；之後「R2 跟進覆核」改完再重跑，report 同 `handoff.json` 而家嘅測試數字、`code_sha256` 同 `artifact_sha256` 係嗰次嘅（見 "Status"）。
- **R2（P2）：paper 刪 closed market 嘅時候，連 chronology 屏障都刪埋。** Round 4 嘅 `close_market` 喺一個 market 嘅單全部終結之後，連個 CLOSED book 都由 paper state 刪走；`_paper_book_older` 之後搵唔到之前嘅 book，一個 close 之前嘅 OPEN book 就會再被接受。Astra 嘅 repro：用舊 book 再落單，match 咗 2.0；再 step 同一個 CLOSED book，就 settle 咗兩張 200p、每張賺 500p 嘅注。即係知道結果之後，再食咗同一個顯示深度。

  **改法：** `paper_state` 升到 `schema_version` 2，加 `closed`：`{"markets": {market_id: closed_ms}, "floor_ms"}`。Step 到 CLOSED 嗰一刻就寫標記，所以嗰一步嘅每條 event 都帶住佢；刪 book 嘅時候標記會留低。見過 CLOSED 嘅 market：`place`／`replace` 一律拒絕；`paper-step` 除咗時間唔早過 close 嘅 CLOSED book，一律忽略（`paper_market_closed`）；兩樣都乜都唔寫入 journal。標記最多留 `PAPER_CLOSED_MARKERS_MAX`（256）個，每條 paper event 最多多大約 7.2 KB。超出就由最早 close 嘅標記開始踢走，`floor_ms` 升到佢嘅 close 時間；之後時間喺 floor 或之前嘅 book，如果 paper state 冇嗰個 market 嘅有時間 book，就會被拒（`paper_book_at_or_before_closed_floor`；只會拒多，唔會放多）。`schema_version` 1 嘅舊 paper state 照讀：`open()` 由 round 4 close 嗰一步嘅 `note` 補返標記，時間用 note 嘅 `recorded_at`，所以只會拒多。R2 之前嘅 code 讀到 version 2 會 `paper_state_unreadable`（fail closed）。Paper 當 CLOSED 係終結，同 `paper-replay` 一樣；Betfair 會唔會重開一個 CLOSED market，冇查過。

  **Probe 結果：** Astra 嘅 repro 喺新 tree 印 `old_book_refused OrderRefused paper_market_closed`。改編嘅 reopen 版本（關咗 gateway，開一個新 gateway 再試）喺舊 tree 照接受、settle 兩張；喺新 tree 拒絕，淨係 settle 一張。

  **測試：** `test_betfair_gateway`（同一個 process、reopen 或者死咗之後、floor 上限、保留咗嘅 CLOSED book、R2 之前嘅 paper state）同 `test_betfair_journal`（schema 2 嘅驗證）。文件喺 `docs/betfair-order-path.md` §7 同 §11，amendment §1 嘅 `paper-step` 行同 R3#12 有日期註記。
- **R2 跟進（2026-10-12）：`place`／`replace` 見到 CLOSED book 都要 close 個 market。** 呢個係上面 R2 修正之後嘅 attack lens（verify-4-0）搵到嘅，base 都係咁，唔係 Astra 嘅 finding，亦唔係 R2 引入嘅。`place`／`replace` 收到 CLOSED book（`--book`、`--fetch-book`，或者 gateway 自己 fetch）只係喺 V9 拒絕（`market_not_open`），冇寫標記、冇留 book。之後喺 `max_snapshot_age_s`（10 秒）之內交一個 close 之前嘅 OPEN book 就照接受，再 step CLOSED 就 settle：BACK 2.00 @ 3.50 match 2.0、settle 賺 500p；`replace` 嘅 child 一樣。即係同 R2 一樣，睇到結果先落注。`paper-step` 因為舊過上一個 book 而忽略嘅 CLOSED book 亦冇寫標記。

  **改法：** 新嘅 `OrderGateway._note_paper_closed`。`place`、`replace`（`_refuse_paper_book`）同 `paper-step` 嘅忽略路徑收到一個 schema 有效、屬於命令自己 market、有時間嘅 CLOSED book，而 paper state 未當嗰個 market closed，就即刻用 book 嘅時間寫標記。嗰個 market 喺 paper state 有 book（仲有掛單）而 CLOSED book 唔舊過佢，就用 CLOSED book 取代，同 step 到 CLOSED 之後一樣，所以標記之後被踢走都仲係 closed。新嘅 paper state 喺嗰個 request 寫任何其他 event 之前，用一條 `note` 寫入 journal（`reason` 係 `paper_market_closed`，`market_id` column 係嗰個 market）。冇新 event kind，亦冇新 schema：同 `paper-step` 嘅 `paper_advance_state` note 一樣，喺 paper lock 入面 append。之後先做 risk decision，所以 submit 嘅 compare-and-append basis 包埋呢條 note。Operator 見到嘅 refusal code 同 exit code 冇變：嗰次照樣 `validation:V9`（exit 2），舊 book 同 floor 嘅拒絕亦一樣；之後同一個 market 就係 R2 嘅 `paper_market_closed`。**刻意嘅例外：** 呢啲拒絕同忽略而家會寫一條 `note`；已經 closed 嘅 market 嘅 `paper_market_closed` 照舊乜都唔寫，R2 嘅測試照舊檢查呢點。唔算嘅 book：另一個 market 嘅（V1；`paper-step` 照舊 R3#6 乜都唔改）同 schema 無效嘅。擋唔到嘅：gateway 從來冇收過嘅 CLOSED book（verify-4-0 D）；同埋一個舊過 market 留低嘅 OPEN book 嘅 CLOSED book，標記被踢走之後，時間遲過嗰個 OPEN book 嘅 book（時間都遲過 CLOSED book，唔係 close 之前嘅 book）。第二點喺下面「R2 跟進覆核」改咗：嗰類標記而家永遠唔踢。Live 冇呢個問題：live 唔收 caller 嘅 book，每次都自己 fetch。

  **測試：** `test_betfair_gateway`：`test_a_closed_book_a_place_or_replace_refuses_closes_its_market`（同一個 process：`place`、`replace`、自己 fetch）、`test_a_closed_book_a_place_refuses_keeps_its_market_closed_after_a_reopen_or_a_death`（reopen，同埋死喺 note 之後）、`test_a_closed_book_paper_step_ignores_or_an_evicted_marker_still_closes_its_market`（`paper-step` 忽略嘅 CLOSED book，同埋標記被踢走）。`test_betfair_cli`：`test_paper_place_on_a_closed_book_closes_the_market_for_later_processes`（每個命令一個 gateway，exit code 同訊息）。4 個喺改之前嘅 tree 都 fail；scratch 版（冇 note 嘅 assertion）證明 5 個行為都係因為 close 之前嘅 book 被接受而 fail。兩個 mutation 各自令相關測試 fail：唔取代留低嘅 book，同 `paper-step` 唔寫標記。Full suite 888 個 test，OK（skipped 1）。文件喺 `docs/betfair-order-path.md` §11。

  **第 4–7 步：** 呢個修正之後未即刻重跑；下面「R2 跟進覆核」嘅修正之後先重跑（見 "Status"）。Astra 未覆核。
- **R2 跟進覆核（2026-10-12）：4 個 confirmed findings（全部 minor），全部處理咗。** 呢輪係對上面 R2 跟進（`_note_paper_closed`）嘅離線覆核（bypass、regressions 兩個角度），唔係 Astra 嘅；第 2、3 個 finding 係同一個 defect。每個 code 修正都有 regression test：喺改之前嘅 tree fail，喺改咗嘅 tree pass。Astra 未覆核呢啲修正。
  - **(minor) 舊過留低 OPEN book 嘅 CLOSED book：標記一被踢走，market 就重開。** `paper-step` 忽略（`paper_book_older_than_previous`）或者 `place`／`replace` 拒絕（`paper_book_older_than_last_seen`）一個舊過 market 留低嘅 OPEN book 嘅 CLOSED book，`_note_paper_closed` 只寫到標記，因為 `_keep_paper_book` 唔收舊 book。標記一被踢走（最早 close 嗰個先踢），floor 唔理有有時間 book 嘅 market，佢 OPEN book 嘅 chronology 就接受之後任何 book：close 之前掛住嘅 BACK 2.00 @ 3.50 喺見到 CLOSED book 之後被動 fill，再 settle 賺 500p。如果 floor 已經過咗嗰個 CLOSED book 嘅時間，標記喺同一個 call 入面就被踢走，乜都冇寫，market 即刻當開。上一輪嘅 docstring 同測試話踢走係安全，但個測試踢咗 1.1 之後冇再查佢。**改法：** `_evict_paper_closed` 唔踢留低 book 唔係 CLOSED 嘅 market 嘅標記；`_note_paper_closed` 先換 book 再寫標記，所以踢嘅時候睇到新 book。之後一個唔舊過留低 book 嘅 CLOSED step 令留低嘅 book 變 CLOSED、lapse 掛單，標記先可以踢。代價：標記數可以多過 `PAPER_CLOSED_MARKERS_MAX`，最多多出 paper state 入面 book 嘅數目；嗰啲 market 本身成個 book、排隊同 `taken` 都已經喺 paper state。
  - **(minor ×2) 冇咗 paper lock 嘅 gateway 照寫 paper state。** `.paper.lock` 被刪或者被換、第二個 gateway 鎖住新檔之後，舊 gateway 嘅 closed-seen note（`place`／`replace` 收到 CLOSED book，喺 V9 之前，冇查 lock）會將佢過時嘅 paper state 變成最新嗰個。之後每次 `open()` 都冇咗新 gateway 寫嘅 `taken`（再開嘅 gateway 再食同一個深度：2.0 嘅深度總共 match 咗 200p）同 closed 標記（close 之前嘅 book 被接受，再 settle 賺 500p）。改之前 `place` 只會喺 submit 過咗 `_submit_refusal` 之後先寫 paper state，所以 `place` 呢條路係 regression；`paper-step`、paper `cancel` 同 `replace` 拒絕時嘅 report 喺 base 已經有同一個問題（verify2-1、verify2-2）。**改法：** 新嘅 `OrderGateway._require_paper_lock`：`paper-step`、paper `cancel` 同 `replace` 一開始就查 `held()`；`_note_paper_closed` 改 state 之前查；`_append` 寫每一條帶 `paper_state` 嘅 event 之前再查。冇咗 lock 就拒絕（`gateway_lock_lost`，CLI exit 2），乜都唔寫。`place` 收到 OPEN book 照舊喺 submit 之前拒絕（嗰啲拒絕 event 冇 paper state）。Request 中途冇咗 lock（submit 之後、report 之前）：report 同佢嘅 paper state 都唔寫，下一次 `open()` 用 `paper_interrupted_before_report` 準確解決。**剩低：** `held()` 查完到 append 之間先換 lock 嘅窗口（同 `_submit_refusal` 一樣，冇 compare-and-swap）；持有新 lock 嘅 gateway 唔會重讀 paper state；只交咗畀冇 lock gateway 嘅 CLOSED book 唔會記低（嗰個 gateway 報 `gateway_lock_lost`，同 verify-4-0 D 同一類），所以 probe 嘅 place／step variant 最後照樣 settle 一張 200p，分別係 operator 見到 `gateway_lock_lost`，而且冇任何標記被抹走。
  - **(minor) handoff 同 report 嘅數字過時。** 上面 R2 跟進之後冇重跑第 4–7 步，`artifact_sha256` 有 4 個 path 對唔上，report 仲寫舊嘅測試數字同 `code_sha256`。**改法：** 呢輪修正之後重跑咗第 4–7 步（見 "Status"）。
  - **測試：** `test_betfair_gateway`：`test_an_older_closed_book_keeps_its_market_closed_when_markers_are_evicted`（`paper-step` 同 `place` 兩條路，標記上限 patch 做 2，floor 已經過咗 CLOSED book 嘅情況，reopen）、`test_a_paper_gateway_that_lost_its_lock_journals_no_paper_state`（冇 lock 嘅 `place`／`replace`／`paper-step`／`cancel`、OPEN book 嘅 `place`、再開嘅 gateway 照計 `taken` 同標記、request 中途冇咗 lock），同埋改咗 `test_a_closed_book_paper_step_ignores_or_an_evicted_marker_still_closes_its_market`（而家查 1.1 嘅標記留低、1.1 照 closed）。三個喺改之前嘅 tree 都 fail。Full suite 890 個 test，OK（skipped 1）；Python 3.11.15 都係 890 個，OK（skipped 2）。文件喺 `docs/betfair-order-path.md` §11。
- **A20：Yu 喺 2026-10-11 決定。** 保留保守做法：Betfair 唔再返嘅 market 一直按 worst case 計。操作規則係 market close 之後盡快 settle。之後可能會考慮一個「forget-market」，但要等 first-live T-tests 做完，再重新設計同 review；v1 一定唔會有。Collab record：`report_94134b37-5c9d-4a98-b1de-9bae35a45a37`。Amendment §8 A20、§3.1、§7、K17，docs（order-path §8 同 §16），fact F77，report 同 handoff 入面「留畀 Yu」嘅字眼已經全部改晒。Astra 嘅 re-review 都記低 A20 已經決定；舊文件仲寫「未決定」，係佢預咗下一輪要改嘅文件更正，唔係新嘅設計問題。
- **C1–C4：Astra 接受為 closed。**
  - C1：Astra 用改正咗嘅 assertion 重做原本 stale matched price 嘅情況，之後嘅 LAY 被 `market_exposure` 拒絕，已有嘅 BACK 唔理舊 matched price 都計 200p。
  - C2：Astra 接受現時架構下喺 API 層執行：`JsonRpcClient.send_order` 每個 place body 都要有整數 `send_by` 同可以 call 嘅 gate，而佢係 production 入面唯一 call `order_digest` 嘅地方。Transport 層嘅「冇 bound 嘅 place 唔准送」guard 只係可選嘅 defence in depth，唔係 blocking，**冇做**。Full cancel 刻意豁免。
  - C3、C4：data reviewer 用原本嘅 recording 同 receipt 比較重做過。
  - Astra 亦確認：`settle-market` 處理一張 stale 嘅 LAY 2.00 @ 3.50，會入 500p worst-case 虧損、釋放 open 嘅 worst case、唔送單；一張 UNKNOWN 有 full-cancel 證據，照保留 200p（`exchange_reduction_withheld_until_settle`）。Astra 嘅 C1／settlement／UNKNOWN script 喺新 tree 輸出同一樣嘅三行。
- **歷史 receipt。** `reports/direct-collection-2026-10-10.json` 冇郁；`protected_sha256` 入面佢嘅 hash 仍然係 `19a06dae…`，同 Astra 記低嘅一樣。

## Status

U1–U6 已交付，每個 unit 都做咗一輪 implementation 同一輪按 review findings 嘅修正。Integration（2026-10-10）：將 `docs/betfair-order-path.md` 同 `docs/betfair-integration.md` 對返 U2–U5 嘅實際行為（PAPER_ASSUMPTIONS、`ABSENT_UNCONFIRMED → UNKNOWN`、preconfirm、A13 hash 檢查、kill／reconcile／settlement 細節）；跑咗第 3–7 步，數字喺 report 同 `handoff.json`。`reviews` 係 integration 寫低嘅摘要（未經 reviewer、冇 review 檔案，所以唔算獨立驗證），coordinator 可以補充 review 檔案，再改做 `{path: sha256}`。Audit 修正見 "Audit fixes and spec amendments"；修正之後重跑咗第 4–6 步。第三輪 audit 同 Astra review 之後，Yu 決定收窄 v1 live 範圍（上一節，amendment A3）：代碼、測試、文件、fact register、report 同呢個 handoff 已經跟住改，之後再跑咗第 4–7 步，數字喺 report 同 `handoff.json`；Astra C1–C4 點樣改喺 report 嘅 "Reviews"。之後收窄咗嘅 tree 再做咗一輪 review（「Review of the narrowed tree」），改完之後再跑咗第 4–7 步。第四輪 review 嘅 21 個 finding 改完之後（"Round 4 review fixes"），再跑咗第 4–7 步。之後 Astra 覆核咗嗰個候選 tree（`report_ef3fb90d-3ad9-4150-9b50-4796f91a4c32`，request_changes：R1、R2；C1–C4 接受為 closed），Yu 亦喺 2026-10-11 決定咗 A20。R1／R2 改完、文件跟住改之後（上一節），再跑咗第 4–7 步，數字喺 report 同 `handoff.json`。之後再做咗一輪 R1 覆核（上一節「R1 覆核（2026-10-12）」），4 個 finding 改完之後，再跑咗第 4–7 步。之後加咗 R2 跟進（`place`／`replace` 見到 CLOSED book 都 close 個 market），再對佢做咗一輪覆核（上一節「R2 跟進覆核（2026-10-12）」），4 個 finding 處理完之後，再跑咗第 4–7 步，數字喺 report 同 `handoff.json`。S0 已經套用：Yu（2026-10-12）同 Codex（2026-10-12，`report_0a7880fc-dddd-48b6-840a-4cf05ccfe71f`）同意之後，coordinator 將 `s0_patch` 原文套用做一個獨立 commit，四個檔案都係 `s0_expected_sha256_after`；同一個 commit 將 handoff 同 report 嘅 `s0_applied` 設 `true`、記低 `s0_agreement_recorded`、刪走 report 嘅 S0 limitation、改咗 report md 嘅 "Merge gate" 段同只喺 S0 之前先啱嘅句子、更新 report 嘅 `code_sha256`，再重做咗第 6 步。Astra 喺 2026-10-12 accept 咗 checkpoint `961a8630`（`report_3fe905c8-a4a7-4a60-aa70-1b45db236e8f`）；integration 喺 `feat/betfair-exchange` commit 做 `90f6b90`。Merge 去 main 之前要過 merge gate（`BETANGEL_BETFAIR_MERGE_GATE=1`）；merge、push 同 PR 都要 Yu 另外批。
