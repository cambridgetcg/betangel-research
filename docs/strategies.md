# What people do with Bet Angel

Reviewed **2026-10-09**. These are mechanisms and research hypotheses, not proven recommendations. Bet Angel's [own risk notice](https://www.betangel.com/user-guide/risk_notice.html) says specialist software does not guarantee profitability.

## Approaches and where an advantage would have to come from

| Approach | Mechanism | Potential edge to test | Main ways it fails |
| --- | --- | --- | --- |
| **Pre-off scalping / spread capture** | Obtain opposing fills a small favourable price distance apart. | Fill quality and market selection in relatively stable conditions. | Only one side fills; adverse moves erase many small gains; queue priority and partial fills matter. [Official example](https://www.betangel.com/user-guide/example_rules.html). |
| **Pre-off swing trading** | Anticipate odds shortening or drifting and close after a larger move. | Predictive information in price/volume changes and race context beyond a simple baseline. | False breakouts, reversals, stale signals and unachievable exits. [Official placement examples](https://www.betangel.com/quicker-bet-placement/). |
| **In-play back-to-lay / dobbing** | A horse backed before the race may be laid at shorter odds while running; front runners are a common hypothesis. | Predict the future tradable path more accurately than the initial price already implies. | It never shortens, the historical low was not available to the order, or video/order delays prevent closing. [Official front-runner explanation](https://www.betangel.com/front-runner-strategies-automation/). |
| **Fundamental value modelling** | Compare a pre-race probability estimate with obtainable odds. | Incremental information from prior performances, pace or conditions after controlling for the market. | Leakage, revised ratings, poor calibration, overfitting and using prices that were not available then. Bet Angel can [import CSV model outputs](https://www.betangel.com/import-selections/); integration capability is not validation. |
| **Dutching / bookmaking** | Spread exposure over several runners or outcomes. | The prices must be favourable relative to probabilities; allocation alone adds no predictive information. | An uncovered outcome wins or only part of the intended portfolio fills. [Official dutching guide](https://www.betangel.com/user-guide/advanced_dutching___back_with_a_stake_of.html). |
| **Guardian automation** | Apply predefined rules consistently across many markets. | Carries an existing signal or execution advantage into repeated implementation. | Repeats a losing rule efficiently; stale inputs, duplicate triggers and unmatched exposure. It is a method of implementation, not a separate statistical edge. [Official introduction](https://www.betangel.com/user-guide/getting_started_2.html). |

## Understanding trading without predicting the winner

A purely hypothetical arithmetic example: a back stake of £100 at decimal odds 5.0, followed by a fully matched lay stake of £125 at 4.0, yields £25 gross under either result. If the horse wins: £400 back profit minus £375 lay liability. If it loses: minus £100 plus £125. This ignores commission and settlement adjustments.

The difficulty is obtaining the second fill at the favourable price. If odds rise instead, closing can realize a loss; leaving exposure open changes the risk. The arithmetic after two fills does not imply an opportunity existed beforehand. There is no current horse selection or live instruction in this example.

## What a backtest must not silently assume

- **A touched price is not a fill.** [Bet Angel's EPIQ documentation](https://www.betangel.com/user-guide/columns_2.html) describes an estimated queue position, affected by cancellations and cross-matching. Historical ladders do not reveal precisely where our hypothetical order would stand.
- **Practice is simulation.** [Practice Mode](https://www.betangel.com/user-guide/practice_mode.html) matches locally and cannot reproduce the effect of actual orders on a market. Extended practice/data-only usage may also lead to delayed data; it is not an unrestricted free research feed.
- **Match resolution to the question.** The [official historical specification](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf) distinguishes minute-level Basic last-traded prices from one-second Advanced depth/volume and tick-level Pro data. Basic cannot establish scalp fills. More detailed data still does not establish counterfactual execution or market impact.
- **Execution quality is part of the edge.** Report missed opportunities, unmatched/partial positions, latency and forced exits rather than deleting inconvenient cases.

## Research priority for this repository

Start with slower **pre-off movement** and **pre-race probability calibration** studies on the Mac. They allow clearer feature timestamps and baseline comparisons than an initial queue-sensitive scalping simulator. This is a judgement about research feasibility, not an estimated ROI ranking.

The existing sectional pilot connects to the probability-calibration track, but only earlier races may inform a future target. No approach above has passed an out-of-sample economic test in this repository. 24/7 operation changes the amount of exposure; it does not turn a negative expected return positive.
