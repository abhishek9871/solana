# SOLANA MEMECOIN SNIPER — SETUP GUIDE

## 1. CREATE A SOLANA WALLET

**Use Phantom (recommended) or Solflare:**
1. Install Phantom browser extension or mobile app: https://phantom.com
2. Create new wallet → save 12-word seed phrase securely (paper, NOT screenshot)
3. Settings → Security & Privacy → Export Private Key
4. Enter password → COPY the base58 string (looks like `5Kx...`, ~88 chars)

**WARNING:** Anyone with this private key can drain your wallet. Treat it like cash.

## 2. GET A SOLANA RPC URL (FREE)

Default RPC is rate-limited. For sniping you need a faster one:

**Helius (recommended, free tier):**
1. Sign up: https://helius.xyz
2. Create API key
3. URL format: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`

**QuickNode (alternative):**
1. https://quicknode.com → free Solana endpoint

## 3. FUND YOUR WALLET

You need SOL on Solana mainnet. Two paths:

**Path A: From Binance (cheapest)**
1. On Binance, BUY SOL with your USDT (use spot, not futures)
2. Go to Wallet → Withdraw → Pick SOL
3. Network: **Solana (SOL)** — DO NOT pick BNB chain
4. Paste your Phantom address
5. Withdraw amount: at least 0.1 SOL ($20 at current prices)
6. Withdrawal fee: ~0.001 SOL ($0.20)

**Path B: From any other CEX with SOL withdrawal support**

After arrival (~1 min), refresh Phantom — you should see SOL.

## 4. CONFIGURE THE BOT

Open Command Prompt or PowerShell:

```powershell
# Set environment variables (this session only)
set SOLANA_PRIVATE_KEY=YOUR_BASE58_PRIVATE_KEY_HERE
set SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY
```

For permanent setup:
```powershell
setx SOLANA_PRIVATE_KEY "YOUR_BASE58_PRIVATE_KEY_HERE"
setx SOLANA_RPC_URL "https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY"
```

## 5. RUN IN PAPER MODE FIRST (RECOMMENDED)

```powershell
cd C:\Users\VASU\Desktop\tradingMahadevjiwin
py solana_sniper.py
```

`PAPER_TRADING = True` is the default. It will:
- Detect new mints
- Run safety checks
- Print "would buy" but NOT spend SOL
- Simulate position management

Watch logs for ~1 hour. Verify it's finding tokens, doing safety checks correctly, and "would have" snipes look reasonable.

## 6. GO LIVE

When you're confident the bot is working in paper mode:

1. Edit `solana_sniper.py`
2. Change `PAPER_TRADING = True` to `PAPER_TRADING = False`
3. Save
4. Run again: `py solana_sniper.py`

**Now real SOL is at risk.** Default config:
- 0.05 SOL per snipe (~$10)
- Max 3 concurrent positions
- Session loss limit: 0.20 SOL (~$40)
- Auto-halts on 3 consecutive losses

## 7. KEY SETTINGS TO TUNE

In `solana_sniper.py`:

| Variable | Default | What it does |
|----------|---------|--------------|
| `SNIPE_AMOUNT_SOL` | 0.05 | SOL per snipe — set lower with smaller wallet |
| `MAX_SLIPPAGE_BPS` | 1500 | 15% slippage — memecoins are volatile, don't lower too much |
| `PRIORITY_FEE_LAMPORTS` | 200000 | ~$0.04 per tx — higher = faster inclusion |
| `TP_LADDER` | 1.25/1.5/2/3x | Take-profit levels |
| `SL_PCT` | -0.40 | Stop loss at -40% from entry |
| `MAX_TOP10_CONCENTRATION` | 0.30 | Reject if top 10 holders own >30% (rug risk) |
| `MAX_SESSION_LOSS_SOL` | 0.20 | Hard halt after 0.2 SOL session loss |

## 8. EXPECTATIONS — THE HARSH MATH

**Documented success rates from research:**
- 90% of memecoin snipes lose money (rug pulls, no momentum, dies)
- 10% pay 5x to 100x
- A SINGLE successful snipe of 50-100x can make a session

**At 0.05 SOL per snipe ($10):**
- 10 snipes → 9 losers (-$90), 1 winner at 5x (+$50) = -$40 session
- 10 snipes → 9 losers (-$90), 1 winner at 50x (+$500) = +$410 session
- The math depends ENTIRELY on outlier wins

**You will see streaks of losses.** That's normal. The strategy only works over MANY attempts. With $20 wallet, you can fund 4-5 attempts before halt — likely insufficient sample size to hit a winner.

**Honest recommendation:** start with at least $100 SOL ($150-200 in current SOL price) for meaningful sample size.

## 9. TROUBLESHOOTING

**"no private key" error in live mode:** env var not set. Re-check step 4.

**"RPC connection failed":** Check your RPC URL. Try the public default:
`https://api.mainnet-beta.solana.com` (slower but free).

**Bot is running but never snipes:** Pump.fun launches happen frequently but most fail safety checks. This is GOOD — it's preventing rug entries. Wait 30-60 min and you should see attempts.

**Buy succeeds but sell fails:** Could be a honeypot we missed. The bot will retry next price check. If repeated failures, manually sell via Phantom + Jupiter Swap UI.

**"no Jupiter route":** Token hasn't graduated to Raydium yet. Bot skips. This is a limitation — we miss the earliest sniping window. Trade-off: more reliable, less alpha.

## 10. SAFETY REMINDERS

- **Never share your private key.** Anyone who has it can drain your wallet.
- **Test in paper mode first.** Always.
- **Start small.** 0.01-0.02 SOL per snipe initially even when going live.
- **Don't run multiple instances** — they may try to snipe the same mint and conflict.
- **Monitor it.** Don't leave running unattended for first day.
- **The bot will lose money sometimes.** That's the strategy. The wins must outweigh.

## 11. KILL SWITCH

To stop the bot:
- Ctrl+C in the terminal — closes immediately
- Active positions: server-side TP/SL not used here (Jupiter doesn't support that), so positions are managed in-memory. If you Ctrl+C with open positions, manually sell via Phantom UI.

---

**This is v1.0 — production-quality core but expect to refine settings based on results.** Run paper for an hour first.
