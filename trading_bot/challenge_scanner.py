from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.leveraged_session import (
    LeverageConfig,
    book_spread_map,
    entry_signal,
    fetch_candles,
    quality_symbols,
    select_top_usdt_futures_symbols,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast scanner for target-capable 50 USDT futures paper setups.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Overrides --top-usdt.")
    parser.add_argument("--top-usdt", type=int, default=80)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--candle-limit", type=int, default=180)
    parser.add_argument("--starting-quote", type=float, default=50.0)
    parser.add_argument("--target-profit", type=float, default=20.0)
    parser.add_argument("--leverage", type=float, default=50.0)
    parser.add_argument("--margin-fraction", type=float, default=0.90)
    parser.add_argument("--max-margin", type=float, default=45.0)
    parser.add_argument("--min-margin", type=float, default=10.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.0035)
    parser.add_argument("--max-spread-pct", type=float, default=0.0015)
    parser.add_argument("--min-volume-ratio", type=float, default=0.8)
    parser.add_argument("--breakout-lookback", type=int, default=10)
    parser.add_argument("--min-price", type=float, default=0.01)
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def build_config(symbols: list[str], args: argparse.Namespace) -> LeverageConfig:
    return LeverageConfig(
        symbols=symbols,
        interval=args.interval,
        candle_limit=args.candle_limit,
        starting_quote=args.starting_quote,
        leverage=args.leverage,
        margin_fraction=args.margin_fraction,
        max_margin_quote=args.max_margin,
        min_margin_quote=args.min_margin,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=required_move_pct(args),
        trailing_stop_pct=max(required_move_pct(args) * 0.35, 0.003),
        max_daily_loss_quote=max(args.target_profit * 1.25, 25),
        target_pnl_quote=args.target_profit,
        min_price=args.min_price,
        max_spread_pct=args.max_spread_pct,
        min_volume_ratio=args.min_volume_ratio,
        breakout_lookback=args.breakout_lookback,
    )


def required_move_pct(args: argparse.Namespace) -> float:
    margin = min(args.starting_quote * args.margin_fraction, args.max_margin)
    notional = margin * args.leverage
    fee_rate = 5.0 / 10_000
    return (args.target_profit + (notional * fee_rate * 2)) / notional


def candidate_payload(
    symbol: str,
    candles,
    spread_pct: float,
    base_config: LeverageConfig,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    config = replace(base_config, symbols=[symbol])
    signal = entry_signal(symbol, candles, config)
    if signal.action != "OPEN":
        return None
    if signal.price < args.min_price or spread_pct > args.max_spread_pct:
        return None

    margin = min(args.starting_quote * args.margin_fraction, args.max_margin)
    notional = margin * args.leverage
    move_needed = required_move_pct(args)
    stop_loss_cash = (args.stop_loss_pct * notional) + (notional * 5.0 / 10_000 * 2)
    recent = candles[-45:]
    recent_range_pct = (max(candle.high for candle in recent) - min(candle.low for candle in recent)) / signal.price
    atr_pct = float(signal.indicators.get("atr_pct") or 0.0)
    volume_ratio = float(signal.indicators.get("volume_ratio") or 0.0)
    momentum_5 = abs(float(signal.indicators.get("momentum_5") or 0.0))

    target_capable = (
        recent_range_pct >= move_needed * 0.85
        and atr_pct >= move_needed / 8
        and volume_ratio >= args.min_volume_ratio
    )
    if not target_capable:
        return None

    score = (
        (recent_range_pct / move_needed)
        + (atr_pct / max(move_needed, 0.000001))
        + min(volume_ratio, 4.0) * 0.25
        + momentum_5 * 100
        - spread_pct * 100
    )

    return {
        "symbol": symbol,
        "side": signal.side,
        "price": signal.price,
        "score": score,
        "move_needed_pct": move_needed,
        "recent_range_pct": recent_range_pct,
        "atr_pct": atr_pct,
        "volume_ratio": volume_ratio,
        "spread_pct": spread_pct,
        "margin": margin,
        "notional": notional,
        "target_profit": args.target_profit,
        "stop_loss_cash_est": stop_loss_cash,
        "take_profit_pct": move_needed,
        "trailing_stop_pct": max(move_needed * 0.35, 0.003),
    }


def command_for(candidate: dict[str, Any], args: argparse.Namespace) -> str:
    return (
        "py -m trading_bot.leveraged_session --reset "
        f"--symbols {candidate['symbol']} --interval {args.interval} --cycles 20 --poll-seconds 15 "
        f"--starting-quote {args.starting_quote:g} --leverage {args.leverage:g} "
        f"--margin-fraction {args.margin_fraction:g} --max-margin {args.max_margin:g} --min-margin {args.min_margin:g} "
        f"--stop-loss-pct {args.stop_loss_pct:g} --take-profit-pct {candidate['take_profit_pct']:.6f} "
        f"--trailing-stop-pct {candidate['trailing_stop_pct']:.6f} --max-daily-loss {max(args.target_profit * 1.25, 25):g} "
        f"--target-pnl {args.target_profit:g} --min-price {args.min_price:g} --max-spread-pct {args.max_spread_pct:g} "
        f"--cooldown-seconds 300 --min-profit {args.target_profit:g} --min-trail-profit-pct {candidate['trailing_stop_pct']:.6f} "
        f"--min-volume-ratio {args.min_volume_ratio:g} --breakout-lookback {args.breakout_lookback}"
    )


def main() -> int:
    args = parse_args()
    client = BinanceFuturesClient()
    symbols = parse_symbols(args.symbols)
    if not symbols:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
    symbols = quality_symbols(symbols, majors_only=False)
    config = build_config(symbols, args)
    spreads = book_spread_map(client)

    candidates = []
    for symbol in symbols:
        try:
            candles = fetch_candles(client, symbol, config)
        except Exception:
            continue
        payload = candidate_payload(symbol, candles, spreads.get(symbol, 0.0), config, args)
        if payload:
            candidates.append(payload)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    print(
        f"Challenge scan: symbols={len(symbols)} target={args.target_profit:.2f} "
        f"required_move={required_move_pct(args) * 100:.2f}%"
    )
    if not candidates:
        print("NO_QUALIFYING_SETUP")
        return 2

    for candidate in candidates[: args.limit]:
        print(
            f"{candidate['symbol']} {candidate['side']} score={candidate['score']:.2f} "
            f"price={candidate['price']:.8f} range={candidate['recent_range_pct'] * 100:.2f}% "
            f"atr={candidate['atr_pct'] * 100:.3f}% vol={candidate['volume_ratio']:.2f} "
            f"spread={candidate['spread_pct'] * 100:.3f}% notional={candidate['notional']:.2f} "
            f"stop_loss_est={candidate['stop_loss_cash_est']:.2f}"
        )
    print("BEST_COMMAND")
    print(command_for(candidates[0], args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
