from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.leveraged_session import (
    LeverageConfig,
    MAJOR_SYMBOLS,
    TradeSignal,
    entry_signal,
    quality_symbols,
    select_top_usdt_futures_symbols,
)
from trading_bot.models import Candle


def entry_signal_mean_reversion(symbol: str, candles: list[Candle], config: LeverageConfig) -> TradeSignal:
    """Mean-reversion-in-trend: buy oversold dips inside uptrends, fade overbought spikes in downtrends.

    Combines (1) higher-timeframe trend regime (200-period EMA + slope), (2) Bollinger Band extreme,
    (3) RSI extreme, (4) volume confirmation, (5) volatility regime filter.
    """
    min_volume_ratio = config.min_volume_ratio
    closes_list = closes(candles)
    if len(closes_list) < 220:
        return TradeSignal("HOLD", "", symbol, closes_list[-1] if closes_list else 0.0, 0.0, "Insufficient data for mean reversion.", {})

    price = closes_list[-1]
    sma_20 = sum(closes_list[-20:]) / 20
    variance = sum((c - sma_20) ** 2 for c in closes_list[-20:]) / 20
    std_dev = variance ** 0.5
    upper_bb = sma_20 + 2 * std_dev
    lower_bb = sma_20 - 2 * std_dev

    rsi_values = rsi(closes_list, 14)
    latest_rsi = rsi_values[-1]
    atr_values = atr(candles, 14)
    latest_atr = atr_values[-1]
    atr_pct = (latest_atr / price) if latest_atr else 0.0

    ema_200 = ema(closes_list, 200)
    trend_slope = (ema_200[-1] - ema_200[-50]) / ema_200[-50] if ema_200[-50] else 0.0
    price_above_trend = price > ema_200[-1]
    price_below_trend = price < ema_200[-1]

    volume_now = candles[-1].volume
    avg_volume = sum(c.volume for c in candles[-21:-1]) / 20
    volume_ratio = volume_now / avg_volume if avg_volume else 1.0

    bb_width = (upper_bb - lower_bb) / sma_20 if sma_20 else 0.0
    bb_distance_pct = (lower_bb - price) / price if price else 0.0
    bb_distance_pct_up = (price - upper_bb) / price if price else 0.0

    indicators = {
        "sma_20": sma_20,
        "ema_200": ema_200[-1],
        "upper_bb": upper_bb,
        "lower_bb": lower_bb,
        "rsi": latest_rsi,
        "atr_pct": atr_pct,
        "volume_ratio": volume_ratio,
        "trend_slope": trend_slope,
        "bb_width": bb_width,
    }

    volatility_ok = 0.0008 <= atr_pct <= 0.025
    bb_meaningful = bb_width >= 0.003

    trend_up = trend_slope > 0.0008 and price_above_trend
    trend_down = trend_slope < -0.0008 and price_below_trend

    long_ok = (
        trend_up
        and price <= lower_bb
        and latest_rsi is not None and latest_rsi <= 32
        and volume_ratio >= min_volume_ratio
        and volatility_ok
        and bb_meaningful
    )
    short_ok = (
        trend_down
        and price >= upper_bb
        and latest_rsi is not None and latest_rsi >= 68
        and volume_ratio >= min_volume_ratio
        and volatility_ok
        and bb_meaningful
    )

    score = abs(trend_slope) * 100 + min(volume_ratio, 3.0) * 0.3 + bb_width * 5
    if long_ok:
        return TradeSignal("OPEN", "LONG", symbol, price, score, "Mean reversion long: oversold pullback in uptrend.", indicators)
    if short_ok:
        return TradeSignal("OPEN", "SHORT", symbol, price, score, "Mean reversion short: overbought spike in downtrend.", indicators)
    return TradeSignal("HOLD", "", symbol, price, 0.0, "No mean reversion setup.", indicators)


STRATEGIES = {
    "momentum": entry_signal,
    "mean_reversion": entry_signal_mean_reversion,
}


@dataclass
class SimState:
    cash: float
    side: str = ""
    entry_price: float = 0.0
    quantity: float = 0.0
    margin: float = 0.0
    entry_fee: float = 0.0
    high_watermark: float = 0.0
    low_watermark: float = 0.0
    realized_pnl: float = 0.0
    cooldown_until_index: int = -1
    loss_streak: int = 0
    kill_switch: bool = False

    @property
    def is_open(self) -> bool:
        return bool(self.side and self.quantity > 0)

    def notional(self, price: float) -> float:
        return self.quantity * price


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    notional: float
    gross_pnl: float
    net_pnl: float
    reason: str
    bars_held: int


@dataclass
class SymbolResult:
    symbol: str
    candles: int
    trades: list[TradeRecord] = field(default_factory=list)
    final_equity: float = 0.0
    starting_equity: float = 0.0
    max_drawdown: float = 0.0
    blocked_reasons: dict[str, int] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the leveraged momentum strategy on futures history.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Overrides --majors-only and --top-usdt.")
    parser.add_argument("--majors-only", action="store_true")
    parser.add_argument("--top-usdt", type=int, default=20)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--starting-quote", type=float, default=50.0)
    parser.add_argument("--leverage", type=float, default=25.0)
    parser.add_argument("--margin-fraction", type=float, default=0.40)
    parser.add_argument("--max-margin", type=float, default=25.0)
    parser.add_argument("--min-margin", type=float, default=8.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.003)
    parser.add_argument("--take-profit-pct", type=float, default=0.007)
    parser.add_argument("--trailing-stop-pct", type=float, default=0.003)
    parser.add_argument("--min-trail-profit-pct", type=float, default=0.003)
    parser.add_argument("--min-volume-ratio", type=float, default=1.0)
    parser.add_argument("--breakout-lookback", type=int, default=12)
    parser.add_argument("--cooldown-bars", type=int, default=5)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--maintenance-margin-rate", type=float, default=0.005)
    parser.add_argument("--candle-warmup", type=int, default=80)
    parser.add_argument("--strategy", choices=list(STRATEGIES.keys()), default="momentum")
    parser.add_argument("--out", default="logs/leveraged_backtest.json")
    return parser.parse_args()


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def fetch_history(client: BinanceFuturesClient, symbol: str, interval: str, days: int) -> list[Candle]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    rows: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk = client.get_klines(symbol, interval, 1500, start_time=cursor)
        if not chunk:
            break
        rows.extend(chunk)
        last_close = chunk[-1][6]
        if last_close <= cursor:
            break
        cursor = last_close + 1
        if len(chunk) < 1500:
            break
        time.sleep(0.05)
    seen: set[int] = set()
    deduped: list[list[Any]] = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        deduped.append(row)
    candles = [Candle.from_binance(row) for row in deduped]
    candles = [c for c in candles if c.close_time <= end_ms]
    return candles


def make_config(args: argparse.Namespace, symbols: list[str]) -> LeverageConfig:
    return LeverageConfig(
        symbols=symbols,
        interval=args.interval,
        candle_limit=args.candle_warmup + 5,
        starting_quote=args.starting_quote,
        leverage=args.leverage,
        margin_fraction=args.margin_fraction,
        max_margin_quote=args.max_margin,
        min_margin_quote=args.min_margin,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        trailing_stop_pct=args.trailing_stop_pct,
        fee_bps=args.fee_bps,
        maintenance_margin_rate=args.maintenance_margin_rate,
        min_trail_profit_pct=args.min_trail_profit_pct,
        min_volume_ratio=args.min_volume_ratio,
        breakout_lookback=args.breakout_lookback,
    )


def simulate_symbol(symbol: str, candles: list[Candle], config: LeverageConfig, args: argparse.Namespace) -> SymbolResult:
    state = SimState(cash=args.starting_quote)
    result = SymbolResult(symbol=symbol, candles=len(candles), starting_equity=args.starting_quote)
    warmup = max(args.candle_warmup, 220 if args.strategy == "mean_reversion" else args.candle_warmup)
    if len(candles) < warmup + 5:
        result.final_equity = state.cash
        return result

    strategy_fn = STRATEGIES[args.strategy]
    max_equity = args.starting_quote
    fee_rate = args.fee_bps / 10_000.0

    for index in range(warmup, len(candles)):
        window = candles[max(0, index - warmup + 1) : index + 1]
        candle = candles[index]

        if state.is_open:
            close_trade = check_intracandle_exit(state, candle, config, args, index, fee_rate)
            if close_trade is not None:
                result.trades.append(close_trade)
                continue
            update_watermarks(state, candle)
            close_signal = check_close_signal(state, window, config, args, index, fee_rate, strategy_fn)
            if close_signal is not None:
                result.trades.append(close_signal)
                continue
            equity_now = equity_at_close(state, candle.close, fee_rate)
        else:
            if index < state.cooldown_until_index:
                blocked = result.blocked_reasons
                blocked["cooldown"] = blocked.get("cooldown", 0) + 1
                equity_now = state.cash
            else:
                signal = strategy_fn(symbol, window, config)
                if signal.action == "OPEN":
                    if not try_open(state, signal, candle, config, args, fee_rate):
                        result.blocked_reasons["margin_too_small"] = result.blocked_reasons.get("margin_too_small", 0) + 1
                else:
                    result.blocked_reasons["no_setup"] = result.blocked_reasons.get("no_setup", 0) + 1
                equity_now = state.cash if not state.is_open else equity_at_close(state, candle.close, fee_rate)

        max_equity = max(max_equity, equity_now)
        drawdown = max_equity - equity_now
        result.max_drawdown = max(result.max_drawdown, drawdown)

    if state.is_open:
        last = candles[-1]
        forced = force_close(state, last, "End of backtest", len(candles) - 1, fee_rate)
        result.trades.append(forced)

    result.final_equity = state.cash + state.realized_pnl - state.realized_pnl + state.cash * 0
    result.final_equity = state.cash
    return result


def equity_at_close(state: SimState, price: float, fee_rate: float) -> float:
    if not state.is_open:
        return state.cash
    if state.side == "LONG":
        unrealized = (price - state.entry_price) * state.quantity
    else:
        unrealized = (state.entry_price - price) * state.quantity
    exit_fee = state.notional(price) * fee_rate
    return state.cash + state.margin + unrealized - exit_fee


def update_watermarks(state: SimState, candle: Candle) -> None:
    state.high_watermark = max(state.high_watermark, candle.high)
    state.low_watermark = min(state.low_watermark, candle.low) if state.low_watermark > 0 else candle.low


def try_open(state: SimState, signal, candle: Candle, config: LeverageConfig, args: argparse.Namespace, fee_rate: float) -> bool:
    margin = min(state.cash * config.margin_fraction, config.max_margin_quote)
    if margin < config.min_margin_quote:
        return False
    notional = margin * config.leverage
    quantity = notional / signal.price
    entry_fee = notional * fee_rate
    if state.cash < margin + entry_fee:
        return False
    state.cash -= margin + entry_fee
    state.side = signal.side
    state.entry_price = signal.price
    state.quantity = quantity
    state.margin = margin
    state.entry_fee = entry_fee
    state.high_watermark = signal.price
    state.low_watermark = signal.price
    return True


def check_intracandle_exit(state: SimState, candle: Candle, config: LeverageConfig, args: argparse.Namespace, index: int, fee_rate: float) -> TradeRecord | None:
    if state.side == "LONG":
        stop_price = state.entry_price * (1 - config.stop_loss_pct)
        tp_price = state.entry_price * (1 + config.take_profit_pct)
        liq_price = state.entry_price * (1 - (1 / config.leverage - config.maintenance_margin_rate))
        if candle.low <= liq_price:
            return close_at(state, liq_price, "Liquidation", index, fee_rate)
        if candle.low <= stop_price:
            return close_at(state, stop_price, "Stop loss", index, fee_rate)
        if candle.high >= tp_price:
            return close_at(state, tp_price, "Take profit", index, fee_rate)
    else:
        stop_price = state.entry_price * (1 + config.stop_loss_pct)
        tp_price = state.entry_price * (1 - config.take_profit_pct)
        liq_price = state.entry_price * (1 + (1 / config.leverage - config.maintenance_margin_rate))
        if candle.high >= liq_price:
            return close_at(state, liq_price, "Liquidation", index, fee_rate)
        if candle.high >= stop_price:
            return close_at(state, stop_price, "Stop loss", index, fee_rate)
        if candle.low <= tp_price:
            return close_at(state, tp_price, "Take profit", index, fee_rate)
    return None


def check_close_signal(state: SimState, window: list[Candle], config: LeverageConfig, args: argparse.Namespace, index: int, fee_rate: float, strategy_fn) -> TradeRecord | None:
    candle = window[-1]
    if state.side == "LONG":
        trail_active = state.high_watermark >= state.entry_price * (1 + config.min_trail_profit_pct)
        trailing = state.high_watermark * (1 - config.trailing_stop_pct)
        if trail_active and candle.close <= trailing:
            return close_at(state, candle.close, "Trailing stop", index, fee_rate)
    else:
        trail_active = state.low_watermark <= state.entry_price * (1 - config.min_trail_profit_pct)
        trailing = state.low_watermark * (1 + config.trailing_stop_pct)
        if trail_active and candle.close >= trailing:
            return close_at(state, candle.close, "Trailing stop", index, fee_rate)

    fresh = strategy_fn("_", window, config)
    if state.side == "LONG" and fresh.side == "SHORT":
        return close_at(state, candle.close, "Trend flipped short", index, fee_rate)
    if state.side == "SHORT" and fresh.side == "LONG":
        return close_at(state, candle.close, "Trend flipped long", index, fee_rate)
    return None


def close_at(state: SimState, exit_price: float, reason: str, exit_index: int, fee_rate: float) -> TradeRecord:
    if state.side == "LONG":
        gross_pnl = (exit_price - state.entry_price) * state.quantity
    else:
        gross_pnl = (state.entry_price - exit_price) * state.quantity
    exit_notional = state.quantity * exit_price
    exit_fee = exit_notional * fee_rate
    net_pnl = gross_pnl - state.entry_fee - exit_fee
    record = TradeRecord(
        symbol="",
        side=state.side,
        entry_index=-1,
        exit_index=exit_index,
        entry_price=state.entry_price,
        exit_price=exit_price,
        quantity=state.quantity,
        margin=state.margin,
        notional=state.quantity * state.entry_price,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        reason=reason,
        bars_held=0,
    )
    state.cash += state.margin + gross_pnl - exit_fee
    state.realized_pnl += net_pnl
    if net_pnl < 0:
        state.loss_streak += 1
        return finalize_close(state, record, exit_index, cooldown_bars=5)
    state.loss_streak = 0
    return finalize_close(state, record, exit_index, cooldown_bars=0)


def finalize_close(state: SimState, record: TradeRecord, exit_index: int, cooldown_bars: int) -> TradeRecord:
    state.side = ""
    state.entry_price = 0.0
    state.quantity = 0.0
    state.margin = 0.0
    state.entry_fee = 0.0
    state.high_watermark = 0.0
    state.low_watermark = 0.0
    state.cooldown_until_index = exit_index + cooldown_bars
    return record


def force_close(state: SimState, candle: Candle, reason: str, exit_index: int, fee_rate: float) -> TradeRecord:
    return close_at(state, candle.close, reason, exit_index, fee_rate)


def aggregate_results(results: list[SymbolResult], starting_per_symbol: float) -> dict[str, Any]:
    all_trades: list[TradeRecord] = []
    per_symbol = []
    total_starting = 0.0
    total_final = 0.0
    total_drawdown = 0.0

    for r in results:
        for t in r.trades:
            t.symbol = r.symbol
        all_trades.extend(r.trades)
        wins = [t for t in r.trades if t.net_pnl > 0]
        losses = [t for t in r.trades if t.net_pnl <= 0]
        total_pnl = sum(t.net_pnl for t in r.trades)
        per_symbol.append({
            "symbol": r.symbol,
            "candles": r.candles,
            "trades": len(r.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(r.trades) * 100) if r.trades else 0.0,
            "starting": r.starting_equity,
            "final": r.final_equity,
            "pnl": total_pnl,
            "max_drawdown": r.max_drawdown,
            "blocked": r.blocked_reasons,
        })
        total_starting += r.starting_equity
        total_final += r.final_equity
        total_drawdown += r.max_drawdown

    wins = [t for t in all_trades if t.net_pnl > 0]
    losses = [t for t in all_trades if t.net_pnl <= 0]
    avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0
    total_pnl = sum(t.net_pnl for t in all_trades)
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0.0
    expectancy = total_pnl / len(all_trades) if all_trades else 0.0
    profit_factor = (sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))) if losses and sum(t.net_pnl for t in losses) != 0 else 0.0

    return {
        "per_symbol": per_symbol,
        "summary": {
            "total_trades": len(all_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy_per_trade": expectancy,
            "total_pnl": total_pnl,
            "profit_factor": profit_factor,
            "total_starting": total_starting,
            "total_final": total_final,
            "total_drawdown": total_drawdown,
            "reward_to_risk": (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0,
        },
    }


def print_summary(report: dict[str, Any], days: int, interval: str) -> None:
    s = report["summary"]
    print(f"\n=== LEVERAGED BACKTEST: {days} days × {interval} ===\n")
    print(f"{'symbol':<12} {'trades':>6} {'wins':>5} {'wr%':>6} {'pnl':>10} {'max_dd':>8}")
    print(f"{'-'*12} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*8}")
    for ps in report["per_symbol"]:
        print(f"{ps['symbol']:<12} {ps['trades']:>6} {ps['wins']:>5} {ps['win_rate']:>5.1f}% {ps['pnl']:>+10.2f} {ps['max_drawdown']:>8.2f}")
    print(f"{'-'*12} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*8}")
    print()
    print(f"Total trades:        {s['total_trades']}")
    print(f"Win rate:            {s['win_rate_pct']:.2f}% ({s['wins']}W / {s['losses']}L)")
    print(f"Avg win:             {s['avg_win']:+.3f}")
    print(f"Avg loss:            {s['avg_loss']:+.3f}")
    print(f"Reward / risk:       {s['reward_to_risk']:.2f}")
    print(f"Expectancy / trade:  {s['expectancy_per_trade']:+.3f}")
    print(f"Profit factor:       {s['profit_factor']:.2f}")
    print(f"Total PnL aggregate: {s['total_pnl']:+.2f}  (sum across all symbol simulations)")
    print(f"Total drawdown sum:  {s['total_drawdown']:.2f}")


def main() -> int:
    args = parse_args()
    client = BinanceFuturesClient()

    if args.symbols:
        symbols = parse_symbols(args.symbols)
    elif args.majors_only:
        symbols = list(MAJOR_SYMBOLS)
    else:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
        symbols = quality_symbols(symbols, majors_only=False)

    print(f"Backtesting {len(symbols)} symbols over {args.days} days @ {args.interval}")
    print(f"Config: lev={args.leverage}x  margin_frac={args.margin_fraction}  SL={args.stop_loss_pct*100:.2f}%  TP={args.take_profit_pct*100:.2f}%")
    print(f"Min vol ratio: {args.min_volume_ratio}  Breakout lookback: {args.breakout_lookback}")

    config = make_config(args, symbols)
    results: list[SymbolResult] = []
    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {symbol} ... ", end="", flush=True)
        try:
            candles = fetch_history(client, symbol, args.interval, args.days)
        except Exception as exc:
            print(f"FETCH FAILED: {exc}")
            continue
        if len(candles) < args.candle_warmup + 100:
            print(f"too few candles ({len(candles)})")
            continue
        result = simulate_symbol(symbol, candles, config, args)
        results.append(result)
        wins = sum(1 for t in result.trades if t.net_pnl > 0)
        total_pnl = sum(t.net_pnl for t in result.trades)
        print(f"{len(candles)} candles, {len(result.trades)} trades, {wins}W, pnl={total_pnl:+.2f}")

    report = aggregate_results(results, args.starting_quote)
    print_summary(report, args.days, args.interval)

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nFull report written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
