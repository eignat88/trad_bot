# Issue #253 — Bybit V5 API Quota Model (v1)

**Purpose:** factual, sourced analysis of Bybit V5 API rate limiting to support (or reject) a
proposed **global client-side budget of 8 requests/second sustained, burst 12, shared across
~5 processes**.

**Type:** documentation research only. No files of the bot were modified; no production
measurements were taken.

---

## 0. Method, sources, and evidence legend

**Sources.** All factual claims below are cited with links to official Bybit documentation
(`bybit-exchange.github.io/docs/...`), fetched live during this research session. Community
sources (SDK READMEs, forums) were deliberately **not** used as authority.

**Tooling constraints (stated explicitly, per research rules):**

* The `web_search` facility was unavailable in this session (no API key), so discovery was done by
  direct fetching of known official doc URLs (rate-limit, error-code, endpoint, WebSocket, FAQ,
  changelog pages). Every page cited below was fetched with HTTP 200 unless noted.
* Live probing of `api.bybit.com` (to verify response headers empirically) **failed from this
  environment**: outbound TLS to the exchange was blocked by the sandbox (`curl` exit 35 /
  `SEC_E_NO_CREDENTIALS`; `Invoke-WebRequest` "base connection closed"). Therefore **no empirical
  header/cap measurement is included** — everything is documentation-derived, and the measurement
  plan in §6.4 is what would produce the missing empirical evidence.
* GitHub: the public V5 docs source repository could not be located (the org's repo list contains
  only SDKs and `docs-legacy`; `raw.githubusercontent.com/.../bybit-official-apidocs/...` returns
  404), so the merged-cell tables of the rate-limit page could only be read as rendered on the
  site. Where a table's merged columns make a value ambiguous, that is flagged.

**Evidence legend:**

| Tag | Meaning |
|---|---|
| **[D]** | Documented verbatim in official Bybit docs (quoted or paraphrased with link) |
| **[D-absence]** | Documented absence: multiple official pages + sitemap searched; the statement does not exist in reachable official docs (search not provably exhaustive — noted where relevant) |
| **[I]** | Inference drawn from documented facts (reasoning stated) |
| **[U]** | Not documented / not verifiable from documentation (explicitly called out, never guessed) |

---

## 1. Bybit V5 rate-limit model

Bybit applies **two independent layers** of request throttling on REST, plus separate rules for
WebSocket connections and WebSocket order entry
([Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit)).

### 1.1 Layer 1 — HTTP IP limit (applies to all traffic)

* **[D]** "You are allowed to send **600 requests within a 5-second window per IP** by default.
  This limit applies to all traffic directed to `api.bybit.com`, `api.bybick.com`, and local site
  hostnames such as `api.bybit.kz`."
  ([Rate Limit Rules → HTTP IP limit](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit))
  * Arithmetic restatement: 600/5 s ≙ **an average of 120 req/s per IP** — this is the only
    documented numeric cap that covers **all** requests including public market data.
* **[D]** Exceeding it produces `403, access too frequent`: "it indicates that your IP has
  exceeded the allowed request frequency. In this case, you should **terminate all HTTP sessions
  and wait for at least 10 minutes**. The ban will be lifted automatically."
  ([HTTP IP limit](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit),
  corroborated by the [Error Codes → HTTP Code](https://bybit-exchange.github.io/docs/v5/error#http-code)
  table: "403 — Forbidden request. Possible causes: 1. IP rate limit breached; …").
* **[D]** "We do not recommend running your application at the very edge of these limits in case
  abnormal network activity results in an unexpected violation."
  ([Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit))
* **[D]** The IP limit counts **all traffic from that IP** — i.e., every process, API key, and
  any other client sharing the same egress IP is aggregated into one 600/5 s budget.

### 1.2 Layer 2 — API rate limit (per second, per UID, per endpoint)

* **[D]** "If you receive `"retCode": 10006, "retMsg": "Too many visits!"` in the JSON response,
  you have hit the API rate limit. The API rate limit is based on the **rolling time window per
  second and UID**. In other words, it is per second per UID."
  ([Rate Limit Rules → API Rate Limit](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit))
* **[D]** The per-endpoint caps are published in the **API Rate Limit Table**
  ([same page](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit-table)) with
  sections: Trade, Position, Account, Asset, User, Referral, Spot Margin Trade, Spread Trading,
  RFQ, Institutional Loan.
* **[D]** The error-code page restates 10006 as: "**Too many visits. Exceeded the API Rate
  Limit.**"
  ([Error Codes → UTA](https://bybit-exchange.github.io/docs/v5/error#uta)).
* **[I]** Combining these two documented statements: 10006 is the **per-UID (per-account, i.e.
  per-API-key-identity) per-second, per-endpoint** violation signal; the **per-IP** violation
  surfaces differently (HTTP `403 "access too frequent"` — see §1.1). The docs never literally
  write "10006 is per-key, not per-IP", so the split is an inference from the scoping sentences.
* **[D]** Batch endpoints carry their **own** limit, separate from single-order endpoints: "The
  batch order endpoint, which includes operations for creating, amending, and canceling, has its
  own rate limit and does not share it with single requests"
  ([Instructions for batch endpoints](https://bybit-exchange.github.io/docs/v5/rate-limit#instructions-for-batch-endpoints));
  for linear/inverse batch calls the quota is consumed as
  `Number of consumption = number of requests × number of orders contained in a single request`.

### 1.3 What the published table covers — and the "Market" gap

* **[D]** The current API Rate Limit Table has **no Market section**. Verified three ways:
  (a) the page's own table of contents lists only Trade / Position / Account / Asset / User /
  Referral / Spot Margin Trade / Spread Trading / RFQ / Institutional Loan; (b) the full page
  content was fetched and contains no `GET /v5/market/...` row; (c) each Market endpoint page
  (kline, instruments-info, open-interest, tickers, funding-history) was fetched individually and
  contains **no rate-limit note at all** — only the site-sidebar link to the rate-limit page.
  ([Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit),
  [Get Kline](https://bybit-exchange.github.io/docs/v5/market/kline),
  [Get Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument),
  [Get Open Interest](https://bybit-exchange.github.io/docs/v5/market/open-interest),
  [Get Tickers](https://bybit-exchange.github.io/docs/v5/market/tickers),
  [Get Funding Rate History](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate))
* **[I]** Therefore the only **documented** numeric ceiling on public market-data requests is the
  **600 req / 5 s per IP** limit. Whether Bybit additionally enforces **undocumented per-endpoint
  caps** on `/v5/market/*` cannot be confirmed or excluded from docs (see §4 — the
  `X-Bapi-Limit` headers are the documented instrument through which such a cap would become
  visible at runtime).
* **[U]** Two similarly-named but *different* endpoints in the table must not be confused with
  the bot's market endpoints: `/v5/account/instruments-info` (10/s, Account section) is **not**
  `/v5/market/instruments-info`, and `/v5/asset/fundinghistory` (30 req/s, Asset section) is
  **not** `/v5/market/funding/history`
  ([Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit-table)).

### 1.4 Public vs private endpoints

* **[D]** The API-rate-limit section scopes the limit by **UID**; public market endpoints do not
  require authentication (their pages document only GET params, no signature headers)
  ([Get Kline](https://bybit-exchange.github.io/docs/v5/market/kline)).
* **[I]** Because unsigned public requests carry no UID, they cannot consume a per-UID counter;
  the docs' only stated cap for them is the per-IP 600/5 s limit.
* **[U]** The docs do **not** contain an explicit "public endpoints are limited differently from
  private endpoints" statement. This split is inference, not a quotation.
* **[D]** Private (signed) endpoints are additionally bounded per endpoint by the Rate Limit
  Table (§2) and can additionally fail with `10010 Unmatched IP` if the API key has bound IP
  addresses ([Error Codes → UTA](https://bybit-exchange.github.io/docs/v5/error#uta)).

### 1.5 Mainnet vs testnet vs demo

* **[D]** The docs distinguish four environments/domains for key matching: mainnet
  (`api.bybit.com`), demo (`api-demo.bybit.com`), testnet (`api-testnet.bybit.com`), and
  testnet-demo, and warn they must not be mixed with keys
  ([FAQ → Why I get 10003](https://bybit-exchange.github.io/docs/faq#why-i-get-10003-api-key-is-invalid)).
* **[D] (absence, verified broadly)** **No official doc page states any testnet-specific
  rate-limit numbers or differences.** Evidence of absence, not just a missing quote:
  (a) the [Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit) page states its
  limits only for mainnet hostnames (`api.bybit.com`, `api.bybick.com`, `api.bybit.kz`);
  (b) the [Integration Guidance](https://bybit-exchange.github.io/docs/v5/guide) page documents
  the environments (`https://api-testnet.bybit.com` vs `https://api.bybit.com`) but contains **no
  rate-limit content at all**;
  (c) the official docs sitemap (`https://bybit-exchange.github.io/docs/sitemap.xml`, 676 URLs)
  contains **zero** URLs with "testnet" in them — no testnet-specific rate-limit page exists.
* **[U]** Whether `api-testnet.bybit.com` actually enforces the same 600/5 s and per-UID tables
  (or looser/tighter values) is therefore **undocumented** — and this environment could not probe
  testnet empirically (TLS blocked, see §0). Treat testnet capacity as **unknown**, and do not
  extrapolate testnet pass/fail results to production.
* **[D]** Four key environments exist and must not be mixed: "there are 4 env: mainnet, testnet,
  mainnet-demo, testnet-demo"
  ([Error Codes → 10003](https://bybit-exchange.github.io/docs/v5/error#uta)); demo hostnames are
  documented in the [FAQ](https://bybit-exchange.github.io/docs/faq#why-i-get-10003-api-key-is-invalid).

### 1.6 REST limits vs WebSocket-triggered limits

**WebSocket connections (public + private streams):**

* **[D]** "Do not establish more than 500 connections within a 5-minute window" (per WebSocket
  domain), "Do not frequently connect and disconnect the connection", and "Do not establish more
  than 1,000 connections per IP for market data. The connection limits are counted separately for
  Spot, Linear, Inverse, and Options markets"
  ([Rate Limit Rules → Websocket IP limit](https://bybit-exchange.github.io/docs/v5/rate-limit#websocket-ip-limit),
  restated on [Connect → IP Limits](https://bybit-exchange.github.io/docs/v5/ws/connect#ip-limits)).
* **[D]** Subscription-level constraints exist (e.g. args array ≤ 21,000 characters per
  subscription request; per-connection args caps)
  ([Connect → Args limits](https://bybit-exchange.github.io/docs/v5/ws/connect#args-limits)).
* **[U]** There is **no documented per-message or per-topic rate limit on receiving public WS
  data**, and no documented statement that WS data pushes consume the REST request budget.

**WebSocket Order Entry (`wss://stream.bybit.com/v5/trade`):**

* **[D]** Its responses carry the **same** rate-limit headers, scoped to the account and op type:
  `X-Bapi-Limit` = "The total rate limit of the current account for this op type",
  `X-Bapi-Limit-Status` = "The remaining rate limit of the current account for this op type",
  `X-Bapi-Limit-Reset-Timestamp` = same wording as REST
  ([Websocket Trade Guideline](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#createamendcancel-order)).
* **[D]** "The account rate limit is **shared between websocket and http batch orders**"
  ([Websocket Trade Guideline → Batch](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#batch-createamendcancel-order)).
* **[D]** WS order-entry error codes: `10403` — "exceed IP rate limit. **3000 requests per second
  per IP**"; `10429` — "System level frequency protection"; `20003` — "Too frequent requests under
  the same session"; `10003` — "Too many sessions under the same UID"
  ([Websocket Trade Guideline](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#createamendcancel-order),
  [Error Codes → WS OE General code](https://bybit-exchange.github.io/docs/v5/error#ws-oe-general-code)).
* **[I]** WS order entry draws from the **same per-UID, per-op-type, per-second counters as REST**
  (evidence: identical header semantics + the explicit "shared" statement for batch orders), while
  having its own, much larger per-IP ceiling (3000 req/s) on the trade WS domain.
* **[D]** Distinction that matters for this bot's data acquisition: the FAQ's recommended pattern
  is websocket-first, REST-to-fill-gaps — "firstly, rely on the websocket … secondly, query the
  REST API to fill in any discrepencies … This frees up your rate limits for other requests"
  ([FAQ](https://bybit-exchange.github.io/docs/faq#how-can-i-ensure-i-am-using-up-to-date-data)).

### 1.7 PRO / institutional tier and self-service cap query (relevant to "who sets the cap")

* **[D]** Since **August 13, 2025** Bybit rolls out "a new institutional API rate limit framework
  … centralized institution-level rate cap with flexible per-UID configurations"
  ([API Rate Limit Rules for PROs](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-pros/introduction)).
* **[D]** Maximum limit for a single UID (Unified Account, same page): **Default: Futures 10/s,
  Option 10/s, Spot 20/s**; PRO1 200/s, PRO2 400/s, PRO3 600/s, PRO4 800/s, PRO5 1000/s,
  PRO6 1200/s; MM1 600/s, MM2 800/s, MM3 1000/s (per-product columns; merged-cell rendering).
  Institutional aggregate caps (all main+sub UIDs): PRO1 10,000/s … PRO6 60,000/s. "The default
  API rate limit for a new subaccount … is: 10/s for futures, 10/s for options, 20/s for spot."
* **[D]** Institutional users can **query the live cap** themselves: `GET /v5/apilimit/query-cap`
  (returns `totalRate` / `insCap` / `uidCap`), `GET /v5/apilimit/query`, `GET /v5/apilimit/query-all`,
  `POST /v5/apilimit/set` — each of these endpoints self-documents "API rate limit: 50 req per
  second" ([PROs introduction](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-pros/introduction)).
  **[I]** For a non-institutional account these endpoints are unavailable ("Only institutional
  users can request this endpoint"), so the default 10/s-futures figure plus runtime
  `X-Bapi-Limit` headers are the only cap sources left.
* **[D]** The PRO pages contain **no** per-endpoint RPS table and **no** burst/sub-second limits.

---

## 2. The bot's endpoint family — documented limits endpoint by endpoint

Source unless stated: [API Rate Limit Table](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit-table)
(Trade / Position / Account sections) plus the [VIP rules](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-vips).

| Endpoint | Documented per-endpoint limit | Notes |
|---|---|---|
| `GET /v5/market/kline` | **None documented** | Page has no rate note; endpoint absent from Rate Limit Table ([kline page](https://bybit-exchange.github.io/docs/v5/market/kline)). Covered only by IP 600/5 s. |
| `GET /v5/market/instruments-info` | **None documented** | Same ([instrument page](https://bybit-exchange.github.io/docs/v5/market/instrument)). Do not confuse with `/v5/account/instruments-info` = 10/s. |
| `GET /v5/market/open-interest` | **None documented** | Same ([open-interest page](https://bybit-exchange.github.io/docs/v5/market/open-interest)). |
| `GET /v5/market/tickers` | **None documented** | Same ([tickers page](https://bybit-exchange.github.io/docs/v5/market/tickers)). |
| `GET /v5/market/funding/history` | **None documented** | Same ([history-fund-rate page](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)). Do not confuse with `/v5/asset/fundinghistory` = 30 req/s. |
| `GET /v5/account/wallet-balance` | **50/s** (`accountType=UNIFIED`) | Account section of the table; page itself has no rate note ([wallet-balance page](https://bybit-exchange.github.io/docs/v5/account/wallet-balance)). |
| `GET /v5/order/realtime` | **50/s** | Trade section; page has no rate note ([open-order page](https://bybit-exchange.github.io/docs/v5/order/open-order)). |
| `GET /v5/order/history` | **50/s** | Trade section; page recommends websocket instead for real-time ([order-list page](https://bybit-exchange.github.io/docs/v5/order/order-list)). |
| `POST /v5/order/create` | **10/s (inverse), 10/s (linear), 20/s (option)**; spot column = "upgradable Y" | Merged-cell rendering caveat below. Place Order page says only: "Rate limit: Please refer to rate limit table" ([create-order](https://bybit-exchange.github.io/docs/v5/order/create-order#rate-limit)). |
| `POST /v5/order/cancel` | **10/s (inverse), 10/s (linear), 20/s (option)**; spot = "upgradable Y" | Same table; cancel page carries no rate note ([cancel-order page](https://bybit-exchange.github.io/docs/v5/order/cancel-order)). |
| `GET /v5/position/list` | **50/s** | Position section; page has no rate note (documented at [position](https://bybit-exchange.github.io/docs/v5/position)). |

**Cross-check and ambiguity flags:**

* **[D]** The [VIP page](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-vips)
  gives the account-level default table (Unified Account): **Default: Futures 10/s, Option 10/s,
  Spot 20/s**; VIP1 20/20/25; VIP2 40/40/30; VIP3+ 60/60/40.
* **[U]** The main table's header uses merged cells (`UTA2.0 Pro` spanning inverse/linear/option,
  `upgradable` over spot), so the *option* column for create/cancel reads 20/s in the Trade table
  while the VIP table says Option default 10/s — the two official tables are ambiguous/inconsistent
  here as rendered. **This does not affect the bot**: for **linear** (the bot's category) both
  readings agree on **10/s default per UID for order create and cancel**.
* **[D]** Cross-check: the [PRO intro](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-pros/introduction)
  "Maximum limit for a single UID" table independently confirms the default: **10/s futures** for
  Default (and for new subaccounts), i.e. the 10/s linear order cap is stated on three official
  pages (rate-limit Trade table, VIP page, PRO page).
* **[I]** All limits above are **per UID** (rolling per-second window) and are therefore shared by
  *every* process using that API key, regardless of which process or IP sends the request —
  a per-process limiter of the same size would oversubscribe the shared quota.

---

## 3. HTTP 429 and retCode semantics (10006 / 10005 / 10016 / 10018)

| Signal | Exact documented meaning | Rate-limit relevance |
|---|---|---|
| **HTTP 429** | "System level frequency protection. Please retry when encounter this" ([Error Codes → HTTP Code](https://bybit-exchange.github.io/docs/v5/error#http-code)) | **Yes** — system-level throttle; docs give no numeric threshold for it. |
| **HTTP 403** | "Forbidden request. Possible causes: 1. IP rate limit breached; 2. You send GET request with an empty json body; 3. You are using U.S IP" ([Error Codes](https://bybit-exchange.github.io/docs/v5/error#http-code)) | **Yes (IP layer)** — paired with `403 access too frequent`: close all sessions, wait ≥10 min, ban auto-lifts ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit)). |
| **retCode 10006** | "Too many visits. Exceeded the API Rate Limit." ([Error Codes → UTA](https://bybit-exchange.github.io/docs/v5/error#uta)); "you have hit the API rate limit … per second per UID" ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit)) | **Yes (per-UID/per-endpoint layer)**. Per-UID, not per-IP, by the scoping sentence **[I]** — see §1.2. |
| **retCode 10018** | Rendered **struck through**: `~~10018~~  ~~Exceeded the IP Rate Limit.~~` ([Error Codes → UTA](https://bybit-exchange.github.io/docs/v5/error#uta)) | **Deprecated/withdrawn.** IP violations are now documented as HTTP 403 (above). Code 10018 should not be treated as a live signal. **[D]** for the strikethrough; interpretation **[I]**. |
| **retCode 10005** | "Permission denied, please check your API key permissions." ([Error Codes](https://bybit-exchange.github.io/docs/v5/error#uta)) | **No** — permissions, not rate limiting. Never retry/back off on it; fix key scope. **[D]** |
| **retCode 10016** | UTA table: "Server error." ([Error Codes](https://bybit-exchange.github.io/docs/v5/error#uta)); WS OE table: "1. internal server error; 2. Service is restarting" ([WS OE codes](https://bybit-exchange.github.io/docs/v5/error#ws-oe-general-code)) | **No** — server/restart condition, not a quota signal; may co-occur during Bybit redeploys (retry with backoff is reasonable **[I]**). |
| **retCode 10429 (WS)** | "System level frequency protection" ([WS OE codes](https://bybit-exchange.github.io/docs/v5/error#ws-oe-general-code)) | **Yes** — WS-side analogue of HTTP 429. |
| **retCode 20003 (WS)** | "Too frequent requests under the same session" ([WS OE codes](https://bybit-exchange.github.io/docs/v5/error#ws-oe-general-code)) | **Yes** — per-connection session throttling on WS. |

**Recommended client behaviour, as documented:**

* **[D]** on HTTP 429: "Please retry when encounter this"
  ([Error Codes](https://bybit-exchange.github.io/docs/v5/error#http-code)) — the docs say *retry*,
  they do **not** prescribe a backoff schedule, jitter, or reduction factor.
* **[D]** on `403 access too frequent`: "terminate all HTTP sessions and wait for at least 10
  minutes"
  ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit)).
* **[D]** on 10006: treat as "you have hit the API rate limit" and stop exceeding it; plus the
  general advice not to run "at the very edge of these limits"
  ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit)).
* **[U]** **No official exponential-backoff parameters, cooldown constants, or token-bucket
  design are documented anywhere on these pages.** Any concrete backoff scheme
  (e.g. `[1,2]`-s backoff, storm guards, per-endpoint buckets — as the repo's existing retry-budget
  code implements) is an engineering choice, not a Bybit specification.
* **[U]** The docs do not state which comes first (429 vs 10006 vs 403) under combined pressure,
  nor whether a 429 response still carries the `X-Bapi-*` headers.

---

## 4. Rate-limit response headers and adaptive throttling

**Exact documented semantics**
([Rate Limit Rules → API Rate Limit](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit);
identical wording is repeated on the WS order-entry response schema
[here](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#createamendcancel-order)):

| Header | Documented meaning **[D]** |
|---|---|
| `X-Bapi-Limit` | "your current limit for current endpoint" (WS wording: "The total rate limit of the current account for this op type") |
| `X-Bapi-Limit-Status` | "your remaining requests for current endpoint" (WS wording: "The remaining rate limit of the current account for this op type") |
| `X-Bapi-Limit-Reset-Timestamp` | "the timestamp indicating when your request limit resets if you have exceeded your rate\_limit. **Otherwise, this is just the current timestamp** (it may not exactly match `timeNow`)" |

* **[D]** Presence: "Every request to the API returns response header shown in the code panel"
  ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit)) — i.e.
  docs claim headers on **every** response, not on a subset of endpoints. This was **not
  independently verified** here (network blocked, §0); absence of headers on public-market or
  error responses remains an open verification item **[U]**.
* **Units of the reset timestamp [I]:** the docs never state a unit, but the example value
  `1672738134824` (REST) and `1711001595208` (WS) are 13-digit **Unix epoch milliseconds**; 13-digit
  ≙ ms is asserted from the magnitude (a seconds timestamp would be 10 digits, ~1.67e9).
  Treat "epoch ms" as high-confidence inference until measured once in production **[U]**.
* **[D]** Example values on a response: `X-Bapi-Limit: 10`, `X-Bapi-Limit-Status: 9`,
  `X-Bapi-Limit-Reset-Timestamp: 1672738134824` — i.e. the counter is decremented per request
  within the current window.
* **[D]** Note the reset header is *informative even when not violated* ("Otherwise, this is just
  the current timestamp") — so a client cannot by itself distinguish "window end" from "now" on a
  non-violating response without comparing against local/`timeNow`.

**How a client should use this for adaptive throttling (all [I], derived from the above):**

1. Record `X-Bapi-Limit`, `X-Bapi-Limit-Status`, `X-Bapi-Limit-Reset-Timestamp` (and response
   `time`) **per endpoint** on every response — they are the only documented ground truth about the
   live caps (especially valuable for `/v5/market/*`, where caps are undocumented).
2. Maintain a per-endpoint, per-UID request budget that never exceeds `X-Bapi-Limit`; back off
   *before* `Status` reaches 0 rather than discovering the cap via 10006/429.
3. On 429/10006: pause the offending endpoint until the reported reset timestamp (ms), then resume
   at reduced rate (AIMD-style shrink is a sensible engineering choice, but the *algorithm* is not
   documented **[U]**).
4. On `403 access too frequent`: apply the documented remedy literally — close all sessions, wait
   ≥10 minutes before resuming.
5. Because the API window is "per second and UID", keep the budget **globally coordinated across
   all ~5 processes** (a single shared limiter or equivalent coordination); per-process budgets of
   8 RPS each would multiply into 40 RPS against one UID.

---

## 5. Short-term/burst limits, order-rate limits, IP-level protections

**Burst / short-term limits:**

* **[D]** The only documented burst-shaped allowance is the IP layer itself: **600 requests within
  a 5-second window per IP**
  ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit)) — within
  that window a burst of up to 600 is arithmetically possible; sustained rate is the constraint.
* **[D]** Per-UID limits are specified as a "rolling time window **per second** and UID"
  ([rate-limit page](https://bybit-exchange.github.io/docs/v5/rate-limit#api-rate-limit)); the
  published table expresses every cap as `X/s`. **No documented burst credit above the stated
  per-second cap** (e.g. no documented "burst allowance" for order endpoints) appears anywhere on
  the rate-limit, VIP, PRO-intro, endpoint, or FAQ pages reviewed.
* **[U]** Whether the server tolerates brief micro-bursts (sub-second clustering that averages
  ≤ the stated cap) is **not documented**; a client must assume zero burst above `X/s` per
  endpoint per UID.

**Order-rate limits:**

* **[D]** Order endpoints (linear, default tier): `POST /v5/order/create` and
  `/v5/order/cancel` at **10/s**; VIP tiers scale Futures to 20/40/60 per second
  ([rate-limit table](https://bybit-exchange.github.io/docs/v5/rate-limit#trade),
  [VIP rules](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-vips)).
* **[D]** Order-count (not rate) caps: perps & futures — max **500 active orders per symbol** per
  account, and max **10 active conditional orders per symbol**; spot — 500 in total; option — 50
  in the coin dimension by default
  ([Place Order → Open orders up limit](https://bybit-exchange.github.io/docs/v5/order/create-order)).
* **[D]** Daily aggregate risk control: "Bybit will monitor on your API requests. When the total
  number of orders of a single user (aggregated … across main account and subaccounts) within a
  day (UTC 0 - UTC 24) exceeds a certain upper limit, the platform will reserve the right to
  remind, warn, and impose necessary restrictions."
  ([Place Order → Risk control limit notice](https://bybit-exchange.github.io/docs/v5/order/create-order))
  — **the numeric threshold is not documented [U]**.
* **[D-absence] A rule phrased like "10 orders per 10 seconds per symbol" does NOT exist in the
  official docs that were reachable and searched.** Verification performed: every relevant
  official page fetched and scanned (rate-limit rules, VIP and PRO pages, Place/Cancel/Open/History
  order pages, error codes, FAQ, integration guide, WS trade guideline, P2P rate-limit page); the
  official docs sitemap (676 URLs) contains no such page; two site-restricted search-engine
  queries returned "No results" before the engine began rate-limiting the researcher (CAPTCHA) —
  so the absence is strong but **not provably exhaustive**.
  The closest documented constructs are: the *open-order count* caps above, spot error
  `170005 "Too many new orders; current limit is %s orders per %s."` (values are placeholders,
  no numbers — [Error Codes → Spot Trade](https://bybit-exchange.github.io/docs/v5/error#spot-trade)),
  `110020 "Not allowed to have more than 500 active orders."`, and the P2P-only rule
  "A single advertisement can be modified no more than 10 times within 5 minutes"
  ([P2P Rate Limit](https://bybit-exchange.github.io/docs/p2p/rate-limit)).
  **If Issue #253's spec cites "10 orders / 10 s per symbol", that phrasing is not in Bybit's
  official docs and must not be treated as a documented guarantee or a documented threat.**
* **[D] Rate-limit numbers change over time — the docs themselves prove it.** The official
  [V5 changelog](https://bybit-exchange.github.io/docs/changelog/v5) entry dated **2026-05-21**
  reads: "The api rate limit downgraded from 50 req/s to 25 req/s" (GET `/v5/account/transaction-log`),
  and a **2026-02-10** entry adds broker endpoints for setting/querying rate limits. A keyword scan
  of the visible changelog portion (2026-01-05 → 2026-10-29) found **zero** entries mentioning
  10006, 429, `X-Bapi-Limit`, burst, short-term, or the 600-requests IP limit.
  **Caveat [U]:** the changelog page was truncated by the fetch tool at entry 2026-01-05, so
  **2023–2025 entries could not be verified** — older undocumented changes may exist.
  **[I]** Consequence for the budget: published caps are mutable unilateral parameters; the client
  must read live caps (`X-Bapi-Limit`, or `GET /v5/apilimit/query-cap` for institutional accounts)
  rather than hard-coding documentation values.
* **[D]** Spot-safety interplay for retries: order create is **asynchronous** — "the
  acknowledgement … indicates that the request was successfully accepted. This request is
  asynchronous so please use the websocket to confirm the order status"
  ([WS Trade Guideline](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#createamendcancel-order),
  same note on [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order)).

**IP-level protections (complete documented set):**

| Protection | Documented value | Source |
|---|---|---|
| REST requests per IP | 600 / 5 s (violation → HTTP 403 "access too frequent"; close sessions, wait ≥10 min) | [rate-limit #http-ip-limit](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit) |
| WS connection churn | ≤500 connections / 5 min per WS domain; no frequent connect/disconnect | [rate-limit #websocket-ip-limit](https://bybit-exchange.github.io/docs/v5/rate-limit#websocket-ip-limit), [ws/connect](https://bybit-exchange.github.io/docs/v5/ws/connect#ip-limits) |
| WS market-data connections | ≤1,000 per IP, counted separately per Spot/Linear/Inverse/Options | [rate-limit #websocket-ip-limit](https://bybit-exchange.github.io/docs/v5/rate-limit#websocket-ip-limit) |
| WS order-entry per IP | 3,000 req/s per IP (retCode 10403) | [WS Trade Guideline](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline#createamendcancel-order) |
| System-level throttle | HTTP 429 / WS 10429 "System level frequency protection" (threshold **undocumented**) | [error #http-code](https://bybit-exchange.github.io/docs/v5/error#http-code) |

---

## 6. Conclusion

### 6.1 Is "8 RPS sustained / burst 12, shared across all consumers" provably safe?

**Verdict: NOT provable as stated — but it is comfortably inside every *documented* numeric cap,
with one concrete exception that the design must handle explicitly.**

What the documentation **does** support:

* **[I] IP layer — large margin.** 8 RPS sustained is **~6.7 %** of the documented 600/5 s
  (120 req/s average) per-IP budget; a burst of 12 is **2 %** of a single 5-second window (600).
  Against the IP layer the proposal is safe *provided* this egress IP serves no other Bybit
  traffic (the limit is per-IP across all traffic — [rate-limit](https://bybit-exchange.github.io/docs/v5/rate-limit#http-ip-limit)).
  Docs still advise not running "at the very edge" — at ~7 % of budget you are not near it.
* **[I] The 50/s GET endpoints — large margin.** `wallet-balance`, `order/realtime`,
  `order/history`, `position/list` each allow 50/s per UID. Even an adversarial distribution of the
  full 8 RPS into one of these endpoints stays ≪50/s; burst 12 < 50.
* **[U] Market-data endpoints — safe against *published* numbers, unproven in absolute terms.**
  `kline`, `instruments-info`, `open-interest`, `tickers`, `funding/history` have **no documented
  per-endpoint cap**; only the IP limit demonstrably covers them. 8/12 is far below the IP limit,
  so docs show no violation — but undocumented caps may exist and can only be discovered at
  runtime via `X-Bapi-Limit` (§4) or incident.

What the documentation **does contradict**:

* **[D+I] The single documented cap 8/12 can breach is the per-UID per-endpoint order cap.**
  Default linear `POST /v5/order/create` (and `/cancel`) is **10/s**. A *global* bucket allows a
  burst of **12 requests within one second**; if those 12 land on order create/cancel (e.g. a
  stop-hunt across 5 processes after a signal), that is **12/s > 10/s** → documented 10006.
  Sustained 8 RPS is also *80 %* of that cap if it ever concentrated on order endpoints.
  ⇒ **A single global token bucket is insufficient; per-endpoint sub-limiters (order endpoints
  capped at ≤10/s, with margin) are required.** **[I]**
* **[I] Cross-process coordination.** The counters are **per UID**, so all ~5 processes share one
  budget; the global budget must be enforced by one coordinator (or one process fanning out), not
  5 independent 8-RPS buckets. Same for retries: Bybit-side retries amplify into the same window.
* **[U] Hidden ceilings:** the 429 "system level frequency protection" threshold, the daily
  aggregate order-count risk-control threshold, and any anti-abuse dynamic throttling are
  explicitly **not quantified** in the docs.
* **[U] Testnet** numbers are undocumented — tests there prove little about production (§1.5).

### 6.2 Evidence needed — production measurement plan (sketch)

1. **Header telemetry (prerequisite):** log `X-Bapi-Limit`, `X-Bapi-Limit-Status`,
   `X-Bapi-Limit-Reset-Timestamp`, response `time`, HTTP status, `retCode`, endpoint, for **every**
   response from all 5 processes. This simultaneously (a) verifies header presence/units on
   public-market endpoints and error responses, and (b) reveals any **undocumented** `/v5/market/*`
   caps. Run ≥1 week of normal traffic first. If the account is institutional, also call
   `GET /v5/apilimit/query-cap` and record `totalRate`/`insCap`/`uidCap`
   ([PROs → apilimit-query-cap](https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-pros/introduction))
   as the authoritative configured cap.
2. **Cap discovery (safe methods):** on testnet, then on mainnet with low-risk GETs only, step up
   request rate per endpoint (1 → 2 → 5 → 10 → 20 → 50 … RPS, holding each plateau ~60 s) until the
   first `X-Bapi-Limit-Status`→0 / 10006 / 429; record the *observed* cap per endpoint and compare
   with §2. Never use order endpoints for the destructive part. Caveat: testnet observations are
   indicative only — testnet limits are undocumented and may differ from mainnet (§1.5).
3. **Burst characterization:** inject controlled bursts (e.g. 12 requests in <100 ms) per endpoint
   family while watching `Status` and `Reset-Timestamp` — establishes whether the window behaves as
   a true rolling per-second counter and how much sub-second clustering is tolerated (**[U]**
   today).
4. **Soak test at target profile:** ≥48–72 h of 8 RPS sustained with realistic endpoint mix and
   burst-12 events across all 5 processes (production); acceptance = zero non-induced 10006/429/403,
   and `Status` headroom ≥30 % on every endpoint at all times.
5. **Failure drills:** deliberately trip 10006 (single endpoint) and verify header-driven recovery;
   verify the documented 403 remedy (close sessions, wait ≥10 min) actually restores service;
   confirm no cross-process double-fire on order retries.
6. **Guardrails in the client:** global bucket (8, burst 12) + per-endpoint buckets (order
   create/cancel ≤ 9/s to leave margin under 10/s; GET buckets ≤ their observed caps) + header-driven
   adaptive backoff (§4) + alerting when any `Status` < 20 % of `Limit`.

### 6.3 Explicitly NOT provable from documentation alone

* Exact algorithm of the per-UID window (true rolling vs fixed 1-s buckets; sub-second burst
  tolerance) — docs say only "rolling time window per second and UID".
* Existence/nonexistence and values of **undocumented per-endpoint caps** for
  `/v5/market/kline|instruments-info|open-interest|tickers|funding/history`.
* Whether `X-Bapi-*` headers are truly present on **all** responses (docs claim yes; unverified
  here), and their exact behaviour on 429/403 responses.
* Unit of `X-Bapi-Limit-Reset-Timestamp` (epoch-ms is strong inference, not a documented fact).
* **Testnet (and demo) rate limits** relative to mainnet.
* Ordering/interaction of 429 vs 10006 vs 403 under combined pressure; HTTP status under which
  retCode 10006 is delivered; backoff parameters Bybit expects (none documented).
* The numeric thresholds behind 429 "system level frequency protection" and the daily order
  risk-control notice.
* Behaviour when multiple API keys / UIDs share one IP; effects of NAT/proxy egress shared with
  unrelated traffic; any undocumented anti-abuse bans beyond the documented 403 ban.
* Whether Bybit's numbers change without notice — the docs carry no versioned SLA for rate-limit
  values, and the changelog proves caps do change (see note below).

**Changelog note [D]:** the official [V5 changelog](https://bybit-exchange.github.io/docs/changelog/v5)
was keyword-scanned for rate-limit material: within the visible range (2026-01-05 → 2026-10-29)
the only hits are the 2026-05-21 `transaction-log` downgrade (50 → 25 req/s) and the 2026-02-10
broker rate-limit endpoints (both in §5). Entries older than 2026-01-05 were **cut off by fetch
truncation and remain unverified [U]**. Nothing found changed any number used in this analysis.

---

## Sources (all official Bybit documentation, fetched this session)

1. Rate Limit Rules — https://bybit-exchange.github.io/docs/v5/rate-limit
2. API Rate Limit Rules for VIPs — https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-vips
3. API Rate Limit Rules for PROs (institutional framework, UID/institutional caps, apilimit
   endpoints — incl. subpages `apilimit-set`, `apilimit-query`, `apilimit-query-cap`,
   `apilimit-query-all`) — https://bybit-exchange.github.io/docs/v5/rate-limit/rules-for-pros/introduction
4. Error Codes — https://bybit-exchange.github.io/docs/v5/error
5. Websocket Trade Guideline — https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline
6. WebSocket Connect — https://bybit-exchange.github.io/docs/v5/ws/connect
7. FAQ — https://bybit-exchange.github.io/docs/faq
8. Integration Guidance (environments; no rate-limit content) — https://bybit-exchange.github.io/docs/v5/guide
9. Place Order — https://bybit-exchange.github.io/docs/v5/order/create-order
10. Cancel Order — https://bybit-exchange.github.io/docs/v5/order/cancel-order
11. Get Open & Closed Orders (`/v5/order/realtime`) — https://bybit-exchange.github.io/docs/v5/order/open-order
12. Get Order History (`/v5/order/history`) — https://bybit-exchange.github.io/docs/v5/order/order-list
13. Get Position Info (`/v5/position/list`) — https://bybit-exchange.github.io/docs/v5/position
14. Get Wallet Balance — https://bybit-exchange.github.io/docs/v5/account/wallet-balance
15. Get Kline — https://bybit-exchange.github.io/docs/v5/market/kline
16. Get Instruments Info — https://bybit-exchange.github.io/docs/v5/market/instrument
17. Get Open Interest — https://bybit-exchange.github.io/docs/v5/market/open-interest
18. Get Tickers — https://bybit-exchange.github.io/docs/v5/market/tickers
19. Get Funding Rate History — https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
20. V5 Changelog — https://bybit-exchange.github.io/docs/changelog/v5
21. Official docs sitemap (used for absence-of-testnet-pages evidence; 676 URLs) —
    https://bybit-exchange.github.io/docs/sitemap.xml
22. P2P Rate Limit (checked, not applicable to trading bot) — https://bybit-exchange.github.io/docs/p2p/rate-limit
