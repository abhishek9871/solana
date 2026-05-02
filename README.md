# Binance Safety-First Trading Bot

This project is a beginner-safe trading bot scaffold. It does not guarantee profit and it does not include live mainnet trading. It can:

- simulate trades in `paper` mode using public Binance market data
- place virtual orders in Binance Spot `testnet` mode
- enforce max trade size, stop loss, take profit, trailing stop, and daily loss limits
- persist state and write decision/trade logs

Do not paste API keys into chat. Put testnet keys only in your local `.env` file. This repo ignores `.env`, `data/`, and `logs/` by default.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Run one paper-trading cycle:

```powershell
python -m trading_bot.main --mode paper --once
```

Run continuously:

```powershell
python -m trading_bot.main --mode paper --loop
```

Run tests:

```powershell
python -m unittest discover
```

## Spot Testnet

Binance Spot Testnet uses virtual funds only. Create testnet keys at:

https://testnet.binance.vision/

Then edit `.env`:

```text
BOT_MODE=testnet
BINANCE_TESTNET_API_KEY=your_testnet_key
BINANCE_TESTNET_SECRET_KEY=your_testnet_secret
```

Run one testnet cycle:

```powershell
python -m trading_bot.main --mode testnet --once
```

The bot is spot-only: no leverage, no shorting, no withdrawals, and no mainnet order endpoint.

## Strategy

The default strategy is a cautious trend-following example:

- buy only when the fast EMA crosses above the slow EMA and RSI is not overbought
- sell on stop loss, take profit, trailing stop, or trend reversal
- hold when signals are unclear

These rules are intentionally simple so you can inspect every decision. They are not financial advice.

## Leveraged Paper Mode

This is still simulation only. It does not place real futures orders.

```powershell
python -m trading_bot.leveraged_session --reset --majors-only --top-usdt 80 --interval 1m --cycles 20 --poll-seconds 15 --starting-quote 100000 --leverage 15 --margin-fraction 0.08 --max-margin 10000 --min-margin 1000
```

Runtime files:

- `data/leveraged_paper_state.json`
- `logs/leveraged_trades.jsonl`
- `logs/leveraged_decisions.jsonl`

Optimize a 50 USDT micro account on recent USD-M futures candles:

```powershell
python -m trading_bot.leverage_optimizer --majors-only --top-usdt 80 --interval 1m --candle-limit 500 --starting-quote 50
```

Run the stricter 50 USDT forward paper profile:

```powershell
python -m trading_bot.leveraged_session --reset --majors-only --top-usdt 80 --interval 1m --cycles 30 --poll-seconds 15 --starting-quote 50 --leverage 12 --margin-fraction 0.45 --max-margin 35 --min-margin 5 --stop-loss-pct 0.002 --take-profit-pct 0.0035 --trailing-stop-pct 0.0012 --max-daily-loss 8 --target-pnl 20 --min-price 0.05 --max-spread-pct 0.0015 --cooldown-seconds 180 --stop-after-profit --min-profit 0.05 --min-trail-profit-pct 0.0012 --min-volume-ratio 0.8 --breakout-lookback 12
```

Scan for a 39.05 USDT to 55 USDT recovery setup using public Binance USD-M futures data:

```powershell
python -m trading_bot.recovery_scanner --balance 39.05 --target-balance 55 --top-usdt 120 --interval 1m --candle-limit 240 --max-loss 8 --max-spread-pct 0.0015 --limit 10
```

## Files Written At Runtime

- `data/paper_state.json`
- `data/testnet_state.json`
- `logs/decisions.jsonl`
- `logs/trades.csv`

Delete the state file if you want to restart the simulation balance.
