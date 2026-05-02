from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.config import ROOT_DIR
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.models import Candle


MAJOR_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "TRXUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "AAVEUSDT",
    "SUIUSDT",
]

STABLE_OR_NOISY_BASES = {
    "USDC",
    "FDUSD",
    "USD1",
    "TUSD",
    "USDP",
    "DAI",
    "EUR",
    "EURI",
    "AEUR",
    "LUNC",
    "AMP",
}


@dataclass
class LeverageConfig:
    symbols: list[str]
    interval: str = "1m"
    candle_limit: int = 180
    cycles: int = 1
    poll_seconds: int = 15
    starting_quote: float = 100_000.0
    leverage: float = 15.0
    margin_fraction: float = 0.08
    max_margin_quote: float = 10_000.0
    min_margin_quote: float = 1_000.0
    fee_bps: float = 5.0
    stop_loss_pct: float = 0.0035
    take_profit_pct: float = 0.007
    trailing_stop_pct: float = 0.003
    max_daily_loss_quote: float = 5_000.0
    target_pnl_quote: float = 100_000.0
    maintenance_margin_rate: float = 0.005
    min_price: float = 0.05
    max_spread_pct: float = 0.0015
    cooldown_seconds: int = 180
    stop_after_profit: bool = False
    min_profit_quote: float = 0.02
    min_trail_profit_pct: float = 0.0015
    min_volume_ratio: float = 0.8
    breakout_lookback: int = 12


@dataclass
class LeveragedState:
    cash_quote: float
    realized_pnl: float = 0.0
    position_symbol: str = ""
    side: str = ""
    entry_price: float = 0.0
    quantity: float = 0.0
    margin_quote: float = 0.0
    entry_fee_quote: float = 0.0
    high_watermark: float = 0.0
    low_watermark: float = 0.0
    opened_at: str = ""
    daily_start_date: str = ""
    daily_start_equity: float = 0.0
    kill_switch: bool = False
    loss_streak: int = 0
    cooldown_until_ts: float = 0.0

    @property
    def is_open(self) -> bool:
        return bool(self.position_symbol and self.side and self.quantity > 0)

    @classmethod
    def fresh(cls, starting_quote: float) -> "LeveragedState":
        today = datetime.now(timezone.utc).date().isoformat()
        return cls(cash_quote=starting_quote, daily_start_date=today, daily_start_equity=starting_quote)

    def notional(self, price: float | None = None) -> float:
        mark = price or self.entry_price
        return self.quantity * mark

    def unrealized_pnl(self, price: float) -> float:
        if not self.is_open:
            return 0.0
        if self.side == "LONG":
            return (price - self.entry_price) * self.quantity
        return (self.entry_price - price) * self.quantity

    def estimated_exit_fee(self, price: float, config: LeverageConfig) -> float:
        return self.notional(price) * (config.fee_bps / 10_000)

    def equity(self, price: float, config: LeverageConfig) -> float:
        if not self.is_open:
            return self.cash_quote
        return self.cash_quote + self.margin_quote + self.unrealized_pnl(price) - self.estimated_exit_fee(price, config)


@dataclass(frozen=True)
class TradeSignal:
    action: str
    side: str
    symbol: str
    price: float
    score: float
    reason: str
    indicators: dict[str, float | None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggressive leveraged paper trading session.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Overrides --top-usdt.")
    parser.add_argument("--majors-only", action="store_true", help="Trade only larger, more liquid symbols.")
    parser.add_argument("--top-usdt", type=int, default=80, help="Scan the top N USDT symbols by 24h quote volume.")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--candle-limit", type=int, default=180)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--starting-quote", type=float, default=100_000.0)
    parser.add_argument("--leverage", type=float, default=15.0)
    parser.add_argument("--margin-fraction", type=float, default=0.08)
    parser.add_argument("--max-margin", type=float, default=10_000.0)
    parser.add_argument("--min-margin", type=float, default=1_000.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.0035)
    parser.add_argument("--take-profit-pct", type=float, default=0.007)
    parser.add_argument("--trailing-stop-pct", type=float, default=0.003)
    parser.add_argument("--max-daily-loss", type=float, default=5_000.0)
    parser.add_argument("--target-pnl", type=float, default=100_000.0)
    parser.add_argument("--min-price", type=float, default=0.05)
    parser.add_argument("--max-spread-pct", type=float, default=0.0015)
    parser.add_argument("--cooldown-seconds", type=int, default=180)
    parser.add_argument("--stop-after-profit", action="store_true")
    parser.add_argument("--min-profit", type=float, default=0.02)
    parser.add_argument("--min-trail-profit-pct", type=float, default=0.0015)
    parser.add_argument("--min-volume-ratio", type=float, default=0.8)
    parser.add_argument("--breakout-lookback", type=int, default=12)
    parser.add_argument("--micro50", action="store_true", help="Use a 50 USDT account profile with conservative leverage.")
    parser.add_argument("--challenge50", action="store_true", help="Use a high-risk 50 USDT paper profile targeting 20 USDT profit.")
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def state_path() -> Path:
    return ROOT_DIR / "data" / "leveraged_paper_state.json"


def trade_log_path() -> Path:
    return ROOT_DIR / "logs" / "leveraged_trades.jsonl"


def decision_log_path() -> Path:
    return ROOT_DIR / "logs" / "leveraged_decisions.jsonl"


def load_leveraged_state(path: Path, starting_quote: float) -> LeveragedState:
    if not path.exists():
        return LeveragedState.fresh(starting_quote)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    defaults = asdict(LeveragedState.fresh(starting_quote))
    defaults.update({key: value for key, value in payload.items() if key in defaults})
    return LeveragedState(**defaults)


def save_leveraged_state(path: Path, state: LeveragedState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(state), handle, indent=2, sort_keys=True)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def quality_symbols(symbols: list[str], majors_only: bool) -> list[str]:
    if majors_only:
        selected = [symbol for symbol in MAJOR_SYMBOLS if symbol in symbols or not symbols]
        return selected or MAJOR_SYMBOLS
    return [symbol for symbol in symbols if base_asset(symbol) not in STABLE_OR_NOISY_BASES]


def select_top_usdt_futures_symbols(client: BinanceFuturesClient, limit: int) -> list[str]:
    exchange_info = client.get_exchange_info()
    tradable = {
        item["symbol"]
        for item in exchange_info["symbols"]
        if item.get("quoteAsset") == "USDT"
        and item.get("contractType") == "PERPETUAL"
        and item.get("status") == "TRADING"
    }
    ranked = []
    for ticker in client.get_24hr_tickers():
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


def book_spread_map(client: BinanceFuturesClient) -> dict[str, float]:
    payload = client.get_book_tickers()
    spreads: dict[str, float] = {}
    for item in payload:
        try:
            bid = float(item["bidPrice"])
            ask = float(item["askPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        spreads[item["symbol"]] = (ask - bid) / mid
    return spreads


def closed_candles(rows: list[list[Any]]) -> list[Candle]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = [Candle.from_binance(row) for row in rows]
    return [candle for candle in candles if candle.close_time <= now_ms]


def fetch_candles(client: BinanceFuturesClient, symbol: str, config: LeverageConfig) -> list[Candle]:
    rows = client.get_klines(symbol, config.interval, config.candle_limit)
    candles = closed_candles(rows)
    if len(candles) < 80:
        raise RuntimeError(f"Not enough closed candles for {symbol}.")
    return candles


def entry_signal(symbol: str, candles: list[Candle], config: LeverageConfig | None = None) -> TradeSignal:
    min_volume_ratio = config.min_volume_ratio if config else 0.8
    breakout_lookback = config.breakout_lookback if config else 12
    close_values = closes(candles)
    fast = ema(close_values, 9)
    slow = ema(close_values, 21)
    trend = ema(close_values, 55)
    rsi_values = rsi(close_values, 14)
    atr_values = atr(candles, 14)

    price = close_values[-1]
    prev_price = close_values[-6]
    momentum_5 = (price - prev_price) / prev_price if prev_price else 0.0
    fast_gap = (fast[-1] - slow[-1]) / slow[-1] if slow[-1] else 0.0
    trend_gap = (price - trend[-1]) / trend[-1] if trend[-1] else 0.0
    latest_rsi = rsi_values[-1]
    latest_atr = atr_values[-1]
    atr_pct = (latest_atr / price) if latest_atr else 0.0
    volume_now = candles[-1].volume
    avg_volume = sum(candle.volume for candle in candles[-21:-1]) / 20
    volume_ratio = volume_now / avg_volume if avg_volume else 1.0
    recent_closes = close_values[-(breakout_lookback + 1) : -1]
    breakout_up = bool(recent_closes) and price >= max(recent_closes)
    breakout_down = bool(recent_closes) and price <= min(recent_closes)

    indicators = {
        "fast_ema": fast[-1],
        "slow_ema": slow[-1],
        "trend_ema": trend[-1],
        "rsi": latest_rsi,
        "atr_pct": atr_pct,
        "momentum_5": momentum_5,
        "fast_gap": fast_gap,
        "trend_gap": trend_gap,
        "volume_ratio": volume_ratio,
        "breakout_up": 1.0 if breakout_up else 0.0,
        "breakout_down": 1.0 if breakout_down else 0.0,
    }

    volatility_ok = 0.0005 <= atr_pct <= 0.035
    volume_ok = volume_ratio >= min_volume_ratio
    long_ok = (
        fast[-1] > slow[-1] > trend[-1]
        and momentum_5 > 0.0008
        and trend_gap > 0
        and breakout_up
        and latest_rsi is not None
        and 50 <= latest_rsi <= 74
        and volatility_ok
        and volume_ok
    )
    short_ok = (
        fast[-1] < slow[-1] < trend[-1]
        and momentum_5 < -0.0008
        and trend_gap < 0
        and breakout_down
        and latest_rsi is not None
        and 26 <= latest_rsi <= 50
        and volatility_ok
        and volume_ok
    )

    base_score = (abs(fast_gap) * 3.0) + (abs(momentum_5) * 2.0) + atr_pct + min(volume_ratio, 3.0) / 1000
    if long_ok:
        return TradeSignal("OPEN", "LONG", symbol, price, base_score, "Long momentum setup.", indicators)
    if short_ok:
        return TradeSignal("OPEN", "SHORT", symbol, price, base_score, "Short momentum setup.", indicators)
    return TradeSignal("HOLD", "", symbol, price, 0.0, "No leveraged setup.", indicators)


def exit_signal(symbol: str, candles: list[Candle], state: LeveragedState, config: LeverageConfig) -> TradeSignal:
    price = candles[-1].close
    mark_watermarks(state, price)
    latest = entry_signal(symbol, candles, config)
    if config.stop_after_profit and state.equity(price, config) >= config.starting_quote + config.min_profit_quote:
        return TradeSignal("CLOSE", state.side, symbol, price, 100, "Net paper profit target reached.", latest.indicators)

    liquidation = liquidation_price(state, config)
    if state.side == "LONG":
        stop = state.entry_price * (1 - config.stop_loss_pct)
        take_profit = state.entry_price * (1 + config.take_profit_pct)
        trailing = state.high_watermark * (1 - config.trailing_stop_pct)
        trail_active = state.high_watermark >= state.entry_price * (1 + config.min_trail_profit_pct)
        if price <= liquidation:
            return TradeSignal("CLOSE", state.side, symbol, price, -999, "Forced liquidation estimate reached.", latest.indicators)
        if price <= stop:
            return TradeSignal("CLOSE", state.side, symbol, price, -10, "Stop loss reached.", latest.indicators)
        if price >= take_profit:
            return TradeSignal("CLOSE", state.side, symbol, price, 10, "Take profit reached.", latest.indicators)
        if trail_active and price <= trailing:
            return TradeSignal("CLOSE", state.side, symbol, price, -1, "Trailing stop reached.", latest.indicators)
        if latest.side == "SHORT":
            return TradeSignal("CLOSE", state.side, symbol, price, -1, "Trend flipped short.", latest.indicators)
    else:
        stop = state.entry_price * (1 + config.stop_loss_pct)
        take_profit = state.entry_price * (1 - config.take_profit_pct)
        trailing = state.low_watermark * (1 + config.trailing_stop_pct)
        trail_active = state.low_watermark <= state.entry_price * (1 - config.min_trail_profit_pct)
        if price >= liquidation:
            return TradeSignal("CLOSE", state.side, symbol, price, -999, "Forced liquidation estimate reached.", latest.indicators)
        if price >= stop:
            return TradeSignal("CLOSE", state.side, symbol, price, -10, "Stop loss reached.", latest.indicators)
        if price <= take_profit:
            return TradeSignal("CLOSE", state.side, symbol, price, 10, "Take profit reached.", latest.indicators)
        if trail_active and price >= trailing:
            return TradeSignal("CLOSE", state.side, symbol, price, -1, "Trailing stop reached.", latest.indicators)
        if latest.side == "LONG":
            return TradeSignal("CLOSE", state.side, symbol, price, -1, "Trend flipped long.", latest.indicators)

    return TradeSignal("HOLD", state.side, symbol, price, 0, "Position open; exit rules not hit.", latest.indicators)


def mark_watermarks(state: LeveragedState, price: float) -> None:
    if not state.is_open:
        state.high_watermark = price
        state.low_watermark = price
        return
    state.high_watermark = max(state.high_watermark, price)
    state.low_watermark = min(state.low_watermark, price)


def liquidation_price(state: LeveragedState, config: LeverageConfig) -> float:
    if not state.is_open:
        return 0.0
    buffer = (1 / config.leverage) - config.maintenance_margin_rate
    if state.side == "LONG":
        return max(state.entry_price * (1 - buffer), 0.0)
    return state.entry_price * (1 + buffer)


def refresh_daily_limits(state: LeveragedState, price: float, config: LeverageConfig) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    equity = state.equity(price, config)
    if state.daily_start_date != today:
        state.daily_start_date = today
        state.daily_start_equity = equity
        state.kill_switch = False
    if state.daily_start_equity - equity >= config.max_daily_loss_quote:
        state.kill_switch = True
    if equity - config.starting_quote >= config.target_pnl_quote:
        state.kill_switch = True


def open_position(state: LeveragedState, signal: TradeSignal, config: LeverageConfig) -> dict[str, Any]:
    margin = min(state.cash_quote * config.margin_fraction, config.max_margin_quote)
    if margin < config.min_margin_quote:
        raise RuntimeError("Available paper balance is below minimum margin.")
    notional = margin * config.leverage
    quantity = notional / signal.price
    entry_fee = notional * (config.fee_bps / 10_000)
    if state.cash_quote < margin + entry_fee:
        raise RuntimeError("Not enough paper cash for margin plus fee.")

    state.cash_quote -= margin + entry_fee
    state.position_symbol = signal.symbol
    state.side = signal.side
    state.entry_price = signal.price
    state.quantity = quantity
    state.margin_quote = margin
    state.entry_fee_quote = entry_fee
    state.high_watermark = signal.price
    state.low_watermark = signal.price
    state.opened_at = datetime.now(timezone.utc).isoformat()
    state.cooldown_until_ts = 0.0

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "OPEN",
        "symbol": signal.symbol,
        "side": signal.side,
        "price": signal.price,
        "margin_quote": margin,
        "notional_quote": notional,
        "quantity": quantity,
        "entry_fee_quote": entry_fee,
        "realized_pnl": 0.0,
        "reason": signal.reason,
    }


def close_position(state: LeveragedState, signal: TradeSignal, config: LeverageConfig) -> dict[str, Any]:
    exit_notional = state.notional(signal.price)
    exit_fee = exit_notional * (config.fee_bps / 10_000)
    gross_pnl = state.unrealized_pnl(signal.price)
    net_pnl = gross_pnl - state.entry_fee_quote - exit_fee
    symbol = state.position_symbol
    side = state.side
    quantity = state.quantity
    margin = state.margin_quote

    state.cash_quote += margin + gross_pnl - exit_fee
    state.realized_pnl += net_pnl
    state.position_symbol = ""
    state.side = ""
    state.entry_price = 0.0
    state.quantity = 0.0
    state.margin_quote = 0.0
    state.entry_fee_quote = 0.0
    state.high_watermark = 0.0
    state.low_watermark = 0.0
    state.opened_at = ""
    if net_pnl < 0:
        state.loss_streak += 1
        state.cooldown_until_ts = time.time() + config.cooldown_seconds
    else:
        state.loss_streak = 0
        state.cooldown_until_ts = 0.0
    if config.stop_after_profit and state.cash_quote > config.starting_quote:
        state.kill_switch = True

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "CLOSE",
        "symbol": symbol,
        "side": side,
        "price": signal.price,
        "margin_quote": margin,
        "notional_quote": exit_notional,
        "quantity": quantity,
        "exit_fee_quote": exit_fee,
        "gross_pnl": gross_pnl,
        "realized_pnl": net_pnl,
        "reason": signal.reason,
    }


def run_cycle(client: BinanceFuturesClient, config: LeverageConfig, state: LeveragedState, path: Path) -> None:
    if state.is_open:
        candles = fetch_candles(client, state.position_symbol, config)
        signal = exit_signal(state.position_symbol, candles, state, config)
        refresh_daily_limits(state, signal.price, config)
        append_jsonl(decision_log_path(), decision_payload(signal, state, config))
        if signal.action == "CLOSE" or state.kill_switch:
            if state.kill_switch and signal.action != "CLOSE":
                signal = TradeSignal("CLOSE", state.side, state.position_symbol, signal.price, -100, "Kill switch or target reached.", signal.indicators)
            row = close_position(state, signal, config)
            append_jsonl(trade_log_path(), row)
            save_leveraged_state(path, state)
            print_trade(row, state.cash_quote)
            return
        save_leveraged_state(path, state)
        print(
            f"HOLD {state.side} {state.position_symbol} price={signal.price:.8f} "
            f"equity={state.equity(signal.price, config):.2f} unrealized={state.unrealized_pnl(signal.price):.2f} "
            f"liq={liquidation_price(state, config):.8f}"
        )
        return

    if state.kill_switch:
        save_leveraged_state(path, state)
        print(f"NO_TRADE cash={state.cash_quote:.2f} reason=target reached or kill switch active")
        return

    if state.cooldown_until_ts > time.time():
        remaining = int(state.cooldown_until_ts - time.time())
        save_leveraged_state(path, state)
        print(f"NO_TRADE cash={state.cash_quote:.2f} reason=loss cooldown {remaining}s remaining")
        return

    candidates: list[TradeSignal] = []
    spreads = book_spread_map(client)
    for symbol in config.symbols:
        try:
            candles = fetch_candles(client, symbol, config)
            signal = entry_signal(symbol, candles, config)
        except Exception as exc:
            append_jsonl(decision_log_path(), {"ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "error": str(exc)})
            continue
        append_jsonl(decision_log_path(), decision_payload(signal, state, config))
        if signal.action == "OPEN":
            spread_pct = spreads.get(symbol, 0.0)
            if signal.price < config.min_price:
                append_jsonl(decision_log_path(), skip_payload(signal, f"price below min_price {config.min_price}"))
                continue
            if spread_pct > config.max_spread_pct:
                append_jsonl(decision_log_path(), skip_payload(signal, f"spread {spread_pct:.6f} above max {config.max_spread_pct:.6f}"))
                continue
            candidates.append(signal)

    if not candidates:
        save_leveraged_state(path, state)
        print(f"NO_TRADE scanned={len(config.symbols)} cash={state.cash_quote:.2f} reason=no leveraged setup")
        return

    candidates.sort(key=lambda item: item.score, reverse=True)
    signal = candidates[0]
    refresh_daily_limits(state, signal.price, config)
    if state.kill_switch:
        save_leveraged_state(path, state)
        print(f"NO_TRADE scanned={len(config.symbols)} cash={state.cash_quote:.2f} reason=kill switch active")
        return
    row = open_position(state, signal, config)
    append_jsonl(trade_log_path(), row)
    save_leveraged_state(path, state)
    print_trade(row, state.equity(signal.price, config))


def decision_payload(signal: TradeSignal, state: LeveragedState, config: LeverageConfig) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": signal.action,
        "side": signal.side,
        "symbol": signal.symbol,
        "price": signal.price,
        "score": signal.score,
        "reason": signal.reason,
        "cash_quote": state.cash_quote,
        "equity": state.equity(signal.price, config),
        "indicators": signal.indicators,
    }


def skip_payload(signal: TradeSignal, reason: str) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "SKIP",
        "side": signal.side,
        "symbol": signal.symbol,
        "price": signal.price,
        "score": signal.score,
        "reason": reason,
        "indicators": signal.indicators,
    }


def print_trade(row: dict[str, Any], equity: float) -> None:
    if row["event"] == "OPEN":
        print(
            f"OPEN {row['side']} {row['symbol']} price={row['price']:.8f} "
            f"margin={row['margin_quote']:.2f} notional={row['notional_quote']:.2f} equity={equity:.2f}"
        )
    else:
        print(
            f"CLOSE {row['side']} {row['symbol']} price={row['price']:.8f} "
            f"pnl={row['realized_pnl']:.2f} cash={equity:.2f} reason={row['reason']}"
        )


def config_from_args(args: argparse.Namespace, client: BinanceFuturesClient) -> LeverageConfig:
    if args.micro50:
        args.starting_quote = 50.0
        args.leverage = 5.0
        args.margin_fraction = 0.40
        args.max_margin = 20.0
        args.min_margin = 5.0
        args.stop_loss_pct = 0.002
        args.take_profit_pct = 0.0015
        args.trailing_stop_pct = 0.0008
        args.max_daily_loss = 5.0
        args.stop_after_profit = True
        args.min_profit = 0.02
        args.min_trail_profit_pct = 0.0012
        args.min_volume_ratio = max(args.min_volume_ratio, 0.9)
        args.breakout_lookback = max(args.breakout_lookback, 12)
        args.cooldown_seconds = max(args.cooldown_seconds, 180)

    if args.challenge50:
        args.starting_quote = 50.0
        args.leverage = 50.0
        args.margin_fraction = 0.90
        args.max_margin = 45.0
        args.min_margin = 10.0
        args.stop_loss_pct = 0.0025
        args.take_profit_pct = 0.010
        args.trailing_stop_pct = 0.003
        args.max_daily_loss = 25.0
        args.target_pnl = 20.0
        args.stop_after_profit = False
        args.min_profit = 20.0
        args.min_trail_profit_pct = 0.004
        args.min_volume_ratio = max(args.min_volume_ratio, 1.0)
        args.breakout_lookback = max(args.breakout_lookback, 10)
        args.cooldown_seconds = max(args.cooldown_seconds, 300)

    symbols = parse_symbols(args.symbols)
    if not symbols:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
    symbols = quality_symbols(symbols, args.majors_only)
    if not symbols:
        raise RuntimeError("No symbols selected.")
    leverage = max(1.0, min(args.leverage, 50.0))
    return LeverageConfig(
        symbols=symbols,
        interval=args.interval,
        candle_limit=args.candle_limit,
        cycles=args.cycles,
        poll_seconds=args.poll_seconds,
        starting_quote=args.starting_quote,
        leverage=leverage,
        margin_fraction=max(0.001, min(args.margin_fraction, 0.95)),
        max_margin_quote=args.max_margin,
        min_margin_quote=args.min_margin,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        max_daily_loss_quote=args.max_daily_loss,
        target_pnl_quote=args.target_pnl,
        min_price=args.min_price,
        max_spread_pct=args.max_spread_pct,
        cooldown_seconds=max(args.cooldown_seconds, 0),
        stop_after_profit=args.stop_after_profit,
        min_profit_quote=max(args.min_profit, 0.0),
        min_trail_profit_pct=max(args.min_trail_profit_pct, 0.0),
        min_volume_ratio=max(args.min_volume_ratio, 0.0),
        breakout_lookback=max(args.breakout_lookback, 3),
    )


def main() -> int:
    args = parse_args()
    client = BinanceFuturesClient()
    config = config_from_args(args, client)
    path = state_path()
    if args.reset and path.exists():
        path.unlink()
    state = load_leveraged_state(path, config.starting_quote)

    shown = ",".join(config.symbols[:10])
    suffix = "..." if len(config.symbols) > 10 else ""
    print(
        f"LEVERAGED PAPER symbols={len(config.symbols)} [{shown}{suffix}] "
        f"start={config.starting_quote:.2f} leverage={config.leverage:.1f}x"
    )
    for cycle in range(max(config.cycles, 1)):
        print(f"CYCLE {cycle + 1}/{max(config.cycles, 1)} {datetime.now(timezone.utc).isoformat()}")
        run_cycle(client, config, state, path)
        if cycle < max(config.cycles, 1) - 1:
            time.sleep(max(config.poll_seconds, 1))
    if state.is_open:
        last_price = state.entry_price if not math.isfinite(state.entry_price) else state.entry_price
        print(f"STATE open={state.side} {state.position_symbol} entry={state.entry_price:.8f} cash={state.cash_quote:.2f}")
    else:
        print(f"STATE flat cash={state.cash_quote:.2f} realized_pnl={state.realized_pnl:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
