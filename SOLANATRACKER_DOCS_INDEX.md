# Solana Tracker Documentation Index

Source: https://docs.solanatracker.io/llms.txt (LLM-friendly index — append `.md` to any path for raw markdown).

## Data API — Tokens

- [Get Token Information](https://docs.solanatracker.io/data-api/tokens/get-token-information.md) — comprehensive token info (risk, pools, holders, events)
- [Get Latest Tokens](https://docs.solanatracker.io/data-api/tokens/get-latest-tokens.md) — last 100 created
- [Get Multiple Tokens](https://docs.solanatracker.io/data-api/tokens/get-multiple-tokens.md) — batch up to 20
- [Get Token by Pool Address](https://docs.solanatracker.io/data-api/tokens/get-token-by-pool-address.md)
- [Get Token Overview](https://docs.solanatracker.io/data-api/tokens/get-token-overview.md) — Axiom Pulse / Photon Memescope style (latest + graduating + graduated)
- [Get Trending Tokens](https://docs.solanatracker.io/data-api/tokens/get-trending-tokens.md) — top 100 by volume
- [Get Trending Tokens by Timeframe](https://docs.solanatracker.io/data-api/tokens/get-trending-tokens-by-timeframe.md)
- [Get Tokens by Volume](https://docs.solanatracker.io/data-api/tokens/get-tokens-by-volume.md)
- [Get Tokens by Volume with Timeframe](https://docs.solanatracker.io/data-api/tokens/get-tokens-by-volume-with-timeframe.md)
- [Get Top Performing Tokens](https://docs.solanatracker.io/data-api/tokens/get-top-performing-tokens.md) — launched today
- [Get Graduated Tokens](https://docs.solanatracker.io/data-api/tokens/get-graduated-tokens.md)
- [Get Graduating Tokens](https://docs.solanatracker.io/data-api/tokens/get-graduating-tokens.md)
- [Get Token Holders (Top 100)](https://docs.solanatracker.io/data-api/tokens/get-token-holders-top-100.md)
- [Get Top 20 Token Holders](https://docs.solanatracker.io/data-api/tokens/get-top-20-token-holders.md) — excludes LP wallets
- [Get All Token Holders (Paginated)](https://docs.solanatracker.io/data-api/tokens/get-all-token-holders-paginated.md) — up to 5000/page
- [Get Token Bundlers](https://docs.solanatracker.io/data-api/tokens/get-token-bundlers.md) — top 500 bundler wallets + stats
- [Get All-Time High Price](https://docs.solanatracker.io/data-api/tokens/get-all-time-high-price.md)
- [Get Tokens by Deployer](https://docs.solanatracker.io/data-api/tokens/get-tokens-by-deployer.md)

## Data API — Top Traders & PnL (copy-trade signal source)

- [Get Top Traders (All Tokens)](https://docs.solanatracker.io/data-api/top-traders/get-top-traders-all-tokens.md)
- [Get Top Traders (All Tokens) with Pagination](https://docs.solanatracker.io/data-api/top-traders/get-top-traders-all-tokens-with-pagination.md) — `/top-traders/all/{page}`, 25/page
- [Get Top Traders for Specific Token](https://docs.solanatracker.io/data-api/top-traders/get-top-traders-for-specific-token.md) — top 100 traders by PnL for a token
- [Get First Token Buyers](https://docs.solanatracker.io/data-api/pnl/get-first-token-buyers.md) — first 100 buyers + PnL
- [Get Wallet PnL](https://docs.solanatracker.io/data-api/pnl/get-wallet-pnl.md)
- [Get Token-Specific PnL](https://docs.solanatracker.io/data-api/pnl/get-token-specific-pnl.md)

## Data API — Wallet

- [Get Wallet Tokens](https://docs.solanatracker.io/data-api/wallet/get-wallet-tokens.md)
- [Get Wallet Tokens with Pagination](https://docs.solanatracker.io/data-api/wallet/get-wallet-tokens-with-pagination.md)
- [Get Basic Wallet Information](https://docs.solanatracker.io/data-api/wallet/get-basic-wallet-information.md)
- [Get Wallet Trades](https://docs.solanatracker.io/data-api/wallet/get-wallet-trades.md) — wallet swap history
- [Get Wallet Portfolio Chart](https://docs.solanatracker.io/data-api/wallet/get-wallet-portfolio-chart.md)

## Data API — Trades & Stats

- [Get Token Trades](https://docs.solanatracker.io/data-api/trades/get-token-trades.md)
- [Get Pool-Specific Trades](https://docs.solanatracker.io/data-api/trades/get-pool-specific-trades.md)
- [Get User-Specific Token Trades](https://docs.solanatracker.io/data-api/trades/get-user-specific-token-trades.md)
- [Get User-Specific Pool Trades](https://docs.solanatracker.io/data-api/trades/get-user-specific-pool-trades.md)
- [Get Token Stats](https://docs.solanatracker.io/data-api/stats/get-token-stats.md) — multi-timeframe
- [Get Token-Pool Stats](https://docs.solanatracker.io/data-api/stats/get-token-pool-stats.md)
- [Get Token Events](https://docs.solanatracker.io/data-api/events/get-token-events.md) — raw binary events
- [Get Pool Events](https://docs.solanatracker.io/data-api/events/get-pool-events.md)

## Data API — Charts (OHLCV)

- [Get OHLCV Data for a token](https://docs.solanatracker.io/data-api/chart/get-ohlcv-data-for-a-token.md)
- [Get OHLCV Data for a token/pool pair](https://docs.solanatracker.io/data-api/chart/get-ohlcv-data-for-a-tokenpool-pair.md)
- [Get Bundlers Chart Data](https://docs.solanatracker.io/data-api/chart/get-bundlers-chart-data.md)
- [Get Holders Chart Data](https://docs.solanatracker.io/data-api/chart/get-holders-chart-data.md)
- [Get Insiders Chart Data](https://docs.solanatracker.io/data-api/chart/get-insiders-chart-data.md)
- [Get Snipers Chart Data](https://docs.solanatracker.io/data-api/chart/get-snipers-chart-data.md)

## Data API — Price

- [Get Token Price](https://docs.solanatracker.io/data-api/price/get-token-price.md)
- [Get Multiple Token Prices](https://docs.solanatracker.io/data-api/price/get-multiple-token-prices.md) — up to 100
- [Post Multiple Token Prices](https://docs.solanatracker.io/data-api/price/post-multiple-token-prices.md)
- [Get Historic Price Information](https://docs.solanatracker.io/data-api/price/get-historic-price-information.md)
- [Get Price at Specific Timestamp](https://docs.solanatracker.io/data-api/price/get-price-at-specific-timestamp.md)
- [Get lowest and highest price in time range](https://docs.solanatracker.io/data-api/price/get-lowest-and-highest-price-in-time-range.md)

## Data API — Risk & Account

- [Risk Score](https://docs.solanatracker.io/data-api/risk.md) — full scoring methodology, 1-10 scale, factors
- [Token Search](https://docs.solanatracker.io/data-api/search/token-search.md) — extensive filter options
- [Get API Credits](https://docs.solanatracker.io/data-api/credits/get-api-credits.md)
- [Get Subscription Information](https://docs.solanatracker.io/data-api/credits/get-subscription-information.md)
- [OpenAPI spec](https://docs.solanatracker.io/data-api/openapi.json)

## Datastream WebSocket (PREMIUM €397/mo+ ONLY)

WebSocket URL: `wss://datastream.solanatracker.io/{API_KEY}` — sub `{"type":"join","room":"<channel>"}`

- [Latest tokens](https://docs.solanatracker.io/datastream/websockets/latesttokens.md) — new mints
- [Graduating](https://docs.solanatracker.io/datastream/websockets/graduating.md) — approaching grad
- [Graduated](https://docs.solanatracker.io/datastream/websockets/graduated.md)
- [Token transactions](https://docs.solanatracker.io/datastream/websockets/tokentransactions.md) — `tx:token:{mint}` — swap stream for a token
- [Wallet transactions](https://docs.solanatracker.io/datastream/websockets/wallettransactions.md) — `tx:wallet:{wallet}` — swaps for a wallet (alternative to shredSubscribe)
- [Pool transactions](https://docs.solanatracker.io/datastream/websockets/pooltransactions.md)
- [Pool wallet transactions](https://docs.solanatracker.io/datastream/websockets/poolwallettransactions.md)
- [Holders](https://docs.solanatracker.io/datastream/websockets/holders.md) — holder count changes
- [Top10 holders](https://docs.solanatracker.io/datastream/websockets/top10holders.md) — concentration changes
- [Bundlers](https://docs.solanatracker.io/datastream/websockets/bundlers.md)
- [Snipers](https://docs.solanatracker.io/datastream/websockets/snipertracking.md)
- [Insiders](https://docs.solanatracker.io/datastream/websockets/insidertracking.md)
- [Developer holdings](https://docs.solanatracker.io/datastream/websockets/developerholdings.md)
- [Curve percentage](https://docs.solanatracker.io/datastream/websockets/curvepercentage.md) — bonding curve progress alerts
- [Fee tracking](https://docs.solanatracker.io/datastream/websockets/feetracking.md)
- [Metadata](https://docs.solanatracker.io/datastream/websockets/metadata.md)
- [Pool statistics](https://docs.solanatracker.io/datastream/websockets/poolstatistics.md)
- [Pool statistics total](https://docs.solanatracker.io/datastream/websockets/poolstatisticstotal.md)
- [Pool updates](https://docs.solanatracker.io/datastream/websockets/poolupdates.md)
- [Pool volume](https://docs.solanatracker.io/datastream/websockets/poolvolume.md)
- [Token volume](https://docs.solanatracker.io/datastream/websockets/tokenvolume.md)
- [Token statistics](https://docs.solanatracker.io/datastream/websockets/tokenstatistics.md)
- [Token statistics total](https://docs.solanatracker.io/datastream/websockets/tokenstatisticstotal.md)
- [Token primary](https://docs.solanatracker.io/datastream/websockets/tokenprimary.md)
- [Token changes](https://docs.solanatracker.io/datastream/websockets/tokenchanges.md)
- [Price by token](https://docs.solanatracker.io/datastream/websockets/pricebytoken.md)
- [Price by pool](https://docs.solanatracker.io/datastream/websockets/pricebypool.md)
- [Price aggregated](https://docs.solanatracker.io/datastream/websockets/priceaggregated.md)
- [Price all pools](https://docs.solanatracker.io/datastream/websockets/priceallpools.md)
- [Wallet balance](https://docs.solanatracker.io/datastream/websockets/walletbalance.md)
- [Wallet token balance](https://docs.solanatracker.io/datastream/websockets/wallettokenbalance.md)
- [AsyncAPI spec](https://docs.solanatracker.io/datastream/asyncapi.json)

## Solana RPC HTTP Methods (€1 RPC plan = WE HAVE THIS)

URL: `https://rpc-mainnet.solanatracker.io/?api_key={KEY}`

- [getPriorityFeeEstimate](https://docs.solanatracker.io/solana-rpc/http/getpriorityfeeestimate.md) — recommended fees, all plans
- [getProgramAccountsV2](https://docs.solanatracker.io/solana-rpc/http/getprogramaccountsv2.md) — cursor-based pagination
- [getTokenAccountsByOwnerV2](https://docs.solanatracker.io/solana-rpc/http/gettokenaccountsbyownerv2.md) — V2 enhanced
- [getTokenAccountsByOwners](https://docs.solanatracker.io/solana-rpc/http/gettokenaccountsbyowners.md) — batch up to 250 wallets
- [getTokenLargestAccounts](https://docs.solanatracker.io/solana-rpc/http/gettokenlargestaccounts.md) — top 20 token accounts
- [getTransaction](https://docs.solanatracker.io/solana-rpc/http/gettransaction.md) — **NOTE: requires `confirmed` minimum, NOT `processed`**
- [getSignaturesForAddress](https://docs.solanatracker.io/solana-rpc/http/getsignaturesforaddress.md)
- [sendTransaction](https://docs.solanatracker.io/solana-rpc/http/sendtransaction.md)
- [simulateTransaction](https://docs.solanatracker.io/solana-rpc/http/simulatetransaction.md)
- [getRecentPrioritizationFees](https://docs.solanatracker.io/solana-rpc/http/getrecentprioritizationfees.md)
- (full list in llms.txt — accountInfo, balance, block, supply, etc.)
- [Credits and Rate Limits](https://docs.solanatracker.io/solana-rpc/credits-and-rate-limits.md)

## Solana RPC WebSocket (€1 RPC plan)

URL: `wss://rpc-mainnet.solanatracker.io/?api_key={KEY}`

- [Shred subscribe](https://docs.solanatracker.io/solana-rpc/websockets/shredsubscribe.md) — **50-150ms latency, raw shreds before block confirmation, FREE on €1 plan**
- [Account subscribe](https://docs.solanatracker.io/solana-rpc/websockets/accountsubscribe.md)
- [Logs subscribe](https://docs.solanatracker.io/solana-rpc/websockets/logssubscribe.md)
- [Program subscribe](https://docs.solanatracker.io/solana-rpc/websockets/programsubscribe.md)
- [Block subscribe](https://docs.solanatracker.io/solana-rpc/websockets/blocksubscribe.md)
- [Slot subscribe](https://docs.solanatracker.io/solana-rpc/websockets/slotsubscribe.md)
- [Slots updates subscribe](https://docs.solanatracker.io/solana-rpc/websockets/slotsupdatessubscribe.md)
- [Signature subscribe](https://docs.solanatracker.io/solana-rpc/websockets/signaturesubscribe.md)
- [Root subscribe](https://docs.solanatracker.io/solana-rpc/websockets/rootsubscribe.md)
- [Vote subscribe](https://docs.solanatracker.io/solana-rpc/websockets/votesubscribe.md)
- [shredstream AsyncAPI spec](https://docs.solanatracker.io/solana-rpc/websocket/shredstream.json)

## Raptor (DEX Aggregator — FREE hosted endpoint)

Hosted: `https://raptor-beta.solanatracker.io/` — works without API key on /quote

- [Overview](https://docs.solanatracker.io/raptor/overview.md)
- [Get swap quote](https://docs.solanatracker.io/raptor/http/get-swap-quote.md)
- [Build swap transaction](https://docs.solanatracker.io/raptor/http/build-swap-transaction.md)
- [Build swap instructions](https://docs.solanatracker.io/raptor/http/build-swap-instructions.md)
- [Quote and swap in one request](https://docs.solanatracker.io/raptor/http/quote-and-swap-in-one-request.md)
- [Send transaction via Yellowstone Jet TPU](https://docs.solanatracker.io/raptor/http/send-transaction-via-yellowstone-jet-tpu.md) — for LIVE mode
- [Get transaction status](https://docs.solanatracker.io/raptor/http/get-transaction-status.md)
- [Transactions](https://docs.solanatracker.io/raptor/transactions.md)
- [/stream — quote stream](https://docs.solanatracker.io/raptor/websocket/websockets/stream.md)
- [/stream/swap — pre-built tx stream](https://docs.solanatracker.io/raptor/websocket/websockets/streamswap.md)

## Yellowstone gRPC (€200/mo+ — premium tier)

- [Yellowstone gRPC overview](https://docs.solanatracker.io/yellowstone-grpc/index.md)
- [Quickstart Guide](https://docs.solanatracker.io/yellowstone-grpc/quickstart.md)
- [Authentication & Setup](https://docs.solanatracker.io/yellowstone-grpc/authentication.md)
- [Account Monitoring](https://docs.solanatracker.io/yellowstone-grpc/account-monitoring.md)
- [Transaction Monitoring](https://docs.solanatracker.io/yellowstone-grpc/transaction-monitoring.md)
- [Slot & Block Monitoring](https://docs.solanatracker.io/yellowstone-grpc/slot-block-monitoring.md)
- [Entry Monitoring](https://docs.solanatracker.io/yellowstone-grpc/entry-monitoring.md)
- [Best Practices](https://docs.solanatracker.io/yellowstone-grpc/best-practices.md)
- [Pump.fun Account Streaming example](https://docs.solanatracker.io/yellowstone-grpc/examples/pumpfun-accounts.md)
- [Pump.fun Buy/Sell Detection example](https://docs.solanatracker.io/yellowstone-grpc/examples/pumpfun-transactions.md) — exact thing we need but paid

## Swap API (alternate)

- [Libraries](https://docs.solanatracker.io/swap-api/libraries.md) — official SDKs
- [Rate](https://docs.solanatracker.io/swap-api/rate.md)
- [Swap](https://docs.solanatracker.io/swap-api/swap.md)

## Other

- [Quick Start](https://docs.solanatracker.io/quickstart.md)
- [AI Integration](https://docs.solanatracker.io/ai.md)
- [Changelog](https://docs.solanatracker.io/changelog.md)
- [Status](https://status.solanatracker.io)
- [Discord](https://discord.gg/JH2e9rR9fc)

---

## Tier-aware capability map (what WE actually have)

| Capability | Tier needed | We have? |
|---|---|---|
| REST Data API (10k req/mo) | Free Data API | ✅ |
| shredSubscribe (50-150ms) | €1 one-time RPC | ✅ |
| getPriorityFeeEstimate, getProgramAccountsV2 | €1 one-time RPC | ✅ |
| Raptor /quote, /swap, /send-transaction | Free (hosted) | ✅ |
| Datastream `tx:wallet`, `tx:token`, `holders`, `top10holders` etc. | Premium €397/mo | ❌ |
| Yellowstone gRPC (50-150ms) | Business €399/mo or standalone €200/mo | ❌ |

## Critical findings from this index

1. **`tx:wallet:{addr}` Datastream** would be the IDEAL copy-trade source (push-based, no parsing needed) — but Premium-only.
2. **shredSubscribe** + getTransaction (Confirmed minimum, NOT Processed) is our current copy-trade pipeline.
3. **getProgramAccountsV2** could be used to monitor pool state changes more efficiently.
4. **`/raptor/http/send-transaction-via-yellowstone-jet-tpu`** is the LIVE mode trade execution path when we go live.
5. **`/data-api/top-traders/get-top-traders-for-specific-token`** could give us top traders for a SPECIFIC token (not all tokens) — useful when a token is hot.
6. **`/data-api/pnl/get-first-token-buyers`** could identify the first 100 buyers of a winning token — see if early buyers are still holding (predicts dump risk).
