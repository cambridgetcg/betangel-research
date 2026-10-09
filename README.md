# Bet Angel research

Research snapshot: **9 October 2026**. 本地研究 repo；未證明有可重複嘅盈利策略。

**初步結論：搵到具名、長期嘅盈利自述，但未搵到獨立審核嘅典型用戶回報。** 買 Bet Angel 可以取得交易工具；工具本身唔足以證明有 edge。公開成功例子、失敗討論同模擬研究要分開睇。

## Start here

- [盈利證據：成功、失敗同證據限制](docs/evidence-review.md)
- [常見玩法、edge 假說同失效原因](docs/strategies.md)
- [研究計劃、費用計法同驗證標準](docs/research-plan.md)
- [機器可讀來源索引](sources/sources.json)
- [待驗證假說](research/hypotheses.csv)

我哋優先研究 **賽前價格走勢** 同 **歷史表現能否改善概率預測**。呢個次序按可驗證程度同現有資料決定，唔係聲稱兩者 ROI 最高。Scalping 需要較細緻嘅成交／排隊假設；in-play 仲涉及畫面延遲同未能平倉嘅風險。

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

The next concrete step is the historical-data feasibility audit in the [research plan](docs/research-plan.md), before selecting a larger experiment or paying for hosting.

## Local verification

On 9 October 2026: all five JSON files parsed; 21 unique source records and three hypothesis rows checked; local Markdown targets and source-registry coverage checked. All nine pilot files matched the original SHA-256 values. Running the copied pilot in a temporary directory reproduced its 19-runner, two-race JSON and CSV byte-for-byte. Worked-example arithmetic was checked. Plot rendering and the original race-source acquisition were not repeated; no live profitability or execution was tested.

The imported SVG and generated CSV retain their original whitespace/line endings for byte-level provenance. A full Git whitespace check flags those two files; the check excluding these preserved generated artifacts passed.
