from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.leveraged_session import book_spread_map, quality_symbols, select_top_usdt_futures_symbols
from trading_bot.models import Candle
from trading_bot.recovery_scanner import logged_loser_symbols, parse_symbols, risk_plans


@dataclass(frozen=True)
class Timeframe:
    price: float
    ema9: float
    ema21: float
    ema55: float
    rsi14: float
    atr_pct: float
    range_pct: float
    momentum_5: float
    momentum_15: float
    volume_ratio: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strict scanner for rare high-asymmetry recovery setups.")
    parser.add_argument("--balance", type=float, default=39.05)
    parser.add_argument("--target-balance", type=float, default=55.0)
    parser.add_argument("--top-usdt", type=int, default=220)
    parser.add_argument("--symbols")
    parser.add_argument("--max-loss", type=float, default=8.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--max-spread-pct", type=float, default=0.0010)
    parser.add_argument("--min-volume-ratio", type=float, default=1.2)
    parser.add_argument("--min-score", type=float, default=12.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-prior-losers", action="store_true")
    return parser.parse_args()


def fetch_candles(client: BinanceFuturesClient, symbol: str, interval: str, limit: int) -> list[Candle]:
    return [Candle.from_binance(row) for row in client.get_klines(symbol, interval, limit)]


def timeframe(candles: list[Candle], range_bars: int = 45) -> Timeframe:
    close_values = closes(candles)
    price = close_values[-1]
    ema9 = ema(close_values, 9)[-1]
    ema21 = ema(close_values, 21)[-1]
    ema55 = ema(close_values, 55)[-1]
    rsi14 = rsi(close_values, 14)[-1] or 50.0
    atr14 = atr(candles, 14)[-1] or 0.0
    recent = candles[-range_bars:]
    avg_volume = sum(c.volume for c in candles[-31:-1]) / 30
    return Timeframe(
        price=price,
        ema9=ema9,
        ema21=ema21,
        ema55=ema55,
        rsi14=float(rsi14),
        atr_pct=atr14 / price if price else 0.0,
        range_pct=(max(c.high for c in recent) - min(c.low for c in recent)) / price if price else 0.0,
        momentum_5=(close_values[-1] / close_values[-6] - 1) if len(close_values) > 6 else 0.0,
        momentum_15=(close_values[-1] / close_values[-16] - 1) if len(close_values) > 16 else 0.0,
        volume_ratio=candles[-1].volume / avg_volume if avg_volume else 1.0,
    )


def direction(tf1: Timeframe, tf5: Timeframe, funding_rate: float, pct_24h: float) -> tuple[str, str] | None:
    one_min_long = tf1.ema9 > tf1.ema21 > tf1.ema55 and tf1.momentum_5 > 0.001 and tf1.momentum_15 > 0.002
    five_min_long = tf5.ema9 > tf5.ema21 and tf5.price > tf5.ema55
    one_min_short = tf1.ema9 < tf1.ema21 < tf1.ema55 and tf1.momentum_5 < -0.001 and tf1.momentum_15 < -0.002
    five_min_short = tf5.ema9 < tf5.ema21 and tf5.price < tf5.ema55

    if one_min_long and five_min_long and funding_rate <= 0.001 and 45 <= tf1.rsi14 <= 78:
        return "LONG", "aligned squeeze/continuation"
    if one_min_short and five_min_short and funding_rate >= -0.001 and 20 <= tf1.rsi14 <= 55:
        return "SHORT", "aligned breakdown/reversion"

    # Allow one special case: violent pump reversion when funding is positive and 1m/5m are both breaking down.
    if one_min_short and tf5.momentum_5 < -0.002 and funding_rate >= 0 and pct_24h >= 10:
        return "SHORT", "overextended pump breakdown"
    return None


def score_candidate(
    tf1: Timeframe,
    tf5: Timeframe,
    target_move: float,
    spread_pct: float,
    funding_rate: float,
    pct_24h: float,
    side: str,
) -> float:
    volatility_score = (tf1.range_pct / target_move) + (tf5.range_pct / target_move * 0.6)
    atr_score = (tf1.atr_pct / target_move) * 2.0
    volume_score = min(tf1.volume_ratio, 5.0) * 0.8
    funding_score = 0.0
    if side == "LONG" and funding_rate < 0:
        funding_score = min(abs(funding_rate) * 500, 4.0)
    if side == "SHORT" and funding_rate > 0:
        funding_score = min(funding_rate * 500, 4.0)
    trend_score = min(abs(tf1.momentum_15) * 250, 4.0)
    return volatility_score + atr_score + volume_score + funding_score + trend_score + abs(pct_24h) / 20 - spread_pct * 200


def best_plan(balance: float, target_profit: float, fee_bps: float, max_loss: float, target_move_cap: float):
    for plan in risk_plans(balance, target_profit, fee_bps, max_loss):
        if plan.target_move_pct <= target_move_cap:
            return plan
    return None


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
    if not args.include_prior_losers:
        losers = logged_loser_symbols()
        symbols = [s for s in symbols if s not in losers]
    else:
        losers = set()

    spreads = book_spread_map(client)
    tickers = {row["symbol"]: row for row in client.get_24hr_tickers()}
    premiums = {row["symbol"]: row for row in client.public_get("/fapi/v1/premiumIndex")}
    out: list[dict[str, Any]] = []

    for symbol in symbols:
        try:
            tf1 = timeframe(fetch_candles(client, symbol, "1m", 240), 45)
            tf5 = timeframe(fetch_candles(client, symbol, "5m", 120), 36)
            spread_pct = spreads.get(symbol, 0.0)
            funding_rate = float(premiums.get(symbol, {}).get("lastFundingRate", 0.0))
            pct_24h = float(tickers.get(symbol, {}).get("priceChangePercent", 0.0))
        except Exception:
            continue
        if spread_pct > args.max_spread_pct or tf1.volume_ratio < args.min_volume_ratio:
            continue
        picked = direction(tf1, tf5, funding_rate, pct_24h)
        if picked is None:
            continue
        side, thesis = picked
        target_move_cap = min(tf1.range_pct, tf5.range_pct) * 0.80
        plan = best_plan(args.balance, target_profit, args.fee_bps, args.max_loss, target_move_cap)
        if plan is None:
            continue
        if tf1.atr_pct < plan.target_move_pct / 8:
            continue
        score = score_candidate(tf1, tf5, plan.target_move_pct, spread_pct, funding_rate, pct_24h, side)
        if score < args.min_score:
            continue
        out.append(
            {
                "symbol": symbol,
                "side": side,
                "thesis": thesis,
                "score": score,
                "tf1": tf1,
                "tf5": tf5,
                "spread_pct": spread_pct,
                "funding_rate": funding_rate,
                "pct_24h": pct_24h,
                "plan": plan,
            }
        )

    out.sort(key=lambda x: x["score"], reverse=True)
    print(
        f"Miracle scan: balance={args.balance:.2f} target={args.target_balance:.2f} "
        f"need={target_profit:.2f} symbols={len(symbols)} skipped_losers={len(losers)}"
    )
    if not out:
        print("NO_MIRACLE_SETUP")
        return 2
    for index, item in enumerate(out[: args.limit], 1):
        plan = item["plan"]
        tf1 = item["tf1"]
        tf5 = item["tf5"]
        print(
            f"{index}. {item['symbol']} {item['side']} score={item['score']:.2f} "
            f"price={tf1.price:.8f} thesis={item['thesis']} target_move={plan.target_move_pct * 100:.2f}% "
            f"risk={plan.stop_loss_cash:.2f} lev={plan.leverage:g}x margin={plan.margin:.2f}"
        )
        print(
            f"   24h={item['pct_24h']:.1f}% funding={item['funding_rate'] * 100:.3f}% "
            f"spread={item['spread_pct'] * 100:.3f}% 1mRange={tf1.range_pct * 100:.2f}% "
            f"5mRange={tf5.range_pct * 100:.2f}% ATR={tf1.atr_pct * 100:.3f}% "
            f"vol={tf1.volume_ratio:.2f} rsi={tf1.rsi14:.1f}"
        )
    best = out[0]
    plan = best["plan"]
    print("BEST_PAPER_COMMAND")
    print(
        "py -m trading_bot.leveraged_session --reset "
        f"--symbols {best['symbol']} --interval 1m --cycles 12 --poll-seconds 15 "
        f"--starting-quote {args.balance:g} --leverage {plan.leverage:g} "
        f"--margin-fraction {plan.margin_fraction:g} --max-margin {plan.margin:.6f} --min-margin 8 "
        f"--stop-loss-pct {plan.stop_loss_pct:.6f} --take-profit-pct {plan.target_move_pct:.6f} "
        f"--trailing-stop-pct {max(plan.target_move_pct * 0.35, 0.003):.6f} "
        f"--max-daily-loss {args.max_loss:g} --target-pnl {target_profit:.6f} "
        "--min-price 0.01 --max-spread-pct 0.0015 --cooldown-seconds 300 "
        f"--min-profit {target_profit:.6f} --min-trail-profit-pct {max(plan.target_move_pct * 0.35, 0.003):.6f} "
        "--min-volume-ratio 0.75 --breakout-lookback 10"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
