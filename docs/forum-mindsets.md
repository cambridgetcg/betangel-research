# 論壇入面，交易者點樣諗？

研究日期：**2026-10-09**。讀咗 Bet Angel 同 Betfair Community 嘅指定帖子，整理 **13 個討論串＋1 個自動 alerts 頁面**，涵蓋 2011–2026；唔係逐頁讀完整個論壇，亦唔代表玩家比例或主流共識。作者、原帖日期、反例同研究提案見 [14 張 observation cards](../research/forum-observations.json)。

我哋嘅綜合判斷：值得學嘅係佢哋點將「其他人可能點反應」連到價格、成交同退出條件。論壇講法只證明有人咁諗；對方身份、心理、因果關係同盈利仍然要另外驗證。

## 六種有用嘅思路，同原帖內嘅反例

| 思路 | 原帖講法與分歧 | 對研究有咩用 |
| --- | --- | --- |
| **估下一批買盤由邊度嚟** | Derek27 留意多位 tipster 嘅共同推介，MickeyFinn 想早過跟隨者入場；wearthefoxhat 同時列出熱門推介 drift 嘅例子。2019-01-08，[F13](https://forum.betangel.com/viewtopic.php?t=18049)。 | 將「多人睇」同「發佈之後仍有可預測移動」分開。必須有原始發佈時間同發佈前走勢。 |
| **睇盤口變化，而唔單靠一張快照** | ShaunWhite 談掛單、成交、取消嘅速率；其他人提醒 WOM 大額掛單未必代表持續需求。2020-02-23/24，[F01](https://forum.betangel.com/viewtopic.php?t=20596)。 | 分清 available size 同 matched volume；盤面淨變化不能直接當成新單、取消量或某類玩家嘅意圖。 |
| **先問市場適唔適合呢種方法** | Euler 傾向按賽事類型預期大走勢或穩定價格；另一串談上一場完結後流動性變化，但 cookyweb 覺得嗰段反而難捉摸。2026-09-14，[F02](https://forum.betangel.com/viewtopic.php?t=32435)；2022-12-29/30，[F14](https://forum.betangel.com/viewtopic.php?t=26752)。 | 波幅大唔等於方向易估。Race type、liquidity、時間要分開檢驗；呢啲暫時係新研究題目。 |
| **留意跨場注意力** | northbound 提議上一場熱門輸咗，下一場熱門可能吸引買盤；佢亦承認反應不一，而且高 in-play odds 未必代表最終輸。2019-10-29 至 11-07，[F03](https://forum.betangel.com/viewtopic.php?t=19873)。 | 要分開「當時見到嘅 provisional signal」同「之後確認嘅結果」。不能用遲來嘅結果去解釋更早入場。 |
| **見到條 trend，可能已經遲咗** | Snaff 追最後兩分鐘縮價馬，稱一週贏幅下一週消失；ShaunWhite 指出追到資金方向時，好價可能已過。2023-05-02，[F04](https://forum.betangel.com/viewtopic.php?p=327753)。 | 只計 signal 之後嘅變化，不能將觸發 signal 嗰段行情當成預測成功。亦不能由一個短測試斷言全部 momentum 無效。 |
| **退出政策本身有代價** | dobbs 想限制逆轉損失；Dallas 談價格跳過退出單，ShaunWhite 則提醒過緊 stop 可能反覆實現本來會回復嘅虧損。2025-06-03，[F07](https://forum.betangel.com/viewtopic.php?p=377450)。 | 將訊號質素、退出是否成交、退出後淨期望值分開。呢段係 in-play 討論，唔直接當成 E01 賽前策略結論。 |

亦有真實分歧：有人將入場錯誤理解成 discipline，另有人覺得係訊號／經驗問題。[Betfair Community，2011-07-21，F11](https://community.betfair.com/general_betting/go/thread/view/94082/27944985/trading-horses---any-advice?liveView=0&pg=2)。我哋應先量度「有冇按預定規則做」同「規則本身有冇增益」，唔將每次虧損都歸因於心態。

## 最直接改善工程嘅兩個例子

**Volume Tracker（2025-09-28）**：Emtaxx 用當下 best lay price 嘅成交量做差值，jamesedwards 指出 quote 一變，前後可能已係不同價位。呢個係具體 measurement bug 風險；後來嘅成功自述冇完整回報序列支持。[F05](https://forum.betangel.com/viewtopic.php?p=382194)

我哋新增嘅驗證要求係：將 best quote 移動、但冇新增成交嘅情境，列入未來 parser fixtures；量度唔應憑空生出成交量 spike。呢次未實作或執行 fixture。

**Condition advice（2026-03-02）**：Dallas 嘅 WOM 百分比例子出現互補數字不一致，用戶指出後佢更正。呢個係文字例子嘅修正，唔係已證實軟件 bug。[F06](https://forum.betangel.com/viewtopic.php?p=387220)

未來 fixture 要用明確 back/lay size 驗證 feature 正負方向，唔依賴論壇口語嘅「back pressure／lay side」去估數據定義。

另外，[Running Multiple Bots，2025-03-20，F10](https://forum.betangel.com/viewtopic.php?t=30424) 嘅討論提醒：多個 bot 可能共用市場持倉／greening 狀態。任何未來 agent execution integration 都要另驗 order ownership；開多個 process 唔係隔離證據。今次只記錄工程題目。

## 點樣接返我哋嘅 framework

**E01 保持原 proposal 不變**：price/quote baseline 對比 joint depth＋volume，固定 lookback/horizon；冇因為論壇故事加入新 feature 或改時間窗。[原 proposal](../research/experiments/e01-hermes-proposal.json)

現在採納嘅係量度要求：價格身份一致、WOM 方向清楚、signal 後先開始計預測效果、所有 variant 入 ledger。Tipster attention、跨場事件、race-type regime 同圓整價位只列 backlog，需要各自註冊實驗。原本 joint test 即使成功，都不能單獨證明 depth 或 volume 邊樣有效。

心理敘事亦可以互相矛盾：同一個 drift 可以被說成「恐慌超賣」或「有人知道壞消息」。用重複可觀察嘅定義、對照組同較後日期驗證，先有機會分辨；事後幫每條走勢配故事唔算預測。

## 證據限制與交付

Bet Angel 係軟件廠商主持嘅社群，Betfair Community 由交易所主持。Euler 嘅團隊關係有[公開說明](https://forum.betangel.com/viewtopic.php?t=5226)；佢嘅意見唔屬於獨立廠商績效驗證。其餘作者身份／利益關係未全面核實。被引用多次嘅同一作者，唔會當成多份獨立證據。

[F12 alerts 頁面](https://forum.betangel.com/viewtopic.php?p=393882) 係 BetAngelBot 發嘅價格與 streak 通知，唔係多個人嘅 sentiment votes；亦未建立完整 alerts 分母、歷史送達時間或 predictive value。所有 forum dates 只保留顯示日期，未推斷 UTC。舊帖不能自動代表 2026 年市場。

採用嘅 14 個頁面都由 coordinator 打開公開正文；部分其他候選頁面回傳 403，登入／附件內容未讀取。只保存精簡 paraphrase、連結、日期同研究提案，冇複製論壇全文或下載 automation 檔。

今輪包括一個 Hermes Astra session（requested ultra，installed route sends max）及一個 native researcher。[Handoff 與來源接受紀錄](../.team-work/forum-mindsets-20261009/handoff.json)。獨立 review 同實際文件檢查會記錄喺 [batch brief](../.team-work/forum-mindsets-20261009/brief.md)。今次冇新 backtest 或盈利驗證。
