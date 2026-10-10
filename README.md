# Bet Angel research

Research snapshot: **10 October 2026**. [Private GitHub repo](https://github.com/cambridgetcg/betangel-research)；離線 framework 同本地 metadata collection starter 已可運行，未證明有可重複嘅盈利策略。

**初步結論：搵到具名、長期嘅盈利自述，但未搵到獨立審核嘅典型用戶回報。** 買 Bet Angel 可以取得交易工具；工具本身唔足以證明有 edge。公開成功例子、失敗討論同模擬研究要分開睇。

## Start here

- [Collection pipeline：receipt、版本、連結與情境分析](docs/collection-pipeline.md)
- [News／racing data API 選擇及已確認限制](docs/data-provider-options.md)
- [AWS／Supabase／Fly 分工及成本模型](docs/infra-options.md)
- [Collection 工程檢查與實際 API probe](reports/collection-infra-2026-10-10.md)
- [Edge 開發框架：資料量、速度、連結點樣驗證](docs/edge-framework.md)
- [Hermes／Astra 研究、獨立覆核同下一個 E01 experiment](reports/hermes-edge-2026-10-09.md)
- [論壇交易者點樣諗：思路、反例同可測試假說](docs/forum-mindsets.md)
- [14 個觀察 → 9 類 motifs → 3 個離線 prototypes](docs/motif-strategies.md)
- [Prototype 工程結果及目前限制](reports/motif-prototypes-2026-10-09.md)
- [真實歷史資料審核與 ID／時間陷阱](docs/data-audit.md)
- [第一輪 replay 結果](reports/sample-replay-2026-10-09.md)
- [資料、時間、模型與指標合約](docs/data-contract.md)
- [盈利證據：成功、失敗同證據限制](docs/evidence-review.md)
- [常見玩法、edge 假說同失效原因](docs/strategies.md)
- [研究計劃、費用計法同驗證標準](docs/research-plan.md)
- [機器可讀來源索引](sources/sources.json)
- [待驗證假說](research/hypotheses.csv)

我哋優先研究 **賽前價格走勢** 同 **歷史表現能否改善概率預測**。呢個次序按可驗證程度同現有資料決定，唔係聲稱兩者 ROI 最高。Scalping 需要較細緻嘅成交／排隊假設；in-play 仲涉及畫面延遲同未能平倉嘅風險。

## Run the offline framework

Python 3.10+，標準庫，唔需要安裝 ML model 或連接 betting account。官方公開 sample 已下載到本機 `data/raw/betfair-samples/`，raw files 唔入 Git；另一部機可由 [data audit 嘅官方連結](docs/data-audit.md) 取得同一 sample，並核對 manifest SHA-256。

```sh
python3 -m unittest discover -s tests -v
python3 -m betangel_research replay \
  --input data/raw/betfair-samples/horse-racing-adv-sample.bz2 \
  --config research/experiments/sample-preoff-v1.json \
  --out outputs/my-next-run
```

每次用新 output directory。輸出 `report.json`、`predictions.csv`、`summary.md`，並記錄 input/config/code hashes 同 local run ledger。比較 no-change、momentum、mean-reversion、rival-linkage，以及 0／2／10／30 秒人工資料延遲。

另外可運行 forum motif prototypes：

```sh
python3 -m betangel_research motifs \
  --input research/fixtures/motif-scenarios.json \
  --out outputs/my-motif-demo
python3 -m betangel_research motifs --format adv \
  --input data/raw/betfair-samples/horse-racing-adv-sample.bz2 \
  --out outputs/my-motif-adv
```

輸出 `features.jsonl`、`decisions.jsonl` 同 `report.json`。P01 產生價格延續假說；P02 留意 public event 後嘅活動；P03 加 explicit context abstention。Context 目前係 synthetic supplied assessments，未接語言模型或真實 news collector；真實 ADV run 冇 context 時，P02／P03 會 abstain。原 E01 proposal 同 v1 replay 方法保持不變。

```mermaid
flowchart LR
    A[來源及版本] --> B[ID 與可用時間核對]
    B --> C[固定假說及 baseline]
    C --> D[Replay 與資料延遲測試]
    D --> E[較後日期驗證與 ablation]
    E --> F[成交及成本驗證]
```

現時完成前四步嘅工程版本；後兩步需要更廣、未睇過嘅資料同成交模型。[四張 strategy cards](research/strategy-cards.json) 列出每個假說、反證方法同晉級條件。

## Run the collection receipt demo

```sh
python3 -m betangel_research collect-demo \
  --db data/private/news-demo.sqlite
```

示範資料全部 synthetic。再跑同一命令會辨認相同 receipts，唔會重複新增版本。保存 publication／receipt／revision clocks，分開 empty success、failure、unchanged，同埋 quarantine 未來時間嘅 metadata。Database 同 raw data 唔入 Git。現時未有 scheduled API adapters、cloud deployment 或 live LLM；[pipeline 文件](docs/collection-pipeline.md) 清楚列出已實作同待完成部分。

## Existing local pilot

[19-runner / two-race pilot](archive/2026-10-09-aw-pilot/) contains public result measurements from Kempton on 5 October 2026. It shows data handling and descriptive analysis, **not a prediction backtest or profit result**. Previous-race sectionals could later be features; the target race's own sectionals cannot be known before it happens.

The original files remain in `~/Documents/horse-racing-research/2026-10-09-aw-pilot/`. [Import manifest](archive/import-manifest.json) records the source path and SHA-256 of each copied file. Source provenance and known limitations are preserved in the pilot's `sources.json`; redistribution permissions have not been established.

Run its dependency-free calculation from the repository root in a temporary copy:

```sh
pilot_check_dir=$(mktemp -d)
cp -R archive/2026-10-09-aw-pilot "$pilot_check_dir/pilot"
python3 "$pilot_check_dir/pilot/analyse.py"
```

The copy receives regenerated JSON/CSV. Optional plot reproduction needs `requirements-plot.txt` and the script's `--plot` flag; the existing PNG/SVG are already included.

## Current state

- Research and offline Python work run on the Mac.
- Windows VPS specifications are a future hosting choice for Bet Angel, not evidence of profitability.
- No purchases, live account connections or orders are part of this repository.
- Code, synthetic fixtures and research notes are published to the private GitHub repository; credentials, local databases and ignored raw data remain outside Git.

The next research step is a consecutive, multi-date dataset with known access terms, followed by a frozen primary experiment. The public sample replay is not a holdout or profit test. See the [research plan](docs/research-plan.md).

## Local verification

Collection starter: **84 tests passed** across the full suite. Synthetic receipt replay and an actual HTTP 429 failure receipt were checked; see [collection results](reports/collection-infra-2026-10-10.md). No cloud deployment or successful news-coverage trial is claimed.

Motif prototypes: **60 tests passed** across the full suite; synthetic scenarios and the real ADV sample completed. See [engineering results](reports/motif-prototypes-2026-10-09.md) for policy counts, independent-review scope and the limits of these checks.

Framework v1: 22 unit/integration tests passed, covering time leakage, revisions, identity ambiguity, snapshots/images, suspensions, zero prices, chronology, balanced scoring, commission and run provenance. Basic/Advanced/Pro real-format replays completed. Independent review findings about delayed direction metrics, suspensions, timestamp precision and identity ordering were corrected and regression-tested. The [results report](reports/sample-replay-2026-10-09.md) states empirical limits.

Initial pilot-import checks, before framework development: all five then-existing JSON files parsed; 21 source records and three hypothesis rows checked; local Markdown targets and source-registry coverage checked. All nine pilot files matched the original SHA-256 values. Running the copied pilot in a temporary directory reproduced its 19-runner, two-race JSON and CSV byte-for-byte. Worked-example arithmetic was checked. Plot rendering and the original race-source acquisition were not repeated; no live profitability or execution was tested.

The imported SVG and generated CSV retain their original whitespace/line endings for byte-level provenance. A full Git whitespace check flags those two files; the check excluding these preserved generated artifacts passed.
