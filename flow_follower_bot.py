"""
Flow Follower Bot

Paper-first Solana pump.fun market-flow bot.

The old bot mostly copied wallets and then tried to survive the aftermath. This bot
listens to the market-wide pump.fun tape and enters only when the tape shifts from
accumulation/absorption into expansion. It is built to catch runners:

1. Birth ignition: fresh mint, clustered buys, almost no sells, price expanding.
2. Dump absorption: token already dumped, sells stop moving price down, buys return.
3. Second wave: later flow confirms a new expansion leg.

It is paper mode by default. Do not set FLOW_PAPER_TRADING=0 until the paper logs
show a repeatable edge and live execution is wired/reviewed separately.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import signal
import struct
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

import websockets
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONK_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
BC_DISC = bytes([0x17, 0xB7, 0xF8, 0x37, 0x60, 0xD8, 0xAC, 0x60])
BC_DISC_B58 = "4y6pru6YvC7"


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        return


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().strip('"').strip("'")


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name, "1" if default else "0").lower()
    return value not in {"0", "false", "no", "off", ""}


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env_str(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except Exception:
        return default


def now_ms() -> int:
    return int(time.time() * 1000)


def short_mint(mint: str) -> str:
    if len(mint) <= 10:
        return mint
    return f"{mint[:4]}..{mint[-4:]}"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


@dataclass(frozen=True)
class FlowConfig:
    paper_trading: bool = True
    st_rpc_ws: str = ""
    base_amount_sol: float = 0.01
    max_amount_sol: float = 0.025
    max_open_positions: int = 3
    max_tape_age_sec: int = 420
    curve_max_age_ms: int = 800
    curve_move_window_ms: int = 1500
    entry_cooldown_sec: float = 20.0
    min_seconds_between_entries: float = 0.7
    heartbeat_sec: float = 0.25
    report_sec: float = 5.0
    paper_drag_bps: float = 250.0
    alpha_min_n: int = 20
    alpha_min_wr: float = 0.60
    alpha_min_avg_exit: float = 0.045
    state_file: Path = DATA_DIR / "flow_follower_state.json"
    alpha_file: Optional[Path] = None
    snipers_file: Path = BASE_DIR / "active_snipers.txt"
    run_seconds: float = 0.0
    print_every_trade: bool = False

    @staticmethod
    def from_env(args: argparse.Namespace) -> "FlowConfig":
        _load_dotenv()
        key = env_str("SOLANATRACKER_RPC_KEY")
        ws = env_str("SOLANATRACKER_RPC_WS")
        if not ws and key:
            ws = f"wss://rpc-mainnet.solanatracker.io?api_key={key}"

        alpha_env = env_str("FLOW_ALPHA_STATE_FILE")
        alpha_file: Optional[Path]
        if args.alpha:
            alpha_file = Path(args.alpha)
        elif alpha_env:
            alpha_file = Path(alpha_env)
        else:
            server_alpha = Path("/root/sniper/data/executable_alpha.json")
            local_alpha = DATA_DIR / "executable_alpha.json"
            alpha_file = server_alpha if server_alpha.is_file() else local_alpha

        return FlowConfig(
            paper_trading=env_bool("FLOW_PAPER_TRADING", True),
            st_rpc_ws=args.ws or ws,
            base_amount_sol=env_float("FLOW_BASE_AMOUNT_SOL", 0.01),
            max_amount_sol=env_float("FLOW_MAX_AMOUNT_SOL", 0.025),
            max_open_positions=env_int("FLOW_MAX_OPEN_POSITIONS", 3),
            max_tape_age_sec=env_int("FLOW_MAX_TAPE_AGE_SEC", 420),
            curve_max_age_ms=env_int("FLOW_CURVE_MAX_AGE_MS", 800),
            curve_move_window_ms=env_int("FLOW_CURVE_MOVE_WINDOW_MS", 1500),
            entry_cooldown_sec=env_float("FLOW_ENTRY_COOLDOWN_SEC", 20.0),
            min_seconds_between_entries=env_float("FLOW_MIN_SECONDS_BETWEEN_ENTRIES", 0.7),
            heartbeat_sec=env_float("FLOW_HEARTBEAT_SEC", 0.25),
            report_sec=env_float("FLOW_REPORT_SEC", 5.0),
            paper_drag_bps=env_float("FLOW_PAPER_DRAG_BPS", 250.0),
            alpha_min_n=env_int("FLOW_ALPHA_MIN_N", 20),
            alpha_min_wr=env_float("FLOW_ALPHA_MIN_WR", 0.60),
            alpha_min_avg_exit=env_float("FLOW_ALPHA_MIN_AVG_EXIT", 0.045),
            state_file=Path(args.state or env_str("FLOW_STATE_FILE", str(DATA_DIR / "flow_follower_state.json"))),
            alpha_file=alpha_file,
            snipers_file=Path(args.snipers or env_str("FLOW_SNIPERS_FILE", str(BASE_DIR / "active_snipers.txt"))),
            run_seconds=float(args.run_seconds or 0.0),
            print_every_trade=bool(args.print_every_trade),
        )


@dataclass(frozen=True)
class FlowEvent:
    ts_ms: int
    sig: str
    signer: str
    mint: str
    is_buy: bool
    sol: float
    price: float
    tracked: bool
    program: str


@dataclass
class WindowStats:
    window_ms: int
    events: int = 0
    buys: int = 0
    sells: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    unique_buyers: int = 0
    tracked_buyers: int = 0
    first_price: float = 0.0
    last_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0

    @property
    def net_sol(self) -> float:
        return self.buy_sol - self.sell_sol

    @property
    def buy_ratio(self) -> float:
        total = self.buy_sol + self.sell_sol
        return self.buy_sol / total if total > 0 else 0.0

    @property
    def buy_pressure(self) -> float:
        return self.buy_sol / max(self.sell_sol, 0.001)

    @property
    def price_change(self) -> float:
        if self.first_price <= 0 or self.last_price <= 0:
            return 0.0
        return (self.last_price / self.first_price) - 1.0


@dataclass
class MintTape:
    mint: str
    events: Deque[FlowEvent] = field(default_factory=deque)
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    peak_price: float = 0.0
    trough_price: float = 0.0
    last_price: float = 0.0

    def add(self, event: FlowEvent, max_age_sec: int) -> None:
        if not self.first_seen_ms:
            self.first_seen_ms = event.ts_ms
        self.last_seen_ms = event.ts_ms
        self.last_price = event.price or self.last_price
        if event.price > 0:
            self.peak_price = max(self.peak_price or event.price, event.price)
            self.trough_price = min(self.trough_price or event.price, event.price)
        self.events.append(event)
        cutoff = event.ts_ms - max_age_sec * 1000
        while self.events and self.events[0].ts_ms < cutoff:
            self.events.popleft()

    def age_sec(self, ts_ms: int) -> float:
        if not self.first_seen_ms:
            return 0.0
        return max(0.0, (ts_ms - self.first_seen_ms) / 1000.0)

    def off_peak(self) -> float:
        if self.peak_price <= 0 or self.last_price <= 0:
            return 1.0
        return self.last_price / self.peak_price

    def bounce_from_trough(self) -> float:
        if self.trough_price <= 0 or self.last_price <= 0:
            return 1.0
        return self.last_price / self.trough_price

    def stats(self, window_ms: int, ts_ms: int) -> WindowStats:
        cutoff = ts_ms - window_ms
        selected = [e for e in self.events if e.ts_ms >= cutoff]
        out = WindowStats(window_ms=window_ms, events=len(selected))
        if not selected:
            return out
        buys = [e for e in selected if e.is_buy]
        sells = [e for e in selected if not e.is_buy]
        out.buys = len(buys)
        out.sells = len(sells)
        out.buy_sol = sum(e.sol for e in buys)
        out.sell_sol = sum(e.sol for e in sells)
        out.unique_buyers = len({e.signer for e in buys if e.signer})
        out.tracked_buyers = len({e.signer for e in buys if e.tracked and e.signer})
        prices = [e.price for e in selected if e.price > 0]
        if prices:
            out.first_price = prices[0]
            out.last_price = prices[-1]
            out.high_price = max(prices)
            out.low_price = min(prices)
        return out


@dataclass(frozen=True)
class AlphaStats:
    n: int
    wins: int
    avg_exit: float
    avg_best: float
    avg_worst: float

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n > 0 else 0.0


class AlphaBook:
    def __init__(self, path: Optional[Path], config: FlowConfig):
        self.path = path
        self.config = config
        self.contexts: dict[str, AlphaStats] = {}
        self.loaded = False
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw_contexts = ((data.get("stats") or {}).get("contexts") or {})
            for key, raw in raw_contexts.items():
                n = int(raw.get("n") or 0)
                if n <= 0:
                    continue
                self.contexts[key] = AlphaStats(
                    n=n,
                    wins=int(raw.get("wins") or 0),
                    avg_exit=float(raw.get("exit_net_sum") or 0.0) / n,
                    avg_best=float(raw.get("best_net_sum") or 0.0) / n,
                    avg_worst=float(raw.get("worst_net_sum") or 0.0) / n,
                )
            self.loaded = True
        except Exception as exc:
            log(f"ALPHA: failed to load {self.path}: {type(exc).__name__}: {exc}")

    def get(self, context: str) -> Optional[AlphaStats]:
        return self.contexts.get(context)

    def is_strong(self, stats: Optional[AlphaStats]) -> bool:
        if not stats:
            return False
        if stats.n < self.config.alpha_min_n:
            return False
        if stats.wr >= self.config.alpha_min_wr and stats.avg_exit >= self.config.alpha_min_avg_exit:
            return True
        if stats.n >= 30 and stats.avg_exit >= self.config.alpha_min_avg_exit * 1.8:
            return True
        return False

    def score(self, stats: Optional[AlphaStats]) -> float:
        if not stats or stats.n <= 0:
            return 0.0
        sample = min(2.0, math.log10(max(stats.n, 1)) / 1.8)
        risk_penalty = max(0.0, -stats.avg_worst) * 65.0
        return (
            stats.avg_exit * 100.0
            + stats.avg_best * 18.0
            + stats.wr * 18.0
            + sample * 4.0
            - risk_penalty
        )

    def print_report(self, limit: int = 30) -> None:
        rows: list[tuple[float, str, AlphaStats]] = []
        for key, stats in self.contexts.items():
            if stats.n < self.config.alpha_min_n:
                continue
            rows.append((self.score(stats), key, stats))
        rows.sort(key=lambda x: x[0], reverse=True)
        for score, key, stats in rows[:limit]:
            log(
                f"ALPHA {key} score={score:.1f} n={stats.n} "
                f"wr={stats.wr * 100:.1f}% avg_exit={stats.avg_exit * 100:+.1f}% "
                f"avg_best={stats.avg_best * 100:+.1f}% avg_worst={stats.avg_worst * 100:+.1f}%"
            )


class BondingCurveCache:
    """Hot pump.fun curve-price cache populated by programSubscribe.

    Shred tape is for speed and intent. This cache is the price truth: all fills,
    PnL, peaks, trails, and close decisions must come from virtual reserves.
    """

    def __init__(self) -> None:
        self.by_curve: dict[str, Deque[tuple[int, int, int, bool]]] = defaultdict(lambda: deque(maxlen=80))
        self._mint_to_curve: dict[str, str] = {}
        self.updates = 0
        self.decode_errors = 0

    def curve_for_mint(self, mint: str) -> Optional[str]:
        cached = self._mint_to_curve.get(mint)
        if cached:
            return cached
        try:
            pda, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(Pubkey.from_string(mint))],
                Pubkey.from_string(PUMP_PROGRAM),
            )
            out = str(pda)
            self._mint_to_curve[mint] = out
            return out
        except Exception:
            return None

    def update_from_program_value(self, value: dict[str, Any], ts_ms: int) -> bool:
        pubkey = value.get("pubkey")
        if not pubkey:
            return False
        account = value.get("account") or {}
        acc_data = account.get("data")
        if isinstance(acc_data, list):
            acc_data = acc_data[0]
        if not isinstance(acc_data, str):
            return False
        try:
            raw = base64.b64decode(acc_data)
            if len(raw) < 49 or raw[:8] != BC_DISC:
                return False
            vtoken = struct.unpack_from("<Q", raw, 0x08)[0]
            vsol = struct.unpack_from("<Q", raw, 0x10)[0]
            complete = raw[48] != 0
            if vtoken <= 0 or vsol <= 0:
                return False
            self.by_curve[str(pubkey)].append((ts_ms, vsol, vtoken, complete))
            self.updates += 1
            return True
        except Exception:
            self.decode_errors += 1
            return False

    def price_for_mint(self, mint: str, max_age_ms: int, ts_ms: Optional[int] = None) -> Optional[tuple[float, bool, int]]:
        curve = self.curve_for_mint(mint)
        if not curve:
            return None
        items = self.by_curve.get(curve)
        if not items:
            return None
        ts_ms = ts_ms or now_ms()
        item_ts, vsol, vtoken, complete = items[-1]
        age_ms = ts_ms - int(item_ts)
        if age_ms < 0:
            age_ms = 0
        if age_ms > max_age_ms or vtoken <= 0:
            return None
        return float(vsol) / float(vtoken), bool(complete), age_ms

    def move_for_mint(self, mint: str, window_ms: int, max_age_ms: int, ts_ms: Optional[int] = None) -> Optional[tuple[float, int, bool]]:
        curve = self.curve_for_mint(mint)
        if not curve:
            return None
        items = list(self.by_curve.get(curve) or [])
        if len(items) < 2:
            return None
        ts_ms = ts_ms or now_ms()
        latest_ts, latest_vsol, latest_vtoken, complete = items[-1]
        latest_age = ts_ms - int(latest_ts)
        if latest_age > max_age_ms or latest_vtoken <= 0:
            return None
        cutoff = ts_ms - window_ms
        recent = [item for item in items if item[0] >= cutoff]
        if len(recent) < 2:
            return None
        first = recent[0]
        if first[2] <= 0:
            return None
        first_price = float(first[1]) / float(first[2])
        last_price = float(latest_vsol) / float(latest_vtoken)
        if first_price <= 0:
            return None
        return last_price / first_price, int(latest_age), bool(complete)


@dataclass
class PaperPosition:
    mint: str
    phase: str
    context: str
    reason: str
    entry_ts_ms: int
    entry_price: float
    amount_sol: float
    remaining_pct: float = 1.0
    realized_sol: float = 0.0
    peak_mult: float = 1.0
    last_mult: float = 1.0
    last_price: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    runner: bool = False

    def age_sec(self, ts_ms: int) -> float:
        return max(0.0, (ts_ms - self.entry_ts_ms) / 1000.0)

    def update(self, price: float) -> float:
        if price <= 0 or self.entry_price <= 0:
            return self.last_mult
        mult = price / self.entry_price
        self.last_price = price
        self.last_mult = mult
        self.peak_mult = max(self.peak_mult, mult)
        return mult

    def mark_pnl_sol(self) -> float:
        open_value = self.amount_sol * self.remaining_pct * self.last_mult
        return self.realized_sol + open_value - self.amount_sol


@dataclass
class SessionStats:
    entries: int = 0
    closes: int = 0
    wins: int = 0
    losses: int = 0
    partials: int = 0
    realized_pnl_sol: float = 0.0
    best_open_mult: float = 1.0
    shreds: int = 0
    trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    entry_signals: int = 0
    started_at: float = field(default_factory=time.time)


class PaperBroker:
    def __init__(self, config: FlowConfig):
        self.config = config
        self.positions: dict[str, PaperPosition] = {}
        self.stats = SessionStats()
        self.closed_recent: dict[str, int] = {}
        self.last_entry_ts_ms = 0
        self.load_state()

    @property
    def drag(self) -> float:
        return max(0.0, self.config.paper_drag_bps) / 10000.0

    def load_state(self) -> None:
        path = self.config.state_file
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.stats = SessionStats(**{**asdict(self.stats), **(data.get("session") or {})})
            for mint, raw in (data.get("positions") or {}).items():
                self.positions[mint] = PaperPosition(**raw)
            log(f"STATE: restored {len(self.positions)} flow positions from {path}")
        except Exception as exc:
            log(f"STATE: could not restore {path}: {type(exc).__name__}: {exc}")

    def save_state(self) -> None:
        path = self.config.state_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            data = {
                "updated_at": time.time(),
                "paper_trading": self.config.paper_trading,
                "session": asdict(self.stats),
                "positions": {mint: asdict(pos) for mint, pos in self.positions.items()},
            }
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            log(f"STATE: save failed: {type(exc).__name__}: {exc}")

    def can_enter(self, mint: str, ts_ms: int) -> bool:
        if mint in self.positions:
            return False
        if len(self.positions) >= self.config.max_open_positions:
            return False
        if ts_ms - self.last_entry_ts_ms < int(self.config.min_seconds_between_entries * 1000):
            return False
        last_closed = self.closed_recent.get(mint, 0)
        if last_closed and ts_ms - last_closed < int(self.config.entry_cooldown_sec * 1000):
            return False
        return True

    def open(self, signal: "EntrySignal", event: FlowEvent) -> None:
        fill_price = event.price * (1.0 + self.drag)
        if fill_price <= 0:
            return
        pos = PaperPosition(
            mint=event.mint,
            phase=signal.phase,
            context=signal.context,
            reason=signal.reason,
            entry_ts_ms=event.ts_ms,
            entry_price=fill_price,
            amount_sol=signal.amount_sol,
            last_price=fill_price,
        )
        self.positions[event.mint] = pos
        self.last_entry_ts_ms = event.ts_ms
        self.stats.entries += 1
        self.stats.entry_signals += 1
        log(
            f"FLOW-ENTRY {short_mint(event.mint)} phase={signal.phase} "
            f"amount={signal.amount_sol:.4f} SOL ctx={signal.context} score={signal.score:.1f} "
            f"buy6={signal.stats6.buy_sol:.3f} sell6={signal.stats6.sell_sol:.3f} "
            f"uniq6={signal.stats6.unique_buyers} trk6={signal.stats6.tracked_buyers} "
            f"reason={signal.reason}"
        )
        self.save_state()

    def partial(self, mint: str, fraction_of_remaining: float, mult: float, reason: str) -> None:
        pos = self.positions.get(mint)
        if not pos:
            return
        fraction = max(0.0, min(pos.remaining_pct, fraction_of_remaining * pos.remaining_pct))
        if fraction <= 0:
            return
        proceeds = pos.amount_sol * fraction * mult * (1.0 - self.drag)
        pos.remaining_pct -= fraction
        pos.realized_sol += proceeds
        pos.runner = True
        self.stats.partials += 1
        log(
            f"FLOW-PARTIAL {short_mint(mint)} reason={reason} sold={fraction * 100:.1f}% "
            f"mult={mult:.3f}x proceeds={proceeds:.5f} SOL rem={pos.remaining_pct * 100:.1f}%"
        )
        self.save_state()

    def close(self, mint: str, ts_ms: int, mult: float, reason: str) -> None:
        pos = self.positions.pop(mint, None)
        if not pos:
            return
        proceeds = pos.amount_sol * pos.remaining_pct * mult * (1.0 - self.drag)
        total_out = pos.realized_sol + proceeds
        pnl = total_out - pos.amount_sol
        self.stats.realized_pnl_sol += pnl
        self.stats.closes += 1
        if pnl >= 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.closed_recent[mint] = ts_ms
        log(
            f"FLOW-CLOSE {short_mint(mint)} reason={reason} age={pos.age_sec(ts_ms):.2f}s "
            f"mult={mult:.3f}x peak={pos.peak_mult:.3f}x pnl={pnl:+.5f} SOL "
            f"session={self.stats.realized_pnl_sol:+.5f} SOL"
        )
        self.save_state()

    def mark_open_pnl(self) -> float:
        return sum(pos.mark_pnl_sol() for pos in self.positions.values())


@dataclass(frozen=True)
class EntrySignal:
    phase: str
    context: str
    reason: str
    amount_sol: float
    score: float
    stats6: WindowStats


def age_bucket(age_sec: float) -> str:
    if age_sec < 3:
        return "a0_3"
    if age_sec < 6:
        return "a3_6"
    if age_sec < 12:
        return "a6_12"
    if age_sec < 30:
        return "a12_30"
    return "a30p"


def swarm_bucket(tracked_buyers: int) -> str:
    if tracked_buyers <= 0:
        return "sw0"
    if tracked_buyers == 1:
        return "sw1"
    if tracked_buyers == 2:
        return "sw2"
    if tracked_buyers == 3:
        return "sw3"
    return "sw4p"


def trend_bucket(price_change: float) -> str:
    if price_change <= -0.06:
        return "dump"
    if price_change < 0.035:
        return "flat"
    if price_change < 0.12:
        return "rise"
    if price_change < 0.35:
        return "run"
    return "chase"


def price_bucket(tape: MintTape) -> str:
    off_peak = tape.off_peak()
    if off_peak <= 0.72:
        return "dumped"
    if off_peak <= 0.88:
        return "cheap"
    if off_peak <= 1.04:
        return "fair"
    if off_peak <= 1.24:
        return "high"
    return "chase"


def context_key(tape: MintTape, ts_ms: int, stats12: WindowStats) -> str:
    return "|".join(
        [
            "market_tape",
            age_bucket(tape.age_sec(ts_ms)),
            swarm_bucket(stats12.tracked_buyers),
            trend_bucket(stats12.price_change),
            price_bucket(tape),
        ]
    )


def parse_base64_shred_for_pump_trades(shred_result: dict[str, Any]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    try:
        tx_outer = (shred_result.get("transaction") or {}).get("transaction")
        if not (isinstance(tx_outer, list) and tx_outer):
            return trades
        raw = base64.b64decode(tx_outer[0])
        vt = VersionedTransaction.from_bytes(raw)
        keys = list(vt.message.account_keys)
        if not keys:
            return trades
        signer = str(keys[0])
        for ix in vt.message.instructions:
            try:
                program = str(keys[ix.program_id_index])
            except Exception:
                continue
            if program not in {PUMP_PROGRAM, BONK_PROGRAM}:
                continue
            data = bytes(ix.data)
            if len(data) < 24:
                continue
            if data[:8] == DISC_BUY:
                is_buy = True
            elif data[:8] == DISC_SELL:
                is_buy = False
            else:
                continue
            amount = int.from_bytes(data[8:16], "little")
            sol_lamports = int.from_bytes(data[16:24], "little")
            if amount <= 0 or sol_lamports <= 0:
                continue
            try:
                mint_index = ix.accounts[2]
            except Exception:
                continue
            if mint_index >= len(keys):
                continue
            trades.append(
                {
                    "signer": signer,
                    "mint": str(keys[mint_index]),
                    "is_buy": is_buy,
                    "amount": amount,
                    "sol_lamports": sol_lamports,
                    "price": sol_lamports / amount,
                    "program": program,
                }
            )
    except Exception:
        return trades
    return trades


class FlowFollowerBot:
    def __init__(self, config: FlowConfig):
        self.config = config
        self.alpha = AlphaBook(config.alpha_file, config)
        self.bc = BondingCurveCache()
        self.broker = PaperBroker(config)
        self.tapes: dict[str, MintTape] = {}
        self.tracked_wallets = self.load_snipers(config.snipers_file)
        self.seen_trade_limit = 20000
        self.seen_trade_keys: Deque[tuple[str, str, bool, int, int]] = deque()
        self.seen_trade_key_set: set[tuple[str, str, bool, int, int]] = set()
        self.stop_event = asyncio.Event()
        self.last_report_at = time.time()

    @staticmethod
    def load_snipers(path: Path) -> set[str]:
        if not path.is_file():
            return set()
        wallets: set[str] = set()
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                wallets.add(line.split()[0].strip())
        except Exception as exc:
            log(f"SNIPERS: failed to load {path}: {type(exc).__name__}: {exc}")
        return wallets

    def dedup_trade(self, sig: str, trade: dict[str, Any]) -> bool:
        key = (
            sig,
            str(trade.get("mint") or ""),
            bool(trade.get("is_buy")),
            int(trade.get("amount") or 0),
            int(trade.get("sol_lamports") or 0),
        )
        if key in self.seen_trade_key_set:
            return False
        self.seen_trade_keys.append(key)
        self.seen_trade_key_set.add(key)
        while len(self.seen_trade_keys) > self.seen_trade_limit:
            old = self.seen_trade_keys.popleft()
            self.seen_trade_key_set.discard(old)
        return True

    def build_event(self, sig: str, trade: dict[str, Any]) -> Optional[FlowEvent]:
        mint = str(trade.get("mint") or "")
        if not mint:
            return None
        signer = str(trade.get("signer") or "")
        sol_lamports = int(trade.get("sol_lamports") or 0)
        if sol_lamports <= 0:
            return None
        curve = self.bc.price_for_mint(mint, self.config.curve_max_age_ms)
        price = curve[0] if curve and not curve[1] else 0.0
        return FlowEvent(
            ts_ms=now_ms(),
            sig=sig,
            signer=signer,
            mint=mint,
            is_buy=bool(trade.get("is_buy")),
            sol=sol_lamports / 1_000_000_000.0,
            price=price,
            tracked=signer in self.tracked_wallets,
            program=str(trade.get("program") or ""),
        )

    async def on_event(self, event: FlowEvent) -> None:
        self.broker.stats.trades += 1
        if event.is_buy:
            self.broker.stats.buy_trades += 1
        else:
            self.broker.stats.sell_trades += 1
        tape = self.tapes.get(event.mint)
        if tape is None:
            tape = MintTape(event.mint)
            self.tapes[event.mint] = tape
        tape.add(event, self.config.max_tape_age_sec)
        if self.config.print_every_trade:
            side = "B" if event.is_buy else "S"
            log(
                f"TAPE {side} {short_mint(event.mint)} sol={event.sol:.4f} "
                f"tracked={int(event.tracked)} age={tape.age_sec(event.ts_ms):.1f}s"
            )

        pos = self.broker.positions.get(event.mint)
        if pos and event.price > 0:
            await self.manage_position(tape, event.ts_ms, event.price, source="event")

        if event.is_buy and event.price > 0:
            signal = self.detect_entry(tape, event)
            if signal:
                self.broker.open(signal, event)

    def detect_entry(self, tape: MintTape, event: FlowEvent) -> Optional[EntrySignal]:
        ts_ms = event.ts_ms
        if not self.broker.can_enter(event.mint, ts_ms):
            return None
        curve = self.bc.price_for_mint(event.mint, self.config.curve_max_age_ms, ts_ms)
        if not curve:
            return None
        curve_price, complete, curve_age_ms = curve
        if complete or curve_price <= 0:
            return None
        curve_move = self.bc.move_for_mint(
            event.mint,
            self.config.curve_move_window_ms,
            self.config.curve_max_age_ms,
            ts_ms,
        )
        curve_mult = curve_move[0] if curve_move else 1.0
        age = tape.age_sec(ts_ms)
        stats2 = tape.stats(2_000, ts_ms)
        stats3 = tape.stats(3_000, ts_ms)
        stats6 = tape.stats(6_000, ts_ms)
        stats12 = tape.stats(12_000, ts_ms)
        stats30 = tape.stats(30_000, ts_ms)
        ctx = context_key(tape, ts_ms, stats12)
        alpha_stats = self.alpha.get(ctx)
        alpha_score = self.alpha.score(alpha_stats)
        alpha_strong = self.alpha.is_strong(alpha_stats)
        off_peak = tape.off_peak()
        bounce = tape.bounce_from_trough()

        if stats2.sells and stats2.sell_sol > stats2.buy_sol * 1.25 and curve_mult < 0.99:
            return None
        if stats6.buy_sol + stats6.sell_sol < 0.025:
            return None

        phase = ""
        reason = ""
        live_score = 0.0

        birth_ignition = (
            age <= 7.0
            and stats3.unique_buyers >= 3
            and stats3.buy_sol >= 0.14
            and stats3.sell_sol <= 0.035
            and curve_mult >= 1.006
            and off_peak >= 0.965
        )
        if birth_ignition:
            phase = "BIRTH_IGNITION"
            live_score = (
                25.0
                + stats3.unique_buyers * 3.0
                + stats3.buy_sol * 35.0
                + (curve_mult - 1.0) * 320.0
                + stats3.tracked_buyers * 6.0
            )
            reason = "fresh clustered expansion"

        absorption = (
            6.0 <= age <= 420.0
            and off_peak <= 0.88
            and bounce >= 1.035
            and stats6.unique_buyers >= 2
            and stats6.buy_sol >= 0.10
            and stats6.buy_pressure >= 1.65
            and curve_mult >= 1.001
            and (alpha_strong or stats6.tracked_buyers >= 1 or stats12.buy_sol >= 0.24)
        )
        if absorption:
            phase = "ABSORPTION_REVERSAL"
            live_score = (
                30.0
                + min(35.0, (1.0 - off_peak) * 85.0)
                + min(22.0, (bounce - 1.0) * 260.0)
                + stats6.unique_buyers * 3.5
                + stats6.buy_pressure * 2.5
                + stats6.tracked_buyers * 7.0
                + (curve_mult - 1.0) * 240.0
            )
            reason = "dump absorbed and buyers reclaimed price"

        second_wave = (
            12.0 <= age <= 420.0
            and stats12.unique_buyers >= 4
            and stats12.buy_sol >= 0.22
            and stats12.buy_pressure >= 1.35
            and curve_mult >= 1.004
            and off_peak >= 0.86
            and (alpha_strong or stats12.tracked_buyers >= 1 or stats30.buy_sol >= 0.45)
        )
        if second_wave and live_score < 50.0:
            phase = "SECOND_WAVE"
            live_score = (
                28.0
                + stats12.unique_buyers * 2.5
                + stats12.buy_sol * 24.0
                + (curve_mult - 1.0) * 280.0
                + stats12.tracked_buyers * 6.0
            )
            reason = "late expansion leg confirmed"

        big_lift = (
            event.sol >= 0.55
            and curve_mult >= 1.004
            and stats2.sell_sol <= event.sol * 0.35
            and off_peak >= 0.90
        )
        if big_lift and live_score < 47.0:
            phase = "WHALE_LIFT"
            live_score = 32.0 + min(28.0, event.sol * 20.0) + (curve_mult - 1.0) * 260.0
            reason = "single large lift with no sell response"

        stable_absorption = bounce >= 1.005 or curve_mult >= 1.0
        alpha_absorption = (
            alpha_strong
            and 3.0 <= age <= 420.0
            and off_peak <= 0.92
            and stats6.buy_sol >= 0.08
            and stats6.unique_buyers >= 2
            and stats6.buy_pressure >= 1.15
            and stable_absorption
            and curve_mult >= 0.999
        )
        if alpha_absorption and live_score < 45.0:
            phase = "ALPHA_ABSORPTION"
            live_score = 24.0 + alpha_score * 0.55 + stats6.buy_sol * 18.0 + stats6.unique_buyers * 2.0
            reason = "learned winning context reappeared with live buy flow"

        if not phase:
            return None

        score = live_score + alpha_score * 0.45
        if alpha_stats and alpha_stats.n >= self.config.alpha_min_n and alpha_stats.avg_worst < -0.12:
            score -= 12.0
        if stats6.sell_sol > stats6.buy_sol * 0.95:
            score -= 10.0
        if curve_mult < 1.0:
            score -= 15.0
        if trend_bucket(stats12.price_change) == "chase" and off_peak < 0.97:
            score -= 16.0

        if score < 44.0:
            return None

        size_mult = 1.0
        if phase in {"ABSORPTION_REVERSAL", "ALPHA_ABSORPTION"}:
            size_mult += 0.25
        if alpha_strong:
            size_mult += min(0.50, alpha_score / 120.0)
        if stats12.tracked_buyers >= 2:
            size_mult += 0.20
        if stats6.sell_sol > stats6.buy_sol * 0.65:
            size_mult -= 0.25
        amount = min(self.config.max_amount_sol, max(0.002, self.config.base_amount_sol * size_mult))

        return EntrySignal(
            phase=phase,
            context=ctx,
            reason=f"{reason}; curve={curve_mult:.4f}x age={curve_age_ms}ms",
            amount_sol=amount,
            score=score,
            stats6=stats6,
        )

    async def manage_position(self, tape: MintTape, ts_ms: int, price: float, source: str) -> None:
        pos = self.broker.positions.get(tape.mint)
        if not pos:
            return
        mult = pos.update(price)
        self.broker.stats.best_open_mult = max(self.broker.stats.best_open_mult, pos.peak_mult)
        age = pos.age_sec(ts_ms)
        stats2 = tape.stats(2_000, ts_ms)
        stats6 = tape.stats(6_000, ts_ms)

        if age < 0.35:
            return

        if mult <= 0.89 and stats2.sell_sol >= stats2.buy_sol * 0.85:
            self.broker.close(tape.mint, ts_ms, mult, "micro_fail_sell_flip")
            return

        if age >= 4.0 and pos.peak_mult < 1.055 and stats6.buy_pressure < 1.35:
            self.broker.close(tape.mint, ts_ms, mult, "no_expansion")
            return

        if not pos.tp1_done and mult >= 1.22:
            pos.tp1_done = True
            self.broker.partial(tape.mint, 0.25, mult, "runner_fuel_1p22")
            return

        if not pos.tp2_done and mult >= 2.00:
            pos.tp2_done = True
            self.broker.partial(tape.mint, 0.33, mult, "runner_fuel_2p00")
            return

        distribution = (
            stats2.sell_sol > max(0.02, stats2.buy_sol * 1.35)
            and stats2.price_change <= -0.025
        )
        if distribution and age >= 0.7:
            self.broker.close(tape.mint, ts_ms, mult, "distribution_flip")
            return

        if pos.peak_mult >= 3.0:
            trail = 0.34
        elif pos.peak_mult >= 2.0:
            trail = 0.25
        elif pos.peak_mult >= 1.45:
            trail = 0.18
        elif pos.peak_mult >= 1.18:
            trail = 0.12
        else:
            trail = 0.0
        if trail and mult <= pos.peak_mult * (1.0 - trail):
            self.broker.close(tape.mint, ts_ms, mult, f"flow_trail_{trail:.2f}")
            return

        if age >= 18.0 and pos.peak_mult < 1.12 and stats6.buy_pressure < 1.15:
            self.broker.close(tape.mint, ts_ms, mult, "stalled_flow")
            return

        if age >= 120.0 and pos.peak_mult < 1.35:
            self.broker.close(tape.mint, ts_ms, mult, "runner_timeout")
            return

    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            ts_ms = now_ms()
            for mint, pos in list(self.broker.positions.items()):
                tape = self.tapes.get(mint)
                if not tape:
                    continue
                curve = self.bc.price_for_mint(mint, self.config.curve_max_age_ms, ts_ms)
                if curve and not curve[1]:
                    price = curve[0]
                    tape.last_price = price
                    tape.peak_price = max(tape.peak_price or price, price)
                    tape.trough_price = min(tape.trough_price or price, price)
                    await self.manage_position(tape, ts_ms, price, source="heartbeat")
                    continue
                if pos.last_price <= 0:
                    continue
                stale_sec = max(0.0, (ts_ms - tape.last_seen_ms) / 1000.0)
                if stale_sec >= 3.0 and pos.age_sec(ts_ms) >= 5.0 and pos.peak_mult < 1.08:
                    self.broker.close(mint, ts_ms, pos.last_mult, "no_flow_after_entry")
                    continue
                if stale_sec >= 12.0 and pos.peak_mult < 1.25:
                    self.broker.close(mint, ts_ms, pos.last_mult, "tape_went_silent")
                    continue
            self.report_if_due()
            await asyncio.sleep(self.config.heartbeat_sec)

    def report_if_due(self, force: bool = False) -> None:
        if not force and time.time() - self.last_report_at < self.config.report_sec:
            return
        self.last_report_at = time.time()
        realized = self.broker.stats.realized_pnl_sol
        open_pnl = self.broker.mark_open_pnl()
        open_bits = []
        for mint, pos in self.broker.positions.items():
            open_bits.append(f"{short_mint(mint)} {pos.last_mult:.2f}x pk={pos.peak_mult:.2f}x")
        open_text = ", ".join(open_bits) if open_bits else "none"
        log(
            f"FLOW-STATUS entries={self.broker.stats.entries} closes={self.broker.stats.closes} "
            f"W/L={self.broker.stats.wins}/{self.broker.stats.losses} "
            f"realized={realized:+.5f} SOL open_pnl={open_pnl:+.5f} SOL "
            f"open={len(self.broker.positions)} [{open_text}] "
            f"shreds={self.broker.stats.shreds} trades={self.broker.stats.trades} "
            f"bc_updates={self.bc.updates}"
        )

    async def stream_loop(self) -> None:
        if not self.config.st_rpc_ws:
            raise RuntimeError("Missing SOLANATRACKER_RPC_WS or SOLANATRACKER_RPC_KEY")
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    self.config.st_rpc_ws,
                    ping_interval=20,
                    ping_timeout=60,
                    max_queue=4096,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    sub = {
                        "jsonrpc": "2.0",
                        "id": 41001,
                        "method": "shredSubscribe",
                        "params": [
                            {
                                "accountInclude": [PUMP_PROGRAM],
                                "accountRequired": [PUMP_PROGRAM],
                                "vote": False,
                            },
                            {
                                "encoding": "base64",
                                "transactionDetails": "full",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    }
                    bc_sub = {
                        "jsonrpc": "2.0",
                        "id": 41002,
                        "method": "programSubscribe",
                        "params": [
                            PUMP_PROGRAM,
                            {
                                "encoding": "base64",
                                "commitment": "processed",
                                "filters": [{"memcmp": {"offset": 0, "bytes": BC_DISC_B58}}],
                            },
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    await ws.send(json.dumps(bc_sub))
                    log("FLOW: subscribed to market-wide pump.fun shred tape + BondingCurve price cache")
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except asyncio.TimeoutError:
                            log("FLOW: no shred messages for 20s, reconnecting")
                            break
                        data = json.loads(raw)
                        method = str(data.get("method", "")).lower()
                        if "program" in method:
                            value = (((data.get("params") or {}).get("result") or {}).get("value") or {})
                            self.bc.update_from_program_value(value, now_ms())
                            continue
                        if "shred" not in method:
                            continue
                        result = ((data.get("params") or {}).get("result") or {})
                        sig = str(result.get("signature") or "")
                        if not sig:
                            continue
                        self.broker.stats.shreds += 1
                        for trade in parse_base64_shred_for_pump_trades(result):
                            if not self.dedup_trade(sig, trade):
                                continue
                            event = self.build_event(sig, trade)
                            if event:
                                await self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = 2
                log(f"FLOW: stream error, reconnecting in {delay}s: {type(exc).__name__}: {exc}")
                await asyncio.sleep(delay)

    async def run(self) -> None:
        mode = "PAPER" if self.config.paper_trading else "LIVE_DISABLED"
        log(
            f"FLOW: starting mode={mode} base={self.config.base_amount_sol:.4f} "
            f"max={self.config.max_amount_sol:.4f} max_open={self.config.max_open_positions}"
        )
        log(
            f"FLOW: tracked_wallets={len(self.tracked_wallets)} "
            f"alpha={'loaded' if self.alpha.loaded else 'missing'} "
            f"contexts={len(self.alpha.contexts)} alpha_file={self.config.alpha_file}"
        )
        if not self.config.paper_trading:
            raise RuntimeError("FLOW_PAPER_TRADING=0 is intentionally disabled in this first build")

        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, self.stop_event.set)
                except NotImplementedError:
                    pass

        heartbeat = asyncio.create_task(self.heartbeat_loop())
        stream = asyncio.create_task(self.stream_loop())
        deadline = time.time() + self.config.run_seconds if self.config.run_seconds > 0 else None
        try:
            while not self.stop_event.is_set():
                if deadline and time.time() >= deadline:
                    self.stop_event.set()
                    break
                await asyncio.sleep(0.2)
        finally:
            stream.cancel()
            heartbeat.cancel()
            await asyncio.gather(stream, heartbeat, return_exceptions=True)
            self.report_if_due(force=True)
            self.broker.save_state()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-first pump.fun market-flow follower bot")
    parser.add_argument("--ws", default="", help="Solana Tracker RPC WS URL")
    parser.add_argument("--state", default="", help="State JSON path")
    parser.add_argument("--alpha", default="", help="Executable alpha JSON path")
    parser.add_argument("--snipers", default="", help="Active sniper wallet file")
    parser.add_argument("--run-seconds", type=float, default=0.0, help="Stop after N seconds; 0 runs forever")
    parser.add_argument("--print-every-trade", action="store_true", help="Log every parsed pump trade")
    parser.add_argument("--alpha-report", action="store_true", help="Print top learned contexts and exit")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config = FlowConfig.from_env(args)
    bot = FlowFollowerBot(config)
    if args.alpha_report:
        bot.alpha.print_report()
        return
    await bot.run()


if __name__ == "__main__":
    asyncio.run(async_main())
