from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.leveraged_session import (
    quality_symbols,
    select_top_usdt_futures_symbols,
)
from trading_bot.models import Candle


FUNDING_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


@dataclass
class FundingEvent:
    symbol: str
    funding_time_ms: int
    funding_rate: float
    mark_price: float


@dataclass
class Trade:
    symbol: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    notional: float
    margin: float
    funding_rate_at_event: float
    funding_collected: float
    price_pnl: float
    fees: float
    net_pnl: float
    exit_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest extreme-negative-funding short-squeeze strategy.")
    parser.add_argument("--top-usdt", type=int, default=80)
    parser.add_argument("--symbols", help="Comma-separated symbol override.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--days-offset", type=int, default=0, help="Skip N days from now backwards (for out-of-sample testing).")
    parser.add_argument("--funding-threshold", type=float, default=-0.005, help="Open long if funding rate <= threshold (e.g., -0.005 = -0.5%).")
    parser.add_argument("--hold-hours", type=float, default=24, help="Hold position N hours after entry.")
    parser.add_argument("--stop-loss-pct", type=float, default=0.05, help="Stop loss as fraction of entry price (e.g., 0.05 = 5%).")
    parser.add_argument("--take-profit-pct", type=float, default=0.15, help="Take profit as fraction of entry price.")
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--margin-per-trade", type=float, default=20.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--min-quote-volume-usd", type=float, default=5_000_000)
    parser.add_argument("--candle-interval", default="5m")
    parser.add_argument("--out", default="logs/funding_backtest.json")
    return parser.parse_args()


def parse_symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def fetch_funding_history(symbol: str, days: int, days_offset: int = 0) -> list[FundingEvent]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - days_offset * 24 * 60 * 60 * 1000
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    events: list[FundingEvent] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000}
        try:
            response = requests.get(FUNDING_HISTORY_URL, params=params, timeout=15).json()
        except Exception:
            return events
        if not response or not isinstance(response, list):
            break
        for row in response:
            events.append(
                FundingEvent(
                    symbol=row["symbol"],
                    funding_time_ms=int(row["fundingTime"]),
                    funding_rate=float(row["fundingRate"]),
                    mark_price=float(row.get("markPrice", 0) or 0),
                )
            )
        if len(response) < 1000:
            break
        cursor = int(response[-1]["fundingTime"]) + 1
        time.sleep(0.05)
    return events


def fetch_5m_history(client: BinanceFuturesClient, symbol: str, days: int, interval: str = "5m", days_offset: int = 0) -> list[Candle]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - days_offset * 24 * 60 * 60 * 1000
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
    seen = set()
    deduped = []
    for row in rows:
        if row[0] in seen:
            continue
        seen.add(row[0])
        deduped.append(row)
    return [Candle.from_binance(row) for row in deduped]


def find_candle_at_or_after(candles: list[Candle], ts_ms: int) -> tuple[int, Candle] | None:
    for i, c in enumerate(candles):
        if c.open_time >= ts_ms:
            return i, c
    return None


def simulate_entry(symbol: str, event: FundingEvent, candles: list[Candle], args: argparse.Namespace, funding_lookup: list[FundingEvent]) -> Trade | None:
    entry_after = find_candle_at_or_after(candles, event.funding_time_ms + 60_000)
    if entry_after is None:
        return None
    entry_idx, entry_candle = entry_after
    entry_price = entry_candle.open

    fee_rate = args.fee_bps / 10_000
    notional = args.margin_per_trade * args.leverage
    quantity = notional / entry_price

    hold_ms = int(args.hold_hours * 60 * 60 * 1000)
    deadline_ms = entry_candle.open_time + hold_ms
    stop_price = entry_price * (1 - args.stop_loss_pct)
    tp_price = entry_price * (1 + args.take_profit_pct)

    exit_reason = "Hold expired"
    exit_price = entry_price
    exit_time_ms = entry_candle.open_time

    for c in candles[entry_idx:]:
        if c.low <= stop_price:
            exit_reason = "Stop loss"
            exit_price = stop_price
            exit_time_ms = c.open_time
            break
        if c.high >= tp_price:
            exit_reason = "Take profit"
            exit_price = tp_price
            exit_time_ms = c.open_time
            break
        if c.close_time >= deadline_ms:
            exit_reason = "Hold expired"
            exit_price = c.close
            exit_time_ms = c.close_time
            break

    funding_collected = 0.0
    for fe in funding_lookup:
        if fe.symbol != symbol:
            continue
        if fe.funding_time_ms <= entry_candle.open_time:
            continue
        if fe.funding_time_ms > exit_time_ms:
            continue
        funding_collected += -fe.funding_rate * quantity * fe.mark_price if fe.mark_price > 0 else -fe.funding_rate * notional

    price_pnl = (exit_price - entry_price) * quantity
    entry_fee = notional * fee_rate
    exit_fee = quantity * exit_price * fee_rate
    fees = entry_fee + exit_fee
    net_pnl = price_pnl + funding_collected - fees

    return Trade(
        symbol=symbol,
        entry_time_ms=entry_candle.open_time,
        exit_time_ms=exit_time_ms,
        entry_price=entry_price,
        exit_price=exit_price,
        notional=notional,
        margin=args.margin_per_trade,
        funding_rate_at_event=event.funding_rate,
        funding_collected=funding_collected,
        price_pnl=price_pnl,
        fees=fees,
        net_pnl=net_pnl,
        exit_reason=exit_reason,
    )


def aggregate(trades: list[Trade]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    total = sum(t.net_pnl for t in trades)
    funding_total = sum(t.funding_collected for t in trades)
    price_total = sum(t.price_pnl for t in trades)
    fee_total = sum(t.fees for t in trades)
    avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0
    expectancy = total / len(trades)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100,
        "total_net_pnl": total,
        "total_funding_collected": funding_total,
        "total_price_pnl": price_total,
        "total_fees": fee_total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy_per_trade": expectancy,
        "reward_to_risk": (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0,
    }


def per_symbol(trades: list[Trade]) -> list[dict[str, Any]]:
    by_sym: dict[str, list[Trade]] = {}
    for t in trades:
        by_sym.setdefault(t.symbol, []).append(t)
    out = []
    for sym, ts in by_sym.items():
        wins = sum(1 for t in ts if t.net_pnl > 0)
        out.append({
            "symbol": sym,
            "trades": len(ts),
            "wins": wins,
            "win_rate": wins / len(ts) * 100,
            "net_pnl": sum(t.net_pnl for t in ts),
            "funding_pnl": sum(t.funding_collected for t in ts),
            "price_pnl": sum(t.price_pnl for t in ts),
        })
    out.sort(key=lambda x: x["net_pnl"], reverse=True)
    return out


def main() -> int:
    args = parse_args()
    client = BinanceFuturesClient()

    if args.symbols:
        symbols = parse_symbols(args.symbols)
    else:
        symbols = select_top_usdt_futures_symbols(client, args.top_usdt)
        symbols = quality_symbols(symbols, majors_only=False)

    print(f"Funding-rate squeeze backtest: {len(symbols)} symbols, {args.days} days")
    print(f"Threshold: funding <= {args.funding_threshold*100:.3f}% per 8h")
    print(f"Hold: {args.hold_hours}h | SL: {args.stop_loss_pct*100:.1f}% | TP: {args.take_profit_pct*100:.1f}% | Lev: {args.leverage}x | Margin: ${args.margin_per_trade:.0f}")

    all_trades: list[Trade] = []
    skipped: int = 0
    qualifying_events: int = 0

    for i, symbol in enumerate(symbols, 1):
        try:
            funding = fetch_funding_history(symbol, args.days, args.days_offset)
        except Exception as exc:
            print(f"  [{i}/{len(symbols)}] {symbol} funding fetch failed: {exc}")
            skipped += 1
            continue
        extreme = [e for e in funding if e.funding_rate <= args.funding_threshold]
        if not extreme:
            print(f"  [{i}/{len(symbols)}] {symbol}: no extreme events")
            continue
        try:
            candles = fetch_5m_history(client, symbol, args.days, args.candle_interval, args.days_offset)
        except Exception as exc:
            print(f"  [{i}/{len(symbols)}] {symbol} candle fetch failed: {exc}")
            skipped += 1
            continue
        if not candles:
            continue

        symbol_trades: list[Trade] = []
        for event in extreme:
            qualifying_events += 1
            trade = simulate_entry(symbol, event, candles, args, funding)
            if trade is not None:
                symbol_trades.append(trade)
        all_trades.extend(symbol_trades)
        sym_wins = sum(1 for t in symbol_trades if t.net_pnl > 0)
        sym_pnl = sum(t.net_pnl for t in symbol_trades)
        print(f"  [{i}/{len(symbols)}] {symbol}: {len(extreme)} events, {len(symbol_trades)} trades, {sym_wins}W, pnl={sym_pnl:+.2f}")

    summary = aggregate(all_trades)
    by_symbol = per_symbol(all_trades)

    print(f"\n=== FUNDING-SQUEEZE BACKTEST: {args.days}d, threshold={args.funding_threshold*100:.3f}% per 8h ===\n")
    print(f"{'symbol':<15} {'trades':>6} {'wins':>5} {'wr%':>6} {'net':>10} {'fund':>9} {'price':>10}")
    print(f"{'-'*15} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*9} {'-'*10}")
    for s in by_symbol:
        print(f"{s['symbol']:<15} {s['trades']:>6} {s['wins']:>5} {s['win_rate']:>5.1f}% {s['net_pnl']:>+10.2f} {s['funding_pnl']:>+9.2f} {s['price_pnl']:>+10.2f}")
    print(f"{'-'*15} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*9} {'-'*10}")
    print()
    print(f"Total qualifying events: {qualifying_events}")
    print(f"Total trades:            {summary.get('trades', 0)}")
    if summary.get("trades", 0) > 0:
        print(f"Win rate:                {summary['win_rate_pct']:.2f}% ({summary['wins']}W / {summary['losses']}L)")
        print(f"Avg win:                 {summary['avg_win']:+.3f}")
        print(f"Avg loss:                {summary['avg_loss']:+.3f}")
        print(f"Reward / risk:           {summary['reward_to_risk']:.2f}")
        print(f"Expectancy / trade:      {summary['expectancy_per_trade']:+.3f}")
        print(f"Total net PnL:           {summary['total_net_pnl']:+.2f}")
        print(f"  Funding contribution:  {summary['total_funding_collected']:+.2f}")
        print(f"  Price contribution:    {summary['total_price_pnl']:+.2f}")
        print(f"  Fees paid:             {summary['total_fees']:.2f}")

    out_data = {
        "config": {
            "days": args.days,
            "funding_threshold": args.funding_threshold,
            "hold_hours": args.hold_hours,
            "stop_loss_pct": args.stop_loss_pct,
            "take_profit_pct": args.take_profit_pct,
            "leverage": args.leverage,
            "margin_per_trade": args.margin_per_trade,
            "fee_bps": args.fee_bps,
        },
        "summary": summary,
        "by_symbol": by_symbol,
        "trades": [t.__dict__ for t in all_trades],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2, default=str)
    print(f"\nFull report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
