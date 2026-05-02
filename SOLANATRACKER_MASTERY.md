# Solana Tracker — Master Reference (PhD-level)

> Single-source-of-truth for the bot. Everything I learned reading every doc end-to-end.
> Companion file: `SOLANATRACKER_DOCS_INDEX.md` (URL list).

Last reviewed: full doc set under `/tmp/sttdocs/` (49 files), llms.txt index, AsyncAPI/OpenAPI specs, Pump.fun IDL.

---

## 0. Quick reference (memorize)

### Hosts
| Service | URL | Auth |
|---|---|---|
| Data API | `https://data.solanatracker.io` | header `x-api-key: <KEY>` |
| RPC HTTP | `https://rpc-mainnet.solanatracker.io` | query `?api_key=<KEY>` |
| RPC WS (incl shredSubscribe) | `wss://rpc-mainnet.solanatracker.io` | query `?api_key=<KEY>` |
| Datastream WS (Premium-only) | `wss://datastream.solanatracker.io/<KEY>` | key in URL path |
| Raptor HTTP | `https://raptor-beta.solanatracker.io` | none (free, public beta) |
| Raptor WS `/stream`, `/stream/swap` | `wss://raptor-beta.solanatracker.io/<path>` | none |
| Yellowstone gRPC EU | `https://grpc.solanatracker.io` | bearer token |
| Yellowstone gRPC US | `https://grpc-us.solanatracker.io` | bearer token |

### Our keys (from `.env`)
- `SOLANATRACKER_API_KEY` = Data API key (header `x-api-key`)
- `SOLANATRACKER_RPC_KEY` = RPC plan key (query `?api_key=`)
- Raptor needs **no key**; works against the public hosted endpoint

### Auth gotcha
Data API uses the **header `x-api-key`**. RPC uses the **query param `?api_key=`**. They are different keys (different products) and different transport modes.

---

## 1. The tier truth (what costs money)

| Capability | Tier | Cost | We have? |
|---|---|---|---|
| Data API REST (10k req/mo) | Free Data API | €0 | ✅ |
| Data API Pro (higher limits) | Pro | varies | ❌ |
| RPC HTTP + WS shared (500k credits/mo, 10 RPS, 2 WS conns) | RPC Plan | ~£1/€1 setup | ✅ |
| `shredSubscribe` (RAW pre-block tx, ~50–150 ms latency) | RPC Plan | included | ✅ |
| `getProgramAccountsV2` / `getTokenAccountsByOwnerV2` (Ridge DB) | RPC Plan | 1 credit each | ✅ |
| `getTransaction` (10 credits) | RPC Plan | included | ✅ |
| Raptor (`/quote`, `/swap`, `/send-transaction`, `/stream*`) | Free hosted | €0 | ✅ |
| Datastream WS rooms (`wallet:`, `transaction:`, `top10:`, `holders:`, `sniper:`, `<market>:curve:`, etc.) | Premium | €397/mo | ❌ |
| Yellowstone gRPC (Jito-Shred-accelerated, 50–100 ms faster than vanilla gRPC) | Professional | $247/mo | ❌ |

**What this means for the bot today:** we replicate Datastream rooms with `shredSubscribe + accountInclude` filters, and we run swap execution through hosted Raptor. Everything Premium-only must be derived ourselves from shred stream, polling, or Data API.

---

## 2. Decision tree — which path to use when

```
Need to detect a tx the moment it lands?
├─ pre-confirmation, RAW (~50-150ms): shredSubscribe (RPC plan)
├─ post-confirmation, parsed: Datastream (Premium) or logsSubscribe (free Helius)
└─ paid super-low-latency: Yellowstone gRPC ($247/mo)

Need to read state of an account / pool?
├─ one-shot: getAccountInfo / Data API /tokens/{mint}
├─ many at once: getProgramAccountsV2 (cursor, changedSince, excludeZero)
├─ multiple wallets, one mint: getTokenAccountsByOwners (1 call, up to 250)
└─ live updates: accountSubscribe (RPC WS, free) or programSubscribe

Need to swap?
├─ get price: GET /raptor/quote
├─ build tx: POST /raptor/swap (or /quote-and-swap = both in one call)
├─ build raw instructions: POST /raptor/swap-instructions
├─ submit tx (low-latency Jet TPU): POST /raptor/send-transaction
├─ track tx: GET /raptor/transaction/{sig}
└─ continuous quotes: WS /raptor/stream (quote) or /raptor/stream/swap (pre-built tx)

Need fees?
├─ getPriorityFeeEstimate (recommended | priorityLevel | all-levels)
└─ getRecentPrioritizationFees (raw history)

Need risk gate?
├─ Data API /tokens/{mint} → token.risk (snipers, bundlers, insiders, top10, dev, fees, score, jupiterVerified, rugged)
└─ /tokens/{mint}/bundlers → top 500 bundlers
```

---

## 3. Data API — token endpoints

Base: `https://data.solanatracker.io`
Auth: header `x-api-key`

### Discovery / overview
| Endpoint | Returns |
|---|---|
| `GET /tokens/multi/all` | 3 lists (latest, graduating, graduated). Filters: `limit` (1-500, default 100), `minCurve` (default 40%), `minHolders` (default 20), `maxHolders`, `minCreatedAt`/`maxCreatedAt` (unix s/ms or ISO), `reduceSpam`, `minLiquidity`/`maxLiquidity`, `minMarketCap`/`maxMarketCap`, `markets` (csv), `minRiskScore`/`maxRiskScore`, `rugged` (bool) |
| `GET /tokens/multi/graduated` | All graduated launchpad tokens, paginated (`page`, default 1) |
| `GET /tokens/multi/graduating` | Tokens approaching graduation |
| `GET /tokens/latest?page=1..10` | Last 100 created tokens (per page) |
| `GET /tokens/trending/{tf}` | Top 100 trending by tx volume — `tf` ∈ {5m,15m,30m,1h,2h,3h,4h,5h,6h,12h,24h} |
| `GET /top-performers/{tf}` | Top performers launched today — `tf` ∈ {5m,15m,30m,1h,6h,12h,24h} |

### Per-token details
| Endpoint | Returns |
|---|---|
| `GET /tokens/{mint}` | Full TokenInfo: `token`, `pools[]`, `events`, `risk`, `buys`, `sells`, `txns`, `holders` |
| `GET /tokens/{mint}/holders` | Top 100 holders + `total` count. Each: `wallet`, `amount`, `value{quote,usd}`, `percentage` |
| `GET /tokens/{mint}/holders/top` | Top 20 (excludes LPs). Same shape, `address` instead of `wallet` |
| `GET /tokens/{mint}/holders/all` (paginated) | Up to 5000/page |
| `GET /tokens/{mint}/bundlers` | Up to 500 bundler wallets + aggregate stats `{total, balance, percentage, initialBalance, initialPercentage}`. Per wallet: `wallet, initialBalance, initialPercentage, balance, percentage, bundleTime` |
| `GET /tokens/{mint}/ath` | All-time-high price |
| `GET /tokens/by-deployer/{wallet}` | All tokens deployed by a wallet |

### Stats / trades / events / charts
| Endpoint | Returns |
|---|---|
| `GET /stats/{mint}` | Multi-timeframe stats: per-interval `buyers, sellers, volume{buys,sells,total}, transactions, buys, sells, wallets, price, priceChangePercentage` |
| `GET /trades/{mint}` | Latest trades cursor-paginated. Filters: `cursor`, `showMeta`, `parseJupiter`, `hideArb`, `sortDirection` (DESC/ASC). Each trade: `tx, amount, priceUsd, volume, volumeSol, type (buy/sell), wallet, time, program, pools[]` |
| `GET /trades/{mint}/{pool}` | Pool-specific trades |
| `GET /trades/{mint}/by-wallet/{wallet}` | User-specific token trades |
| `GET /chart/{mint}` | OHLCV |
| `GET /chart/{mint}/{pool}` | OHLCV for token/pool pair |
| `GET /chart/{mint}/holders` | Holders over time |
| `GET /chart/{mint}/bundlers` | Bundler share over time |

### Search
- `GET /search?...` — extensive filters (mc, liquidity, age, market, holders, dev %, etc.)

### TokenInfo schema (canonical)
```jsonc
{
  "token": {"name","symbol","mint","uri","decimals","description","image","hasFileMetaData","strictSocials",
            "creation":{"creator","created_tx","created_time"}},
  "pools": [{"poolId","liquidity":{quote,usd},"price":{quote,usd},"tokenSupply","lpBurn",
             "tokenAddress","marketCap":{quote,usd},"market","quoteToken","decimals",
             "security":{"freezeAuthority","mintAuthority"},"lastUpdated","deployer",
             "curvePercentage","curve","createdAt",
             "txns":{buys,sells,total,volume,volume24h},"bundleId"}],
  "events": {"<tf>": {"priceChangePercentage": x}},   // 1m, 5m, 15m, 1h, 6h, 24h
  "risk": {                                            // see §8 for full Risk
    "snipers":  {count,totalBalance,totalPercentage,wallets[]},
    "bundlers": {count,totalBalance,totalPercentage,totalInitialBalance,totalInitialPercentage,
                 wallets:[{wallet,initialBalance,initialPercentage,balance,percentage,bundleTime}]},
    "insiders": {count,totalBalance,totalPercentage,wallets[]},
    "top10":    <number>,
    "dev":      {percentage,amount},
    "fees":     {<name>:<bps>},
    "rugged":   bool,
    "risks":    [<string>...],
    "score":    1..10,
    "jupiterVerified": bool
  },
  "buys": int, "sells": int, "txns": int, "holders": int
}
```

`pool.curve` is the bonding-curve PDA address. `pool.curvePercentage` is the % progress to graduation. For pre-grad pump.fun tokens these are populated; for graduated tokens, `pool.market` switches to `pumpfun-amm` / `raydium` / etc.

### `risk.score` rule (V41+ bot)
**Score >= 8 is NOT enough on its own to reject a token.** Pre-grad pump.fun tokens routinely score 9-10 and a chunk of those are winners. The empirically working filter is the SPECIFIC markers below, not the overall score.

---

## 4. Data API — wallet & PnL & top-trader endpoints

| Endpoint | Returns |
|---|---|
| `GET /wallet/{owner}` | All token holdings + USD values |
| `GET /wallet/{owner}/{page}` | Paginated holdings |
| `GET /wallet/{owner}/basic` | Just SOL balance + count |
| `GET /wallet/{owner}/trades` | Wallet swap history. Cursor-paginated. Use this if Datastream `wallet:` is unavailable |
| `GET /wallet/{owner}/chart` | Portfolio chart |
| `GET /pnl/{wallet}` | Per-token PnL + summary. Optional: `showHistoricPnL` (BETA, 1d/7d/30d), `holdingCheck` (verify current holding), `hideDetails` (summary only) |
| `GET /pnl/{wallet}/{mint}` | Token-specific PnL |
| `GET /first-buyers/{mint}` | First 100 buyers + their PnL (`realized`, `unrealized`, `holding`) — useful for predicting dump risk |
| `GET /top-traders/all` | Top profitable traders across all tokens. Filters: `expandPnl`, `sortBy` (total \| winPercentage) |
| `GET /top-traders/all/{page}` | Paginated, 25/page |
| `GET /top-traders/{mint}` | Top 100 traders for a specific token |

### `/pnl/{wallet}` response schema
```jsonc
{
  "tokens": {
    "<MINT>": {
      "holding", "held", "sold", "sold_usd",
      "realized", "unrealized", "total",
      "total_sold", "total_invested", "average_buy_amount",
      "current_value", "cost_basis",
      "first_buy_time", "last_buy_time", "last_sell_time", "last_trade_time",
      "buy_transactions", "sell_transactions", "total_transactions"
    }
  },
  "summary": {
    "realized","unrealized","total","totalInvested","averageBuyAmount",
    "totalWins","totalLosses","winPercentage","lossPercentage"
  }
}
```

### `/top-traders/all` response
```jsonc
{ "wallets": [{"wallet": "<addr>", "summary": <PnLSummary>}] }
```

When picking copy-trade wallets, prefer:
- `winPercentage` >= 55% (60%+ is elite)
- `total` realized >> `unrealized` (avoid bag-holders)
- `totalWins` >= 100 trades (sample size)

---

## 5. Data API — credits & rate limits

| Method type | Cost |
|---|---|
| Data API REST endpoints | 1 credit each (most) |
| RPC standard methods (getAccountInfo, getBalance, etc.) | 1 credit |
| RPC `getTransaction` | **10 credits** (heavy) |
| RPC V2 methods (`getProgramAccountsV2`, `getTokenAccountsByOwnerV2`, `getTokenAccountsByOwners`) | 1 credit |
| WS subscriptions (shred, account, logs, program, slot, sig) | 1 credit per active sub-second OR billed per message — the doc treats them as included up to plan WS-conn cap |

**Free RPC plan caps:** 500k credits/mo, 10 RPS, 2 concurrent WS connections.

That ~500k/month over 30 days = ~16k/day = ~11/min. With `getTransaction` at 10 credits, that's 1100 getTx/day max. **Avoid getTx in hot loops.** Prefer `jsonParsed` shredSubscribe to skip the getTx call entirely.

---

## 6. Solana RPC HTTP methods we use

POST to `https://rpc-mainnet.solanatracker.io/?api_key=<KEY>`. Standard JSON-RPC 2.0 envelope.

### `getPriorityFeeEstimate`
- Provide either `transaction` (base58/base64 serialized) **or** `accountKeys[]`.
- Options:
  - `recommended: true` → returns `{priorityFeeEstimate: <microLamports/CU>}` (50th percentile)
  - `priorityLevel`: `Min`(0), `Low`(25), `Medium`(50), `High`(75), `VeryHigh`(95), `UnsafeMax`(100)
  - `includeAllPriorityFeeLevels: true` → returns all 6 levels
  - `lookbackSlots`: 1-300, default 150
  - `evaluateEmptySlotAsZero: true` → useful for sparse accounts
  - `includeVote: false` (default)

For sniping pump.fun graduations, use **High or VeryHigh**, capped via `maxPriorityFee` to avoid getting fleeced when Jito tips spike.

### `getProgramAccountsV2` (Ridge DB)
- Cursor-based pagination (up to 10,000 per page)
- `changedSince: <slot>` → only modified-since (incremental updates, GAME-CHANGER for tracking new pump.fun curves)
- `excludeZero: true` → skip empty token accounts
- `filters`: `{dataSize: N}` and `{memcmp: {offset, bytes}}`
- `dataSlice: {offset, length}` to minimize bandwidth
- `encoding`: `jsonParsed` | `base58` | `base64` | `base64+zstd`

**Pump.fun BondingCurve discriminator is `[23,183,248,55,96,...]`** (8 bytes at offset 0). Use a `memcmp` filter at offset 0 with that base58 prefix to enumerate only BondingCurve accounts. Combined with `changedSince`, you can get a near-real-time stream of curve state changes for free.

### `getTokenAccountsByOwnerV2` / `getTokenAccountsByOwners`
- `ByOwnerV2` = single owner with V2 features (cursor, changedSince, excludeZero)
- `ByOwners` = batch up to **250 owners** for one mint in a single call
- Use case: track which of the top-trader wallets currently hold token X (for confirmation of copy-trade signal)

### `getTokenLargestAccounts`
- Returns top 20 token accounts for a mint
- Use after entry: if your buy didn't put you in top 20, the float is too thin and entry was likely bad

### `getTransaction`
- **Critical: minimum commitment is `confirmed`.** `processed` is rejected with `Method does not support commitment below 'confirmed'`. (We hit this bug — silent failure.)
- 10 credits → expensive. Skip when possible.
- Encoding: `json` | `jsonParsed` | `base64` | `base58`
- `maxSupportedTransactionVersion: 0` for v0 versioned txs

### `sendTransaction`
- Standard Solana send. Options: `skipPreflight` (faster, no slippage check), `preflightCommitment`, `maxRetries`, `minContextSlot`, `encoding` (base58|base64).
- For sniping prefer Raptor `/send-transaction` (Jet TPU) instead — much higher landing rate.

### `simulateTransaction`
- Pre-flight your tx without sending.
- `accounts.addresses[]` lets you fetch state of specified accounts post-simulation (e.g., to read expected output amount).
- Use sparingly — adds latency.

---

## 7. Streaming — every option, ranked

### 7.1 `shredSubscribe` (RPC WS) ★ what we use ★
URL: `wss://rpc-mainnet.solanatracker.io?api_key=<KEY>`

```jsonc
{
  "jsonrpc":"2.0", "id":1, "method":"shredSubscribe",
  "params":[
    {                                  // filter object (AND between fields)
      "accountInclude": ["<wallet1>", "<wallet2>", "..."],   // OR
      "accountExclude": ["<addr>"],                          // NOT
      "accountRequired": ["<addr1>", "<addr2>"],             // AND
      "vote": false
    },
    {                                  // options
      "encoding": "jsonParsed",        // base64 | json | jsonParsed
      "transactionDetails": "full",    // full | signatures | accounts | none
      "maxSupportedTransactionVersion": 0,
      "showRewards": false
    }
  ]
}
```

**Notification shape** (`method: shredTransaction`):
```jsonc
{
  "params": {
    "subscription": <id>,
    "result": {
      "signature": "<base58>",
      "slot": <int>,
      "transaction": {
        "meta": null,                  // ← always null for shreds; no logs, no err
        "transaction": { "message": {...}, "signatures": [...] },
        "version": 0
      }
    }
  }
}
```

**Key facts**:
- ~50-150ms ahead of standard confirmed-block notifications.
- `meta=null` always — there is no logMessages, no err, no innerInstructions. You **must** parse the raw instruction data yourself if you need TradeEvent decoding.
- `accountInclude` is OR. To watch 100 wallets, subscribe to one stream with all 100 in `accountInclude`. Don't open 100 sockets — you'll burn through the 2-conn cap.
- With `encoding=jsonParsed`, account keys come pre-resolved with `signer/writable/source` flags, instructions come with `programId/accounts/data`, all base58 — no manual base64 decode.
- Subscribe response: `{"jsonrpc":"2.0","id":1,"result":<subscription_id>}`.

**Pump.fun example payload** (from doc):
```jsonc
{ "jsonrpc":"2.0","id":6,"method":"shredSubscribe",
  "params":[{"accountInclude":["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"], "vote":false},
            {"encoding":"jsonParsed","transactionDetails":"full","maxSupportedTransactionVersion":0}] }
```
Note: `pAMMBay6...` is the **pumpswap** AMM (post-grad). Pre-grad pump.fun bonding curve program is `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`.

### 7.2 Datastream WS (Premium €397/mo) — what we'd ideally have
URL: `wss://datastream.solanatracker.io/<KEY>`
Subscribe: `{"type":"join","room":"<channel>"}`
Unsubscribe: `{"type":"leave","room":"<channel>"}`

| Room | Purpose | Payload highlights |
|---|---|---|
| `wallet:{addr}` | Swap stream for a single wallet | `tx, type(buy/sell), wallet, time, price{usd,quote}, volume{usd,sol}, program, pools[], from{address,amount,token{...}}, to{address,amount,token{...}}` |
| `transaction:{mint}` | Swap stream for a token | array of swaps, each `{tx, amount, priceUsd, volume, volumeSol, type, wallet, time, program, pools, token:{from,to}}` |
| `holders:{mint}` | Holder count changes | `{total: int}` |
| `top10:{mint}` | Top-10 concentration changes | `{token, holders[{address,amount,percentage}], totalPercentage, previousPercentage, timestamp}` |
| `sniper:{mint}` | Sniper wallet buy/sell | `{wallet, amount, tokenAmount, percentage, previousAmount, previousPercentage, totalSniperPercentage, totalInsiderPercentage, action(buy/sell), timestamp}` |
| `<market>:curve:{pct}` | Bonding-curve threshold alert | market ∈ {pumpfun, launchpad, boop, meteora-curve}, pct 1-99 |
| `latestTokens` / `graduating` / `graduated` | Discovery streams | TokenInfo updates |
| `bundlers:{mint}` | Bundler activity | similar to sniper |
| `insiders:{mint}` | Insider activity | similar |
| `developerHoldings:{wallet}` | Dev wallet activity | dev wallet movement |
| `feetracking:*`, `metadata:*`, `poolStatistics:*`, `poolUpdates:*`, `poolVolume:*`, `tokenVolume:*`, `tokenStatistics:*`, `tokenChanges:*`, `priceByToken:*`, `priceByPool:*`, `priceAggregated`, `priceAllPools:{mint}`, `walletBalance:{addr}`, `walletTokenBalance:{addr}` | Various | (see AsyncAPI spec for full payloads) |

**For copy-trading: `wallet:{addr}` is THE channel.** It's already-parsed, single message per swap, with full token/value info — no decode needed. Without Premium we replicate this via `shredSubscribe + accountInclude=[wallet]` and parse instruction data ourselves.

### 7.3 Yellowstone gRPC (Professional $247/mo)
- Endpoints: EU `https://grpc.solanatracker.io`, US `https://grpc-us.solanatracker.io`
- Jito-Shred-accelerated → **50–100 ms faster** than vanilla Yellowstone gRPC nodes.
- Stream types: `accounts`, `transactions`, `slots`, `blocks`, `entries`.
- Filters: `account_include` (OR), `account_exclude` (NOT), `account_required` (AND), `vote`, `failed`, `signature`. Account-side: `account[]`, `owner[]`, `filters` (memcmp + dataSize), `accounts_data_slice`.
- Commitment: `processed` | `confirmed` | `finalized`.
- Keep-alive via `ping: { id: 1 }` (server replies pong every 15s).
- Client: `@triton-one/yellowstone-grpc` (Node), Rust `yellowstone-grpc-client`, Python `yellowstone-grpc-client` (gRPC + protobuf).
- See §9 for the Pump.fun pattern.

### 7.4 Raptor `/stream` and `/stream/swap`
URL: `wss://raptor-beta.solanatracker.io/stream` and `/stream/swap`. **Free, no key.**

`/stream` (quote stream):
```json
{ "type":"subscribe", "id":"<optional>",
  "inputMint":"<mint>", "outputMint":"<mint>", "amount": <lamports>,
  "slippageBps":"50",          // or "dynamic"
  "maxHops": 4,
  "dexes":"raydium,whirlpool"   // optional CSV filter
}
```
Server pushes recalculated quotes whenever pool state changes (slot-based).

`/stream/swap` (pre-built tx stream):
```json
{ "type":"subscribe",
  "inputMint":"...", "outputMint":"...", "amount":<lamports>,
  "userPublicKey":"...",        // REQUIRED here (not in /stream)
  "slippageBps":"50", "maxHops":4, "dexes":"...", "pools":"...",
  "wrapUnwrapSol":true,
  "priorityFee":"medium",       // min|low|medium|high|veryHigh|unsafeMax | exact microLamports
  "maxPriorityFee": <microL>,
  "computeUnitPriceMicroLamports": <int>, "computeUnitLimit": <int>,
  "txVersion":"v0",             // or "legacy"
  "feeAccount":"...","feeBps":<bps>,"feeFromInput":false,"chargeBps":<bps>,
  "destinationTokenAccount":"...",
  "tipAccount":"...","tipLamports":<lamports>
}
```
Server pushes `{ quote, swapTransaction, lastValidBlockHeight }` repeatedly. After **10 slots without an update, server resends the latest** (anti-expiry safeguard). Unsubscribe: `{"type":"unsubscribe","id":"swap_..."}`. Keep-alive: `{"type":"ping"}`.

**Why this is gold for sniping:** you don't pay quote latency at trade time. The transaction is pre-built and sitting in a buffer. When your trigger fires, you sign and ship — that's 10–30 ms vs ~300–500 ms for the `/quote` + `/swap` round trip.

### 7.5 Helius `logsSubscribe` (free fallback)
Standard Solana WS RPC `logsSubscribe` works on any provider. Helius has a public free WS at `wss://mainnet.helius-rpc.com`. Decent for fallback but `meta` arrives only after `confirmed` (~1500 ms). Worse than shredSubscribe for sniping but useful for redundancy when our 2-conn cap is hit.

---

## 8. Risk scoring — full methodology

`/data-api/risk.md` distills risk into a single 1–10 score (10 = highest risk). Major contributing factors:

| Factor | Why it matters |
|---|---|
| `mintAuthority`, `freezeAuthority` not null | Owner can mint more / freeze your wallet |
| Top10 % | Concentration > 30% = high; > 50% = severe |
| Sniper count + % | High sniper concentration = bots ready to dump on graduation |
| Insider count + % | Wallets close to dev/creator |
| Bundler count + % | Multi-wallet entries that often coordinate dump |
| Dev share | > 5% considered high |
| Liquidity | Low absolute → easy rug, ratio to MC |
| LP burn | Not burned → dev can pull |
| `rugged` flag | Solana Tracker's own classifier — IF this is `true`, abort |
| Fees | Anomalous transfer fees = honeypot signal |
| Jupiter verified | Whitelist trust signal |
| Risks list | Specific named flags (e.g. `mint_authority_can_mint`, `freeze_authority`, `low_liquidity`, `high_dev_holding`, `ribi_event`, etc.) |

### Bot's empirically-tuned filter (V41.16b)
**Don't reject on `score >= 8` alone.** Pre-grad pump.fun tokens score 9-10 by structure and many are winners. Reject only on these specific markers:

```python
# Hard reject (proven rug patterns)
if risk.rugged is True: reject
if risk.bundlers.count >= 100: reject     # 100+ bundlers = coordinated entry
if risk.bundlers.totalPercentage > 20: reject   # bundlers hold > 20% of supply
if risk.top10 > 50: reject                 # > 50% in top 10 wallets
if risk.dev.percentage > 15: reject        # dev holds > 15%
if risk.snipers.totalPercentage > 25: reject  # snipers hold > 25%
```

Empirical baseline: `CFWsZSFd...` rugger (rejected) had bundlers.count=147 + top10=68. Winners `GqkStXr3` and `CJiDhsnv` had bundlers.count<50 and top10<35.

---

## 9. Pump.fun deep knowledge (for our exact use case)

### Programs
| Name | Program ID | Phase |
|---|---|---|
| Pump.fun bonding curve | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | pre-graduation |
| PumpSwap AMM | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | post-graduation |
| LetsBONK / Raydium LaunchLab | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | bonk.fun pre-grad |
| Raydium AMM v4 | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | post-grad (legacy) |
| Token Program | `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` | SPL transfers |
| ATA Program | `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL` | ATA derivation |
| Compute Budget | `ComputeBudget111111111111111111111111111111` | priority fees |

### Pump.fun IDL — discriminators (8-byte, offset 0 of instruction data)

**Instructions:**
| Name | Discriminator (bytes) | Meaning |
|---|---|---|
| `buy` | `[102, 6, 61, 18, 1, 218, 235, 234]` | Buy from bonding curve |
| `sell` | `[51, 230, 133, 164, 1, 127, 131, 173]` | Sell into bonding curve |
| `create` | `[24, 30, 200, 40, 5, ...]` | Create new coin + curve |
| `migrate` | `[155, 234, 231, 146, 236, ...]` | Graduate curve → pump_amm |
| `collect_creator_fee` | `[20, 22, 86, 123, 198, ...]` | Creator pulls fees |
| `claim_token_incentives` | `[16, 4, 71, 28, 204, ...]` | Volume cashback claim |
| `init_user_volume_accumulator` | `[94, 6, 202, 115, 255, ...]` | One-time init |
| `close_user_volume_accumulator` | `[249, 69, 164, 218, 150, ...]` | Cleanup |
| `sync_user_volume_accumulator` | `[86, 31, 192, 87, 163, ...]` | Sync state |
| `extend_account` | `[234, 102, 194, 203, 150, ...]` | Account resize |
| `initialize` (global) | `[175, 175, 109, 31, 13, ...]` | Program init (one-time) |
| `set_params` | `[27, 234, 178, 52, 147, ...]` | Update global params |
| `update_global_authority` | `[227, 181, 74, 196, 208, ...]` | Authority change |
| `set_creator` | `[254, 148, 255, 112, 207, ...]` | Set curve creator |
| `sync_creator_with_metadata` | `[138, 96, 174, 217, 48, ...]` | Pull creator from Metaplex |
| `admin_set_creator` | `[69, 25, 171, 142, 57, 239, 13, 4]` | Admin override creator |
| `admin_set_idl_authority` | `[8, 217, 96, 231, 144, ...]` | Admin |
| `admin_update_token_incentives` | `[209, 11, 115, 87, 213, ...]` | Admin |

**Account types** (8-byte discriminator at offset 0 of account data):
| Name | Discriminator |
|---|---|
| `BondingCurve` | `[23, 183, 248, 55, 96, ...]` |
| `FeeConfig` | `[143, 52, 146, 187, 219, ...]` |
| `Global` | `[167, 232, 232, 177, 200, ...]` |
| `GlobalVolumeAccumulator` | `[202, 42, 246, 43, 142, ...]` |
| `UserVolumeAccumulator` | (in IDL) |

**Events** (logged via `Program data: <base64>` in tx logs):
| Name | Discriminator |
|---|---|
| `TradeEvent` | `[189, 219, 127, 211, 78, 230, 97, 238]` |
| `CompleteEvent` | `[95, 114, 97, 156, ...]` (graduation fired) |
| `UpdateGlobalAuthorityEvent` | `[182, 195, 137, 42, 35, 206, 207, 247]` |
| `CreateEvent` | (in IDL) |
| `SetParamsEvent` | (in IDL) |

### `TradeEvent` payload (canonical for parsing)
After 8-byte discriminator, Borsh-decoded little-endian:
```
mint:                       Pubkey   (32)
sol_amount:                 u64      (8)
token_amount:               u64      (8)
is_buy:                     bool     (1)
user:                       Pubkey   (32)
timestamp:                  i64      (8)
virtual_sol_reserves:       u64      (8)
virtual_token_reserves:     u64      (8)
real_sol_reserves:          u64      (8)
real_token_reserves:        u64      (8)
fee_recipient:              Pubkey   (32)
fee_basis_points:           u64      (8)
fee:                        u64      (8)
creator:                    Pubkey   (32)
creator_fee_basis_points:   u64      (8)
creator_fee:                u64      (8)
track_volume:               bool     (1)
total_unclaimed_tokens:     u64      (8)
total_claimed_tokens:       u64      (8)
current_sol_volume:         u64      (8)
last_update_timestamp:      i64      (8)
```

Token decimals = 6, SOL = 9. Price = `sol_amount / token_amount` (in raw units, then × 10^(decimals_token - decimals_sol) = × 10^(6-9) = × 10^-3 — actually price/token in SOL = sol_amount / 10^9 / (token_amount / 10^6) = sol_amount / token_amount × 10^-3).

### Pump.fun program errors (when buy/sell/migrate fails)
| Code | Name | Trigger |
|---|---|---|
| 6002 | `TooMuchSolRequired` | Buy slippage hit (too little tokens for the SOL you sent) |
| 6003 | `TooLittleSolReceived` | Sell slippage hit (too little SOL for the tokens you sold) |
| 6004 | `MintDoesNotMatchBondingCurve` | Wrong bonding curve PDA |
| **6005** | **`BondingCurveComplete`** | **Curve already graduated → MUST switch to pump_amm**. Detect this early! |
| 6006 | `BondingCurveNotComplete` | Tried to migrate too early |
| 6020 | `BuyZeroAmount` | Zero-amount buy |
| 6021 | `NotEnoughTokensToBuy` | Curve nearly depleted |
| 6022 | `SellZeroAmount` | Zero sell |
| 6023 | `NotEnoughTokensToSell` | You don't hold what you tried to sell |
| 6024-6026 | `Overflow`, `Truncation`, `DivisionByZero` | Math errors (rare) |
| 6027 | `NotEnoughRemainingAccounts` | Wrong account count (e.g., post-2026-04-28 upgrade: buy is 18, sell 16/17) |

**Pre-flight detection of 6005**: poll `pool.curvePercentage` from Data API, or read the BondingCurve account directly and check the `complete: bool` field. If `complete=true` use `pAMMBay6...` (pumpswap) instead.

### 2026-04-28 program upgrade (critical, in our memory)
Pump.fun changed the buy account list from 17 → 18 accounts (added `creator_vault` etc.) and sell from 15 → 16/17. Raw-instruction bots that hardcoded the list are now broken. **Solution**: always build instructions through Raptor `/swap`, which tracks DEX upgrades automatically.

### Yellowstone gRPC pump.fun pattern (reference, requires $247/mo)
```js
import Client, { CommitmentLevel, SubscribeRequest } from "@triton-one/yellowstone-grpc";
import { BorshEventCoder } from "@coral-xyz/anchor";

const client = new Client("https://grpc.solanatracker.io", apiKey,
  {"grpc.max_receive_message_length": 100 * 1024 * 1024});
const stream = await client.subscribe();

stream.on("data", (data) => {
  if (data?.transaction) {
    const tx = data.transaction.transaction;
    for (const log of tx.meta?.logMessages ?? []) {
      if (log.startsWith("Program data: ")) {
        const evt = eventCoder.decode(log.slice(14));      // strip prefix
        if (evt?.name === "TradeEvent") { /* mint, isBuy, solAmount, tokenAmount, user */ }
      }
    }
  }
});

const req: SubscribeRequest = {
  accounts:{}, slots:{},
  transactions:{ pump:{ vote:false, failed:false,
                         accountInclude:["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
                         accountExclude:[], accountRequired:[] } },
  transactionsStatus:{}, entry:{}, blocks:{}, blocksMeta:{},
  accountsDataSlice:[], commitment: CommitmentLevel.CONFIRMED, ping:{id:1}
};
stream.write(req);
```
This is what we'd run if we had Yellowstone. Approximately **2-5x faster** than shredSubscribe for parsed events because Yellowstone Jet uses Jito-Shred ingestion AND ships parsed `meta` (logMessages, err, innerInstructions) immediately. Our shredSubscribe path delivers raw shreds without meta — we save latency at the cost of having to parse instructions ourselves.

---

## 10. Raptor swap pipeline (full)

Free hosted at `https://raptor-beta.solanatracker.io`. No key, no rate limits, no charges currently.

### `GET /quote` — get a quote
```
GET /quote?inputMint=<>&outputMint=<>&amount=<lamports>&slippageBps=50
        &dexes=raydium,pumpfun&maxHops=4&onlyDirectRoutes=false&pools=<csv>
        &feeAccount=<>&feeBps=<>&feeFromInput=false&chargeBps=<>
```
Returns full `QuoteResponse`:
```jsonc
{
  "inputMint","outputMint",
  "amountIn","amountOut","minAmountOut",
  "feeAmount","priceImpact","slippageBps",
  "routePlan":[{"programId","dex","pool","inputMint","outputMint","amountIn","amountOut","feeAmount","priceImpact"}],
  "swapUsdValue",
  "priorityFee":{...},
  "platformFee":{...},
  "contextSlot","timeTaken"
}
```

### `POST /swap` — build a transaction (recommended path)
Body:
```jsonc
{
  "userPublicKey":"<wallet>",
  "quoteResponse": <result from /quote>,    // OR omit and pass swap params directly
  "wrapUnwrapSol": true,
  "txVersion": "v0",                          // or "legacy"
  "computeUnitPriceMicroLamports": <int>,
  "computeUnitLimit": <int>,
  "priorityFee": "high",                      // levels or microLamports
  "maxPriorityFee": <microL cap>,
  "tipAccount": "<jito-tip-acct>",
  "tipLamports": <lamports>,
  "feeAccount":"...","feeBps":<>,"feeFromInput":false,"chargeBps":<>,
  "destinationTokenAccount":"<custom ATA>"
}
```
Returns `{ swapTransaction (base64), lastValidBlockHeight, ... }`.

### `POST /quote-and-swap` — single-call combo
Body: `userPublicKey`, `inputMint`, `outputMint`, `amount` (required) + all optional swap params from above.
Returns: `{ quote: QuoteResponse, swapTransaction: base64, lastValidBlockHeight }`.

**Use this for sniping** — saves one round-trip.

### `POST /swap-instructions` — for tx composition
Returns raw instructions:
```jsonc
{
  "tokenLedgerInstruction":   <SerializedInstruction>?,
  "computeBudgetInstructions":[<SerializedInstruction>],
  "setupInstructions":        [<SerializedInstruction>],
  "swapInstruction":          <SerializedInstruction>,
  "cleanupInstruction":       <SerializedInstruction>?,
  "addressLookupTableAddresses": [<addr>]
}
```
`SerializedInstruction = {programId, accounts:[{pubkey,isSigner,isWritable}], data:base64}`. Use when bundling with other ixs (e.g., a pre-buy SOL transfer or a same-block multi-token grab).

### `POST /send-transaction` — Yellowstone Jet TPU sender
Body: `{"transaction":"<base64-signed-tx>"}`. Returns `{signature, signature_base64, success}` immediately. Behind the scenes: 4 random identities (configurable), automatic re-send for up to 30 s, Jet TPU connection per-leader.

### `GET /transaction/{signature}` — poll status
Status: `pending` | `confirmed` | `failed` | `expired`. Includes `latency_ms`, parsed `events` (Raptor `SwapEvent`, `SwapCompleteEvent`, `PlaceOrderEvent`, `FillOrderEvent`, `CancelOrderEvent`, `UpdateOrderEvent`).
Errors: 400 invalid tx, 404 not tracked, 503 sender disabled.

### Supported DEXs (auto-routed)
- **Raydium**: AMM, CLMM, CPMM, LaunchLab/Launchpad
- **Meteora**: DLMM, Dynamic AMM, DAMM (V2), Curve, DBC
- **Orca**: Whirlpool, Whirlpool V2
- **Bonding curves**: Pump.fun, PumpSwap, Heaven, MoonIt, Boopfun
- **PropAMM**: Humidifi, Tessera, Solfi V1/V2, AlphaQ, ZeroFi, BisonFi, GoonFi V2
- **Other**: FluxBeam, PancakeSwap V3

### Priority fee levels (Raptor mapping)
`Min` / `Low` (cost-saving) → `Auto` / `Medium` (default) → `High` / `VeryHigh` (speed) → `Turbo` / `UnsafeMax` (max).

### Tips & dynamic slippage
- `slippageBps: "dynamic"` → Raptor auto-calculates based on volatility + route depth.
- `tipAccount` + `tipLamports` adds a SOL tip transfer (Jito tip pattern).
- Setting `onlyDirectRoutes=true` (or `maxHops=1`) reduces tx size — useful when the curve is too packed for 4-hop routes.

---

## 11. Practical bot recipes

### Recipe A — Copy-trade with shredSubscribe (no Datastream needed)
1. Build wallet allowlist of top traders (call `GET /top-traders/all` once, filter `winPercentage>=55%`, `total>0`, `totalWins>=100`).
2. Open ONE shredSubscribe with `accountInclude=[wallet1, wallet2, ...]` (1 conn covers up to ~100 wallets comfortably; 2-conn cap is the bottleneck).
3. On notification: parse outermost `instructions[]` for `programId == 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` (or pumpswap `pAMMBay6...`). If first 8 bytes of `data` (base58-decoded) match `buy` discriminator → wallet just bought. Extract `mint` from instruction accounts (account index 2 in pump.fun buy ix is the mint).
4. Race-safe dedup: `graduated_seen.add(mint)` BEFORE running rug-check (we hit a race in V41.16 where two parallel shred messages for same mint both passed dedup).
5. Run rug-check via `GET /tokens/{mint}` (1.5s timeout) — see §8.
6. If safe: skip the 8-second observation period and go straight to copy_fast entry path.
7. Build entry tx via `POST /raptor/quote-and-swap`, sign, ship via `POST /raptor/send-transaction`.
8. Manage exit via trailing stop (peak × 0.96, activation at 1.04x, min lock 1.04x).

### Recipe B — Graduation snipe (pre-grad → post-grad transition)
1. Poll `GET /tokens/multi/graduating?limit=500&minCurve=85` every 5–10s.
2. For each near-grad mint: subscribe via accountSubscribe to its BondingCurve PDA (`pool.curve` from Data API). When account data shows `complete=1`, graduation is imminent.
3. Better: subscribe `programSubscribe` to `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` filtered by memcmp on the BondingCurve discriminator + the specific mint, and watch for the `complete=true` flip in the account data.
4. Even better (paid): Datastream `pumpfun:curve:99` and `pumpfun:curve:100` — single-message graduation alerts.
5. On graduation: switch entry to PumpSwap (`pAMMBay6...`) or whatever Raptor chooses.

### Recipe C — Cashback-coin discovery (free positive expectancy as of 2026)
1. Pump.fun cashback coins are detected via byte 82 of the BondingCurve account data (a flag bit per memory).
2. Use `getProgramAccountsV2` with memcmp for BondingCurve discriminator + a slice covering byte 82, paginated, filtering for the cashback flag.
3. These earn 0.3% fee redistribution by trader volume share — even losing trades earn back some fee.

### Recipe D — Risk gate (called before every entry)
```python
def rug_check(mint: str) -> tuple[bool, str]:
    try:
        r = requests.get(f"{DATA_BASE}/tokens/{mint}",
                        headers={"x-api-key": API_KEY}, timeout=1.5).json()
        risk = r.get("risk", {})
        if risk.get("rugged"): return False, "rugged"
        if risk.get("bundlers", {}).get("count", 0) >= 100: return False, "bundlers>=100"
        if risk.get("bundlers", {}).get("totalPercentage", 0) > 20: return False, "bundlers>20%"
        if risk.get("top10", 0) > 50: return False, "top10>50%"
        if risk.get("dev", {}).get("percentage", 0) > 15: return False, "dev>15%"
        if risk.get("snipers", {}).get("totalPercentage", 0) > 25: return False, "snipers>25%"
        sec = (r.get("pools") or [{}])[0].get("security", {})
        if sec.get("freezeAuthority"): return False, "freezeAuthority"
        if sec.get("mintAuthority"): return False, "mintAuthority"
        return True, "ok"
    except Exception as e:
        return True, f"check_failed_{e}"   # fail-open or fail-closed — your call
```

### Recipe E — Priority fee for sniping
```python
r = requests.post(f"{RPC_HTTP}",
  json={"jsonrpc":"2.0","id":1,"method":"getPriorityFeeEstimate",
        "params":[{"accountKeys":[PUMP_PROGRAM, mint], "options":{"priorityLevel":"VeryHigh"}}]}).json()
fee_microL_per_cu = r["result"]["priorityFeeEstimate"]
# Cap at 100k microL/CU to avoid getting fleeced
fee_microL_per_cu = min(fee_microL_per_cu, 100_000)
```
Then pass to Raptor as `computeUnitPriceMicroLamports` or `priorityFee:"VeryHigh"` + `maxPriorityFee:100000`.

---

## 12. Gotchas & lessons (everything that has bitten us)

1. **`getTransaction` rejects `processed` commitment.** Minimum is `confirmed`. We had 100% silent copy-trade failure for hours because of this. Always log unhandled exceptions in the shred handler.
2. **Race between two shredSubscribe handlers for the same mint.** Two top traders bought within ~30 ms; both notifications fired; both passed `graduated_seen` dedup; both ran rug-check. Fix: claim the mint **before** any await — `graduated_seen.add(mint)` first, then `await rug_check`.
3. **MAX_CONCURRENT_POSITIONS silently rejecting `copy_fast`.** Cap of 3 was hit by 5 grad-imminent positions, so copy entries were dropped. Fix: exempt strategy types from the cap or cap per-strategy.
4. **Stale graduation curve price.** When `pool.curve.complete=true`, the curve price is frozen at graduation. Use a fresh `/quote` against the new pool (`pumpfun-amm`) for entry pricing.
5. **Score-based reject blocked winners.** Pump.fun pre-grad tokens score 9-10 by structure. Only the specific markers in §8 should reject.
6. **Datastream silent-fail under €1 plan.** Connection succeeds, no messages arrive — because Datastream is Premium-only (€397/mo). Don't waste time on it without paying.
7. **2-conn WS cap.** RPC plan allows only 2 concurrent WS connections. Don't open 1 per wallet — open 1 with `accountInclude=[100 wallets]`.
8. **shred `meta` is always null.** Cannot read `logMessages` to decode `TradeEvent`. Either parse instruction data yourself, or fetch parsed tx via `getTransaction` after the fact (but that's 10 credits + ~600ms latency).
9. **`processed` commitment and accountSubscribe.** `accountSubscribe` DOES support `processed`. Use it for cheap-and-fast curve state reads (no credits, no rate limit).
10. **Pump.fun Apr 2026 program upgrade** silently broke raw-instruction bots: buy is now 18 accounts (was 17), sell 16/17 (was 15/16). Build via Raptor — it tracks upgrades automatically.
11. **Solana has no mempool.** "Predict rugs before they confirm" is structurally impossible. Even paid Geyser sees txs faster, not pre-confirmation.
12. **Pump.fun smart-wallet copy-trading is structurally lossy at retail latency.** Elite wallets fire at 50–90% curve, AND even rare <50% follows get peak=1.00x because the smart wallet's own buy IS the pump. Only Geyser gRPC + Jito bundles fix this — and we don't have either.
13. **Sample-size noise.** 3–9 trades is noise. Don't tune mid-session. Lock params, run 50+ trades.
14. **Don't force trades in calm markets.** "Empty scan" = no setup, not a reason to lower the bar.

---

## 13. Cheat sheet (paste-able)

```python
# Auth
RPC_HTTP   = "https://rpc-mainnet.solanatracker.io"
RPC_WS     = "wss://rpc-mainnet.solanatracker.io"
DATA_BASE  = "https://data.solanatracker.io"
RAPTOR     = "https://raptor-beta.solanatracker.io"
RAPTOR_WS  = "wss://raptor-beta.solanatracker.io"
HEADERS    = {"x-api-key": SOLANATRACKER_API_KEY}              # Data API
RPC_QS     = {"api_key": SOLANATRACKER_RPC_KEY}                # RPC
# Programs
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP     = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
LETSBONK     = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
SOL_MINT     = "So11111111111111111111111111111111111111112"
USDC_MINT    = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROG   = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Raptor priority levels
PRIORITY = {"snipe": "VeryHigh", "normal": "Medium", "exit": "High"}
# Pump.fun discriminators
DISC_BUY   = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_SELL  = bytes([51, 230, 133, 164, 1, 127, 131, 173])
DISC_TRADE_EVT  = bytes([189, 219, 127, 211, 78, 230, 97, 238])
DISC_BCURVE_ACCT = bytes([23, 183, 248, 55, 96])  # first 5 of 8
```

```jsonc
// shredSubscribe template (RPC WS)
{"jsonrpc":"2.0","id":1,"method":"shredSubscribe",
 "params":[{"accountInclude":["WALLET1","WALLET2"], "vote":false},
           {"encoding":"jsonParsed","transactionDetails":"full","maxSupportedTransactionVersion":0}]}
```

```bash
# Quick-test paths
curl -s "$DATA_BASE/tokens/$MINT" -H "x-api-key: $KEY"                               # token info
curl -s "$DATA_BASE/top-traders/all?sortBy=winPercentage" -H "x-api-key: $KEY" | jq  # top traders
curl -s "$RAPTOR/quote?inputMint=$SOL&outputMint=$MINT&amount=1000000000"            # quote
curl -s -X POST "$RPC_HTTP/?api_key=$RPC_KEY" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getPriorityFeeEstimate","params":[{"accountKeys":["'$PUMP_PROGRAM'"],"options":{"recommended":true}}]}'
```

---

## 14. What to read next (when paying matters)

If we ever budget for paid tiers, here's the priority order:

1. **Yellowstone gRPC ($247/mo)** — biggest sniper-bot upgrade. ~50–100 ms faster than shreds with parsed `meta` included. Doesn't fix Solana's no-mempool problem but does fix our parse-overhead problem. With this, copy_fast goes from "lossy on smart wallets" to "competitive with Geyser bots".

2. **Datastream Premium (€397/mo)** — only worth it if we want >2 WS conns or want to subscribe to 5+ different channel types simultaneously. The `wallet:` and `transaction:` rooms are nice but our shredSubscribe replicates them at 30% the latency. The `<market>:curve:N` rooms are unique value (graduation prediction) — those alone could justify it for a graduation-focused bot.

3. **Data API Pro** — only if we hit 10k req/mo cap (we won't with current usage).

4. **RPC Pro tier** — if we exceed 500k credits or need >10 RPS / >2 WS conns. We're nowhere near.

---

End of mastery doc. Update this file when you learn something new — don't trust the index alone.
