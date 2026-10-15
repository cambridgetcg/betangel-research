# Backtest lab: data plan, point-in-time rules and the P0 BSP contract

Prepared **2026-10-14**. 計劃同實驗合約，唔係 edge 或者盈利證據。寫呢份文件嗰陣 repo 冇任何 backtest 結果，亦冇跑過任何 backtest；P0 archive 下載咗未、下載咗幾多，manifest 未建立之前一律當未知。

**回測只係實驗室：出嚟嘅數唔係 edge 嘅證據，策略嘅單永遠唔會去到 Betfair。**

呢份文件講 Betfair historical data 嘅離線回測：用咩資料、每段日期做咩用、引擎點分層、策略睇得到乜、P0 嘅 BSP 模型同會計點計，同埋實驗點樣先凍結、後睇結果。設計由 Claude 提出，Astra 2026-10-14 review 並且修正（下文「Astra」指嗰份 design agreement）；Yu 嘅決定喺 §2.7 同 §12；§3 嘅 holdout 收窄係 Astra 2026-10-14 嘅技術決定，Claude 採納，Yu 同日同意；已決定同仲未決定嘅問題喺 §13。Claude 實作，Astra review。外部事實嘅出處同讀取日期喺 [§14](#14-sources-with-access-dates)；冇核實嘅嘢逐項標明。

## 1. Purpose and status

- **目的。** 將來嘅策略可以喺 historical 檔案上重播、模擬、計數，全部跟同一套 point-in-time、結算同會計規則，唔使每個實驗自己寫一次 loader 同會計。
- **唔係 edge 證據。** repo 仍然冇任何已確立嘅盈利策略（AGENTS.md）。2015–16 年嘅結果就算好睇，都只係 engineering／research 證據，唔代表而家有 edge；BSP 本身有冇效率都未證明（§9）。
- **冇單會去 Betfair。** AGENTS.md（2026-10-14 改）規定：策略只可以喺 `betangel_research/betfair/backtest` 入面、用明確畀佢嘅本地 historical、recorded 或 synthetic 檔案產生*模擬* order intent，只有離線 simulator 可以食；冇策略可以經 order gateway 或者 exchange API 落單；backtester 唔可以直接或者間接 import gateway、account／session／credential client、network transport 或者 trading CLI，亦唔可以讀 account profile 或者 live order state；root analysis module 照舊唔 import betfair。管理手動 gateway 同 arming 嗰條 bullet 冇改。
- **點樣守。** [`tests/test_betfair_backtest_boundaries.py`](../tests/test_betfair_backtest_boundaries.py) 淨係用 AST（乜都唔 import、唔執行）將成個 `betangel_research` 砌做 import graph：由 bytes parse，所以同 importer 一樣跟 PEP 263 coding cookie（另外要求 package 每個檔都係純 UTF-8）；function 入面同條件式嘅 import 都計，relative import 按 package 同 level 解析；`from X import Y` 嘅 `Y` 唔係 module，就一定要係 `X` 自己 bind 嘅名。解析唔到嘅 import、由 internal module 嘅 star import、source 旁邊 scan 唔到但 importer 載得入嘅檔（`.pyc`、extension module），同埋同名並存嘅 `X.py` 同 `X/__init__.py`（importer 先載 package，所以 scan 都讀 package，但照樣當失敗），全部當失敗。由每個 backtest module 出發，transitive closure 唔准去到 forbidden 嘅 betfair module（`gateway`、`api`、`auth`、`credentials`、`transport`、`esa`、`cli`、`__main__`、`killswitch`、`arming`、`presence`、`reconcile`、`paths`、`config`、`recorder`、`capability`、`risk`、`journal`）；closure 入面每個 module 只可以 import allowlist 上面嘅標準庫 module（`BACKTEST_STDLIB`：`argparse`、`json`、`pathlib`、`re` 等等）；查 allowlist 之前，先睇 package 旁邊（喺 repo 根用 `-m` 行嗰陣嘅 `sys.path[0]`）同 closure 入面每個 module 旁邊（直接當 script 行嗰陣嘅 `sys.path[0]`）嘅檔（`.py`、無 source 嘅 `.pyc`、extension module）同目錄，名唔分大細楷比較（macOS／Windows 設咗 `PYTHONCASEOK` 嗰陣，importer 都唔分）：closure import 嘅外部名喺度有同名檔或者目錄，就拒絕（Astra 2026-10-14 嘅 reproduction：package 旁邊一個 import gateway 嘅 `fractions.py`，以前淨係查 allowlist 會放行）；喺度任何同標準庫 module 同名嘅檔或者目錄，closure 有冇 import 都一律拒絕，因為標準庫自己用絕對名 import 自己（例如 `pathlib` import `fnmatch`，package 旁邊一個 `fnmatch.py` 就會經 `pathlib` 被載入）；root rule 一樣咁查。Importer 可能會先載佢哋（冇 `__init__.py` 嘅目錄同 builtin／frozen module 其實唔會被蓋過，呢度保守咁一齊拒絕）；第三方 package、`_socket` 呢類 private module、同埋經 `timeit`、`pickle`、`ctypes`、`concurrent.futures` 呢類 wrapper 去到網絡、process 或者 import 機制嘅，全部唔准；唔准用 `importlib`、`__import__`、`exec`、`eval`、`__package__`／`__path__`、`sys.modules`／`sys.path` 呢類動態 import；backtest module 唔准 bind backtest 以外嘅 internal package object；closure 入面唔准出現 state root、client certificate 目錄或者 home 目錄（`Path.home`、`expanduser`）。Root module 唔可以間接去到 betfair，亦只可以 import 標準庫（`scripts`、`tests` 呢類 repo 目錄喺 scan 範圍以外）。[`tests/test_betfair_backtest_contract.py`](../tests/test_betfair_backtest_contract.py) 再喺一個隔離嘅 interpreter 跑一次 validator，核對實際載入咗嘅 module。
- **佢證明唔到嘅嘢。** 標準庫自己入面嘅 import 冇追（例如 `pathlib` 自己 import `os`），不過上面講嘅搜尋位置入面任何標準庫名嘅檔都已經拒絕，所以標準庫唔會喺嗰度載到 repo 嘅檔；runtime 先砌出嚟、傳畀 `getattr` 嘅字串睇唔到，經 module object 嘅 attribute 去到佢 import 過嘅 module 亦睇唔到；`sys.path` 上 repo 以外嘅位置（另一個工作目錄、`PYTHONPATH`、user 或 site packages）有冇同名檔，亦睇唔到；repo 入面只喺其他啟動方式先入 `sys.path` 嘅目錄亦冇 scan（行 `scripts/` 入面嘅 script 嗰陣；`tests/` 係 `unittest discover` 嘅 `sys.path[0]`，只有 real-tree 測試查佢有冇標準庫名）；Python 唔會阻止一個 process 按路徑開檔，所以「唔讀 live state」都要靠 code review。Astra 講得清楚：「must not」係架構規則，唔係話 Python 有 sandbox；測試亦唔係 sandbox。一個以 Yu 身份刻意作惡嘅 process 照樣 import 得到任何嘢，同 [接入指南 §2](betfair-integration.md#2-threat-model) 嘅威脅模型一樣。

| 部分 | 狀態 |
|---|---|
| 本文件、AGENTS.md 嘅 scope bullet、P0 experiment contract 同佢嘅 validator、兩個測試 | task (a) 交付（§4） |
| Archive manifest、index、capability audit | task (b) 交付（§4.1；TAR 格式事實喺 §2.8）；內部 review 嘅修正已做，等 Astra review |
| BSP runner、會計、metrics、trial ledger、report | task (c)，計劃中 |
| LIMIT 單、flumine 對照、付費資料、racing data、prospective | P1–P4，暫定，未批（§4） |

## 2. Data plan

### 2.1 Betfair Historical Data tiers (horse racing, read 2026-10-14)

| Tier | 內容（spec） | 解像度 | 價錢（每個 sport-month） |
|---|---|---|---|
| BASIC | `LTP`、`MARKET_DEF` | 1 分鐘 | **£0**，每個月都係 |
| ADVANCED | `EX_BEST_OFFERS`（sample 有 3 層）、`TV`、`TRD`、`LTP`、`MARKET_DEF` | 1 秒 | £69 |
| PRO | `EX_ALL_OFFERS`、`TRD`、`TV`、`LTP`、`MARKET_DEF`、`SP_TRADED`、`SP_PROJECTED` | tick-by-tick | £230 |

- 價錢嚟自 portal 公開嘅 `GetStandardRateCard`（portal 寫 "all in GBP"）。單位係 sport-month，包晒所有國家：**唔可以淨係買 GB/IE**（`inferred`：rate card 按 sport-month 計價，portal 購物籃按 sport、plan、月份分項；Betfair 冇寫明）；GB/IE、`WIN`、file type `M` 只係下載時嘅 filter。
- **12 個月 bundle**：任何 12 個月、一個 sport，經 Developer Support 買；horse racing ADV £699、PRO £2,299（support 文章 2026-09-24 更新）。
- **免費年**：`GetPricePlan` 將 horse racing 2015-05..2016-04 列做所有 tier £0。佢亦列咗其他折扣月（例如 2020-03..05 全部 £0），本計劃冇用。
- **起始月有衝突**：介紹頁同 spec 寫 "from May 2015"，FAQ 寫 "from April 2015"，`AvailableData.csv` 列 horse racing 2015-04 有 19 個檔。P0 由 2015-05 開始，唔倚賴 2015-04。
- **幾時有**：portal FAQ 話資料喺 event settlement 之後 5 日先出，而且一個 event 嘅 market 要等成個 event 所有 market settle 晒先一齊出。§3 嘅 prospective 規則就係因為咁。
- **大細**：2026-09 horse racing 全部國家，portal 列 BASIC 92、ADV 3316、PRO 6463（單位推斷係 MB，`inferred`；三個 tier 都係 25,719 個檔，portal 購物籃叫佢做 markets）。GB/IE WIN 只係其中一部分；實際大細等 manifest 量度（§12），唔用推算。

### 2.2 Terms

- Portal 條款 "Personal Use Only"：所有 historical data 只限個人、內部用途，商業用途要另外搵 Betfair。"No onward distribution"：轉售、分發畀其他機構或個人、或者畀佢哋用，全部禁止，除非同 Betfair 有書面協議。下載或者使用即係接受條款。
- 要用 Betfair account 登入，經 account 買。Portal bundle 有 `INSUFFICIENT_FUNDS` 呢個錯誤字串，所以推斷係由 account 餘額扣錢（`inferred`）。.es、.it、.ro、.se 嘅 account 唔適用。
- **我哋嘅做法**（唔係 Betfair 嘅書面解釋）：raw 檔案同由佢抽出嚟嘅逐 market 資料，留喺 Git 同所有 worktree 以外（AGENTS.md：licensed raw data 唔入 Git）；雲端 bucket 一定 private（§12）；repo 只放代碼、contract、manifest hash 同結果摘要。結果摘要可唔可以公開分享，未問過 Betfair，唔當已准許。

### 2.3 What BASIC can and cannot support

下面「有」嘅欄位喺 2017 Punchestown BASIC sample 逐個 parse 過（2026-10-14 再數一次：runner change 只有 `ltp`；market definition 有 `ACTIVE`、`REMOVED`、`LOSER`、`WINNER`、`PLACED` 幾種 runner status）。一個 sample 唔代表 2015–16 或者 2023–25 嘅每個檔都一樣，所以 task (b) 嘅 capability audit 要逐檔數。

| 有（BASIC） | 冇（BASIC） |
|---|---|
| 每 1 分鐘嘅 runner `ltp`（成交價，唔係可執行報價） | 可執行嘅 back／lay 報價、depth、排隊位置 |
| Market definition：`marketTime`、`suspendTime`、`openDate`、`status`、`inPlay`、`betDelay`、`numberOfWinners`、`marketBaseRate`、`discountAllowed`、`bspMarket`、`bspReconciled`、`settledTime` 等 | 成交量（`tv`、`trd`）、runner 或 market 嘅 matched 總額 |
| Runner：`status`（ACTIVE／WINNER／LOSER／REMOVED…）、`adjustmentFactor`、`removalDate`、最終 `bsp` | `SP_PROJECTED`（near／far）、`SP_TRADED`（BSP 嘅 back／lay 量） |
| 由 market definition 得出嘅結果同 removal | 一分鐘以下嘅時間、我哋自己注碼對 BSP reconciliation 嘅影響 |

**冇就係冇，唔係零。**

- 某一分鐘冇 `ltp` 更新，係「冇新成交價」或者「唔知」，唔係價錢變咗 0；BASIC 冇成交量欄位，唔係成交量 0。
- 冇 `bsp`、`bspReconciled` 唔係 true、冇 CLOSED 嘅 market definition：BSP 同結果都係「未知」，唔可以當 0 或者當輸（§7）。結算唔當 0、唔當輸；§8 嘅悲觀 all-issued ROI 只係另外報嘅下限，唔係結算。
- **Capability 分兩類，contract 分開寫（`capabilities.decision` 同 `capabilities.outcome`）：**
  - **決定時間嘅 input**（`ltp`、`market_definition` 等）：決定時間嘅 observed prefix 入面冇，就要有明確嘅 `unsupported` 或者 `abstain` 原因寫入 ledger（§6），唔可以靜靜跳過（`on_missing` `unsupported_abstain`）。有冇係睇決定時間嘅 prefix，唔係睇成個檔之後有冇。
  - **結果**（`final_bsp`、`results`）：只係用嚟結算。冇或者之後改咗，**都唔可以改變之前已經做咗嘅決定**：張單照出、照留喺 ledger、exposure 照計，結算做 unresolved（`on_missing` `unresolved_settlement`；§7 `missing_bsp_or_result`）。因為結果冇而 abstain，會令 population 跟結果有冇而變，違反 prefix-invariance（§5），所以 validator 拒絕（`would_change_an_earlier_decision`）；將結果寫入 decision 類亦拒絕（`final_bsp_is_an_outcome`）。
  - 一整個 data product（一個 tier 或者 provider）嘅 spec 或者 audit 顯示成個 product 都冇某個 capability（例如成個 tier 都冇 BSP 欄位），而且判斷唔睇邊啲 market 有冇結果，就可以喺凍結之前喺 contract 入面拒絕（例如 `population.tier` 或者 `population.provider` 揀另一個）。已接受嘅 product 入面個別 market 或者檔冇 BSP，照留喺 cohort，張單照出，結算 unresolved；唔可以按結果有冇揀走一批檔，單一 market 冇結果亦唔可以事後變成策略嘅 abstain。
- P0 contract：decision 要 `ltp`、`market_definition`，outcome 要 `final_bsp`、`results`。Decision 類亦識 `best_offers`、`all_offers`、`traded_volume`、`traded_ladder`、`sp_projected`、`sp_traded`，留畀 ADV／PRO 嘅 contract。邊個檔真係有邊個 capability，要等 task (b) 嘅 audit，contract 本身證明唔到；audit 嘅 `fields_present_before_cutoff` 同 `fields_valued_before_cutoff` 就係按 cutoff 之前嘅 prefix 數（`fields_present` 係成個檔，連結果都計埋，唔可以用嚟判斷 decision capability，§4.1）。

### 2.4 Strategy class and minimum tier

| 策略類別 | 最低 tier | 點解／限制 |
|---|---|---|
| Fixed-stake BACK at BSP（MOC），用賽前嘅 1 分鐘 LTP、schedule、field 做決定 | BASIC | 最終 BSP 同結果喺 market definition；要假設我哋嘅注碼唔改 BSP，BASIC 重建唔到（§7） |
| 只出概率、唔落單嘅 forecast／calibration（LTP 衍生） | BASIC | LTP 唔係可執行報價；只計 proper score（§8） |
| LAY at BSP、LOC | 未定（延後） | liability 同 LOC 嘅 matching 要另外建模，唔聲稱 BASIC 夠 |
| 賽前 LIMIT 單、E01（price／volume，30／60／180 秒 feature）、motif lane | ADVANCED | 要報價、size 同成交量；motif reader 只認 ADV 格式，BASIC 會被標 `unsupported_basic_or_unknown_fields`，PRO 欄位會被標 `unsupported_pro_or_mixed_fields`（[接入指南 §9](betfair-integration.md#9-stream-recorder)） |
| 排隊位置、full depth、被動 fill、BSP projection（`SP_PROJECTED`／`SP_TRADED`）、flumine simulation | PRO | 要 tick-by-tick 同 `EX_ALL_OFFERS`；Betfair Data Scientists 嘅 flumine 教學只用 PRO 檔，寫明冇喺 BASIC／ADVANCED 試過；flumine 本身最低要邊個 tier，未核實（§11） |

Astra 特別講明：BASIC 嘅 P0 係一個新嘅 LTP／BSP 實驗，**唔係原封不動嘅 E01**。E01 要 quotes、sizes、volume 同 30／60／180 秒 feature；motif 亦要更豐富嘅資料同 context。

### 2.5 The BSP CSV licence question

`promo.betfair.com/betfairsp/prices` 有每日嘅 BSP CSV：免費、唔使登入，UK／IRE WIN 由 2007-04 起，PLACE 由 2008-08-16 起；檔名日期係賽事翌日（`13102026` 嗰個係 2026-10-12 嘅賽事）。欄位有 `bsp`、`ppwap`、`morningwap`、`ppmax`／`ppmin`、`ipmax`／`ipmin` 同幾個成交量；`bsp` 有 8 個小數位。欄位名大細楷跟檔案年份唔同（[data audit](data-audit.md) 睇過嘅 2017 檔係大寫 `EVENT_ID`、`BSP`…，2026-10 檔係細寫），parser 要唔分大細楷。

但係 BSP 頁尾寫 "may not be used for any purpose without a licence"，冇講適用邊個 licence；Historical Data portal 嘅條款就寫 "any business purpose" 先要 licence，並准個人、內部用途。BSP CSV 屬唔屬後者嘅授權範圍，未清楚。**P0 唔用 BSP CSV**：BSP 由 BASIC 檔嘅 market definition 嚟。Repo 嘅 [data audit](data-audit.md) 之前睇過兩個 2017 IE CSV（喺 Git 以外）。要拎 BSP CSV 做 cross-check 之前，先要 Yu 決定，或者向 Betfair 問清楚。

### 2.6 Racing data: deferred

P0 唔需要賽馬 form data，全部延後。2026-10-14 睇過嘅選擇（價錢係當日公開頁面，未買、未試）：

| 來源 | 價錢同條款（摘要） |
|---|---|
| The Racing API | Free £0、Basic £27.99、Standard £59.99、Pro £99.99 每月；Pro historical add-on 一次過 £499；ToS（2023-07-12 更新）禁止未經准許轉售 |
| Timeform API | 只有 commercial terms，冇公開價錢 |
| Smartform（Betwise） | database £195 + 每月 £65；由 2003-01-01 起；授權係畀 Betwise 會員個人用 |
| Proform | Standard £37.50、Premium £97.50 每月；TPD sectionals add-on 每月 £50（第一年要一次過預繳 £600） |
| Betfair Data Scientists hub | UK/IE ratings model CSV（2024、2025、2026-01..09），有 `WIN_BSP`、`WIN_RESULT`；"you use the data at your own risk" |

- Sporting Life 條款明確禁止 screen scraping；Racing Post 條款 403、At The Races 有 challenge；BHA 結果頁冇 API；三個嘅條款都係 `unverified`。
- 將來用任何 racing data，都要先確定每個欄位幾時公佈；今日嘅新聞、修訂過嘅 rating 或者 LLM 嘅知識，唔會自動變成當時用得到嘅 context（AGENTS.md 同 [edge framework](edge-framework.md)）。
- Hub 嘅 2026-01..08 檔落喺 holdout 期（§3），唔好下載；2026-09 嗰個研究時已經下載過，係已知暴露（§3）。

### 2.7 Greyhounds and soccer: not now (Yu, 2026-10-14)

Yu 2026-10-14 問咗 greyhound racing 同 soccer 要唔要，之後（約 13:57Z）決定："no need for greyhound and soccer now. We focus on horse racing." 所以而家只做 horse racing（P0 係 GB/IE、WIN）。下面嘅價錢同檔數留做參考，將來再諗嗰陣用；冇因為呢個決定改過任何數字。

- **價錢同 coverage（參考）**（2026-10-14 讀）：同一份 `GetStandardRateCard` 每個 sport-month：Greyhound Racing BASIC £0、ADV £49、PRO £150；Soccer BASIC £0、ADV £69、PRO £230。`GetPricePlan` 2015-05..2016-04 兩個 sport 都係所有 tier £0。`AvailableData.csv` 兩個都由 2015-04 起有檔；2026-09 全部國家：greyhound BASIC 43、ADV 1107、PRO 2395（單位推斷係 MB，`inferred`），28,170 個檔；soccer BASIC 487、ADV 16,281、PRO 23,603，206,525 個檔。只係價錢同檔數，冇做 capability audit、冇睇內容。
- Contract schema v1 嘅 `population.sport` 只收 `horse_racing`；`greyhound_racing` 同 `soccer` 會被拒（`not_in_schema_v1`）。呢個範圍同 Yu 嘅決定一致；將來 Yu 要加，要開 schema v2 同新 contract。`countries` 只收 GB、IE，`market_types` 只收 WIN。
- 引擎保持 sport-agnostic：market、runner、決定、結算同會計嘅 interface 唔寫死 horse racing。將來加一個 sport，要先做佢自己嘅價錢查核同 capability audit（例如 soccer 有好多 market type、`numberOfWinners` 唔一定係 1、有 handicap market），再開一份新 contract。

### 2.8 What a Historical Data TAR looks like (task (b), read 2026-10-14)

- **Betfair 自己寫嘅**：portal FAQ 寫 "The files are downloaded as compressed .tar files"，資料係 ".json format"；"File are provided by both marketId and eventId"，event 檔包晒成個 event 所有 market，按 published time 排；下載 filter 有 File Type（M=market、E=event），FAQ 建議淨係揀 M 或者 E 嚟減細下載量，即係一個 TAR 可以兩樣都有，同一個 market 會出現兩次。Spec PDF 就寫 "provided as tar.bz2 files"。
- **Betfair 嘅文字冇講 TAR 入面嘅路徑格式。** 唯一寫咗嘅路徑係 historic API `DownloadListOfFiles` 嘅例子 `/data/xds/historic/BASIC/28139610/1.130129050.bz2`，即係 `<PLAN>/<eventId>/<marketId>.bz2`，**冇日期**。Betfair Data Scientists 兩個教學用 `tarfile.TarFile`（冇外層壓縮）開 TAR，再逐個 member `bz2.open`；flumine 教學由 member 檔名攞 market id。
- **Task (b) 嘅假設（`unverified`）**：TAR 入面係 `<PLAN>/<YYYY>/<Mon>/<D>/<eventId>/<marketId 或 eventId>.bz2`（用家普遍報告嘅格式，本文件冇 Betfair 原文證明）。`archive.py` 只認呢個格式；其他格式（包括上面 API 嗰個冇日期嘅）一律報 `no_parseable_month` 拒絕，唔估月份。第一個真 TAR 嘅 manifest 會話畀我哋知：如果所有 member 都係 `no_parseable_month`，就要改 layout（新 manifest schema），唔可以放寬去估。外層 TAR 有冇再壓縮都未見過：`archive.py` 只收冇壓縮嘅 TAR（同教學一樣），壓縮咗嘅報 `archive:not_a_readable_tar`。
- **真 TAR 證實咗（2026-10-14，Yu 喺 portal 下載嘅 BASIC GB/IE WIN M 檔；只列 header 同檔名，冇解壓、冇讀內容）**：52,561 個 member 全部係 `BASIC/<YYYY>/<Mon>/<D>/<eventId>/<marketId>.bz2`（例如 `BASIC/2015/May/1/27434261/1.118541020.bz2`），即係上面嘅假設啱；全部係普通檔（type `0`）、冇 E 檔。外層 TAR 冇壓縮，header magic `ustar\0` `00`，Python 讀到係 PAX format：**每個 member 都有一個 pax extended header**，入面淨係 `atime`、`ctime`、`mtime`、`LIBARCHIVE.creationtime`（大約 150 bytes，mtime 例如 1557303870 即係 2019-05-08），冇 `path`／`size` 覆寫，冇 global header；最後一個 member 之後正好 1,024 個 zero byte，冇補到 10,240 byte 嘅 record。即係用 libarchive 砌嘅。檔案 496,945,664 bytes，sha256 `3a16add79316fccb1a44bb86098fdc9eee2e090888c48656ec76978d88aad2cb`。`tests/test_betfair_backtest_archive.py` 有一個照呢個容器格式砌嘅 regression test。
- **路徑上嗰個日期係乜嘢日期，未知**（可能係 event 日期）。所以 index 另外由第一次公佈嘅 `marketTime` 計月份，唔夾就標 `month_mismatch`；ingest 亦會掃 member 內容入面嘅 `marketTime` 月份做第二重把關（§4.1）。
- **Smoke run 見到嘅**（2026-10-14；2017 Punchestown 三個 sample，已經睇過嘅資料，抄去 scratch 砌成上面嘅 layout，只做格式測試，冇入 repo）：event 檔壓縮比 BASIC 20.2、ADVANCED 10.0、PRO 12.3；每個 tier 34 個 market（8 WIN、7 PLACE、12 OTHER_PLACE、7 EACH_WAY），全部有 CLOSED、冇 dead heat、冇 `marketTime` 修改、路徑同 `marketTime` 月份全部夾；removal 喺 cutoff 前 50 個、後 5 個（每個 tier）。PRO 嘅 runner change 有 `tv`（spec 表寫 PRO 嘅 runner `tv` 係 N），而且每個 `mc` 都有 `con` 同 `img` key（`img` true 只喺 34 個入面 22 個 market）；ADVANCED 有 `batb`／`batl`、冇 `atb`／`atl`，PRO 相反；`spn`／`spf`／`spb`／`spl` 只喺 PRO 嘅 BSP market（7 WIN、7 PLACE）。**8 個 WIN market 入面有 1 個 `bspMarket` 係 false**（`1m7f Charity Stks`），即係冇 BSP；`bspMarket` 係決定時間已知嘅欄位，P0 要點處理未定（§13）。另外有兩個 WIN market 嘅 `name` 喺開跑前改過（例如 `3m Chs` → `3m XC Chs`）。
- **Review 修正之後再跑一次**（同日，同一個 TAR，sha256 `f2dcfde8…`）：上面嘅數照舊。另外：每個 tier 有 20 個 market `bspMarket` false（1 WIN、7 EACH_WAY、12 OTHER_PLACE），缺最終 BSP 嘅就係呢 20 個，BSP market 一個都冇缺。EACH_WAY 嘅 `numberOfWinners` 係名次數（2、3 或 4），結算係 1 個 WINNER 加其餘 PLACED，所以 dead heat 要按 market type 判（§4.1）；三個 tier 21 個 EACH_WAY 全部判到，冇 dead heat。PRO 嘅 runner change 入面 `spn`／`spf` 有引號字串：`spf` `"NaN"` 14,656 個、`"Infinity"` 271 個，`spn` `"NaN"` 14 個、`"Infinity"` 1,168 個（數字嘅 `spn` 51,355、`spf` 37,610）；market definition 嘅 1,181 個 runner `bsp` 全部係數字。Cutoff 之前：三個 tier 所有 WIN 同 EACH_WAY market 都有 > 0 嘅 `ltp`；PRO 8 個 WIN 入面 7 個（BSP market）有數字嘅 `spn`／`spf`。冇 allowlist 以外嘅 definition 欄位、冇未知 runner status、路徑日期同 event id 全部夾、冇 CLOSED 之後改結果。Python `tarfile`（GNU format）同 bsdtar 3.5.3 寫嘅 TAR 都以兩個以上 zero block 結尾，過到 end-of-archive 規則；但 macOS `/usr/bin/tar` 預設會加 AppleDouble `._*` member，成個 TAR 會因 `unsafe_name` 被拒（`COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs` 砌嘅就收）。Ingest 290 MB（解壓後）大約 3.6 秒，index 大約 11 秒，audit 0.1 秒，呢部 MacBook Air 上量；每行 record 平均 14 KB（7.7–27 KB）。
- **Astra review（R1–R4、N1–N2）修正之後再跑**（同日，同一批三個 sample 重新 stage 成同一個 layout；TAR sha256 `a61ff10c…`，同上面唔同只係因為 stage 檔嘅 mtime）：修正前（35bfe6f）同修正後嘅 code 各自喺一個新 data root 跑 ingest → index → audit → 再 ingest → 再 index，兩邊嘅 manifest（sha256 `f5b92272…`）、markets 檔、head（`index_sha256` `abc49c75…`）同 audit JSON 逐 byte 一樣：102 個 market、removal cutoff 前 150 個、後 15 個、冇 dead heat、冇 `marketTime` 修改、全部 CLOSED、冇讀唔完嘅檔，第二次 ingest `already_ingested`、第二次 index `unchanged`。即係 PRO 嘅 274 個 `img` true change（全部帶齊 definition 同 runner，§5）冇改到任何 record，冇 member 失敗所以 R3 冇影響，冇 pax sparse，`marketTime` 冇喺 chunk 邊界改月份。兩個版本總時間都係大約 31 秒（五步加埋）。之後 verification 嘅修正再跑一次（同一個 TAR，新 data root）：manifest、markets 檔、head 同 audit JSON 照樣同 35bfe6f 逐 byte 一樣（`index_sha256` `abc49c75…`），第二次 ingest `already_ingested`、第二次 index `unchanged`，大約 31 秒。
- **Astra 再 review（R1 primary 來源）修正之後再跑**（同一個 TAR `a61ff10c…`，三個 member 同 data-options 嘅三個 sample 逐 byte 一樣，喺 scratch 跑）：修正前（0db64db）同修正後各自一個新 data root 跑同樣五步，manifest（`f5b92272…`）、markets 檔（`9235359d…`）、head（`index_sha256` `abc49c75…`）同 audit JSON 同上一輪逐 byte 一樣：102 個 market，第二次 ingest `already_ingested`、第二次 index `unchanged`，兩邊都係大約 31 秒。呢個 TAR 淨係有 E 檔、每個 market 都喺開跑前公佈，所以 primary 同 view 都冇變，冇一個空 view（§4.1）；即係呢個 smoke 證明唔到新規則，只證明佢冇改到普通情況。

## 3. Date ranges and roles

| 範圍 | 角色 | 規則 |
|---|---|---|
| 2015-05..2016-04，BASIC horse racing | **P0 engineering corpus**（所有 tier £0） | 只係 engineering／research 證據，唔係而家有 edge 嘅證據 |
| 2023-01..2025-12，BASIC horse racing | **Research corpus** | schema v1 唔再切 research：`fitting.kind` 只准 `none`（唔由結果估任何參數），train／validation／test 或者 walk-forward 要 schema v2（§13） |
| 2026-01..2026-08 | **Holdout：刻意唔下載** | 靠「唔下載」封住：就本 project 嘅記錄冇下載過，但冇獨立 audit 證明冇人睇過；呢個係 workflow control，唔係保安封條 |
| 2026-09 | **已暴露：只做 exploratory** | 研究時下載過有結果嘅 hub CSV（見下面「已知暴露」）；永遠唔係 holdout，v1 冇任何 split 收得佢 |
| 2026-10 起 | **Prospective**，有條件 | 只有喺凍結之後、事件之前記錄咗嘅決定先算；2026-10-12 嘅賽事已經暴露，永遠唔算 |
| 2017-04-29 Punchestown samples（BASIC／ADV／PRO）、兩個 2017 IE BSP CSV、繼承嘅兩場 pilot | 已經睇過 | **永遠唔係 holdout**，只可以做 exploratory／engineering |

**月份點計。** 上面所有月份（holdout、ingest 嘅 month gate、audit 嘅分組、`month_mismatch`）都係 `marketTime` 嘅 UTC 曆月；例如澳洲當地 2026-01-01、`marketTime` 係 `2025-12-31T23:30Z` 嘅賽事屬於 2025-12。TAR 路徑上嘅日期係乜嘢日期（UTC、當地、event 日）未知（§2.8）。

**原本嘅提議作廢（superseded）。** 9716526 嗰版將 holdout 定做 2026-01..2026-09；呢個範圍已經作廢，因為 2026-09 有已知暴露（見下面）。v1 未被接受，所以直接更正 v1（`schema_version` 照係 1，P0 contract 嘅 hash 變咗）。

Contract（§10）將頭三段寫做 `population.splits`：`engineering`、`research`，同 `holdout`（`state: sealed_not_downloaded`）。Validator 將 holdout 釘死喺 2026-01..2026-08（`SEALED_HOLDOUT`；寫其他月份報 `not_the_sealed_holdout`，例如原本嘅 2026-01..2026-09 會報 `not_the_sealed_holdout` 同 `exposed_not_holdout`），拒絕同呢段重疊嘅 engineering 或 research（`overlaps_sealed_holdout`）、包含已經睇過月份（`INSPECTED_MONTHS`，2017-04）嘅 holdout（`inspected_not_holdout`）或者 research（`inspected_exploratory_only`；兩種 `registration.kind` 都係咁，engineering 就可以包含）、包含已暴露月份（`EXPOSED_MONTHS` 嘅 2026-09，同 `NOT_PROSPECTIVE_DATES` 嘅 2026-10-12 所屬嘅 2026-10）嘅任何 split（holdout 報 `exposed_not_holdout`；engineering 或 research 報 `exposed_exploratory_only`，而且因為排喺 holdout 之後，亦報 `not_after_other_splits`）、重疊嘅 split、唔係排喺最後嘅 holdout，同埋任何評估 holdout 嘅 contract（`population.evaluate_split` 寫 `holdout` 會報 `sealed`）。改呢啲常數就係改 schema。`registration.kind` 決定評估邊段：`engineering_check` 只可以評估 engineering，`registered_hypothesis` 只可以評估 research。Engineering 嘅月份由第一次跑開始就當睇過。Validator 只係用固定常數擋住已知睇過嘅月份，唔證明 research 其他月份未掂過：邊啲月份已經評估過，由 trial ledger 同工作流程記錄。

**Prospective 點先算。** 次序一定要係：contract 凍結（有 hash）→ 喺事件之前記錄決定 → 事件發生 → archive 出（portal FAQ：settlement 之後大約 5 日）→ 評估。一個喺凍結之後先*公佈*嘅月份唔夠，因為 archive 本身就係 settlement 之後先出：佢入面嘅事件可能早過凍結。最好連預測都喺結果之前記錄。

**已知暴露（2026-10-14）。** Data-options 研究為咗睇格式，喺 session scratchpad（Git 以外）下載咗兩個有結果嘅檔：Betfair Data Scientists hub 嘅 `uk_ie_2026-09.csv`（2026-09 UK/IE，有 `WIN_BSP` 同 `WIN_RESULT`）同 BSP CSV `13102026`（2026-10-12 嘅賽事）。寫呢份文件嗰陣睇過兩個檔嘅 header 同頭一至三行；研究嗰陣睇過幾多，冇記錄（`unknown`）。所以 2026-09 唔可以話「完全冇人見過」。兩個 scratch 檔 2026-10-14 已經刪走（刪之前嘅 sha256：`uk_ie_2026-09.csv` `8e999e30a7fc17b2dd981cc8d223071b63d94797fbe08884d7ef67d0ec22ec92`，`sample_ukwin_13102026.csv` `e1bdc0816bac5ebac2a1fdefa4a46f661cfb65ab0fbbd080ab3dc3545c5e2244`）；刪咗唔會令佢哋變返未睇過。

**決定（2026-10-14）：Astra 嘅技術決定，Claude 採納，Yu 同日同意（「very sensible decision」）。**

- Holdout 收窄到 **2026-01..2026-08**（`SEALED_HOLDOUT`），`state` 照係 `sealed_not_downloaded`。冇證據要放棄呢八個月，但亦冇人做過獨立 audit 證明冇人睇過：佢哋係 holdout，只係因為就本 project 嘅記錄冇下載過。
- **2026-09 記做已暴露、只做 exploratory**：`contract.py` 嘅 `EXPOSED_MONTHS` 記低檔名、來源、sha256、刪除日期、`inspection_extent` `unknown`，同已知最少睇過嘅（`known_minimum`：2026-10-14 為咗寫呢份計劃睇過 header 同頭一至三行）；validator 拒絕任何包含佢嘅 split。
- **2026-10-12 嘅賽事唔可以做 prospective**：佢哋早過任何凍結，而且結果已經下載過；記喺 `NOT_PROSPECTIVE_DATES`（同樣有 sha256 同 `known_minimum`），validator 拒絕任何包含 2026-10 嘅 split（v1 嘅 split 以月計，所以成個月都拒絕）。Schema v1 冇 prospective split，所以呢個係畀之後嘅 schema 同 trial ledger 用嘅記錄。
- 將來嘅 trial ledger 都要帶住呢兩個 hash、已知最少睇過嘅（header 同頭一至三行），同「研究時睇過幾多：unknown」呢個記錄。

## 4. Architecture

```mermaid
flowchart LR
    RAW[raw archive outside Git, per-archive and per-file sha256] --> MAN[versioned manifest and index]
    MAN --> AUD[capability and coverage audit]
    MAN --> LOAD[streaming per-market loader]
    LOAD --> VIEW[point-in-time view: observed prefix only]
    VIEW --> STRAT[strategy: forecasts, watch/abstain, simulated intents]
    STRAT --> SIM[offline simulator: BSP MOC in P0]
    MAN -.->|evaluator only: BSP, results, removals| SIM
    SIM --> ACC[accounting: settlement, commission per market]
    ACC --> EVAL[evaluation: ROI, coverage, blocks, scores]
    EVAL --> LEDGER[trial ledger and report]
```

**Raw archive。** 放喺 Git 同所有 worktree 以外，用一個可設定嘅 data root。每個 archive（tar）同入面每個檔都記 sha256，記咗就唔改。解壓要有界同安全：拒絕絕對路徑、`..`、symlink／hardlink，限制檔數、大細同壓縮比。Manifest 同 index 有 schema version。Index 由 market definition 記 market、event、venue、country、type、`marketTime`、runner、removal、`marketBaseRate`；BSP 同結果都喺 index 入面，但只畀 evaluator 用，策略睇唔到（§5）。Task (b) 已經照呢段實作，用法同細節喺 §4.1。

**代碼位置。** 全部喺 `betangel_research/betfair/backtest/`，用獨立嘅 `python3 -m betangel_research.betfair.backtest` 行，唔經現有兩個 CLI。佢只收明確畀佢嘅本地檔案，冇 storage client、冇 account client。Root module 唔 import betfair 嘅規則保留。Repo 已經有 forecast replay／scoring、motif trace 同固定 order 嘅 `PaperExchange` replay；欠嘅係一個統一嘅 strategy／corpus runner。Reuse 要留意（Astra）：

- `PaperExchange` 唔支援 MOC／LOC，用 LIMIT／LAPSE 假設（[落單路徑 §11](betfair-order-path.md#11-paper-model-assumptions)），**唔係現成嘅 BSP engine**；用佢要畀明確嘅本地 run path，唔讀 profile 或者 live state。
- `motifs` 會 lazy import root `__main__`；要 reuse 就 reuse 佢嘅純 policy。
- 現有 `pnl.py` 遇到 removal 或者 dead heat 就將成個 market 剔出 P&L 總數；backtest 唔可以咁做，要留低做 unresolved（§7）。
- 舊嘅 `test_betfair_boundaries` 只掃直屬檔案、假設 relative import 只係上一層、冇查 transitive import；所以 task (a) 另外寫咗 §1 嗰個 recursive、識 package 嘅 boundary test。

**而家邊啲 module 真係 reuse 得（task (a) 嘅 boundary test 記低咗，2026-10-14 喺 base `8b0da84` 上）：**

| 結果 | Modules | 原因 |
|---|---|---|
| 乾淨 | `constants`、`ticks`、`pnl`、`errors`、`secret`，同 root 嘅 `evaluation`、`stream`、`temporal` | closure 冇 forbidden module，只用 allowlist 上面嘅標準庫 |
| Forbidden | `journal` | Gateway open 嗰陣 fold 嘅 order journal；`OrderJournal(path, mode="live")` 會寫 live order state（intent、resume、kill、halt），AGENTS.md 唔准 backtester 碰 |
| 唔乾淨 | `receipts`，同所有 import 佢嘅：`schemas`、`filters`、`lifecycle`、`book`、`paper`、`validate` | `receipts` 喺 module level import `subprocess`（`git_state` 會跑 git），亦 import `os`、`fcntl`、`secrets`、`time` 呢啲 allowlist 以外嘅 module |
| 唔乾淨（另加） | `validate`、`paper` | `validate` import `config`（讀 profile 嘅 `config.json`），`config` 再 import `paths`（state root、live journal、KILL file）；`paper` 經 `validate` 去到兩者，亦直接 import `journal` |

即係話 task (c) 有兩條路：一係先開一個獨立 task 將純函數拆出嚟（例如將 canonical JSON 同時間 parse 搬去一個冇 `git_state` 嘅 module、將 `money_pence` 搬出 `config`），畀 Astra 另外 review；一係只用 `constants`、`ticks`、`pnl` 寫自己嘅純代碼。Function 入面 lazy import 都唔得，boundary test 一樣計。P1 想 reuse `PaperExchange` 都要等呢個拆分，而且要一個 review 過、收唔到 `mode="live"` 嘅 paper-only journal facade。

**Tasks。** Claude 一個人寫，Astra review；每個 task 要窄、要先 claim。Collector、feeds、deploy 同 root entrypoint 唔喺呢啲 task 範圍。

| Task | 交付 |
|---|---|
| (a) plan 同 contract | 本文件；AGENTS.md scope bullet（coordinator 套用）；`betangel_research/betfair/backtest/` 嘅 `__init__.py`（只有 docstring）、`contract.py`（schema v1 validator 同 canonical sha256）同 `__main__.py`（`validate-contract`）；P0 draft contract [`research/experiments/backtest-p0-bsp-v1.json`](../research/experiments/backtest-p0-bsp-v1.json)；[`tests/test_betfair_backtest_contract.py`](../tests/test_betfair_backtest_contract.py) 同 [`tests/test_betfair_backtest_boundaries.py`](../tests/test_betfair_backtest_boundaries.py)。冇 loader、冇 simulator、冇會計 |
| (b) archive | `archive.py`（有界、安全、按月份把關嘅 TAR ingest 同 manifest）、`market_index.py`（逐 market index，`decision_time` 同 `evaluator_only_outcome` 分開）、`coverage_audit.py`（只有整數計數嘅 capability／coverage audit）、`__main__.py` 三個 subcommand、[`tests/test_betfair_backtest_archive.py`](../tests/test_betfair_backtest_archive.py)；boundary test 加咗 `tarfile`（§4.1）。已交付；內部 review 嘅修正已做（§4.1），等 Astra review |
| (c) BSP runner | streaming loader、point-in-time view、strategy API、BSP simulator、會計、metrics、trial ledger、report，同 §9 嘅 synthetic 測試；一定要包括 §5 嘅 prefix-invariance 測試，連「之後嘅 BSP 同結果刪走或者改咗，之前嘅決定一模一樣」（計劃中） |

**之後（暫定，Yu 同 Astra 未批；只有 (a)–(c) 係同意咗嘅範圍）：**

- **P1**：LIMIT 單。將 `PaperExchange` in-process 接入（in-memory journal；要先過上面嘅 reuse 拆分同 paper-only journal facade），喺免費年嘅 PRO 檔同 flumine 對照（§11）。
- **P2**：付費 ADV／PRO 資料。Yu 決定，而且要對住 P0／P1 量度到嘅結果先傾。
- **P3**：racing data 同其他 context（§2.6），要先解決公佈時間同授權。
- **P4**：prospective：凍結之後喺事件之前記錄決定，archive 出咗先評估（§3）。

### 4.1 Archive tools (task (b))

全部只收明確畀佢嘅本地檔案同一個明確嘅 data root；冇 network、冇 account、冇 profile、唔落單。2026-10-14 交付之後，內部 review（security、data、point-in-time、tests 四個 lens）搵到嘅問題同日修正咗；之後 Astra review 搵到四個 P2（R1–R4）同兩個 nonblocking（N1、N2），同日再修正，每個都有一個先重現佢個例子、修正後先過嘅 regression test。下面寫嘅係兩輪修正之後嘅行為：primary 來源唔再睇讀唔讀得完（R1）、image 真係喺佢個 `pt` reset market state（R2）、失敗 member 嘅解壓量計入成個 archive 嘅上限（R3）、pax GNU sparse 喺 tarfile 讀 sparse map 之前拒絕（R4）、俾 chunk 邊界切開嘅 `marketTime` 唔再用字面月份（N1）、`manifests/<sha>.json.partial` 喺寫任何嘢之前查（N2）。之後再做咗一輪內部 verification（index、archive、tests 三個 lens，連 mutation 測試），搵到 R3 嘅上限漏咗 libbz2 失敗時丟咗嘅輸出（blocker）、讀得到 `pt` 嘅壞 message 仍然會將 view 截早（R1 同一類），同幾個未測或者寫過頭嘅邊界；同日修正，下面逐項寫明。之後 Astra 再 review（R2、R3、R4、N1、N2 閂咗）話 R1 仲有一樣：primary 揀「第一個有呢個 market 嘅 record 嘅來源」，而一個來源*會唔會*有 record 本身就係將來嘅資訊；同日改成淨係按 member 存唔存在同路徑揀，冇 view 嘅 market 一律用同一個明確嘅空 view（見 `decision_time` 同 `other_sources`）。之後再一輪內部 verification（invariance 同 tests 兩個 lens）搵到：冇以呢個 market 命名嘅 M 檔時，primary 仍然睇「喺邊度見到呢個 market」，任何時間（包括開跑後）見到都算，所以開跑後先加或者截走嘅一行仍然可以換來源、抹走或者變出之前嘅 view；同日改成睇最早提到呢個 market 嘅 message（見 `other_sources` 嘅「最早提到」），連埋幾個未測嘅 guard 同寫過頭嘅說法。

```sh
python3 -m betangel_research.betfair.backtest archive-ingest --tar <下載咗嘅>.tar --data-root ~/betangel-data/historic
python3 -m betangel_research.betfair.backtest archive-index --data-root ~/betangel-data/historic [--archive <sha256>]
python3 -m betangel_research.betfair.backtest archive-audit --data-root ~/betangel-data/historic
```

- **輸出同 exit code**：每個命令印一行 JSON（key 排好序）；`status` 係 `refused` 就 exit 2，列晒所有 `problems`（`<位置>:<原因>`）；其他 exit 0；argparse 用法錯誤都係 2。檔案系統出錯（冇權限、應該係檔嘅位置係目錄、唯讀嘅 data root、`manifests/` 讀唔到）一律變成有位置嘅 refusal（例如 `raw:<路徑>:unreadable`、`manifest:<sha>:unreadable`、`index:<sha>:write_failed`、`manifests:unreadable`、`data_root:unreadable`），唔會 traceback，亦唔會當成 `empty`。`validate-contract` 照舊。
- **Data root**：一定要已經存在、列得出內容，唔可以喺呢個 repo 入面（按檔案身份比較，所以經 `/System/Volumes/Data` firmlink 或者大細楷唔同嘅寫法都認得出），亦唔可以喺任何 Git worktree 入面（佢自己或者任何上層目錄有 `.git` 檔、目錄，或者懸空嘅 `.git` symlink，就拒絕；所以 lab 機嘅 home 如果本身係一個 Git repo，`~/betangel-data` 都會被拒）。Data root 底下任何一層（`raw/`、`manifests/`、`index/` 或者更深）有 `.git` 都拒絕（`unsafe_target`）：licensed bytes 唔會寫入任何 Git worktree。Layout：`raw/<archive sha256>/<member 路徑>`（同 TAR 入面一模一樣嘅 bz2 bytes，永遠唔解壓落 disk）、`manifests/<archive sha256>.json`、`index/<archive sha256>/head-v1.json` 同 `markets-v1.jsonl`。
- **信任假設**：data root 同佢所有上層目錄只可以由 operator 寫，而且同一時間只跑一個工具。路徑係先查（`safe_path`）後按名開；冇 `os`（`dir_fd`、`O_NOFOLLOW` 都唔喺 allowlist），另一個 process 可以喺中間將查過嘅目錄換成 symlink（review 試過，咁樣寫得出 data root 以外）。每次 rename 之後會再查一次：換咗就拒絕，但已經寫出去嘅檔收唔返。
- **Ingest 點樣安全**：TAR 讀兩次（`tarfile` 嘅 `"r|"` stream，唔 seek，從來唔 call extract）：第一次 hash 成個檔、查晒每個 header、對要留嘅 member 做有界嘅 bz2 解壓檢查（用 `BZ2Decompressor` 嘅 `max_length`，最多 hold 1 MiB，唔寫解壓資料）；第二次先寫，邊寫邊再 hash，有任何唔同（包括 TAR 中途唔見咗）就拒絕。以下任何一樣都會拒絕成個 archive，乜都唔寫：
  - 名同類型：絕對路徑（包括 `C:` 呢類 drive letter）、`..`、唔係 ASCII 字母、數字同 `._-` 嘅名（亦唔准 `.` 開頭，所以 macOS `tar` 加嘅 AppleDouble `._*` member 會被拒）、多過 8 層、symlink、hardlink、device、FIFO、其他非 regular member（contiguous、sparse）、重複名、淨係大細楷唔同嘅名、檔名撞另一個 member 嘅目錄。
  - 大細：member 太多、單個或者總大細超過上限、header 宣稱嘅大細超出檔案結尾（喺 tarfile 跳去下一個 header 之前查：review 量過，一個 2 KiB、宣稱 2^60 bytes 嘅檔以前會令 ingest 行幾年）。
  - Extended header（喺 tarfile 讀佢之前，經 `TarInfo._proc_member` 呢個 tarfile 自己註明可以 override 嘅 hook 查）：pax 或者 GNU long name header 超過 64 KiB（tarfile 會成個讀入記憶體先；review 量過 256 MiB 嘅 header 用到 1.4 GiB RSS，而家 25 MiB）、一個 member 前面連住多過 4 個 extended header（tarfile 每個遞迴一次，5,000 個會 `RecursionError`）、任何 pax global header（佢會靜靜改後面所有 member 嘅讀法）、舊式 GNU sparse header（截斷咗嘅話 tarfile 自己會 `IndexError`）。
  - **Pax GNU sparse（R4）**：任何版本（0.0、0.1、1.0，或者其他 major／minor）、任何 `GNU.sparse.*` key（`major`、`minor`、`realsize`、`map`、`size`、`name`、`numblocks`、`offset`、`numbytes`）都報 `<名>:not_regular`，喺 tarfile 用佢之前拒絕。原因：tarfile 處理 pax header 時，攞咗下一個 header 之後、`_scan` 任何檢查之前，就會 call sparse hook（`_proc_gnusparse_00`／`_01`／`_10`）；1.0 版會由 member data 讀成個 sparse map，冇上限（Astra 嘅例子：`max_member_bytes` 1 KiB，tarfile 照讀 200,192 bytes、砌 50,000 個 sparse entry 先至被拒）。而家 `BoundedTarInfo` override 咗呢三個 hook（直接拒絕，唔行 stdlib 嗰個）同 `_apply_pax_info`（其他 `GNU.sparse.*` key 改得到 member 嘅名同大細，喺嗰度拒絕）。測試包住 stdlib 三個 hook：普通 tarfile 讀同一個 TAR 會行 `_proc_gnusparse_10`、砌 50,000 個 entry（證明個 probe 有效），ingest 一個 hook 都冇行過。`GNU.sparse.*` 唔分大細楷都拒絕（stdlib 只認準確嘅大細楷，所以細楷嗰啲本身冇害，照拒）。libarchive 嘅普通 pax key（`atime`、`ctime`、`mtime`、`LIBARCHIVE.creationtime`）照收，真容器格式嘅 regression test 照過。呢四個 override 係 tarfile 嘅 private method（見「證明唔到嘅嘢」）。
  - **TAR 冇乾淨咁結尾**：最後一個 member 之後只可以有 zero bytes，而且最少兩個 512-byte zero block（POSIX 嘅寫法；Python `tarfile`、bsdtar、GNU tar、Go 都係咁寫）。冇呢條，喺 member 邊界截斷、第二個 header checksum 壞咗、member 之後有垃圾、中間插咗 zero block、兩個 TAR 駁埋、或者 pax `size=-1`（tarfile 會吞咗自己個錯），都會當完整 archive 收，少咗 member 都冇人知（review 逐個重現過）。
  - Member 內容：空、唔係 bz2、截斷、最後一個 bz2 stream 後面有多餘 bytes、解壓後超過上限或者超過壓縮大細嘅 `max_ratio` 倍（bz2 bomb）。名或者月份被拒嘅 member 從來唔解壓。
  - **成個 archive 嘅解壓量（R3）**：`max_total_decompressed_bytes` 計晒每個解壓檢查過嘅 member 用咗嘅量，**失敗嘅都計**（以前淨係計成功嘅：Astra 嘅例子，三個 2 MiB random member、上限 1 MiB，每個失敗 member 都由成個上限重新開始，總共解壓咗 5,399,886 bytes，member 越多越多）。上限用完就唔再解壓任何 member（header 照查、檔照 hash，所以其他問題照列），所以一次 ingest 嘅解壓量最多係上限加一個 1 MiB chunk，唔理有幾多個 member 失敗。
    - **libbz2 丟咗嘅輸出都計**：libbz2 解完成個 block 先查佢嘅 CRC，查唔過 `BZ2Decompressor.decompress` 就 raise，嗰次 call 已經解咗嘅資料（最多佢嘅 `max_length`，即 1 MiB）就咁丟咗，`_decompressed` 一個 byte 都冇 yield，所以以前呢類 member 完全唔計。Verification 嘅例子：1,000 個 48-byte、block CRC 壞咗嘅 member（`bz2.compress` 1,000,000 個零，改一個 CRC byte；每個解晒 1,000,000 bytes 先失敗），上限 1 MiB，實際解咗大約 1 GB（約 950 倍），0.5 秒；換成 BWT 重嘅 block，每個失敗 member 大約 3.7 ms CPU，冇上限限住。而家失敗嘅 call 當用咗成個 chunk（1 MiB，佢最多可能解咗咁多）：同一個例子只有 2 次失敗嘅 call、上界 2,097,152 bytes（等於上限加一個 chunk），之後嘅 member 唔再解壓，0.02 秒。代價係一個開頭已經唔係 bz2 嘅 member 都當用咗 1 MiB：成個 archive 反正都會被拒，呢個只係令佢早啲停止解壓。
    - **測試量嘅係總工作量，唔止最後 status**：喺 `BZ2Decompressor` 度數，成功嘅 call 數佢嘅輸出，raise 嘅 call 數佢嘅 `max_length`（yield 出嚟嘅 bytes 睇唔到丟咗嘅輸出）。Astra 嘅例子而家係 1,799,962 bytes（上限加一個 chunk 係 2,097,152）；三個喺最後一個 bz2 block 截斷嘅 member（每個解壓大約 1.8 MB 先報 `truncated_bz2`）、上限 2 MiB，亦都喺上限加一個 chunk 之內；40 個 CRC 壞咗嘅 member、上限 1 MiB：只有頭兩個失敗 call，之後唔再解壓。
- **上限**（`archive.LIMIT_CEILINGS`，每個都寫咗理由，測試釘死咗數值；`Limits.stricter` 只可以收緊，等於上限嘅值照收）：TAR 64 GiB；400,000 個 member（連目錄）；單個 member 壓縮 1 GiB、解壓 8 GiB；成個 archive 解壓 1 TiB；壓縮比 200（sample 最高 20.2；20 MiB 嘅零壓縮後得 49 bytes）；路徑 200 字元、最多 8 層；一行 16 MiB（sample 最長一行 59,418 bytes）；extended header 64 KiB、最多連住 4 個。
- **月份把關**（用 `contract.py` 嘅常數；月份一律係 UTC 曆月，§3）：月份只由路徑嚟。2026-01..08（`month_sealed_holdout`）、2026-09（`month_exposed_exploratory_only`）、2026-10（`month_not_prospective`）、之後任何月份（`month_prospective_out_of_scope`），同埋路徑解唔到月份（`no_parseable_month`、`invalid_date_in_path`）或者檔名唔係 market／event id（`unrecognised_file_name`）嘅 member：只 hash，唔解壓、唔寫，記喺 manifest 嘅 `refused`（連原因同準確嘅 sha256、大細），archive 其餘部分照樣 ingest。
  - **第二重：內容。** 留低嘅 member 解壓檢查時會掃 `marketTime` 值（UTC 月份；parse 唔到就用佢開頭嘅 `YYYY-MM`）：任何一個屬於上面嘅拒絕類別，成個 member 都拒絕（`content_month_<原因>`，manifest 記低 `content_months`），唔理路徑寫乜，亦唔理嗰個值係唔係決定時間之後先改。即係 review 嘅 E5：12 月 31 日嘅賽事開跑之後改期到 1 月 1 日，佢嘅 market 檔同成個 event 檔（連同一個 meeting 冇受影響嘅賽事）都被拒。呢個方向保護 holdout，但係用決定時間之後嘅資訊剔走 population：task (c) 要報告呢啲被拒 member（只會喺 2025-12／2026-01 交界出現）。
  - **Chunk 邊界（N1）**：解壓係逐個 1 MiB chunk 掃。一個 `marketTime` 值俾 chunk 邊界切開（例如 `2026-01-01T00:00:00+01:00` 喺 `2026-01` 之後切），以前會即刻記低字面月份 2026-01，下一個 chunk 再記正確嘅 UTC 2025-12，於是錯拒做 `content_month_sealed_holdout`（保守方向，唔係 holdout 漏出）。而家切開嘅值（個 match 喺 window 最後 128 bytes 開始嘅）會帶去下一個 chunk 讀完先計，只記 2025-12；member 本身喺值中間完咗，或者個 match（由 `"marketTime"` 計起）開始得太早帶唔過去，先用字面月份（保守方向，只會錯拒）。測試逐個位置切一刀、喺值入面逐對位置切兩刀，全部只得 2025-12；128 bytes 嘅邊界亦釘死（啱啱好 128 bytes 嘅 match 帶得過去，多一個 byte 就用字面月份；呢個 guard 以前冇測試，拎走佢 holdout 月份嘅 raw bytes 會照寫）。**限制**：key 同值之間嘅空白多到個 match 長過 128 bytes（喺月份之後切，大約 90 個空白以上就用字面月份；喺月份寫完之前切，大約 110 個以上就搵唔到），兩個 chunk 都搵唔到嘅值同下面嘅 escape 一樣漏咗，由 index 把關。
  - **Content scan 係 best effort**：佢只掃冇 escape 嘅 bytes，JSON escape（例如 `"2026-03…"` 或者 `"marketTime"`）同上面講嘅長空白加啱位切開都過到，raw bytes 會照寫；正式嘅把關係 index 嗰度 parse 咗嘅 `marketTime`，嗰個 market 會入 `refused_markets`，唔會有 record。Betfair 嘅檔冇見過 escape 數字，要人手砌先會咁。
  - 2017-04（`INSPECTED_MONTHS`）照收，路徑或者內容係呢個月都標 `inspected_month`。
  - **報告。** `sealed_holdout_members` 唔係 0，即係有人下載咗 holdout 嘅檔：冇儲存，但要按 §3 記低。有 member 喺知道月份之前已經被拒（上面三種解唔到嘅），`month_unknown_members` 就唔係 0，而 `sealed_holdout_members` 係 null：唔知，唔係 0。一個 member 都冇留低嘅 archive 會被拒（`archive:nothing_kept`，exit 2，照樣報 archive sha256 同所有計數），唔寫 manifest，例如成個 TAR 都係 API 嗰種冇日期嘅 layout。
- **Manifest**：`schema_version` 1，canonical JSON（同 contract 一樣嘅 bytes 規則）：archive 名、大細、sha256、plan（多過一個 tier 就係 `mixed`）、layout、month gate、limits、每個 member（路徑、大細、sha256、解壓大細、plan、路徑嘅月份同日期、event id、market id、檔案類型、`content_months`、`inspected_month`）、被拒 member（路徑、大細、sha256、原因、路徑月份或者 null、內容被拒嘅就連 `content_months`）、目錄；所有 list 排好序。同一個 archive 再 ingest：manifest bytes 一樣、raw 檔逐個再 hash（唔見咗嘅補返）→ `already_ingested`；改咗檔名或者 limits 會令 manifest 唔同 → `would_change` 拒絕；gate 改咗 → `gate_changed` 拒絕；raw 檔 hash 唔同（就算大細一樣）→ `existing_hash_mismatch` 拒絕，永遠唔覆蓋。寫檔用 `.partial`（exclusive create）再 rename；`.partial` 留低（中斷或者同時跑）就拒絕，要人手清。Raw 檔同 `manifests/<sha>.json.partial` 嘅 `.partial`（懸空嘅 symlink 都算）喺寫任何 raw 檔之前一齊查，全部列晒（N2：以前 manifest 嗰個要等 raw 抄完、寫 manifest 先發現，raw 已經寫咗；懸空 symlink 以前過到檢查，去到 exclusive create 先失敗，前面嘅 raw 檔已經寫咗）。就算 archive 已經完整 ingest 咗、manifest 一模一樣（冇 partial 會係 `already_ingested`），留低嘅 manifest `.partial` 照樣拒絕；manifest 會變嘅話先報 `would_change`／`gate_changed`，連埋 manifest 嗰個 `.partial`，raw 嘅 `.partial` 就未查。冇 fsync（`os` 唔喺 allowlist），crash 之後可能有唔完整嘅檔：下次 ingest 或者 index 會因為 hash 唔夾而拒絕，唔會信佢。
- **Index**：每個 (plan, market id) 一行 canonical JSON，按 plan 同 market id 排；唔同 tier 永遠唔 merge。每行有分開嘅 top-level object：`provenance`、`checks`、`coverage`、`decision_time`、`evaluator_only_outcome`。**只有 `decision_time` 可以畀策略睇**，其他全部係 provenance、audit 或者 evaluator 用：
  - **`decision_time`**：只有 cutoff 之前公佈嘅事實，每個連 `pt`：第一次公佈嘅 `marketTime`、definition 欄位嘅 timeline（`marketTime` 同佢開跑前嘅修改、status、`inPlay`、宣佈嘅 `numberOfWinners`、`marketBaseRate`、`bspMarket`、`version` 等等）、runner（`sortPriority`／`name`／status／`adjustmentFactor`／`removalDate`／listed）、removal（REMOVED 或 REMOVED_VACANT，連當時嘅 `removalDate` 同 `adjustmentFactor`），同 `view_ends`（`{pt, reason}`，即係 cutoff 本身）。Cutoff 係以下最早嗰個：第一次公佈嘅 `marketTime`、第一個帶結果嘅 market definition（CLOSED、`settledTime`、`bspReconciled` true、任何 WINNER／LOSER／PLACED、任何 runner `bsp`）、同埋（member 讀唔完嘅話）`complete_before_pt`。所以入面一定冇結果，亦冇開跑時間或之後公佈嘅嘢。**之前「v1 決定時間仲早，所以照住 `pt` 篩就得」呢句唔啱**（review 嘅 E6：開跑前 20 分鐘 CLOSED、全部 runner REMOVED 嘅 market，篩完仲係 OPEN、ACTIVE）：`view_ends` 同其他 entry 一樣有 `pt`，consumer 只留 `pt` ≤ 決定時間嘅 entry；**決定時間 ≥ `view_ends.pt`，即係呢個 view 唔完整**（market 已經有帶結果嘅 definition、到咗開跑時間、或者 member 讀唔落去），task (c) 要畀明確嘅 abstain 或者 unsupported 原因，唔可以用呢個截咗嘅 view 模擬，亦唔可以剔走（用邊個原因，§13）。
  - **冇 view（R1 再 review）**：primary 喺 cutoff 之前冇公佈過呢個 market 嘅 `marketTime`（parse 得到嘅），即係佢完全冇呢個 market（淨係 heartbeat、淨係其他 market、或者喺任何佢嘅 message 之前已經讀唔落去）、冇 `marketTime` 或者 parse 唔到、第一次公佈喺佢講嘅開跑時間或之後、第一次公佈就係帶結果嘅 definition、或者喺嗰之前已經讀唔落去：`decision_time` 一律係**同一個明確嘅空 view**：冇 definition、runner、removal，`first_published_market_time` null，`view_ends` `{pt: null, reason: primary_source_no_observation_before_cutoff}`。以前呢幾種各有唔同嘅 `view_ends`（null、開跑時間、`complete_before_pt`），之後先出嘅嘢可以將一種變成另一種：Astra 嘅例子入面，M 開跑後先出嘅第一個 definition 會令佢由冇 record 變成 `view_ends` 係開跑時間。點解冇 view 仍然記喺 evaluator 嗰邊嘅 `decision_cutoff`（例如 `no_first_published_market_time`，或者 `member_unreadable_from_pt` 連 `complete_before_pt`）。空 view 冇決定時間（primary 冇第一次公佈嘅 `marketTime` 做 anchor），而且**有冇呢個 record 本身唔係 prefix-invariant**：開跑前冇任何 member 提過嘅 market，要有 member 之後先提到佢先有 record（verification 嘅 scratch sweep：開跑後加嘢令 92 個空 view record 出現、截走令 64 個消失，全部 `decision_time` 都係空 view，冇一個改到有 view 嘅 market 或者佢嘅 primary）。所以 task (c) 唔可以將佢存唔存在當決定時間嘅事實：呢啲 market 要明確報告、畀 unsupported 原因，唔可以靜靜剔走，但唔入任何決定時間嘅 population，亦唔入 prefix-invariance 嘅比較；或者將每個綁去任何 member 第一次提到呢個 market 嘅 `pt`（index v1 冇記呢個值）。用邊個做法，§13。
  - **Image（R2，§5 `img_replaces_market_state`）**：`img` true 嘅 market change 喺佢個 `pt` 取代成個已公佈嘅 market state，唔係 merge。佢有 definition 就照常成個套用；image 冇帶嘅嘢（冇 definition 就係所有 definition 欄位、結算欄位同所有 runner；有 definition 就係佢冇列嘅 runner）喺 image 嘅 `pt` 記一個 null（runner `listed` false），即係由嗰刻起唔再係 current，之前公佈過嘅 history 照留（timeline 唔刪）。以前 image 淨係記喺 coverage（`mc.img`），state 照舊：Astra 嘅例子，開跑前 10 分鐘 OPEN、兩隻 ACTIVE，開跑前 6 分鐘一個冇 definition 嘅 image，`decision_time` 同 `img` false 一模一樣。普通（非 image）definition 冇列一隻 runner，照舊淨係記 `listed` false（佢嘅 status timeline 唔變，即係仲係 ACTIVE），所以 task (c) 揀 cohort 嘅 runner 要決定時間嘅 status ACTIVE **而且** `listed` true（§6）。`img` 唔係 boolean（亦唔係 null）就當讀唔落去（`malformed_image_flag`），唔估係 merge 定 replace。Image reset 加嘅 null 唔當 removal（REMOVED → null → REMOVED 仍然係一個 removal），亦唔當 `marketTime` 修改（audit 只計改做另一個值）。
  - **讀唔完嘅 member**：每個 message 成個檢查過先套用；讀唔落去嘅 member（JSON 錯、`pt` 倒退或者唔係整數、segmented、行太長、太大、NaN、`1e999` 呢類溢出做 infinity 嘅數、形狀唔啱、runner id 重複）記做 unreadable，連 `complete_before_pt`：壞嗰行讀到 `pt` 先失敗（segmented、形狀唔啱、market id 唔啱、`img` 唔係 boolean、runner id 重複：呢啲都喺 `pt` 查完先查）就係嗰行嘅 `pt`，因為 `pt` 唔會倒退，佢之前公佈嘅 message 全部套用咗；讀唔到 `pt`（JSON 錯、NaN、行太長、喺行中間截斷）就係最後一個套用咗嘅 message 嘅 `pt`。兩樣都係只信 `complete_before_pt` 之前公佈嘅（壞嗰行可能屬於同一個 `pt` 嗰批）。之前見過嘅 market 照樣入 index，只用 `complete_before_pt` 之前嘅資料，`read_complete` false：所以 cutoff 之後先公佈嘅壞資料唔會令成個 meeting 嘅 market 消失（review 嘅 E4），亦唔會改到佢哋嘅 `decision_time`；壞喺 cutoff 之前，`decision_time` 就喺 `complete_before_pt` 完（`view_ends.reason` `member_unreadable_from_pt`），就算另一個來源（例如 E 檔）讀得完都唔會換 primary（R1，見下面 `other_sources`）。Task (c) 要將 `read_complete` false 嘅 market 結算當 unresolved（之後可能有改）。
    - **讀得到 `pt` 嘅壞行（R1 verification）**：以前一律當讀唔到，用最後套用咗嘅 `pt`：off 前 10 分鐘最後一個 definition，跟住 off 後 10 分鐘一行形狀唔啱（或者 `img` 1、segmented、market id 唔啱、runner id 重複）嘅 message，`complete_before_pt` 係 off 前 10 分鐘，嗰批被截走，off 前 5 分鐘嘅決定就冇 view；中間有個 off 後嘅 heartbeat 就冇事。即係決定時間之後嘅失敗改咗之前嘅決定，同 Astra 嘅 R1 同一類（R2 修正令 off 後一個 `img` 1 由靜靜 merge 變成呢種失敗）。而家呢五種都同完整檔嘅 `decision_time` 一樣；同樣嘅壞行喺 off 前 5 分鐘，view 就喺 off 前 5 分鐘完，off 前 10 分鐘嗰批照留。
    - **讀唔到 `pt` 嘅壞行係例外**：佢屬於邊個時間唔知，所以 view 喺最後套用咗嘅 `pt` 完，就算佢實際上喺決定時間之後（例如喺決定時間之後第一行中間截斷）。修正後喺 scratch 量（一個 synthetic market，有冇 image 兩款，四個決定時間，決定時間之後每個行邊界加 250 個隨機位置，共 2,192 個截斷）：截喺行邊界嘅冇一個改到決定時間嘅 view；改到嘅 43 個全部截喺決定時間之後第一行入面。
    - **第一批都截咗**：連第一次公佈嘅 `marketTime` 都唔喺 `complete_before_pt` 之前（例如截喺第一批入面），`decision_time` 係上面嘅明確空 view（以前係 `{complete_before_pt, member_unreadable_from_pt}`，再之前係 null，好似個 market 冇 `marketTime`）；`decision_cutoff` 照寫 `{complete_before_pt, member_unreadable_from_pt}`。冇 `marketTime` 嘅 definition 照舊唔入 `decision_time`：讀唔讀得完都係空 view。
  - **`evaluator_only_outcome`**：cutoff 同原因、`read_complete`、cutoff 之後嘅 removal 同修訂、`bspReconciled`／`settledTime` 同 runner `bsp` 嘅 timeline（原值）、最終 runner status、最終 BSP（只有 ≥ 1.01 嘅有限數字先算，否則 null；PRO sample 嘅 rc 有 `"NaN"`／`"Infinity"` 字串，§2.8；兩樣都只計最後仲 `listed` 嘅 runner，見下面）、`unusable_final_bsp_runner_ids`（有值但唔用得）、缺最終 BSP 嘅 runner（最終 status 唔係 REMOVED、REMOVED_VACANT 或者 HIDDEN）、有冇 CLOSED、settled time（冇 definition 嘅 image 之後係 null）、贏家數目（最後嘅 state 冇結果就係 null，唔係 0：即係最後 status 唔係 CLOSED，而最後仲 `listed` 嘅 runner 又冇 WINNER／LOSER／PLACED；以前睇 history，CLOSED 之後被 image 清空或者重開嘅 market 都報 0）、宣佈嘅 `numberOfWinners`、`dead_heat`（最後 status 唔係 CLOSED 就係 null；以前睇「CLOSED 過」，重開咗嘅 market 報 false）、`results_revised_after_close`（CLOSED 之後 runner status、`bsp`、runner 喺 definition 出現或者消失、或者 `numberOfWinners` 再改；未 CLOSED 就 null）。**只有最後仲 `listed` 嘅 runner（喺最後一個 definition 入面，之後亦冇俾 image 清空）先係而家嘅結果（R2）**：image 或者之後嘅 definition 冇再列嘅 runner，最終 status 同 BSP 都係 null（`listed` false），佢舊嘅 WINNER／BSP 只留喺 timeline，唔入贏家數目、`dead_heat`、缺 BSP 或者 BSP 唔用得。Astra 嘅例子：CLOSED definition 寫 runner 1 WINNER／BSP 2、runner 2 LOSER／BSP 3，之後一個 image definition 淨係有 runner 2 WINNER／BSP 3；以前 runner 1 仲係 WINNER，贏家 2 個、`dead_heat` true、audit 計一個 dead heat；而家贏家 1 個、`dead_heat` false、`results_revised_after_close` true（結算 unresolved）。**Dead heat 按 market type**：EACH_WAY 嘅 `numberOfWinners` 係名次數，結算係 1 個 WINNER 加其餘 PLACED，所以 WINNER 多過 1 個或者 WINNER + PLACED 多過宣佈數就係 dead heat；WIN、PLACE、OTHER_PLACE 係 WINNER 多過宣佈數；宣佈數 < 1（flumine 3.2.7 當 0 係「贏家數目唔定」）、冇宣佈、其他 type 或者最後 status 唔係 CLOSED：null（判斷唔到）。
  - **`checks`**：路徑月份、第一次公佈嘅 `marketTime` 嘅 UTC 月份、`month_mismatch`、`date_mismatch`（路徑日期 vs 第一次公佈嘅 `marketTime` 嘅 UTC 日期）、`event_id_mismatch`（路徑 event id vs 第一個 definition 嘅 `eventId`）、market 檔有冇裝住另一個 market、`untracked_definition_fields`（definition 入面唔喺 allowlist 嘅 key：只有名入 coverage，值唔入 `decision_time`；allowlist 仍然係防漏）、`unrecognised_runner_statuses`（spec 冇列嘅 status，唔會靜靜歸類）。第一個真 TAR 就靠 `date_mismatch` 同 `event_id_mismatch` 驗證路徑 layout 嘅意思。
  - **`coverage`（只畀 audit 用，同結果有關）**：`whole_file` 係成個 member 出現過嘅欄位名（所以 `md.runners.bsp`、`md.settledTime` 有冇，等於話你知有冇結果）；`before_cutoff` 係 cutoff 之前已經出現嘅；`valued_whole_file`／`valued_before_cutoff` 只計有用得嘅值（`rc.ltp` > 0、有限數字嘅 `rc.spn`／`rc.spf`、≥ 1.01 嘅 `bsp`）。§2.3 嘅 decision capability 要睇 before-cutoff 嗰兩個。
  - **Primary 來源同 `other_sources`（只畀 audit 用）**：同一個 market 喺 M 檔同 E 檔都有，primary（決定用嘅來源）**按 member 存唔存在同佢嘅路徑揀；冇以呢個 market 命名嘅 M 檔，就再睇邊個 member 最早提到呢個 market**，唔睇之後講乜、最後有冇呢個 market 嘅 record、讀唔讀得完，因為三樣都可以由決定時間之後先出嘅嘢決定：manifest 有以呢個 market 命名嘅 M 檔（留低定被拒都算：被拒可以係因為內容），佢就係 primary（多過一個就按路徑第一個，佢有冇呢個 market、讀唔讀得完都係），唔理入面有乜；冇嘅話，搵第一個提到呢個 market 嘅 message 最早（`pt` 最細，同 `pt` 就按路徑）嘅 member（佢呢個 market 嘅 record 俾 month gate 拒咗都計），primary 係嗰個 member 目錄入面嘅 E 檔（manifest 有就算，留低定被拒都係，佢有冇提過呢個 market 都係），冇 E 檔就係嗰個 member 本身（以其他 market 命名嘅 M 檔）。Primary 讀到（成個或者部分）但冇呢個 market 嘅 record，record 就係 primary 嘅 provenance、上面嘅空 view 同乜都未結算嘅 outcome（冇 timeline、冇最終 runner、`final_status`、`winners_observed`、`dead_heat` 全部 null，唔係 0；`closed_reached` false）。Primary 被拒（ingest 時按路徑或者內容月份、index 時按路徑月份，或者佢入面呢個 market 嘅 `marketTime` 月份），成個 market 冇 record：其他來源嘅 record 唔會頂上，逐個記入 head 嘅 `refused_markets`，原因 `primary_source_refused`。其他來源記低 `read_complete`、`agrees`（成個 record 比，包括結果，所以係結果衍生嘅）同 `agrees_decision_time`（淨係比 `decision_time`）；同名但冇呢個 market 嘅 record 嘅另一個 M 檔都會列，兩樣都係 null（audit 當唔夾）。`other_sources` 同 `read_complete` 永遠唔係 decision input，cohort、abstain 原因同 intent 都唔可以睇佢哋。
    - **R1 嘅原例子**：M 同 E 喺開跑前 10 分鐘唔夾（M：runner 1 ACTIVE；E：REMOVED），兩個都喺開跑後 10 分鐘有一個 heartbeat；M 喺 heartbeat 之後多一行讀唔到嘅資料，以前 primary 就由 M 變 E（讀得完嘅排第一），`decision_time` 由 ACTIVE 變成開跑前已 REMOVED。上一輪改成按名揀「有 record 嘅來源」，兩個 TAR 已經一樣（M 嘅），淨係結算嗰邊 `read_complete` 唔同。每個來源自己嘅 view 喺佢讀唔落去嘅地方完，唔會用另一個來源補。
    - **R1 再 review（Astra 第二輪）**：「有 record 嘅來源」仍然唔得：一個來源*會唔會*有 record 係將來嘅資訊。Astra 嘅例子（兩個檔都讀得完；OFF = 1433167200000，market 1.100000001，event 27000001）：M 淨係 heartbeat，E 喺開跑前 10 分鐘有正常 definition（GB、WIN、OPEN、BSP、兩隻 ACTIVE）、開跑後一個 heartbeat；第二個 TAR 淨係喺 M 加一個開跑後 10 分鐘（root 嘅版本）或者 20 分鐘（另一個 reviewer 嘅版本）先出嘅第一個 definition，同 E 嘅一樣。以前第一個 TAR 嘅 primary 係 E（第一次公佈嘅 `marketTime`、兩隻 ACTIVE），第二個變 M（乜都冇）：開跑後先出嘅 message 抹走咗之前嘅 view，task (c) 喺 `decision_time` 篩都救唔返，因為 E 嘅 view 已經唔喺 record。而家 M 存在，兩個 TAR 嘅 primary 都係 M，`decision_time` 都係同一個空 view；E 淨係列喺 `other_sources`（讀得完、`agrees_decision_time` false）。
    - **以前兩個例外冇咗**：M 喺任何呢個 market 嘅 message 之前已經讀唔落去，以前 primary 係 E，而家係 M（空 view）。M 被 month gate 拒、E 嘅月份過到（例如淨係 M 有一個開跑後改去 2026-02 嘅 `marketTime`），以前 E 嘅 record 照入 index（一個 M 話喺 holdout 嘅 market 經 E 留低），而家成個 market 冇 record，E 嗰個記做 `primary_source_refused`（ingest 時內容被拒同 index 時 `marketTime` 被拒都測咗）。代價：M 同 E 唔夾嘅時候，以前用 E 嘅 market 而家冇 view 或者冇 record；Betfair 嘅 M 同 E 應該一樣（未用真資料核實）。Record 仍然要有讀咗嘅 member 見到呢個 market，**而且最少一個 member 嘅 record 過到 month gate** 先有：完全讀唔到、又冇其他來源見到呢個 market 嘅 M，照舊冇 record（只喺 head 嘅 `files` 列做 unreadable）；見到佢嘅 member 嘅 record 全部俾 `marketTime` 月份拒咗（例如 M 淨係 heartbeat，E 開跑後將佢改去 2026-02，key escape 咗令 ingest 掃唔到），亦冇 record，連 M 嘅空 view 都冇（否則一個 holdout 嘅 market 會經空 view 入 index），`refused_markets` 淨係有 E 嗰個 `market_time_month_sealed_holdout`。
    - **實際上 M 一定係 primary**：Yu 下載嘅真 BASIC TAR 淨係有 M 檔（§2.8：52,561 個 member 全部係 `BASIC/<YYYY>/<Mon>/<D>/<eventId>/<marketId>.bz2`，冇 E 檔）。只要每個 M 檔裝嘅係佢自己個 market（淨係睇過檔名，未讀內容核實），每個 market 嘅 primary 就係佢自己個檔。E 同目錄規則淨係用喺淨係 E 檔嘅 TAR、M 加 E 嘅 TAR，或者 M 檔裝住其他 market 嘅時候。
    - **最早提到（R1 verification）**：上一版冇以呢個 market 命名嘅 M 檔時，揀「見到呢個 market 嘅任何 event 目錄入面按路徑第一個 E 檔，冇就見到佢嘅第一個 member」，任何時間見到都算，所以開跑後先提到或者截走一行照樣換來源（verification 經 public `ingest_archive` → `index_archives` 重現，全部檔讀得完，OFF = 1433167200000）：A1，淨係 E 檔嘅 TAR（三個 sample TAR 都係呢個 layout），E1（`…/27000002/27000002.bz2`）開跑前 10 分鐘有呢個 market（兩隻 ACTIVE），按路徑排先嘅 E0（`…/27000001/27000001.bz2`，另一個 market）開跑後 30 分鐘先加呢個 market 嘅 definition，primary 就由 E1（有 view）變 E0（空 view）；A3，冇 E 檔，按路徑排先嘅 `1.100000000.bz2` 開跑後先提到，primary 由 `1.100000002.bz2`（有 view）變佢（空 view）；A5，截斷：E0 開跑後 15 分鐘先提到，primary 係 E0（空 view），喺 E0 開跑後第一行之後截斷，primary 變 E1（有 view）；A4，E0 存在但從來冇提過，E0 目錄入面一個以其他 market 命名嘅 M 檔開跑後先提到，primary 由 E1 變 E0（空 view），兩個 E 檔都冇改過：呢個係上一版對 0db64db 嘅 regression（0db64db 保留 E1）。而家揀第一個提到呢個 market 嘅 message 最早嘅 member（同埋佢目錄嘅 E 檔）：任何時間 `t` 之後加一行淨係加 `t` 之後嘅提及，截走亦淨係刪 `t` 之後嘅提及，所以 `t` 或之前最早嗰個唔會變；gate 拒咗嘅 record 都計，否則開跑後一個改去 holdout 月份嘅 `marketTime` 會將來源換去下一個 member。四個例子而家 primary 同 `decision_time` 都唔變；以呢個 market 命名嘅 M 檔嗰條規則冇改。
    - **剩低嘅限制**：（1）開跑前冇任何 member 提過嘅 market，primary 由一個開跑後嘅提及揀，開跑後再加一個更早（但仍然喺開跑後）嘅提及、或者截走嗰個，primary 仍然會換。兩邊喺決定時間都冇 view（空 view；加嘅 `marketTime` 遲過開跑時間就係之後一個新決定），所以冇決定改變，但 record 嘅 provenance 會變；record 存唔存在亦唔係 prefix-invariant（上面「冇 view」）。（2）Ingest 時因為內容月份被拒嘅 member 冇讀過，佢提過乜唔知：佢係最早提到嘅話，開跑後令佢被 content gate 拒嘅一行可以換來源，或者令成個 market 因為 primary 被拒而冇 record；content gate 本身已經係用決定時間之後嘅資訊剔走 member，只會喺 2025-12／2026-01 交界出現（見上面）。（3）邊啲 member 存在係下載嗰陣（portal 嘅 File Type filter）定嘅，唔係 stream 定：同一個 market 喺 M 加 E 嘅 TAR 同淨係 E 嘅 TAR 可以有唔同 view（M 同 E 唔夾先會）。真 BASIC TAR 淨係有以自己 market 命名嘅 M 檔（上面），呢三樣喺嗰度都唔會發生，要淨係 E 檔或者 M 檔裝住其他 market 先會。Market id 淨係前面多咗 0 唔同（例如 `1.0100000001` 同 `1.100000001`）而家係兩個 record，以前會 merge 埋一齊。
    - **測試**：`PrimarySourceTests` 十三個 test。上一輪嘅七個：Astra 嘅例子兩個版本（經 public `ingest_archive` → `index_archives`，兩個 TAR 嘅 `decision_time` 一樣、都係 M 嘅空 view）；M 開跑前有 record、E 唔夾，E 開跑後加結果、改 `marketTime`、image、壞行或者截短都唔改 M 嘅 view；冇 M、E 有 record，E 開跑後加嘢都唔改 view，同目錄另一個 M 檔開跑後先提到呢個 market 亦唔改，反過來淨係另一個 M 檔有、E 冇，primary 係 E（空 view），E 開跑後先加都唔變；primary 被拒就成個 market 冇 record；`other_sources` 按名、E、其他 M 檔排（同名但乜都冇嘅第二個 M 檔列做 null），兩個 event 目錄都有嘅 market 用路徑第一個 E；前面多咗 0 嘅 market id 係兩個 market；同埋下面嘅 property-style loop。今輪加嘅六個：A1、A3、A5（冇以佢命名嘅 M 檔，開跑後加或者截走都唔換最早提到嘅來源、唔改佢開跑前 10 分鐘嘅 view）；A4；兩個以呢個 market 命名嘅 M 檔，按路徑第一個永遠係 primary（佢淨係 heartbeat、開跑後先加 definition、或者開跑後一行讀唔到都係；verification 嘅 mutant M11、M21 揀「讀得完」或者「有呢個 market」嗰個，以前冇 test 殺到）；所有 record 都俾 gate 拒嘅 market 冇 record（M15）；最早提到嘅 member 嘅 record 俾 gate 拒，成個 market 拒絕，唔會交畀之後（而且路徑排先）提到嘅 member；ingest 時被拒嘅 E 檔照係 primary（M08）。Property-style loop：hashlib 定 seed，七個 base TAR，每個 member 都以開跑後嘅行結尾：一個 event 目錄嘅四個（A，Astra 嘅；B，M 有 record 加唔夾嘅 E；C，E 加同目錄另一個 M 檔；D，淨係另一個 M 檔有、E 存在冇提過），兩個 event 目錄、冇以佢命名嘅 M 檔嘅兩個（E，淨係 E 檔，按路徑排先嘅 E0 開跑後先提到、E1 有；F，E0 冇提過、E0 目錄入面一個以其他 market 命名嘅 M 檔，E1 有），同埋 G，冇 E 檔、兩個以其他 market 命名嘅 M 檔（按路徑第二個有）。每個 base 逐個 member 加一個開跑後嘅 definition、同喺佢開跑後第一行之後截斷，再加 24 個隨機 variant（喺嗰點或之後隨機截斷、加隨機開跑後嘅行：definition、結果、image、其他 market、壞行，或者兩樣都做）：`decision_time` 同 primary 一次都冇變。上一輪嘅七個入面六個喺 0db64db fail（M 有 record 嗰個以前已經啱），上一輪改咗預期嘅四個舊 test（空 view 嘅三種寫法、以其他 market 命名嘅 M 檔）亦係；今輪 A1、A3、A5、A4、gate 嗰個同 property loop（E、F、G 三個 base；A–D 上一版已經過）喺上一版（verification 前）fail，A4 喺 0db64db 過（佢正係分得開三個規則嗰個）；兩個同名 M 檔、淨係 gate 拒嘅 record、ingest 被拒嘅 E 三個喺上一版已經過，佢哋釘死嘅係之前冇 test 嘅 guard。
    - **Mutation 同 sweep（scratch，2026-10-14）**：上一輪實作者嘅 14 個 mutant 入面 11 個仲適用（另外 3 個改嘅係已經換走嘅「見到嘅目錄」規則），全部殺咗。Verification 嘅 22 個（佢 `mutate.py` 嘅 20 個加 M21、M22；當中 M06、M09、M10、M20 按新 code 改寫）同今輪 8 個改「最早提到」規則嘅（只睇路徑、揀最遲、唔要 E 檔、上一版嘅目錄規則、用最後一個 message 嘅 `pt`、唔計 gate 拒咗嘅 record、同 `pt` 按路徑倒序、淨係計留低嘅 E 檔），共 30 個，喺修正後嘅 code 全部俾最少一個 test 殺咗（上一版 verification 嘅 M08、M11、M15、M21 冇殺到；今輪第一次跑仲有兩個冇殺到，即係唔計 gate 拒咗嘅 record，加咗 gate 嗰個 test 先殺到）。殺到唔等於冇 bug。Sweep（只係描述呢啲 synthetic layout，唔係證明）：上面七個 base，25 個 seed、每個 base 48 個隨機 variant（8,400 個）：修正後 0 個改到 `decision_time` 或 primary（3,936 個改到 outcome），上一版 726 個，全部喺 E、F、G（A–D 兩版都係 0）。所以上一輪寫嘅「4,800 個 variant、0 個改到」淨係講 A–D 四個單一目錄嘅 base，量唔到多目錄嘅問題。Verification 嘅兩個 sweep 喺修正後再跑：單一目錄 grid（M × E × 同目錄另一個 M 檔，8,015 個 variant）結果同上一版一模一樣；多目錄隨機 sweep（3,096 個 variant）改到 primary 或 `decision_time` 嘅由 135 個（60 個由有 view 變空 view）跌到 24 個，全部係開跑前冇 member 提過嘅 market、兩邊都係空 view、淨係 primary 變（上面限制 1）。截喺開跑後第一行入面係上面讀唔到 `pt` 嘅已知例外，冇抽；加嘅 `marketTime` 都唔遲過開跑時間，因為宣佈一個更遲嘅開跑時間係之後一個新決定，唔係改之前嘅決定。
  - **完整性**：member hash 唔夾或者唔見咗 → 成個 index 拒絕。`head-v1.json` 有 manifest sha256、markets 檔 sha256 同每個檔嘅狀態、`complete_before_pt` 同 market id；`index_sha256` 就係 head 嘅 sha256。已有 index 而 bytes 唔同就拒絕（`index_would_change`；manifest 係舊 gate 寫嘅就報 `gate_changed`）：改意思要開新 schema version 同新檔名。
  - **記憶體**：record 一邊讀一邊寫去 `index/<sha>/markets-v1.jsonl.spill`，之後由 spill merge 出 markets 檔，記憶體只留每個 record 一個 key（spill 留低就拒絕，要人手清）。Review 嘅 synthetic 測試（每個 market 12 隻 runner、20 次 `adjustmentFactor` 修改）：4,000 個 market 嘅 index peak RSS 由 426 MB 跌到 43 MB，1,000 個由 127 MB 跌到 31 MB；而家主要係 manifest 同 head（每個 member 大約 4 KB），推算 400,000 個 member 嘅上限大約 1.5 GB（推算，冇量）。Audit 一行行讀，31–42 MB。
- **Audit**：按 tier × 月份（第一次公佈嘅 `marketTime` 嘅 UTC 月份；冇就係 `unknown`）× country × marketType（都係第一次公佈嘅值），只報整數計數：market 數；成個檔出現過嘅欄位（`fields_present`）、cutoff 之前出現過嘅（`fields_present_before_cutoff`）同 cutoff 之前有用得嘅值嘅（`fields_valued_before_cutoff`），同從來冇出現嘅已知欄位（`fields_absent`、`fields_absent_before_cutoff`：冇就寫冇，唔當 0）；`bspMarket` true／false／冇；缺最終 BSP（全部同 BSP market）、BSP 唔用得；removal（cutoff 前後）；dead heat、CLOSED 但判斷唔到 dead heat、CLOSED 之後改結果；從未 CLOSED（primary 冇呢個 market 嘅 record 嘅 market 都計入：佢 `closed_reached` false、`final_status` null，即係「乜都冇」，唔係真係見到冇 CLOSED；佢亦唔計缺最終 BSP，因為冇最終 runner）；月份、日期、event id 唔夾；`marketTime` 修改（改做另一個值；image reset 記嘅 null 同再公佈同一個值唔計）；讀唔完嘅 member 嘅 market；兩個重疊註記（`markets_never_closed_read_incomplete`：從未 CLOSED 而且讀唔完；`markets_missing_final_bsp_read_incomplete`：缺最終 BSP 而且讀唔完，冇其他條件），係擺喺主計數旁邊嘅子集，唔係另一個分類，唔可以同主計數加埋：每個 market 照計主計數；讀唔讀得完係成個 member 計（event 檔後面一行壞咗，成個檔嘅 market 都標讀唔完）；入咗子集唔代表個缺口係因為讀唔完，亦唔代表之前見到嘅結算資料唔用得；未知 runner status；多個來源、來源唔夾（成個 record 同淨係 `decision_time`；冇呢個 market 嘅 record 嘅 M 檔，`agrees` null，都當唔夾）；仲有讀唔到嘅檔（連 `complete_before_pt` 同仲交到幾多個 market）、被拒 member 同 market、未 index 嘅 archive、每個 archive 嘅 manifest gate 係咪而家嘅（`month_gate_current`）、喺幾個 archive 重複嘅 market（`markets_in_more_than_one_archive` 數 market，每個只計一次，唔理重複咗幾多個 archive；多出嚟嘅副本數 = 各 archive 嘅 market 數加埋減 `totals.markets`）。冇 win rate、冇價錢、冇 BSP 值、冇回報。佢核對 head 同 manifest、markets 檔同 head（hash 同行數），再將而家嘅 month gate 套落每個 record：路徑月份或者 `marketTime` 月份而家被拒嘅，成個 audit 拒絕（`stale_gate`），舊 gate 建嘅 index 唔會被計。唔寫任何嘢。冇最少 group 大細：一個 group 得一個 market，嗰啲計數就等於講晒嗰個 market 嘅結算 coverage（§10）。
- **Boundary**：`BACKTEST_STDLIB` 加咗 `tarfile`，係成個 backtest package 都用得嘅 allowlist；`tests/test_betfair_backtest_archive.py` 用 AST 釘死：成個 `betangel_research` 只有 `archive.py` import `tarfile`，佢唯一嘅 `tarfile.open` 係 `mode="r|"`，冇 extract、建立 archive 或者 filter 呢類 attribute（tarfile 自己 import `pwd`／`grp` 係為咗 extract 同建立 archive，呢度兩樣都冇用）。`__main__` 喺 function 入面先 import 三個新 module，所以 `validate-contract` 照樣只載入 `contract.py`（contract test 冇改）；boundary test 記低嘅 `__main__` import 集合因此加咗呢三個 backtest module。`archive` 同 `market_index` reuse root 嘅 `temporal.timestamp_ms`（乾淨，見上面 reuse 表）；冇用 root `stream.messages`，因為佢按路徑開檔、冇行長同大細上限、冇 hash 檢查，所以另外寫咗一個同規則（blank 行、`op` mcm、`pt` 唔倒退、冇 `segmentType`）嘅有界 reader。
- **證明唔到嘅嘢**：測試全部用測試入面即場砌嘅 synthetic TAR 同 stream；佢哋證明唔到真 TAR 用 §2.8 嘅 layout、以兩個 zero block 結尾、冇 pax global header，亦證明唔到 cutoff 之前嘅 `pt` 就係我哋當時真係收到嘅時間（§5）。Content scan 係 best effort（見上面）；信任假設之外嘅同時寫入防唔到；head 嘅記憶體冇上限，只係推算。Mutation 測試（逐個改一處 code，睇測試有冇 fail；2026-10-14 喺 scratch 副本跑）：review 嘅 163 個 mutant 入面仲適用嘅 135 個殺咗 129 個，剩低 6 個改嘅係重複嘅防線（例如 `stricter` 收 bool 會先俾 `__post_init__` 擋；`decision_safe` 本身係 belt and braces），輸出唔變；修正新加嘅 97 個 mutant 全部殺咗。殺到唔等於冇 bug，只係話呢啲改動測試睇得出。Astra review（R1–R4、N1–N2）嘅修正：每個 regression test 喺修正前嘅 code（35bfe6f）上 fail、修正後過；之後嘅 verification 報告佢跑咗 36 個 mutant，28 個殺咗、2 個等價、6 個係測試缺口。補咗嗰啲測試同今輪修正之後，喺 scratch 副本再跑 57 個 mutant（verification 嗰批 39 個，連今輪 18 個，每個撤銷或者削弱一處修正）：56 個殺咗；剩低嗰個（`content_months` 拎走 `match.end() < end`）係等價，因為個 match 會帶去下一個 window，最後一次 scan 一定計到，20,000 個隨機值同切法比較冇一個唔同。今輪每個新 regression test 都喺今輪修正前嘅 code 上 fail、修正後過。R4 嘅 guard 靠 tarfile 嘅 private、冇文件嘅 method（`_proc_gnusparse_00`／`_01`／`_10` 同 `_apply_pax_info`；`_proc_member` 先係文件寫明可以 override 嘅）：名同 call site 喺 Python 3.14.6 同 3.11.15 核實過，改咗名測試會 fail，但係將來 Python 用新名加一個 sparse hook，測試睇唔到。Image 處理淨係用 synthetic stream 測過，真 PRO sample 嘅 image 全部帶齊 definition 同 runner（§5），所以真資料仲未行過 reset 嗰條路。

## 5. Point-in-time rules

策略喺時間 `t` 嘅 view 只可以有**已經觀察到嘅 prefix**：

- **當時知道嘅 runner**（包括之後退出嘅）、當時公佈嘅 schedule（`marketTime` 可以之後改）同 status、**每個欄位自己嘅時間戳**（最後一次更新嘅 `pt`）、有上限嘅歷史，同 provenance（檔案 sha256、行號）。歷史嘅上限由每個 feature 聲明：`features[].lookback_s`（0..86400 秒）係 feature 喺決定時間嗰陣生效嘅 state 以外，最多可以讀幾多秒之前嘅歷史；0 即係淨係讀嗰陣生效嘅 state（每個欄位最新嘅值同佢自己嘅 `pt`）。P0 兩個 feature 都係 0，`as_of` 都係 `decision_time`。
- **同一個 `pt` 嘅 message 當一批**，按檔案次序處理完先畀策略睇；策略唔會見到半批。（現有 paper model 都係咁：同一時間有幾個 snapshot 就用最後嗰個，[落單路徑 §11](betfair-order-path.md#11-paper-model-assumptions) 第 2 步。）
- **Timer**：策略可以要求喺某個時間醒，例如 P0 嘅「第一次公佈嘅 `marketTime` 前 300 秒」；醒嗰陣嘅 view 係 `pt ≤` 嗰個時間嘅 prefix。Timer 同埋同一個 `pt` 嘅一批 data 撞正同一時間：view 嘅定義已經包咗嗰批，所以 schema v1 只准 `decision_timing.equal_pt_order` `timer_after_equal_pt_batch`（先處理完嗰批，timer 先醒）。BASIC 一分鐘先更新一次，timer 之間 view 可能完全冇變。
- **Delay**：intent 嘅生效時間 = 決定時間 + `decision_timing.delay_s`（0..3600 秒整數）；validator 要求 `offset_s + delay_s < 0`，即係生效時間喺第一次公佈嘅 `marketTime` 之前。P0 draft 嘅 `delay_s` 係 null（draft 准 null），task (c) 要喺任何 run 之前定。生效時間亦要喺觀察到嘅 suspend／in-play 之前；BASIC 一分鐘嘅解像度令呢個邊界有不確定性，要明確處理，唔可以靜靜當成功。
- **Image／reset**：有 `img` 嘅 message 係取代成個 market 嘅 state，唔係 merge（`decision_timing.image_handling` `img_replaces_market_state`，schema v1 只准呢個）。2017 BASIC 同 ADVANCED sample 冇 `img`；PRO sample 有 274 個 `img` true 嘅 market change，全部都唔係 market 嘅第一個 change、全部帶 definition、冇一個漏咗之前有嘅 runner（2026-10-14 數，淨係睇格式）。Task (b) 嘅 index 已經照呢條做（§4.1 Image）：image 冇帶嘅欄位同 runner 喺 image 嘅 `pt` 變 null（runner `listed` false），之前嘅 history 照留；最後仲 `listed` 嘅 runner（喺最後一個 definition 入面、冇俾之後嘅 image 清空）先算而家嘅結果。冇 definition 嘅 image、少咗 runner 嘅 image definition、CLOSED 之後漏咗 WINNER 嘅 image 都係用 synthetic stream 測。Task (c) 嘅 loader 要跟同一個規則。
- **Historic `pt` 只係 proxy**：佢係 Betfair 嘅 publish time，唔係我哋量度到嘅收到時間或者延遲（[edge framework](edge-framework.md)：historical download 嘅 retrieval time 唔係當時 delivery speed 嘅證據）。所有 delay 都係假設。

**Evaluator 嘅資料同策略嘅資料分開。** 最終 BSP、結果、喺決定時間之後先出現嘅 removal 同修訂過嘅 metadata，就算喺 index 入面，都唔可以入策略嘅 view；只有 simulator 同 evaluator 讀得到。決定時間或之前已經觀察到嘅 removal 屬於 prefix（見上面第一點）。Contract 層面：`final_bsp` 同 `results` 只可以寫喺 `capabilities.outcome`（結算要用），寫喺 `capabilities.decision` 會被拒（`final_bsp_is_an_outcome`），亦唔可以做 feature（`outcome_not_a_feature`）；佢哋冇或者之後改咗，只影響結算（unresolved），唔會令之前任何決定變 abstain（§2.3）；每個 variant 用 `uses_features` 列明佢用邊啲已聲明嘅 feature（冇聲明嘅報 `undeclared_feature`），runner 只畀佢睇呢啲。Feature 定義同 variant 描述係自由文字，validator **冇**檢查入面有冇偷用結果；真正嘅防線係 runtime 嘅 observed-prefix view 同 review。

**唔用回顧式規則。** 「最後一個賽前 update」或者「實際開跑前 60 秒」都要事後先知道實際開跑時間。決定時間一定要由當時睇得到嘅嘢定。P0 contract 用 `decision_timing.anchor` `first_published_scheduled_off`、`offset_s` −300：第一次公佈嘅 `marketTime` 前 5 分鐘，之後 `marketTime` 改咗都唔會郁個決定時間。Validator 拒絕任何其他 anchor；名入面（唔分大細楷）有 `actual`、`last`、`latest`、`final`、`revised`、`close`、`suspend`、`in_play`、`inplay`、`turn`、`result`、`settle`、`reconcil`、`bsp`、`winner`、`post` 呢啲字串嘅，會報 `retrospective`，其他報 `not_allowed`（例如有連字號嘅 `in-play-start`）。`offset_s` 一定要係 −86400 至 −1 嘅整數。

**Prefix-invariance 測試。** 將 stream 喺 `t` 截斷再跑，`t` 或之前嘅決定要同用完整 stream 跑出嚟嘅一模一樣；即係將來嘅改變唔可以改到之前嘅決定（task (c)）。Task (c) 一定要另外測：將 `t` 之後先出現嘅最終 BSP、結果、removal 同 market definition **刪走**或者**改咗**（例如 resettlement），`t` 或之前嘅決定（decision ID、abstain／unsupported 原因、intent）都要一模一樣；可以變嘅只有結算，包括由 resolved 變 unresolved。呢個係 contract 入面 `engineering_acceptance` 嘅 `prefix_invariance` 準則（§10）。

## 6. Decisions and the issuance ledger

- **穩定嘅 decision ID**：同一份 contract、同一個策略版本、同一份 input，重跑一定得出同一個 ID；佢唔可以靠結果或者檔案入面嘅位置。
- **完整嘅 issuance ledger**：cohort 入面每個應該考慮嘅 (market, runner, decision point) 都有一行，包括之後退出嘅 runner、`abstain`、冇 label、settlement 唔支援嘅情況。唔准喺睇咗結果之後剔走任何一行。每個 variant 每行只有一個結果：`issued`（出單）、`not_selected`（variant 冇為呢個 market abstain，但冇揀呢隻 runner，即係下面嘅 watch）、`abstained`（連決定時間嘅原因）或者 `unsupported`（連缺咗嘅 `capabilities.decision` input）；按結果同原因數，加埋等於行數（`engineering_acceptance` 嘅 `issuance_exposure_reconciliation`，§10）。
- **重複決定要有 decision axis**：一個策略喺同一個 market 決定幾次（例如幾個時間點），scoring 要知道每次係邊個 axis，唔可以事後揀最好嗰次。
- **Forecast、hypothesis、intent 同 label、fill 分開**：策略只出 forecast（概率）、watch／abstain 同模擬 intent；label 由 evaluator 由結果砌，fill 由 simulator 決定。
- **留畀 E01**：完整 runner cohort、資料新鮮度、training cutoff 同 model version、只用過去嘅 context。今日嘅新聞或者 LLM 知識唔會自動係當時有效嘅 context。

**P0 contract 嘅 cohort（`cohort`，文字規則，task (c) 照住實作）：**

- Market：評估月份入面每個 GB 或 IE horse-racing WIN market，只要佢第一個 market definition 喺決定時間或者之前公佈咗；決定時間之後公佈嘅嘢唔會加入或者剔走任何 market。Task (b) 嘅空 view record（§4.1「冇 view」）冇決定時間，佢存唔存在亦唔係 prefix-invariant（開跑前冇 member 提過嘅 market，要之後有 member 提到先有），所以唔可以當呢條規則入面已公佈嘅 market，亦唔入 prefix-invariance 比較；要明確報告、唔可以靜靜剔走，點報見 §13。
- Runner：決定時間或者之前最新嗰個 market definition 入面每個 ACTIVE runner；之後先被 remove 嘅 runner 照留喺 issuance ledger。用 task (b) 嘅 `decision_time` 即係嗰陣 status ACTIVE **而且** `listed` true：普通 definition 冇再列嘅 runner，status 仲係 ACTIVE 但 `listed` false，唔喺最新 definition 入面，唔算；image 清空咗嘅 runner status 係 null，亦唔算（§4.1）。
- Abstain 原因：`market_not_published_by_decision`（決定時間之前冇 market definition）、`market_not_open_pre_off`（唔係 OPEN 或者已經 in-play），同決定時間嘅 observed prefix 缺少 decision capability（`capabilities.decision`）時嗰個 capability 嘅 unsupported 原因。
- **缺結果唔係 abstain 原因**：冇 `final_bsp` 或者 `results`（`capabilities.outcome`），張單照出、照留喺 ledger、exposure 照計，結算做 unresolved（§7）。
- **原因只可以嚟自決定時間**：`cohort.exclusions` 同 variant 描述係自由文字，validator 唔讀入面意思，改變唔到上面呢條。Runner 嘅 abstain／unsupported 原因只可以係上面兩個固定 cohort 原因、`capabilities.decision` 缺咗嘅 input，或者 variant 用自己聲明嘅 feature 定嘅規則（例如 `ltp_favourite` 打和）；exclusion 文字唔可以加一個按結果 abstain 嘅原因，因為 `capabilities.outcome.on_missing` 只准 `unresolved_settlement`。

## 7. P0 BSP model

P0 只模擬一種單：**fixed-stake BACK at BSP（MOC）**。

| 假設 | 內容 |
|---|---|
| 接受 | 賽前送出嘅 MOC 無條件被接受 |
| 影響 | 我哋嘅注碼對 BSP 影響可以忽略；BASIC 重建唔到我哋加入嘅注碼點樣改變 reconciliation |
| 價錢 | 用檔案畀嘅 BSP，保留原本精度；**唔會**夾去 LIMIT 嘅 tick ladder，亦唔聲稱可以重建到準確到一便士。PRO sample 嘅 stream `bsp` 似乎截到兩個小數位，同 BSP CSV（8 個小數位）差少過 0.01（[data audit](data-audit.md)）；Betfair 用邊個值結算、BASIC 係咪一樣，未核實 |
| 延後 | LOC、LAY |

P0 draft contract 嘅 `staking`：`side` BACK、`price` BSP、`acceptance` `unconditional_pre_off`、`market_impact` `assumed_negligible`、`stake_pence` 100（即係 £1）。Validator 拒絕 `LAY`、`LIMIT`、`LIMIT_ON_CLOSE`（`deferred`）。`capabilities.outcome.required` 一定要有 `results`（冇就報 `missing_results`），用 BSP 就一定要有 `final_bsp`（`missing_final_bsp`）；`capabilities.decision.required` 一定要有 `market_definition`（`missing_market_definition`）。結果寫入 decision 類報 `<cap>_is_an_outcome`，decision input 寫入 outcome 類報 `<cap>_is_a_decision_input`；decision 類用 `unresolved_settlement` 報 `not_for_decision_inputs`，outcome 類用 `unsupported_abstain` 報 `would_change_an_earlier_decision`。

**結算。** 每種情況都喺 contract 嘅 `settlement` 寫明；做唔到嘅就係 **unresolved**，保留決定同 exposure，唔准喺睇咗結果之後靜靜剔走，亦唔准當 0 結算。Schema v1 每個 `settlement` 欄位都只准 `unresolved`，P0 draft 全部寫 `unresolved`；要真係結算呢啲情況，就要 schema v2 加規則，喺凍結之前改 contract（hash 會變）：

| 情況 | `settlement` 欄位 | 處理（v1） |
|---|---|---|
| WINNER／LOSER，有 BSP | — | 按 BSP 結算 |
| Market void | `market_void` | unresolved |
| 我哋嘅 runner 喺決定時間之後先被 remove | `own_runner_removed_after_decision` | unresolved |
| 其他 runner 喺決定時間之後先被 remove | `other_runner_removed_after_decision` | 對 BSP 單嘅影響喺本文件未核實；unresolved |
| Market CLOSED 之後再改結果、`bsp` 或 `numberOfWinners`（例如 resettlement） | `result_revised_after_close` | unresolved |
| Dead heat（`numberOfWinners` 係 1 但有多過一個 WINNER） | `dead_heat` | unresolved |
| 冇 CLOSED market definition、冇 BSP、`bspReconciled` 唔係 true | `missing_bsp_or_result` | unresolved，exposure 照計；之前嘅決定唔改，唔會變 abstain（`capabilities.outcome.on_missing` `unresolved_settlement`） |

決定時間或之前已經被 remove 嘅 runner 由 cohort 規則排除（§6），唔係 unresolved。淨係 `marketTime`、`version` 或者其他唔影響結算嘅欄位改變，唔會令一注變 unresolved。

## 8. Commission and metrics

**Commission。** 按 market 計：`commission_m = c × max(g_m, 0)`，`g_m` 係一個 market 嘅淨贏輸；rate `c` 同 rounding 規則喺 contract 聲明（公式同 +£10／−£10 例子亦喺 [research plan §4](research-plan.md#4-count-costs-correctly)）。檔案入面嘅 `marketBaseRate`（2017 IE sample 係 5.0）**唔等於**實際收費：實際 rate 跟 account 嘅 package（UK 係 My Betfair Rewards；Betfair 自己嘅例子係 Basic 2%，5% 同 8% 只有二手來源，[fact register](betfair-facts.md) F62）。Yu 用邊個 package、P0 應該用邊個 rate，**仲未決定**（§13）。

P0 draft contract 暫時聲明 `commission_rate` 0.05、`rate_source` `declared_assumption`、`basis` `positive_net_market_winnings`、`rounding` `per_market_ceil_penny`（每個 market 嘅 commission 向上捨入到一便士）。5% 係 example config 用嘅保守數，嚟自二手來源（F62），唔係任何 account 嘅實際收費。`rate_source` 寫 `market_base_rate` 會被拒（`not_effective_charge`）；知道 Yu 嘅 package 之後可以改用 `account_statement_verified`。Commission 用精確算術計（例如 `Decimal(repr(rate))` 或者整數 basis points），唔好用 float 乘完再 ceil：rate 0.07、market 淨贏 100p，`math.ceil(0.07 * 100)` 會得 8p 而唔係 7p。Contract 入面 `commission_rate` 一定要寫成 JSON float（`0.05`；`0` 報 `not_float`，`-0.0` 報 `negative_zero`），等同一個 rate 只有一個 hash。

**Metrics（P0，淨 BACK）：** 全部按 variant 分開計。每個 variant 定義：`N_R` = resolved 注嘅淨贏輸（每個 market 扣咗 commission 之後加埋）、`S_R` = resolved 注嘅總 stake、`S_U` = unresolved 注嘅總 stake。

- **Resolved ROI** = `N_R / S_R`（primary，`sum_net_over_sum_stakes_resolved`）。佢係有條件嘅：只計有結算嘅注，所以就算 resolved ROI 係正數，本身都證明唔到所有出咗嘅注加埋都賺錢。
- **悲觀 all-issued ROI** = `(N_R − S_U) / (S_R + S_U)`（`roi_worst_case_unresolved_lost`：unresolved 全部當輸）。P0 每個 variant 每個 market 最多一注 BACK：一注輸咗就係輸晒 stake、冇 commission，所以呢條式就係所有出咗嘅注嘅最差情況；每個 market 多過一注嘅 contract 要重新推導。
- 分母係 0（`S_R = 0`，或者 `S_R + S_U = 0`），嗰個 ROI **冇定義**，照報冇定義，唔當 0。
- **唔准將 candidate 同 baseline 對沖**：每個 variant 自己一套會計，commission 按 (variant, market) 計；唔可以將兩個 variant 喺同一個 market 嘅注夾埋計 net 或者 commission。
- Unresolved 嘅注數同 stake（`S_U`）一定並排報（`unresolved_exposure`），唔當 0、唔剔走。Validator 要求 secondary metrics 有 `coverage`、`unresolved_exposure` 同 `roi_worst_case_unresolved_lost`。
- **不確定性**：先按 market 加埋 net，再按日或者 block resample；保留同一場賽、同一個 meeting 之間嘅相關，亦要睇長啲嘅時間 block（例如週、月）。
- **Drawdown** 要用全域時間次序計，唔可以將每個 market 嘅 trace 駁埋。
- **冇動態共用 bankroll**（延後）。
- **CLV**：入場價就係 BSP，所以「執行 CLV vs BSP」機械上一定係 0，冇意思。之前嘅 LTP 去 BSP 嘅變動可以另外報，但佢係唔可執行嘅比較，唔係執行結果。
- **一定要報**：模擬嘅 resolved ROI 同悲觀 all-issued ROI（分母係 0 就報冇定義）、coverage（cohort、決定、abstain、unsupported 數目）、unresolved exposure；策略有出概率就報 proper score（race-level log loss、Brier、calibration，見 [edge framework](edge-framework.md)）。

P0 draft contract：primary metric `roi`（`sum_net_over_sum_stakes_resolved`）；secondary 有 `abstentions_by_reason`、`bet_count`、`coverage`、`ltp_to_bsp_movement_non_executable`、`max_drawdown_global_chronology`、`net_pnl`、`roi_worst_case_unresolved_lost`、`total_stakes`、`unresolved_exposure`。對返上面嘅定義：`net_pnl` = `N_R`；`total_stakes` = `S_R + S_U`（`S_R` 同 `S_U` 亦分開報，`S_U` 喺 `unresolved_exposure`）；`bet_count` = 出咗嘅注 = resolved + unresolved。`clv_vs_bsp` 會被拒（`zero_by_construction_for_bsp_entry`）；`brier_score` 同 `log_loss` 喺 schema v1 延後（`deferred_no_forecast_output`），因為冇 variant 聲明得到概率輸出。不確定性用 `block_bootstrap`，先按 market 計 net，block 係 `race_day`，10,000 次 resample，95%。Schema 准 `race_day`、`iso_week`、`calendar_month`，拒絕短過一日嘅 block（`shorter_than_race_day`）；但一份 contract 只可以揀一個 block，「同時睇長啲嘅 block」點樣寫入 contract 仲未定（§13）。

## 9. Baselines and engine checks

**Random selection 唔係 engine 嘅答案。** 佢係一個有不確定性嘅 empirical baseline，唔可以用嚟證明引擎啱。就算賠率完全公平（`o = 1/p`），一注 fixed-stake BACK 扣 commission `c` 之後嘅期望 net ROI 係 `−c(1−p)`，唔係 `−c`：

```text
E[net] / stake = p × (1 − c) × (o − 1) − (1 − p)
               = (1 − c)(1 − p) − (1 − p)        # o − 1 = (1 − p) / p
               = −c × (1 − p)
p = 0.5, c = 0.02  →  −1%
```

而且 BSP 係咪公平（有效率）本身都未證明，所以 random-at-BSP 嘅結果唔係已知答案。

P0 draft contract 有兩個 variant：`ltp_favourite`（candidate：決定時間 `ltp_at_decision` 最低嗰隻，打和或者有任何 runner 冇 LTP 就成個 market abstain）同 `seeded_random_runner`（baseline：由 `seeds.selection` 同 market id 揀一隻 ACTIVE runner）。後者係有不確定性嘅 empirical baseline，唔係 engine oracle。

**引擎正確性用 synthetic fixture，答案人手計好**（contract 嘅 `engineering_acceptance` 入面 `known_accounting_answers` 呢條準則，§10）。2% commission：

| Fixture | 人手答案 |
|---|---|
| 兩個獨立 market：£1 BACK @ 2.0 贏（+£1，commission £0.02）；£1 BACK @ 2.0 輸（−£1） | 合計 net −£0.02 |
| 同一個 market 入面 +£10 同 −£10 | market 淨 0，commission 0，net 0 |
| 兩個獨立 market：+£10 同 −£10 | commission £0.20，net −£0.20 |
| Void、dead heat | Schema v1 一律 unresolved：要見到 unresolved 同 exposure；有 v2 規則之後先人手計 |

## 10. Experiment protocol

**Contract 先凍結。** 每個實驗喺睇任何結果或者做任何搜尋之前，寫一份 strategy card／experiment contract 並凍結：切法、cohort、exclusion、feature、成本、staking、primary metric、stopping rule 同所有 variant。P0 嘅係 [`research/experiments/backtest-p0-bsp-v1.json`](../research/experiments/backtest-p0-bsp-v1.json)，用下面命令驗證：

```sh
python3 -m betangel_research.betfair.backtest validate-contract --contract research/experiments/backtest-p0-bsp-v1.json
```

- 佢讀一個本地 regular file（FIFO、device、目錄報 `not_a_regular_file`，喺開檔之前查），印一行 JSON `{"status", "contract_sha256", "problems"}`：`valid_draft` 或 `valid_frozen` 係 exit 0，`invalid` 係 exit 2（argparse 用法錯誤都係 2）；加 `--require-frozen` 嘅話 `valid_draft` 係 exit 3，等 shell gate 唔會將 draft 當 frozen。`problems` 列晒所有問題（`<path>:<reason>`），唔係淨係第一個。`invalid` 嘅 contract 都會印 sha256，但只係用嚟認檔；寫 trial ledger 之前一定要查 `status` 係 `valid_frozen`。
- 讀檔好嚴：UTF-8、唔准重複 key、唔准 NaN／Infinity，超出 ±2^63 嘅整數報 `number_too_large`；每一層嘅未知 key 都拒絕；集合式嘅 list 一定要排好序、冇重複，`features` 同 `engineering_acceptance.criteria` 按 `name`、`variants` 按 `id` 排序，比例欄位（`costs.commission_rate`、`financial_rule.decision_threshold`、`uncertainty.confidence_level`）一定要係 JSON float、唔准 `-0.0`，等同一個意思只有一個 hash。`contract_sha256` 係 Python `json` 寫出嚟嘅 canonical JSON（按 code point 排 key、緊湊分隔、UTF-8、float 用 `repr`）嘅 sha256；唔係 RFC 8785，其他語言要重現就要跟 Python 嘅 float 格式。
- **Draft 同 frozen**：draft 只可以將 `decision_timing.delay_s`、`stopping_rule.max_trials`，同 `registered_hypothesis` 嘅 `financial_rule.hypothesis`、`financial_rule.decision_threshold`、`financial_rule.decision_rule` 留 null，`frozen_on` 一定係 null；frozen 乜都唔可以 null，`frozen_on` 唔可以早過 schema v1 嘅日期 2026-10-14（`before_schema_v1`；將來嘅日期冇時鐘就查唔到）。Frozen 另外要至少一個 exclusion（`empty_in_frozen`），任何文字欄位都唔可以係 placeholder（例如 `TBD`、`TODO`、`n/a`、`-`、`?`，報 `placeholder_in_frozen`），hypothesis、決定規則、`financial_rule.reason`、engineering 準則嘅定義、stopping rule、cohort 規則、feature 定義同 variant 描述最少 20 個字元（`too_short_in_frozen`），ROI 門檻要喺 −1 同 999 之間。呢啲係擋住空殼凍結嘅 heuristic，唔證明文字有意思。
- **兩種 registration，各自有誠實嘅凍結形式**（Astra 2026-10-14 re-review）：
  - `financial_rule` 有類型，一定要對應 `registration.kind`。`registered_hypothesis` 用 `roi_threshold`：hypothesis、ROI 門檻、決定規則，凍結時全部要填。`engineering_check` 用 `not_applicable` 加一段 `reason`：明確寫明冇財務 hypothesis、冇 ROI 門檻、冇決定規則；唔係留 null，亦唔使為咗過凍結作一個出嚟。用錯類型報 `not_applicable_required_for_engineering_check` 或者 `roi_threshold_required_for_registered_hypothesis`。
  - 兩種都一定要有 `engineering_acceptance`：三條確定性嘅準則，名固定（`ENGINEERING_CRITERIA`），每條有定義。`known_accounting_answers`：§9 synthetic fixture 嘅人手答案，準確到一便士；void、dead heat 呢類 fixture 係 unresolved 而且 exposure 照計。`prefix_invariance`：§5，包括將之後先出嘅 BSP 同結果刪走或者改咗。`issuance_exposure_reconciliation`：§6 嘅 ledger 每個 cohort 位一行、冇事後剔走；每行只有一個結果（`issued`、`not_selected`、`abstained` 連原因、`unsupported` 連 capability），按結果同原因數加埋等於行數；issued = resolved + unresolved；issued stake = `S_R + S_U`（§8）。`rule` 係固定 token `all_criteria_must_hold_outcome_metrics_not_judged`（`ENGINEERING_RULES`；寫其他嘢，包括自由文字，報 `not_allowed`）：任何一條唔過，成個 run 就唔過；觀察到嘅 ROI、net、drawdown 只係報，唔可以令 run 過或者唔過。準則嘅定義係自由文字，validator 只查 placeholder 同長度（同 feature 定義一樣），入面有冇偷偷加結果條件，要靠凍結時嘅 review。
  - Operational 嘅選擇（delay、成本、cohort、variant、seed、trial 上限同 stopping rule）兩種都一定要凍結時填好。冇任何繞過 frozen gate 嘅路：跑之前同寫 trial ledger 之前，照樣要 `valid_frozen`（`--require-frozen`）。
- **P0 而家係 `draft`**，`registration.kind` 係 `engineering_check`：佢只評估 engineering 年；`financial_rule` 係 `not_applicable`，因為呢次跑嘅任何結果都唔可以支持或者否定一個 edge；`engineering_acceptance` 已經寫好。淨係 `delay_s` 同 `max_trials` 仲係 null，task (c) 喺凍結同任何 run 之前定；P0 凍結咗先跑，唔會以 draft 身份跑。Astra 嘅 reproduction（draft 加 `status` frozen、`frozen_on` 2026-10-14、`delay_s` 1、`max_trials` 2）以前報三個 `null_in_frozen`，而家係 `valid_frozen`；`delay_s` 或者 `max_trials` 係 null 就照樣 `null_in_frozen`。Stopping rule：每個 variant 跑晒成個 engineering split；睇咗結果之後要加、減或者改任何 variant、feature、cohort 規則或者成本，就要開新 contract、新 hash；每個 trial 連 contract sha256 寫入 trial ledger。
- **Validator 證明到同證明唔到。** 一份 valid contract 只證明呢啲選擇係用呢個格式寫低、hash 係呢個；佢證明唔到係咪喺任何人睇結果之前寫、檔案存唔存在、有冇聲明嘅 capability，更加唔係盈利證據。

**凍結前後嘅其他規則：**

- **Integrity inventory** 可以喺凍結之前跑（檔數、market 數、欄位同 BSP coverage），但唔可以做全 corpus 嘅結果分析。`archive-audit`（§4.1）就係呢個 inventory，只報整數計數；不過佢冇最少 group 大細，一個 tier × 月份 × country × type 嘅 group 得一個 market 嗰陣，dead heat、從未 CLOSED、缺 BSP 呢啲計數等於講晒嗰個 market 嘅結算 coverage（講唔到贏家、價錢或者 BSP 值）。凍結之前睇 audit 要知道呢點。
- **Trial ledger** 記低每個 trial：started、failed、aborted、completed，用咗邊個 variant、input hash、seed 同 training cutoff。失敗同中途放棄嘅都計（[edge framework](edge-framework.md)：揀嚟揀去本身就係 overfitting 嘅來源）。
- **Holdout gate**：schema v1 嘅 validator 一律拒絕評估 holdout（`evaluate_split` 寫 `holdout` 報 `sealed`，frozen 都一樣）；而家冇任何讀資料嘅 tooling。幾時、點樣解封要新 schema，同 Yu、Astra 決定（§13）。呢個係 workflow control，唔係防到直接打開 raw 檔嘅保安封條；2026-01..08 而家靠唔下載嚟封，2026-09 已暴露、唔係 holdout（§3）。
- **Walk-forward** 要真係每一步只用過去資料 fit、要 purge，唔係將一個用晒成段時間揀出嚟嘅 model 按日期切開再計分。Schema v1 嘅 `fitting.kind` 只准 `none`，寫 `walk_forward` 報 `deferred`；聲明 `none` 但其實喺結果上 fit，validator 查唔到，要靠 review。

## 11. P1: flumine cross-check conditions

- 兩邊要用**一樣**嘅決定、時鐘、成本同執行假設；兩個 simulator 夾，都唔代表係真相。
- Betfair Data Scientists 嘅 flumine simulation 教學只用 PRO 檔（"has not been tested on the BASIC or ADVANCED level files"），並列明冇 market catalogue、冇 cross matching、冇 market impact；ratings 教學用 PRO，話方法應該用 ADVANCED 都得、BASIC 唔得。flumine 本身用唔用得 ADV／BASIC 未核實，P1 照計劃用 PRO。免費年 2015-05..2016-04 嘅 PRO 檔係 £0。
- flumine 3.2.7（PyPI 2026-10-13，MIT）嘅 source：`commission_base` 旁邊寫住 `# not implemented`，market summary 只報 `max(profit × commission_base, 0)`；即係 commission 要我哋自己計。佢嘅 docs 亦列咗 known issue，例如 PLACE market 嘅 dead heat 冇處理。

## 12. Storage

- **Lab 機（Yu 2026-10-14 決定）。** 一部專用、清空咗嘅 MacBook Pro（大約 256 GB），同 MacBook Air 喺同一個屋企網絡，做 raw archive 同重型計算：資料放 `~/betangel-data/historic/`（Git 同任何 clone 以外），MacBook Air 上面嘅 Claude 經 LAN SSH（`<name>.local`，冇 Tailscale）用。Lab 機**冇任何 Betfair credentials**，唔跑任何會 login 嘅命令；落單、Keychain 同 live client 留喺 MacBook Air。設定步驟喺 session archive 嘅 `lab-setup-brief.md`（Git 以外）。篩咗 GB/IE WIN 嘅 PRO 大約每個月 1 GB（Yu 喺 portal 見到嘅數）。呢個目錄就係 §4.1 嘅 data root：佢同所有上層目錄都唔可以有 `.git`。
- **之後先上一個獨立嘅 private Tigris bucket。** 唔係 collector 嘅 backup bucket 或者 prefix，亦唔原封不動 reuse collector 嘅 backup uploader 同 retention（現有文件只係提議 backup retention）。Upload／materialization 用有範圍限制嘅 credentials；離線 runner 只收本地檔案，冇 storage client。
- **£0 資料唔等於 £0 雲端。** Tigris 嘅免費額（Astra 2026-10-14 讀）係每個 account 每個月：頭 5 GB Standard storage、10k Class A、100k Class B request，唔係每個 bucket。現有 account 用咗幾多，`unverified`。所以上傳大批之前，先量 archive 大細同剩餘額度。
- **Credentials 只放 macOS Keychain**，用隱藏輸入（`security add-generic-password … -w`，`-w` 放最後，見 [接入指南 §5](betfair-integration.md#5-keychain-setup)），唔入檔案、argv 或者 log。

## 13. Open questions

**已決定（2026-10-14，留低做記錄）：**

- **Greyhound racing 同 soccer：而家唔做。** Yu（約 13:57Z）："no need for greyhound and soccer now. We focus on horse racing."（§2.7；價錢同檔數留做參考）。Schema v1 本來就只收 `horse_racing`。
- **2026-09 已知暴露：** holdout 收窄到 2026-01..2026-08，2026-09 記做已暴露、只做 exploratory，2026-10-12 嘅賽事唔可以做 prospective；原本嘅 2026-01..2026-09 作廢（§3）。呢個係 Astra 2026-10-14 嘅技術決定，Claude 採納，Yu 同日同意。
- **Engineering check 點凍結：** 用有類型嘅 `financial_rule` `not_applicable` 加確定性嘅 `engineering_acceptance`，operational 選擇凍結時照填；唔以 draft 身份跑，冇繞過 frozen gate（§10）。（Astra R3 要求唔以 draft 身份跑、唔繞過 frozen gate；`financial_rule`／`engineering_acceptance` 嘅 schema 形狀係 Claude 嘅實作，等 Astra 再 review，見下面）

**未決定：**

- **Commission rate**：Yu 嘅 Rewards package 同 P0 用邊個 rate（§8）。
- **BSP CSV 授權**（§2.5）。
- **Holdout 幾時、點樣解封**：schema v1 一律拒絕評估 holdout（§10）；解封程序要 Yu 同 Astra 定，再開新 schema。
- **2015-04 coverage**：portal 頁面之間有衝突（§2.1）；P0 唔用。
- **Removal、void、dead heat 對 BSP 單嘅實際規則**：本文件未核實 Betfair 規則；schema v1 每個 `settlement` 欄位只准 `unresolved`（§7）。
- **Schema v1 未有嘅嘢**：research 入面嘅 train／validation／test 或者 walk-forward 切法（`fitting.kind` 只准 `none`）、真正結算 void／removal／dead-heat 嘅規則、按 tier 收緊 `offset_s` 上限（而家 BASIC 一分鐘取樣下 `offset_s` −1 照樣通過）、同一份 contract 報幾個 bootstrap block、holdout 解封程序。v1 拒絕未知 key，所以要先開 schema v2（新 hash）。
- **Astra 2026-10-14 re-review 之後嘅更正，等佢再 review**：佢喺 9716526 要求嘅 R1–R4 同 N1–N3 喺呢版做咗：decision／outcome capability 分開（`capabilities.decision`／`capabilities.outcome`）、holdout 收窄同 `EXPOSED_MONTHS`／`NOT_PROSPECTIVE_DATES`、`financial_rule` 同 `engineering_acceptance`、boundary test 嘅 shadowing 規則、拒絕 year 0000、§8 嘅 metrics 定義。之後 internal review 再收緊咗：`engineering_acceptance.rule` 改做固定 token、issuance ledger 每行嘅結果加咗 `not_selected`、已暴露嘅月份喺任何 split 都報、`EXPOSED_MONTHS`／`NOT_PROSPECTIVE_DATES` 加咗 `known_minimum`、boundary test 拒絕搜尋位置入面所有標準庫名（唔分大細楷）同並存嘅 `X.py`／`X/__init__.py`。呢啲新欄位同規則佢未睇過；v1 喺佢接受之前都唔算定稿。
- **Runner 點拒絕 state root 底下嘅輸入（架構未定）**：boundary test 查嘅係成個 backtest closure，唔止 backtest package；backtest import 嘅任何 module，放喺邊度都喺 closure 入面，一樣唔准寫 state root 字串或者查 home 目錄（Astra 試過：root module 入面一個 `Path.home()` helper 照樣令嗰個測試 fail）。所以將 guard 搬出 backtest **唔係**解決方法，亦唔會令佢豁免。要麼一個 review 過嘅 boundary 改動，要麼一個真正分開、backtest 唔 import 嘅 preflight 階段；task (c) 開始之前要同 Astra 定。
- **長啲嘅 bootstrap block**：一份 contract 只揀一個 block（P0 係 `race_day`），點樣同時報週或月 block 未定（§8）。
- **P1–P4 嘅內容同次序**：暫定（§4）。
- **真 TAR 嘅路徑 layout 未核實**（§2.8）：`archive.py` 假設 `<PLAN>/<YYYY>/<Mon>/<D>/<eventId>/<檔>.bz2`、外層冇壓縮、以兩個 zero block 結尾、冇 pax global header。第一個真 TAR 唔係咁，就會成個被拒：所有 member 都係 `no_parseable_month`（`archive:nothing_kept`）、`not_a_readable_tar`、`archive:not_ended_by_zero_blocks` 或者 `archive:pax_global_header`；之後要睇過、開新 manifest schema，唔可以放寬去估。路徑日期同 `marketTime` 嘅關係（`date_mismatch`、`event_id_mismatch`）亦要用真資料睇。
- **`view_ends` 同讀唔完嘅 member 喺 cohort 點處理**（task (c) 凍結之前定）：決定時間 ≥ `decision_time.view_ends.pt` 嘅 market 用邊個原因（例如 §6 嘅 `market_not_open_pre_off`，定一個新嘅 unsupported 原因）；空 view（`view_ends` `{pt: null, reason: primary_source_no_observation_before_cutoff}`）冇 `pt`，上面嗰條比較包唔到佢：佢用邊個 unsupported 原因、喺 ledger 定喺另一個 population 報告（佢冇決定時間，存唔存在亦唔係 prefix-invariant，§4.1），定係綁去任何 member 第一次提到呢個 market 嘅 `pt`（index v1 冇記，要新 schema）；primary 被拒（`refused_markets` 入面 `primary_source_refused`，成個 market 冇 record）嘅 market 點樣報做 population exclusion；`read_complete` false 嘅 market 結算一律 unresolved，primary 冇呢個 market 嘅 record 嘅 market（`final_status` null、`closed_reached` false）亦係 unresolved，唔係 void；被 content gate 拒絕嘅 member（只會喺 2025-12／2026-01 交界）點樣報做 population exclusion。
- **`bspMarket` false 嘅 WIN market**（§2.8 smoke：8 個入面 1 個）：P0 只做 BACK at BSP；`bspMarket` 係決定時間已知，所以 task (c) 要喺凍結之前定：佢係 cohort 嘅決定時間 unsupported 原因，定係留喺 cohort、結算 unresolved。
- **Task (b) 改咗 boundary test 兩處，等 Astra review**：`BACKTEST_STDLIB` 加 `tarfile`（註解寫明係 package-wide，由 `tests/test_betfair_backtest_archive.py` 釘死只有 `archive.py` import、只開 `"r|"`）；`__main__` 嘅 import 集合加咗 `archive`、`coverage_audit`、`market_index`（佢哋喺 function 入面 import；冇放寬任何規則）。

## 14. Sources with access dates

全部 **2026-10-14** 讀取，除非另外註明。

- **Betfair Historical Data portal** [historicdata.betfair.com](https://historicdata.betfair.com/)：portal 係 JS app，文字由佢自己嘅 translation bundle 讀（頁面冇日期）；條款（"Personal Use Only"、"No onward distribution"）、FAQ（5 日、"from April 2015"、".tar files"、按 marketId 同 eventId 出檔）同 historic data API 說明（`DownloadListOfFiles` 嘅冇日期路徑例子，§2.8）。[`GetStandardRateCard`](https://historicdata.betfair.com/api/GetStandardRateCard)（公開、唔使登入）同 `GetPricePlan`：價錢同折扣月。`AvailableData.csv`：每月檔數同大細。
- **[Betfair Historical Data Feed Specification](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf)**（Last-Modified 2026-05-20）：三個 tier 嘅內容同解像度。
- **官方 samples** `https://historicdata.betfair.com/files/horse-racing-{basic,adv,pro}-sample.bz2`：2017-04-29 Punchestown；欄位係本地 parse 出嚟，已經睇過，見 [data audit](data-audit.md)。
- **Betfair Developer Support**：[Are bulk purchase discounts available?](https://support.developer.betfair.com/hc/en-us/articles/360019984158)（2026-09-24 更新）；[How frequently is the historical data updated?](https://support.developer.betfair.com/hc/en-us/articles/360002427051-How-frequently-is-the-historical-data-updated)（Astra 引用）。
- **Betfair support** [a_id/10620](https://support.betfair.com/app/answers/detail/a_id/10620/)：Astra 引用；本文件冇重讀，冇任何事實單靠佢。
- **BSP CSV** [promo.betfair.com/betfairsp/prices](https://promo.betfair.com/betfairsp/prices) 同 [SP_history](https://promo.betfair.com/betfairsp/SP_history.html)：coverage、欄位同頁尾 licence 句。
- **Betfair Data Scientists hub**：[data listing](https://betfair-datascientists.github.io/data/dataListing/)、flumine simulation 同 backtesting ratings 教學（兩個教學點樣開 TAR，§2.8 用咗）。
- **flumine 3.2.7**（PyPI 2026-10-13，MIT）：docs（known issues 等）同 sdist source `flumine/clients/baseclient.py`、`flumine/markets/market.py`，本地讀。
- **Tigris** [pricing](https://www.tigrisdata.com/pricing/)：Astra 讀；本文件冇重讀。
- **Racing data**（全部公開頁面，未買、未試）：The Racing API（public plans JSON、ToS、FAQ）、timeform.com 嘅 commercial page、Betwise Smartform、Proform、Sporting Life T&Cs；Racing Post 條款 403、At The Races 有 challenge、BHA 結果頁冇 API，三個嘅條款 `unverified`。
- **Repo 內**：[research plan §4](research-plan.md#4-count-costs-correctly)（commission 計法，2026-10-09）、[fact register](betfair-facts.md) F62（Rewards）、[edge framework](edge-framework.md)、[data audit](data-audit.md)、[接入指南](betfair-integration.md)、[落單路徑](betfair-order-path.md)。
