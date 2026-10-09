# Public historical data feasibility audit

Audit date: **2026-10-09**. Actual files downloaded and parsed locally; no account, payment, private endpoint or order connection was used. These are format, coverage and linkage observations, **not an edge or profitability result**.

## Acquired files and provenance

Three publisher-linked horse-racing samples were obtained from the [official historical help page](https://historicdata.betfair.com/help). Its current public frontend links the following files directly:

- [Basic sample](https://historicdata.betfair.com/files/horse-racing-basic-sample.bz2)
- [Advanced sample](https://historicdata.betfair.com/files/horse-racing-adv-sample.bz2)
- [Pro sample](https://historicdata.betfair.com/files/horse-racing-pro-sample.bz2)

They are **bz2-compressed JSONL**, with one market-change message per line, without a TAR wrapper. The compressed raw files live in `data/raw/betfair-samples/` and are ignored by Git. SHA-256, exact retrieval UTC, HTTP metadata, sizes, field counts and per-market coverage are in [the sample manifest](../data/manifests/betfair-public-samples-2026-10-09.json).

| Package | Download bytes | Decompressed bytes | JSONL messages | Markets |
| --- | ---: | ---: | ---: | ---: |
| BASIC | 79,576 | 1,609,190 | 1,251 | 34 |
| ADV | 8,487,377 | 84,791,246 | 71,764 | 34 |
| PRO | 16,812,379 | 206,028,882 | 407,927 | 34 |

Two small [official SP archive](https://promo.betfair.com/betfairsp/SP_history.html) CSVs were also inspected for identity/coverage feasibility:

- `data/raw/betfair-sp/dwbfpricesirewin29042017.csv`: 21,960 bytes; 100 rows. [Manifest](../data/manifests/betfair-sp-ire-win-2017-04-29.json).
- `data/raw/betfair-sp/dwbfpricesirewin30042017.csv`: 23,599 bytes; 112 rows. [Manifest](../data/manifests/betfair-sp-ire-win-2017-04-30.json).

Total acquired data: **25,424,891 bytes**, below the 50 MB task cap. The manifests contain source URLs; the raw files are not repository distributables. No historical store login or authenticated bulk API was attempted.

## Scope and independence

All three exchange samples describe the **same Punchestown meeting in Ireland on 29 April 2017**. Each has 34 related markets: 8 WIN, 7 PLACE, 12 OTHER_PLACE and 7 EACH_WAY. There are only eight scheduled races and one meeting, not 34 independent events. The stream's `marketDefinition.eventId` is `28209874` for the whole meeting.

Publish timestamps span 27 April 2017, approximately 11:34 UTC, to 29 April 2017 at 17:56:27.745 UTC; Basic begins about one minute later. All lines parsed as JSON, with no backward publish timestamps. Pro has 299 equal adjacent publish timestamps. Samples are selected, old, and confined to one meeting: they are unsuitable for population ROI claims, a chronological independent holdout, or estimating current market conditions.

## Observed schema and timing

All packages use top-level `op`, `clk`, `pt`, and `mc`; `mc` contains market IDs, definition updates and runner changes. `pt` is the publisher's epoch-millisecond timestamp. It can be a **source-availability proxy**, but there is no local `received_at`, no network latency and no physical event-arrival timestamp. `marketTime` is a scheduled UTC start. Actual first observed `inPlay` differs between packages because updates are sampled/conflated.

- **Basic:** runner changes contain `id` and `ltp`. No depth or traded-volume fields were observed.
- **Advanced:** observed runner keys are `id`, `batb`, `batl`, `ltp`, `tv`, `trd`. Depth updates are level/price/volume triples. There are **174,290 non-positive LTP entries**, all zero, across runner changes; treat them as unavailable prices, not valid odds.
- **Pro:** observed `id`, `atb`, `atl`, `trd`, `ltp`, `tv`, `spn`, `spf`, `spb`, `spl`. Price ladders use price/volume pairs. There are **274 explicit `img:true` market images**; all include a market definition, but 228 omit LTP for at least one ACTIVE runner. A replay must replace the prior cache and accept incomplete snapshots, not carry old prices across the image to manufacture completeness.

The historical [feed specification](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf) describes nominal package resolution and delta semantics. Actual messages are sparse and sometimes include metadata changes at shorter intervals; the minimum global time step is not the effective per-runner sampling rate. Basic [LTP is sent only when it changes](https://support.developer.betfair.com/hc/en-us/articles/25918481395740-How-is-the-BASIC-Historical-Data-last-price-traded-ltp-updated). A missing field is not a new observation, and no update is not proof of a frozen real market.

Order IDs, our hypothetical queue position, orders ahead of us and counterfactual market impact are absent. A traded price or displayed quantity does not prove an order would have filled. No receipt-time speed advantage can be measured from these files.

## Entity linkage findings

Joining the SP file dated **30 April** to the Pro WIN markets matches **112 settled runner rows across eight races**, with no name discrepancies. The tested key is `(CSV EVENT_ID, CSV SELECTION_ID) == (stream market ID without its leading 1., stream runner id)`. This is a verified mapping **for this sample**, not a universal identifier contract. Despite its header, CSV `EVENT_ID` does not match stream `marketDefinition.eventId` here.

The stream includes **14 additional REMOVED runners** absent from that SP CSV. Do not treat this as 14 unexplained missing outcomes or silently discard it from a coverage denominator. Names alone are an inadequate long-term identity key. This overlap checks two products from the same provider; it does not establish a cross-provider linkage advantage or horse histories.

Other traps:

- Filename `29042017` contains event dates **28 April**; filename `30042017` contains **29 April**. Use the row's event date, not the filename, to establish chronology.
- CSV `EVENT_DT` has no offset. All 112 linked rows are one hour ahead of UTC `marketTime`, consistent with Europe/London summer time on this date. A global timezone policy still requires verification.
- Of 88 rows with BSP present in the final Pro definition, 55 differ from CSV BSP by more than 0.000001; every difference is below 0.01. The stream appears to truncate these values to two decimals. Exact floating-point BSP equality would reject valid joins.

The detailed linkage counts and interpretation are recorded in the sample manifest.

## SP fields and unavailable backtests

Actual CSV columns are `EVENT_ID`, `MENU_HINT`, `EVENT_NAME`, `EVENT_DT`, `SELECTION_ID`, `SELECTION_NAME`, `WIN_LOSE`, `BSP`, `PPWAP`, `MORNINGWAP`, `PPMAX`, `PPMIN`, `IPMAX`, `IPMIN`, `MORNINGTRADEDVOL`, `PPTRADEDVOL`, `IPTRADEDVOL`.

These are settled results and retrospective aggregates, without observation timestamps or order-book paths. In particular, **daily min/max cannot establish the sequence of prices, entry/exit availability, liquidity or execution**. Final BSP, target-race results and later aggregate volume cannot be earlier predictive inputs. Publication time cannot be recovered from a historical retrieval timestamp. The archive also documents a BSP sentinel of 1001 when no backers existed at SP; a larger ingestion must handle that explicitly.

## Access and reuse limits

Public access is not an open-data licence. The SP history page expressly reserves copyright/database rights and requires a licence for use. No reuse or redistribution licence was established here, and the SP files are **not used as a strategy benchmark**. The historical portal publishes sample downloads for inspection, but that does not grant broad republication or commercial use. Keep raw data local and ignored; any expansion or distributable dataset needs its actual provider terms checked.

The ordinary [historical API](https://support.developer.betfair.com/hc/en-us/articles/12859956891932-How-Can-I-Make-HTTP-Requests-to-the-Historical-Data-API) requires a Betfair session token and files previously acquired through the store. That route was not used.

## What these files can establish

They can exercise real-format streaming ingestion, timestamp cutoffs, missing-price handling, coverage reports and cautious identity mapping. Optional reduced-data or artificial-delay experiments can check framework behaviour and sensitivity, but must label assumptions and cannot establish real latency advantage. They cannot establish a profitable strategy, an independent predictive edge, realistic fill rates or current UK-wide coverage.
