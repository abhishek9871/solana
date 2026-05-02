from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.challenge_scanner import parse_symbols
from trading_bot.config import ROOT_DIR
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.leveraged_session import book_spread_map, quality_symbols, select_top_usdt_futures_symbols
from trading_bot.models import Candle


@dataclass(frozen=True)
class RiskPlan:
    leverage: float
    margin_fraction: float
    stop_loss_pct: float
    margin: float
    notional: float
    target_move_pct: float
    stop_loss_cash: float
    fees_round_trip: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research scanner for a small-account recovery target.")
    parser.add_argument("--balance", type=float, default=39.05)
    parser.add_argument("--target-balance", type=float, default=55.0)
    parser.add_argument("--top-usdt", type=int, default=120)
    parser.add_argument("--symbols")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--candle-limit", type=int, default=240)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--max-loss", type=float, default=8.0)
    parser.add_argument("--min-price", type=float, default=0.01)
    parser.add_argument("--max-spread-pct", type=float, default=0.0015)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--skip-symbols", default="")
    parser.add_argument("--include-loss-log-symbols", action="store_true")
    return parser.parse_args()


def fetch_candles(client: BinanceFuturesClient, symbol: str, interval: str, limit: int) -> list[Candle]:
    return [Candle.from_binance(row) for row in client.get_klines(symbol, interval, limit)]


def logged_loser_symbols() -> set[str]:
    paths = [
        ROOT_DIR / "logs" / "funding_live_trades.jsonl",
        ROOT_DIR / "logs" / "leveraged_trades.jsonl",
    ]
    pnl_by_symbol: dict[str, float] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            symbol = row.get("symbol")
            if not symbol:
                continue
            pnl = row.get("net_pnl", row.get("realized_pnl"))
            if pnl in ("", None):
                continue
            try:
                pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, 0.0) + float(pnl)
            except (TypeError, ValueError):
                continue
    return {symbol for symbol, pnl in pnl_by_symbol.items() if pnl <= -1.0}


def risk_plans(balance: float, target_profit: float, fee_bps: float, max_loss: float) -> list[RiskPlan]:
    out: list[RiskPlan] = []
    fee_rate = fee_bps / 10_000
    for leverage in [15, 20, 25, 35, 50]:
        for margin_fraction in [0.35, 0.5, 0.65, 0.8, 0.9]:
            margin = balance * margin_fraction
            notional = margin * leverage
            if margin < 8 or notional <= 0:
                continue
            fees = notional * fee_rate * 2
            target_move = (target_profit + fees) / notional
            for stop_loss_pct in [0.003, 0.004, 0.005, 0.0075, 0.01]:
                stop_loss_cash = (notional * stop_loss_pct) + fees
                if stop_loss_cash > max_loss:
                    continue
                if target_move <= 0 or target_move > 0.08:
                    continue
                out.append(
                    RiskPlan(
                        leverage=leverage,
                        margin_fraction=margin_fraction,
                        stop_loss_pct=stop_loss_pct,
                        margin=margin,
                        notional=notional,
                        target_move_pct=target_move,
                        stop_loss_cash=stop_loss_cash,
                        fees_round_trip=fees,
                    )
                )
    out.sort(key=lambda plan: (plan.target_move_pct, plan.stop_loss_cash))
    return out


def feature_pack(candles_1m: list[Candle], candles_5m: list[Candle]) -> dict[str, float]:
    close_values = closes(candles_1m)
    close_5m = closes(candles_5m)
    price = close_values[-1]
    fast = ema(close_values, 9)[-1]
    slow = ema(close_values, 21)[-1]
    trend = ema(close_values, 55)[-1]
    trend_5m = ema(close_5m, 34)[-1]
    rsi_1m = rsi(close_values, 14)[-1] or 50.0
    atr_1m = atr(candles_1m, 14)[-1] or 0.0
    high_45 = max(c.high for c in candles_1m[-45:])
    low_45 = min(c.low for c in candles_1m[-45:])
    high_120 = max(c.high for c in candles_1m[-120:])
    low_120 = min(c.low for c in candles_1m[-120:])
    high_5m = max(c.high for c in candles_5m[-36:])
    low_5m = min(c.low for c in candles_5m[-36:])
    volume_now = candles_1m[-1].volume
    avg_volume = sum(c.volume for c in candles_1m[-31:-1]) / 30
    momentum_5 = (price - close_values[-6]) / close_values[-6]
    momentum_15 = (price - close_values[-16]) / close_values[-16]
    return {
        "price": price,
        "fast_gap": (fast - slow) / slow if slow else 0.0,
        "trend_gap": (price - trend) / trend if trend else 0.0,
        "trend_5m_gap": (price - trend_5m) / trend_5m if trend_5m else 0.0,
        "rsi": float(rsi_1m),
        "atr_pct": atr_1m / price if price else 0.0,
        "range_45_pct": (high_45 - low_45) / price if price else 0.0,
        "range_120_pct": (high_120 - low_120) / price if price else 0.0,
        "range_5m_pct": (high_5m - low_5m) / price if price else 0.0,
        "volume_ratio": volume_now / avg_volume if avg_volume else 1.0,
        "momentum_5": momentum_5,
        "momentum_15": momentum_15,
        "close_vs_45_high": (price - high_45) / price if price else 0.0,
        "close_vs_45_low": (price - low_45) / price if price else 0.0,
    }


def setup_side(features: dict[str, float], funding_rate: float, pct_24h: float) -> tuple[str, str, float] | None:
    long_score = 0.0
    short_score = 0.0

    if features["fast_gap"] > 0 and features["trend_gap"] > 0 and features["trend_5m_gap"] > 0:
        long_score += 2.0
    if features["momentum_5"] > 0.0015 and features["momentum_15"] > 0.003:
        long_score += 1.5
    if funding_rate < -0.002:
        long_score += min(abs(funding_rate) * 500, 3.0)
    if 48 <= features["rsi"] <= 78:
        long_score += 1.0

    if features["fast_gap"] < 0 and features["trend_gap"] < 0 and features["trend_5m_gap"] < 0:
        short_score += 2.0
    if features["momentum_5"] < -0.0015 and features["momentum_15"] < -0.003:
        short_score += 1.5
    if funding_rate > 0.0015:
        short_score += min(funding_rate * 500, 3.0)
    if pct_24h > 15 and features["close_vs_45_high"] < -0.003:
        short_score += 1.5
    if 22 <= features["rsi"] <= 55:
        short_score += 1.0

    if long_score >= 4.0 and long_score >= short_score + 0.75:
        return "LONG", "squeeze/continuation long", long_score
    if short_score >= 4.0 and short_score >= long_score + 0.75:
        return "SHORT", "reversion/breakdown short", short_score
    return None


def evaluate_symbol(
    symbol: str,
    candles_1m: list[Candle],
    candles_5m: list[Candle],
    risk_options: list[RiskPlan],
    spread_pct: float,
    funding_rate: float,
    pct_24h: float,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    features = feature_pack(candles_1m, candles_5m)
    if features["price"] < args.min_price or spread_pct > args.max_spread_pct:
        return None
    side = setup_side(features, funding_rate, pct_24h)
    if side is None:
        return None
    direction, thesis, thesis_score = side
    for plan in risk_options:
        range_ok = features["range_45_pct"] >= plan.target_move_pct * 0.75 and features["range_120_pct"] >= plan.target_move_pct
        atr_ok = features["atr_pct"] >= plan.target_move_pct / 10
        volume_ok = features["volume_ratio"] >= 0.75
        if not (range_ok and atr_ok and volume_ok):
            continue
        rr = args.target_balance - args.balance
        rr = rr / max(plan.stop_loss_cash, 0.01)
        if rr < 1.8:
            continue
        score = (
            thesis_score
            + (features["range_45_pct"] / plan.target_move_pct)
            + (features["atr_pct"] / max(plan.target_move_pct, 0.0001))
            + min(features["volume_ratio"], 4.0) * 0.35
            - spread_pct * 100
            - plan.stop_loss_cash / 10
        )
        return {
            "symbol": symbol,
            "side": direction,
            "thesis": thesis,
            "score": score,
            "price": features["price"],
            "funding_rate": funding_rate,
            "pct_24h": pct_24h,
            "spread_pct": spread_pct,
            "features": features,
            "plan": plan,
        }
    return None


def print_candidate(candidate: dict[str, Any], rank: int, target_profit: float) -> None:
    plan: RiskPlan = candidate["plan"]
    f = candidate["features"]
    print(
        f"{rank}. {candidate['symbol']} {candidate['side']} score={candidate['score']:.2f} "
        f"price={candidate['price']:.8f} thesis={candidate['thesis']} "
        f"target_move={plan.target_move_pct * 100:.2f}% risk={plan.stop_loss_cash:.2f} "
        f"target={target_profit:.2f} lev={plan.leverage:g}x margin={plan.margin:.2f} notional={plan.notional:.2f}"
    )
    print(
        f"   24h={candidate['pct_24h']:.1f}% funding={candidate['funding_rate'] * 100:.3f}% "
        f"spread={candidate['spread_pct'] * 100:.3f}% range45={f['range_45_pct'] * 100:.2f}% "
        f"range120={f['range_120_pct'] * 100:.2f}% atr={f['atr_pct'] * 100:.3f}% "
        f"vol={f['volume_ratio']:.2f} rsi={f['rsi']:.1f}"
    )


def command_for(candidate: dict[str, Any], args: argparse.Namespace) -> str:
    plan: RiskPlan = candidate["plan"]
    return (
        "py -m trading_bot.leveraged_session --reset "
        f"--symbols {candidate['symbol']} --interval {args.interval} --cycles 12 --poll-seconds 15 "
        f"--starting-quote {args.balance:g} --leverage {plan.leverage:g} "
        f"--margin-fraction {plan.margin_fraction:g} --max-margin {plan.margin:.6f} --min-margin 8 "
        f"--stop-loss-pct {plan.stop_loss_pct:.6f} --take-profit-pct {plan.target_move_pct:.6f} "
        f"--trailing-stop-pct {max(plan.target_move_pct * 0.35, 0.003):.6f} "
        f"--max-daily-loss {args.max_loss:g} --target-pnl {args.target_balance - args.balance:.6f} "
        f"--min-price {args.min_price:g} --max-spread-pct {args.max_spread_pct:g} "
        f"--cooldown-seconds 300 --min-profit {args.target_balance - args.balance:.6f} "
        f"--min-trail-profit-pct {max(plan.target_move_pct * 0.35, 0.003):.6f} "
        "--min-volume-ratio 0.75 --breakout-lookback 10"
    )


def main() -> int:
    args = parse_args()
    target_profit = args.target_balance - args.balance
    if target_profit <= 0:
        print("Target balance must be above balance.")
        return 1
    client = BinanceFuturesClient()
    symbols = parse_symbols(args.symbols)
    if not symbols:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
    symbols = quality_symbols(symbols, majors_only=False)
    manual_skips = set(parse_symbols(args.skip_symbols))
    loss_log_skips = set() if args.include_loss_log_symbols else logged_loser_symbols()
    skip_symbols = manual_skips | loss_log_skips
    symbols = [symbol for symbol in symbols if symbol not in skip_symbols]
    spreads = book_spread_map(client)
    tickers = {row["symbol"]: row for row in client.get_24hr_tickers()}
    premiums = {row["symbol"]: row for row in client.public_get("/fapi/v1/premiumIndex")}
    plans = risk_plans(args.balance, target_profit, args.fee_bps, args.max_loss)

    candidates: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            candles_1m = fetch_candles(client, symbol, args.interval, args.candle_limit)
            candles_5m = fetch_candles(client, symbol, "5m", 96)
            funding_rate = float(premiums.get(symbol, {}).get("lastFundingRate", 0.0))
            pct_24h = float(tickers.get(symbol, {}).get("priceChangePercent", 0.0))
        except Exception:
            continue
        candidate = evaluate_symbol(
            symbol,
            candles_1m,
            candles_5m,
            plans,
            spreads.get(symbol, 0.0),
            funding_rate,
            pct_24h,
            args,
        )
        if candidate:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    print(
        f"Recovery scan: balance={args.balance:.2f} target_balance={args.target_balance:.2f} "
        f"needed_profit={target_profit:.2f} max_loss={args.max_loss:.2f} symbols={len(symbols)}"
    )
    if skip_symbols:
        print(f"Skipped prior losers: {','.join(sorted(skip_symbols))}")
    if not candidates:
        print("NO_A_GRADE_SETUP")
        print("Reason: no current setup passed direction, funding/volume, volatility, spread, and risk/reward filters together.")
        return 2
    for index, candidate in enumerate(candidates[: args.limit], start=1):
        print_candidate(candidate, index, target_profit)
    print("BEST_PAPER_COMMAND")
    print(command_for(candidates[0], args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
