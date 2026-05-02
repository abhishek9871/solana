from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.config import ROOT_DIR
from trading_bot.leveraged_session import (
    quality_symbols,
    select_top_usdt_futures_symbols,
)


PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


CHRONIC_LOSER_SYMBOLS = {
    "HYPERUSDT",
    "SPKUSDT",
    "HIGHUSDT",
}


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    margin: float
    entry_fee: float
    funding_rate_at_entry: float
    funding_collected: float = 0.0
    entry_time_ms: int = 0
    deadline_ms: int = 0
    last_funding_check_ms: int = 0


@dataclass
class FundingState:
    cash_quote: float = 50.0
    realized_pnl: float = 0.0
    positions: list[Position] = field(default_factory=list)
    daily_start_date: str = ""
    daily_start_equity: float = 50.0
    realized_today: float = 0.0
    kill_switch: bool = False
    cooldown_until_ms: int = 0
    trades_count: int = 0
    last_close_per_symbol: dict[str, int] = field(default_factory=dict)
    live_mode: bool = False


@dataclass
class FundingConfig:
    starting_quote: float = 50.0
    leverage: float = 10.0
    margin_per_trade: float = 20.0
    fee_bps: float = 5.0
    funding_threshold: float = -0.003
    hold_hours: float = 24.0
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    max_event_age_hours: float = 4.0
    poll_seconds: int = 60
    max_daily_loss: float = 30.0
    cooldown_minutes: int = 5
    target_pnl: float = 50.0
    top_usdt: int = 80
    max_positions: int = 3
    live_mode: bool = False
    per_symbol_cooldown_minutes: int = 30
    whitelist_symbols: list[str] = field(default_factory=list)
    extra_skip_symbols: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live multi-position funding-rate squeeze paper trading session.")
    parser.add_argument("--starting-quote", type=float, default=50.0)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--margin-per-trade", type=float, default=20.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--funding-threshold", type=float, default=-0.003)
    parser.add_argument("--hold-hours", type=float, default=24.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.05)
    parser.add_argument("--take-profit-pct", type=float, default=0.15)
    parser.add_argument("--max-event-age-hours", type=float, default=4.0)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-daily-loss", type=float, default=30.0)
    parser.add_argument("--cooldown-minutes", type=int, default=5)
    parser.add_argument("--target-pnl", type=float, default=50.0)
    parser.add_argument("--top-usdt", type=int, default=80)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--cycles", type=int, default=0, help="0 = infinite. Otherwise stop after N cycles.")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument("--live", action="store_true", help="Trade with real money (requires .env with API keys).")
    parser.add_argument("--per-symbol-cooldown-min", type=int, default=30, help="Per-symbol cooldown after close, in minutes.")
    parser.add_argument("--symbols", help="Comma-separated symbol whitelist. Restrict trading to these only.")
    parser.add_argument("--skip-symbols", help="Comma-separated symbol blacklist. Exclude these on top of CHRONIC_LOSER_SYMBOLS.")
    return parser.parse_args()


def state_path(live: bool = False) -> Path:
    return ROOT_DIR / "data" / ("funding_live_state.json" if live else "funding_paper_state.json")


def trade_log_path(live: bool = False) -> Path:
    return ROOT_DIR / "logs" / ("funding_live_trades.jsonl" if live else "funding_trades.jsonl")


def event_log_path(live: bool = False) -> Path:
    return ROOT_DIR / "logs" / ("funding_live_events.jsonl" if live else "funding_events.jsonl")


def migrate_v1_to_v2(payload: dict[str, Any]) -> FundingState:
    """Migrate single-position v1 state into multi-position v2 state."""
    positions: list[Position] = []
    if payload.get("position_symbol"):
        positions.append(
            Position(
                symbol=payload["position_symbol"],
                entry_price=float(payload.get("entry_price", 0.0)),
                quantity=float(payload.get("quantity", 0.0)),
                margin=float(payload.get("margin", 0.0)),
                entry_fee=float(payload.get("entry_fee", 0.0)),
                funding_rate_at_entry=float(payload.get("funding_rate_at_entry", 0.0)),
                funding_collected=float(payload.get("funding_collected", 0.0)),
                entry_time_ms=int(payload.get("entry_time_ms", 0)),
                deadline_ms=int(payload.get("deadline_ms", 0)),
                last_funding_check_ms=int(payload.get("last_funding_check_ms", 0)),
            )
        )
    return FundingState(
        cash_quote=float(payload.get("cash_quote", 50.0)),
        realized_pnl=float(payload.get("realized_pnl", 0.0)),
        positions=positions,
        daily_start_date=str(payload.get("daily_start_date", "")),
        daily_start_equity=float(payload.get("daily_start_equity", 50.0)),
        realized_today=float(payload.get("realized_today", 0.0)),
        kill_switch=bool(payload.get("kill_switch", False)),
        cooldown_until_ms=int(payload.get("cooldown_until_ms", 0)),
        trades_count=int(payload.get("trades_count", 0)),
    )


def load_state(path: Path, starting_quote: float) -> FundingState:
    if not path.exists():
        today = datetime.now(timezone.utc).date().isoformat()
        return FundingState(cash_quote=starting_quote, daily_start_date=today, daily_start_equity=starting_quote)
    payload = json.loads(path.read_text())
    if "positions" in payload:
        positions = [Position(**p) for p in payload.get("positions", [])]
        return FundingState(
            cash_quote=float(payload.get("cash_quote", 50.0)),
            realized_pnl=float(payload.get("realized_pnl", 0.0)),
            positions=positions,
            daily_start_date=str(payload.get("daily_start_date", "")),
            daily_start_equity=float(payload.get("daily_start_equity", 50.0)),
            realized_today=float(payload.get("realized_today", 0.0)),
            kill_switch=bool(payload.get("kill_switch", False)),
            cooldown_until_ms=int(payload.get("cooldown_until_ms", 0)),
            trades_count=int(payload.get("trades_count", 0)),
            last_close_per_symbol={k: int(v) for k, v in payload.get("last_close_per_symbol", {}).items()},
            live_mode=bool(payload.get("live_mode", False)),
        )
    return migrate_v1_to_v2(payload)


def save_state(path: Path, state: FundingState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cash_quote": state.cash_quote,
        "realized_pnl": state.realized_pnl,
        "positions": [asdict(p) for p in state.positions],
        "daily_start_date": state.daily_start_date,
        "daily_start_equity": state.daily_start_equity,
        "realized_today": state.realized_today,
        "kill_switch": state.kill_switch,
        "cooldown_until_ms": state.cooldown_until_ms,
        "trades_count": state.trades_count,
        "last_close_per_symbol": state.last_close_per_symbol,
        "live_mode": state.live_mode,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_last_funding_events(symbols: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            response = requests.get(FUNDING_RATE_URL, params={"symbol": symbol, "limit": 1}, timeout=10).json()
            if response and isinstance(response, list):
                row = response[0]
                out[symbol] = {
                    "symbol": symbol,
                    "funding_time_ms": int(row["fundingTime"]),
                    "funding_rate": float(row["fundingRate"]),
                    "mark_price": float(row.get("markPrice", 0) or 0),
                }
        except Exception:
            continue
    return out


def get_mark_price(symbol: str) -> float | None:
    try:
        response = requests.get(PREMIUM_INDEX_URL, params={"symbol": symbol}, timeout=8).json()
        return float(response.get("markPrice", 0))
    except Exception:
        return None


def evaluate_entry_candidates(client: BinanceFuturesClient, config: FundingConfig, exclude: set[str], state: FundingState | None = None) -> list[dict[str, Any]]:
    if config.whitelist_symbols:
        symbols = list(config.whitelist_symbols)
    else:
        symbols = select_top_usdt_futures_symbols(client, config.top_usdt)
        symbols = quality_symbols(symbols, majors_only=False)
    extra_skip = set(config.extra_skip_symbols)
    symbols = [s for s in symbols if s not in CHRONIC_LOSER_SYMBOLS and s not in exclude and s not in extra_skip]
    if state is not None and state.last_close_per_symbol:
        cutoff_per_sym = now_ms() - config.per_symbol_cooldown_minutes * 60 * 1000
        symbols = [s for s in symbols if state.last_close_per_symbol.get(s, 0) <= cutoff_per_sym]
    last_events = fetch_last_funding_events(symbols)
    cutoff_ms = now_ms() - int(config.max_event_age_hours * 3600 * 1000)
    candidates = []
    for symbol, event in last_events.items():
        if event["funding_time_ms"] < cutoff_ms:
            continue
        if event["funding_rate"] > config.funding_threshold:
            continue
        candidates.append(event)
    candidates.sort(key=lambda e: e["funding_rate"])
    return candidates


def open_position(state: FundingState, candidate: dict[str, Any], config: FundingConfig, executor=None) -> Position | None:
    fee_rate = config.fee_bps / 10_000
    target_margin = config.margin_per_trade
    margin = min(state.cash_quote * 0.95, target_margin)
    if margin < 5:
        return None
    if executor is not None:
        result = executor.open_long_position(
            symbol=candidate["symbol"],
            margin_usdt=margin,
            leverage=int(config.leverage),
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        )
        if not result.success:
            print(f"  LIVE OPEN FAILED {candidate['symbol']}: {result.error}", flush=True)
            return None
        entry_price = result.avg_fill_price if result.avg_fill_price > 0 else float(get_mark_price(candidate["symbol"]) or 0)
        quantity = result.executed_qty
        if quantity <= 0 or entry_price <= 0:
            return None
        notional = quantity * entry_price
        entry_fee = notional * fee_rate
        state.cash_quote -= margin + entry_fee
    else:
        mark = get_mark_price(candidate["symbol"])
        if mark is None or mark <= 0:
            return None
        notional = margin * config.leverage
        quantity = notional / mark
        entry_fee = notional * fee_rate
        if state.cash_quote < margin + entry_fee:
            return None
        state.cash_quote -= margin + entry_fee
        entry_price = mark
    state.trades_count += 1
    position = Position(
        symbol=candidate["symbol"],
        entry_price=entry_price,
        quantity=quantity,
        margin=margin,
        entry_fee=entry_fee,
        funding_rate_at_entry=candidate["funding_rate"],
        entry_time_ms=now_ms(),
        deadline_ms=now_ms() + int(config.hold_hours * 3600 * 1000),
        last_funding_check_ms=candidate["funding_time_ms"],
    )
    return position


def accrue_funding(position: Position) -> dict[str, Any] | None:
    try:
        response = requests.get(
            FUNDING_RATE_URL,
            params={"symbol": position.symbol, "limit": 5, "startTime": position.last_funding_check_ms + 1},
            timeout=10,
        ).json()
    except Exception:
        return None
    if not response or not isinstance(response, list):
        return None
    accrued = 0.0
    rows_processed = []
    for row in response:
        ft = int(row["fundingTime"])
        if ft <= position.last_funding_check_ms:
            continue
        if ft > now_ms():
            continue
        fr = float(row["fundingRate"])
        mark = float(row.get("markPrice", position.entry_price) or position.entry_price)
        delta = -fr * position.quantity * mark
        accrued += delta
        position.last_funding_check_ms = ft
        rows_processed.append({"funding_time_ms": ft, "funding_rate": fr, "mark_price": mark, "delta": delta})
    if accrued == 0.0 and not rows_processed:
        return None
    position.funding_collected += accrued
    return {
        "ts": now_iso(),
        "event": "FUNDING_ACCRUED",
        "symbol": position.symbol,
        "accrued": accrued,
        "events": rows_processed,
    }


def close_position(state: FundingState, position: Position, mark: float, reason: str, config: FundingConfig, executor=None) -> dict[str, Any]:
    fee_rate = config.fee_bps / 10_000
    if executor is not None:
        result = executor.close_long_position(position.symbol, position.quantity)
        if result.success and result.avg_fill_price > 0:
            mark = result.avg_fill_price
        else:
            print(f"  LIVE CLOSE WARNING {position.symbol}: {result.error or 'unknown'} — using mark price for accounting", flush=True)
    exit_notional = position.quantity * mark
    exit_fee = exit_notional * fee_rate
    price_pnl = (mark - position.entry_price) * position.quantity
    net_pnl = price_pnl + position.funding_collected - position.entry_fee - exit_fee
    state.cash_quote += position.margin + price_pnl + position.funding_collected - exit_fee
    state.realized_pnl += net_pnl
    today = datetime.now(timezone.utc).date().isoformat()
    if state.daily_start_date == today:
        state.realized_today += net_pnl
    if net_pnl < 0:
        state.cooldown_until_ms = now_ms() + config.cooldown_minutes * 60 * 1000
    state.last_close_per_symbol[position.symbol] = now_ms()
    closed = {
        "ts": now_iso(),
        "event": "CLOSE",
        "symbol": position.symbol,
        "entry_price": position.entry_price,
        "exit_price": mark,
        "quantity": position.quantity,
        "margin": position.margin,
        "exit_fee": exit_fee,
        "entry_fee": position.entry_fee,
        "price_pnl": price_pnl,
        "funding_collected": position.funding_collected,
        "net_pnl": net_pnl,
        "reason": reason,
        "hold_seconds": (now_ms() - position.entry_time_ms) / 1000,
    }
    return closed


def check_exit_for_position(state: FundingState, position: Position, config: FundingConfig, executor=None) -> dict[str, Any] | None:
    mark = get_mark_price(position.symbol)
    if mark is None or mark <= 0:
        return None
    funding_event = accrue_funding(position)
    if funding_event:
        append_jsonl(event_log_path(config.live_mode), funding_event)
    if executor is not None:
        live_positions = {p.symbol for p in executor.get_open_positions()}
        if position.symbol not in live_positions:
            return close_position(state, position, mark, "Exchange-side close (SL/TP fired)", config, executor=None)
    if mark <= position.entry_price * (1 - config.stop_loss_pct):
        return close_position(state, position, mark, "Stop loss reached", config, executor=executor)
    if mark >= position.entry_price * (1 + config.take_profit_pct):
        return close_position(state, position, mark, "Take profit reached", config, executor=executor)
    if now_ms() >= position.deadline_ms:
        return close_position(state, position, mark, "Hold expired", config, executor=executor)
    return None


def refresh_daily_limits(state: FundingState, config: FundingConfig) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if state.daily_start_date != today:
        state.daily_start_date = today
        state.daily_start_equity = state.cash_quote + sum(p.margin for p in state.positions)
        state.realized_today = 0.0
        state.kill_switch = False
    if state.realized_today <= -config.max_daily_loss:
        state.kill_switch = True
    if state.realized_pnl >= config.target_pnl:
        state.kill_switch = True


def position_summary(position: Position, mark: float | None, fee_rate: float) -> str:
    if mark is None:
        return f"{position.symbol} entry={position.entry_price:.6g} (mark unavailable)"
    chg_pct = (mark - position.entry_price) / position.entry_price * 100
    unrealized_price = (mark - position.entry_price) * position.quantity
    exit_fee = position.quantity * mark * fee_rate
    net_unrealized = unrealized_price + position.funding_collected - position.entry_fee - exit_fee
    time_left = max(0, (position.deadline_ms - now_ms()) / 60_000)
    return (
        f"{position.symbol} entry={position.entry_price:.6g} mark={mark:.6g} "
        f"chg={chg_pct:+.2f}% px_pnl={unrealized_price:+.2f} "
        f"funding={position.funding_collected:+.2f} net={net_unrealized:+.2f} "
        f"left={time_left:.0f}m"
    )


def run_cycle(client: BinanceFuturesClient, state: FundingState, config: FundingConfig, executor=None) -> None:
    refresh_daily_limits(state, config)
    fee_rate = config.fee_bps / 10_000

    closed_indices: list[int] = []
    for i, position in enumerate(state.positions):
        result = check_exit_for_position(state, position, config, executor=executor)
        if result:
            append_jsonl(trade_log_path(config.live_mode), result)
            print(
                f"CLOSE LONG {result['symbol']} pnl={result['net_pnl']:+.2f} "
                f"reason={result['reason']} cash={state.cash_quote:.2f} "
                f"realized_total={state.realized_pnl:+.2f}",
                flush=True,
            )
            closed_indices.append(i)

    for i in reversed(closed_indices):
        state.positions.pop(i)

    refresh_daily_limits(state, config)

    for position in state.positions:
        mark = get_mark_price(position.symbol)
        print(f"  HOLD {position_summary(position, mark, fee_rate)}", flush=True)

    if state.kill_switch:
        print(f"KILL_SWITCH active. realized={state.realized_pnl:+.2f}", flush=True)
        return

    if now_ms() < state.cooldown_until_ms:
        remaining = (state.cooldown_until_ms - now_ms()) / 1000
        print(f"  COOLDOWN {remaining:.0f}s, no new entries", flush=True)
        return

    if len(state.positions) >= config.max_positions:
        return

    held_symbols = {p.symbol for p in state.positions}
    candidates = evaluate_entry_candidates(client, config, exclude=held_symbols, state=state)
    if not candidates:
        if not state.positions:
            print(f"NO_SETUP cash={state.cash_quote:.2f} realized={state.realized_pnl:+.2f}", flush=True)
        return

    for candidate in candidates:
        if len(state.positions) >= config.max_positions:
            break
        if state.cash_quote < config.margin_per_trade + 5:
            print(f"  Insufficient cash for new position. cash={state.cash_quote:.2f}", flush=True)
            break
        position = open_position(state, candidate, config, executor=executor)
        if position:
            state.positions.append(position)
            payload = {
                "ts": now_iso(),
                "event": "OPEN",
                "symbol": position.symbol,
                "entry_price": position.entry_price,
                "margin": position.margin,
                "notional": position.margin * config.leverage,
                "quantity": position.quantity,
                "funding_rate_at_entry": position.funding_rate_at_entry,
                "deadline_ms": position.deadline_ms,
                "entry_fee": position.entry_fee,
            }
            append_jsonl(trade_log_path(config.live_mode), payload)
            print(
                f"OPEN LONG {position.symbol} entry={position.entry_price:.8g} "
                f"funding_rate={candidate['funding_rate']*100:+.3f}% margin={position.margin:.2f} "
                f"notional={position.margin*config.leverage:.2f} quantity={position.quantity:.6g}",
                flush=True,
            )


def config_from_args(args: argparse.Namespace) -> FundingConfig:
    return FundingConfig(
        starting_quote=args.starting_quote,
        leverage=args.leverage,
        margin_per_trade=args.margin_per_trade,
        fee_bps=args.fee_bps,
        funding_threshold=args.funding_threshold,
        hold_hours=args.hold_hours,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        max_event_age_hours=args.max_event_age_hours,
        poll_seconds=max(args.poll_seconds, 5),
        max_daily_loss=args.max_daily_loss,
        cooldown_minutes=args.cooldown_minutes,
        target_pnl=args.target_pnl,
        top_usdt=args.top_usdt,
        max_positions=max(1, args.max_positions),
        live_mode=getattr(args, "live", False),
        per_symbol_cooldown_minutes=getattr(args, "per_symbol_cooldown_min", 30),
        whitelist_symbols=[s.strip().upper() for s in (getattr(args, "symbols", None) or "").split(",") if s.strip()],
        extra_skip_symbols=[s.strip().upper() for s in (getattr(args, "skip_symbols", None) or "").split(",") if s.strip()],
    )


def main() -> int:
    args = parse_args()
    config = config_from_args(args)
    path = state_path(config.live_mode)
    if args.reset and path.exists():
        path.unlink()
    state = load_state(path, config.starting_quote)
    state.live_mode = config.live_mode
    client = BinanceFuturesClient()

    executor = None
    if config.live_mode:
        from trading_bot.live_executor import LiveExecutor, load_credentials_from_env
        api_key, secret_key = load_credentials_from_env()
        if not api_key or not secret_key:
            print("LIVE MODE FAILED: missing BINANCE_API_KEY or BINANCE_SECRET_KEY in .env", flush=True)
            return 2
        executor = LiveExecutor(api_key=api_key, secret_key=secret_key, max_margin_per_trade=config.margin_per_trade)
        health = executor.health_check()
        if "error" in health:
            print(f"LIVE MODE HEALTH CHECK FAILED: {health['error']}", flush=True)
            return 3
        if not health.get("can_trade"):
            print("LIVE MODE BLOCKED: API key lacks trading permission.", flush=True)
            return 4
        print("=" * 60, flush=True)
        print("!!! LIVE TRADING MODE — REAL MONEY AT RISK !!!", flush=True)
        print(f"   USDT balance:    {health['usdt_balance']:.4f}", flush=True)
        print(f"   Existing positions: {health['open_positions_count']}", flush=True)
        print(f"   Withdrawals enabled: {health['can_withdraw_warning']}", flush=True)
        print("=" * 60, flush=True)
        state.cash_quote = health["usdt_balance"]
        live_positions = executor.get_open_positions()
        if live_positions:
            print(f"  Reconciling {len(live_positions)} existing exchange position(s)...", flush=True)
            existing_symbols = {p.symbol for p in state.positions}
            for lp in live_positions:
                if lp.symbol in existing_symbols:
                    continue
                if lp.side != "LONG":
                    print(f"   SKIPPING non-LONG exchange position {lp.symbol} {lp.side}", flush=True)
                    continue
                state.positions.append(
                    Position(
                        symbol=lp.symbol,
                        entry_price=lp.entry_price,
                        quantity=lp.quantity,
                        margin=lp.margin,
                        entry_fee=lp.entry_price * lp.quantity * (config.fee_bps / 10_000),
                        funding_rate_at_entry=0.0,
                        entry_time_ms=now_ms(),
                        deadline_ms=now_ms() + int(config.hold_hours * 3600 * 1000),
                        last_funding_check_ms=now_ms(),
                    )
                )
                print(f"   Adopted {lp.symbol} qty={lp.quantity} entry={lp.entry_price}", flush=True)
        live_symbols = {p.symbol for p in live_positions}
        state.positions = [p for p in state.positions if p.symbol in live_symbols] if live_positions else []
    else:
        print(
            f"FUNDING-RATE PAPER SESSION (multi-position v2)",
            flush=True,
        )
    print(
        f"  start={config.starting_quote:.2f}  lev={config.leverage}x  "
        f"max_positions={config.max_positions}  margin/trade={config.margin_per_trade:.2f}",
        flush=True,
    )
    print(
        f"  threshold={config.funding_threshold*100:+.3f}%  hold={config.hold_hours}h  "
        f"SL={config.stop_loss_pct*100:.1f}%  TP={config.take_profit_pct*100:.1f}%  "
        f"target_pnl={config.target_pnl} max_daily_loss={config.max_daily_loss}",
        flush=True,
    )
    if state.positions:
        print(f"  Resuming with {len(state.positions)} open position(s):", flush=True)
        for p in state.positions:
            print(f"    - {p.symbol} entry={p.entry_price:.6g} margin={p.margin:.2f}", flush=True)

    if args.once:
        run_cycle(client, state, config, executor=executor)
        save_state(path, state)
        return 0

    cycle = 0
    max_cycles = args.cycles if args.cycles > 0 else 10**9
    while cycle < max_cycles:
        cycle += 1
        ts = now_iso()
        mode_tag = "LIVE" if config.live_mode else "PAPER"
        print(f"[CYCLE {cycle} {mode_tag}] {ts} cash={state.cash_quote:.2f} positions={len(state.positions)} realized={state.realized_pnl:+.2f}", flush=True)
        try:
            run_cycle(client, state, config, executor=executor)
        except Exception as exc:
            print(f"CYCLE_ERROR {exc}", flush=True)
        sys.stdout.flush()
        save_state(path, state)
        if state.kill_switch and not state.positions:
            print(f"KILL_SWITCH terminated session. realized={state.realized_pnl:+.2f}", flush=True)
            break
        time.sleep(config.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
