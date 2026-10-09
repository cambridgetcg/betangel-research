# Forum motifs → offline prototype contracts

設計日期：**2026-10-09**。呢份係獨立於 E01 嘅 prototype 規格：將現有論壇觀察變成可重複檢查嘅 decision rules、資料要求同反證條件。唔係盈利策略、原作者實作重建或真人心理模型。三個離線 prototypes 已實作；[工程結果](../reports/motif-prototypes-2026-10-09.md) 分開記錄真實 sample 同 synthetic checks。

[機器可讀 catalogue](../research/motif-catalog.json) 完整保留 **F01–F14**，逐項分開 `participant_mental_model`、`our_inference`、所需資料、假說機制、反機制、操作定義、反證／失效條件同狀態。原始來源摘要仍然係 [observation cards](../research/forum-observations.json)，唔改原帖意思。

## 14 項觀察點樣分工

| ID／來源 | 玩家講法或爭議 | 我哋提煉嘅 motif／去向 |
| --- | --- | --- |
| [F01](https://forum.betangel.com/viewtopic.php?t=20596) | 動態 offers／fills／cancellations 比一張 WOM 快照重要；亦有人重視 matched activity。 | **M01 先量度先解釋 → P01**。Available size 同成交量分開；唔由 aggregate ladder 推斷意圖。 |
| [F02](https://forum.betangel.com/viewtopic.php?t=32435) | 按 race type 選 swing／細幅方法。 | **M02 regime → 延後**。大波幅未必較易估方向，要有當時已知 race type 同對照組。 |
| [F03](https://forum.betangel.com/viewtopic.php?t=19873) | 上一場熱門輸可能影響下一場；原作者亦承認反應不一。 | **M03 跨場事件 → P02**。Provisional alert 同 confirmed result 分開；只 watch activity。 |
| [F04](https://forum.betangel.com/viewtopic.php?p=327753) | 最後兩分鐘追縮價可能見得太遲；短期盈利後回吐。 | **M04 晚到嘅 continuation；M07 研究紀律 → P01/P03**。只檢查訊號之後，保留分析延遲。 |
| [F05](https://forum.betangel.com/viewtopic.php?p=382194) | Best quote 改變後，前後 volume 可能屬於不同價位。 | **M01 → P01 measurement gate**。換 quote 冇新成交，唔可以製造 volume spike。 |
| [F06](https://forum.betangel.com/viewtopic.php?p=387220) | WOM 互補百分比例子有 typo，後來更正。 | **M01 → 定義／fixture**。以明確 back/lay size 驗公式；唔當成軟件 bug 或新 alpha。 |
| [F07](https://forum.betangel.com/viewtopic.php?p=377450) | Tight stops、逆轉同退出跟唔上價格之間有 trade-off。 | **M05 exits → 延後**。係 in-play 討論，不能直接移植到賽前；要另建成交假設。 |
| [F08](https://forum.betangel.com/viewtopic.php?p=162876) | 圓整／fractional odds 係支持阻力、擠塞定雜訊？ | **M06 anchors → 延後**。預先指定價位，同相近 tick geometry 對照。 |
| [F09](https://forum.betangel.com/viewtopic.php?start=1350&t=21616) | 虧損後想換 bot；有人質疑 entry value／exit cost，有人稱 edge 消失。 | **M07 research hopping；M05 exits → 全部 policy 嘅研究控制**。留低失敗 variants，唔將舊 test 再當新證據。 |
| [F10](https://forum.betangel.com/viewtopic.php?t=30424) | 多個 bot／process 可能仍讀同一市場持倉。 | **M08 shared state → 延後工程題目**。目前只輸出離線 records，冇 account／order path。 |
| [F11](https://community.betfair.com/general_betting/go/thread/view/94082/27944985/trading-horses---any-advice?liveView=0&pg=2) | 頭馬 value、價格路徑、discipline 同 knowledge 有不同解釋。 | **M09 target；M07 evidence → 全部 policy**。遵守規則、估中方向同盈利係不同指標。 |
| [F12](https://forum.betangel.com/viewtopic.php?p=393882) | Bot 發佈價格／streak alerts。 | **M03 events；M07 coverage → P02/P03**。Bot posts 唔係多個真人嘅 sentiment votes；缺失 coverage 唔等於冇新聞。 |
| [F13](https://forum.betangel.com/viewtopic.php?t=18049) | 想早過 tipster 跟隨者入場；亦有熱門推介 drift 反例。 | **M03 public events → P02/P03**。Tip 只係候選事件，唔預設縮價、價值或因果。 |
| [F14](https://forum.betangel.com/viewtopic.php?t=26752) | 上一場完結後可能多錢，但亦有人覺得更反覆。 | **M02 regime；M03 linkage → P02，其他延後**。同 time-to-off／正常流動性增長分開。 |

以上係有目的揀選嘅 13 個討論串＋1 個 alerts 頁面，唔代表成功率、主流比例或 14 份獨立證據。同一作者出現幾次仍然係同一人；Bet Angel／Betfair 主持社群，Euler 嘅團隊關係見[原有 affiliation source](https://forum.betangel.com/viewtopic.php?t=5226)。原日期只係論壇顯示日，未重建時區、編輯或當時送達時間。

## P01：價格延續＋可觀察成交活動

**可檢查嘅假說：**價格已出現一個方向嘅變動，而同一段期間有有效嘅 unsigned matched activity，會唔會比單看價格提供更多之後嘅資訊？「有人成交」唔代表已識別 aggressor、買入壓力、內幕或恐慌。

工程例子使用 `mid=(best_back+best_lay)/2`，`shortening=(past_mid-current_mid)/past_mid`；正值標示縮價、負值標示 drift。呢個 midpoint 只係描述指標，唔係可成交價。

暫定 engineering constants：`abs(shortening)>=0.002`、unsigned `volume_delta>=1`、relative spread `<=0.03`、兩邊 available size 都正數、issue 時 market-message age `<10s`。Sampler 本身要求兩個 endpoint market messages age `<=10s`，P01 喺發出時再套嚴格 expiry。呢個係 **market message** age，唔保證每個 quote／volume counter 都喺十秒內更新；delta feed 保留未變欄位，輸出另有各欄位 observation times。呢啲係方便 synthetic fixtures 檢查嘅數字，**未調參、未驗證亦唔係推薦落注門檻**。成交數值必須有一致貨幣、同一 runner counter 及 window；missing／reset／負修正未釐清就 abstain，唔補零。

輸出只係 continuation-pattern label／direction 或 abstain，加原因及 input evidence。冇 stake、entry／exit order、fill 或 P&L。價格動咗而 matched counter 冇增加，activity gate 應失敗；換 best quote 亦唔應憑空增加成交。

**對照及反證：**同一條 price rule 保留 spread／data gates，但移除 activity gate；之後先比較有冇額外資訊。所有 abstain 同失效 window 都保留。若較後完整 cohort 冇達到預先指定嘅增益，或者加真實可觀察延遲後消失，就唔支持呢個版本嘅 edge 假說。兩個版本覆蓋唔同樣本時，不能只比較各自揀中嘅子集。

## P02：public event → activity watch，direction 永遠空白

接受嘅工程事件類型係 `public_tip` 同 `previous_race_finish`。每個 market／cutoff 最多一個 watch，保留所用 event IDs；重覆 alert 唔會增加獨立證據數量。今版 previous-race finish 只接受 supplied `event_status=confirmed`，亦要有不同於 target 嘅 explicit prior-market ID；唔接受 provisional odds proxy，更唔可以用事後賽果取代當時 alert。

只有 source、event version、explicit identity link 同分析都已可用，先發出 watch。Watch 只表示留意其後活動；**`odds_direction=null`**，唔推斷邊匹會縮價。即使某個 timely event 已足夠建立 watch，coverage 不完整仍然要明示，不能進一步推論冇其他新聞。

未來 event study 可以量度其後 unsigned activity、spread 同絕對價格變化，對比預先固定嘅 no-event windows；要配對 time-to-off、前段走勢、odds 同 liquidity。未有完整事件／非事件分母、publication timing 或 matched controls，結果只能係資料可行性，唔係 attention 引致價格嘅因果證據。

**反證：**加入上述對照後冇後續活動增益，或只有補上當時未確認賽果／事後 popular-tip list 先見到效果。Activity 增加本身仍然唔支持方向預測。

## P03：同一 P01，加明確 context abstention

P03 必須重用 **完全相同嘅 P01**；只可保留或 abstain，唔可以反轉方向、追加另一個訊號或靠長篇解釋改玩法。P01 嘅 price／activity／spread component 保持原樣，explicit contextual contradiction 由呢個 overlay 處理。

工程合約要求一份已可用嘅 `kind=context_assessment`、`assessment=compatible`，同時有明確 `coverage_scope`；contradiction、uncertainty、missing、late 或 expired context 都 abstain。`compatible` 只代表指定來源範圍內，按照已凍結規則未發現指定衝突，**唔代表世界上冇壞消息**。例如測試已知事件是否屬於同一 selection、所需 assessment 是否及時、聲稱嘅條件有冇明確反例；唔由一條新聞自行猜贏輸。

Fixtures 嘅 assessment 係 supplied structured input；engine 冇真實 semantic model。所謂 semantic understanding 最終都要操作化成具版本嘅規則、tags、證據同 abstention policy。佢係待驗證嘅判斷過程，唔係無限制 reasoning 自動產生 edge。

**未來 ablations：**完整 P01、相同 P01＋機械 availability／coverage gate、相同 P01＋context overlay，以及相同延遲但冇 context 嘅對照。全部比較同一完整 issuance cohort；另外報 retained／abstained、原因、資料覆蓋同條件分數。若需要 numerical forecasting，要預先指定 abstain 使用嘅固定 fallback，同一市場 runner 集合及可行 probability-share vector，先可以比較整體 loss。不能刪去輸例後話 accuracy 提高。

**反證：**context overlay 冇超越 coverage／delay 對照，優勢只來自挑較易樣本或現代模型嘅事後知識，或者成本／延遲大過所量到嘅研究效益。

## 同一份 packet 嘅時鐘、覆蓋同成本

Synthetic context 必須有 stable ID、`source` identifier、content hash、explicit source-to-market/selection link、explanation 同 alternative interpretation。Context assessment 另需非空字串 list `coverage_scope`；public event watch 不代表 coverage 完整。真正 collector 仲要保存來源 URL／版本同可核對嘅 receipt evidence；engine 只驗 supplied schema／時間，唔會認證 hash 內容或來源真確。事件本身、來源公開、收到資料、entity link 可用同分析完成係不同時刻：

```text
event_ms <= published_ms <= received_ms <= analysis_completed_ms
link_known_ms <= analysis_completed_ms
effective_ready = max(received_ms, link_known_ms, analysis_completed_ms)
effective_ready <= decision_cutoff
```

Expiry 要喺實際 delayed issue 時重新檢查，係 exclusive deadline：取 signal lifespan、scheduled start、market-message age deadline，再按 policy 加 context 自身 expiry／event-age deadline。工程延遲例子係 **0／2／10／30 秒**，同一 frozen packet 用 **15 秒 lifespan**；唔喺延遲後偷偷換最新資料或改 event clock。研究要分開 observed source-to-collection lag、analysis runtime 同人工加入嘅 scenario delay，避免 double counting。

歷史檔如果只有 publisher time 而冇 receipt time，實際 historical latency 應記 unknown。多收集到來源係 **collection coverage**，唔等於比其他參與者早知道。呢個 framework **未量到 speed advantage**，synthetic delay failure 亦唔係市場真實失效比例。

用現代 LLM 讀舊新聞，即使 retrieval 截止喺舊日期，模型本身仍可能知道後來嘅結果；因此不能聲稱係當時可行嘅 semantic backtest。暫時只用 synthetic supplied assessment 驗工程。較強驗證需要 prospective context：固定來源清單，真實記錄 received／analysis completion，先封存 forecast，再等 outcome，並保留 model／prompt／code／config versions。

每輪記錄可用嘅 data、software、hosting、model/API 用量與費用、currency 同 runtime。Unknown cost 留 `null`，唔當免費；費用亦唔可以由一段 hypothetical profit 倒推。LTP／midpoint／pattern labels 冇隱含 fill、cash return 或 ROI。未來若另做執行經濟研究，仲要明確 quotes、depth、queue/fill、delay、退出及 applicable fees。

## 升級條件同目前邊界

1. 先驗身份、時鐘、counter、quote side、coverage、duplicate／expiry 同 abstention fixtures；synthetic pass 只證明預期工程行為。
2. 再確認可取得允許使用、連續、多日期嘅市場及 context 資料，凍結假說、欄位、門檻、全 cohort fallback、ablation、覆蓋要求同分析成本。
3. 按完整賽事／日期做 chronological validation，再用未睇過嘅較後資料或 prospective collection；所有 variants 同 failures 入 ledger。分辨 invalid test、無增益，同資料不足而 inconclusive。

Anchors、race-type regime、退出政策及 shared-account state 保留獨立 backlog，唔硬塞入呢三個 prototype。[E01 原 proposal](../research/experiments/e01-hermes-proposal.json) **保持不變**；既有 sample、forum-derived specification 同 synthetic scenarios 全部屬 inspected engineering material，不能再當 untouched holdout。


## 執行及輸出

```sh
python3 -m betangel_research motifs \
  --input research/fixtures/motif-scenarios.json \
  --out outputs/my-motif-demo
python3 -m betangel_research motifs --format adv \
  --input data/raw/betfair-samples/horse-racing-adv-sample.bz2 \
  --out outputs/my-motif-adv
```

每次用新 directory。`features.jsonl` 保留 quote／volume 同原始 observation times；`decisions.jsonl` 保留每個 policy／delay 嘅 hypothesis、watch 或 abstain，連原因、effective expiry 同 context evidence；`report.json` 記錄 hashes、環境、排除原因及計數。Input context 可用 `--context path/to/context.json` 提供 list，唔會自動連網或 call model。現時 CLI 只做 engineering traces，未提供 outcome scoring；[具體結果及下一個驗證](../reports/motif-prototypes-2026-10-09.md) 另列。
