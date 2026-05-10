"""
Birth First Sniper

Paper-first pump.fun launch sniper. This is not a copy-trading bot and it does
not wait for late confirmation. It watches market-wide raw shreds for pump.fun
create/buy/sell instructions, treats the first public launch flow as the edge,
and only adds size if the next wave proves the move is real.

Default mode is paper. Live execution is deliberately gated off until the paper
stream proves positive expectancy and the live sell path is completed.
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
# 2026-05-10 — PumpSwap AMM program. Subscribed alongside PUMP_PROGRAM so the
# bot can SEE post-migration AMM trades and score them via moonshot_unified.
# Without this subscription the bot is blind to ~70% of real volume (large
# pumps continue as AMM after BC graduation; this catches them too).
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SOL_MINT = "So11111111111111111111111111111111111111112"

BC_DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])
BC_DISC_B58 = "4y6pru6YvC7"

DISC_CREATE = bytes([24, 30, 200, 40, 5, 28, 7, 119])
DISC_CREATE_V2 = bytes([214, 144, 76, 236, 95, 139, 49, 180])
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_BUY_EXACT_SOL_IN = bytes([56, 252, 116, 8, 158, 223, 205, 95])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
DISC_MIGRATE = bytes([155, 234, 231, 146, 236, 158, 162, 30])
# PumpSwap AMM (post-migration). Same regular-buy disc as BC; AMM also has
# a buy_exact_quote_in variant. Sell shares the same disc as BC sell.
DISC_AMM_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_AMM_BUY_EXACT_QUOTE_IN = bytes([198, 46, 21, 82, 180, 217, 232, 112])
DISC_AMM_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
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
    raw = env_str(name, "1" if default else "0").lower()
    return raw not in {"", "0", "false", "no", "off"}


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


def short_addr(addr: str) -> str:
    if len(addr) <= 10:
        return addr
    return f"{addr[:4]}..{addr[-4:]}"


def log(msg: str) -> None:
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {safe}", flush=True)


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


def get_key(keys: list[Pubkey], idx: int) -> str:
    if idx < 0 or idx >= len(keys):
        return ""
    return str(keys[idx])


def get_account_key(keys: list[Pubkey], accounts: list[int], pos: int) -> str:
    if pos < 0 or pos >= len(accounts):
        return ""
    return get_key(keys, int(accounts[pos]))


def parse_borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 4 > len(data):
        return "", offset
    n = int.from_bytes(data[offset:offset + 4], "little")
    offset += 4
    if n < 0 or offset + n > len(data):
        return "", offset
    raw = data[offset:offset + n]
    offset += n
    return raw.decode("utf-8", errors="replace"), offset


@dataclass(frozen=True)
class BotConfig:
    paper_trading: bool = True
    live_enabled: bool = False
    st_rpc_ws: str = ""
    run_seconds: float = 0.0
    report_sec: float = 5.0
    heartbeat_sec: float = 0.025
    shred_stall_reconnect_sec: float = 8.0
    curve_max_age_ms: int = 900
    max_tape_age_sec: int = 180

    scout_sol: float = 0.0025
    max_position_sol: float = 0.035
    max_open_positions: int = 6
    max_pending_strikes: int = 8
    min_seconds_between_strikes: float = 0.05
    cooldown_sec: float = 10.0
    paper_drag_bps: float = 280.0

    birth_max_age_ms: int = 3500
    first_buy_max_age_ms: int = 900
    pending_fill_ttl_ms: int = 1200
    first_buy_min_sol: float = 0.050
    first_buy_max_sol: float = 2.50
    two_wallet_buy_sol: float = 0.014
    velocity_buy_sol: float = 0.045
    max_initial_sell_ratio: float = 0.35

    state_file: Path = DATA_DIR / "birth_first_state.json"
    raw_events_file: Path = DATA_DIR / "birth_first_raw_events.jsonl"
    decisions_file: Path = DATA_DIR / "birth_first_decisions.jsonl"
    snipers_file: Path = BASE_DIR / "active_snipers.txt"
    print_events: bool = False

    @staticmethod
    def from_env(args: argparse.Namespace) -> "BotConfig":
        load_dotenv()
        rpc_key = env_str("SOLANATRACKER_RPC_KEY")
        ws = env_str("SOLANATRACKER_RPC_WS")
        if not ws and rpc_key:
            ws = f"wss://rpc-mainnet.solanatracker.io?api_key={rpc_key}"
        return BotConfig(
            paper_trading=env_bool("BIRTH_PAPER_TRADING", True),
            live_enabled=env_bool("BIRTH_ENABLE_LIVE", False),
            st_rpc_ws=args.ws or ws,
            run_seconds=float(args.run_seconds or 0.0),
            report_sec=env_float("BIRTH_REPORT_SEC", 5.0),
            heartbeat_sec=env_float("BIRTH_HEARTBEAT_SEC", 0.025),
            shred_stall_reconnect_sec=env_float("BIRTH_SHRED_STALL_RECONNECT_SEC", 8.0),
            curve_max_age_ms=env_int("BIRTH_CURVE_MAX_AGE_MS", 900),
            max_tape_age_sec=env_int("BIRTH_MAX_TAPE_AGE_SEC", 180),
            scout_sol=env_float("BIRTH_SCOUT_SOL", 0.0025),
            max_position_sol=env_float("BIRTH_MAX_POSITION_SOL", 0.035),
            max_open_positions=env_int("BIRTH_MAX_OPEN_POSITIONS", 6),
            max_pending_strikes=env_int("BIRTH_MAX_PENDING_STRIKES", 8),
            min_seconds_between_strikes=env_float("BIRTH_MIN_SECONDS_BETWEEN_STRIKES", 0.05),
            cooldown_sec=env_float("BIRTH_COOLDOWN_SEC", 10.0),
            paper_drag_bps=env_float("BIRTH_PAPER_DRAG_BPS", 280.0),
            birth_max_age_ms=env_int("BIRTH_MAX_AGE_MS", 3500),
            first_buy_max_age_ms=env_int("BIRTH_FIRST_BUY_MAX_AGE_MS", 900),
            pending_fill_ttl_ms=env_int("BIRTH_PENDING_FILL_TTL_MS", 1200),
            first_buy_min_sol=env_float("BIRTH_FIRST_BUY_MIN_SOL", 0.050),
            first_buy_max_sol=env_float("BIRTH_FIRST_BUY_MAX_SOL", 2.50),
            two_wallet_buy_sol=env_float("BIRTH_TWO_WALLET_BUY_SOL", 0.014),
            velocity_buy_sol=env_float("BIRTH_VELOCITY_BUY_SOL", 0.045),
            max_initial_sell_ratio=env_float("BIRTH_MAX_INITIAL_SELL_RATIO", 0.35),
            state_file=Path(args.state or env_str("BIRTH_STATE_FILE", str(DATA_DIR / "birth_first_state.json"))),
            raw_events_file=Path(args.raw_log or env_str("BIRTH_RAW_EVENTS_FILE", str(DATA_DIR / "birth_first_raw_events.jsonl"))),
            decisions_file=Path(args.decisions or env_str("BIRTH_DECISIONS_FILE", str(DATA_DIR / "birth_first_decisions.jsonl"))),
            snipers_file=Path(args.snipers or env_str("BIRTH_SNIPERS_FILE", str(BASE_DIR / "active_snipers.txt"))),
            print_events=bool(args.print_events),
        )


@dataclass(frozen=True)
class PumpEvent:
    ts_ms: int
    recv_ns: int
    sig: str
    slot: int
    signer: str
    kind: str
    mint: str
    bonding_curve: str
    is_buy: bool = False
    sol_lamports: int = 0
    token_amount: int = 0
    user: str = ""
    creator: str = ""
    name: str = ""
    symbol: str = ""
    uri: str = ""
    create_version: str = ""
    is_mayhem: bool = False
    tracked: bool = False
    instruction_kind: str = ""
    price_hint: float = 0.0

    @property
    def sol(self) -> float:
        return self.sol_lamports / 1_000_000_000.0


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
        return self.vsol_lamports / 1_000_000_000.0


class BondingCurveCache:
    def __init__(self) -> None:
        self.by_curve: dict[str, Deque[CurvePoint]] = defaultdict(lambda: deque(maxlen=240))
        self.mint_to_curve: dict[str, str] = {}
        self.updates = 0
        self.decode_errors = 0

    def remember(self, mint: str, curve: str) -> None:
        if mint and curve:
            self.mint_to_curve[mint] = curve

    def derive_curve(self, mint: str) -> Optional[str]:
        if not mint:
            return None
        cached = self.mint_to_curve.get(mint)
        if cached:
            return cached
        try:
            pda, _ = Pubkey.find_program_address(
                [b"bonding-curve", bytes(Pubkey.from_string(mint))],
                Pubkey.from_string(PUMP_PROGRAM),
            )
            out = str(pda)
            self.mint_to_curve[mint] = out
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
            vtoken = struct.unpack_from("<Q", raw, 8)[0]
            vsol = struct.unpack_from("<Q", raw, 16)[0]
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
        curve = self.derive_curve(mint)
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

    def move_for_mint(self, mint: str, window_ms: int, max_age_ms: int, ts_ms: int) -> Optional[tuple[float, int, bool]]:
        curve = self.derive_curve(mint)
        if not curve:
            return None
        items = list(self.by_curve.get(curve) or [])
        if len(items) < 2:
            return None
        latest = items[-1]
        if ts_ms - latest.ts_ms > max_age_ms or latest.price <= 0:
            return None
        cutoff = ts_ms - window_ms
        recent = [p for p in items if p.ts_ms >= cutoff]
        if len(recent) < 2 or recent[0].price <= 0:
            return None
        return latest.price / recent[0].price, ts_ms - latest.ts_ms, latest.complete


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
    f_lt_50ms: float = 0.0
    interarrival_cv: float = 0.0
    top_buyer_flip: float = 0.0

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
    events: Deque[PumpEvent] = field(default_factory=deque)
    prices: Deque[tuple[int, float]] = field(default_factory=deque)
    first_seen_ms: int = 0
    first_create_ms: int = 0
    first_buy_ms: int = 0
    first_buyer: str = ""
    first_buy_sol: float = 0.0
    creator: str = ""
    create_version: str = ""
    is_mayhem: bool = False
    peak_price: float = 0.0
    peak_ts_ms: int = 0
    last_price: float = 0.0

    def add_event(self, event: PumpEvent, max_age_sec: int) -> None:
        if not self.first_seen_ms:
            self.first_seen_ms = event.ts_ms
        if event.kind == "create":
            if not self.first_create_ms:
                self.first_create_ms = event.ts_ms
            if event.creator:
                self.creator = event.creator
            self.create_version = event.create_version or self.create_version
            self.is_mayhem = self.is_mayhem or event.is_mayhem
        if event.kind == "trade" and event.is_buy and not self.first_buy_ms:
            self.first_buy_ms = event.ts_ms
            self.first_buyer = event.user or event.signer
            self.first_buy_sol = event.sol
        self.events.append(event)
        self.prune(event.ts_ms, max_age_sec)

    def add_price(self, ts_ms: int, price: float, max_age_sec: int) -> None:
        if price <= 0:
            return
        self.last_price = price
        self.prices.append((ts_ms, price))
        if self.peak_price <= 0 or price > self.peak_price:
            self.peak_price = price
            self.peak_ts_ms = ts_ms
        self.prune(ts_ms, max_age_sec)

    def prune(self, ts_ms: int, max_age_sec: int) -> None:
        cutoff = ts_ms - max_age_sec * 1000
        while self.events and self.events[0].ts_ms < cutoff:
            self.events.popleft()
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.popleft()

    def age_ms(self, ts_ms: int) -> int:
        if not self.first_seen_ms:
            return 0
        return max(0, ts_ms - self.first_seen_ms)

    def buy_age_ms(self, ts_ms: int) -> int:
        if not self.first_buy_ms:
            return self.age_ms(ts_ms)
        return max(0, ts_ms - self.first_buy_ms)

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
        selected = [e for e in self.events if e.kind == "trade" and e.ts_ms >= cutoff]
        prices = [p for t, p in self.prices if t >= cutoff and p > 0]
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
        out.buy_sol = sum(e.sol for e in buys)
        out.sell_sol = sum(e.sol for e in sells)
        out.net_sol = out.buy_sol - out.sell_sol
        out.unique_buyers = len({(e.user or e.signer) for e in buys if e.user or e.signer})
        out.unique_sellers = len({(e.user or e.signer) for e in sells if e.user or e.signer})
        out.tracked_buyers = len({(e.user or e.signer) for e in buys if e.tracked and (e.user or e.signer)})

        buy_by_wallet: dict[str, float] = defaultdict(float)
        sell_by_wallet: dict[str, float] = defaultdict(float)
        buy_sizes: list[float] = []
        buy_times: list[int] = []
        for e in buys:
            wallet = e.user or e.signer
            buy_by_wallet[wallet] += e.sol
            buy_sizes.append(e.sol)
            buy_times.append(e.ts_ms)
        for e in sells:
            wallet = e.user or e.signer
            sell_by_wallet[wallet] += e.sol
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
class StrikePlan:
    mint: str
    ts_ms: int
    lane: str
    reason: str
    score: float
    scout_sol: float
    target_sol: float
    price: float
    needs_curve_fill: bool
    features: dict[str, Any]


@dataclass
class PendingStrike:
    mint: str
    ts_ms: int
    expires_ts_ms: int
    plan: StrikePlan


@dataclass
class Position:
    mint: str
    state: str
    opened_ts_ms: int
    avg_price: float
    tokens_bought: float
    remaining_tokens: float
    cost_sol: float
    scout_sol: float
    target_sol: float
    lane: str
    reason: str
    entry_features: dict[str, Any] = field(default_factory=dict)
    realized_sol: float = 0.0
    peak_price: float = 0.0
    peak_mult: float = 1.0
    last_price: float = 0.0
    last_mult: float = 1.0
    scale1_done: bool = False
    scale2_done: bool = False
    derisk_done: bool = False
    dry_live_cost_sol: float = 0.0
    dry_live_locked_rent_sol: float = 0.0
    moonshot_mode: bool = False
    moonshot_arm_ts: int = 0
    moonshot_arm_peak: float = 0.0
    scale_out_step: int = 0
    peak_advance_ts: int = 0

    def age_sec(self, ts_ms: int) -> float:
        return max(0.0, (ts_ms - self.opened_ts_ms) / 1000.0)

    def update(self, price: float) -> float:
        if price <= 0 or self.avg_price <= 0:
            return self.last_mult
        next_mult = price / self.avg_price
        max_reasonable_mult = env_float("PIGGY_MAX_REASONABLE_PRICE_MULT", 50.0)
        if max_reasonable_mult > 0 and next_mult > max_reasonable_mult:
            return self.last_mult
        self.last_price = price
        self.last_mult = next_mult
        if self.peak_price <= 0 or price > self.peak_price:
            self.peak_price = price
        self.peak_mult = max(self.peak_mult, self.last_mult)
        return self.last_mult

    def open_pnl_sol(self, drag: float) -> float:
        fill = max(self.last_price, 0.0) * (1.0 - drag)
        return self.realized_sol + self.remaining_tokens * fill - self.cost_sol


@dataclass
class SessionStats:
    shreds: int = 0
    creates: int = 0
    trades: int = 0
    buys: int = 0
    sells: int = 0
    curve_updates: int = 0
    reconnects: int = 0
    strike_plans: int = 0
    pending: int = 0
    pending_filled: int = 0
    pending_expired: int = 0
    scouts: int = 0
    scale1: int = 0
    scale2: int = 0
    partials: int = 0
    closes: int = 0
    kills: int = 0
    wins: int = 0
    losses: int = 0
    best_mult: float = 1.0
    realized_pnl_sol: float = 0.0
    started_at: float = field(default_factory=time.time)


class JsonlLogger:
    def __init__(self, config: BotConfig):
        self.raw_path = config.raw_events_file
        self.decisions_path = config.decisions_file
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)

    def raw_event(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        row = {
            "kind": event.kind,
            "ts_ms": event.ts_ms,
            "recv_ns": event.recv_ns,
            "sig": event.sig,
            "slot": event.slot,
            "mint": event.mint,
            "bonding_curve": event.bonding_curve,
            "signer": event.signer,
            "user": event.user,
            "creator": event.creator,
            "side": "buy" if event.is_buy else ("sell" if event.kind == "trade" else ""),
            "sol": event.sol,
            "token_amount": event.token_amount,
            "tracked": event.tracked,
            "instruction_kind": event.instruction_kind,
            "create_version": event.create_version,
            "is_mayhem": event.is_mayhem,
            "curve_price": curve.price if curve else 0.0,
            "vsol_sol": curve.vsol_sol if curve else 0.0,
            "complete": curve.complete if curve else False,
        }
        self.append(self.raw_path, row)

    def decision(self, kind: str, mint: str, payload: dict[str, Any]) -> None:
        row = {"kind": kind, "ts_ms": now_ms(), "mint": mint, **payload}
        self.append(self.decisions_path, row)

    @staticmethod
    def append(path: Path, row: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n")
        except Exception as exc:
            log(f"LOGGER: write failed {path}: {type(exc).__name__}: {exc}")


class PaperBroker:
    def __init__(self, config: BotConfig):
        self.config = config
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingStrike] = {}
        self.closed_recent: dict[str, int] = {}
        self.last_strike_ts_ms = 0
        self.stats = SessionStats()
        self.dry_live_enabled = (
            env_bool("PGG2_DRY_LIVE_MODE", False)
            or env_str("PGG2_EXECUTION_MODE", "paper").lower() == "dry_live"
        )
        self.dry_live_direct_execution = env_bool("PGG2_DRY_LIVE_DIRECT_EXECUTION", True)
        self.dry_live_recover_ata_rent = env_bool("PGG2_DRY_LIVE_RECOVER_ATA_RENT", True)
        self.dry_live_ata_rent_sol = env_float("PGG2_DRY_LIVE_ATA_RENT_SOL", 0.002039280)
        self.dry_live_base_tx_fee_sol = env_float("PGG2_DRY_LIVE_BASE_TX_FEE_SOL", 0.000005)
        self.dry_live_priority_fee_sol = env_float("PGG2_DRY_LIVE_PRIORITY_FEE_SOL", 0.0)
        self.dry_live_close_account_fee_sol = env_float("PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL", 0.000005)
        default_platform_bps = 0.0 if self.dry_live_direct_execution else 50.0
        self.dry_live_platform_fee_bps = env_float("PGG2_DRY_LIVE_PLATFORM_FEE_BPS", default_platform_bps)
        self.dry_live_protocol_fee_bps = env_float("PGG2_DRY_LIVE_PROTOCOL_FEE_BPS", 0.0)
        self.dry_live_protocol_fee_label = env_str("PGG2_DRY_LIVE_PROTOCOL_FEE_LABEL", "none")
        self.dry_live_extra_fee_bps = env_float("PGG2_DRY_LIVE_EXTRA_FEE_BPS", 0.0)
        if self.dry_live_enabled:
            log(
                "DRY-LIVE-COST: "
                f"drag_bps={self.config.paper_drag_bps:.1f} direct={int(self.dry_live_direct_execution)} "
                f"platform_bps={self.dry_live_platform_fee_bps:.1f} "
                f"protocol_bps={self.dry_live_protocol_fee_bps:.1f} protocol={self.dry_live_protocol_fee_label} "
                f"extra_bps={self.dry_live_extra_fee_bps:.1f} "
                f"tx_fee={self.dry_live_tx_fee_sol():.9f} ata_rent={self.dry_live_ata_rent_sol:.9f} "
                f"recover_ata={int(self.dry_live_recover_ata_rent)}"
            )
        self.load_state()

    @property
    def drag(self) -> float:
        return max(0.0, self.config.paper_drag_bps) / 10000.0

    def dry_live_tx_fee_sol(self) -> float:
        return max(0.0, self.dry_live_base_tx_fee_sol + self.dry_live_priority_fee_sol)

    def dry_live_percent_fee(self, amount_sol: float) -> float:
        if not self.dry_live_enabled:
            return 0.0
        bps = max(
            0.0,
            self.dry_live_platform_fee_bps
            + self.dry_live_protocol_fee_bps
            + self.dry_live_extra_fee_bps,
        )
        return max(0.0, amount_sol) * bps / 10000.0

    def dry_live_buy_costs(self, amount_sol: float) -> tuple[float, float]:
        if not self.dry_live_enabled:
            return 0.0, 0.0
        tx_and_percent = self.dry_live_tx_fee_sol() + self.dry_live_percent_fee(amount_sol)
        locked_rent = max(0.0, self.dry_live_ata_rent_sol)
        rent_cost = 0.0 if self.dry_live_recover_ata_rent else locked_rent
        return tx_and_percent + rent_cost, locked_rent

    def dry_live_sell_cost(self, proceeds_sol: float, closes_account: bool) -> float:
        if not self.dry_live_enabled:
            return 0.0
        out = self.dry_live_tx_fee_sol() + self.dry_live_percent_fee(proceeds_sol)
        if closes_account and self.dry_live_recover_ata_rent:
            out += max(0.0, self.dry_live_close_account_fee_sol)
        return out

    def load_state(self) -> None:
        path = self.config.state_file
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.stats = SessionStats(**{**asdict(self.stats), **(data.get("session") or {})})
            for mint, raw in (data.get("positions") or {}).items():
                base = asdict(Position(
                    mint=mint,
                    state="SCOUT",
                    opened_ts_ms=0,
                    avg_price=0.0,
                    tokens_bought=0.0,
                    remaining_tokens=0.0,
                    cost_sol=0.0,
                    scout_sol=0.0,
                    target_sol=0.0,
                    lane="",
                    reason="",
                ))
                base.update(raw)
                self.positions[mint] = Position(**base)
            log(f"BIRTH-STATE: restored {len(self.positions)} positions from {path}")
        except Exception as exc:
            log(f"BIRTH-STATE: restore failed {path}: {type(exc).__name__}: {exc}")

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
                "pending": {
                    mint: {
                        "ts_ms": p.ts_ms,
                        "expires_ts_ms": p.expires_ts_ms,
                        "lane": p.plan.lane,
                        "reason": p.plan.reason,
                        "score": p.plan.score,
                    }
                    for mint, p in self.pending.items()
                },
            }
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            for attempt in range(6):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:
                    if attempt >= 5:
                        raise
                    time.sleep(0.025 * (attempt + 1))
        except Exception as exc:
            log(f"BIRTH-STATE: save failed: {type(exc).__name__}: {exc}")

    def can_strike(self, mint: str, ts_ms: int) -> tuple[bool, str]:
        if mint in self.positions:
            return False, "already_open"
        if mint in self.pending:
            return False, "already_pending"
        if len(self.positions) >= self.config.max_open_positions:
            return False, "max_open"
        if len(self.pending) >= self.config.max_pending_strikes:
            return False, "max_pending"
        if ts_ms - self.last_strike_ts_ms < int(self.config.min_seconds_between_strikes * 1000):
            return False, "strike_rate"
        last = self.closed_recent.get(mint, 0)
        if last and ts_ms - last < int(self.config.cooldown_sec * 1000):
            return False, "cooldown"
        return True, "ok"

    def queue_or_fill(self, plan: StrikePlan, price: float) -> Optional[Position]:
        ok, reason = self.can_strike(plan.mint, plan.ts_ms)
        if not ok:
            return None
        self.stats.strike_plans += 1
        self.last_strike_ts_ms = plan.ts_ms
        if price > 0 and plan.lane != "first_public_buy":
            return self.open_position(plan, price, plan.ts_ms)
        pending = PendingStrike(
            mint=plan.mint,
            ts_ms=plan.ts_ms,
            expires_ts_ms=plan.ts_ms + self.config.pending_fill_ttl_ms,
            plan=plan,
        )
        self.pending[plan.mint] = pending
        self.stats.pending += 1
        log(
            f"BIRTH-PENDING {short_addr(plan.mint)} lane={plan.lane} "
            f"score={plan.score:.1f} scout={plan.scout_sol:.4f} reason={plan.reason}"
        )
        self.save_state()
        return None

    def fill_pending(self, mint: str, ts_ms: int, price: float) -> Optional[Position]:
        pending = self.pending.pop(mint, None)
        if not pending or price <= 0:
            return None
        self.stats.pending_filled += 1
        log(
            f"BIRTH-PENDING-FILL {short_addr(mint)} delay={ts_ms - pending.ts_ms}ms "
            f"lane={pending.plan.lane}"
        )
        return self.open_position(pending.plan, price, ts_ms)

    def expire_pending(self, mint: str, ts_ms: int, reason: str) -> None:
        pending = self.pending.pop(mint, None)
        if not pending:
            return
        self.stats.pending_expired += 1
        self.closed_recent[mint] = ts_ms
        log(f"BIRTH-PENDING-CANCEL {short_addr(mint)} reason={reason} age={ts_ms - pending.ts_ms}ms")
        self.save_state()

    def open_position(self, plan: StrikePlan, price: float, ts_ms: int) -> Optional[Position]:
        if price <= 0:
            return None
        amount = max(0.0005, min(plan.scout_sol, plan.target_sol))
        fill_price = price * (1.0 + self.drag)
        tokens = amount / max(fill_price, 1e-18)
        dry_cost, locked_rent = self.dry_live_buy_costs(amount)
        pos = Position(
            mint=plan.mint,
            state="SCOUT",
            opened_ts_ms=ts_ms,
            avg_price=fill_price,
            tokens_bought=tokens,
            remaining_tokens=tokens,
            cost_sol=amount,
            scout_sol=amount,
            target_sol=plan.target_sol,
            lane=plan.lane,
            reason=plan.reason,
            entry_features=dict(plan.features or {}),
            peak_price=fill_price,
            last_price=fill_price,
            dry_live_cost_sol=dry_cost,
            dry_live_locked_rent_sol=locked_rent,
        )
        self.positions[plan.mint] = pos
        self.stats.scouts += 1
        log(
            f"BIRTH-SCOUT {short_addr(plan.mint)} lane={plan.lane} scout={amount:.4f} "
            f"target={plan.target_sol:.4f} score={plan.score:.1f} fill={fill_price:.6e} "
            f"reason={plan.reason}"
            + (
                f" dry_cost={dry_cost:.6f} rent_lock={locked_rent:.6f}"
                if self.dry_live_enabled else ""
            )
        )
        self.save_state()
        return pos

    def scale(self, mint: str, add_sol: float, price: float, state: str, reason: str) -> Optional[Position]:
        pos = self.positions.get(mint)
        if not pos or add_sol <= 0 or price <= 0:
            return None
        add_sol = min(add_sol, max(0.0, self.config.max_position_sol - pos.cost_sol))
        add_sol = min(add_sol, max(0.0, pos.target_sol - pos.cost_sol))
        if add_sol <= 0:
            return None
        fill_price = price * (1.0 + self.drag)
        add_tokens = add_sol / max(fill_price, 1e-18)
        dry_cost, _ = self.dry_live_buy_costs(add_sol)
        pos.cost_sol += add_sol
        pos.tokens_bought += add_tokens
        pos.remaining_tokens += add_tokens
        pos.dry_live_cost_sol += dry_cost
        pos.avg_price = pos.cost_sol / max(pos.tokens_bought, 1e-18)
        pos.state = state
        pos.update(price)
        if state == "SCALE1":
            pos.scale1_done = True
            self.stats.scale1 += 1
        elif state == "RUNNER_FULL":
            pos.scale2_done = True
            self.stats.scale2 += 1
        log(
            f"BIRTH-SCALE {short_addr(mint)} state={state} add={add_sol:.4f} "
            f"cost={pos.cost_sol:.4f} mult={pos.last_mult:.3f} reason={reason}"
            + (f" dry_cost+={dry_cost:.6f}" if self.dry_live_enabled else "")
        )
        self.save_state()
        return pos

    def partial(self, mint: str, fraction: float, price: float, reason: str) -> Optional[Position]:
        pos = self.positions.get(mint)
        if not pos or price <= 0:
            return None
        fraction = max(0.0, min(1.0, fraction))
        tokens = pos.remaining_tokens * fraction
        if tokens <= 0:
            return None
        fill_price = price * (1.0 - self.drag)
        gross_proceeds = tokens * fill_price
        dry_sell_cost = self.dry_live_sell_cost(gross_proceeds, closes_account=False)
        proceeds = max(0.0, gross_proceeds - dry_sell_cost)
        pos.remaining_tokens -= tokens
        pos.realized_sol += proceeds
        pos.derisk_done = True
        pos.state = "RUNNER"
        pos.update(price)
        self.stats.partials += 1
        log(
            f"BIRTH-DERISK {short_addr(mint)} sold={fraction * 100:.1f}% "
            f"proceeds={proceeds:.5f} mult={pos.last_mult:.3f} reason={reason}"
            + (f" dry_sell_cost={dry_sell_cost:.6f}" if self.dry_live_enabled else "")
        )
        self.save_state()
        return pos

    def close(self, mint: str, ts_ms: int, price: float, reason: str, killed: bool) -> Optional[float]:
        pos = self.positions.pop(mint, None)
        if not pos:
            return None
        fill_price = max(price, 0.0) * (1.0 - self.drag)
        gross_proceeds = pos.remaining_tokens * fill_price
        dry_sell_cost = self.dry_live_sell_cost(gross_proceeds, closes_account=True)
        proceeds = max(0.0, gross_proceeds - dry_sell_cost)
        total_out = pos.realized_sol + proceeds
        pnl = total_out - pos.cost_sol - pos.dry_live_cost_sol
        self.stats.realized_pnl_sol += pnl
        self.stats.closes += 1
        if killed:
            self.stats.kills += 1
        if pnl >= 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.best_mult = max(self.stats.best_mult, pos.peak_mult)
        self.closed_recent[mint] = ts_ms
        log(
            f"BIRTH-CLOSE {short_addr(mint)} reason={reason} age={pos.age_sec(ts_ms):.2f}s "
            f"state={pos.state} mult={pos.last_mult:.3f} peak={pos.peak_mult:.3f} "
            f"pnl={pnl:+.5f} SOL session={self.stats.realized_pnl_sol:+.5f} SOL"
            + (
                f" dry_buy_cost={pos.dry_live_cost_sol:.6f} dry_sell_cost={dry_sell_cost:.6f} "
                f"rent_recovered={pos.dry_live_locked_rent_sol if self.dry_live_recover_ata_rent else 0.0:.6f}"
                if self.dry_live_enabled else ""
            )
        )
        self.save_state()
        return pnl

    def open_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            pnl = pos.open_pnl_sol(self.drag)
            if self.dry_live_enabled:
                fill = max(pos.last_price, 0.0) * (1.0 - self.drag)
                gross_proceeds = pos.remaining_tokens * fill
                pnl -= pos.dry_live_cost_sol
                pnl -= self.dry_live_sell_cost(gross_proceeds, closes_account=True)
            total += pnl
        return total


def parse_base64_shred_for_pump_events(shred_result: dict[str, Any], tracked_wallets: set[str]) -> list[PumpEvent]:
    events: list[PumpEvent] = []
    try:
        tx_outer = (shred_result.get("transaction") or {}).get("transaction")
        if not (isinstance(tx_outer, list) and tx_outer):
            return events
        raw = base64.b64decode(tx_outer[0])
        vt = VersionedTransaction.from_bytes(raw)
        keys = list(vt.message.account_keys)
        if not keys:
            return events
        signer = str(keys[0])
        slot = int(shred_result.get("slot") or 0)
        sig = str(shred_result.get("signature") or "")
        ts_ms = now_ms()
        recv_ns = now_ns()
        for ix in vt.message.instructions:
            program = get_key(keys, int(ix.program_id_index))
            if program not in (PUMP_PROGRAM, PUMP_AMM_PROGRAM):
                continue
            data = bytes(ix.data)
            if len(data) < 8:
                continue
            disc = data[:8]
            accounts = list(ix.accounts)
            # 2026-05-10 — AMM trade parsing for moonshot detection.
            # PumpSwap (post-migration) trades flow through PUMP_AMM_PROGRAM.
            # Account layout (both buy and sell, first 5 are stable):
            #   0=pool, 1=user, 2=global_config, 3=base_mint, 4=quote_mint
            # Data layout per disc:
            #   AMM regular buy:  u64(base_amount_out) + u64(max_quote_amount_in)
            #   buy_exact_quote_in: u64(spend_lamports) + u64(min_base_out) + 1B
            #   AMM sell:         u64(base_amount) + u64(min_quote_out)
            # We use the args (intent) as approximate sol/token amounts —
            # good enough for moonshot accumulation detection.
            if program == PUMP_AMM_PROGRAM:
                if len(data) < 24:
                    continue
                if disc not in (DISC_AMM_BUY, DISC_AMM_BUY_EXACT_QUOTE_IN, DISC_AMM_SELL):
                    continue
                mint = get_account_key(keys, accounts, 3)
                # If account[3] is WSOL, the layout differs (some sell variants
                # have base_mint at index 4). Try [4] in that case. If still
                # WSOL or empty, skip.
                if mint == "So11111111111111111111111111111111111111112" or not mint:
                    mint = get_account_key(keys, accounts, 4)
                if not mint or mint == "So11111111111111111111111111111111111111112":
                    continue
                user = get_account_key(keys, accounts, 1) or signer
                arg_a = int.from_bytes(data[8:16], "little")
                arg_b = int.from_bytes(data[16:24], "little")
                is_buy = disc in (DISC_AMM_BUY, DISC_AMM_BUY_EXACT_QUOTE_IN)
                if disc == DISC_AMM_BUY_EXACT_QUOTE_IN:
                    sol_lamports = arg_a
                    token_amount = arg_b
                    instruction_kind = "amm_buy_exact_quote_in"
                elif disc == DISC_AMM_BUY:
                    token_amount = arg_a
                    sol_lamports = arg_b
                    instruction_kind = "amm_buy"
                else:
                    token_amount = arg_a
                    # AMM SELL: arg_b is min_quote_out (slippage floor), so
                    # actual SOL received is typically 1.5-2× higher.
                    # Scale up to better estimate REAL sell pressure for
                    # downstream filters (sell_ratio, etc).
                    amm_sell_scale = env_float("BIRTH_AMM_SELL_SOL_SCALE", 1.7)
                    sol_lamports = int(arg_b * amm_sell_scale)
                    instruction_kind = "amm_sell"
                max_trade_lamports = int(env_float("BIRTH_MAX_DECODED_TRADE_SOL", 250.0) * 1_000_000_000)
                if sol_lamports > max_trade_lamports:
                    continue
                # Set price_hint from sol/token args (intent-based but
                # gives queue_or_fill a non-zero price to fill immediately
                # instead of expiring with no_curve_price).
                price_hint = (sol_lamports / max(token_amount, 1)) if token_amount > 0 else 0.0
                events.append(PumpEvent(
                    ts_ms=ts_ms,
                    recv_ns=recv_ns,
                    sig=sig,
                    slot=slot,
                    signer=signer,
                    kind="trade",
                    mint=mint,
                    bonding_curve="",
                    user=user,
                    is_buy=is_buy,
                    sol_lamports=sol_lamports,
                    token_amount=token_amount,
                    tracked=(signer in tracked_wallets) or (user in tracked_wallets),
                    instruction_kind=instruction_kind,
                    price_hint=price_hint,
                ))
                continue
            if disc in {DISC_CREATE, DISC_CREATE_V2}:
                is_v2 = disc == DISC_CREATE_V2
                mint = get_account_key(keys, accounts, 0)
                curve = get_account_key(keys, accounts, 2)
                user = get_account_key(keys, accounts, 5 if is_v2 else 7) or signer
                name = symbol = uri = ""
                creator = user
                is_mayhem = False
                try:
                    off = 8
                    name, off = parse_borsh_string(data, off)
                    symbol, off = parse_borsh_string(data, off)
                    uri, off = parse_borsh_string(data, off)
                    if off + 32 <= len(data):
                        creator = str(Pubkey.from_bytes(data[off:off + 32]))
                        off += 32
                    if is_v2 and off < len(data):
                        is_mayhem = data[off] != 0
                except Exception:
                    creator = user
                if mint:
                    events.append(PumpEvent(
                        ts_ms=ts_ms,
                        recv_ns=recv_ns,
                        sig=sig,
                        slot=slot,
                        signer=signer,
                        kind="create",
                        mint=mint,
                        bonding_curve=curve,
                        user=user,
                        creator=creator,
                        name=name,
                        symbol=symbol,
                        uri=uri,
                        create_version="create_v2" if is_v2 else "create",
                        is_mayhem=is_mayhem,
                        tracked=(signer in tracked_wallets) or (user in tracked_wallets) or (creator in tracked_wallets),
                        instruction_kind="create_v2" if is_v2 else "create",
                    ))
                continue
            if disc in {DISC_BUY, DISC_BUY_EXACT_SOL_IN, DISC_SELL}:
                if len(data) < 24:
                    continue
                mint = get_account_key(keys, accounts, 2)
                curve = get_account_key(keys, accounts, 3)
                user = get_account_key(keys, accounts, 6) or signer
                is_buy = disc in {DISC_BUY, DISC_BUY_EXACT_SOL_IN}
                max_trade_lamports = int(env_float("BIRTH_MAX_DECODED_TRADE_SOL", 250.0) * 1_000_000_000)
                if disc == DISC_BUY_EXACT_SOL_IN:
                    first_u64 = int.from_bytes(data[8:16], "little")
                    second_u64 = int.from_bytes(data[16:24], "little")
                    if first_u64 > max_trade_lamports and 0 < second_u64 <= max_trade_lamports:
                        # Some buy-exact variants carry token amount first and
                        # max SOL second. Do not let the token amount become a
                        # fake billion-SOL buy in the birth ledger.
                        token_amount = first_u64
                        sol_lamports = second_u64
                    else:
                        sol_lamports = first_u64
                        token_amount = second_u64
                    instruction_kind = "buy_exact_sol_in"
                else:
                    token_amount = int.from_bytes(data[8:16], "little")
                    sol_lamports = int.from_bytes(data[16:24], "little")
                    instruction_kind = "buy" if disc == DISC_BUY else "sell"
                if not mint or sol_lamports <= 0:
                    continue
                if sol_lamports > max_trade_lamports:
                    continue
                price_hint = 0.0
                if token_amount > 0 and sol_lamports > 0:
                    price_hint = sol_lamports / max(token_amount, 1)
                events.append(PumpEvent(
                    ts_ms=ts_ms,
                    recv_ns=recv_ns,
                    sig=sig,
                    slot=slot,
                    signer=signer,
                    kind="trade",
                    mint=mint,
                    bonding_curve=curve,
                    is_buy=is_buy,
                    sol_lamports=sol_lamports,
                    token_amount=token_amount,
                    user=user,
                    tracked=(signer in tracked_wallets) or (user in tracked_wallets),
                    instruction_kind=instruction_kind,
                    price_hint=price_hint,
                ))
                continue
            if disc == DISC_MIGRATE:
                mint = get_account_key(keys, accounts, 2) or get_account_key(keys, accounts, 0)
                curve = get_account_key(keys, accounts, 3) or ""
                if mint:
                    events.append(PumpEvent(
                        ts_ms=ts_ms,
                        recv_ns=recv_ns,
                        sig=sig,
                        slot=slot,
                        signer=signer,
                        kind="migrate",
                        mint=mint,
                        bonding_curve=curve,
                        instruction_kind="migrate",
                    ))
    except Exception:
        return events
    return events


class BirthFirstSniper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.bc = BondingCurveCache()
        self.broker = PaperBroker(config)
        self.logger = JsonlLogger(config)
        self.tapes: dict[str, MintTape] = {}
        self.tracked_wallets = self.load_tracked(config.snipers_file)
        self.stop_event = asyncio.Event()
        self.seen_keys: Deque[tuple[str, str, str, int, int]] = deque()
        self.seen_set: set[tuple[str, str, str, int, int]] = set()
        self.seen_limit = 50000
        self.last_report_at = time.time()
        ts = now_ms()
        self.last_shred_msg_ms = ts
        self.last_curve_msg_ms = ts
        # Phase 11 2026-05-08: smart-wallet WS tracker. Subscribes to PumpPortal
        # subscribeAccountTrade for known alpha wallets. Provides the missing
        # coordinated-buy signal — when 1+ smart wallet buys a mint we're
        # evaluating, that's a strong moonshot pre-confirmation per Marino paper.
        self.smart_wallet_tracker = None
        if env_bool("SMART_WALLET_WS_ENABLED", True):
            try:
                from pumpportal_smart_wallet import SmartWalletTracker
                self.smart_wallet_tracker = SmartWalletTracker(
                    log_fn=log,
                    window_sec=env_float("SMART_WALLET_WINDOW_SEC", 30.0),
                )
                log(f"PHASE11: SmartWalletTracker initialized with {len(self.smart_wallet_tracker.wallets)} wallets")
            except Exception as exc:
                log(f"PHASE11: smart-wallet tracker init failed {type(exc).__name__}: {exc}")
                self.smart_wallet_tracker = None
        # Phase 15 2026-05-08: signal-scraping layer.
        # (A) RugCheck pre-buy gate: rejects rug-pattern tokens before strike.
        # (B) Pump.fun engagement poller: livestream viewers + reply count
        #     signals retail bots can't compute from raw on-chain data.
        self.rugcheck_client = None
        if env_bool("RUGCHECK_ENABLED", True):
            try:
                from rugcheck_client import RugCheckClient
                self.rugcheck_client = RugCheckClient(
                    log_fn=log,
                    reject_score=env_int("RUGCHECK_REJECT_SCORE", 4),
                    timeout_sec=env_float("RUGCHECK_TIMEOUT_SEC", 0.45),
                    cache_ttl_sec=env_float("RUGCHECK_CACHE_TTL_SEC", 300.0),
                )
                log(f"PHASE15A: RugCheckClient initialized reject_score={self.rugcheck_client.reject_score}")
            except Exception as exc:
                log(f"PHASE15A: RugCheck init failed {type(exc).__name__}: {exc}")
                self.rugcheck_client = None
        self.engagement_poller = None
        if env_bool("ENGAGEMENT_POLL_ENABLED", True):
            try:
                from pumpfun_engagement import PumpfunEngagementPoller
                self.engagement_poller = PumpfunEngagementPoller(
                    log_fn=log,
                    poll_sec=env_float("ENGAGEMENT_POLL_SEC", 4.0),
                    limit=env_int("ENGAGEMENT_POLL_LIMIT", 50),
                )
                log(f"PHASE15B: EngagementPoller initialized poll={self.engagement_poller.poll_sec}s")
            except Exception as exc:
                log(f"PHASE15B: engagement poller init failed {type(exc).__name__}: {exc}")
                self.engagement_poller = None

    @staticmethod
    def load_tracked(path: Path) -> set[str]:
        wallets: set[str] = set()
        if not path.is_file():
            return wallets
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                wallets.add(line.split()[0])
        except Exception as exc:
            log(f"BIRTH: failed to load tracked wallets: {type(exc).__name__}: {exc}")
        return wallets

    def dedup_event(self, event: PumpEvent) -> bool:
        key = (event.sig, event.mint, event.instruction_kind, event.sol_lamports, event.token_amount)
        if key in self.seen_set:
            return False
        self.seen_set.add(key)
        self.seen_keys.append(key)
        while len(self.seen_keys) > self.seen_limit:
            old = self.seen_keys.popleft()
            self.seen_set.discard(old)
        return True

    def tape_for(self, mint: str) -> MintTape:
        tape = self.tapes.get(mint)
        if tape is None:
            tape = MintTape(mint=mint)
            self.tapes[mint] = tape
        return tape

    def on_curve_update(self, value: dict[str, Any], ts_ms: int) -> None:
        if self.bc.update_from_program_value(value, ts_ms):
            self.broker.stats.curve_updates = self.bc.updates
            self.last_curve_msg_ms = ts_ms

    async def on_event(self, event: PumpEvent) -> None:
        if not self.dedup_event(event):
            return
        if event.bonding_curve:
            self.bc.remember(event.mint, event.bonding_curve)
        curve = self.bc.latest_for_mint(event.mint, self.config.curve_max_age_ms, event.ts_ms)
        tape = self.tape_for(event.mint)
        tape.add_event(event, self.config.max_tape_age_sec)
        if curve and curve.price > 0:
            tape.add_price(event.ts_ms, curve.price, self.config.max_tape_age_sec)
        self.logger.raw_event(event, curve)

        if event.kind == "create":
            self.broker.stats.creates += 1
            if self.config.print_events:
                log(
                    f"BIRTH-CREATE {short_addr(event.mint)} v={event.create_version} "
                    f"creator={short_addr(event.creator)} mayhem={int(event.is_mayhem)}"
                )
            return
        if event.kind == "migrate":
            await self.close_if_open(event.mint, event.ts_ms, "migration_seen", killed=False)
            return
        if event.kind != "trade":
            return

        self.broker.stats.trades += 1
        if event.is_buy:
            self.broker.stats.buys += 1
        else:
            self.broker.stats.sells += 1
        if self.config.print_events:
            side = "B" if event.is_buy else "S"
            log(
                f"BIRTH-TAPE {side} {short_addr(event.mint)} sol={event.sol:.4f} "
                f"age={tape.age_ms(event.ts_ms)}ms tracked={int(event.tracked)}"
            )

        await self.manage_existing(event.mint, event.ts_ms)
        if event.is_buy:
            await self.maybe_plan_strike(event, curve)

    def feature_snapshot(self, mint: str, ts_ms: int) -> Optional[dict[str, Any]]:
        tape = self.tapes.get(mint)
        if not tape:
            return None
        curve = self.bc.latest_for_mint(mint, self.config.curve_max_age_ms, ts_ms)
        price = curve.price if curve else tape.last_price
        if price and price > 0:
            tape.add_price(ts_ms, price, self.config.max_tape_age_sec)
        s250 = tape.stats(250, ts_ms)
        s700 = tape.stats(700, ts_ms)
        s1500 = tape.stats(1500, ts_ms)
        s3000 = tape.stats(3000, ts_ms)
        s8000 = tape.stats(8000, ts_ms)
        m250 = self.bc.move_for_mint(mint, 250, self.config.curve_max_age_ms, ts_ms)
        m700 = self.bc.move_for_mint(mint, 700, self.config.curve_max_age_ms, ts_ms)
        m1500 = self.bc.move_for_mint(mint, 1500, self.config.curve_max_age_ms, ts_ms)
        move250 = m250[0] if m250 else 1.0
        move700 = m700[0] if m700 else (1.0 + max(0.0, s700.price_change))
        move1500 = m1500[0] if m1500 else (1.0 + max(0.0, s1500.price_change))
        complete = bool(curve.complete) if curve else False
        vsol_sol = curve.vsol_sol if curve else 0.0
        age_ms = tape.age_ms(ts_ms)
        buy_age_ms = tape.buy_age_ms(ts_ms)
        sell_ratio700 = s700.sell_ratio
        concentration = s1500.top_buy_share
        score = self.birth_score(tape, s250, s700, s1500, move250, move700, move1500, complete)
        return {
            "ts_ms": ts_ms,
            "price": price or 0.0,
            "has_curve": bool(curve and curve.price > 0),
            "complete": complete,
            "vsol_sol": vsol_sol,
            "age_ms": age_ms,
            "buy_age_ms": buy_age_ms,
            "first_buy_sol": tape.first_buy_sol,
            "first_buyer": tape.first_buyer,
            "creator": tape.creator,
            "create_version": tape.create_version,
            "is_mayhem": tape.is_mayhem,
            "move250": move250,
            "move700": move700,
            "move1500": move1500,
            "sell_ratio700": sell_ratio700,
            "concentration1500": concentration,
            "off_peak": tape.off_peak(),
            "time_since_peak": tape.time_since_peak_sec(ts_ms),
            "score": score,
            "s250": asdict(s250),
            "s700": asdict(s700),
            "s1500": asdict(s1500),
            "s3000": asdict(s3000),
            "s8000": asdict(s8000),
        }

    @staticmethod
    def birth_score(
        tape: MintTape,
        s250: WindowStats,
        s700: WindowStats,
        s1500: WindowStats,
        move250: float,
        move700: float,
        move1500: float,
        complete: bool,
    ) -> float:
        score = 18.0
        score += min(18.0, s700.buy_sol * 180.0)
        score += min(20.0, s1500.buy_sol * 90.0)
        score += min(24.0, s1500.unique_buyers * 6.0)
        score += min(14.0, s700.tracked_buyers * 5.0)
        score += max(0.0, math.log(max(move700, 1e-9))) * 520.0
        score += max(0.0, math.log(max(move1500, 1e-9))) * 260.0
        if tape.first_create_ms:
            score += 5.0
        if tape.first_buy_sol >= 0.05:
            score += 5.0
        if s250.buy_sol >= 0.02 and s250.sells == 0:
            score += 5.0
        score -= min(28.0, s1500.sell_ratio * 28.0)
        score -= max(0.0, s1500.top_buy_share - 0.82) * 42.0
        score -= max(0.0, s1500.f_lt_50ms - 0.65) * 24.0
        score -= max(0.0, 1.0 - move250) * 160.0
        if tape.is_mayhem:
            score -= 4.0
        if complete:
            score -= 100.0
        return score

    async def maybe_plan_strike(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        ts_ms = event.ts_ms
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return
        ok, _ = self.broker.can_strike(event.mint, ts_ms)
        if not ok:
            return
        features = self.feature_snapshot(event.mint, ts_ms)
        if not features:
            return
        plan = self.build_strike_plan(event, features)
        if not plan:
            return
        price = float(features.get("price") or 0.0)
        self.logger.decision(
            "strike_plan",
            event.mint,
            {
                "lane": plan.lane,
                "reason": plan.reason,
                "score": plan.score,
                "scout_sol": plan.scout_sol,
                "target_sol": plan.target_sol,
                "needs_curve_fill": plan.needs_curve_fill,
                "features": self.slim_features(features),
            },
        )
        pos = self.broker.queue_or_fill(plan, price)
        if pos:
            self.logger.decision("open", event.mint, {"lane": plan.lane, "features": self.slim_features(features)})

    def build_strike_plan(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        if features["complete"]:
            return None
        age_ms = int(features["age_ms"])
        buy_age_ms = int(features["buy_age_ms"])
        if age_ms > self.config.birth_max_age_ms:
            return None
        s250 = features["s250"]
        s700 = features["s700"]
        s1500 = features["s1500"]
        sell_ratio = s1500["sell_sol"] / max(s1500["buy_sol"], 0.001)
        if s700["sell_sol"] > max(0.006, s700["buy_sol"] * self.config.max_initial_sell_ratio):
            return None
        if features["off_peak"] < 0.88 and s1500["sell_sol"] > 0:
            return None

        lane = ""
        reason = ""
        if (
            buy_age_ms <= self.config.first_buy_max_age_ms
            and s1500["buys"] <= 2
            and s700["sells"] == 0
            and self.config.first_buy_min_sol <= event.sol <= self.config.first_buy_max_sol
        ):
            lane = "first_public_buy"
            reason = f"first_buy={event.sol:.4f} age={buy_age_ms}ms create={int(bool(features['create_version']))}"
        elif (
            age_ms <= 1400
            and s700["unique_buyers"] >= 2
            and s700["buy_sol"] >= self.config.two_wallet_buy_sol
            and sell_ratio <= 0.20
        ):
            lane = "two_wallet_birth"
            reason = f"uniq700={s700['unique_buyers']} buy700={s700['buy_sol']:.4f} sell_ratio={sell_ratio:.2f}"
        elif (
            age_ms <= self.config.birth_max_age_ms
            and s1500["unique_buyers"] >= 3
            and s1500["buy_sol"] >= self.config.velocity_buy_sol
            and sell_ratio <= 0.28
            and (features["move700"] >= 1.001 or features["move1500"] >= 1.003 or not features["has_curve"])
        ):
            lane = "velocity_birth"
            reason = f"uniq1500={s1500['unique_buyers']} buy1500={s1500['buy_sol']:.4f} move700={features['move700']:.3f}"
        elif (
            age_ms <= self.config.birth_max_age_ms
            and s1500["tracked_buyers"] >= 1
            and s1500["buy_sol"] >= 0.018
            and sell_ratio <= 0.35
        ):
            lane = "tracked_birth_boost"
            reason = f"tracked={s1500['tracked_buyers']} buy1500={s1500['buy_sol']:.4f}"
        else:
            return None

        score = float(features["score"])
        if lane == "first_public_buy":
            scout = min(self.config.scout_sol, max(0.0008, self.config.max_position_sol * 0.06))
        else:
            scout = self.config.scout_sol
        quality = max(0.0, min(1.0, (score - 34.0) / 42.0))
        target = scout + (self.config.max_position_sol - scout) * quality
        target_floor = {
            "first_public_buy": 0.55,
            "two_wallet_birth": 0.70,
            "velocity_birth": 0.85,
            "tracked_birth_boost": 0.75,
        }.get(lane, 0.50)
        target = max(target, self.config.max_position_sol * target_floor)
        if lane in {"velocity_birth", "tracked_birth_boost"} and score >= 55.0:
            target = max(target, self.config.max_position_sol * 0.65)
        target = max(scout, min(self.config.max_position_sol, target))
        price = float(features["price"] or 0.0)
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane=lane,
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=price <= 0,
            features=self.slim_features(features),
        )

    @staticmethod
    def pending_fill_ready(pending: PendingStrike, features: dict[str, Any]) -> tuple[bool, str]:
        if pending.plan.lane != "first_public_buy":
            return True, "non_first_lane"
        s700 = features["s700"]
        s1500 = features["s1500"]
        first_buy_sol = max(float(features.get("first_buy_sol") or 0.0), 0.0)
        sell_ratio700 = s700["sell_sol"] / max(s700["buy_sol"], 0.001)
        sell_ratio1500 = s1500["sell_sol"] / max(s1500["buy_sol"], 0.001)
        second_buyer = (
            s700["unique_buyers"] >= 2
            and s700["buy_sol"] >= max(0.075, first_buy_sol + 0.018)
            and s700["sell_sol"] <= max(0.006, s700["buy_sol"] * 0.12)
        )
        early_breadth = (
            s1500["unique_buyers"] >= 3
            and s1500["buy_sol"] >= max(0.120, first_buy_sol + 0.035)
            and sell_ratio1500 <= 0.18
            and s1500["top_buy_share"] <= 0.78
        )
        curve_alive = (
            (features["move250"] >= 1.001 or features["move700"] >= 1.003)
            and sell_ratio700 <= 0.18
            and s700["unique_buyers"] >= 2
        )
        if second_buyer or early_breadth or curve_alive:
            return True, (
                "second_buyer"
                if second_buyer
                else "early_breadth"
                if early_breadth
                else "curve_alive"
            )
        return False, "waiting_second_buyer"

    async def manage_existing(self, mint: str, ts_ms: int) -> None:
        features = self.feature_snapshot(mint, ts_ms)
        if features:
            curve_price = float(features["price"] or 0.0)
            if mint in self.broker.pending and curve_price > 0:
                pending = self.broker.pending.get(mint)
                ready, why = self.pending_fill_ready(pending, features) if pending else (False, "missing_pending")
                if ready:
                    pos = self.broker.fill_pending(mint, ts_ms, curve_price)
                    if pos:
                        self.logger.decision(
                            "pending_fill",
                            mint,
                            {"reason": why, "features": self.slim_features(features)},
                        )
                elif pending and ts_ms >= pending.expires_ts_ms:
                    self.broker.expire_pending(mint, ts_ms, why)
                    self.logger.decision("pending_expired", mint, {"reason": why, "features": self.slim_features(features)})
            pos = self.broker.positions.get(mint)
            if pos and curve_price > 0:
                await self.manage_position(pos, ts_ms, curve_price, features)

    async def manage_position(self, pos: Position, ts_ms: int, price: float, features: dict[str, Any]) -> None:
        mint = pos.mint
        mult = pos.update(price)
        self.broker.stats.best_mult = max(self.broker.stats.best_mult, pos.peak_mult)
        dist = self.distribution_reason(pos, features)
        if dist:
            self.close_position(mint, ts_ms, price, f"kill_{dist}", features, killed=True)
            return
        if features["complete"]:
            self.close_position(mint, ts_ms, price, "migration_complete", features, killed=False)
            return

        if pos.state == "SCOUT":
            if mult <= 0.78:
                self.close_position(mint, ts_ms, price, "scout_hard_break", features, killed=True)
                return
            scale_reason = self.scale1_reason(pos, features)
            if scale_reason:
                target_after_scale = min(pos.target_sol * 0.50, self.config.max_position_sol)
                add_sol = max(0.0, target_after_scale - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "SCALE1", scale_reason)
                if scaled:
                    self.logger.decision(
                        "scale1",
                        mint,
                        {"add_sol": add_sol, "reason": scale_reason, "features": self.slim_features(features)},
                    )
                return
            if pos.age_sec(ts_ms) >= 2.20 and pos.peak_mult < 1.08:
                self.close_position(mint, ts_ms, price, "first_wave_failed", features, killed=True)
                return
            if pos.age_sec(ts_ms) >= 5.0 and pos.peak_mult < 1.18:
                self.close_position(mint, ts_ms, price, "no_birth_expansion", features, killed=True)
                return

        if pos.state == "SCALE1":
            if mult <= 0.90:
                self.close_position(mint, ts_ms, price, "scale1_failed", features, killed=True)
                return
            if not pos.derisk_done and self.derisk_gate(pos, features):
                self.broker.partial(mint, 0.45, price, "birth_pop_finance_runner")
                self.logger.decision("derisk", mint, {"features": self.slim_features(features)})
                return
            if not pos.derisk_done and pos.age_sec(ts_ms) >= 7.0 and mult < 1.12:
                self.close_position(mint, ts_ms, price, "scale1_stalled", features, killed=False)
                return

        if pos.state in {"RUNNER", "RUNNER_FULL"}:
            if self.scale2_reason(pos, features):
                add_sol = max(0.0, pos.target_sol - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "RUNNER_FULL", "second_birth_wave")
                if scaled:
                    self.logger.decision("scale2", mint, {"add_sol": add_sol, "features": self.slim_features(features)})
                    return
            if pos.peak_mult >= 2.0 and mult <= pos.peak_mult * 0.72:
                self.close_position(mint, ts_ms, price, "runner_big_trail", features, killed=False)
                return
            if pos.peak_mult >= 1.45 and mult <= pos.peak_mult * 0.80 and features["s700"]["sell_sol"] > 0:
                self.close_position(mint, ts_ms, price, "runner_sell_trail", features, killed=False)
                return
            if pos.age_sec(ts_ms) >= 45.0 and mult < 1.22:
                self.close_position(mint, ts_ms, price, "runner_no_followthrough", features, killed=False)
                return

        if pos.age_sec(ts_ms) >= 90.0:
            self.close_position(mint, ts_ms, price, "hard_90s_time_stop", features, killed=False)

    @staticmethod
    def distribution_reason(pos: Position, features: dict[str, Any]) -> Optional[str]:
        s250 = features["s250"]
        s700 = features["s700"]
        s1500 = features["s1500"]
        if s250["sell_sol"] >= max(0.010, s250["buy_sol"] * 0.80) and features["move250"] < 0.998:
            return "instant_sell_shock"
        if s700["sell_sol"] >= max(0.018, s700["buy_sol"] * 0.55) and features["move700"] < 0.997:
            return "unabsorbed_first_sell"
        if s1500["top_buyer_flip"] >= 0.35 and pos.peak_mult < 1.35:
            return "top_buyer_flip"
        if features["off_peak"] < 0.72 and pos.age_sec(features["ts_ms"]) <= 8.0:
            return "birth_collapse"
        if pos.state == "SCOUT" and pos.peak_mult >= 1.18 and pos.last_mult < 0.96:
            return "round_trip_before_scale"
        return None

    @staticmethod
    def scale1_reason(pos: Position, features: dict[str, Any]) -> Optional[str]:
        if pos.scale1_done:
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        sell_ratio1500 = s1500["sell_sol"] / max(s1500["buy_sol"], 0.001)
        clean_second_wave = (
            pos.age_sec(features["ts_ms"]) <= 6.0
            and pos.last_mult >= 1.12
            and features["move700"] >= 1.003
            and s700["buy_sol"] >= 0.030
            and s1500["unique_buyers"] >= 3
            and s700["sell_sol"] <= max(0.008, s700["buy_sol"] * 0.18)
            and s1500["top_buy_share"] <= 0.82
        )
        if clean_second_wave:
            return "clean_second_wave"
        violent_moon = (
            pos.age_sec(features["ts_ms"]) <= 4.0
            and pos.last_mult >= 1.25
            and s1500["buy_sol"] >= 0.080
            and s1500["unique_buyers"] >= 3
            and sell_ratio1500 <= 0.22
        )
        if violent_moon:
            return "violent_birth_moon"
        return None

    @staticmethod
    def derisk_gate(pos: Position, features: dict[str, Any]) -> bool:
        if pos.last_mult >= 1.85:
            return True
        if pos.last_mult >= 1.45 and features["s700"]["sell_sol"] > features["s700"]["buy_sol"] * 0.10:
            return True
        if pos.last_mult >= 1.35 and features["move250"] < 0.999:
            return True
        return False

    @staticmethod
    def scale2_reason(pos: Position, features: dict[str, Any]) -> Optional[str]:
        if pos.scale2_done or not pos.derisk_done:
            return None
        s1500 = features["s1500"]
        if (
            pos.last_mult >= 1.65
            and features["move700"] >= 1.004
            and s1500["unique_buyers"] >= 4
            and s1500["buy_sol"] >= 0.10
            and s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) <= 0.25
            and pos.cost_sol < pos.target_sol
        ):
            return "second_birth_wave"
        return None

    def close_position(self, mint: str, ts_ms: int, price: float, reason: str, features: dict[str, Any], killed: bool) -> None:
        pnl = self.broker.close(mint, ts_ms, price, reason, killed)
        self.logger.decision(
            "close",
            mint,
            {"reason": reason, "pnl_sol": pnl, "killed": killed, "features": self.slim_features(features)},
        )

    async def close_if_open(self, mint: str, ts_ms: int, reason: str, killed: bool) -> None:
        pos = self.broker.positions.get(mint)
        if not pos:
            return
        features = self.feature_snapshot(mint, ts_ms)
        price = float(features.get("price") or pos.last_price) if features else pos.last_price
        if features and price > 0:
            self.close_position(mint, ts_ms, price, reason, features, killed)
        elif price > 0:
            self.broker.close(mint, ts_ms, price, reason, killed)

    @staticmethod
    def slim_features(features: dict[str, Any]) -> dict[str, Any]:
        return {
            "price": features["price"],
            "has_curve": features["has_curve"],
            "complete": features["complete"],
            "vsol_sol": features["vsol_sol"],
            "age_ms": features["age_ms"],
            "buy_age_ms": features["buy_age_ms"],
            "first_buy_sol": features["first_buy_sol"],
            "create_version": features["create_version"],
            "is_mayhem": features["is_mayhem"],
            "move250": features["move250"],
            "move700": features["move700"],
            "move1500": features["move1500"],
            "score": features["score"],
            "buy700": features["s700"]["buy_sol"],
            "sell700": features["s700"]["sell_sol"],
            "uniq700": features["s700"]["unique_buyers"],
            "buy1500": features["s1500"]["buy_sol"],
            "sell1500": features["s1500"]["sell_sol"],
            "uniq1500": features["s1500"]["unique_buyers"],
            "top_share1500": features["s1500"]["top_buy_share"],
            "top_flip1500": features["s1500"]["top_buyer_flip"],
        }

    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            ts = now_ms()
            for mint in list(self.broker.pending.keys()):
                features = self.feature_snapshot(mint, ts)
                price = float(features.get("price") or 0.0) if features else 0.0
                if price > 0:
                    pending = self.broker.pending.get(mint)
                    ready, why = self.pending_fill_ready(pending, features) if pending and features else (False, "missing_features")
                    if ready:
                        pos = self.broker.fill_pending(mint, ts, price)
                        if pos and features:
                            self.logger.decision(
                                "pending_fill",
                                mint,
                                {"reason": why, "features": self.slim_features(features)},
                            )
                        continue
                pending = self.broker.pending.get(mint)
                if pending and ts >= pending.expires_ts_ms:
                    reason = "no_curve_price" if price <= 0 else "waiting_second_buyer"
                    self.broker.expire_pending(mint, ts, reason)
                    payload = {"reason": reason}
                    if features:
                        payload["features"] = self.slim_features(features)
                    self.logger.decision("pending_expired", mint, payload)

            for mint, pos in list(self.broker.positions.items()):
                features = self.feature_snapshot(mint, ts)
                if not features:
                    continue
                price = float(features.get("price") or 0.0)
                if price > 0:
                    await self.manage_position(pos, ts, price, features)
            self.report_if_due()
            await asyncio.sleep(self.config.heartbeat_sec)

    def report_if_due(self, force: bool = False) -> None:
        if not force and time.time() - self.last_report_at < self.config.report_sec:
            return
        self.last_report_at = time.time()
        ts = now_ms()
        shred_age = max(0.0, (ts - self.last_shred_msg_ms) / 1000.0)
        curve_age = max(0.0, (ts - self.last_curve_msg_ms) / 1000.0)
        open_bits = [
            f"{short_addr(m)} {p.state} {p.last_mult:.2f}x pk={p.peak_mult:.2f} cost={p.cost_sol:.4f}"
            for m, p in self.broker.positions.items()
        ]
        open_text = ", ".join(open_bits) if open_bits else "none"
        st = self.broker.stats
        log(
            f"BIRTH-STATUS creates={st.creates} trades={st.trades} buys/sells={st.buys}/{st.sells} "
            f"plans={st.strike_plans} pend={len(self.broker.pending)} filled={st.pending_filled} "
            f"expired={st.pending_expired} scouts={st.scouts} scale1={st.scale1} "
            f"scale2={st.scale2} partials={st.partials} closes={st.closes} "
            f"W/L={st.wins}/{st.losses} kills={st.kills} best={st.best_mult:.2f}x "
            f"realized={st.realized_pnl_sol:+.5f} open_pnl={self.broker.open_pnl():+.5f} SOL "
            f"open={len(self.broker.positions)} [{open_text}] shreds={st.shreds} "
            f"bc={self.bc.updates} age={shred_age:.1f}/{curve_age:.1f}s reconn={st.reconnects}"
        )

    async def combined_stream_loop(self) -> None:
        if not self.config.st_rpc_ws:
            raise RuntimeError("Missing SOLANATRACKER_RPC_WS or SOLANATRACKER_RPC_KEY")
        reconnect_base_sec = env_float("BIRTH_RECONNECT_BASE_SEC", 2.0)
        reconnect_clean_wait_sec = env_float("BIRTH_RECONNECT_CLEAN_WAIT_SEC", 3.0)
        reconnect_max_sec = env_float("BIRTH_RECONNECT_MAX_SEC", 30.0)
        reconnect_policy_limit_sec = env_float("BIRTH_RECONNECT_POLICY_LIMIT_SEC", 45.0)
        reconnect_delay = reconnect_base_sec
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
                        "id": 61001,
                        "method": "shredSubscribe",
                        "params": [
                            {"accountInclude": [PUMP_PROGRAM, PUMP_AMM_PROGRAM], "accountRequired": [], "vote": False},
                            {
                                "encoding": "base64",
                                "transactionDetails": "full",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    }
                    curve_sub = {
                        "jsonrpc": "2.0",
                        "id": 61002,
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
                    await ws.send(json.dumps(curve_sub))
                    ts = now_ms()
                    self.last_shred_msg_ms = ts
                    self.last_curve_msg_ms = ts
                    log("BIRTH: subscribed to combined pump.fun shreds + BondingCurve cache")
                    reconnect_delay = reconnect_base_sec
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            stale = max(0.0, (now_ms() - self.last_shred_msg_ms) / 1000.0)
                            if stale >= self.config.shred_stall_reconnect_sec:
                                self.broker.stats.reconnects += 1
                                log(f"BIRTH: shreds stalled for {stale:.1f}s, reconnecting")
                                break
                            continue
                        data = json.loads(raw)
                        method = str(data.get("method") or "").lower()
                        ts = now_ms()
                        if "program" in method:
                            value = (((data.get("params") or {}).get("result") or {}).get("value") or {})
                            self.on_curve_update(value, ts)
                        elif "shred" in method:
                            result = ((data.get("params") or {}).get("result") or {})
                            sig = str(result.get("signature") or "")
                            if sig:
                                self.last_shred_msg_ms = ts
                                self.broker.stats.shreds += 1
                                for event in parse_base64_shred_for_pump_events(result, self.tracked_wallets):
                                    await self.on_event(event)
                        if "shred" not in method:
                            stale = max(0.0, (now_ms() - self.last_shred_msg_ms) / 1000.0)
                            if stale >= self.config.shred_stall_reconnect_sec:
                                self.broker.stats.reconnects += 1
                                log(f"BIRTH: shreds stalled for {stale:.1f}s while stream stayed open, reconnecting")
                                break
                if not self.stop_event.is_set() and reconnect_clean_wait_sec > 0:
                    await asyncio.sleep(reconnect_clean_wait_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.broker.stats.reconnects += 1
                msg = str(exc)
                if "Connection limit" in msg or "policy violation" in msg or "1008" in msg:
                    delay = reconnect_policy_limit_sec
                else:
                    delay = reconnect_delay
                    reconnect_delay = min(reconnect_max_sec, max(reconnect_base_sec, reconnect_delay * 1.7))
                log(f"BIRTH: stream error, reconnecting in {delay:.1f}s: {type(exc).__name__}: {exc}")
                await asyncio.sleep(max(0.0, delay))

    async def run(self) -> None:
        if not self.config.paper_trading and not self.config.live_enabled:
            raise RuntimeError(
                "Live execution is not enabled in this file yet. This bot must pass paper validation first."
            )
        mode = "PAPER" if self.config.paper_trading else "LIVE"
        log(
            f"BIRTH: starting mode={mode} scout={self.config.scout_sol:.4f} "
            f"max_pos={self.config.max_position_sol:.4f} max_open={self.config.max_open_positions} "
            f"birth_age={self.config.birth_max_age_ms}ms"
        )
        log(
            f"BIRTH: tracked_wallets={len(self.tracked_wallets)} raw_log={self.config.raw_events_file} "
            f"decisions={self.config.decisions_file}"
        )
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
            asyncio.create_task(self.combined_stream_loop()),
        ]
        if self.smart_wallet_tracker is not None:
            tasks.append(asyncio.create_task(self.smart_wallet_tracker.run()))
            tasks.append(asyncio.create_task(self.smart_wallet_tracker.prune()))
        if self.engagement_poller is not None:
            tasks.append(asyncio.create_task(self.engagement_poller.run()))
        # Phase 20C 2026-05-08: register optional subclass loops by name probe.
        # Lets PGG2.py add poll-driven strike + management loops without
        # needing to override run() (which would duplicate this whole block).
        for fn_name in ("engagement_poll_strike_loop", "engagement_manage_loop"):
            fn = getattr(self, fn_name, None)
            if fn is not None and asyncio.iscoroutinefunction(fn):
                tasks.append(asyncio.create_task(fn()))
                log(f"PHASE20C: registered subclass loop {fn_name}")
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
    parser = argparse.ArgumentParser(description="Birth-first paper sniper for pump.fun launches")
    parser.add_argument("--ws", default="", help="Solana Tracker RPC WebSocket URL")
    parser.add_argument("--state", default="", help="State JSON path")
    parser.add_argument("--raw-log", default="", help="Raw event JSONL path")
    parser.add_argument("--decisions", default="", help="Decision JSONL path")
    parser.add_argument("--snipers", default="", help="Tracked wallet file for bonus only")
    parser.add_argument("--run-seconds", type=float, default=0.0, help="Stop after N seconds; 0 runs forever")
    parser.add_argument("--print-events", action="store_true", help="Print every parsed create/trade")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config = BotConfig.from_env(args)
    bot = BirthFirstSniper(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(async_main())
