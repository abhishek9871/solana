"""Real-time scalper v9.0 — research-grounded multi-regime.

Major changes informed by deep research:

1. LIQUIDATION TREND-FOLLOW (not fade). Research: cascades usually accelerate
   the existing trend, not reverse it. We fade only the rarer "exhaustion at
   resistance" case (handled by FADE_SPIKE), and we now FOLLOW cascades that
   occur in the symbol's prevailing direction.

2. WHALE-WEIGHTED CVD. Pro systems tier trades:
     - $5K+ "informed" trade
     - $20K+ "whale" trade
     - $100K+ "institutional" trade
   Our CVD now weights by tier, so a $50K print signals more than 50 $1K
   retail prints. Also expose whale-only CVD as a separate flow indicator.

3. PER-SYMBOL REGIME. Each symbol has its own 60s velocity. Strategy gates
   on the symbol's regime, not BTC's:
     - TRENDING (|v60| >= 0.30%): LIQ_FOLLOW + MOMENTUM-with-trend allowed,
       FADE_SPIKE blocked
     - CHOPPY: FADE_SPIKE+CVD enabled (proven winner in chop)

4. KEEP THE PROVEN: event-driven confirmation, asymmetric exits, hard floor,
   BIG_WIN_LOCK, SCALP_OUT, single-position, per-symbol consecutive-loss ban.

Run live:    py momentum_scalper.py
Run dry:     py momentum_scalper.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.live_executor import LiveExecutor, load_credentials_from_env

# === DYNAMIC UNIVERSE (full ocean) ===
MIN_QV_USD = 5_000_000         # any reasonably liquid pair
MIN_24H_PC = 1.5               # any pair that's actually moving
MAX_SYMBOLS = 200              # effectively all liquid USDT futures
EXCLUDE = {"HYPERUSDT", "SPKUSDT", "HIGHUSDT"}

# === BUFFERS ===
TICK_BUFFER_SEC = 90
TRADE_BUFFER_SEC = 60
LIQ_BUFFER_SEC = 30
MIN_TICKS = 8
MIN_TRADES = 3

# === WHALE TIERS (USD) ===
TIER_INFORMED_USD = 5_000        # 5x weight
TIER_WHALE_USD = 20_000          # 15x weight
TIER_INSTITUTIONAL_USD = 100_000 # 50x weight

# === REGIME DETECTION (per-symbol) ===
REGIME_WINDOW_SEC = 60
REGIME_TREND_PCT = 0.30          # |v60| >= this -> TRENDING

# === FADE_SPIKE SIGNAL — permissive ===
SPIKE_MIN_V1 = 0.15              # was 0.25 — small spikes too
SPIKE_RATIO = 0.40               # was 0.55 — looser ratio
SPIKE_NO_TREND_RATIO = 2.00      # was 1.30 — basically disabled
FADE_CVD_MIN = 0.20              # was 0.35 — lots of opposing flow patterns
FADE_WHALE_CVD_MIN = 0.20

# === LIQ_FOLLOW SIGNAL — any liquidation triggers ===
LIQ_FOLLOW_WINDOW_SEC = 30       # wider window
LIQ_FOLLOW_MIN_COUNT = 1
LIQ_FOLLOW_MIN_USD = 5_000       # was 20K — any meaningful liq
LIQ_FOLLOW_MAX_AGO_SEC = 10
LIQ_FOLLOW_REQUIRE_TREND_AGREE = False  # don't require trend agreement

# === ENTRY CONFIRMATION (event-driven) ===
ENTRY_WATCH_MIN_SEC = 0.3
ENTRY_WATCH_MAX_SEC = 4.0
MIN_CONFIRM_FAVORABLE_PCT = 0.10
CONFIRM_V1_THRESHOLD = 0.05
CONFIRM_CVD_THRESHOLD = 0.30
# Auto-reverse: when wait shows price moved strongly OPPOSITE to original signal
# AND v1 + cvd both confirm opposite direction, fire opposite-side entry. The
# 3+ seconds of evidence makes the opposite signal more validated than typical.
REVERSE_FAV_THRESHOLD = -0.20      # fav <= this -> consider reverse
REVERSE_V1_THRESHOLD = 0.04        # opposite v1 must be at least this magnitude
REVERSE_CVD_THRESHOLD = 0.30       # opposite cvd must be at least this magnitude
FORCE_MIN_V5 = 0.03
FORCE_MIN_CVD = 0.10

# === EXIT (asymmetric) ===
LOSS_FLIP_MIN_3S = 0.18
LOSS_DRIFT_15S = 0.20
PROFIT_FLIP_MIN_3S = 0.40
PROFIT_DRIFT_15S = 0.50
PROFIT_FLOOR = 0.60
STRONG_FLIP_MIN_3S = 0.40
DIV_GRACE_SEC = 8                 # was 30 — react to adverse drift fast
V3_GRACE_SEC = 2                  # was 8 — react to v3 flip in 2s
HARD_MAX_LOSS_FRAC = 0.055
BIG_WIN_LOCK_USD = 2.50
BIG_WIN_LOCK_FLIP_3S = 0.20
BIG_WIN_LOCK_GIVEBACK = 0.40
SCALP_OUT_ARM = 0.30
SCALP_OUT_FLOOR = 0.10

# === ADAPTIVE TRAIL ===
TRAIL_ARM_R = 0.4
TRAIL_50_R = 1.0
TRAIL_70_R = 2.0

# === TRADE PARAMS ===
MARGIN_BASE = 6.0              # smaller per trade to fit 3 concurrent
LEVERAGE = 10
SL_PCT = 0.010
TP_PCT = 0.030
MAX_HOLD_SECONDS = 45             # was 180 — stop holding stale trades
STALE_HOLD_SECONDS = 5            # was 30 — exit flat trades in 5s
MAX_CONCURRENT_POSITIONS = 3

# === SESSION ===
SESSION_TARGET = 5.0
SESSION_LOSS_LIMIT = -3.0
COOLDOWN_AFTER_WIN = 4
COOLDOWN_AFTER_LOSS = 8
SYMBOL_LOSS_COOLDOWN_SEC = 30
CONSECUTIVE_LOSS_BAN = 2

LOG_FILE = PROJECT_ROOT / "logs" / "momentum_scalper.log"


def now_ms() -> int:
    return int(time.time() * 1000)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    safe_line = line.encode("ascii", errors="replace").decode("ascii")
    print(safe_line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def trade_weight(usd: float) -> float:
    """Tier-based weight for whale-weighted CVD."""
    if usd >= TIER_INSTITUTIONAL_USD:
        return 50.0
    if usd >= TIER_WHALE_USD:
        return 15.0
    if usd >= TIER_INFORMED_USD:
        return 5.0
    return 1.0


def select_volatile_universe() -> list[str]:
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10)
        data = r.json()
    except Exception as exc:
        log(f"universe fetch failed: {exc}")
        return ["DAMUSDT", "PRLUSDT", "ZKJUSDT", "AIOTUSDT", "ZBTUSDT", "SWARMSUSDT"]
    candidates = []
    for t in data:
        try:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in EXCLUDE:
                continue
            qv = float(t.get("quoteVolume", 0))
            pc = abs(float(t.get("priceChangePercent", 0)))
            if qv < MIN_QV_USD or pc < MIN_24H_PC:
                continue
            candidates.append((sym, pc, qv))
        except Exception:
            continue
    candidates.sort(key=lambda x: -x[1])
    return [c[0] for c in candidates[:MAX_SYMBOLS]]


@dataclass
class Tick:
    ts_ms: int
    mid: float
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass
class Trade:
    ts_ms: int
    price: float
    qty: float
    is_aggressive_buy: bool

    @property
    def usd(self) -> float:
        return self.price * self.qty


@dataclass
class Liquidation:
    ts_ms: int
    side: str           # "BUY" = short liquidated; "SELL" = long liquidated
    qty: float
    price: float

    @property
    def usd(self) -> float:
        return self.qty * self.price


@dataclass
class SymbolState:
    symbol: str
    ticks: deque = field(default_factory=lambda: deque(maxlen=4000))
    trades: deque = field(default_factory=lambda: deque(maxlen=4000))
    liquidations: deque = field(default_factory=lambda: deque(maxlen=200))

    def add_tick(self, t: Tick) -> None:
        self.ticks.append(t)
        cutoff = t.ts_ms - TICK_BUFFER_SEC * 1000
        while self.ticks and self.ticks[0].ts_ms < cutoff:
            self.ticks.popleft()

    def add_trade(self, tr: Trade) -> None:
        self.trades.append(tr)
        cutoff = tr.ts_ms - TRADE_BUFFER_SEC * 1000
        while self.trades and self.trades[0].ts_ms < cutoff:
            self.trades.popleft()

    def add_liquidation(self, liq: Liquidation) -> None:
        self.liquidations.append(liq)
        cutoff = liq.ts_ms - LIQ_BUFFER_SEC * 1000
        while self.liquidations and self.liquidations[0].ts_ms < cutoff:
            self.liquidations.popleft()

    def velocity_pct(self, window_sec: float) -> float | None:
        if not self.ticks:
            return None
        latest = self.ticks[-1]
        target_ts = latest.ts_ms - int(window_sec * 1000)
        ref = None
        for t in self.ticks:
            if t.ts_ms >= target_ts:
                ref = t
                break
        if ref is None or ref.ts_ms == latest.ts_ms or ref.mid <= 0:
            return None
        return (latest.mid - ref.mid) / ref.mid * 100

    def regime(self) -> str:
        """TRENDING_UP, TRENDING_DOWN, or CHOP based on 60s slope."""
        v60 = self.velocity_pct(REGIME_WINDOW_SEC)
        if v60 is None:
            return "UNKNOWN"
        if v60 >= REGIME_TREND_PCT:
            return "TRENDING_UP"
        if v60 <= -REGIME_TREND_PCT:
            return "TRENDING_DOWN"
        return "CHOP"

    def book_imbalance(self, window_sec: float = 3) -> float:
        if not self.ticks:
            return 0.0
        latest_ts = self.ticks[-1].ts_ms
        cutoff = latest_ts - int(window_sec * 1000)
        vals = []
        for t in self.ticks:
            if t.ts_ms < cutoff:
                continue
            tot = t.bid_qty + t.ask_qty
            if tot > 0:
                vals.append((t.bid_qty - t.ask_qty) / tot)
        return sum(vals) / len(vals) if vals else 0.0

    def cvd_signal(self, window_sec: float, whale_only: bool = False, weighted: bool = True) -> tuple[float, float]:
        """Whale-weighted CVD over window. Returns (ratio, total_weighted_usd).

        weighted=True: each trade contributes usd * weight(usd) to the sum.
        whale_only=True: only count trades >= TIER_INFORMED_USD.
        """
        if not self.trades:
            return 0.0, 0.0
        latest_ts = self.trades[-1].ts_ms
        cutoff = latest_ts - int(window_sec * 1000)
        buy = 0.0
        sell = 0.0
        for tr in self.trades:
            if tr.ts_ms < cutoff:
                continue
            usd = tr.usd
            if whale_only and usd < TIER_INFORMED_USD:
                continue
            w = trade_weight(usd) if weighted else 1.0
            contribution = usd * w
            if tr.is_aggressive_buy:
                buy += contribution
            else:
                sell += contribution
        total = buy + sell
        if total <= 0:
            return 0.0, 0.0
        return (buy - sell) / total, total

    def liquidation_cluster(self) -> tuple[str, dict]:
        """Detect recent liquidation cluster and its dominant side.

        Returns ("BUY"|"SELL"|"NONE", info_dict)
        BUY = short positions liquidated (forced buying = upward pressure)
        SELL = long positions liquidated (forced selling = downward pressure)
        """
        if not self.liquidations:
            return "NONE", {}
        now_t = now_ms()
        cutoff = now_t - LIQ_FOLLOW_WINDOW_SEC * 1000
        buys = [l for l in self.liquidations if l.ts_ms >= cutoff and l.side == "BUY"]
        sells = [l for l in self.liquidations if l.ts_ms >= cutoff and l.side == "SELL"]
        buy_usd = sum(l.usd for l in buys)
        sell_usd = sum(l.usd for l in sells)
        last_buy_ago = (now_t - buys[-1].ts_ms) / 1000 if buys else 1e9
        last_sell_ago = (now_t - sells[-1].ts_ms) / 1000 if sells else 1e9
        info = {
            "buy_count": len(buys), "sell_count": len(sells),
            "buy_usd": buy_usd, "sell_usd": sell_usd,
            "last_buy_ago": last_buy_ago, "last_sell_ago": last_sell_ago,
        }
        # Dominant side qualifies if count + usd thresholds met AND recent
        if (len(buys) >= LIQ_FOLLOW_MIN_COUNT
                and buy_usd >= LIQ_FOLLOW_MIN_USD
                and last_buy_ago <= LIQ_FOLLOW_MAX_AGO_SEC
                and buy_usd > sell_usd * 1.5):
            return "BUY", info
        if (len(sells) >= LIQ_FOLLOW_MIN_COUNT
                and sell_usd >= LIQ_FOLLOW_MIN_USD
                and last_sell_ago <= LIQ_FOLLOW_MAX_AGO_SEC
                and sell_usd > buy_usd * 1.5):
            return "SELL", info
        return "NONE", info

    def detect_signal(self) -> tuple[str, str, float, str] | None:
        """Returns (strategy, side, score, reason) or None.

        Strategy chosen by symbol's regime:
        - TRENDING_UP/DOWN: LIQ_FOLLOW (ride cascade in trend direction)
        - CHOP: FADE_SPIKE+CVD (fade exhaustion spikes)
        """
        if len(self.ticks) < MIN_TICKS or len(self.trades) < MIN_TRADES:
            return None
        regime = self.regime()
        v1 = self.velocity_pct(1)
        v5 = self.velocity_pct(5)
        v15 = self.velocity_pct(15)
        if v1 is None or v5 is None or v15 is None:
            return None

        # --- LIQ_FOLLOW: trending regime, cascade in trend direction ---
        if regime in ("TRENDING_UP", "TRENDING_DOWN"):
            cluster_side, info = self.liquidation_cluster()
            if cluster_side != "NONE":
                # BUY liquidations = upward pressure; SELL liquidations = downward
                pressure_dir = "LONG" if cluster_side == "BUY" else "SHORT"
                trend_dir = "LONG" if regime == "TRENDING_UP" else "SHORT"
                if (not LIQ_FOLLOW_REQUIRE_TREND_AGREE) or pressure_dir == trend_dir:
                    score = 2.5
                    cluster_usd = info["buy_usd"] if cluster_side == "BUY" else info["sell_usd"]
                    if cluster_usd >= 50_000:
                        score += 0.5
                    if cluster_usd >= 200_000:
                        score += 0.5
                    cluster_count = info["buy_count"] if cluster_side == "BUY" else info["sell_count"]
                    if cluster_count >= 5:
                        score += 0.3
                    reason = (f"LIQ_FOLLOW {trend_dir} cluster_{cluster_side}=${cluster_usd:.0f} "
                              f"x{cluster_count} regime={regime}")
                    return ("LIQ_FOLLOW", pressure_dir, score, reason)

        # --- FADE_SPIKE+CVD: chop regime only, exhaustion spike ---
        if regime == "CHOP":
            abs_v1 = abs(v1)
            abs_v5 = abs(v5)
            abs_v15 = abs(v15)
            if abs_v1 < SPIKE_MIN_V1:
                return None
            if abs_v1 < abs_v5 * SPIKE_RATIO:
                return None
            if abs_v15 > abs_v5 * SPIKE_NO_TREND_RATIO:
                return None
            spike_up = v1 > 0
            fade_dir = "SHORT" if spike_up else "LONG"
            cvd_3s, _ = self.cvd_signal(3, weighted=True)
            whale_cvd, whale_total = self.cvd_signal(5, whale_only=True, weighted=True)
            cvd_opposes = (
                (fade_dir == "LONG" and cvd_3s >= FADE_CVD_MIN)
                or (fade_dir == "SHORT" and cvd_3s <= -FADE_CVD_MIN)
            )
            if not cvd_opposes:
                return None
            # Whale CVD should at least not oppose the fade
            whale_ok = True
            if whale_total > 0:
                if fade_dir == "LONG" and whale_cvd < -0.20:
                    whale_ok = False
                if fade_dir == "SHORT" and whale_cvd > 0.20:
                    whale_ok = False
            if not whale_ok:
                return None
            imb = self.book_imbalance(3)
            score = 2.0
            if abs(cvd_3s) > 0.70:
                score += 0.4
            if whale_total > 0 and abs(whale_cvd) >= FADE_WHALE_CVD_MIN:
                if (fade_dir == "LONG" and whale_cvd > 0) or (fade_dir == "SHORT" and whale_cvd < 0):
                    score += 0.5  # whales agree with fade
            if abs_v1 > 0.50:
                score += 0.3
            if (fade_dir == "LONG" and imb > 0.15) or (fade_dir == "SHORT" and imb < -0.15):
                score += 0.3
            reason = (f"FADE v1={v1:+.2f} cvd3={cvd_3s:+.2f} "
                      f"whale_cvd={whale_cvd:+.2f}(${whale_total:.0f}) imb={imb:+.2f}")
            return ("FADE_SPIKE", fade_dir, score, reason)

        return None

    def detect_flip(self, position_side: str, unrealized: float,
                    skip_drift: bool = False, skip_v3_loss: bool = False) -> tuple[str, str]:
        if len(self.ticks) < 5:
            return "NONE", ""
        v3 = self.velocity_pct(3)
        v15 = self.velocity_pct(15)
        if v3 is None:
            return "NONE", ""
        in_profit = unrealized > PROFIT_FLOOR
        if in_profit:
            mild_thr = PROFIT_FLIP_MIN_3S
            drift_thr = PROFIT_DRIFT_15S
        else:
            mild_thr = LOSS_FLIP_MIN_3S
            drift_thr = LOSS_DRIFT_15S
        strong_thr = STRONG_FLIP_MIN_3S
        if position_side == "LONG":
            if v3 < -strong_thr:
                return "REVERSE", f"v3={v3:+.2f}% strongDOWN"
            if v3 < -mild_thr and not skip_v3_loss:
                tag = "win-trail" if in_profit else "loss-cut"
                return "EXIT", f"v3={v3:+.2f}% {tag}"
            if not skip_drift and v15 is not None and v15 < -drift_thr:
                tag = "win-trail" if in_profit else "loss-cut"
                return "EXIT", f"v15={v15:+.2f}% drift-{tag}"
        else:
            if v3 > strong_thr:
                return "REVERSE", f"v3={v3:+.2f}% strongUP"
            if v3 > mild_thr and not skip_v3_loss:
                tag = "win-trail" if in_profit else "loss-cut"
                return "EXIT", f"v3={v3:+.2f}% {tag}"
            if not skip_drift and v15 is not None and v15 > drift_thr:
                tag = "win-trail" if in_profit else "loss-cut"
                return "EXIT", f"v15={v15:+.2f}% drift-{tag}"
        return "NONE", ""


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_ts_ms: int
    notional: float
    margin: float
    strategy: str = "FADE_SPIKE"
    peak_unrealized: float = 0.0
    moved_to_breakeven: bool = False


@dataclass
class PendingEntry:
    symbol: str
    side: str
    strategy: str
    score: float
    reason: str
    signal_ts_ms: int
    signal_price: float


class MomentumScalper:
    def __init__(
        self,
        executor: LiveExecutor | None,
        dry_run: bool,
        watch_symbols: list[str],
        force_after_sec: float = 0.0,
    ):
        self.executor = executor
        self.dry_run = dry_run
        self.watch_symbols = watch_symbols
        self.force_after_sec = force_after_sec
        self.start_ts = time.time()
        self.last_force_ts = 0.0
        self.states: dict[str, SymbolState] = {s: SymbolState(s) for s in watch_symbols}
        self.lock = threading.Lock()
        self.positions: dict[str, Position] = {}  # multi-position
        self.session_realized = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.last_close_time = 0.0
        self.last_close_was_win = False
        self.stop_flag = False
        self.symbol_loss_until: dict[str, float] = {}
        self.symbol_consecutive_losses: dict[str, int] = {}
        self.symbol_session_banned: set[str] = set()
        self.pending_entries: dict[str, PendingEntry] = {}
        self._last_heartbeat = 0.0
        self._last_exchange_check = 0.0

    def on_tick(self, sym: str, tick: Tick) -> None:
        if sym not in self.states:
            return
        st = self.states[sym]
        st.add_tick(tick)
        if time.time() - self._last_heartbeat > 20:
            self._last_heartbeat = time.time()
            self._emit_heartbeat()

        # Manage existing position for this symbol (if any)
        if sym in self.positions:
            self._check_exit(self.positions[sym], tick)
            return

        # Entry check
        if self.stop_flag:
            return
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            return
        cd = COOLDOWN_AFTER_WIN if self.last_close_was_win else COOLDOWN_AFTER_LOSS
        if time.time() - self.last_close_time < cd:
            return
        if sym in self.symbol_session_banned:
            return
        if time.time() < self.symbol_loss_until.get(sym, 0):
            return
        sig = st.detect_signal()
        if sig:
            strategy, side, score, reason = sig
            # Both directions enabled (locked v9.7 config)
            with self.lock:
                if sym in self.positions:
                    return
                if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
                    return
                log(f"SIGNAL {sym} {side} [{strategy}] score={score:.2f} pos={len(self.positions)+1}/{MAX_CONCURRENT_POSITIONS} {reason}")
                self._execute_entry(sym, side, tick, strategy)
        elif self.force_after_sec > 0 and time.time() - self.start_ts >= self.force_after_sec:
            if time.time() - self.last_force_ts < max(COOLDOWN_AFTER_LOSS, 8):
                return
            v5 = st.velocity_pct(5)
            cvd_5s, _ = st.cvd_signal(5, weighted=True)
            if v5 is None or abs(v5) < FORCE_MIN_V5 or abs(cvd_5s) < FORCE_MIN_CVD:
                return
            if (v5 > 0 and cvd_5s < 0) or (v5 < 0 and cvd_5s > 0):
                return
            side = "LONG" if v5 > 0 else "SHORT"
            with self.lock:
                if sym in self.positions or len(self.positions) >= MAX_CONCURRENT_POSITIONS:
                    return
                self.last_force_ts = time.time()
                log(f"FORCE_SIGNAL {sym} {side} [FORCE_MOMENTUM] v5={v5:+.2f}% cvd5={cvd_5s:+.2f}")
                self._execute_entry(sym, side, tick, "FORCE_MOMENTUM")

    def _emit_heartbeat(self) -> None:
        rows = []
        for s, state in self.states.items():
            v5 = state.velocity_pct(5)
            cvd_5s, _ = state.cvd_signal(5, weighted=True)
            whale_cvd, whale_usd = state.cvd_signal(5, whale_only=True, weighted=True)
            reg = state.regime()
            sig = state.detect_signal()
            if v5 is None:
                continue
            tag = ""
            if sig:
                tag = f" >>{sig[1]}({sig[2]:.1f})[{sig[0]}]"
            elif reg in ("TRENDING_UP", "TRENDING_DOWN"):
                tag = f" [{reg}]"
            whale_tag = f" wcvd={whale_cvd:+.2f}" if whale_usd > 0 else ""
            rows.append((abs(v5), s, v5, cvd_5s, whale_tag, tag))
        rows.sort(key=lambda x: -x[0])
        parts = []
        for _, s, v5, cvd, wcvd, tag in rows[:5]:
            parts.append(f"{s.replace('USDT','')}: v5={v5:+.2f} cvd5={cvd:+.2f}{wcvd}{tag}")
        mode = "DRY" if self.dry_run else "LIVE"
        open_str = ",".join(f"{s}({p.side[0]})" for s, p in self.positions.items()) or "-"
        log(f"[hb {mode} pnl=${self.session_realized:+.2f} trades={self.trade_count} wins={self.win_count} open={open_str}] " + " | ".join(parts))

    def _execute_entry(self, symbol: str, side: str, tick: Tick, strategy: str) -> None:
        if self.dry_run:
            qty = round(MARGIN_BASE * LEVERAGE / tick.mid, 4)
            self.positions[symbol] = Position(
                symbol=symbol, side=side, qty=qty,
                entry_price=tick.mid, entry_ts_ms=tick.ts_ms,
                notional=qty * tick.mid, margin=MARGIN_BASE,
                strategy=strategy,
            )
            self.trade_count += 1
            log(f"  [DRY] OPENED {side} {symbol} [{strategy}] qty={qty} @ {tick.mid}")
            return
        try:
            if side == "LONG":
                result = self.executor.open_long_position(symbol, MARGIN_BASE, LEVERAGE, SL_PCT, TP_PCT)
            else:
                result = self.executor.open_short_position(symbol, MARGIN_BASE, LEVERAGE, SL_PCT, TP_PCT)
        except Exception as exc:
            log(f"  open exception: {exc}")
            return
        if not result.success or result.executed_qty <= 0:
            log(f"  OPEN FAILED: {result.error}")
            return
        self.positions[symbol] = Position(
            symbol=symbol, side=side, qty=result.executed_qty,
            entry_price=result.avg_fill_price, entry_ts_ms=now_ms(),
            notional=result.executed_qty * result.avg_fill_price,
            margin=MARGIN_BASE,
            strategy=strategy,
        )
        self.trade_count += 1
        log(f"  OPENED {side} {symbol} [{strategy}] qty={result.executed_qty} @ {result.avg_fill_price}")

    def _unrealized(self, p: Position, current_price: float) -> float:
        if p.side == "LONG":
            price_pnl = (current_price - p.entry_price) * p.qty
        else:
            price_pnl = (p.entry_price - current_price) * p.qty
        fees = (p.qty * current_price + p.qty * p.entry_price) * 0.0005
        return price_pnl - fees

    def _check_exit(self, p: Position, tick: Tick) -> None:
        st = self.states[p.symbol]
        unrealized = self._unrealized(p, tick.mid)
        if unrealized > p.peak_unrealized:
            p.peak_unrealized = unrealized
        if not self.dry_run and time.time() - self._last_exchange_check > 8:
            self._last_exchange_check = time.time()
            try:
                positions = self.executor.get_open_positions()
                if not any(pos.symbol == p.symbol for pos in positions):
                    log(f"  EXCHANGE-SIDE close detected for {p.symbol}")
                    self._finalize_close(p, unrealized, "EXCHANGE_CLOSE", tick.mid)
                    return
            except Exception:
                pass
        chg_pct = (tick.mid - p.entry_price) / p.entry_price * 100
        if p.side == "LONG":
            if chg_pct >= TP_PCT * 100:
                self._do_close(p, "TP_HIT", tick.mid); return
            if chg_pct <= -SL_PCT * 100:
                self._do_close(p, "SL_HIT", tick.mid); return
        else:
            if chg_pct <= -TP_PCT * 100:
                self._do_close(p, "TP_HIT", tick.mid); return
            if chg_pct >= SL_PCT * 100:
                self._do_close(p, "SL_HIT", tick.mid); return
        if p.peak_unrealized >= SCALP_OUT_ARM and unrealized < SCALP_OUT_FLOOR:
            self._do_close(p, f"SCALP_OUT peak ${p.peak_unrealized:.2f}", tick.mid); return
        if p.peak_unrealized >= BIG_WIN_LOCK_USD:
            v3 = st.velocity_pct(3)
            if v3 is not None:
                if p.side == "LONG" and v3 < -BIG_WIN_LOCK_FLIP_3S:
                    self._do_close(p, f"BIG_WIN_LOCK v3={v3:+.2f}%", tick.mid); return
                if p.side == "SHORT" and v3 > BIG_WIN_LOCK_FLIP_3S:
                    self._do_close(p, f"BIG_WIN_LOCK v3={v3:+.2f}%", tick.mid); return
            if unrealized < p.peak_unrealized * (1 - BIG_WIN_LOCK_GIVEBACK):
                self._do_close(p, f"BIG_WIN_GIVEBACK peak ${p.peak_unrealized:.2f}", tick.mid); return
        age_s = (now_ms() - p.entry_ts_ms) / 1000
        skip_drift = age_s < DIV_GRACE_SEC
        skip_v3_loss = age_s < V3_GRACE_SEC and unrealized > -PROFIT_FLOOR

        # ACTIVE FLIP — losing position + opposite strongly confirmed (v1 + cvd) -> flip
        # Faster trigger: -$0.15 loss after just 1.5s
        if (unrealized < -0.15 and age_s >= 1.5
                and p.symbol not in self.symbol_session_banned
                and len(self.positions) <= MAX_CONCURRENT_POSITIONS):
            v1_now = st.velocity_pct(1)
            cvd_3s_now, _ = st.cvd_signal(3, weighted=True)
            opposite = "SHORT" if p.side == "LONG" else "LONG"
            opp_v1_strong = v1_now is not None and (
                (opposite == "LONG" and v1_now > 0.10)
                or (opposite == "SHORT" and v1_now < -0.10)
            )
            opp_cvd_strong = (
                (opposite == "LONG" and cvd_3s_now > 0.40)
                or (opposite == "SHORT" and cvd_3s_now < -0.40)
            )
            if opp_v1_strong and opp_cvd_strong:
                log(f"  ACTIVE_FLIP {p.symbol} {p.side}->{opposite} unr=${unrealized:.2f} v1={v1_now:+.2f} cvd3={cvd_3s_now:+.2f}")
                symbol = p.symbol
                strategy = p.strategy
                self._do_close(p, f"ACTIVE_FLIP({p.side}->{opposite})", tick.mid)
                self._execute_entry(symbol, opposite, tick, strategy + "_AFLIP")
                return

        hard_floor = -HARD_MAX_LOSS_FRAC * p.margin
        if unrealized < hard_floor:
            self._do_close(p, f"HARD_MAX_LOSS unr=${unrealized:.2f}", tick.mid); return
        flip_action, flip_reason = st.detect_flip(p.side, unrealized,
                                                  skip_drift=skip_drift,
                                                  skip_v3_loss=skip_v3_loss)
        if flip_action == "REVERSE":
            # Check if opposite is confirmed -> flip instead of just closing
            v1_now = st.velocity_pct(1)
            cvd_3s_now, _ = st.cvd_signal(3, weighted=True)
            opposite = "SHORT" if p.side == "LONG" else "LONG"
            opp_v1_strong = v1_now is not None and (
                (opposite == "LONG" and v1_now > 0.08)
                or (opposite == "SHORT" and v1_now < -0.08)
            )
            opp_cvd_strong = (
                (opposite == "LONG" and cvd_3s_now > 0.30)
                or (opposite == "SHORT" and cvd_3s_now < -0.30)
            )
            if (opp_v1_strong and opp_cvd_strong
                    and p.symbol not in self.symbol_session_banned
                    and len(self.positions) <= MAX_CONCURRENT_POSITIONS):
                log(f"  STRONG_FLIP+AFLIP {p.symbol} {p.side}->{opposite} {flip_reason} v1={v1_now:+.2f} cvd3={cvd_3s_now:+.2f}")
                symbol = p.symbol
                strategy = p.strategy
                self._do_close(p, f"STRONG_FLIP({flip_reason})", tick.mid)
                self._execute_entry(symbol, opposite, tick, strategy + "_SFLIP")
                return
            log(f"  STRONG FLIP {p.symbol} {flip_reason} -> close (opposite not confirmed)")
            self._do_close(p, f"STRONG_FLIP({flip_reason})", tick.mid); return
        elif flip_action == "EXIT":
            self._do_close(p, f"FLIP({flip_reason})", tick.mid); return
        arm = TRAIL_ARM_R * p.margin
        trail_50 = TRAIL_50_R * p.margin
        trail_70 = TRAIL_70_R * p.margin
        if p.peak_unrealized >= arm and not p.moved_to_breakeven:
            p.moved_to_breakeven = True
            log(f"  [trail armed {p.symbol}] peak ${p.peak_unrealized:.2f}")
        if p.moved_to_breakeven and unrealized <= 0:
            self._do_close(p, "BREAKEVEN_TRAIL", tick.mid); return
        if p.peak_unrealized >= trail_50 and unrealized < p.peak_unrealized * 0.5:
            self._do_close(p, "TRAIL_50", tick.mid); return
        if p.peak_unrealized >= trail_70 and unrealized < p.peak_unrealized * 0.7:
            self._do_close(p, "TRAIL_70", tick.mid); return
        if age_s > STALE_HOLD_SECONDS and abs(unrealized) < 0.001 * p.notional:
            self._do_close(p, "STALE", tick.mid); return
        if age_s > MAX_HOLD_SECONDS:
            self._do_close(p, "TIMEOUT", tick.mid); return

    def _do_close(self, p: Position, reason: str, current_price: float) -> None:
        with self.lock:
            if p.symbol not in self.positions:
                return
            if self.dry_run:
                if p.side == "LONG":
                    pnl = (current_price - p.entry_price) * p.qty
                else:
                    pnl = (p.entry_price - current_price) * p.qty
                fees = (p.qty * current_price + p.qty * p.entry_price) * 0.0005
                spread_cost = p.notional * 0.0005
                pnl_net = pnl - fees - spread_cost
                self._finalize_close(p, pnl_net, reason, current_price)
                return
            try:
                if p.side == "LONG":
                    result = self.executor.close_long_position(p.symbol, p.qty)
                else:
                    result = self.executor.close_short_position(p.symbol, p.qty)
            except Exception as exc:
                log(f"  CLOSE exception {p.symbol}: {exc}")
                self.executor.emergency_close_all()
                self._finalize_close(p, 0, f"EXCEPTION:{reason}", current_price)
                return
            actual_exit = result.avg_fill_price if (result.success and result.avg_fill_price > 0) else 0
            if actual_exit > 0:
                if p.side == "LONG":
                    pnl = (actual_exit - p.entry_price) * p.qty - (p.qty * actual_exit + p.qty * p.entry_price) * 0.0005
                else:
                    pnl = (p.entry_price - actual_exit) * p.qty - (p.qty * actual_exit + p.qty * p.entry_price) * 0.0005
            else:
                pnl = 0
            try:
                self.executor.client.cancel_all_algo_orders(p.symbol)
            except Exception:
                pass
            self._finalize_close(p, pnl, reason, actual_exit if actual_exit else current_price)

    def _finalize_close(self, p: Position, pnl: float, reason: str, exit_price: float) -> None:
        if p.symbol not in self.positions:
            return
        self.session_realized += pnl
        self.last_close_was_win = pnl > 0
        if pnl > 0:
            self.win_count += 1
            self.symbol_consecutive_losses[p.symbol] = 0
        else:
            self.symbol_loss_until[p.symbol] = time.time() + SYMBOL_LOSS_COOLDOWN_SEC
            self.symbol_consecutive_losses[p.symbol] = self.symbol_consecutive_losses.get(p.symbol, 0) + 1
            if self.symbol_consecutive_losses[p.symbol] >= CONSECUTIVE_LOSS_BAN:
                self.symbol_session_banned.add(p.symbol)
                log(f"  BANNED {p.symbol} for session ({self.symbol_consecutive_losses[p.symbol]} consecutive losses)")
        win_rate = (self.win_count / self.trade_count * 100) if self.trade_count else 0
        log(f"  CLOSED {p.side} {p.symbol} [{p.strategy}] pnl=${pnl:+.2f} reason={reason} session=${self.session_realized:+.2f} winrate={win_rate:.0f}% ({self.win_count}/{self.trade_count}) open={len(self.positions)-1}")
        del self.positions[p.symbol]
        self.last_close_time = time.time()
        if self.session_realized >= SESSION_TARGET:
            log(f"*** SESSION TARGET +${self.session_realized:.2f} REACHED ***")
            self.stop_flag = True
        elif self.session_realized <= SESSION_LOSS_LIMIT:
            log(f"*** SESSION LOSS LIMIT ${self.session_realized:.2f} HIT ***")
            self.stop_flag = True


_global_scalper: MomentumScalper | None = None


def signal_handler(signum, frame):
    log(f"\n!!! Signal {signum} — closing all positions before exit")
    global _global_scalper
    if _global_scalper:
        for sym, p in list(_global_scalper.positions.items()):
            st = _global_scalper.states.get(sym)
            last_mid = st.ticks[-1].mid if st and st.ticks else 0
            _global_scalper._do_close(p, "MANUAL_STOP", last_mid)
    sys.exit(0)


async def ws_public_consumer(scalper: MomentumScalper, url: str) -> None:
    while not scalper.stop_flag:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                log("WS /public connected (bookTicker).")
                while not scalper.stop_flag:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        log("WS /public recv timeout, reconnecting")
                        break
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data") if "data" in msg else msg
                        if data.get("e") == "bookTicker" or data.get("u"):
                            sym = data.get("s")
                            bid = float(data.get("b", 0))
                            ask = float(data.get("a", 0))
                            bid_qty = float(data.get("B", 0))
                            ask_qty = float(data.get("A", 0))
                            if sym and bid > 0 and ask > 0:
                                mid = (bid + ask) / 2
                                tick = Tick(now_ms(), mid, bid, ask, bid_qty, ask_qty)
                                scalper.on_tick(sym, tick)
                    except Exception as exc:
                        log(f"  public parse err: {exc}")
        except Exception as exc:
            log(f"WS /public connect err: {exc}")
        if scalper.stop_flag:
            break
        log("/public reconnecting in 5s...")
        await asyncio.sleep(5)


async def ws_market_consumer(scalper: MomentumScalper, url: str) -> None:
    while not scalper.stop_flag:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                log("WS /market connected (aggTrade + forceOrder).")
                while not scalper.stop_flag:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data") if "data" in msg else msg
                        e = data.get("e") if isinstance(data, dict) else None
                        if e == "aggTrade":
                            sym = data.get("s")
                            price = float(data.get("p", 0))
                            qty = float(data.get("q", 0))
                            is_buyer_maker = data.get("m", False)
                            is_aggressive_buy = not is_buyer_maker
                            if sym and price > 0 and qty > 0 and sym in scalper.states:
                                tr = Trade(now_ms(), price, qty, is_aggressive_buy)
                                scalper.states[sym].add_trade(tr)
                        elif e == "forceOrder":
                            order = data.get("o", {})
                            sym = order.get("s")
                            side = order.get("S")
                            qty = float(order.get("q", 0))
                            price = float(order.get("ap", order.get("p", 0)))
                            if sym and side in ("BUY", "SELL") and qty > 0 and price > 0 and sym in scalper.states:
                                liq = Liquidation(now_ms(), side, qty, price)
                                scalper.states[sym].add_liquidation(liq)
                                if liq.usd >= 5000:  # log only meaningful
                                    log(f"  LIQ {sym} {side} ${liq.usd:.0f} @ {price}")
                    except Exception as exc:
                        log(f"  market parse err: {exc}")
        except Exception as exc:
            log(f"WS /market connect err: {exc}")
        if scalper.stop_flag:
            break
        log("/market reconnecting in 5s...")
        await asyncio.sleep(5)


async def main_async(scalper: MomentumScalper, public_url: str, market_url: str) -> None:
    await asyncio.gather(
        ws_public_consumer(scalper, public_url),
        ws_market_consumer(scalper, market_url),
    )


def main() -> int:
    global _global_scalper, SESSION_TARGET, SESSION_LOSS_LIMIT, MARGIN_BASE, LEVERAGE
    global TP_PCT, SL_PCT, MAX_HOLD_SECONDS, STALE_HOLD_SECONDS, HARD_MAX_LOSS_FRAC, FORCE_MIN_V5, FORCE_MIN_CVD
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--testnet", action="store_true", help="place real orders on Binance USD-M Futures testnet")
    parser.add_argument("--testnet-base-url", default=os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", "https://testnet.binancefuture.com"))
    parser.add_argument("--testnet-ws-url", default=os.getenv("BINANCE_FUTURES_TESTNET_WS_URL", "wss://stream.binancefuture.com"))
    parser.add_argument("--dry-wallet", type=float, default=24.0)
    parser.add_argument("--max-seconds", type=float, default=0.0, help="stop after this many seconds; 0 runs until target/loss/interrupted")
    parser.add_argument("--force-after", type=float, default=0.0, help="after N seconds, force best available micro-momentum entries")
    parser.add_argument("--session-target", type=float, default=SESSION_TARGET)
    parser.add_argument("--session-loss", type=float, default=SESSION_LOSS_LIMIT)
    parser.add_argument("--margin-base", type=float, default=MARGIN_BASE)
    parser.add_argument("--leverage", type=int, default=LEVERAGE)
    parser.add_argument("--tp-pct", type=float, default=TP_PCT)
    parser.add_argument("--sl-pct", type=float, default=SL_PCT)
    parser.add_argument("--max-hold-seconds", type=float, default=MAX_HOLD_SECONDS)
    parser.add_argument("--stale-hold-seconds", type=float, default=STALE_HOLD_SECONDS)
    parser.add_argument("--hard-max-loss-frac", type=float, default=HARD_MAX_LOSS_FRAC)
    parser.add_argument("--force-min-v5", type=float, default=FORCE_MIN_V5)
    parser.add_argument("--force-min-cvd", type=float, default=FORCE_MIN_CVD)
    parser.add_argument("--symbols", default="", help="comma-separated symbols; empty keeps volatile auto-selection")
    args = parser.parse_args()
    SESSION_TARGET = args.session_target
    SESSION_LOSS_LIMIT = args.session_loss
    MARGIN_BASE = args.margin_base
    LEVERAGE = args.leverage
    TP_PCT = args.tp_pct
    SL_PCT = args.sl_pct
    MAX_HOLD_SECONDS = args.max_hold_seconds
    STALE_HOLD_SECONDS = args.stale_hold_seconds
    HARD_MAX_LOSS_FRAC = args.hard_max_loss_frac
    FORCE_MIN_V5 = args.force_min_v5
    FORCE_MIN_CVD = args.force_min_cvd

    if args.testnet:
        api_key, secret = load_credentials_from_env("BINANCE_TESTNET")
        if not api_key or not secret:
            log("ERROR: missing BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET_KEY for exchange-side dry run")
            return 1
        executor = LiveExecutor(
            api_key=api_key,
            secret_key=secret,
            max_margin_per_trade=MARGIN_BASE,
            base_url=args.testnet_base_url,
        )
        starting_balance = executor.get_usdt_balance()
    elif args.dry_run:
        executor = None
        starting_balance = args.dry_wallet
    else:
        api_key, secret = load_credentials_from_env()
        if not api_key or not secret:
            log("ERROR: missing credentials")
            return 1
        executor = LiveExecutor(api_key=api_key, secret_key=secret, max_margin_per_trade=MARGIN_BASE)
        starting_balance = executor.get_usdt_balance()

    if args.symbols.strip():
        universe = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        log("Selecting volatile universe...")
        universe = select_volatile_universe()
    if not universe:
        universe = ["DAMUSDT", "PRLUSDT", "ZKJUSDT", "AIOTUSDT"]

    mode_tag = "BINANCE FUTURES TESTNET" if args.testnet else ("DRY-RUN (shadow)" if args.dry_run else "LIVE TRADING")
    log("=" * 70)
    log(f"MOMENTUM SCALPER v9.0 — {mode_tag}")
    log(f"  Starting USDT: ${starting_balance:.4f}")
    log(f"  Universe ({len(universe)}): {universe}")
    log(f"  Per-trade: margin=${MARGIN_BASE} lev={LEVERAGE}x SL={SL_PCT*100:.1f}% TP={TP_PCT*100:.1f}%")
    log(f"  Strategies: regime-gated")
    log(f"    TRENDING (|v60| >= {REGIME_TREND_PCT}%): LIQ_FOLLOW (ride cascade with trend)")
    log(f"    CHOP: FADE_SPIKE+CVD (whale-weighted, oppose by >={FADE_CVD_MIN})")
    log(f"  Whale tiers: ${TIER_INFORMED_USD}/${TIER_WHALE_USD}/${TIER_INSTITUTIONAL_USD} (5x/15x/50x weight)")
    log(f"  Confirm: fav>={MIN_CONFIRM_FAVORABLE_PCT}% + v1 + cvd_3s>={CONFIRM_CVD_THRESHOLD}, watch {ENTRY_WATCH_MIN_SEC}-{ENTRY_WATCH_MAX_SEC}s")
    log(f"  Session: target +${SESSION_TARGET} max-loss ${SESSION_LOSS_LIMIT}")
    log("=" * 70)

    if not args.dry_run:
        try:
            assert executor is not None
            existing = executor.get_open_positions()
            if existing:
                log("WARN: pre-existing position, closing")
                executor.emergency_close_all()
                time.sleep(3)
        except Exception:
            pass

    scalper = MomentumScalper(
        executor,
        dry_run=args.dry_run and not args.testnet,
        watch_symbols=universe,
        force_after_sec=args.force_after,
    )
    _global_scalper = scalper

    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass

    public_streams = "/".join(f"{s.lower()}@bookTicker" for s in universe)
    market_streams = "/".join(f"{s.lower()}@aggTrade" for s in universe) + "/!forceOrder@arr"
    if args.testnet:
        ws_base = args.testnet_ws_url.rstrip("/")
        public_url = f"{ws_base}/stream?streams={public_streams}"
        market_url = f"{ws_base}/stream?streams={market_streams}"
    else:
        public_url = f"wss://fstream.binance.com/public/stream?streams={public_streams}"
        market_url = f"wss://fstream.binance.com/market/stream?streams={market_streams}"
    log(f"Connecting /public + /market for {len(universe)} symbols + global liquidation feed")

    try:
        if args.max_seconds > 0:
            asyncio.run(asyncio.wait_for(main_async(scalper, public_url, market_url), timeout=args.max_seconds))
        else:
            asyncio.run(main_async(scalper, public_url, market_url))
    except asyncio.TimeoutError:
        log(f"Timed session stop after {args.max_seconds:.1f}s")
    except KeyboardInterrupt:
        log("KeyboardInterrupt")
    except Exception as exc:
        log(f"Main exception: {exc}")

    for sym, p in list(scalper.positions.items()):
        st = scalper.states.get(sym)
        last_mid = st.ticks[-1].mid if st and st.ticks else 0
        scalper._do_close(p, "SHUTDOWN", last_mid)

    final = executor.get_usdt_balance() if not scalper.dry_run and executor is not None else starting_balance + scalper.session_realized
    win_rate = (scalper.win_count / scalper.trade_count * 100) if scalper.trade_count else 0
    log("")
    log(f"=== FINAL ({mode_tag}) ===")
    log(f"  USDT: ${final:.4f}  net session: ${scalper.session_realized:+.4f}")
    log(f"  Trades: {scalper.trade_count}  Wins: {scalper.win_count}  Win-rate: {win_rate:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
