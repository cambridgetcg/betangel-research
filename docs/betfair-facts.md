# Betfair facts: what the code relies on, and how sure we are

Prepared **2026-10-10**. Fact register for `betangel_research/betfair`, not evidence of profitability. No credentials existed and no fact below has been checked against the live Betfair API.

**每個外部事實都要有出處、等級同處理方法；未驗證就照寫未驗證，代碼要預最壞情況。**

呢份係 [`research/betfair-facts.json`](../research/betfair-facts.json) 嘅人讀版本。[接入指南](betfair-integration.md) 同 [落單路徑](betfair-order-path.md) 入面嘅 F-id 全部指返呢度。`constants.PROVENANCE` 將每個 public constant 對應到呢度嘅 F-id。冇任何程式自動生成呢份文件：改事實要先改 JSON，再改返呢度嗰一行；`tests/test_betfair_facts_doc.py` 會逐格對比兩邊，唔夾就 fail。

## How to read a row

- **value used**：代碼用嘅講法，paraphrase 唔係原文引用，規則係最多一句（spec §15）。呢度同 JSON 入面嘅 "spec section N"／"spec §N" 指嘅係 binding spec [`final.txt`](../.team-work/betfair-exchange-20261010/final.txt) 嘅章節號，唔係呢幾份 docs 自己嘅章節號。要倚賴之前請睇返原頁。
- **仲係兩句、等 JSON 擁有者收窄嘅 value：** F03、F10、F18、F25、F37、F63。第二句應該搬去 code handling 或者 "Conflicts and open questions"；`test_betfair_facts_doc` 會對呢張清單同 JSON，而且唔准再多。
- **evidence_type · status**：證據類型，有第二個較弱來源就加 `secondary`；`·` 後面係 verification status。
- **retrieval**：點攞到。`official_rest_api_storage_read` 係經 Confluence REST API 讀全文；`zendesk_api_read` 係經 Zendesk API 讀 support article；`wayback_snapshot_<ts>` 係 live 頁返 HTTP 403，改讀存檔。一條事實嘅幾部分用唔同方法攞到嘅時候，JSON 嘅 retrieval 係一個 list，按 value 入面嘅次序（表入面用 ` + ` 連住）；引用 `local:` 來源嘅事實一定列 `local_python_check`。
- **source**：`DD/<id>` = `https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/<id>`；`SA/<id>` = `https://support.developer.betfair.com/hc/en-us/articles/<id>`。舊 `docs.developer.betfair.com` 網址而家全部 301 去 atlassian。
- **published_or_updated**：頁面自己寫嘅日期；冇寫就係 `not stated`。**accessed** 全部係 2026-10-10。
- **code handling**：代碼點樣處理，包括唔確定時揀咗邊個較保守嘅做法。

## Evidence labels

Betfair 係 exchange operator，所以 Betfair 自己嘅頁面算 `official_*`；Apple 嘅 man page 係 macOS 工具嘅官方文件，所以都算 `official_*`（F68、F74；F73 man page 冇寫，所以係 `unverified`；F74 嘅 SQLite 部分係本地檢查，結構欄位都標咗：`secondary` `inferred`，retrieval `man_page_read` + `local_python_check`）。`vendor_*` 只指 Bet Angel。`first_person_observation` 留畀我哋自己 first-live-test（T0–T10；T11 跟 live replace 一齊剔走）嘅收據，而家一條都未有。冇人獨立重做過任何結果，所以呢度冇任何事實標成「已獨立核實」。

| field | allowed values |
|---|---|
| evidence_type | `official_documentation`, `official_current_policy`, `official_data_specification`, `vendor_documentation`, `vendor_current_pricing`, `forum_participant_opinion`, `self_report_unverified`, `primary_research_simulation`, `inferred`, `unverified`, `first_person_observation` |
| retrieval | `official_rest_api_storage_read`, `official_page_read`, `official_four_page_pdf_read`, `zendesk_api_read`, `reader_proxy_read`, `wayback_snapshot_<ts>`, `man_page_read`, `local_python_check` |
| verification_status | `documented`, `documented_corrected`, `partially_unverified`, `conflicting`, `unverified`, `unknown`, `inferred`, `local_check` |

## Facts by area

一共 77 條（F01–F77），每條只出現喺一個表。

### `auth` · 登入、session 同 login 保護

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F01 | Certificate login is POST https://identitysso-cert.&lt;tld&gt;/api/certlogin with the client certificate presented in the TLS connection; tld is com, com.au (Australian and New Zealand customers), it, es or ro. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687915](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915) | 2026-07-01 | 2026-10-10 | constant (SSO_CERT_HOSTS, SSO_PATHS) |
| F02 | certlogin takes a form-urlencoded username and password body; X-Application is sent but its value is not validated. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687915](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915) | 2026-07-01 | 2026-10-10 | constant |
| F03 | certlogin answers {sessionToken, loginStatus}; a failure carries loginStatus only. Dropping the sample's curl -k (keeping certificate verification on) is our own advice. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687915](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915) | 2026-07-01 | 2026-10-10 | parser; verification stays on |
| F04 | certlogin documents 45 loginStatus values and the list may grow. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687915](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915) | 2026-07-01 | 2026-10-10 | any unknown status is a failure |
| F06 | Interactive login is POST https://identitysso.&lt;tld&gt;/api/login with mandatory Accept: application/json; the response is {token, product, status, error}; LIMITED_ACCESS still returns a token; exceptional codes are listed outside FAIL. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687772](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687772) | 2026-04-28 | 2026-10-10 | can_bet=False for LIMITED_ACCESS; match exceptional codes on error |
| F07 | keepAlive and logout live at &lt;sso&gt;/api/keepAlive and &lt;sso&gt;/api/logout with Accept: application/json, X-Authentication and X-Application, and answer {token, product, status, error}. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687869](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687869) | 2026-09-08 | 2026-10-10 | constant |
| F08 | The HTTP method for keepAlive and logout: the page says POST but its curl sample sends GET. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687869](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687869), [DD/2687786](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687786) | 2026-09-08 | 2026-10-10 | config session.sso_http_method (default POST); first-live-test T2 |
| F09 | Sessions last 24 h for UK/IE customers and 12 h for other .com customers, 20 min on IT/ES; API calls do not extend a session; Logout Preferences can change it. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687869](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687869), [developer forum](https://forum.developer.betfair.com/forum/developer-program/announcements/34635-change-to-login-session-expiry-time-24-to-12-hours-week-commencing-10th-may-2021) | 2026-09-08 | 2026-10-10 | keepAlive bounds per jurisdiction; NO_SESSION halves the interval |
| F10 | More than 100 successful logins per minute blocks new logins for 20 minutes with TEMPORARY_BAN_TOO_MANY_REQUESTS; existing sessions stay valid. Whether failed logins count is unclear. | `official_documentation` · partially_unverified | `official_rest_api_storage_read` | [DD/2687869](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687869), [SA/115003887351](https://support.developer.betfair.com/hc/en-us/articles/115003887351-Why-am-I-receiving-TEMPORARY-BAN-TOO-MANY-REQUESTS-when-attempting-to-Login) | 2026-09-08 | 2026-10-10 | local login caps and a persisted ban (spec section 6) |
| F67 | A new login does not invalidate other sessions, so Bet Angel and the website can coexist with our session. | `inferred` · inferred | `official_rest_api_storage_read` | [DD/2687730](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687730) | not stated | 2026-10-10 | --bet-angel-paused required until first-live-test T3; the kill logs in a separate emergency session and prints the F67 warning while coexistence is unverified |
| F69 | ssl.SSLContext.load_cert_chain(certfile, keyfile, password) loads the client certificate and urllib.parse.urlencode returns str that needs .encode() before sending; checked locally on Python 3.14.6 with synthetic files, never against Betfair. | `inferred` · local_check | `local_python_check` | `local:python-3.14.6` | not stated | 2026-10-10 | as specified; make_tls_context never lets OpenSSL prompt for a passphrase |

### `credentials` · macOS Keychain 同 security(1)

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F68 | security add-generic-password with -w as the last option prompts for the value; find-generic-password -w prints only the password. | `official_documentation` · partially_unverified | `man_page_read` | `man:security(1)` | not stated | 2026-10-10 | exit codes not relied on, except that anchor_read treats exit 44 (errSecItemNotFound) from both lookups as 'no anchor' and fails closed on any other result; exit 44 is inferred from the OSStatus, not in the man page (unverified); first-live-test T0 |
| F73 | security add-generic-password -T "" creates an item that no application may read silently, so every read prompts. | `unverified` · unverified | `man_page_read` | `man:security(1)` | not stated | 2026-10-10 | printed by keychain-setup; first-live-test T0 |

### `config` · 設定檔同 jurisdiction

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F12 | New Zealand customers use api.betfair.com.au; Italian and Spanish accounts use api.betfair.com. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687158](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687158), [DD/2687808](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687808) | 2026-07-31 | 2026-10-10 | api_host allowlist; api.betfair.com.au only with jurisdiction com.au (the AUS/NZ SSO hosts) |
| F24 | customerStrategyRef is at most 15 characters. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496) | 2026-07-31 | 2026-10-10 | pattern ^[A-Za-z0-9-]{1,15}$ |

### `transport` · HTTP transport

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F13 | One session token can be shared across calls; request gzip and keep-alive; idle connections close after 3 minutes; do not send Expect: 100-Continue. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687730](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687730), [DD/2699882](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2699882) | 2026-08-19 | 2026-10-10 | idle_reconnect_s=170; no Expect header |

### `api` · JSON-RPC read path

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F11 | Betting and Accounts JSON-RPC endpoints are https://api.betfair.com/exchange/betting/json-rpc/v1 and /exchange/account/json-rpc/v1 with methods SportsAPING/v1.0/&lt;op&gt; and AccountAPING/v1.0/&lt;op&gt;, POST, and X-Application, X-Authentication and Accept: application/json headers. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687786](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687786), [DD/2687725](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687725) | 2026-07-14 | 2026-10-10 | constant (paths, method prefixes, READ_METHODS) |
| F14 | JSON-RPC errors carry error.data.exceptionname naming an APINGException with errorCode; code -32099 carries ANGX-00nn in the message. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2699907](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2699907) | 2014 | 2026-10-10 | spec section 7 precedence; first-live-test T4 |
| F16 | Market data request weights and the 200-point per-request rule (TOO_MUCH_DATA above it). | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687478](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687478) | 2025-05-21 | 2026-10-10 | weight planner with conservative depth multiplier |
| F17 | Call listMarketBook at most 5 times per second per market (guidance); request OPEN and CLOSED markets separately; at most 250 betIds/marketIds. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687510](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687510) | 2024-06-04 | 2026-10-10 | throttle 4/s per market; single-id re-request of missing ids |
| F18 | listMarketBook with order projections, listCurrentOrders and listMarketProfitAndLoss share a 3-request concurrency limit per account; listClearedOrders is separate. The two sources disagree on whether queued or executing requests count. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687450](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687450), [SA/360000406111](https://support.developer.betfair.com/hc/en-us/articles/360000406111-Why-am-I-receiving-the-TOO-MANY-REQUESTS-error) | 2023-06-01 | 2026-10-10 | local in-flight cap 2 |
| F34 | listCurrentOrders takes betIds/marketIds (250 combined), includeSourceId and dateRange (not the legacy placedDateRange); pages hold 1000 with moreAvailable; BY_BET ordering is deprecated. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687504](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687504), [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | not stated | 2026-10-10 | BY_PLACE_TIME with dateRange |
| F43 | MarketBook gating fields include isMarketDataDelayed and runner statuses; REST MarketBook has no bettingType, marketType, eventTypeId, ladder type or marketTime. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | not stated | 2026-10-10 | catalogue join; V9, V10 and A18 |
| F65 | getAccountFunds returns availableToBetBalance, exposure, retainedCommission, exposureLimit, discountRate and pointsBalance; the sign of exposure is unverified. | `official_documentation` · partially_unverified | `official_rest_api_storage_read` | [DD/2687852](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687852) | not stated | 2026-10-10 | abs(exposure); first-live-test T7 |
| F70 | RANK is not a MarketSort member; listMarketCatalogue maxResults is at most 1000; CLOSED markets are not returned by the catalogue. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687517](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687517), [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | not stated | 2026-10-10 | never send sort RANK; cache catalogue rows |
| F72 | The filter parameter of listEventTypes and listMarketCatalogue is required. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687448](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687448), [DD/2687517](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687517) | not stated | 2026-10-10 | always send {} or refuse None |

### `filters` · REST 同 stream market filter

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F75 | REST MarketFilter keys (marketCountries, marketTypeCodes, marketStartTime) differ from stream keys (countryCodes, marketTypes, no time range). | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465), [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396) | not stated | 2026-10-10 | filters.py mapping |

### `errors` · 錯誤分類

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F15 | APINGException codes, JSON-RPC codes -32700/-32601/-32602/-32603 and DSC transport fault codes. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687450](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687450), [DD/2686993](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2686993) | 2023-06-01 | 2026-10-10 | (surface, code) taxonomy |
| F45 | PERMISSION_DENIED wording about the delayed key differs between the enum page and the support article. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455), [SA/360015530311](https://support.developer.betfair.com/hc/en-us/articles/360015530311-Why-am-I-receiving-the-error-PERMISSION-DENIED-ACCESS-DENIED-when-attempting-to-place-a-bet) | not stated | 2026-10-10 | terminal_account; never probed (v1 never orders on the delayed key) |

### `esa` · Exchange Stream API protocol

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F48 | The stream is a TLS socket of CRLF-delimited JSON at stream-api.betfair.com:443; the first message must be sent within 15 s; connection, authentication and status message fields; ladderLevels 1-10 in the Swagger. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [SA/12937897844252](https://support.developer.betfair.com/hc/en-us/articles/12937897844252-Does-the-Stream-API-allow-Web-Socket-connections), [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json) | 2026-05-11 | 2026-10-10 | constants (STREAM_HOSTS, STREAM_PORT, timeouts) |
| F49 | Stream error codes; every error except SUBSCRIPTION_LIMIT_EXCEEDED closes the connection (INVALID_REQUEST appears in the Swagger only). | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json) | 2026-05-11 | 2026-10-10 | stream_status error surface |
| F50 | heartbeatMs may be requested as 500-5000 and is echoed as 500-30000; a heartbeat line carries clk and is sent only when nothing else was sent in the interval. | `official_documentation` · conflicting | `official_page_read` | [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json), [SA/360000402611](https://support.developer.betfair.com/hc/en-us/articles/360000402611-Market-Streaming-How-do-the-Heartbeat-and-Conflation-requests-work) | not stated | 2026-10-10 | use the echoed value; conflation-aware liveness; first-live-test T6 |
| F51 | conflateMs may be requested as 0-120000; the delayed key gets 180000; the default of 0 is inferred from SUB_IMAGE examples only. | `official_documentation` · documented_corrected | `official_page_read` | [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json), [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396) | not stated | 2026-10-10 | null default; record the echoed value |
| F53 | clk and initialClk support resubscription (RESUB_DELTA) and INVALID_CLOCK exists; pt ordering is guaranteed. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [SA/360000391612](https://support.developer.betfair.com/hc/en-us/articles/360000391612-Market-Streaming-how-do-I-managed-re-connections), [SA/25873962091420](https://support.developer.betfair.com/hc/en-us/articles/25873962091420-Are-Stream-API-messages-guaranteed-to-be-delivered-in-strict-sequential-order-according-to-pt) | 2026-05-11 | 2026-10-10 | fresh image per segment; never resume with clocks |
| F56 | The live-key stream connection limit is not published. | `unverified` · unknown | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396) | not stated | 2026-10-10 | read connectionsAvailable at authentication |

### `book` · stream market cache

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F54 | ChangeMessage, MarketChange and RunnerChange fields; deletion semantics; GBP sizes; Infinity sizes; settlement zeroing; duplicate markets keep the higher version. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json), [SA/360003061798](https://support.developer.betfair.com/hc/en-us/articles/360003061798-Why-am-I-receiving-Infinity-size-from-the-Streaming-API), [SA/4802553453457](https://support.developer.betfair.com/hc/en-us/articles/4802553453457-Why-does-the-penultimate-rc-have-all-runner-volumes-of-Price-Points-Traded-Available-To-Back-Lay-set-to-zero) | 2026-05-11 | 2026-10-10 | cache rules in book.py |

### `recorder` · stream recorder

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F46 | The live key costs a one-off, non-refundable £499; it needs a KYC-verified, funded account; read-only use of the live key is not permitted. | `official_current_policy` · documented | `zendesk_api_read` | [SA/115003864531](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access), [SA/25033076334748](https://support.developer.betfair.com/hc/en-us/articles/25033076334748-What-is-read-only-Betfair-API-access), [DD/2687105](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687105) | 2026-10-08 | 2026-10-10 | recorder.allow_live_key=false by default |
| F52 | Segmentation is size-based; whether segmentationEnabled:false reliably prevents segments is unverified. | `official_documentation` · partially_unverified | `zendesk_api_read` | [SA/360000402331](https://support.developer.betfair.com/hc/en-us/articles/360000402331-How-does-segmentation-work-benefits), [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396) | not stated | 2026-10-10 | close the segment if a segmented line is seen |
| F55 | The default subscription limit is 200 markets; closed markets are evicted about 1 h 5 min after close. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [SA/11741143435932](https://support.developer.betfair.com/hc/en-us/articles/11741143435932-Are-closed-markets-auto-removed-from-the-Stream-API-subscription) | 2026-05-11 | 2026-10-10 | pre-count; max_markets 150 (ceiling 180); stop on SUBSCRIPTION_LIMIT_EXCEEDED |
| F59 | Historical data uses the stream format; the ADV package carries batb/batl, trd, tv and ltp at 1 s. | `official_data_specification` · documented | `official_four_page_pdf_read` | [SA/360000402211](https://support.developer.betfair.com/hc/en-us/articles/360000402211-How-do-I-download-and-view-Betfair-Historical-Data), [historical data spec PDF](https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf) | not stated | 2026-10-10 | ADV-like recorder fields by default |

### `validate` · 落單前檢查

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F20 | Per-request instruction limits: place 200 (50 on the Italian exchange); cancel, replace and update 60; exceeding them gives INVALID_INPUT_DATA. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496), [DD/2687491](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687491), [DD/2687487](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687487) | 2026-07-31 | 2026-10-10 | V7; cancel requests hold at most 60 bet ids |
| F21 | customerRef is at most 32 characters from letters, digits and : - . _ + * ; ~ and de-duplicates resubmissions within 60 s. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496) | 2026-07-31 | 2026-10-10 | unique per request and never reused |
| F38 | GBP minimum bet £1, minimum BSP liability £10, minimum bet payout £10. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2686993](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2686993) | 2026-09-04 | 2026-10-10 | V5 (at least £1 only) |
| F39 | INVALID_PROFIT_RATIO refuses bets whose rounded profit moves more than 20% down or 25% up; the rounding mode is inferred. | `official_documentation` · partially_unverified | `zendesk_api_read` | [SA/360010423978](https://support.developer.betfair.com/hc/en-us/articles/360010423978-Why-am-I-receiving-the-INVALID-PROFIT-RATIO-error) | not stated | 2026-10-10 | V14 checks both half-up and half-even rounding |
| F40 | Bet sizes have 2 decimal places of precision. | `unverified` · unverified | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | not stated | 2026-10-10 | exact 2 dp required |
| F42 | In-play bets carry a 1-12 s delay; PASSIVE/DYNAMIC delay types; KEEP persistence is invalid on PASSIVE markets. | `official_documentation` · documented | `zendesk_api_read` | [SA/360002825652](https://support.developer.betfair.com/hc/en-us/articles/360002825652-Why-do-you-have-a-delay-on-placing-bets-on-a-market-that-is-in-play), [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | not stated | 2026-10-10 | in-play refused in v1; LAPSE only; IN_PLAY_DELAY_MAX_S = 12 is part of the kill's re-sweep bound |

### `ticks` · 價格階梯

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F41 | CLASSIC ladder (350 ticks 1.01-1000), FINEST and LINE_RANGE ladders; catalogue priceLadderDescription.type; the stream's priceLadderDefinition is an object in the Swagger but a string in the doc table. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2686993](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2686993), [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455), [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [ESA Swagger](https://raw.githubusercontent.com/betfair/stream-api-sample-code/master/ESASwaggerSchema.json) | 2026-09-04 | 2026-10-10 | CLASSIC only; book.ladder_type() accepts both shapes |

### `gateway` · 落單閘門

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F22 | Whether customerRef is echoed in the execution report. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496), [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | 2026-07-31 | 2026-10-10 | not relied on; first-live-test T9 observes it |
| F23 | customerOrderRef is at most 32 characters, is not checked for uniqueness and can be filtered on; its character set is not stated. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465), [DD/2687504](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687504), [DD/2687749](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687749) | not stated | 2026-10-10 | charset [A-Za-z0-9-]; local unique index; A19 via live-probe |
| F58 | The Heartbeat API cancels all LIMIT bets for the customer on the exchange (not per app key) when heartbeats stop; the timeout is 10-300 s; it can unregister silently. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687861](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687861) | 2024-06-27 | 2026-10-10 | A9 refuses heartbeat_api_enabled; the path is not in the transport allowlist |

### `lifecycle` · 訂單狀態機

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F25 | marketVersion lapses the order on a material market change; the rejection is FAILURE / BET_ACTION_ERROR / BET_TAKEN_OR_LAPSED with a betId echoed. The page is internally inconsistent about a higher version. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496) | 2026-07-31 | 2026-10-10 | spec section 9.4 rule 2: UNKNOWN until confirmed |
| F27 | placeOrders is atomic unless Best Execution is off, in which case PROCESSED_WITH_ERRORS can occur. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687496](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687496) | 2026-07-31 | 2026-10-10 | classify per instruction |
| F29 | ExecutionReportStatus, InstructionReportStatus, error code enums, OrderStatus, PersistenceType and Side values. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | 2026-07-17 | 2026-10-10 | (surface, code) tables; enum constants |
| F30 | How BET_IN_PROGRESS differs from TIMEOUT in practice. | `unverified` · unverified | `official_rest_api_storage_read` | [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | 2026-07-17 | 2026-10-10 | treated as UNKNOWN |
| F32 | replaceOrders is a cancel followed by an atomic place with no rollback; CANCELLED_NOT_PLACED exists; the new betId is in placeInstructionReport. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687487](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687487), [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | not stated | 2026-10-10 | paper only: REPLACED with a linked child, provisional child counted adversarially; live replace removed in v1; T11 dropped |

### `reconcile` · 未知結果對數

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F28 | Order calls time out after 5 s (TIMEOUT); allow 15 s for the order to appear before treating it as lapsed. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455), [DD/2687450](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687450) | 2026-07-17 | 2026-10-10 | reconcile window, grace and 3 successful polls; the kill's re-sweep bound (ORDER_TIMEOUT_S + ORDER_VISIBILITY_S + IN_PLAY_DELAY_MAX_S + 3 s = 35 s after the KILL file) |
| F33 | Whether the replacement bet inherits customerOrderRef and customerStrategyRef. | `unverified` · unverified | `official_rest_api_storage_read` | [DD/2687487](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687487) | not stated | 2026-10-10 | paper only (provisional child); live replace removed in v1, so no live code depends on it; T11 dropped |
| F35 | listClearedOrders needs betStatus; groupBy applies only to SETTLED (BET needed for LAPSED/CANCELLED); the default window is 90 days and a from date is advised. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687749](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687749) | not stated | 2026-10-10 | settled_from_utc required by us; groupBy only for SETTLED |
| F36 | Lapsed and cancelled bets are visible in listClearedOrders before the market settles. | `unverified` · unverified | `official_rest_api_storage_read` | [DD/2687749](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687749) | not stated | 2026-10-10 | not sufficient on its own for ABSENT_UNCONFIRMED; in live, cleared rows only establish that a bet exists (EXECUTABLE at worst case, never a terminal state); first-live-test T10 |
| F77 | After a market closes, listMarketBook returns it as CLOSED for an unverified period, and listCurrentOrders stops listing a bet once its market is settled. | `inferred` · inferred | `official_rest_api_storage_read` | [DD/2687504](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687504), [DD/2687510](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687510) | not stated | 2026-10-10 | settle-market P2 (CLOSED on listMarketBook) and P3 (none of our bets current); if Betfair keeps listing them settle-market refuses; a market no longer returned cannot be settled in v1 and stays counted at worst case (A20, decided by Yu 2026-10-11: kept; settle soon after close); first-live-test T10 extended |

### `killswitch` · kill switch

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F31 | cancelOrders with no marketId and no instructions cancels every bet on the account; a concurrent cancel-all is rejected or gives ERROR_IN_MATCHER; it applies to LIMIT bets only. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687491](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687491) | not stated | 2026-10-10 | own-scope kill by default; account-wide kill gated; a rejected concurrent cancel-all ends the kill kill_incomplete (exit 3) |
| F71 | listCurrentOrders and listClearedOrders accept includeSourceId; sourceIdKey is the app key used to place the order; its value format is unverified. | `official_documentation` · partially_unverified | `official_rest_api_storage_read` | [DD/2687504](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687504), [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | not stated | 2026-10-10 | startup reconcile and reconcile_unknown leg (it only halts); no longer a kill leg since the v1 narrowing; stripped from storage |

### `risk` · 風險上限

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F19 | More than 1000 instructions per second gives TOO_MANY_REQUESTS. | `official_documentation` · documented | `zendesk_api_read` | [SA/360000406111](https://support.developer.betfair.com/hc/en-us/articles/360000406111-Why-am-I-receiving-the-TOO-MANY-REQUESTS-error) | not stated | 2026-10-10 | local caps far below |
| F26 | MarketBook.version monotonicity: MarketBook says it increments, the MarketVersion type says non-monotonic. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465) | not stated | 2026-10-10 | no monotonic assumption |
| F61 | Transaction charges apply above 5000 transactions per clock hour with documented counting rules; a conflicting copy of the per-transaction rates was seen during research but its URL was not recorded, so it is not relied upon. | `official_documentation` · partially_unverified | `zendesk_api_read` | [SA/20029343399836](https://support.developer.betfair.com/hc/en-us/articles/20029343399836-Transaction-Charge-how-are-transactions-counted), [Wayback 20250717193515](https://web.archive.org/web/20250717193515/https://support.betfair.com/app/answers/detail/betfair-charges/) | not stated | 2026-10-10 | count only; max_transactions_per_hour 300 (ceiling 1000) |

### `pnl` · 盈虧計算

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F37 | ClearedOrderSummary documents profit; its commission field names an EXCHANGE grouping that the GroupBy enum (EVENT_TYPE, EVENT, MARKET, SIDE, BET) does not offer. The sign and basis of profit and the use of priceMatched/sizeSettled are unverified. | `official_documentation` · partially_unverified | `official_rest_api_storage_read` | [DD/2687465](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465), [DD/2687455](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687455) | not stated | 2026-10-10 | live P&L informational: no code path writes settled profit from cleared rows; settle-market books each order's worst case; orders-cleared shows Betfair's figure |

### `paper` · paper fill model 同 commission

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F60 | Traded volume is counted as backer's stake x 2 in PRO historical data; the live API basis is unverified. | `official_documentation` · partially_unverified | `zendesk_api_read` | [SA/360000402211](https://support.developer.betfair.com/hc/en-us/articles/360000402211-How-do-I-download-and-view-Betfair-Historical-Data) | not stated | 2026-10-10 | traded_volume_divisor 2, labelled; first-live-test T8 |
| F62 | UK commission follows the My Betfair Rewards package and Basic 2% is Betfair's own example; the 5% and 8% rates (and so the example config's 5%) come only from a secondary source, not from Betfair. | `official_current_policy` + secondary `forum_participant_opinion` · partially_unverified | `wayback_snapshot_20250717193515` | [Wayback 20250717193515](https://web.archive.org/web/20250717193515/https://support.betfair.com/app/answers/detail/betfair-charges/), [Wayback 20250123063716](https://web.archive.org/web/20250123063716/https://support.betfair.com/app/answers/detail/6801-my-betfair-rewards-faq/), [Sporting Life guide (secondary)](https://www.sportinglife.com/free-bets/guides/betfair-exchange-education/how-does-commission-work) | 2025-07-17 | 2026-10-10 | commission_rate and commission_source are required config |

### `arming` · live arming

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F44 | The delayed key places real bets (key table); there is no sandbox; delayed data is 1-180 s late with 3 levels and no runner totalMatched; the stream allows 2 connections and 200 markets and is conflated to 180000 ms. | `official_documentation` · documented_corrected | `official_rest_api_storage_read` | [DD/2687105](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687105), [SA/115003886111](https://support.developer.betfair.com/hc/en-us/articles/115003886111-Is-there-an-API-testbed-available), [SA/360009638032](https://support.developer.betfair.com/hc/en-us/articles/360009638032-When-should-I-use-the-Delayed-or-Live-Application-Key), [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396) | 2026-05-07 | 2026-10-10 | arming independent of key; labels; the gateway refuses delayed-key orders |
| F47 | An unfunded or overridden live key returns delayed data. | `official_documentation` · documented | `zendesk_api_read` | [SA/115003886711](https://support.developer.betfair.com/hc/en-us/articles/115003886711-Why-am-I-receiving-delayed-data-when-using-a-live-Application-Key) | not stated | 2026-10-10 | A18 requires is_market_data_delayed False |
| F64 | API access from geo-restricted IP regions (MaxMind), including the US, DE, NL, FR and SG, is blocked. | `official_documentation` · documented | `zendesk_api_read` | [SA/28271961503516](https://support.developer.betfair.com/hc/en-us/articles/28271961503516-Which-IP-regions-are-restricted-from-accessing-the-Betfair-API) | 2026-09-21 | 2026-10-10 | A10 operator egress attestation and cloud markers |
| F66 | AccountDetails includes currencyCode. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687852](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687852) | 2022-03-02 | 2026-10-10 | A16 (an absent value fails) |
| F76 | One person may hold one account (T&C 6.1); a second local profile for the same account would split the caps. | `official_current_policy` · documented | `wayback_snapshot_20260511012028` | [Wayback 20260511012028](https://web.archive.org/web/20260511012028/https://support.betfair.com/app/answers/detail/betfair-general-terms-and-conditions/) | 2026-05-11 | 2026-10-10 | accounts.json registry (A15) |

### `receipts` · durable write

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F74 | macOS fsync does not flush the drive cache and F_FULLFSYNC does (man page); SQLite's PRAGMA fullfsync was checked locally on SQLite 3.53.2. | `official_documentation` + secondary `inferred` · documented | `man_page_read` + `local_python_check` | `man:fsync(2)`, `local:sqlite-3.53.2` | not stated | 2026-10-10 | receipts.full_fsync and the journal pragmas; the SQLite part is a local check (secondary inferred, local_python_check), not documentation |

### `docs` · 只用喺文件嘅事實

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F05 | Certificate login needs a 2048-bit RSA self-signed client certificate made with the documented openssl steps and uploaded by hand on the website. | `official_documentation` · documented | `official_rest_api_storage_read` | [DD/2687915](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915) | 2026-07-01 | 2026-10-10 | docs only |
| F63 | Terms and conditions: the API is a betting channel (18.3, 12.1.4); data is for personal non-commercial use (19.3); one account per person (6.1); cancellation is effective on confirmation (18.12). The live page returned 403. | `official_current_policy` · documented | `wayback_snapshot_20260511012028` | [Wayback 20260511012028](https://web.archive.org/web/20260511012028/https://support.betfair.com/app/answers/detail/betfair-general-terms-and-conditions/), [Wayback 20251226113352](https://web.archive.org/web/20251226113352/https://support.betfair.com/app/answers/detail/betfair-general-terms-and-conditions/) | 2026-05-11 | 2026-10-10 | docs; licence note in recorder receipts |

### `future` · v1 未用、留畀將來

| id | value used | evidence_type · status | retrieval | source | published_or_updated | accessed | code handling |
|---|---|---|---|---|---|---|---|
| F57 | The order stream's initial image holds only executable orders and runner removal voids show on the stream; the doc and a support article conflict. | `official_documentation` · conflicting | `official_rest_api_storage_read` | [DD/2687396](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687396), [SA/360000391492](https://support.developer.betfair.com/hc/en-us/articles/360000391492-How-are-void-bets-treated-by-the-Stream-API) | not stated | 2026-10-10 | v1 reconciles through REST only |

## Conflicts and open questions

以下每條嘅 status 都係 `conflicting`、`unverified`、`unknown`、`inferred` 或 `partially_unverified`。代碼一律揀較保守嘅做法；first-live-test 只係觀察，唔會自動放寬任何規則。

- **F08** keepAlive／logout 用 POST 定 GET：頁面寫 POST，佢個 curl sample 就用 GET。代碼用 `session.sso_http_method`（預設 POST）；T2 兩樣都試。
- **F10** 失敗嘅 login 計唔計入每分鐘 100 次：文件冇講清楚。代碼喺本地限成功 login 每 10 分鐘 30 次、連續失敗 2 次就停、ban 寫落 ledger 跨 restart 都有效。
- **F18** order-projection 讀取嘅 3 個 concurrency 係計排緊隊定執行緊：兩份官方文件講法唔同。代碼 in-flight 上限 2。
- **F22** execution report 有冇 echo `customerRef`：placeOrders 頁同 type definition 唔一致。代碼唔依賴佢；T9 觀察。
- **F25** marketVersion 對「更高 version」嘅描述自相矛盾；rejection 會 echo betId。代碼按[落單路徑 §5](betfair-order-path.md#5-lifecycle-state-machine) place report 分類第 2 條，未確認之前當 UNKNOWN。
- **F26** MarketBook.version 係咪單調遞增：MarketBook 話會遞增，MarketVersion type 話唔一定。代碼唔假設單調。
- **F30** BET_IN_PROGRESS 同 TIMEOUT 實際有咩分別：冇文件。代碼當 UNKNOWN。
- **F33** replaceOrders 開出嚟嘅新 bet 會唔會繼承 customerOrderRef／customerStrategyRef：未驗證。Live replace 喺 v1 刪咗，所以冇 live 代碼倚賴佢；paper 用 provisional child。T11 跟 live replace 一齊剔走，呢條會一直未驗證。
- **F36** lapsed／cancelled bet 喺 market settle 之前會唔會出現喺 listClearedOrders：未驗證。代碼唔會單靠呢個判 ABSENT_UNCONFIRMED；live 嘅 cleared row 只證明 bet 存在，永遠唔會令單變終結或者零 exposure；T10 觀察。
- **F37** ClearedOrderSummary `profit` 嘅正負號同計法、`priceMatched`／`sizeSettled` 點用：未驗證。v1 收窄之後 live P&L 只係資訊：冇代碼由 cleared rows 寫已實現 P&L，`settle-market` 按每張單嘅 worst case 入數，`pnl_basis_mismatch` HALT 已經刪咗。
- **F39** INVALID_PROFIT_RATIO 用邊種 rounding：係推斷。V14 同時試 half-up 同 half-even，兩樣都要過。
- **F40** size 係咪 2 個小數位：未驗證。代碼要求正好 2 dp，唔會自己 round。
- **F41** stream 嘅 `priceLadderDefinition` 係 object 定 string：Swagger 同文件表格唔一致。`book.ladder_type()` 兩種都收；落單只做 CLASSIC。
- **F45** PERMISSION_DENIED 同 delayed key 嘅關係：enum 頁同 support article 講法唔同。代碼當 `terminal_account`；v1 唔會用 delayed key 落單，所以唔去試。
- **F50** heartbeatMs 範圍：request 500–5000，echo 500–30000，兩個來源唔一致。liveness 用 echo 返嚟嘅值計；T6 觀察。
- **F52** `segmentationEnabled: false` 係咪一定唔會分段：未驗證。recorder 一見分段 line 就 close segment。
- **F56** live key 嘅 stream connection 上限：冇公佈。代碼喺 authentication 讀 `connectionsAvailable`。
- **F57** order stream 嘅 initial image 同 void 處理：文件同 support article 唔一致。v1 只用 REST 對數，唔用 order stream。
- **F60** live API 嘅 traded volume 係咪都計 backer stake × 2：PRO historical 係，live 未驗證。paper model 用 divisor 2，寧願少 fill；T8 對比。
- **F61** transaction charge 費率：研究時見過一個同 Wayback 存檔講法唔同嘅版本，但冇記低佢嘅 URL，所以唔倚賴，status 係 `partially_unverified`（以前標 `conflicting`，但衝突嘅另一邊冇來源紀錄）。代碼只數 transaction，唔計錢；上限每小時 300（ceiling 1000）。
- **F62** commission rate：UK 跟 My Betfair Rewards，Basic 2% 係 Betfair 自己嘅例子；5% 同 8% 只有二手來源。`commission_rate` 同 `commission_source` 一定要填。
- **F65** getAccountFunds `exposure` 嘅正負號：未驗證。R12 用 `abs()`；T7 觀察。
- **F67** 新 login 會唔會令其他 session（Bet Angel、網站）失效：推斷、信心低。T3 未做之前，每個要 login 嘅命令都要 `--bet-angel-paused`；`kill` 除外：佢用獨立嘅 emergency session login，`coexistence.bet_angel` 未驗證時喺 stderr 印 F67 警告，叫你 kill 之後睇返 Bet Angel。
- **F68** man page 冇 exit status 一節：`security` 搵唔到 item 時 exit 44（errSecItemNotFound -25300 嘅低位 byte）係由 OSStatus 推出嚟、未驗證。`anchor_read` 只將兩次 lookup 都係 exit 44 當「冇 anchor」，其他任何結果都 fail closed；T0 觀察。
- **F71** `sourceIdKey` 嘅格式：未驗證。Startup reconcile 同 `reconcile_unknown` 多一條比對 live key 嘅腿（只會 HALT），冇呢個 field 就記低 skip；所有儲存紀錄都刪走佢。v1 收窄之後 kill 唔再用呢條腿；本來由 T11 觀察，T11 剔走之後未有 test 覆蓋。
- **F73** `security add-generic-password -T ""` 係咪令每次讀取都彈 dialog：man page 只寫 `-T appPath`。`keychain-setup` 照印；arming 唔依賴佢；kill 讀 Keychain 之前會提示撳 Allow；T0 觀察。
- **F77** market close 咗之後 listMarketBook 返 CLOSED 返幾耐，同 listCurrentOrders 係咪 market settle 咗就唔再列我哋嘅 bet：推斷（`inferred`），冇新來源。`settle-market` 嘅 P2／P3 靠佢：如果 Betfair 繼續列，`settle-market` 只會拒絕（唔會更寬鬆）；Betfair 唔再返嘅 market 喺 v1 settle 唔到，worst case 一直計住（Yu 2026-10-11 決定保留，amendment A20：CLOSED 之後盡快 settle）。T10 加長觀察。

其他未解決嘅問題（唔屬於任何一條 F-id）：

- Yu 個 account 嘅 jurisdiction（假設 UK `.com`，session 24 小時）同 Logout Preferences 設定。
- 係咪已經做完 KYC、會唔會付 £499 開 live key。未開之前，只有 delayed key，而 delayed key 都可以落真錢（F44）。
- Betfair 係咪每個 account 只認一張 certificate、上載新 `.crt` 會唔會取代舊嗰張、certificate 過期點處理：文件冇講（官方例子用 `-days 365`）。
- NZ mapping：F01 記 AUS/NZ 客用 `.com.au` SSO host，F12 記 NZ 客用 `api.betfair.com.au`（兩條都係 fact register 入面嘅官方文件）。設計規格嘅 host 表本來將 NZ 當 `com` 加 `.au` API host；audit 之後代碼改跟 F01 同 F12：只有 `com.au` 可以揀 `api.betfair.com.au`，`com` 加 `.au` 會被拒。Live 仍然只限 `com`；NZ 用之前要喺 first-live-test 確認。

## First-live-test checklist

**每一項都要 Yu 明確同意、而且本人喺終端機前面先做。** 呢份 checklist 唔授權任何 live call。

- **結果放邊：** raw response 只放 `STATE_ROOT/profiles/<p>/live-tests/`（唔入 Git）。入 Git 嘅 report 只可以有 test id、status 同 error code、field 名、boolean、數量、時間長度，同私人收據嘅 sha256。
- **先記收據：** 每個做完嘅 test 寫入一份新嘅 dated report `reports/betfair-live-tests-<YYYY-MM-DD>.json`，`first_live_tests_run` 每項係 `{"test_id": "T<n>", "receipt_sha256": "<私人收據 sha256>"}`。[Setup report](../reports/betfair-setup-2026-10-10.json) 唔改：佢嘅 status 講明冇 network call，所以佢嘅 `first_live_tests_run` 永遠係空。
- **再改標籤：** 有收據之後，下表「解決邊條」列出嘅事實先可以標 `first_person_observation`；`sources` 要加 `live-test:T<n>:sha256:<同一個 receipt_sha256>`，再更新 JSON 同上面對應嗰行。冇對應收據、或者 test 冇列呢條事實，`test_betfair_facts_doc` 都會拒絕呢個標籤。

| test | 做咩 | 解決邊條 |
|---|---|---|
| T0 | 用 `minimal_env` 檢查 Keychain item 存在；`-T ""` 係咪每次讀都彈 dialog；Keychain 鎖住時 `security` 點反應；item 唔存在時係咪 exit 44 | F68, F73 |
| T1 | certificate login 成功，記低 `loginStatus` | F01–F04 |
| T2 | keepAlive 用 GET 同 POST 各試一次 | F08 |
| T3 | 拎住 token A，喺 Bet Angel 或網站再 login，之後用 A call keepAlive；A 仲有效就將 `coexistence.bet_angel` 改做 `token_survival_verified` | F67 |
| T4 | 攞一個 JSON-RPC error envelope，例如用無效 marketId | F14, F15 |
| T5 | `isMarketDataDelayed` 喺 delayed key 同 live key 下嘅值 | F43, F47 |
| T6 | delayed-key stream：echo 嘅 conflateMs、`connectionsAvailable`、conflation 期間有冇 HEARTBEAT line、runner `tv` 有冇、order stream 准唔准 | F50, F51, F56, F59 |
| T7 | AccountDetails 嘅 field 名，同 funds `exposure` 嘅正負號 | F65, F66 |
| T8 | listMarketBook 嘅 traded volume 計法，同網站對比 | F60 |
| T9 | 經 `live-probe`（LAY £1.00 @ 1.01，最多輸 £0.01，即刻 cancel）：report 有冇 echo `customerRef`。**唔會**刻意重複提交，因為違反「ref 永不重用」 | F22 |
| T10 | 經 `live-probe`：cancel 咗嘅 probe 喺 market settle 之前會唔會出現喺 cleared orders；加長（v1 收窄）：probe 嘅 market settle 咗之後，listMarketBook 返 CLOSED 返幾耐、listCurrentOrders 係咪唔再列已 settle 嘅 bet（`settle-market` 嘅 P2／P3）；仲有：取消咗嘅 probe bet（冇 match）喺 market settle 之前仲會唔會喺 listCurrentOrders（`orderProjection=ALL`）出現（`KILL_COMPLETE_LINE` 因此唔再講呢樣） | F36, F77 |
| T11 | 已剔走：`live-probe --replace-step` 跟 live replace 喺 v1 一齊刪咗（Yu 2026-10-10 決定）。F33 唔再影響 live 代碼；F71 嘅 `sourceIdKey` 格式未有 test 覆蓋 | — |

**T12（新 delayed key 會唔會出 PERMISSION_DENIED）已經剔走。** v1 gateway 拒絕用 delayed key 落單；要試佢就要經一條我哋刻意唔起嘅路徑落真注。

