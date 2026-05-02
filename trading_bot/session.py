from __future__ import annotations

import argparse
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.binance_client import BinanceSpotClient
from trading_bot.config import BotConfig, load_config
from trading_bot.models import BotState, Candle, Signal
from trading_bot.momentum_strategy import decide as momentum_decide
from trading_bot.momentum_strategy import score as score_signal
from trading_bot.risk import RiskManager
from trading_bot.storage import append_decision, append_trade, load_state, save_state


DEFAULT_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "TRXUSDT",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live-data multi-symbol paper trading session.")
    parser.add_argument("--symbols", help="Comma-separated symbols to scan.")
    parser.add_argument("--top-usdt", type=int, help="Scan the top N USDT spot pairs by 24h quote volume.")
    parser.add_argument("--interval", default="1m", help="Candle interval for the scanner.")
    parser.add_argument("--candle-limit", type=int, default=150)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--reset", action="store_true", help="Reset the session paper wallet before running.")
    return parser.parse_args()


def session_state_path(config: BotConfig) -> Path:
    return config.data_dir / "multi_paper_state.json"


def closed_candles(rows: list[list[Any]]) -> list[Candle]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = [Candle.from_binance(row) for row in rows]
    return [candle for candle in candles if candle.close_time <= now_ms]


def fetch_symbol_candles(client: BinanceSpotClient, symbol: str, config: BotConfig) -> list[Candle]:
    rows = client.get_klines(symbol, config.interval, config.candle_limit)
    candles = closed_candles(rows)
    if not candles:
        raise RuntimeError(f"No closed candles for {symbol}.")
    return candles


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return DEFAULT_SYMBOLS
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def select_top_usdt_symbols(client: BinanceSpotClient, limit: int) -> list[str]:
    exchange_info = client.public_get("/api/v3/exchangeInfo")
    tradable = {
        item["symbol"]
        for item in exchange_info["symbols"]
        if item.get("quoteAsset") == "USDT"
        and item.get("status") == "TRADING"
        and item.get("isSpotTradingAllowed")
        and not item["symbol"].endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
    }
    tickers = client.public_get("/api/v3/ticker/24hr")
    ranked = []
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if symbol not in tradable:
            continue
        try:
            quote_volume = float(ticker.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        ranked.append((quote_volume, symbol))
    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[: max(limit, 1)]]


def run_cycle(config: BotConfig, symbols: list[str], state: BotState, state_path: Path) -> None:
    client = BinanceSpotClient(config.mainnet_base_url)
    risk = RiskManager(config)

    if state.is_open:
        symbol = state.position_symbol or symbols[0]
        candles = fetch_symbol_candles(client, symbol, config)
        price = candles[-1].close
        mark_price(state, price)
        risk.refresh_daily_limits(state, price)
        signal = momentum_decide(candles, state, replace(config, symbol=symbol))
        decision = risk.evaluate(signal, state)
        append_decision(config.decisions_path, "paper-session", symbol, signal, state.equity(price), decision.reason)

        if decision.allowed and decision.action == "SELL":
            row = sell_all(config, state, state_path, symbol, price, signal.reason)
            print_trade("EXIT", row, state.equity(price))
            return

        save_state(state_path, state)
        print(
            f"HOLD {symbol} price={price:.8f} equity={state.equity(price):.2f} "
            f"unrealized={state.equity(price) - state.daily_start_equity:.2f} reason={decision.reason}"
        )
        return

    candidates: list[tuple[float, str, Signal]] = []
    scan_errors: list[str] = []
    reference_price = 0.0
    for symbol in symbols:
        try:
            candles = fetch_symbol_candles(client, symbol, config)
        except Exception as exc:
            scan_errors.append(f"{symbol}: {exc}")
            continue
        reference_price = reference_price or candles[-1].close
        signal = momentum_decide(candles, state, replace(config, symbol=symbol))
        decision = risk.evaluate(signal, state)
        append_decision(config.decisions_path, "paper-session", symbol, signal, state.equity(signal.price), decision.reason)
        if decision.allowed and signal.action == "BUY":
            candidates.append((score_signal(signal), symbol, signal))

    if not candidates:
        save_state(state_path, state)
        suffix = f" scan_errors={len(scan_errors)}" if scan_errors else ""
        print(f"NO_TRADE scanned={len(symbols)} equity={state.equity(reference_price):.2f} reason=no allowed setup{suffix}")
        return

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, symbol, signal = candidates[0]
    decision = risk.evaluate(signal, state)
    row = buy(config, state, state_path, symbol, decision.quote_amount, signal.price, signal.reason)
    print_trade("ENTRY", row, state.equity(signal.price))


def buy(
    config: BotConfig,
    state: BotState,
    state_path: Path,
    symbol: str,
    quote_amount: float,
    price: float,
    reason: str,
) -> dict[str, Any]:
    fee_quote = quote_amount * (config.fee_bps / 10_000)
    net_quote = quote_amount - fee_quote
    base_qty = net_quote / price

    state.quote_qty -= quote_amount
    state.base_qty = base_qty
    state.position_symbol = symbol
    state.entry_price = price
    state.entry_quote_spent = quote_amount
    state.high_watermark = price
    state.last_order_id = f"session-{int(datetime.now(timezone.utc).timestamp())}"
    save_state(state_path, state)

    row = trade_row(config, symbol, "BUY", price, base_qty, quote_amount, fee_quote, 0.0, reason, state.last_order_id)
    append_trade(config.trades_path, row)
    return row


def sell_all(config: BotConfig, state: BotState, state_path: Path, symbol: str, price: float, reason: str) -> dict[str, Any]:
    gross_quote = state.base_qty * price
    fee_quote = gross_quote * (config.fee_bps / 10_000)
    net_quote = gross_quote - fee_quote
    realized_pnl = net_quote - state.entry_quote_spent
    base_qty = state.base_qty

    state.quote_qty += net_quote
    state.base_qty = 0.0
    state.position_symbol = ""
    state.entry_price = 0.0
    state.entry_quote_spent = 0.0
    state.high_watermark = price
    state.realized_pnl += realized_pnl
    state.last_order_id = f"session-{int(datetime.now(timezone.utc).timestamp())}"
    save_state(state_path, state)

    row = trade_row(config, symbol, "SELL", price, base_qty, gross_quote, fee_quote, realized_pnl, reason, state.last_order_id)
    append_trade(config.trades_path, row)
    return row


def mark_price(state: BotState, price: float) -> None:
    if state.is_open:
        state.high_watermark = max(state.high_watermark, price)
    else:
        state.high_watermark = price


def trade_row(
    config: BotConfig,
    symbol: str,
    side: str,
    price: float,
    base_qty: float,
    quote_qty: float,
    fee_quote: float,
    realized_pnl: float,
    reason: str,
    order_id: str,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "paper-session",
        "symbol": symbol,
        "side": side,
        "price": round(price, 8),
        "base_qty": round(base_qty, 10),
        "quote_qty": round(quote_qty, 8),
        "fee_quote": round(fee_quote, 8),
        "realized_pnl": round(realized_pnl, 8),
        "reason": reason,
        "order_id": order_id,
    }


def print_trade(label: str, row: dict[str, Any], equity: float) -> None:
    print(
        f"{label} {row['side']} {row['symbol']} price={row['price']} "
        f"quote={row['quote_qty']} equity={equity:.2f} reason={row['reason']}"
    )


def main() -> int:
    args = parse_args()
    config = replace(
        load_config(),
        mode="paper",
        interval=args.interval,
        candle_limit=args.candle_limit,
        poll_seconds=args.poll_seconds,
    )
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    if args.top_usdt:
        selector_client = BinanceSpotClient(config.mainnet_base_url)
        symbols = select_top_usdt_symbols(selector_client, args.top_usdt)
        print(f"SELECTED top_usdt={len(symbols)} symbols={','.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")
    else:
        symbols = parse_symbols(args.symbols)
    state_path = session_state_path(config)
    if args.reset and state_path.exists():
        state_path.unlink()

    state = load_state(state_path, config.starting_quote, 0.0)
    for cycle in range(max(args.cycles, 1)):
        print(f"CYCLE {cycle + 1}/{max(args.cycles, 1)} {datetime.now(timezone.utc).isoformat()}")
        run_cycle(config, symbols, state, state_path)
        if cycle < max(args.cycles, 1) - 1:
            time.sleep(max(args.poll_seconds, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
