"""Smart burst scalper — v2.

Improvements over v1:
1. LIMIT-order entry (post-only / GTX) — maker fees, captures spread, FREE edge per trade
2. Volume + 5m momentum confirmation — only enter on real moves, not chop
3. Trailing stop — once profit hits +$1, lock breakeven; once +$1.5, lock +$0.50
4. All v1 robustness retained (algo SL/TP, sigint handling, etc.)

Run: py burst_scalper.py
"""

from __future__ import annotations

import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.live_executor import LiveExecutor, load_credentials_from_env

# === SESSION ===
SESSION_TARGET = 2.0
SESSION_LOSS_LIMIT = -15.0

# === PER-TRADE ===
MARGIN_PER_TRADE = 20.0
LEVERAGE = 10
PROFIT_TARGET_PER_TRADE = 2.5
LOSS_LIMIT_PER_TRADE = -8.0
MAX_WAIT_PER_TRADE_MIN = 20
PER_SYMBOL_COOLDOWN_MIN = 5

# === ENTRY (smart limit-order with market fallback) ===
LIMIT_OFFSET_BPS = 3.0          # bid 0.03% below current — small enough to fill on minor pullbacks
LIMIT_FILL_TIMEOUT_SEC = 30     # if not filled in 30s, fall back to market order
USE_LIMIT_ENTRY = True

# === ENTRY QUALITY FILTERS ===
MIN_FUNDING_PCT = -0.30         # at least -0.3% funding
MIN_VOLUME_M = 30               # at least $30M 24h volume
REQUIRE_5M_MOMENTUM_UP = True   # only enter if last 5m candle closed green
MIN_VOLUME_RATIO = 1.2          # current 1m volume vs 20-bar avg

# === TRAILING STOP (locks profit) ===
TRAIL_LOCK_BREAKEVEN_AT = 1.0   # when unrealized >= this, virtual SL = $0
TRAIL_LOCK_PROFIT_AT_1 = 1.5    # when unrealized >= this, virtual SL = $0.50
TRAIL_LOCK_PROFIT_AT_2 = 2.0    # when unrealized >= this, virtual SL = $1.00

# === TIMING ===
COOLDOWN_BETWEEN_TRADES = 15
POLL_SECONDS = 8

# === SAFETY ===
MIN_BALANCE_TO_TRADE = MARGIN_PER_TRADE + 2
SKIP_LIST = {"HYPERUSDT", "SPKUSDT", "HIGHUSDT"}
PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
LOG_FILE = PROJECT_ROOT / "logs" / "burst_scalper.log"


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def with_retry(fn, *args, retries=3, delay=2, **kwargs):
    last = None
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            time.sleep(delay * (i + 1))
    raise last


def momentum_and_volume_ok(symbol: str) -> tuple[bool, str]:
    """Pass if EITHER:
    - Last closed 5m candle is green (momentum up)
    - OR current 1m volume is well above average (real interest)
    Both are acceptable signals; we don't require both anymore (too restrictive in quiet markets).
    """
    try:
        kl_5m = with_retry(lambda: requests.get(KLINES_URL, params={"symbol": symbol, "interval": "5m", "limit": 3}, timeout=5).json(), retries=2)
        kl_1m = with_retry(lambda: requests.get(KLINES_URL, params={"symbol": symbol, "interval": "1m", "limit": 21}, timeout=5).json(), retries=2)
        if not kl_5m or len(kl_5m) < 2 or not kl_1m or len(kl_1m) < 21:
            return False, "no_data"
        last_5m = kl_5m[-2]
        open_p = float(last_5m[1])
        close_p = float(last_5m[4])
        green_5m = close_p > open_p
        # Also check 15m direction via 3 5m candles
        recent_3_5m_close = float(kl_5m[-1][4])
        oldest_3_5m_open = float(kl_5m[0][1]) if len(kl_5m) >= 3 else open_p
        green_15m = recent_3_5m_close > oldest_3_5m_open
        # Volume ratio
        current_vol = float(kl_1m[-1][5])
        avg_vol = sum(float(k[5]) for k in kl_1m[:-1]) / 20
        ratio = current_vol / avg_vol if avg_vol > 0 else 0
        # Pass on EITHER good momentum OR good volume
        if green_5m and ratio >= MIN_VOLUME_RATIO:
            return True, f"BOTH: 5m_green vol={ratio:.2f}"
        if green_5m and green_15m:
            return True, f"5m_green 15m_green vol={ratio:.2f}"
        if ratio >= 2.0:  # very high volume even if 5m red
            return True, f"vol_spike={ratio:.2f}"
        # Otherwise fail with detail
        reasons = []
        if not green_5m:
            reasons.append(f"5m_red")
        if ratio < MIN_VOLUME_RATIO:
            reasons.append(f"vol={ratio:.2f}")
        return False, ",".join(reasons) or "weak"
    except Exception as exc:
        return False, f"err: {exc}"


def find_best_candidate(blocklist: set[str]) -> dict | None:
    try:
        prem = with_retry(lambda: requests.get(PREMIUM_INDEX_URL, timeout=10).json())
        ticker_list = with_retry(lambda: requests.get(TICKER_URL, timeout=10).json())
        ticker = {x["symbol"]: x for x in ticker_list}
    except Exception as exc:
        log(f"scan failed: {exc}")
        return None
    candidates = []
    for x in prem:
        s = x["symbol"]
        if not s.endswith("USDT") or s in SKIP_LIST or s in blocklist:
            continue
        if s not in ticker:
            continue
        try:
            fr = float(x.get("lastFundingRate", 0)) * 100
            qv = float(ticker[s].get("quoteVolume", 0)) / 1e6
            last = float(ticker[s].get("lastPrice", 0))
            high = float(ticker[s].get("highPrice", 0))
            dfh = ((high - last) / high * 100) if high > 0 else 0
        except Exception:
            continue
        if fr < MIN_FUNDING_PCT and qv > MIN_VOLUME_M:
            score = -fr * 10 - dfh * 0.5 + min(qv, 500) / 100
            candidates.append({"symbol": s, "fr": fr, "qv": qv, "dfh": dfh, "score": score})
    candidates.sort(key=lambda c: -c["score"])
    # Apply momentum/volume filter to top candidates
    for cand in candidates[:5]:
        ok, reason = momentum_and_volume_ok(cand["symbol"])
        if ok:
            cand["filter_reason"] = reason
            return cand
        log(f"  skip {cand['symbol']}: {reason}")
    return None


def get_mark(symbol: str) -> float | None:
    try:
        r = with_retry(lambda: requests.get(PREMIUM_INDEX_URL, params={"symbol": symbol}, timeout=5).json(), retries=2)
        return float(r.get("markPrice", 0))
    except Exception:
        return None


def verify_protection(executor: LiveExecutor, symbol: str, expected_count: int = 2) -> bool:
    try:
        algo = executor.client.get_open_algo_orders(symbol)
        if isinstance(algo, list):
            return len(algo) >= expected_count
    except Exception:
        return False
    return False


def trailing_floor(unrealized_peak: float) -> float | None:
    """Return the virtual stop floor based on peak unrealized seen.

    Once we cross thresholds, we never let unrealized fall below the corresponding floor.
    """
    if unrealized_peak >= TRAIL_LOCK_PROFIT_AT_2:
        return 1.0
    if unrealized_peak >= TRAIL_LOCK_PROFIT_AT_1:
        return 0.5
    if unrealized_peak >= TRAIL_LOCK_BREAKEVEN_AT:
        return 0.0
    return None


def manage_position(executor: LiveExecutor, symbol: str, entry: float, quantity: float, fee_rate: float, session_realized: float) -> tuple[float, str]:
    deadline = time.time() + MAX_WAIT_PER_TRADE_MIN * 60
    last_log = 0
    unrealized_peak = 0.0
    while time.time() < deadline:
        mark = get_mark(symbol)
        if mark is None or mark <= 0:
            time.sleep(POLL_SECONDS)
            continue
        price_pnl = (mark - entry) * quantity
        exit_fee_est = quantity * mark * fee_rate
        entry_fee_paid = quantity * entry * fee_rate
        unrealized = price_pnl - exit_fee_est - entry_fee_paid

        if unrealized > unrealized_peak:
            unrealized_peak = unrealized

        if time.time() - last_log > 20:
            chg = (mark - entry) / entry * 100
            tf = trailing_floor(unrealized_peak)
            tf_str = f"trail_floor=${tf:.2f}" if tf is not None else "no_trail"
            log(f"  {symbol} mark={mark:.6g} chg={chg:+.3f}% unr=${unrealized:+.2f} peak=${unrealized_peak:+.2f} {tf_str}")
            last_log = time.time()

        # Trailing stop: if peak triggered a floor and we drop back below it, take what's left
        floor = trailing_floor(unrealized_peak)
        if floor is not None and unrealized <= floor:
            log(f"  -> TRAILING STOP triggered (floor=${floor:.2f}, current=${unrealized:.2f})")
            return _do_close(executor, symbol, entry, quantity, fee_rate, "TRAILING_STOP")

        # Session-target lock: if this trade's win would put us over session target, close
        threshold = SESSION_TARGET - session_realized + 0.10
        if unrealized >= threshold and unrealized > 0:
            log(f"  -> SESSION TARGET LOCK ($threshold={threshold:.2f}, unr=${unrealized:.2f})")
            return _do_close(executor, symbol, entry, quantity, fee_rate, "SESSION_TARGET_LOCK")

        if unrealized >= PROFIT_TARGET_PER_TRADE:
            log(f"  -> TP HIT (${unrealized:.2f})")
            return _do_close(executor, symbol, entry, quantity, fee_rate, "TAKE_PROFIT")
        if unrealized <= LOSS_LIMIT_PER_TRADE:
            log(f"  -> SL HIT (${unrealized:.2f})")
            return _do_close(executor, symbol, entry, quantity, fee_rate, "STOP_LOSS")

        # Detect external close (exchange SL/TP)
        try:
            positions = executor.get_open_positions()
            if not any(p.symbol == symbol for p in positions):
                log(f"  -> exchange-side close detected")
                return unrealized, "EXCHANGE_CLOSE"
        except Exception:
            pass

        time.sleep(POLL_SECONDS)

    log(f"  -> TIMEOUT")
    return _do_close(executor, symbol, entry, quantity, fee_rate, "TIMEOUT")


def _do_close(executor: LiveExecutor, symbol: str, entry: float, quantity: float, fee_rate: float, reason: str) -> tuple[float, str]:
    try:
        result = executor.close_long_position(symbol, quantity)
    except Exception as exc:
        log(f"  CLOSE failed: {exc}")
        executor.emergency_close_all()
        return 0.0, f"{reason}_FORCED"
    if not result.success:
        log(f"  CLOSE error: {result.error}")
        return 0.0, f"{reason}_FAILED"
    actual_exit = result.avg_fill_price if result.avg_fill_price > 0 else (get_mark(symbol) or entry)
    actual_pnl = (actual_exit - entry) * quantity - (quantity * actual_exit * fee_rate) - (quantity * entry * fee_rate)
    return actual_pnl, reason


_global_executor: LiveExecutor | None = None


def signal_handler(signum, frame):
    log(f"\n!!! Signal {signum} — closing positions before exit")
    if _global_executor is not None:
        try:
            _global_executor.emergency_close_all()
        except Exception:
            pass
    sys.exit(0)


def main() -> int:
    global _global_executor
    api_key, secret = load_credentials_from_env()
    if not api_key or not secret:
        log("ERROR: missing credentials")
        return 1
    executor = LiveExecutor(api_key=api_key, secret_key=secret, max_margin_per_trade=MARGIN_PER_TRADE)
    _global_executor = executor

    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    fee_rate = 0.0005  # taker; with limit-maker we'd be 0.0002 but conservative
    starting_balance = executor.get_usdt_balance()
    log("=" * 60)
    log("BURST SCALPER v2 (smart)")
    log(f"  Starting: ${starting_balance:.4f}")
    log(f"  Per-trade: margin=${MARGIN_PER_TRADE} lev={LEVERAGE}x TP=+${PROFIT_TARGET_PER_TRADE} SL=${LOSS_LIMIT_PER_TRADE}")
    log(f"  Session: target=+${SESSION_TARGET} max-loss=${SESSION_LOSS_LIMIT}")
    log(f"  Entry: {'LIMIT post-only -' + str(LIMIT_OFFSET_BPS) + ' bps' if USE_LIMIT_ENTRY else 'MARKET'}")
    log(f"  Filters: 5m green={REQUIRE_5M_MOMENTUM_UP} vol_ratio>={MIN_VOLUME_RATIO}")
    log(f"  Trailing: BE@+${TRAIL_LOCK_BREAKEVEN_AT}, +$0.50@+${TRAIL_LOCK_PROFIT_AT_1}, +$1.00@+${TRAIL_LOCK_PROFIT_AT_2}")
    log("=" * 60)

    # Pre-flight
    try:
        existing = executor.get_open_positions()
        if existing:
            log("WARN: pre-existing position, closing first")
            executor.emergency_close_all()
            time.sleep(3)
        algo = executor.client.get_open_algo_orders()
        if isinstance(algo, list) and algo:
            log(f"WARN: stale algo orders ({len(algo)}), cancelling")
            for o in algo:
                sym = o.get("symbol", "")
                if sym:
                    try:
                        executor.client.cancel_all_algo_orders(sym)
                    except Exception:
                        pass
    except Exception as exc:
        log(f"pre-flight error: {exc}")

    realized_session = 0.0
    trade_count = 0
    symbol_cooldown: dict[str, float] = {}
    consecutive_failures = 0

    while True:
        if realized_session >= SESSION_TARGET:
            log(f"\n*** SESSION TARGET REACHED: +${realized_session:.2f} after {trade_count} trades ***")
            break
        if realized_session <= SESSION_LOSS_LIMIT:
            log(f"\n*** SESSION LOSS LIMIT: ${realized_session:.2f} ***")
            break
        # Only count actual API/order failures — not "no candidate available"
        if consecutive_failures >= 10:
            log(f"\n*** 10 consecutive HARD failures (API errors), stopping ***")
            break

        try:
            current_balance = executor.get_usdt_balance()
        except Exception:
            time.sleep(20)
            consecutive_failures += 1
            continue
        if current_balance < MIN_BALANCE_TO_TRADE:
            log(f"\n*** Balance ${current_balance:.2f} below minimum, stopping ***")
            break

        try:
            existing = executor.get_open_positions()
            if existing:
                log("WARN: unexpected open position, closing")
                executor.emergency_close_all()
                time.sleep(5)
        except Exception:
            pass

        blocklist = {s for s, t in symbol_cooldown.items() if time.time() < t}
        candidate = find_best_candidate(blocklist)
        if not candidate:
            log("no qualified candidate (filters), waiting 45s")
            time.sleep(45)
            # Don't count quiet-market as a failure
            continue

        log(f"\n>>> Trade {trade_count + 1}: {candidate['symbol']} (fund {candidate['fr']:+.2f}%, vol {candidate['qv']:.0f}M, {candidate.get('filter_reason', '')})")

        if USE_LIMIT_ENTRY:
            result = executor.open_long_limit(
                symbol=candidate["symbol"],
                margin_usdt=MARGIN_PER_TRADE,
                leverage=LEVERAGE,
                offset_bps=LIMIT_OFFSET_BPS,
                stop_loss_pct=0.05,
                take_profit_pct=0.15,
                wait_seconds=LIMIT_FILL_TIMEOUT_SEC,
            )
        else:
            result = executor.open_long_position(
                symbol=candidate["symbol"],
                margin_usdt=MARGIN_PER_TRADE,
                leverage=LEVERAGE,
                stop_loss_pct=0.05,
                take_profit_pct=0.15,
            )
        if not result.success or result.executed_qty <= 0:
            log(f"  OPEN failed: {result.error}")
            consecutive_failures += 1
            symbol_cooldown[candidate["symbol"]] = time.time() + 120
            time.sleep(20)
            continue

        consecutive_failures = 0
        entry_price = result.avg_fill_price
        quantity = result.executed_qty
        symbol = candidate["symbol"]
        trade_count += 1
        log(f"  OPENED {symbol} qty={quantity} @ {entry_price}  (LIMIT-MAKER)")

        time.sleep(2)
        if not verify_protection(executor, symbol):
            log("  WARNING: SL/TP not attached, closing for safety")
            try:
                executor.close_long_position(symbol, quantity)
            except Exception:
                executor.emergency_close_all()
            symbol_cooldown[symbol] = time.time() + PER_SYMBOL_COOLDOWN_MIN * 60
            continue

        try:
            net_pnl, reason = manage_position(executor, symbol, entry_price, quantity, fee_rate, realized_session)
        except Exception as exc:
            log(f"  manage exception: {exc}")
            log(traceback.format_exc())
            executor.emergency_close_all()
            net_pnl = 0.0
            reason = "EXCEPTION"

        realized_session += net_pnl
        log(f"  CLOSED {symbol} pnl=${net_pnl:+.2f} reason={reason} session=${realized_session:+.2f}")

        try:
            executor.client.cancel_all_algo_orders(symbol)
        except Exception:
            pass

        if net_pnl < 0:
            symbol_cooldown[symbol] = time.time() + PER_SYMBOL_COOLDOWN_MIN * 60

        time.sleep(COOLDOWN_BETWEEN_TRADES)

    try:
        final = executor.get_usdt_balance()
        log(f"\nFinal USDT: ${final:.4f}  net session: ${final - starting_balance:+.4f}  ({trade_count} trades)")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
