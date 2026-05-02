from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.leveraged_session import (
    LeverageConfig,
    LeveragedState,
    TradeSignal,
    close_position,
    entry_signal,
    exit_signal,
    fetch_candles,
    quality_symbols,
    select_top_usdt_futures_symbols,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize leveraged paper settings on recent futures candles.")
    parser.add_argument("--symbols", help="Comma-separated symbols.")
    parser.add_argument("--majors-only", action="store_true")
    parser.add_argument("--top-usdt", type=int, default=40)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--candle-limit", type=int, default=600)
    parser.add_argument("--starting-quote", type=float, default=50.0)
    parser.add_argument("--challenge", action="store_true", help="Search high-risk settings for a 50 USDT challenge target.")
    parser.add_argument("--walk-forward", action="store_true", help="Score candidates on a train/test split.")
    return parser.parse_args()


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def base_config(symbols: list[str], args: argparse.Namespace) -> LeverageConfig:
    if args.challenge:
        return LeverageConfig(
            symbols=symbols,
            interval=args.interval,
            candle_limit=args.candle_limit,
            starting_quote=args.starting_quote,
            leverage=50,
            margin_fraction=0.90,
            max_margin_quote=45,
            min_margin_quote=10,
            stop_loss_pct=0.0025,
            take_profit_pct=0.010,
            trailing_stop_pct=0.003,
            max_daily_loss_quote=25,
            target_pnl_quote=20,
            min_price=0.01,
            cooldown_seconds=0,
            stop_after_profit=False,
            min_profit_quote=20,
            min_volume_ratio=1.0,
            breakout_lookback=10,
        )
    return LeverageConfig(
        symbols=symbols,
        interval=args.interval,
        candle_limit=args.candle_limit,
        starting_quote=args.starting_quote,
        leverage=5,
        margin_fraction=0.45,
        max_margin_quote=35,
        min_margin_quote=5,
        stop_loss_pct=0.002,
        take_profit_pct=0.003,
        trailing_stop_pct=0.0012,
        max_daily_loss_quote=8,
        target_pnl_quote=50,
        min_price=0.05,
        cooldown_seconds=0,
        stop_after_profit=False,
        min_profit_quote=0.02,
    )


def simulate_symbol(symbol: str, candles, config: LeverageConfig) -> dict[str, Any]:
    state = LeveragedState.fresh(config.starting_quote)
    trades: list[dict[str, Any]] = []
    max_equity = config.starting_quote
    max_drawdown = 0.0
    start_index = 90

    for index in range(start_index, len(candles)):
        visible = candles[: index + 1]
        price = visible[-1].close
        if state.is_open:
            signal = exit_signal(symbol, visible, state, config)
            if signal.action == "CLOSE":
                row = close_position(state, signal, config)
                trades.append(row)
        else:
            signal = entry_signal(symbol, visible, config)
            if signal.action == "OPEN" and signal.price >= config.min_price:
                open_position_backtest(state, signal, config)

        equity = state.equity(price, config)
        max_equity = max(max_equity, equity)
        max_drawdown = max(max_drawdown, max_equity - equity)

    if state.is_open:
        final_price = candles[-1].close
        signal = TradeSignal("CLOSE", state.side, symbol, final_price, 0, "Backtest end.", {})
        row = close_position(state, signal, config)
        trades.append(row)

    wins = [trade for trade in trades if trade.get("realized_pnl", 0) > 0]
    return {
        "symbol": symbol,
        "ending_quote": state.cash_quote,
        "pnl": state.cash_quote - config.starting_quote,
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "max_drawdown": max_drawdown,
        "config": config,
    }


def open_position_backtest(state: LeveragedState, signal: TradeSignal, config: LeverageConfig) -> None:
    margin = min(state.cash_quote * config.margin_fraction, config.max_margin_quote)
    if margin < config.min_margin_quote:
        return
    notional = margin * config.leverage
    entry_fee = notional * (config.fee_bps / 10_000)
    if state.cash_quote < margin + entry_fee:
        return
    state.cash_quote -= margin + entry_fee
    state.position_symbol = signal.symbol
    state.side = signal.side
    state.entry_price = signal.price
    state.quantity = notional / signal.price
    state.margin_quote = margin
    state.entry_fee_quote = entry_fee
    state.high_watermark = signal.price
    state.low_watermark = signal.price


def candidate_configs(config: LeverageConfig):
    for leverage in [8, 10, 12]:
        for stop_loss in [0.002, 0.0025]:
            for take_profit in [0.0035, 0.005]:
                for min_volume in [0.8, 1.0]:
                    yield replace(
                        config,
                        leverage=leverage,
                        stop_loss_pct=stop_loss,
                        take_profit_pct=take_profit,
                        trailing_stop_pct=0.0012,
                        min_volume_ratio=min_volume,
                    )


def challenge_candidate_configs(config: LeverageConfig):
    for leverage in [35, 50]:
        for stop_loss in [0.0025, 0.0035]:
            for take_profit in [0.008, 0.010]:
                for min_volume in [0.8, 1.0]:
                    yield replace(
                        config,
                        leverage=leverage,
                        stop_loss_pct=stop_loss,
                        take_profit_pct=take_profit,
                        trailing_stop_pct=0.003,
                        min_volume_ratio=min_volume,
                    )


def score_result(result: dict[str, Any]) -> float:
    if result["trades"] < 2:
        return -999
    return result["pnl"] - (result["max_drawdown"] * 0.75)


def walk_forward_result(symbol: str, candles, config: LeverageConfig) -> dict[str, Any]:
    split = int(len(candles) * 0.65)
    train = simulate_symbol(symbol, candles[:split], config)
    test = simulate_symbol(symbol, candles[split - 90 :], config)
    return {
        "symbol": symbol,
        "ending_quote": test["ending_quote"],
        "pnl": test["pnl"],
        "trades": test["trades"],
        "wins": test["wins"],
        "win_rate": test["win_rate"],
        "max_drawdown": test["max_drawdown"],
        "train_pnl": train["pnl"],
        "train_trades": train["trades"],
        "config": config,
    }


def score_walk_forward(result: dict[str, Any]) -> float:
    if result["train_trades"] < 1 or result["trades"] < 1:
        return -999
    if result["train_pnl"] <= 0:
        return -999
    return result["pnl"] - (result["max_drawdown"] * 0.5)


def main() -> int:
    args = parse_args()
    client = BinanceFuturesClient()
    symbols = parse_symbols(args.symbols)
    if not symbols:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
    symbols = quality_symbols(symbols, args.majors_only)
    config = base_config(symbols, args)

    results: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            candles = fetch_candles(client, symbol, config)
        except Exception:
            continue
        candidates = challenge_candidate_configs(config) if args.challenge else candidate_configs(config)
        for candidate in candidates:
            candidate = replace(candidate, symbols=[symbol])
            result = walk_forward_result(symbol, candles, candidate) if args.walk_forward else simulate_symbol(symbol, candles, candidate)
            results.append(result)

    results.sort(key=score_walk_forward if args.walk_forward else score_result, reverse=True)
    print("Optimizer results")
    for result in results[:10]:
        cfg = result["config"]
        train = f" train_pnl={result['train_pnl']:.4f} train_trades={result['train_trades']}" if "train_pnl" in result else ""
        print(
            f"{result['symbol']} pnl={result['pnl']:.4f} end={result['ending_quote']:.4f} "
            f"trades={result['trades']} wins={result['wins']} win_rate={result['win_rate']:.1f}% "
            f"dd={result['max_drawdown']:.4f} lev={cfg.leverage} stop={cfg.stop_loss_pct} "
            f"tp={cfg.take_profit_pct} trail={cfg.trailing_stop_pct} vol={cfg.min_volume_ratio}{train}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
