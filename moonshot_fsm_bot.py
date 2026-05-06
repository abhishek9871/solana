"""
Moonshot FSM Bot

Fresh paper-first pump.fun bot built from the two Deep Research reports.

This bot does not copy wallets and does not buy full size on first impulse.
It samples many launches with a tiny scout, scales only after an absorbed sell
and a post-absorption higher high, de-risks the first real pop, then lets a
financed runner work until flow turns toxic.

Default mode is paper. Live Raptor execution is intentionally represented by
interfaces but disabled until the paper gate passes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import signal
import statistics
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
BC_DISC = bytes([0x17, 0xB7, 0xF8, 0x37, 0x60, 0xD8, 0xAC, 0x60])
BC_DISC_B58 = "4y6pru6YvC7"
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])


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
    return value not in {"", "0", "false", "no", "off"}


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


def now_ns() -> int:
    return time.time_ns()


def short_mint(mint: str) -> str:
    if len(mint) <= 10:
        return mint
    return f"{mint[:4]}..{mint[-4:]}"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    if abs(b) <= 1e-12:
        return default
    return a / b


def percentile(sorted_values: list[float], q: float, default: float = 0.0) -> float:
    if not sorted_values:
        return default
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - pos) + sorted_values[hi] * (pos - lo)


def sigmoid(x: float) -> float:
    if x >= 60:
        return 1.0
    if x <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class BotConfig:
    paper_trading: bool = True
    st_rpc_ws: str = ""
    split_ws: bool = False
    enable_datastream: bool = False
    datastream_ws: str = ""
    scout_sol: float = 0.005
    max_position_sol: float = 0.05
    max_open_positions: int = 4
    max_armed: int = 70
    max_tape_age_sec: int = 180
    curve_max_age_ms: int = 750
    arm_ttl_ms: int = 1800
    scout_timeout_ms: int = 3800
    material_sell_timeout_ms: int = 1900
    cooldown_sec: float = 18.0
    min_seconds_between_scouts: float = 0.18
    heartbeat_sec: float = 0.05
    report_sec: float = 5.0
    shred_stall_reconnect_sec: float = 8.0
    paper_drag_bps: float = 250.0
    state_file: Path = DATA_DIR / "moonshot_fsm_state.json"
    raw_events_file: Path = DATA_DIR / "moonshot_raw_events.jsonl"
    candidates_file: Path = DATA_DIR / "moonshot_candidates.jsonl"
    snipers_file: Path = BASE_DIR / "active_snipers.txt"
    run_seconds: float = 0.0
    print_trades: bool = False

    @staticmethod
    def from_env(args: argparse.Namespace) -> "BotConfig":
        _load_dotenv()
        rpc_key = env_str("SOLANATRACKER_RPC_KEY")
        ws = env_str("SOLANATRACKER_RPC_WS")
        if not ws and rpc_key:
            ws = f"wss://rpc-mainnet.solanatracker.io?api_key={rpc_key}"

        data_key = env_str("SOLANATRACKER_API_KEY")
        ds_ws = env_str("MOONSHOT_DATASTREAM_WS")
        if not ds_ws and data_key:
            ds_ws = f"wss://datastream.solanatracker.io/{data_key}"

        return BotConfig(
            paper_trading=env_bool("MOONSHOT_PAPER_TRADING", True),
            st_rpc_ws=args.ws or ws,
            split_ws=env_bool("MOONSHOT_SPLIT_WS", False),
            enable_datastream=env_bool("MOONSHOT_ENABLE_DATASTREAM", False),
            datastream_ws=args.datastream_ws or ds_ws,
            scout_sol=env_float("MOONSHOT_SCOUT_SOL", 0.005),
            max_position_sol=env_float("MOONSHOT_MAX_POSITION_SOL", 0.05),
            max_open_positions=env_int("MOONSHOT_MAX_OPEN_POSITIONS", 4),
            max_armed=env_int("MOONSHOT_MAX_ARMED", 70),
            max_tape_age_sec=env_int("MOONSHOT_MAX_TAPE_AGE_SEC", 180),
            curve_max_age_ms=env_int("MOONSHOT_CURVE_MAX_AGE_MS", 750),
            arm_ttl_ms=env_int("MOONSHOT_ARM_TTL_MS", 1800),
            scout_timeout_ms=env_int("MOONSHOT_SCOUT_TIMEOUT_MS", 3800),
            material_sell_timeout_ms=env_int("MOONSHOT_MATERIAL_SELL_TIMEOUT_MS", 1900),
            cooldown_sec=env_float("MOONSHOT_COOLDOWN_SEC", 18.0),
            min_seconds_between_scouts=env_float("MOONSHOT_MIN_SECONDS_BETWEEN_SCOUTS", 0.18),
            heartbeat_sec=env_float("MOONSHOT_HEARTBEAT_SEC", 0.05),
            report_sec=env_float("MOONSHOT_REPORT_SEC", 5.0),
            shred_stall_reconnect_sec=env_float("MOONSHOT_SHRED_STALL_RECONNECT_SEC", 8.0),
            paper_drag_bps=env_float("MOONSHOT_PAPER_DRAG_BPS", 250.0),
            state_file=Path(args.state or env_str("MOONSHOT_STATE_FILE", str(DATA_DIR / "moonshot_fsm_state.json"))),
            raw_events_file=Path(args.raw_log or env_str("MOONSHOT_RAW_EVENTS_FILE", str(DATA_DIR / "moonshot_raw_events.jsonl"))),
            candidates_file=Path(args.candidate_log or env_str("MOONSHOT_CANDIDATES_FILE", str(DATA_DIR / "moonshot_candidates.jsonl"))),
            snipers_file=Path(args.snipers or env_str("MOONSHOT_SNIPERS_FILE", str(BASE_DIR / "active_snipers.txt"))),
            run_seconds=float(args.run_seconds or 0.0),
            print_trades=bool(args.print_trades),
        )


@dataclass(frozen=True)
class FlowEvent:
    ts_ms: int
    recv_ns: int
    sig: str
    slot: int
    signer: str
    mint: str
    is_buy: bool
    intent_sol: float
    token_amount: int
    price: float
    tracked: bool
    program: str


@dataclass
class CurvePoint:
    ts_ms: int
    vsol_lamports: int
    vtoken: int
    complete: bool

    @property
    def price(self) -> float:
        if self.vtoken <= 0:
            return 0.0
        return float(self.vsol_lamports) / float(self.vtoken)

    @property
    def vsol_sol(self) -> float:
        return float(self.vsol_lamports) / 1_000_000_000.0


class BondingCurveCache:
    def __init__(self) -> None:
        self.by_curve: dict[str, Deque[CurvePoint]] = defaultdict(lambda: deque(maxlen=160))
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
            self.by_curve[str(pubkey)].append(CurvePoint(ts_ms, vsol, vtoken, complete))
            self.updates += 1
            return True
        except Exception:
            self.decode_errors += 1
            return False

    def latest_for_mint(self, mint: str, max_age_ms: int, ts_ms: Optional[int] = None) -> Optional[CurvePoint]:
        curve = self.curve_for_mint(mint)
        if not curve:
            return None
        items = self.by_curve.get(curve)
        if not items:
            return None
        ts_ms = ts_ms or now_ms()
        latest = items[-1]
        if ts_ms - latest.ts_ms > max_age_ms:
            return None
        return latest

    def move_for_mint(self, mint: str, window_ms: int, max_age_ms: int, ts_ms: Optional[int] = None) -> Optional[tuple[float, int, bool]]:
        curve = self.curve_for_mint(mint)
        if not curve:
            return None
        items = list(self.by_curve.get(curve) or [])
        if len(items) < 2:
            return None
        ts_ms = ts_ms or now_ms()
        latest = items[-1]
        latest_age = ts_ms - latest.ts_ms
        if latest_age > max_age_ms or latest.price <= 0:
            return None
        cutoff = ts_ms - window_ms
        recent = [item for item in items if item.ts_ms >= cutoff]
        if len(recent) < 2 or recent[0].price <= 0:
            return None
        return latest.price / recent[0].price, int(latest_age), latest.complete

    def points_for_mint(self, mint: str, window_ms: int, ts_ms: int) -> list[CurvePoint]:
        curve = self.curve_for_mint(mint)
        if not curve:
            return []
        cutoff = ts_ms - window_ms
        return [p for p in (self.by_curve.get(curve) or []) if p.ts_ms >= cutoff]


@dataclass
class WindowStats:
    window_ms: int
    events: int = 0
    buys: int = 0
    sells: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    net_sol: float = 0.0
    unique_buyers: int = 0
    unique_sellers: int = 0
    tracked_buyers: int = 0
    first_price: float = 0.0
    last_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    top_buy_share: float = 0.0
    buyer_hhi: float = 0.0
    median_buy_sol: float = 0.0
    p90_buy_sol: float = 0.0
    max_buy_sol: float = 0.0
    usr: float = 0.0
    f_lt_50ms: float = 0.0
    interarrival_cv: float = 0.0
    top_buyer_flip: float = 0.0

    @property
    def buy_pressure(self) -> float:
        return self.buy_sol / max(self.sell_sol, 0.001)

    @property
    def sell_ratio(self) -> float:
        return self.sell_sol / max(self.buy_sol, 0.001)

    @property
    def price_change(self) -> float:
        if self.first_price <= 0 or self.last_price <= 0:
            return 0.0
        return self.last_price / self.first_price - 1.0

    @property
    def price_range(self) -> float:
        if self.low_price <= 0 or self.high_price <= 0:
            return 0.0
        return self.high_price / self.low_price - 1.0


@dataclass
class MintTape:
    mint: str
    events: Deque[FlowEvent] = field(default_factory=deque)
    price_points: Deque[tuple[int, float]] = field(default_factory=deque)
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    peak_price: float = 0.0
    peak_ts_ms: int = 0
    trough_price: float = 0.0
    last_price: float = 0.0

    def add_event(self, event: FlowEvent, max_age_sec: int) -> None:
        if not self.first_seen_ms:
            self.first_seen_ms = event.ts_ms
        self.last_seen_ms = event.ts_ms
        self.events.append(event)
        if event.price > 0:
            self.add_price(event.ts_ms, event.price, max_age_sec)
        self.prune(event.ts_ms, max_age_sec)

    def add_price(self, ts_ms: int, price: float, max_age_sec: int) -> None:
        if price <= 0:
            return
        self.last_price = price
        self.price_points.append((ts_ms, price))
        if self.peak_price <= 0 or price > self.peak_price:
            self.peak_price = price
            self.peak_ts_ms = ts_ms
        if self.trough_price <= 0 or price < self.trough_price:
            self.trough_price = price
        self.prune(ts_ms, max_age_sec)

    def prune(self, ts_ms: int, max_age_sec: int) -> None:
        cutoff = ts_ms - max_age_sec * 1000
        while self.events and self.events[0].ts_ms < cutoff:
            self.events.popleft()
        while self.price_points and self.price_points[0][0] < cutoff:
            self.price_points.popleft()

    def age_sec(self, ts_ms: int) -> float:
        if not self.first_seen_ms:
            return 0.0
        return max(0.0, (ts_ms - self.first_seen_ms) / 1000.0)

    def off_peak(self) -> float:
        if self.peak_price <= 0 or self.last_price <= 0:
            return 1.0
        return self.last_price / self.peak_price

    def time_since_peak_sec(self, ts_ms: int) -> float:
        if not self.peak_ts_ms:
            return 0.0
        return max(0.0, (ts_ms - self.peak_ts_ms) / 1000.0)

    def stats(self, window_ms: int, ts_ms: int) -> WindowStats:
        cutoff = ts_ms - window_ms
        selected = [e for e in self.events if e.ts_ms >= cutoff]
        prices = [p for t, p in self.price_points if t >= cutoff and p > 0]
        out = WindowStats(window_ms=window_ms, events=len(selected))
        if prices:
            out.first_price = prices[0]
            out.last_price = prices[-1]
            out.high_price = max(prices)
            out.low_price = min(prices)
        if not selected:
            return out

        buys = [e for e in selected if e.is_buy]
        sells = [e for e in selected if not e.is_buy]
        out.buys = len(buys)
        out.sells = len(sells)
        out.buy_sol = sum(e.intent_sol for e in buys)
        out.sell_sol = sum(e.intent_sol for e in sells)
        out.net_sol = out.buy_sol - out.sell_sol
        out.unique_buyers = len({e.signer for e in buys if e.signer})
        out.unique_sellers = len({e.signer for e in sells if e.signer})
        out.tracked_buyers = len({e.signer for e in buys if e.tracked and e.signer})
        out.usr = len({e.signer for e in selected if e.signer}) / max(len(selected), 1)

        buy_by_wallet: dict[str, float] = defaultdict(float)
        sell_by_wallet: dict[str, float] = defaultdict(float)
        buy_sizes: list[float] = []
        buy_times: list[int] = []
        for e in buys:
            buy_by_wallet[e.signer] += e.intent_sol
            buy_sizes.append(e.intent_sol)
            buy_times.append(e.ts_ms)
        for e in sells:
            sell_by_wallet[e.signer] += e.intent_sol
        if out.buy_sol > 0 and buy_by_wallet:
            shares = [v / out.buy_sol for v in buy_by_wallet.values()]
            out.top_buy_share = max(shares)
            out.buyer_hhi = sum(s * s for s in shares)
        if buy_sizes:
            sorted_sizes = sorted(buy_sizes)
            out.median_buy_sol = statistics.median(sorted_sizes)
            out.p90_buy_sol = percentile(sorted_sizes, 0.90)
            out.max_buy_sol = sorted_sizes[-1]
        if len(buy_times) >= 2:
            gaps = [b - a for a, b in zip(buy_times, buy_times[1:]) if b >= a]
            if gaps:
                out.f_lt_50ms = sum(1 for g in gaps if g < 50) / len(gaps)
                mean_gap = statistics.mean(gaps)
                if mean_gap > 0 and len(gaps) >= 2:
                    out.interarrival_cv = statistics.pstdev(gaps) / mean_gap
        if buy_by_wallet:
            top_buyers = sorted(buy_by_wallet.items(), key=lambda x: x[1], reverse=True)[:3]
            top_bought = sum(v for _, v in top_buyers)
            top_sold = sum(sell_by_wallet.get(w, 0.0) for w, _ in top_buyers)
            out.top_buyer_flip = top_sold / max(top_bought, 0.001)
        return out


@dataclass
class ActorMintState:
    mint: str
    totals: dict[str, float] = field(default_factory=dict)
    deltas: dict[str, float] = field(default_factory=dict)
    updated_ms: int = 0

    def update_total(self, key: str, total: float, previous: Optional[float], ts_ms: int) -> None:
        old = self.totals.get(key, previous if previous is not None else total)
        self.totals[key] = total
        self.deltas[key] = total - old
        self.updated_ms = ts_ms

    def hard_distribution_reason(self) -> Optional[str]:
        for key in ("dev", "insider", "bundler"):
            if self.deltas.get(key, 0.0) < -0.05:
                return f"actor_{key}_unload"
        top10_delta = self.deltas.get("top10", 0.0)
        if top10_delta < -0.50:
            return "actor_top10_distribution"
        return None


class ActorFlowStream:
    """Optional Datastream adapter.

    The current Solana Tracker plan does not include Datastream. The adapter is
    present so the bot can use it immediately if the account is upgraded, while
    the default production path still works without it.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.desired_rooms: set[str] = set()
        self.active_rooms: set[str] = set()
        self.states: dict[str, ActorMintState] = {}
        self.stop_event: Optional[asyncio.Event] = None
        self.messages = 0
        self.errors = 0

    @staticmethod
    def rooms_for_mint(mint: str) -> list[str]:
        return [
            f"top10:{mint}",
            f"sniper:{mint}",
            f"bundlers:{mint}",
            f"insiders:{mint}",
        ]

    def join_mint(self, mint: str) -> None:
        if not self.config.enable_datastream:
            return
        for room in self.rooms_for_mint(mint):
            self.desired_rooms.add(room)

    def leave_mint(self, mint: str) -> None:
        if not self.config.enable_datastream:
            return
        for room in self.rooms_for_mint(mint):
            self.desired_rooms.discard(room)

    def state_for(self, mint: str) -> ActorMintState:
        state = self.states.get(mint)
        if state is None:
            state = ActorMintState(mint=mint)
            self.states[mint] = state
        return state

    async def run(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        if not self.config.enable_datastream:
            return
        if not self.config.datastream_ws:
            log("ACTOR: datastream enabled but no URL/key configured")
            return
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    self.config.datastream_ws,
                    ping_interval=20,
                    ping_timeout=45,
                    max_queue=1024,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    log("ACTOR: datastream connected")
                    while not stop_event.is_set():
                        await self.sync_rooms(ws)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                        except asyncio.TimeoutError:
                            continue
                        self.messages += 1
                        self.handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                log(f"ACTOR: stream error, reconnecting: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def sync_rooms(self, ws: websockets.WebSocketClientProtocol) -> None:
        for room in sorted(self.desired_rooms - self.active_rooms):
            await ws.send(json.dumps({"type": "join", "room": room}))
            self.active_rooms.add(room)
            log(f"ACTOR-JOIN {room}")
        for room in sorted(self.active_rooms - self.desired_rooms):
            await ws.send(json.dumps({"type": "leave", "room": room}))
            self.active_rooms.discard(room)
            log(f"ACTOR-LEAVE {room}")

    def handle_message(self, raw: str | bytes) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            data = json.loads(raw)
        except Exception:
            return
        room = str(data.get("room") or data.get("channel") or "")
        payload = data.get("data") or data.get("payload") or data
        if not room or ":" not in room:
            return
        kind, mint = room.split(":", 1)
        state = self.state_for(mint)
        ts_ms = now_ms()
        if kind == "top10":
            total = float(payload.get("totalPercentage") or 0.0)
            prev_raw = payload.get("previousPercentage")
            prev = float(prev_raw) if prev_raw is not None else None
            state.update_total("top10", total, prev, ts_ms)
            return
        if kind == "sniper":
            total = float(payload.get("totalSniperPercentage") or payload.get("percentage") or 0.0)
            prev_raw = payload.get("previousPercentage")
            prev = float(prev_raw) if prev_raw is not None else None
            state.update_total("sniper", total, prev, ts_ms)
            return
        if kind == "bundlers":
            total = float(payload.get("totalPercentage") or payload.get("percentage") or 0.0)
            prev_raw = payload.get("previousPercentage")
            prev = float(prev_raw) if prev_raw is not None else None
            state.update_total("bundler", total, prev, ts_ms)
            return
        if kind == "insiders":
            total = float(payload.get("totalInsiderPercentage") or payload.get("percentage") or 0.0)
            prev_raw = payload.get("previousPercentage")
            prev = float(prev_raw) if prev_raw is not None else None
            state.update_total("insider", total, prev, ts_ms)


class EventLogger:
    def __init__(self, config: BotConfig):
        self.config = config
        self.raw_path = config.raw_events_file
        self.candidate_path = config.candidates_file
        self.raw_writes = 0
        self.candidate_writes = 0
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.candidate_path.parent.mkdir(parents=True, exist_ok=True)

    def write_raw(self, event: FlowEvent, curve: Optional[CurvePoint]) -> None:
        row = {
            "kind": "trade",
            "ts_ms": event.ts_ms,
            "recv_ns": event.recv_ns,
            "sig": event.sig,
            "slot": event.slot,
            "mint": event.mint,
            "signer": event.signer,
            "side": "buy" if event.is_buy else "sell",
            "intent_sol": event.intent_sol,
            "token_amount": event.token_amount,
            "tracked": event.tracked,
            "curve_price": curve.price if curve else 0.0,
            "vsol_sol": curve.vsol_sol if curve else 0.0,
            "complete": curve.complete if curve else False,
            "source": "SHRED",
        }
        self._append(self.raw_path, row)
        self.raw_writes += 1

    def write_candidate(self, kind: str, mint: str, state: str, payload: dict[str, Any]) -> None:
        row = {
            "kind": kind,
            "ts_ms": now_ms(),
            "mint": mint,
            "state": state,
            **payload,
        }
        self._append(self.candidate_path, row)
        self.candidate_writes += 1

    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n")
        except Exception as exc:
            log(f"LOGGER: write failed {path}: {type(exc).__name__}: {exc}")


@dataclass
class Candidate:
    mint: str
    state: str
    created_ts_ms: int
    arm_ts_ms: int
    expires_ts_ms: int
    arm_price: float
    arm_score: float
    arm_reason: str
    target_sol: float
    scout_ts_ms: int = 0
    scout_price: float = 0.0
    post_scout_high: float = 0.0
    pending_sell_ts_ms: int = 0
    pending_sell_ref_high: float = 0.0
    absorbed_sells: int = 0
    failed_highs: int = 0
    clean_runway_scales: int = 0
    last_absorbed_ts_ms: int = 0
    last_absorbed_price: float = 0.0
    last_transition_ts_ms: int = 0


@dataclass
class PaperPosition:
    mint: str
    state: str
    opened_ts_ms: int
    avg_price: float
    tokens_bought: float
    remaining_tokens: float
    cost_sol: float
    scout_sol: float
    target_sol: float
    realized_sol: float = 0.0
    peak_price: float = 0.0
    peak_mult: float = 1.0
    last_price: float = 0.0
    last_mult: float = 1.0
    scale1_done: bool = False
    scale2_done: bool = False
    derisk_done: bool = False
    warmed_sell_ready: bool = False

    def age_sec(self, ts_ms: int) -> float:
        return max(0.0, (ts_ms - self.opened_ts_ms) / 1000.0)

    def update(self, price: float) -> float:
        if price <= 0 or self.avg_price <= 0:
            return self.last_mult
        self.last_price = price
        self.last_mult = price / self.avg_price
        if self.peak_price <= 0 or price > self.peak_price:
            self.peak_price = price
        self.peak_mult = max(self.peak_mult, self.last_mult)
        return self.last_mult

    def mark_pnl_sol(self, price: Optional[float] = None) -> float:
        mark = price if price and price > 0 else self.last_price
        return self.realized_sol + self.remaining_tokens * mark - self.cost_sol


@dataclass
class SessionStats:
    shreds: int = 0
    trades: int = 0
    buys: int = 0
    sells: int = 0
    shred_reconnects: int = 0
    curve_reconnects: int = 0
    arms: int = 0
    arm_cancels: int = 0
    scouts: int = 0
    scale1: int = 0
    scale2: int = 0
    partials: int = 0
    closes: int = 0
    kills: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl_sol: float = 0.0
    best_mult: float = 1.0
    started_at: float = field(default_factory=time.time)


class PaperExecutor:
    def __init__(self, config: BotConfig):
        self.config = config
        self.positions: dict[str, PaperPosition] = {}
        self.stats = SessionStats()
        self.closed_recent: dict[str, int] = {}
        self.last_scout_ts_ms = 0
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
                base = asdict(PaperPosition(
                    mint=mint,
                    state="SCOUT_FILLED",
                    opened_ts_ms=0,
                    avg_price=0.0,
                    tokens_bought=0.0,
                    remaining_tokens=0.0,
                    cost_sol=0.0,
                    scout_sol=0.0,
                    target_sol=0.0,
                ))
                base.update(raw)
                self.positions[mint] = PaperPosition(**base)
            log(f"STATE: restored {len(self.positions)} moonshot positions from {path}")
        except Exception as exc:
            log(f"STATE: restore failed {path}: {type(exc).__name__}: {exc}")

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

    def can_scout(self, mint: str, ts_ms: int) -> bool:
        if mint in self.positions:
            return False
        if len(self.positions) >= self.config.max_open_positions:
            return False
        if ts_ms - self.last_scout_ts_ms < int(self.config.min_seconds_between_scouts * 1000):
            return False
        last_closed = self.closed_recent.get(mint, 0)
        if last_closed and ts_ms - last_closed < int(self.config.cooldown_sec * 1000):
            return False
        return True

    def open_scout(self, candidate: Candidate, price: float, ts_ms: int) -> Optional[PaperPosition]:
        if price <= 0 or not self.can_scout(candidate.mint, ts_ms):
            return None
        amount = min(self.config.scout_sol, candidate.target_sol * 0.20)
        amount = max(0.001, amount)
        fill_price = price * (1.0 + self.drag)
        tokens = amount / fill_price
        pos = PaperPosition(
            mint=candidate.mint,
            state="SCOUT_FILLED",
            opened_ts_ms=ts_ms,
            avg_price=fill_price,
            tokens_bought=tokens,
            remaining_tokens=tokens,
            cost_sol=amount,
            scout_sol=amount,
            target_sol=candidate.target_sol,
            peak_price=fill_price,
            last_price=fill_price,
            warmed_sell_ready=True,
        )
        self.positions[candidate.mint] = pos
        self.last_scout_ts_ms = ts_ms
        self.stats.scouts += 1
        log(
            f"MOON-SCOUT {short_mint(candidate.mint)} amount={amount:.4f} SOL "
            f"target={candidate.target_sol:.4f} score={candidate.arm_score:.1f} "
            f"fill={fill_price:.6e} reason={candidate.arm_reason}"
        )
        self.save_state()
        return pos

    def scale(self, mint: str, add_sol: float, price: float, state: str, reason: str) -> Optional[PaperPosition]:
        pos = self.positions.get(mint)
        if not pos or add_sol <= 0 or price <= 0:
            return None
        add_sol = min(add_sol, max(0.0, self.config.max_position_sol - pos.cost_sol))
        if add_sol <= 0:
            return None
        fill_price = price * (1.0 + self.drag)
        add_tokens = add_sol / fill_price
        pos.cost_sol += add_sol
        pos.tokens_bought += add_tokens
        pos.remaining_tokens += add_tokens
        pos.avg_price = pos.cost_sol / max(pos.tokens_bought, 1e-18)
        pos.state = state
        pos.update(price)
        if state == "SCALE1_FILLED":
            pos.scale1_done = True
            self.stats.scale1 += 1
        elif state == "RUNNER_FULL":
            pos.scale2_done = True
            self.stats.scale2 += 1
        log(
            f"MOON-SCALE {short_mint(mint)} state={state} add={add_sol:.4f} SOL "
            f"cost={pos.cost_sol:.4f} avg={pos.avg_price:.6e} mult={pos.last_mult:.3f}x "
            f"reason={reason}"
        )
        self.save_state()
        return pos

    def partial(self, mint: str, fraction_of_remaining: float, price: float, reason: str) -> Optional[PaperPosition]:
        pos = self.positions.get(mint)
        if not pos or price <= 0:
            return None
        fraction = max(0.0, min(1.0, fraction_of_remaining))
        tokens = pos.remaining_tokens * fraction
        if tokens <= 0:
            return None
        fill_price = price * (1.0 - self.drag)
        proceeds = tokens * fill_price
        pos.remaining_tokens -= tokens
        pos.realized_sol += proceeds
        pos.derisk_done = True
        pos.state = "RUNNER"
        pos.update(price)
        self.stats.partials += 1
        log(
            f"MOON-DERISK {short_mint(mint)} sold={fraction * 100:.1f}% "
            f"proceeds={proceeds:.5f} SOL mult={pos.last_mult:.3f}x "
            f"rem_tokens={pos.remaining_tokens:.2f} reason={reason}"
        )
        self.save_state()
        return pos

    def close(self, mint: str, ts_ms: int, price: float, reason: str, killed: bool = False) -> Optional[float]:
        pos = self.positions.pop(mint, None)
        if not pos:
            return None
        fill_price = max(price, 0.0) * (1.0 - self.drag)
        proceeds = pos.remaining_tokens * fill_price
        total_out = pos.realized_sol + proceeds
        pnl = total_out - pos.cost_sol
        self.stats.realized_pnl_sol += pnl
        self.stats.closes += 1
        if killed:
            self.stats.kills += 1
        if pnl >= 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.closed_recent[mint] = ts_ms
        log(
            f"MOON-CLOSE {short_mint(mint)} reason={reason} age={pos.age_sec(ts_ms):.2f}s "
            f"state={pos.state} mult={pos.last_mult:.3f}x peak={pos.peak_mult:.3f}x "
            f"pnl={pnl:+.5f} SOL session={self.stats.realized_pnl_sol:+.5f} SOL"
        )
        self.save_state()
        return pnl

    def open_pnl(self) -> float:
        return sum(pos.mark_pnl_sol() for pos in self.positions.values())


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
        slot = int(shred_result.get("slot") or 0)
        for ix in vt.message.instructions:
            try:
                program = str(keys[ix.program_id_index])
            except Exception:
                continue
            if program != PUMP_PROGRAM:
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
            token_amount = int.from_bytes(data[8:16], "little")
            sol_lamports = int.from_bytes(data[16:24], "little")
            if token_amount <= 0 or sol_lamports <= 0:
                continue
            try:
                mint_index = ix.accounts[2]
            except Exception:
                continue
            if mint_index >= len(keys):
                continue
            trades.append(
                {
                    "slot": slot,
                    "signer": signer,
                    "mint": str(keys[mint_index]),
                    "is_buy": is_buy,
                    "token_amount": token_amount,
                    "intent_lamports": sol_lamports,
                    "program": program,
                }
            )
    except Exception:
        return trades
    return trades


class MoonshotFsmBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.bc = BondingCurveCache()
        self.broker = PaperExecutor(config)
        self.actor = ActorFlowStream(config)
        self.logger = EventLogger(config)
        self.tapes: dict[str, MintTape] = {}
        self.candidates: dict[str, Candidate] = {}
        self.tracked_wallets = self.load_snipers(config.snipers_file)
        self.stop_event = asyncio.Event()
        self.seen_trade_keys: Deque[tuple[str, str, bool, int, int]] = deque()
        self.seen_trade_key_set: set[tuple[str, str, bool, int, int]] = set()
        self.seen_trade_limit = 25000
        self.last_report_at = time.time()
        ts_ms = now_ms()
        self.last_shred_msg_ms = ts_ms
        self.last_curve_msg_ms = ts_ms

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
                wallets.add(line.split()[0])
        except Exception as exc:
            log(f"SNIPERS: failed to load {path}: {type(exc).__name__}: {exc}")
        return wallets

    def dedup_trade(self, sig: str, trade: dict[str, Any]) -> bool:
        key = (
            sig,
            str(trade.get("mint") or ""),
            bool(trade.get("is_buy")),
            int(trade.get("token_amount") or 0),
            int(trade.get("intent_lamports") or 0),
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
        signer = str(trade.get("signer") or "")
        if not mint or not signer:
            return None
        ts_ms = now_ms()
        curve = self.bc.latest_for_mint(mint, self.config.curve_max_age_ms, ts_ms)
        price = curve.price if curve and not curve.complete else 0.0
        return FlowEvent(
            ts_ms=ts_ms,
            recv_ns=now_ns(),
            sig=sig,
            slot=int(trade.get("slot") or 0),
            signer=signer,
            mint=mint,
            is_buy=bool(trade.get("is_buy")),
            intent_sol=int(trade.get("intent_lamports") or 0) / 1_000_000_000.0,
            token_amount=int(trade.get("token_amount") or 0),
            price=price,
            tracked=signer in self.tracked_wallets,
            program=str(trade.get("program") or ""),
        )

    def feature_snapshot(self, mint: str, ts_ms: int) -> Optional[dict[str, Any]]:
        tape = self.tapes.get(mint)
        if not tape:
            return None
        curve = self.bc.latest_for_mint(mint, self.config.curve_max_age_ms, ts_ms)
        if not curve or curve.price <= 0:
            return None

        s200 = tape.stats(200, ts_ms)
        s500 = tape.stats(500, ts_ms)
        s1 = tape.stats(1000, ts_ms)
        s2 = tape.stats(2000, ts_ms)
        s5 = tape.stats(5000, ts_ms)
        s15 = tape.stats(15000, ts_ms)
        s30 = tape.stats(30000, ts_ms)
        m200 = self.bc.move_for_mint(mint, 200, self.config.curve_max_age_ms, ts_ms)
        m1 = self.bc.move_for_mint(mint, 1000, self.config.curve_max_age_ms, ts_ms)
        m2 = self.bc.move_for_mint(mint, 2000, self.config.curve_max_age_ms, ts_ms)
        m5 = self.bc.move_for_mint(mint, 5000, self.config.curve_max_age_ms, ts_ms)

        move200 = m200[0] if m200 else 1.0
        move1 = m1[0] if m1 else 1.0
        move2 = m2[0] if m2 else 1.0
        move5 = m5[0] if m5 else 1.0
        v1 = math.log(max(move1, 1e-9))
        v2 = math.log(max(move2, 1e-9)) / 2.0
        accel = math.log(max(move200, 1e-9)) * 5.0 - v1
        y = max(curve.vsol_sol, 0.001)

        n_ofi_1 = (s1.buy_sol - s1.sell_sol) / y
        n_ofi_2 = (s2.buy_sol - s2.sell_sol) / y
        ar_sell = self.compute_ar_sell(s2, y)
        ipd = self.compute_ipd(s5, s30)
        compression = safe_div(s5.price_range, s15.price_range, 1.0)
        actor_state = self.actor.states.get(mint)
        actor_reason = actor_state.hard_distribution_reason() if actor_state else None

        breadth = min(1.0, s2.unique_buyers / 5.0)
        reserve_speed = max(0.0, math.log(max(move1, 1e-9))) * 180.0
        ofi_score = max(0.0, n_ofi_2) * 900.0
        width_score = max(0.0, 1.0 - s2.top_buy_share) * 16.0
        churn_penalty = max(0.0, s2.f_lt_50ms - 0.35) * 35.0 + max(0.0, s2.top_buy_share - 0.82) * 28.0
        sell_penalty = max(0.0, s2.sell_ratio - 0.55) * 20.0 + max(0.0, ar_sell - 0.75) * 16.0
        tracked_bonus = min(12.0, s2.tracked_buyers * 4.0)
        impulse_score = 28.0 + reserve_speed + ofi_score + breadth * 18.0 + width_score + tracked_bonus - churn_penalty - sell_penalty
        prob2x = sigmoid((impulse_score - 52.0) / 11.0)

        return {
            "ts_ms": ts_ms,
            "price": curve.price,
            "complete": curve.complete,
            "vsol_sol": curve.vsol_sol,
            "curve_age_ms": ts_ms - curve.ts_ms,
            "age_sec": tape.age_sec(ts_ms),
            "move200": move200,
            "move1": move1,
            "move2": move2,
            "move5": move5,
            "v1": v1,
            "v2": v2,
            "accel": accel,
            "n_ofi_1": n_ofi_1,
            "n_ofi_2": n_ofi_2,
            "ar_sell_2": ar_sell,
            "ipd": ipd,
            "compression": compression,
            "time_since_high": tape.time_since_peak_sec(ts_ms),
            "off_peak": tape.off_peak(),
            "actor_reason": actor_reason or "",
            "impulse_score": impulse_score,
            "prob2x": prob2x,
            "s200": asdict(s200),
            "s500": asdict(s500),
            "s1": asdict(s1),
            "s2": asdict(s2),
            "s5": asdict(s5),
            "s15": asdict(s15),
            "s30": asdict(s30),
        }

    @staticmethod
    def compute_ar_sell(stats: WindowStats, y_sol: float) -> float:
        if stats.sell_sol <= 0 or stats.first_price <= 0 or stats.last_price <= 0:
            return 0.0
        sell_frac = min(0.85, stats.sell_sol / max(y_sol, 0.001))
        expected = 2.0 * math.log(max(0.001, 1.0 - sell_frac))
        real = math.log(max(stats.last_price / stats.first_price, 1e-9))
        if real >= 0:
            return 0.0
        if expected >= 0:
            return 1.0
        return max(0.0, real / expected)

    @staticmethod
    def compute_ipd(stats5: WindowStats, stats30: WindowStats) -> float:
        impact5 = max(0.0, stats5.price_change) / max(stats5.buy_sol, 0.001)
        impact30 = max(0.0, stats30.price_change) / max(stats30.buy_sol, 0.001)
        if impact30 <= 0:
            return 1.0 if impact5 > 0 else 0.0
        return impact5 / impact30

    def stage_a_gate(self, features: dict[str, Any], event: FlowEvent) -> tuple[bool, str]:
        s500 = features["s500"]
        s1 = features["s1"]
        s2 = features["s2"]
        s5 = features["s5"]
        if features["complete"]:
            return False, "complete"
        if features["age_sec"] > 90.0 and features["off_peak"] < 0.88:
            return False, "old_dumped"
        if s2["sell_sol"] > max(0.035, s2["buy_sol"] * 0.90) and features["move1"] < 0.998:
            return False, "sell_flip"
        if s2["f_lt_50ms"] > 0.45 and s2["usr"] < 0.42:
            return False, "same_template_burst"
        if s2["top_buy_share"] > 0.86 and s2["unique_buyers"] < 3 and not event.tracked:
            return False, "one_wallet_lift"
        if features["actor_reason"]:
            return False, features["actor_reason"]

        broad_impulse = (
            s500["buy_sol"] >= 0.018
            and s2["buy_sol"] >= 0.045
            and s2["unique_buyers"] >= 2
            and features["move1"] >= 1.001
            and features["n_ofi_2"] > 0
        )
        sell_ratio2 = s2["sell_sol"] / max(s2["buy_sol"], 0.001)
        tracked_impulse = (
            s2["tracked_buyers"] >= 1
            and s2["buy_sol"] >= 0.025
            and features["move1"] >= 1.0005
            and sell_ratio2 < 0.75
        )
        whale_with_width = (
            event.intent_sol >= 0.25
            and s2["unique_buyers"] >= 2
            and features["move200"] >= 1.0005
            and s1["sell_sol"] <= max(0.02, s1["buy_sol"] * 0.45)
        )
        fresh_ladder = (
            features["age_sec"] <= 12.0
            and s5["unique_buyers"] >= 4
            and s5["buy_sol"] >= 0.10
            and features["move2"] >= 1.0025
            and s5["top_buy_share"] <= 0.68
        )
        if broad_impulse or tracked_impulse or whale_with_width or fresh_ladder:
            return True, (
                "fresh_ladder" if fresh_ladder else
                "tracked_impulse" if tracked_impulse else
                "whale_width" if whale_with_width else
                "broad_impulse"
            )
        return False, "no_a_grade_impulse"

    def scout_gate(self, candidate: Candidate, features: dict[str, Any]) -> tuple[bool, str]:
        s2 = features["s2"]
        if features["complete"]:
            return False, "complete"
        if features["price"] <= 0:
            return False, "no_price"
        if features["actor_reason"]:
            return False, features["actor_reason"]
        if features["move1"] < 1.0005 and features["move200"] < 1.0002:
            return False, "curve_not_live"
        if features["n_ofi_2"] < -0.0005:
            return False, "negative_ofi"
        sell_ratio2 = s2["sell_sol"] / max(s2["buy_sol"], 0.001)
        if sell_ratio2 > 0.95:
            return False, "sell_ratio"
        if features["prob2x"] < 0.33 and s2["tracked_buyers"] <= 0:
            return False, "prob_low"
        return True, "scout_live_impulse"

    def distribution_reason(
        self,
        candidate: Optional[Candidate],
        pos: Optional[PaperPosition],
        features: dict[str, Any],
    ) -> Optional[str]:
        s2 = features["s2"]
        s5 = features["s5"]
        if features["actor_reason"]:
            return str(features["actor_reason"])
        if s2["sell_sol"] > 0.015 and features["ar_sell_2"] > 1.05:
            return "unabsorbed_sell"
        if s2["top_buyer_flip"] >= 0.42:
            return "top_buyer_flip"
        if (
            features["age_sec"] >= 6.0
            and features["ipd"] < 0.45
            and s5["buy_sol"] > max(0.08, features["s30"]["buy_sol"] / 8.0)
        ):
            return "impact_decay"
        if features["compression"] < 0.32 and s5["buy_sol"] > 0.12 and features["move5"] < 1.015:
            return "range_compression"
        if candidate and candidate.failed_highs >= 2:
            return "failed_highs"
        if pos and pos.last_mult >= 1.20 and features["time_since_high"] > 8.0 and s5["buy_sol"] > 0.06:
            return "stale_high_distribution"
        return None

    def target_size(self, features: dict[str, Any]) -> float:
        y_cap = max(0.002, features["vsol_sol"] * 0.005)
        p = float(features["prob2x"])
        # Conservative fractional sizing proxy until the offline classifier exists.
        quality = max(0.0, min(1.0, (p - 0.32) / 0.45))
        target = self.config.scout_sol + (self.config.max_position_sol - self.config.scout_sol) * quality
        return max(self.config.scout_sol, min(self.config.max_position_sol, y_cap, target))

    async def on_event(self, event: FlowEvent) -> None:
        self.broker.stats.trades += 1
        if event.is_buy:
            self.broker.stats.buys += 1
        else:
            self.broker.stats.sells += 1

        tape = self.tapes.get(event.mint)
        if tape is None:
            tape = MintTape(event.mint)
            self.tapes[event.mint] = tape
        tape.add_event(event, self.config.max_tape_age_sec)
        curve = self.bc.latest_for_mint(event.mint, self.config.curve_max_age_ms, event.ts_ms)
        self.logger.write_raw(event, curve)

        if self.config.print_trades and event.price > 0:
            side = "B" if event.is_buy else "S"
            log(
                f"MOON-TAPE {side} {short_mint(event.mint)} intent={event.intent_sol:.4f} "
                f"tracked={int(event.tracked)} price={event.price:.6e}"
            )

        features = self.feature_snapshot(event.mint, event.ts_ms)
        if not features:
            return

        candidate = self.candidates.get(event.mint)
        pos = self.broker.positions.get(event.mint)
        if candidate or pos:
            await self.manage_mint(event.mint, event.ts_ms, features, event)
            return

        if not event.is_buy:
            return
        if not self.broker.can_scout(event.mint, event.ts_ms):
            return
        if len(self.candidates) >= self.config.max_armed:
            self.prune_candidates(event.ts_ms)
            if len(self.candidates) >= self.config.max_armed:
                return

        ok, reason = self.stage_a_gate(features, event)
        if not ok:
            return
        self.arm_candidate(event.mint, event.ts_ms, features, reason)
        await self.manage_mint(event.mint, event.ts_ms, features, event)

    def arm_candidate(self, mint: str, ts_ms: int, features: dict[str, Any], reason: str) -> Candidate:
        target = self.target_size(features)
        candidate = Candidate(
            mint=mint,
            state="ARMED",
            created_ts_ms=ts_ms,
            arm_ts_ms=ts_ms,
            expires_ts_ms=ts_ms + self.config.arm_ttl_ms,
            arm_price=float(features["price"]),
            arm_score=float(features["impulse_score"]),
            arm_reason=reason,
            target_sol=target,
            last_transition_ts_ms=ts_ms,
        )
        self.candidates[mint] = candidate
        self.actor.join_mint(mint)
        self.broker.stats.arms += 1
        log(
            f"MOON-ARM {short_mint(mint)} score={features['impulse_score']:.1f} "
            f"p2x={features['prob2x']:.2f} target={target:.4f} "
            f"buy2={features['s2']['buy_sol']:.3f} sell2={features['s2']['sell_sol']:.3f} "
            f"uniq2={features['s2']['unique_buyers']} reason={reason}"
        )
        self.logger.write_candidate("arm", mint, "ARMED", {"features": self.slim_features(features), "reason": reason, "target_sol": target})
        return candidate

    async def manage_mint(
        self,
        mint: str,
        ts_ms: int,
        features: dict[str, Any],
        event: Optional[FlowEvent],
    ) -> None:
        candidate = self.candidates.get(mint)
        pos = self.broker.positions.get(mint)
        price = float(features["price"])
        if candidate and pos:
            self.update_absorption(candidate, ts_ms, price, features, event)
        if pos:
            pos.update(price)
            self.broker.stats.best_mult = max(self.broker.stats.best_mult, pos.peak_mult)
            await self.manage_position(candidate, pos, ts_ms, price, features)
            return
        if not candidate:
            return

        dist = self.distribution_reason(candidate, None, features)
        if dist:
            self.cancel_candidate(candidate, ts_ms, f"pre_scout_{dist}", features)
            return
        if ts_ms >= candidate.expires_ts_ms:
            self.cancel_candidate(candidate, ts_ms, "arm_expired", features)
            return
        ok, reason = self.scout_gate(candidate, features)
        if ok:
            pos = self.broker.open_scout(candidate, price, ts_ms)
            if not pos:
                return
            candidate.state = "SCOUT_FILLED"
            candidate.scout_ts_ms = ts_ms
            candidate.scout_price = price
            candidate.post_scout_high = price
            candidate.last_transition_ts_ms = ts_ms
            self.logger.write_candidate(
                "scout",
                mint,
                candidate.state,
                {"features": self.slim_features(features), "reason": reason, "scout_sol": pos.scout_sol, "target_sol": pos.target_sol},
            )

    async def manage_position(
        self,
        candidate: Optional[Candidate],
        pos: PaperPosition,
        ts_ms: int,
        price: float,
        features: dict[str, Any],
    ) -> None:
        mint = pos.mint
        mult = pos.update(price)
        if candidate:
            candidate.post_scout_high = max(candidate.post_scout_high, price)

        dist = self.distribution_reason(candidate, pos, features)
        if dist:
            self.close_position(mint, ts_ms, price, f"kill_{dist}", features, killed=True)
            return
        if features["complete"]:
            self.close_position(mint, ts_ms, price, "migration_complete", features, killed=False)
            return

        if pos.state == "SCOUT_FILLED":
            if mult <= 0.92:
                self.close_position(mint, ts_ms, price, "scout_scratch_drawdown", features, killed=True)
                return
            scale_reason = self.scale1_reason(candidate, pos, features)
            if scale_reason:
                target_after_scale1 = min(pos.target_sol * 0.50, self.config.max_position_sol)
                add_sol = max(0.0, target_after_scale1 - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "SCALE1_FILLED", scale_reason)
                if scaled:
                    if candidate and scale_reason == "clean_runway_higher_high":
                        candidate.clean_runway_scales += 1
                    self.logger.write_candidate(
                        "scale1",
                        mint,
                        "SCALE1_FILLED",
                        {
                            "features": self.slim_features(features),
                            "add_sol": add_sol,
                            "reason": scale_reason,
                            "absorbed_sells": candidate.absorbed_sells if candidate else 0,
                        },
                    )
                return
            if candidate and ts_ms - candidate.scout_ts_ms >= self.config.scout_timeout_ms:
                self.close_position(mint, ts_ms, price, "scout_timeout_no_absorption", features, killed=True)
                return

        if pos.state == "SCALE1_FILLED":
            if mult <= 0.85:
                self.close_position(mint, ts_ms, price, "scale1_hard_scratch", features, killed=True)
                return
            if not pos.derisk_done and self.derisk_gate(pos, features):
                self.broker.partial(mint, 0.50, price, "first_expansion_pop")
                self.logger.write_candidate("derisk", mint, "RUNNER", {"features": self.slim_features(features), "sold_fraction": 0.50})
                return
            if (
                not pos.derisk_done
                and pos.age_sec(ts_ms) >= 8.0
                and (pos.last_mult < 1.12 or features["move1"] < 0.998)
            ):
                self.close_position(mint, ts_ms, price, "scale1_no_derisk_stall", features, killed=False)
                return

        if pos.state in {"RUNNER", "RUNNER_FULL"}:
            if self.scale2_gate(candidate, pos, features):
                add_sol = max(0.0, pos.target_sol - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "RUNNER_FULL", "second_absorbed_continuation")
                if scaled:
                    self.logger.write_candidate(
                        "scale2",
                        mint,
                        "RUNNER_FULL",
                        {"features": self.slim_features(features), "add_sol": add_sol, "absorbed_sells": candidate.absorbed_sells if candidate else 0},
                    )
                    return
            trail = 0.18
            if pos.peak_mult >= 1.20 and mult <= pos.peak_mult * (1.0 - trail):
                self.close_position(mint, ts_ms, price, "runner_structure_trail", features, killed=False)
                return
            if mult >= 1.30 and features["accel"] < -0.008 and features["move1"] < 0.998:
                self.close_position(mint, ts_ms, price, "runner_accel_decay", features, killed=False)
                return

        if pos.age_sec(ts_ms) >= 60.0:
            self.close_position(mint, ts_ms, price, "hard_60s_time_stop", features, killed=False)

    def scale1_reason(self, candidate: Optional[Candidate], pos: PaperPosition, features: dict[str, Any]) -> Optional[str]:
        if not candidate or pos.scale1_done:
            return None
        if candidate.absorbed_sells >= 1:
            if features["price"] < candidate.last_absorbed_price * 0.999:
                return None
            if features["move1"] < 1.0002 and features["move200"] < 1.0001:
                return None
            if features["ar_sell_2"] > 0.55:
                return None
            if features["prob2x"] < 0.42 and features["s2"]["tracked_buyers"] <= 0:
                return None
            return "absorbed_sell_higher_high"

        scout_age_ms = features["ts_ms"] - candidate.scout_ts_ms
        s2 = features["s2"]
        clean_runway = (
            450 <= scout_age_ms <= 5000
            and pos.last_mult >= 1.12
            and candidate.post_scout_high >= candidate.scout_price * 1.14
            and s2["buy_sol"] >= 0.60
            and s2["unique_buyers"] >= 4
            and s2["sell_sol"] <= max(0.018, s2["buy_sol"] * 0.08)
            and s2["top_buy_share"] <= 0.72
            and features["n_ofi_2"] >= 0.015
            and features["move1"] >= 1.003
            and features["time_since_high"] <= 1.8
            and features["prob2x"] >= 0.70
        )
        if clean_runway:
            return "clean_runway_higher_high"
        return None

    def derisk_gate(self, pos: PaperPosition, features: dict[str, Any]) -> bool:
        if pos.last_mult >= 1.60:
            return True
        if pos.last_mult >= 1.30 and features["accel"] < -0.004:
            return True
        if (
            pos.last_mult >= 1.22
            and (features["move200"] < 0.999 or features["s2"]["sell_sol"] > features["s2"]["buy_sol"] * 0.15)
        ):
            return True
        return False

    def scale2_gate(self, candidate: Optional[Candidate], pos: PaperPosition, features: dict[str, Any]) -> bool:
        if not candidate or pos.scale2_done or not pos.derisk_done:
            return False
        if candidate.absorbed_sells < 2:
            return False
        if pos.last_mult < 1.35:
            return False
        if features["move1"] < 1.001:
            return False
        if features["prob2x"] < 0.55:
            return False
        if pos.cost_sol >= pos.target_sol:
            return False
        return True

    def update_absorption(
        self,
        candidate: Candidate,
        ts_ms: int,
        price: float,
        features: dict[str, Any],
        event: Optional[FlowEvent],
    ) -> None:
        if candidate.state not in {"SCOUT_FILLED", "SCALE1_FILLED", "RUNNER", "RUNNER_FULL"}:
            return
        if price > candidate.post_scout_high:
            candidate.post_scout_high = price
        if event and not event.is_buy and candidate.pending_sell_ts_ms <= 0:
            recent_buy = max(features["s1"]["buy_sol"], features["s2"]["buy_sol"] * 0.40)
            material = event.intent_sol >= max(0.012, recent_buy * 0.20)
            if material:
                candidate.pending_sell_ts_ms = event.ts_ms
                candidate.pending_sell_ref_high = max(candidate.post_scout_high, price)
                self.logger.write_candidate(
                    "material_sell",
                    candidate.mint,
                    candidate.state,
                    {"sell_sol": event.intent_sol, "ref_high": candidate.pending_sell_ref_high, "features": self.slim_features(features)},
                )
        if candidate.pending_sell_ts_ms <= 0:
            return
        elapsed = ts_ms - candidate.pending_sell_ts_ms
        if price >= candidate.pending_sell_ref_high * 1.001 and features["ar_sell_2"] <= 0.55:
            candidate.absorbed_sells += 1
            candidate.last_absorbed_ts_ms = ts_ms
            candidate.last_absorbed_price = price
            candidate.pending_sell_ts_ms = 0
            candidate.pending_sell_ref_high = 0.0
            self.logger.write_candidate(
                "absorbed_sell",
                candidate.mint,
                candidate.state,
                {"absorbed_sells": candidate.absorbed_sells, "price": price, "features": self.slim_features(features)},
            )
            log(f"MOON-ABSORB {short_mint(candidate.mint)} count={candidate.absorbed_sells} price={price:.6e}")
            return
        if elapsed >= self.config.material_sell_timeout_ms and price < candidate.pending_sell_ref_high * 0.990:
            candidate.failed_highs += 1
            candidate.pending_sell_ts_ms = 0
            candidate.pending_sell_ref_high = 0.0
            self.logger.write_candidate(
                "failed_absorption",
                candidate.mint,
                candidate.state,
                {"failed_highs": candidate.failed_highs, "price": price, "features": self.slim_features(features)},
            )

    def close_position(
        self,
        mint: str,
        ts_ms: int,
        price: float,
        reason: str,
        features: dict[str, Any],
        killed: bool,
    ) -> None:
        pnl = self.broker.close(mint, ts_ms, price, reason, killed=killed)
        candidate = self.candidates.pop(mint, None)
        self.actor.leave_mint(mint)
        self.logger.write_candidate(
            "close",
            mint,
            "CLOSED",
            {"reason": reason, "pnl_sol": pnl, "killed": killed, "features": self.slim_features(features)},
        )
        if candidate:
            candidate.state = "CLOSED"

    def cancel_candidate(self, candidate: Candidate, ts_ms: int, reason: str, features: dict[str, Any]) -> None:
        self.candidates.pop(candidate.mint, None)
        self.actor.leave_mint(candidate.mint)
        self.broker.closed_recent[candidate.mint] = ts_ms
        self.broker.stats.arm_cancels += 1
        log(f"MOON-CANCEL {short_mint(candidate.mint)} reason={reason} age={(ts_ms - candidate.arm_ts_ms) / 1000.0:.2f}s")
        self.logger.write_candidate(
            "cancel",
            candidate.mint,
            "IDLE",
            {"reason": reason, "features": self.slim_features(features)},
        )

    def prune_candidates(self, ts_ms: int) -> None:
        for mint, candidate in list(self.candidates.items()):
            if mint in self.broker.positions:
                continue
            if ts_ms >= candidate.expires_ts_ms:
                features = self.feature_snapshot(mint, ts_ms)
                if features:
                    self.cancel_candidate(candidate, ts_ms, "prune_expired", features)
                else:
                    self.candidates.pop(mint, None)

    @staticmethod
    def slim_features(features: dict[str, Any]) -> dict[str, Any]:
        return {
            "price": features["price"],
            "vsol_sol": features["vsol_sol"],
            "age_sec": features["age_sec"],
            "move200": features["move200"],
            "move1": features["move1"],
            "move2": features["move2"],
            "move5": features["move5"],
            "n_ofi_2": features["n_ofi_2"],
            "ar_sell_2": features["ar_sell_2"],
            "ipd": features["ipd"],
            "compression": features["compression"],
            "prob2x": features["prob2x"],
            "impulse_score": features["impulse_score"],
            "buy2": features["s2"]["buy_sol"],
            "sell2": features["s2"]["sell_sol"],
            "uniq2": features["s2"]["unique_buyers"],
            "tracked2": features["s2"]["tracked_buyers"],
            "top_buy_share2": features["s2"]["top_buy_share"],
            "usr2": features["s2"]["usr"],
            "top_buyer_flip2": features["s2"]["top_buyer_flip"],
        }

    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            ts_ms = now_ms()
            for mint, tape in list(self.tapes.items()):
                curve = self.bc.latest_for_mint(mint, self.config.curve_max_age_ms, ts_ms)
                if curve and curve.price > 0:
                    tape.add_price(ts_ms, curve.price, self.config.max_tape_age_sec)
                if mint in self.candidates or mint in self.broker.positions:
                    features = self.feature_snapshot(mint, ts_ms)
                    if features:
                        await self.manage_mint(mint, ts_ms, features, None)
                    else:
                        pos = self.broker.positions.get(mint)
                        if (
                            pos
                            and pos.state == "SCOUT_FILLED"
                            and pos.last_price > 0
                            and pos.age_sec(ts_ms) >= max(5.0, self.config.scout_timeout_ms / 1000.0)
                        ):
                            self.close_stale_position(mint, ts_ms, "stale_curve_scout_timeout", killed=True)
            self.prune_candidates(ts_ms)
            self.report_if_due()
            await asyncio.sleep(self.config.heartbeat_sec)

    def close_stale_position(self, mint: str, ts_ms: int, reason: str, killed: bool) -> None:
        pos = self.broker.positions.get(mint)
        if not pos or pos.last_price <= 0:
            return
        pnl = self.broker.close(mint, ts_ms, pos.last_price, reason, killed=killed)
        self.candidates.pop(mint, None)
        self.actor.leave_mint(mint)
        self.logger.write_candidate(
            "close",
            mint,
            "CLOSED",
            {"reason": reason, "pnl_sol": pnl, "killed": killed, "stale": True},
        )

    def report_if_due(self, force: bool = False) -> None:
        if not force and time.time() - self.last_report_at < self.config.report_sec:
            return
        self.last_report_at = time.time()
        open_bits = []
        for mint, pos in self.broker.positions.items():
            open_bits.append(
                f"{short_mint(mint)} {pos.state} {pos.last_mult:.2f}x pk={pos.peak_mult:.2f}x cost={pos.cost_sol:.3f}"
            )
        open_text = ", ".join(open_bits) if open_bits else "none"
        st = self.broker.stats
        ts_ms = now_ms()
        shred_age = max(0.0, (ts_ms - self.last_shred_msg_ms) / 1000.0)
        curve_age = max(0.0, (ts_ms - self.last_curve_msg_ms) / 1000.0)
        log(
            f"MOON-STATUS arms={st.arms} cancels={st.arm_cancels} scouts={st.scouts} "
            f"scale1={st.scale1} scale2={st.scale2} partials={st.partials} "
            f"closes={st.closes} W/L={st.wins}/{st.losses} kills={st.kills} "
            f"realized={st.realized_pnl_sol:+.5f} SOL open_pnl={self.broker.open_pnl():+.5f} SOL "
            f"open={len(self.broker.positions)} armed={len(self.candidates)} [{open_text}] "
            f"shreds={st.shreds} trades={st.trades} bc_updates={self.bc.updates} "
            f"shred_age={shred_age:.1f}s curve_age={curve_age:.1f}s "
            f"reconn={st.shred_reconnects}/{st.curve_reconnects} actor_msgs={self.actor.messages}"
        )

    async def shred_loop(self) -> None:
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
                    shred_sub = {
                        "jsonrpc": "2.0",
                        "id": 51001,
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
                    await ws.send(json.dumps(shred_sub))
                    self.last_shred_msg_ms = now_ms()
                    log("MOON-SHRED: subscribed to market pump.fun shreds")
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.config.shred_stall_reconnect_sec)
                        except asyncio.TimeoutError:
                            stale = max(0.0, (now_ms() - self.last_shred_msg_ms) / 1000.0)
                            self.broker.stats.shred_reconnects += 1
                            log(f"MOON-SHRED: no shreds for {stale:.1f}s, reconnecting")
                            break
                        data = json.loads(raw)
                        method = str(data.get("method") or "").lower()
                        if "shred" not in method:
                            continue
                        result = ((data.get("params") or {}).get("result") or {})
                        sig = str(result.get("signature") or "")
                        if not sig:
                            continue
                        self.last_shred_msg_ms = now_ms()
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
                self.broker.stats.shred_reconnects += 1
                log(f"MOON-SHRED: stream error, reconnecting in 2s: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def curve_loop(self) -> None:
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
                    bc_sub = {
                        "jsonrpc": "2.0",
                        "id": 52002,
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
                    await ws.send(json.dumps(bc_sub))
                    self.last_curve_msg_ms = now_ms()
                    log("MOON-CURVE: subscribed to BondingCurve reserve truth")
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except asyncio.TimeoutError:
                            self.broker.stats.curve_reconnects += 1
                            log("MOON-CURVE: no curve messages for 20s, reconnecting")
                            break
                        data = json.loads(raw)
                        method = str(data.get("method") or "").lower()
                        if "program" not in method:
                            continue
                        value = (((data.get("params") or {}).get("result") or {}).get("value") or {})
                        if self.bc.update_from_program_value(value, now_ms()):
                            self.last_curve_msg_ms = now_ms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.broker.stats.curve_reconnects += 1
                log(f"MOON-CURVE: stream error, reconnecting in 2s: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def combined_stream_loop(self) -> None:
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
                    shred_sub = {
                        "jsonrpc": "2.0",
                        "id": 53001,
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
                        "id": 53002,
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
                    await ws.send(json.dumps(shred_sub))
                    await ws.send(json.dumps(bc_sub))
                    ts_ms = now_ms()
                    self.last_shred_msg_ms = ts_ms
                    self.last_curve_msg_ms = ts_ms
                    log("MOON: subscribed to combined shreds + BondingCurve with shred watchdog")
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            stale = max(0.0, (now_ms() - self.last_shred_msg_ms) / 1000.0)
                            if stale >= self.config.shred_stall_reconnect_sec:
                                self.broker.stats.shred_reconnects += 1
                                log(f"MOON: shreds stalled for {stale:.1f}s, reconnecting combined socket")
                                break
                            continue
                        data = json.loads(raw)
                        method = str(data.get("method") or "").lower()
                        ts_ms = now_ms()
                        if "program" in method:
                            value = (((data.get("params") or {}).get("result") or {}).get("value") or {})
                            if self.bc.update_from_program_value(value, ts_ms):
                                self.last_curve_msg_ms = ts_ms
                        elif "shred" in method:
                            result = ((data.get("params") or {}).get("result") or {})
                            sig = str(result.get("signature") or "")
                            if sig:
                                self.last_shred_msg_ms = ts_ms
                                self.broker.stats.shreds += 1
                                for trade in parse_base64_shred_for_pump_trades(result):
                                    if not self.dedup_trade(sig, trade):
                                        continue
                                    event = self.build_event(sig, trade)
                                    if event:
                                        await self.on_event(event)
                        stale = max(0.0, (now_ms() - self.last_shred_msg_ms) / 1000.0)
                        if stale >= self.config.shred_stall_reconnect_sec:
                            self.broker.stats.shred_reconnects += 1
                            log(f"MOON: shreds stalled for {stale:.1f}s while socket was alive, reconnecting")
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.broker.stats.shred_reconnects += 1
                log(f"MOON: combined stream error, reconnecting in 2s: {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def run(self) -> None:
        mode = "PAPER" if self.config.paper_trading else "LIVE_DISABLED"
        log(
            f"MOON: starting mode={mode} scout={self.config.scout_sol:.4f} "
            f"max_pos={self.config.max_position_sol:.4f} max_open={self.config.max_open_positions} "
            f"ws={'split' if self.config.split_ws else 'combined_watchdog'} "
            f"datastream={'on' if self.config.enable_datastream else 'off'}"
        )
        log(
            f"MOON: tracked_wallets={len(self.tracked_wallets)} "
            f"raw_log={self.config.raw_events_file} candidates={self.config.candidates_file}"
        )
        if not self.config.paper_trading:
            raise RuntimeError("Live execution is intentionally disabled until paper/tiny-live gates pass")

        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, self.stop_event.set)
                except NotImplementedError:
                    pass

        tasks = [
            asyncio.create_task(self.heartbeat_loop()),
            asyncio.create_task(self.actor.run(self.stop_event)),
        ]
        if self.config.split_ws:
            tasks.extend([
                asyncio.create_task(self.shred_loop()),
                asyncio.create_task(self.curve_loop()),
            ])
        else:
            tasks.append(asyncio.create_task(self.combined_stream_loop()))
        deadline = time.time() + self.config.run_seconds if self.config.run_seconds > 0 else None
        try:
            while not self.stop_event.is_set():
                if deadline and time.time() >= deadline:
                    self.stop_event.set()
                    break
                await asyncio.sleep(0.2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.report_if_due(force=True)
            self.broker.save_state()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper-first pump.fun moonshot scout/scale FSM bot")
    parser.add_argument("--ws", default="", help="Solana Tracker RPC WebSocket URL")
    parser.add_argument("--datastream-ws", default="", help="Optional Solana Tracker Datastream WS URL")
    parser.add_argument("--state", default="", help="State JSON path")
    parser.add_argument("--raw-log", default="", help="Raw event JSONL path")
    parser.add_argument("--candidate-log", default="", help="Candidate lifecycle JSONL path")
    parser.add_argument("--snipers", default="", help="Active sniper wallet file")
    parser.add_argument("--run-seconds", type=float, default=0.0, help="Stop after N seconds; 0 runs forever")
    parser.add_argument("--print-trades", action="store_true", help="Print each parsed pump trade")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config = BotConfig.from_env(args)
    bot = MoonshotFsmBot(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(async_main())
