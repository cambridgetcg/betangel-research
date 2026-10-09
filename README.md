# Bet Angel research

Research snapshot: **9 October 2026**. 本地研究 repo；離線 framework 已可運行，未證明有可重複嘅盈利策略。

**初步結論：搵到具名、長期嘅盈利自述，但未搵到獨立審核嘅典型用戶回報。** 買 Bet Angel 可以取得交易工具；工具本身唔足以證明有 edge。公開成功例子、失敗討論同模擬研究要分開睇。

## Start here

- [Edge 開發框架：資料量、速度、連結點樣驗證](docs/edge-framework.md)
- [Hermes／Astra 研究、獨立覆核同下一個 E01 experiment](reports/hermes-edge-2026-10-09.md)
- [論壇交易者點樣諗：思路、反例同可測試假說](docs/forum-mindsets.md)
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

```mermaid
flowchart LR
    A[來源及版本] --> B[ID 與可用時間核對]
    B --> C[固定假說及 baseline]
    C --> D[Replay 與資料延遲測試]
    D --> E[較後日期驗證與 ablation]
    E --> F[成交及成本驗證]
```

現時完成前四步嘅工程版本；後兩步需要更廣、未睇過嘅資料同成交模型。[四張 strategy cards](research/strategy-cards.json) 列出每個假說、反證方法同晉級條件。

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
- This is a local Git repository; no remote publication is implied.

The next research step is a consecutive, multi-date dataset with known access terms, followed by a frozen primary experiment. The public sample replay is not a holdout or profit test. See the [research plan](docs/research-plan.md).

## Local verification

Framework v1: 22 unit/integration tests passed, covering time leakage, revisions, identity ambiguity, snapshots/images, suspensions, zero prices, chronology, balanced scoring, commission and run provenance. Basic/Advanced/Pro real-format replays completed. Independent review findings about delayed direction metrics, suspensions, timestamp precision and identity ordering were corrected and regression-tested. The [results report](reports/sample-replay-2026-10-09.md) states empirical limits.

Initial pilot-import checks, before framework development: all five then-existing JSON files parsed; 21 source records and three hypothesis rows checked; local Markdown targets and source-registry coverage checked. All nine pilot files matched the original SHA-256 values. Running the copied pilot in a temporary directory reproduced its 19-runner, two-race JSON and CSV byte-for-byte. Worked-example arithmetic was checked. Plot rendering and the original race-source acquisition were not repeated; no live profitability or execution was tested.

The imported SVG and generated CSV retain their original whitespace/line endings for byte-level provenance. A full Git whitespace check flags those two files; the check excluding these preserved generated artifacts passed.
