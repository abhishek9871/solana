from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from trading_bot.binance_client import BinanceSpotClient
from trading_bot.config import BotConfig, load_config
from trading_bot.models import BotState, Candle
from trading_bot.risk import RiskManager
from trading_bot.strategy import decide


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the bot on historical Binance candles.")
    parser.add_argument("--symbol", help="Override SYMBOL from .env, for example BTCUSDT.")
    parser.add_argument("--interval", help="Override INTERVAL from .env, for example 5m.")
    parser.add_argument("--limit", type=int, default=1000, help="Candles to fetch, max 1000 for one Binance request.")
    parser.add_argument("--today", action="store_true", help="Backtest from local midnight to now.")
    parser.add_argument("--days", type=int, help="Backtest the last N days.")
    return parser.parse_args()


def apply_overrides(config: BotConfig, args: argparse.Namespace) -> BotConfig:
    updates = {"mode": "paper"}
    if args.symbol:
        updates["symbol"] = args.symbol.upper()
    if args.interval:
        updates["interval"] = args.interval
    return replace(config, **updates)


def local_midnight_timestamp_ms() -> int:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def days_ago_timestamp_ms(days: int) -> int:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return int(start.timestamp() * 1000)


def fetch_candles(config: BotConfig, args: argparse.Namespace) -> list[Candle]:
    limit = min(max(args.limit, 1), 1000)
    start_time = None
    if args.today:
        start_time = local_midnight_timestamp_ms()
    elif args.days:
        start_time = days_ago_timestamp_ms(args.days)

    client = BinanceSpotClient(config.mainnet_base_url)
    rows = client.get_klines(config.symbol, config.interval, limit, start_time=start_time)
    candles = [Candle.from_binance(row) for row in rows]

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = [candle for candle in candles if candle.close_time <= now_ms]
    return closed


def run_backtest(candles: list[Candle], config: BotConfig) -> dict:
    if not candles:
        raise RuntimeError("No closed candles available for backtest.")

    state = BotState.fresh(config.starting_quote, candles[0].close)
    first_date = candle_date(candles[0])
    state.daily_start_date = first_date
    state.daily_start_equity = config.starting_quote

    min_required = max(config.slow_ema, config.rsi_period, config.atr_period) + 3
    risk = RiskManager(config)
    trades: list[dict] = []
    max_equity = config.starting_quote
    max_drawdown = 0.0

    for index in range(min_required, len(candles)):
        visible = candles[: index + 1]
        candle = visible[-1]
        price = candle.close
        mark_price(state, price)
        refresh_daily_limits_at(state, price, candle, config)

        signal = decide(visible, state, config)
        decision = risk.evaluate(signal, state)
        if decision.allowed and decision.action == "BUY":
            trades.append(backtest_buy(state, decision.quote_amount, price, config, candle, signal.reason))
        elif decision.allowed and decision.action == "SELL":
            trades.append(backtest_sell(state, price, config, candle, signal.reason))

        equity = state.equity(price)
        max_equity = max(max_equity, equity)
        drawdown = max_equity - equity
        max_drawdown = max(max_drawdown, drawdown)

    final_price = candles[-1].close
    ending_equity = state.equity(final_price)
    sells = [trade for trade in trades if trade["side"] == "SELL"]
    wins = [trade for trade in sells if trade["realized_pnl"] > 0]

    return {
        "symbol": config.symbol,
        "interval": config.interval,
        "candles": len(candles),
        "from": ms_to_iso(candles[0].open_time),
        "to": ms_to_iso(candles[-1].close_time),
        "starting_quote": config.starting_quote,
        "ending_equity": ending_equity,
        "pnl": ending_equity - config.starting_quote,
        "pnl_pct": ((ending_equity / config.starting_quote) - 1) * 100,
        "trades": len(trades),
        "round_trips": len(sells),
        "wins": len(wins),
        "win_rate_pct": (len(wins) / len(sells) * 100) if sells else 0.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": (max_drawdown / max_equity * 100) if max_equity else 0.0,
        "open_position": state.is_open,
        "final_price": final_price,
        "realized_pnl": state.realized_pnl,
        "kill_switch": state.kill_switch,
        "last_trades": trades[-5:],
    }


def mark_price(state: BotState, price: float) -> None:
    if state.is_open:
        state.high_watermark = max(state.high_watermark, price)
    else:
        state.high_watermark = price


def refresh_daily_limits_at(state: BotState, price: float, candle: Candle, config: BotConfig) -> None:
    day = candle_date(candle)
    equity = state.equity(price)
    if state.daily_start_date != day:
        state.daily_start_date = day
        state.daily_start_equity = equity
        state.kill_switch = False

    if state.daily_start_equity - equity >= config.max_daily_loss_quote:
        state.kill_switch = True


def backtest_buy(state: BotState, quote_amount: float, price: float, config: BotConfig, candle: Candle, reason: str) -> dict:
    fee_quote = quote_amount * (config.fee_bps / 10_000)
    net_quote = quote_amount - fee_quote
    base_qty = net_quote / price

    state.quote_qty -= quote_amount
    state.base_qty = base_qty
    state.entry_price = price
    state.entry_quote_spent = quote_amount
    state.high_watermark = price
    return trade_row(candle, "BUY", price, base_qty, quote_amount, fee_quote, 0.0, reason)


def backtest_sell(state: BotState, price: float, config: BotConfig, candle: Candle, reason: str) -> dict:
    gross_quote = state.base_qty * price
    fee_quote = gross_quote * (config.fee_bps / 10_000)
    net_quote = gross_quote - fee_quote
    realized_pnl = net_quote - state.entry_quote_spent
    base_qty = state.base_qty

    state.quote_qty += net_quote
    state.base_qty = 0.0
    state.entry_price = 0.0
    state.entry_quote_spent = 0.0
    state.high_watermark = price
    state.realized_pnl += realized_pnl
    return trade_row(candle, "SELL", price, base_qty, gross_quote, fee_quote, realized_pnl, reason)


def trade_row(
    candle: Candle,
    side: str,
    price: float,
    base_qty: float,
    quote_qty: float,
    fee_quote: float,
    realized_pnl: float,
    reason: str,
) -> dict:
    return {
        "ts": ms_to_iso(candle.close_time),
        "side": side,
        "price": round(price, 8),
        "base_qty": round(base_qty, 10),
        "quote_qty": round(quote_qty, 8),
        "fee_quote": round(fee_quote, 8),
        "realized_pnl": round(realized_pnl, 8),
        "reason": reason,
    }


def candle_date(candle: Candle) -> str:
    return datetime.fromtimestamp(candle.close_time / 1000, tz=timezone.utc).date().isoformat()


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def print_report(result: dict) -> None:
    print(f"Backtest {result['symbol']} {result['interval']}")
    print(f"Range: {result['from']} to {result['to']} ({result['candles']} closed candles)")
    print(f"Start equity: {result['starting_quote']:.2f}")
    print(f"End equity:   {result['ending_equity']:.2f}")
    print(f"PnL:          {result['pnl']:.2f} ({result['pnl_pct']:.2f}%)")
    print(f"Trades:       {result['trades']} ({result['round_trips']} closed round trips)")
    print(f"Win rate:     {result['win_rate_pct']:.2f}% ({result['wins']} wins)")
    print(f"Max drawdown: {result['max_drawdown']:.2f} ({result['max_drawdown_pct']:.2f}%)")
    print(f"Open pos:     {result['open_position']}")
    print(f"Kill switch:  {result['kill_switch']}")
    if result["last_trades"]:
        print("Last trades:")
        for trade in result["last_trades"]:
            print(
                f"  {trade['ts']} {trade['side']} price={trade['price']} "
                f"quote={trade['quote_qty']} pnl={trade['realized_pnl']} reason={trade['reason']}"
            )


def main() -> int:
    args = parse_args()
    config = apply_overrides(load_config(), args)
    candles = fetch_candles(config, args)
    result = run_backtest(candles, config)
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
