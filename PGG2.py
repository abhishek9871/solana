"""
Same Block Piggyback Bot

Paper-first pump.fun scalper for the only regime that can plausibly turn a tiny
bankroll quickly: manufactured same-block / first-second markup.

This is intentionally not a copy-trader and not a "safe token" filter. It tries
to sit immediately behind a coordinated early buyer cluster, scale only while
that cluster keeps buying, and exit when flow stalls or the first distribution
prints. Live execution is still gated; use the paper/replay outputs before any
real wallet touches this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from birth_first_sniper import (
    BASE_DIR,
    DATA_DIR,
    BotConfig as BaseConfig,
    BirthFirstSniper,
    CurvePoint,
    PumpEvent,
    StrikePlan,
    asdict,
    env_bool,
    env_float,
    env_int,
    env_str,
    load_dotenv,
    log,
    now_ms,
    now_ns,
    short_addr,
)


@dataclass
class WaveArm:
    mint: str
    armed_ts_ms: int
    first_seen_ms: int
    first_price: float = 0.0
    first_price_ts_ms: int = 0
    peak_price: float = 0.0
    initial_buy_sol: float = 0.0
    initial_buyers: set[str] = field(default_factory=set)
    initial_sellers: set[str] = field(default_factory=set)
    initial_sell_sol: float = 0.0
    initial_slot_buy_sol: float = 0.0
    initial_slot_buyers: int = 0
    initial_slot_top_share: float = 0.0
    armed_without_curve: bool = False
    last_update_ms: int = 0


def compute_bot_share(tape: Any, max_buys: int = 20) -> float:
    """Phase 12 2026-05-08: anti-bot share classifier.

    Marino arXiv 2602.14860: tokens whose early activity is dominated by
    bot-like transactions exhibit LOWER graduation probability. This is the
    inverse of every retail bot's selection bias — they all chase high-volume
    bot pile-on. We filter AGAINST it.

    Returns a 0.0-1.0 score where higher = more bot-like = avoid.
    Uses three free-RPC-derivable signals:
    1. Inter-arrival entropy (low entropy = regular intervals = bots)
    2. SOL-amount uniqueness (identical amounts = bots)
    3. Slot concentration (multiple buyers in same slot = bundle bots)
    """
    if tape is None or not getattr(tape, "events", None):
        return 0.5  # no data — neutral, don't reject blindly
    buys = []
    for ev in tape.events:
        if getattr(ev, "kind", "") == "trade" and getattr(ev, "is_buy", False):
            buys.append(ev)
            if len(buys) >= max_buys:
                break
    if len(buys) < 5:
        return 0.5  # not enough samples — neutral

    bot_score = 0.0
    weights_sum = 0.0

    # Signal 1: inter-arrival entropy
    intervals = []
    for i in range(1, len(buys)):
        dt = buys[i].ts_ms - buys[i - 1].ts_ms
        if dt > 0:
            intervals.append(dt)
    if intervals:
        bins = [0, 0, 0, 0, 0]  # <100, 100-500, 500-1000, 1000-3000, >3000 ms
        for dt in intervals:
            if dt < 100:
                bins[0] += 1
            elif dt < 500:
                bins[1] += 1
            elif dt < 1000:
                bins[2] += 1
            elif dt < 3000:
                bins[3] += 1
            else:
                bins[4] += 1
        total = len(intervals)
        entropy = 0.0
        for c in bins:
            if c > 0:
                p = c / total
                entropy -= p * math.log2(p)
        norm_entropy = entropy / math.log2(5)  # max entropy for 5 bins
        bot_score += (1.0 - norm_entropy) * 0.4
        weights_sum += 0.4

    # Signal 2: SOL-amount uniqueness (bots use scripted amounts)
    amounts = [getattr(e, "sol_lamports", 0) for e in buys]
    unique_amounts = len(set(amounts))
    sameness = 1.0 - (unique_amounts / len(amounts))
    bot_score += sameness * 0.30
    weights_sum += 0.30

    # Signal 3: slot concentration (multiple buyers in same slot = bundle)
    slots = [getattr(e, "slot", 0) for e in buys]
    unique_slots = len(set(slots))
    slot_concentration = 1.0 - (unique_slots / len(slots))
    bot_score += slot_concentration * 0.30
    weights_sum += 0.30

    if weights_sum > 0:
        return min(1.0, bot_score / weights_sum * (weights_sum))
    return 0.5


def piggy_config(args: argparse.Namespace) -> BaseConfig:
    load_dotenv()
    base = BaseConfig.from_env(args)
    execution_mode = env_str("PGG2_EXECUTION_MODE", "paper").lower()
    paper_mode = execution_mode in {"paper", "dry_live"} and env_bool("PIGGY_PAPER_TRADING", True)
    return replace(
        base,
        paper_trading=paper_mode,
        live_enabled=execution_mode in {"quote", "live"} and env_bool("PGG2_ENABLE_LIVE", False),
        report_sec=env_float("PIGGY_REPORT_SEC", 3.0),
        heartbeat_sec=env_float("PIGGY_HEARTBEAT_SEC", 0.020),
        curve_max_age_ms=env_int("PIGGY_CURVE_MAX_AGE_MS", 650),
        max_tape_age_sec=env_int("PIGGY_MAX_TAPE_AGE_SEC", 90),
        scout_sol=env_float("PIGGY_SCOUT_SOL", 0.0200),
        max_position_sol=env_float("PIGGY_MAX_POSITION_SOL", 0.2000),
        max_open_positions=env_int("PIGGY_MAX_OPEN_POSITIONS", 3),
        max_pending_strikes=env_int("PIGGY_MAX_PENDING_STRIKES", 6),
        min_seconds_between_strikes=env_float("PIGGY_MIN_SECONDS_BETWEEN_STRIKES", 0.04),
        cooldown_sec=env_float("PIGGY_COOLDOWN_SEC", 8.0),
        paper_drag_bps=env_float("PIGGY_PAPER_DRAG_BPS", 280.0),
        birth_max_age_ms=env_int("PIGGY_MAX_AGE_MS", 1350),
        first_buy_max_age_ms=env_int("PIGGY_FIRST_BUY_MAX_AGE_MS", 900),
        pending_fill_ttl_ms=env_int("PIGGY_PENDING_FILL_TTL_MS", 650),
        first_buy_min_sol=env_float("PIGGY_FIRST_BUY_MIN_SOL", 0.30),
        first_buy_max_sol=env_float("PIGGY_FIRST_BUY_MAX_SOL", 4.50),
        two_wallet_buy_sol=env_float("PIGGY_MIN_BUY_700_SOL", 1.20),
        velocity_buy_sol=env_float("PIGGY_MIN_BUY_1200_SOL", 1.75),
        max_initial_sell_ratio=env_float("PIGGY_MAX_INITIAL_SELL_RATIO", 0.06),
        state_file=Path(args.state or env_str("PIGGY_STATE_FILE", str(DATA_DIR / "same_block_piggy_state.json"))),
        raw_events_file=Path(args.raw_log or env_str("PIGGY_RAW_EVENTS_FILE", str(DATA_DIR / "same_block_piggy_raw.jsonl"))),
        decisions_file=Path(args.decisions or env_str("PIGGY_DECISIONS_FILE", str(DATA_DIR / "same_block_piggy_decisions.jsonl"))),
    )


class SameBlockPiggybackBot(BirthFirstSniper):
    def __init__(self, config: BaseConfig) -> None:
        super().__init__(config)
        if not config.paper_trading:
            live_broker = env_str("PGG2_LIVE_BROKER", "raptor").lower()
            if live_broker in {"direct", "direct_pump", "pump"}:
                from pgg2_direct_pump import DirectPumpQuoteBroker

                self.broker = DirectPumpQuoteBroker(config)
            else:
                from pgg2_live_raptor import RaptorLiveBroker

                self.broker = RaptorLiveBroker(config)
        self.wave_arms: dict[str, WaveArm] = {}
        self.position_follow: dict[str, dict[str, Any]] = {}
        self.profitable_closes: dict[str, dict[str, float]] = {}
        self.breadth_ignition_seen: set[str] = set()
        self.birth_fanout_seen: set[str] = set()
        self.birth_fanout_watch: dict[str, dict[str, Any]] = {}
        self.stealth_arm_seen: set[str] = set()
        self.spark3_arm_seen: set[str] = set()
        self.spark3_arms: dict[str, dict[str, Any]] = {}
        self.spark3_breakout_watch: dict[str, dict[str, Any]] = {}
        self.spark3_breakout_seen: set[str] = set()
        self.curve_lag_reveal_seen: set[str] = set()
        self.engagement_driven_seen: set[str] = set()
        # Phase 21 2026-05-08: bounce_buy lane state
        self.bounce_buy_seen: set[str] = set()
        self.bounce_buy_ts: dict[str, int] = {}  # for re-eligibility cooldown
        # Phase 2A 2026-05-08: adaptive guards (consecutive-loss circuit breaker).
        # State tracks: how many losses in a row, and when bot is paused-until.
        self.consecutive_losses: int = 0
        self.circuit_breaker_until_ts: int = 0
        # Phase 3 2026-05-08: anti-martingale stake scaling.
        # consecutive_wins ratchets stake UP after streaks; reset on any loss.
        # consecutive_losses (above) ratchets stake DOWN; reset on any win.
        self.consecutive_wins: int = 0
        self.preprice_reveal_seen: set[str] = set()
        self.priced_snap_seen: set[str] = set()
        self.priced_breakout_watch: dict[str, dict[str, Any]] = {}
        self.priced_breakout_seen: set[str] = set()
        self.late_swarm_seen: set[str] = set()
        self.curve_arm_scout_seen: set[str] = set()
        self.raw_momentum_seen: set[str] = set()
        self.raw_momentum_arms: dict[str, dict[str, Any]] = {}
        self.whale_spark_seen: set[str] = set()

    @staticmethod
    def moonshot_lane(lane: str) -> bool:
        return lane in {
            "second_wave_after_cluster",
            "reclaim_wave",
            "early_ignition",
            "late_ignition",
            "breadth_ignition",
            "birth_fanout",
            "stealth_arm",
            "spark3_arm",
            "spark3_breakout",
            "curve_lag_reveal",
            "preprice_reveal",
            "priced_snap",
            "priced_breakout",
            "late_swarm",
            "curve_arm_scout",
            "raw_momentum",
            "whale_spark",
        }

    def recent_profit_reentry_locked(self, mint: str, ts_ms: int) -> bool:
        prior = self.profitable_closes.get(mint)
        if not prior:
            return False
        block_ms = env_int(
            "PGG2_PROFIT_REENTRY_BLOCK_MS",
            env_int("PIGGY_PROFIT_REENTRY_BLOCK_MS", 900000),
        )
        if ts_ms - int(prior["ts_ms"]) > block_ms:
            return False
        if not env_bool(
            "PGG2_PROFIT_REENTRY_LOCK_AFTER_WIN",
            env_bool("PIGGY_PROFIT_REENTRY_LOCK_AFTER_WIN", True),
        ):
            return False
        return float(prior.get("pnl_sol") or 0.0) >= env_float(
            "PGG2_PROFIT_REENTRY_LOCK_MIN_PNL_SOL",
            0.001,
        )

    def profit_reentry_blocked(
        self,
        mint: str,
        ts_ms: int,
        base_move: float,
        age_ms: int,
        reclaim_strength: bool,
        late_reclaim_trigger: bool,
    ) -> bool:
        prior = self.profitable_closes.get(mint)
        if not prior:
            return False
        if self.recent_profit_reentry_locked(mint, ts_ms):
            return True
        if ts_ms - int(prior["ts_ms"]) > env_int("PIGGY_PROFIT_REENTRY_BLOCK_MS", 900000):
            return False
        overextended = base_move >= env_float("PIGGY_PROFIT_REENTRY_BLOCK_BASE_MOVE", 3.0)
        reclaim_like = reclaim_strength or late_reclaim_trigger or age_ms > env_int("PIGGY_SECOND_MAX_REGULAR_AGE_MS", 15000)
        return overextended and reclaim_like

    def probe_entry_sol(self) -> float:
        default_probe = self.config.scout_sol
        return min(
            self.config.max_position_sol,
            max(0.0005, env_float("PIGGY_PROBE_SOL", default_probe)),
        )

    @staticmethod
    def full_entry_reason(
        lane: str,
        features: dict[str, Any],
        fresh700: dict[str, Any],
        fresh1500: dict[str, Any],
    ) -> Optional[str]:
        buy700 = float(fresh700.get("fresh_buy_sol") or 0.0)
        buy1500 = float(fresh1500.get("fresh_buy_sol") or 0.0)
        top700 = float(fresh700.get("fresh_top_share") or 1.0)
        move700 = float(features.get("move700") or 1.0)
        age_ms = int(features.get("age_ms") or 0)
        base_move = float(features.get("wave_base_move") or features.get("base_move") or 1.0)
        if lane == "reclaim_wave":
            early_absorbed_reclaim = (
                age_ms <= env_int("PIGGY_FULL_RECLAIM_MAX_AGE_MS", 6000)
                and int(features.get("last_sell_age_ms") or 999999)
                <= env_int("PIGGY_FULL_RECLAIM_MAX_LAST_SELL_AGE_MS", 350)
                and buy700 >= env_float("PIGGY_FULL_MIN_BUY700_SOL", 5.0)
                and buy1500 >= env_float("PIGGY_FULL_MIN_BUY1500_SOL", 5.0)
                and top700 <= env_float("PIGGY_FULL_MAX_TOP700", 0.70)
                and float(features.get("move1500") or 1.0)
                >= env_float("PIGGY_FULL_RECLAIM_MIN_MOVE1500", 1.15)
            )
            if not early_absorbed_reclaim:
                if (
                    age_ms >= env_int("PIGGY_FULL_RECLAIM_PROBE_AFTER_AGE_MS", 15000)
                    or base_move >= env_float("PIGGY_FULL_RECLAIM_PROBE_BASE_MOVE", 2.0)
                ):
                    features["entry_size_reason"] = "probe_late_reclaim_confirm"
                else:
                    features["entry_size_reason"] = "probe_reclaim_confirm"
                return None
        if (
            buy700 >= env_float("PIGGY_FULL_MIN_BUY700_SOL", 5.0)
            and buy1500 >= env_float("PIGGY_FULL_MIN_BUY1500_SOL", 5.0)
            and top700 <= env_float("PIGGY_FULL_MAX_TOP700", 0.70)
        ):
            return "full_clean_flow"
        if (
            move700 >= env_float("PIGGY_FULL_VELOCITY_MOVE700", 1.25)
            and buy700 >= env_float("PIGGY_FULL_VELOCITY_BUY700_SOL", 4.0)
            and top700 <= env_float("PIGGY_FULL_VELOCITY_MAX_TOP700", 0.75)
        ):
            return "full_velocity_breakout"
        if (
            lane == "reclaim_wave"
            and buy700 >= env_float("PIGGY_FULL_RECLAIM_MIN_BUY700_SOL", 8.0)
            and top700 <= env_float("PIGGY_FULL_RECLAIM_MAX_TOP700", 0.45)
        ):
            return "full_reclaim_breadth"
        return None

    def init_position_follow(
        self,
        pos: Any,
        trusted: bool,
        entry_features: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        follow = {
            "opened_ts_ms": pos.opened_ts_ms,
            "trusted": trusted,
            "sig_buy_sol": 0.0,
            "sig_buy_count": 0,
            "sig_buyers": set(),
            "last_sig_buy_ms": 0,
            "entry_features": dict(entry_features) if entry_features else {},
        }
        self.position_follow[pos.mint] = follow
        return follow

    def follow_for_position(self, pos: Any) -> dict[str, Any]:
        follow = self.position_follow.get(pos.mint)
        if follow is None:
            follow = self.init_position_follow(pos, trusted=False)
        return follow

    async def on_event(self, event: PumpEvent) -> None:
        key = (event.sig, event.mint, event.instruction_kind, event.sol_lamports, event.token_amount)
        already_seen = key in self.seen_set
        await super().on_event(event)
        if already_seen or event.kind != "trade" or not event.is_buy:
            return
        pos = self.broker.positions.get(event.mint)
        if not pos or not self.moonshot_lane(pos.lane):
            return
        follow = self.position_follow.get(event.mint)
        if follow is None:
            follow = self.init_position_follow(pos, trusted=event.ts_ms <= pos.opened_ts_ms)
        grace_ms = env_int("PIGGY_FOLLOW_GRACE_MS", 1000)
        if (
            env_bool("PGG2_LAYERED_RISK_ENABLED", False)
            and pos.lane in {"priced_snap", "birth_fanout"}
        ):
            grace_ms = env_int("PGG2_LAYERED_FOLLOW_GRACE_MS", 0)
        if event.ts_ms < int(follow["opened_ts_ms"]) + grace_ms:
            return
        min_sig_sol = env_float("PIGGY_FOLLOW_SIG_BUY_SOL", 0.08)
        if (
            env_bool("PGG2_LAYERED_RISK_ENABLED", False)
            and pos.lane in {"priced_snap", "birth_fanout"}
        ):
            min_sig_sol = env_float("PGG2_LAYERED_FOLLOW_MIN_BUY_SOL", 0.08)
        if event.sol < min_sig_sol:
            return
        follow["sig_buy_sol"] = float(follow["sig_buy_sol"]) + event.sol
        follow["sig_buy_count"] = int(follow["sig_buy_count"]) + 1
        wallet = event.user or event.signer
        if wallet:
            follow["sig_buyers"].add(wallet)
        follow["last_sig_buy_ms"] = event.ts_ms

    def add_follow_features(self, pos: Any, features: dict[str, Any]) -> None:
        follow = self.follow_for_position(pos)
        last_sig_buy_ms = int(follow.get("last_sig_buy_ms") or 0)
        features["post_open_follow_trusted"] = bool(follow.get("trusted", False))
        features["post_open_sig_buy_sol"] = float(follow.get("sig_buy_sol") or 0.0)
        features["post_open_sig_buy_count"] = int(follow.get("sig_buy_count") or 0)
        features["post_open_sig_buyers"] = len(follow.get("sig_buyers") or set())
        features["last_post_open_sig_buy_age_ms"] = (
            int(features["ts_ms"]) - last_sig_buy_ms if last_sig_buy_ms else 999999
        )

    def same_slot_cluster(self, mint: str, ts_ms: int) -> dict[str, Any]:
        tape = self.tapes.get(mint)
        if not tape:
            return {"slot_buyers": 0, "slot_buy_sol": 0.0, "slot_top_share": 0.0}
        trades = [e for e in tape.events if e.kind == "trade" and e.is_buy and ts_ms - e.ts_ms <= 1400]
        if not trades:
            return {"slot_buyers": 0, "slot_buy_sol": 0.0, "slot_top_share": 0.0}
        first_slot = min(e.slot for e in trades if e.slot)
        slot_trades = [e for e in trades if e.slot in {first_slot, first_slot + 1}]
        by_wallet: dict[str, float] = {}
        for e in slot_trades:
            by_wallet[e.user or e.signer] = by_wallet.get(e.user or e.signer, 0.0) + e.sol
        total = sum(by_wallet.values())
        return {
            "slot_buyers": len(by_wallet),
            "slot_buy_sol": total,
            "slot_top_share": max(by_wallet.values()) / total if total > 0 else 0.0,
        }

    def event_window_stats(self, mint: str, start_ts_ms: int, end_ts_ms: int) -> dict[str, Any]:
        tape = self.tapes.get(mint)
        if not tape:
            return {"buy_sol": 0.0, "sell_sol": 0.0, "unique_buyers": 0, "top_buy_share": 0.0}
        buys: list[PumpEvent] = []
        sells: list[PumpEvent] = []
        for e in tape.events:
            if e.kind != "trade" or e.ts_ms < start_ts_ms or e.ts_ms > end_ts_ms:
                continue
            if e.is_buy:
                buys.append(e)
            else:
                sells.append(e)
        buy_sol = sum(e.sol for e in buys)
        sell_sol = sum(e.sol for e in sells)
        by_wallet: dict[str, float] = {}
        for e in buys:
            wallet = e.user or e.signer
            if wallet:
                by_wallet[wallet] = by_wallet.get(wallet, 0.0) + e.sol
        top_buy = max(by_wallet.values()) if by_wallet else 0.0
        return {
            "buy_sol": buy_sol,
            "sell_sol": sell_sol,
            "unique_buyers": len(by_wallet),
            "top_buy_share": top_buy / buy_sol if buy_sol > 0 else 0.0,
        }

    def birth_price_context(self, mint: str, ts_ms: int, price: float) -> Optional[dict[str, Any]]:
        tape = self.tapes.get(mint)
        if not tape or not tape.prices or price <= 0:
            return None
        first_price_ts, first_price = tape.prices[0]
        if first_price <= 0:
            return None
        anchor_ts = tape.first_create_ms or tape.first_seen_ms or first_price_ts
        first_price_delay_ms = max(0, first_price_ts - anchor_ts)
        first_price_age_ms = max(0, ts_ms - first_price_ts)
        birth1500 = self.event_window_stats(mint, anchor_ts, min(ts_ms, anchor_ts + 1500))
        pre_price = self.event_window_stats(mint, anchor_ts, first_price_ts)
        post1500 = self.event_window_stats(mint, first_price_ts, min(ts_ms, first_price_ts + 1500))
        return {
            "first_price_ts": first_price_ts,
            "first_price": first_price,
            "first_price_delay_ms": first_price_delay_ms,
            "first_price_age_ms": first_price_age_ms,
            "entry_move_from_first": price / max(first_price, 1e-18),
            "birth1500_buy_sol": birth1500["buy_sol"],
            "birth1500_sell_sol": birth1500["sell_sol"],
            "birth1500_unique_buyers": birth1500["unique_buyers"],
            "birth1500_top_share": birth1500["top_buy_share"],
            "pre_price_buy_sol": pre_price["buy_sol"],
            "pre_price_sell_sol": pre_price["sell_sol"],
            "pre_price_unique_buyers": pre_price["unique_buyers"],
            "pre_price_top_share": pre_price["top_buy_share"],
            "post1500_buy_sol": post1500["buy_sol"],
            "post1500_sell_sol": post1500["sell_sol"],
            "post1500_unique_buyers": post1500["unique_buyers"],
            "post1500_top_share": post1500["top_buy_share"],
        }

    def last_trade_ages(self, mint: str, ts_ms: int) -> dict[str, int]:
        tape = self.tapes.get(mint)
        last_buy = 999999
        last_sell = 999999
        if tape:
            for e in reversed(tape.events):
                if e.kind != "trade":
                    continue
                if e.is_buy and last_buy == 999999:
                    last_buy = ts_ms - e.ts_ms
                if (not e.is_buy) and last_sell == 999999:
                    last_sell = ts_ms - e.ts_ms
                if last_buy != 999999 and last_sell != 999999:
                    break
        return {"last_buy_age_ms": last_buy, "last_sell_age_ms": last_sell}

    def refresh_wave_arm(self, mint: str, features: dict[str, Any]) -> Optional[WaveArm]:
        arm = self.wave_arms.get(mint)
        if not arm:
            return None
        ts_ms = int(features["ts_ms"])
        price = float(features.get("price") or 0.0)
        if price > 0:
            if arm.first_price <= 0:
                arm.first_price = price
                arm.first_price_ts_ms = ts_ms
            arm.peak_price = max(arm.peak_price, price)
        tape = self.tapes.get(mint)
        if tape:
            initial_cutoff = arm.first_seen_ms + env_int("PIGGY_INITIAL_CLUSTER_MS", 1600)
            buy_by_wallet: dict[str, float] = {}
            sell_by_wallet: dict[str, float] = {}
            for e in tape.events:
                if e.kind != "trade" or e.ts_ms > initial_cutoff:
                    continue
                wallet = e.user or e.signer
                if not wallet:
                    continue
                if e.is_buy:
                    arm.initial_buyers.add(wallet)
                    buy_by_wallet[wallet] = buy_by_wallet.get(wallet, 0.0) + e.sol
                else:
                    arm.initial_sellers.add(wallet)
                    sell_by_wallet[wallet] = sell_by_wallet.get(wallet, 0.0) + e.sol
            if buy_by_wallet:
                arm.initial_buy_sol = sum(buy_by_wallet.values())
            if sell_by_wallet:
                arm.initial_sell_sol = sum(sell_by_wallet.values())
        arm.last_update_ms = ts_ms
        if ts_ms - arm.armed_ts_ms > env_int("PIGGY_WAVE_ARM_TTL_MS", 75000):
            self.wave_arms.pop(mint, None)
            return None
        return arm

    def maybe_arm_first_burst(self, event: PumpEvent, features: dict[str, Any]) -> None:
        if event.mint in self.wave_arms:
            self.refresh_wave_arm(event.mint, features)
            return
        if not event.is_buy or features["complete"]:
            return
        age_ms = int(features["age_ms"])
        buy_age_ms = int(features["buy_age_ms"])
        if age_ms > env_int("PIGGY_ARM_MAX_AGE_MS", 1650) or buy_age_ms > env_int("PIGGY_ARM_MAX_BUY_AGE_MS", 1450):
            return
        if not (self.config.first_buy_min_sol <= features["first_buy_sol"] <= self.config.first_buy_max_sol):
            return
        s700 = features["s700"]
        s1500 = features["s1500"]
        if (
            features["slot_buy_sol"] > env_float("PIGGY_ARM_MAX_SLOT_SOL", 40.0)
            or s1500.get("max_buy_sol", 0.0) > env_float("PIGGY_ARM_MAX_SINGLE_BUY_SOL", 20.0)
        ):
            return
        broad_cluster = (
            s700["unique_buyers"] >= env_int("PIGGY_ARM_MIN_BUYERS_700", 3)
            and s700["buy_sol"] >= env_float("PIGGY_ARM_MIN_BUY_SOL_700", 1.20)
            and s700["top_buy_share"] <= env_float("PIGGY_ARM_MAX_TOP_SHARE", 0.74)
        )
        same_slot = (
            features["slot_buyers"] >= env_int("PIGGY_ARM_MIN_SLOT_BUYERS", 3)
            and features["slot_buy_sol"] >= env_float("PIGGY_ARM_MIN_SLOT_SOL", 1.20)
            and features["slot_top_share"] <= env_float("PIGGY_ARM_MAX_SLOT_TOP_SHARE", 0.76)
        )
        if not (broad_cluster or same_slot):
            return
        if s1500["sell_sol"] > max(0.008, s1500["buy_sol"] * env_float("PIGGY_ARM_MAX_SELL_RATIO", 0.08)):
            return
        if features["off_peak"] < env_float("PIGGY_ARM_MIN_OFF_PEAK", 0.91):
            return
        tape = self.tapes.get(event.mint)
        first_seen = tape.first_seen_ms if tape and tape.first_seen_ms else event.ts_ms
        arm = WaveArm(
            mint=event.mint,
            armed_ts_ms=event.ts_ms,
            first_seen_ms=first_seen,
            initial_slot_buy_sol=float(features.get("slot_buy_sol") or 0.0),
            initial_slot_buyers=int(features.get("slot_buyers") or 0),
            initial_slot_top_share=float(features.get("slot_top_share") or 0.0),
            armed_without_curve=not bool(features.get("has_curve")) or float(features.get("price") or 0.0) <= 0,
        )
        self.wave_arms[event.mint] = arm
        self.refresh_wave_arm(event.mint, features)
        self.logger.decision(
            "wave_arm",
            event.mint,
            {
                "reason": (
                    f"arm cluster slot_buyers={features['slot_buyers']} slot_sol={features['slot_buy_sol']:.3f} "
                    f"u700={s700['unique_buyers']} b700={s700['buy_sol']:.3f}"
                ),
                "features": self.slim_features(features),
            },
        )

    def fresh_wave_stats(self, mint: str, ts_ms: int, arm: WaveArm, window_ms: int) -> dict[str, Any]:
        tape = self.tapes.get(mint)
        out = {
            "fresh_buy_sol": 0.0,
            "fresh_unique": 0,
            "fresh_top_share": 0.0,
            "fresh_max_buy": 0.0,
            "fresh_sells": 0,
            "fresh_sell_sol": 0.0,
            "old_buyer_buy_sol": 0.0,
        }
        if not tape:
            return out
        cutoff = ts_ms - window_ms
        fresh_by_wallet: dict[str, float] = {}
        for e in tape.events:
            if e.kind != "trade" or e.ts_ms < cutoff or e.ts_ms > ts_ms:
                continue
            wallet = e.user or e.signer
            if e.is_buy:
                if wallet and wallet not in arm.initial_buyers:
                    fresh_by_wallet[wallet] = fresh_by_wallet.get(wallet, 0.0) + e.sol
                else:
                    out["old_buyer_buy_sol"] += e.sol
            else:
                out["fresh_sells"] += 1
                out["fresh_sell_sol"] += e.sol
        if fresh_by_wallet:
            out["fresh_buy_sol"] = sum(fresh_by_wallet.values())
            out["fresh_unique"] = len(fresh_by_wallet)
            out["fresh_max_buy"] = max(fresh_by_wallet.values())
            out["fresh_top_share"] = out["fresh_max_buy"] / max(out["fresh_buy_sol"], 0.001)
        return out

    def prior_peak_price(self, mint: str, ts_ms: int, before_ms: int = 0) -> float:
        tape = self.tapes.get(mint)
        if not tape:
            return 0.0
        cutoff = ts_ms - max(0, before_ms)
        prior = [price for t, price in tape.prices if t < cutoff and price > 0]
        return max(prior) if prior else 0.0

    def second_wave_ready(self, event: PumpEvent, features: dict[str, Any]) -> tuple[Optional[StrikePlan], str]:
        if env_bool("PGG2_DISABLE_LEGACY_WAVE_LANES", False):
            return None, "legacy_wave_disabled"
        pre_wave_peak = self.prior_peak_price(
            event.mint,
            event.ts_ms,
            env_int("PIGGY_PRE_WAVE_PEAK_CUTOFF_MS", 900),
        ) or float(features.get("wave_prev_peak") or 0.0)
        arm = self.refresh_wave_arm(event.mint, features)
        if not arm:
            return None, "not_armed"
        if not event.is_buy:
            return None, "not_buy"
        if event.sol < env_float("PIGGY_SECOND_MIN_DUST_TRIGGER_SOL", 0.05):
            return None, "dust_trigger"
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None, "already_active"

        age_ms = int(features["age_ms"])
        arm_age_ms = int(features["ts_ms"] - arm.armed_ts_ms)
        if age_ms < env_int("PIGGY_SECOND_MIN_TOKEN_AGE_MS", 1800) or arm_age_ms < env_int("PIGGY_SECOND_MIN_ARM_AGE_MS", 1700):
            return None, "too_early"
        if age_ms > env_int("PIGGY_SECOND_MAX_TOKEN_AGE_MS", 75000):
            self.wave_arms.pop(event.mint, None)
            return None, "arm_expired"
        if not features["has_curve"] or arm.first_price <= 0 or features["price"] <= 0:
            return None, "no_curve"
        if features["complete"]:
            return None, "complete"
        if features["s8000"].get("max_buy_sol", 0.0) > env_float("PIGGY_SECOND_MAX_SINGLE_BUY_SOL", 20.0):
            return None, "absurd_trade_size"

        price = float(features["price"])
        base_move = price / max(arm.first_price, 1e-18)
        peak_hold = price / max(arm.peak_price or arm.first_price, 1e-18)
        pre_peak_breakout = price / max(pre_wave_peak or arm.first_price, 1e-18)
        fresh700 = self.fresh_wave_stats(event.mint, int(features["ts_ms"]), arm, env_int("PIGGY_SECOND_WINDOW_MS", 900))
        fresh1500 = self.fresh_wave_stats(event.mint, int(features["ts_ms"]), arm, 1500)
        reclaim_strength = (
            fresh1500["fresh_buy_sol"] >= env_float("PIGGY_SECOND_RECLAIM_MIN_SOL", 4.50)
            and fresh1500["fresh_unique"] >= env_int("PIGGY_SECOND_RECLAIM_MIN_BUYERS", 5)
            and features["move700"] >= env_float("PIGGY_SECOND_RECLAIM_MIN_MOVE700", 1.08)
            and base_move >= env_float("PIGGY_SECOND_RECLAIM_MIN_BASE_MOVE", 1.22)
        )
        if age_ms > env_int("PIGGY_SECOND_MAX_REGULAR_AGE_MS", 15000) and not reclaim_strength:
            return None, "too_late_regular_wave"
        if base_move < env_float("PIGGY_SECOND_MIN_BASE_MOVE", 1.25) and not reclaim_strength:
            return None, "base_move_low"
        if (
            pre_peak_breakout > env_float("PIGGY_SECOND_MAX_PRE_PEAK_BREAKOUT", 1.24)
            and age_ms < env_int("PIGGY_SECOND_PRE_PEAK_BREAKOUT_MAX_AGE_MS", 30000)
        ):
            return None, "vertical_price_reveal"
        if peak_hold < env_float("PIGGY_SECOND_MIN_PEAK_HOLD", 0.88) and not reclaim_strength:
            return None, "too_far_off_peak"
        if features["off_peak"] < env_float("PIGGY_SECOND_MIN_OFF_PEAK", 0.86) and not reclaim_strength:
            return None, "tape_off_peak"
        if arm.initial_sell_sol > max(0.020, arm.initial_buy_sol * env_float("PIGGY_SECOND_MAX_INITIAL_SELL_RATIO", 0.16)):
            return None, "initial_cluster_sold"

        s700 = features["s700"]
        s1500 = features["s1500"]
        fresh_buy = float(fresh700["fresh_buy_sol"])
        fresh_unique = int(fresh700["fresh_unique"])
        fresh_sells = float(fresh700["fresh_sell_sol"])
        late_reclaim_trigger = (
            age_ms >= env_int("PIGGY_LATE_RECLAIM_TRIGGER_MIN_AGE_MS", 30000)
            and fresh1500["fresh_buy_sol"] >= env_float("PIGGY_LATE_RECLAIM_TRIGGER_MIN_FRESH_SOL", 10.0)
            and event.sol >= env_float("PIGGY_LATE_RECLAIM_TRIGGER_MIN_BUY_SOL", 0.10)
        )
        if self.profit_reentry_blocked(event.mint, event.ts_ms, base_move, age_ms, reclaim_strength, late_reclaim_trigger):
            return None, "prior_profit_reentry_block"
        if (
            env_bool("PIGGY_REJECT_EARLY_VERTICAL_VACUUM", True)
            and age_ms <= env_int("PIGGY_EARLY_VERTICAL_MAX_AGE_MS", 5000)
            and features["move700"] >= env_float("PIGGY_EARLY_VERTICAL_MIN_MOVE700", 1.50)
        ):
            return None, "early_vertical_vacuum"
        if (
            env_bool("PIGGY_REJECT_WEAK_TOP_HEAVY_VACUUM", True)
            and fresh_buy < env_float("PIGGY_WEAK_TOP_HEAVY_MAX_BUY700_SOL", 1.50)
            and fresh700["fresh_top_share"] > env_float("PIGGY_WEAK_TOP_HEAVY_MIN_TOP700", 0.75)
            and features["move700"] < env_float("PIGGY_WEAK_TOP_HEAVY_MAX_MOVE700", 1.08)
        ):
            return None, "weak_top_heavy_vacuum"
        if (
            env_bool("PIGGY_REJECT_OVEREXTENDED_CLEAN_RECLAIM", True)
            and reclaim_strength
            and base_move > env_float("PIGGY_OVEREXTENDED_RECLAIM_BASE_MOVE", 3.0)
            and fresh700["fresh_top_share"] <= env_float("PIGGY_OVEREXTENDED_RECLAIM_MAX_TOP700", 0.70)
        ):
            return None, "overextended_clean_reclaim_vacuum"
        if (
            env_bool("PIGGY_REJECT_EXHAUSTED_RECLAIM", True)
            and late_reclaim_trigger
            and fresh_buy >= env_float("PIGGY_EXHAUSTED_RECLAIM_MIN_FRESH_SOL", 12.0)
            and base_move <= env_float("PIGGY_EXHAUSTED_RECLAIM_MAX_BASE_MOVE", 1.42)
            and features["move700"] >= env_float("PIGGY_EXHAUSTED_RECLAIM_MIN_MOVE700", 1.55)
        ):
            return None, "exhausted_reclaim_no_headroom"
        if (
            env_bool("PGG2_REJECT_MIDAGE_VERTICAL_RECLAIM", True)
            and reclaim_strength
            and age_ms >= env_int("PGG2_MIDAGE_VERTICAL_RECLAIM_MIN_AGE_MS", 12000)
            and age_ms <= env_int("PGG2_MIDAGE_VERTICAL_RECLAIM_MAX_AGE_MS", 30000)
            and base_move >= env_float("PGG2_MIDAGE_VERTICAL_RECLAIM_MIN_BASE_MOVE", 1.75)
            and features["move700"] >= env_float("PGG2_MIDAGE_VERTICAL_RECLAIM_MIN_MOVE700", 1.75)
        ):
            return None, "midage_vertical_reclaim_no_headroom"
        if (
            not reclaim_strength
            and features["move700"] < env_float("PIGGY_SECOND_MIN_NON_RECLAIM_MOVE700", 1.0)
        ):
            return None, "non_reclaim_not_marking_up"
        if event.sol < env_float("PIGGY_SECOND_MIN_TRIGGER_BUY_SOL", 0.50) and not late_reclaim_trigger:
            return None, "weak_trigger_buy"
        sells_ok = fresh_sells <= max(0.015, fresh_buy * env_float("PIGGY_SECOND_MAX_FRESH_SELL_RATIO", 0.15))
        breadth_ok = (
            fresh_unique >= env_int("PIGGY_SECOND_MIN_FRESH_BUYERS", 2)
            and fresh_buy >= env_float("PIGGY_SECOND_MIN_FRESH_SOL", 1.15)
            and fresh700["fresh_top_share"] <= env_float("PIGGY_SECOND_MAX_FRESH_TOP_SHARE", 0.84)
        )
        force_buy_ok = (
            fresh_unique >= 1
            and fresh_buy >= env_float("PIGGY_SECOND_MIN_FORCE_SOL", 1.35)
            and event.sol >= env_float("PIGGY_SECOND_MIN_LAST_BUY_SOL", 0.25)
            and features["move700"] >= env_float("PIGGY_SECOND_FORCE_MOVE700", 1.045)
        )
        curve_accel = (
            features["move700"] >= env_float("PIGGY_SECOND_MIN_MOVE700", 1.025)
            or features["move1500"] >= env_float("PIGGY_SECOND_MIN_MOVE1500", 1.055)
            or price >= max(arm.peak_price, arm.first_price) * env_float("PIGGY_SECOND_BREAKOUT_OF_ARM_PEAK", 1.005)
        )
        tape_ok = (
            s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) <= env_float("PIGGY_SECOND_MAX_TAPE_SELL_RATIO", 0.22)
            and s700["top_buy_share"] <= env_float("PIGGY_SECOND_MAX_TOP_SHARE_700", 0.88)
            and features["last_buy_age_ms"] <= env_int("PIGGY_SECOND_MAX_LAST_BUY_AGE_MS", 220)
        )
        if not sells_ok:
            return None, "fresh_sells"
        if fresh700["old_buyer_buy_sol"] > max(
            env_float("PIGGY_SECOND_MAX_OLD_BUYER_SOL", 0.60),
            fresh_buy * env_float("PIGGY_SECOND_MAX_OLD_BUYER_MULT", 1.0),
        ) and not reclaim_strength:
            return None, "old_cluster_dominates"
        if (
            fresh700["fresh_top_share"] > env_float("PIGGY_SECOND_SOFT_TOP_SHARE", 0.75)
            and base_move < env_float("PIGGY_SECOND_TOP_HEAVY_MIN_BASE_MOVE", 1.34)
            and not reclaim_strength
        ):
            return None, "top_heavy_without_markup"
        if (
            base_move >= env_float("PIGGY_SECOND_OVERHEATED_BASE_MOVE", 1.75)
            and fresh_buy < env_float("PIGGY_SECOND_OVERHEATED_MIN_FRESH_SOL", 2.50)
            and not late_reclaim_trigger
        ):
            return None, "overheated_thin_wave"
        if (
            base_move >= env_float("PIGGY_SECOND_OVERHEATED_BASE_MOVE", 1.75)
            and age_ms < env_int("PIGGY_SECOND_OVERHEATED_MIN_AGE_MS", 8000)
            and not late_reclaim_trigger
        ):
            return None, "overheated_too_early"
        if not ((breadth_ok or force_buy_ok or reclaim_strength) and curve_accel and tape_ok):
            return None, "second_wave_not_ready"
        if (
            env_bool("PGG2_REJECT_LATE_WHALE_DRAG", True)
            and not reclaim_strength
            and float(features.get("first_buy_sol") or 0.0) >= env_float("PGG2_LATE_WHALE_MIN_FIRST_BUY_SOL", 2.0)
            and base_move >= env_float("PGG2_LATE_WHALE_MIN_BASE_MOVE", 1.50)
            and (
                fresh700["fresh_top_share"] >= env_float("PGG2_LATE_WHALE_MIN_TOP700", 0.60)
                or fresh_unique <= env_int("PGG2_LATE_WHALE_MAX_UNIQ700", 3)
                or features["slot_top_share"] >= env_float("PGG2_LATE_WHALE_MIN_SLOT_TOP", 0.75)
            )
        ):
            return None, "late_whale_drag"

        score = 80.0
        score += min(32.0, fresh_buy * 10.0)
        score += min(20.0, fresh_unique * 6.0)
        score += max(0.0, base_move - 1.0) * 85.0
        score += max(0.0, features["move700"] - 1.0) * 280.0
        score -= max(0.0, fresh700["fresh_top_share"] - 0.70) * 45.0
        score -= max(0.0, s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) - 0.08) * 70.0
        lane = "reclaim_wave" if reclaim_strength else "second_wave_after_cluster"
        full_reason = self.full_entry_reason(lane, features, fresh700, fresh1500)
        if (
            not reclaim_strength
            and not full_reason
            and score < env_float("PIGGY_SECOND_MIN_NON_RECLAIM_PROBE_SCORE", 180.0)
        ):
            return None, "non_reclaim_probe_score_low"
        full_entry_sol = min(
            self.config.max_position_sol,
            max(
                self.config.scout_sol,
                env_float("PIGGY_SECOND_ENTRY_SOL", self.config.max_position_sol),
            ),
        )
        size_reason = str(features.get("entry_size_reason") or full_reason or "probe_then_confirm")
        scout = full_entry_sol if full_reason else self.probe_entry_sol()
        if (
            env_bool("PGG2_SECOND_WAVE_FORCE_SCOUT", True)
            and lane == "second_wave_after_cluster"
            and full_reason
        ):
            # PGG2 risk fix: one full-size second-wave entry erased the live
            # session. Keep this lane scout-first; scale logic can still add
            # size after confirmation, but an 80ms rug cannot cost 0.20 SOL.
            scout = min(
                full_entry_sol,
                max(0.0005, env_float("PGG2_SECOND_WAVE_MAX_SCOUT_SOL", self.config.scout_sol)),
            )
            size_reason = f"{full_reason}_scout_capped"
        if size_reason == "probe_late_reclaim_confirm":
            scout = min(scout, env_float("PIGGY_LATE_RECLAIM_PROBE_SOL", max(0.0005, self.config.scout_sol * 0.50)))
        features["entry_size_reason"] = size_reason
        features["entry_probe_sol"] = scout
        quality = max(0.0, min(1.0, (score - 90.0) / 55.0))
        target = scout + (self.config.max_position_sol - scout) * max(0.45, quality)
        if base_move >= 1.30 and fresh1500["fresh_buy_sol"] >= 1.25:
            target = max(target, self.config.max_position_sol * 0.78)
        target = min(self.config.max_position_sol, max(scout, target, self.config.max_position_sol))
        reason = (
            f"second_wave fresh={fresh_buy:.3f}/{fresh_unique} base={base_move:.2f}x "
            f"m700={features['move700']:.3f} peak_hold={peak_hold:.2f} "
            f"sell1500={s1500['sell_sol'] / max(s1500['buy_sol'], 0.001):.2f} "
            f"lane={lane} size={size_reason}"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane=lane,
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update(
            {
                "arm_first_price": arm.first_price,
                "arm_peak_price": arm.peak_price,
                "base_move": base_move,
                "pre_peak_breakout": pre_peak_breakout,
                "fresh700": fresh700,
                "fresh1500": fresh1500,
                "entry_size_reason": size_reason,
                "entry_probe_sol": scout,
            }
        )
        return plan, "ready"

    def feature_snapshot(self, mint: str, ts_ms: int) -> Optional[dict[str, Any]]:
        features = super().feature_snapshot(mint, ts_ms)
        if not features:
            return None
        features.update(self.same_slot_cluster(mint, ts_ms))
        features.update(self.last_trade_ages(mint, ts_ms))
        s700 = features["s700"]
        s1500 = features["s1500"]
        for label, stats in (
            ("250", features["s250"]),
            ("700", s700),
            ("1500", s1500),
            ("3000", features["s3000"]),
            ("8000", features["s8000"]),
        ):
            features[f"buy{label}"] = float(stats.get("buy_sol") or 0.0)
            features[f"sell{label}"] = float(stats.get("sell_sol") or 0.0)
            features[f"uniq{label}"] = int(stats.get("unique_buyers") or 0)
            features[f"top_share{label}"] = float(stats.get("top_buy_share") or 1.0)
        features["cluster_score"] = self.cluster_score(features)
        features["cluster_width_ok"] = (
            s700["unique_buyers"] >= 3
            and s700["buy_sol"] >= self.config.two_wallet_buy_sol
            and s700["top_buy_share"] <= 0.72
        )
        features["flow_live"] = features["last_buy_age_ms"] <= 450 and s700["sell_sol"] <= max(0.005, s700["buy_sol"] * 0.05)
        features["buy_stall"] = features["last_buy_age_ms"] >= 650 and s1500["buy_sol"] < max(1.0, features["slot_buy_sol"] * 0.35)
        arm_before = self.wave_arms.get(mint)
        features["wave_prev_peak"] = arm_before.peak_price if arm_before else 0.0
        arm = self.refresh_wave_arm(mint, features)
        if arm:
            features["wave_armed"] = True
            features["wave_arm_age_ms"] = ts_ms - arm.armed_ts_ms
            features["wave_base_move"] = (features["price"] / max(arm.first_price, 1e-18)) if features["price"] > 0 and arm.first_price > 0 else 1.0
        else:
            features["wave_armed"] = False
            features["wave_arm_age_ms"] = 0
            features["wave_base_move"] = 1.0
        return features

    async def on_event(self, event: PumpEvent) -> None:
        if event.kind == "trade" and event.price_hint > 0 and env_bool("PGG2_USE_PRICE_HINTS", False):
            # Off by default. buy_exact_sol_in carries a min-out style token
            # field on many pump.fun transactions, so treating it as executed
            # token amount creates fake trillion-x price moves. Only enable this
            # manually for controlled parser tests.
            tape = self.tape_for(event.mint)
            tape.add_price(event.ts_ms, event.price_hint, self.config.max_tape_age_sec)
        await super().on_event(event)

    @staticmethod
    def cluster_score(features: dict[str, Any]) -> float:
        s250 = features["s250"]
        s700 = features["s700"]
        s1500 = features["s1500"]
        sell_ratio1500 = s1500["sell_sol"] / max(s1500["buy_sol"], 0.001)
        score = 0.0
        score += min(35.0, s700["buy_sol"] * 10.0)
        score += min(20.0, s1500["buy_sol"] * 4.0)
        score += min(28.0, s700["unique_buyers"] * 7.0)
        score += min(16.0, features.get("slot_buyers", 0) * 4.0)
        score += max(0.0, features["move700"] - 1.0) * 700.0
        score += max(0.0, features["move1500"] - 1.0) * 260.0
        if s250["sells"] == 0 and s700["sells"] == 0:
            score += 8.0
        if 0.30 <= features["first_buy_sol"] <= 4.50:
            score += 8.0
        score -= max(0.0, s700["top_buy_share"] - 0.72) * 90.0
        score -= max(0.0, s700["buyer_hhi"] - 0.42) * 45.0
        score -= min(55.0, sell_ratio1500 * 120.0)
        score -= max(0.0, 1.0 - features["move250"]) * 220.0
        if features["is_mayhem"]:
            score -= 4.0
        return score

    def build_strike_plan(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        self.maybe_arm_first_burst(event, features)
        plan = self.spark3_arm_ready(event, features)
        if plan:
            return plan
        plan = self.spark3_breakout_ready(event, features)
        if plan:
            return plan
        plan = self.preprice_reveal_ready(event, features)
        if plan:
            return plan
        plan = self.priced_snap_ready(event, features)
        if plan:
            return plan
        plan = self.priced_breakout_ready(event, features)
        if plan:
            return plan
        plan = self.birth_fanout_ready(event, features)
        if plan:
            return plan
        plan = self.stealth_arm_ready(event, features)
        if plan:
            return plan
        plan = self.curve_lag_reveal_ready(event, features)
        if plan:
            return plan
        plan = self.early_ignition_ready(event, features)
        if plan:
            return plan
        plan, _ = self.second_wave_ready(event, features)
        return plan

    def birth_fanout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 birth_fanout lane.

        Birth-ledger lane: watch from the first real curve price, require broad
        launch buying, then wait a tiny confirmation window for fresh follow-on
        buy flow. This targets the actual blind spot without chasing late 2x
        breakouts or using fake transaction price hints.
        """
        if not env_bool("PGG2_BIRTH_FANOUT_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.birth_fanout_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if features.get("complete"):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        ctx = self.birth_price_context(event.mint, event.ts_ms, price)
        if not ctx:
            return None
        watch = self.birth_fanout_watch.get(event.mint)
        confirm_ms = env_int("PGG2_BIRTH_FANOUT_CONFIRM_MS", 250)
        max_confirm_ms = env_int("PGG2_BIRTH_FANOUT_CONFIRM_MAX_MS", 850)
        if watch:
            watch_age_ms = event.ts_ms - int(watch.get("ts_ms") or 0)
            if watch_age_ms > max_confirm_ms:
                self.birth_fanout_seen.add(event.mint)
                self.birth_fanout_watch.pop(event.mint, None)
                self.logger.decision(
                    "birth_fanout_reject",
                    event.mint,
                    {
                        "lane": "birth_fanout",
                        "reason": f"confirm_timeout age={watch_age_ms}ms",
                        "features": self.slim_features(features),
                    },
                )
                return None
            if watch_age_ms < confirm_ms:
                return None
            if ctx["first_price_age_ms"] > env_int("PGG2_BIRTH_FANOUT_CONFIRM_MAX_FIRST_PRICE_AGE_MS", 1500):
                self.birth_fanout_seen.add(event.mint)
                self.birth_fanout_watch.pop(event.mint, None)
                self.logger.decision(
                    "birth_fanout_reject",
                    event.mint,
                    {
                        "lane": "birth_fanout",
                        "reason": f"confirm_stale_first_price age={ctx['first_price_age_ms']}ms",
                        "features": self.slim_features(features),
                    },
                )
                return None
            confirm = self.event_window_stats(event.mint, int(watch["ts_ms"]), event.ts_ms)
            confirm_buy = float(confirm.get("buy_sol") or 0.0)
            confirm_sell = float(confirm.get("sell_sol") or 0.0)
            confirm_unique = int(confirm.get("unique_buyers") or 0)
            confirm_top = float(confirm.get("top_buy_share") or 0.0)
            if (
                confirm_buy < env_float("PGG2_BIRTH_FANOUT_CONFIRM_MIN_BUY_SOL", 0.50)
                or confirm_unique < env_int("PGG2_BIRTH_FANOUT_CONFIRM_MIN_BUYERS", 1)
                or confirm_top > env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_TOP_SHARE", 1.0)
                or confirm_sell > max(
                    env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_SELL_SOL", 0.075),
                    confirm_buy * env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_SELL_RATIO", 0.08),
                )
            ):
                self.birth_fanout_seen.add(event.mint)
                self.birth_fanout_watch.pop(event.mint, None)
                self.logger.decision(
                    "birth_fanout_reject",
                    event.mint,
                    {
                        "lane": "birth_fanout",
                        "reason": (
                            f"confirm_failed age={watch_age_ms}ms "
                            f"b={confirm_buy:.3f}/{confirm_unique} s={confirm_sell:.3f}"
                        ),
                        "features": self.slim_features(features),
                    },
                )
                return None
            if ctx["entry_move_from_first"] > env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_ENTRY_MOVE", 1.70):
                self.birth_fanout_seen.add(event.mint)
                self.birth_fanout_watch.pop(event.mint, None)
                self.logger.decision(
                    "birth_fanout_reject",
                    event.mint,
                    {
                        "lane": "birth_fanout",
                        "reason": f"confirm_overextended move={ctx['entry_move_from_first']:.3f}x",
                        "features": self.slim_features(features),
                    },
                )
                return None
            watch_ctx = dict(watch.get("ctx") or ctx)
            ctx["confirm_buy_sol"] = confirm_buy
            ctx["confirm_sell_sol"] = confirm_sell
            ctx["confirm_unique_buyers"] = confirm_unique
            ctx["confirm_top_share"] = confirm_top
            ctx["confirm_ms"] = watch_age_ms
        else:
            if ctx["first_price_delay_ms"] > env_int("PGG2_BIRTH_FANOUT_MAX_FIRST_PRICE_DELAY_MS", 5000):
                return None
            if ctx["first_price_age_ms"] > env_int("PGG2_BIRTH_FANOUT_MAX_FIRST_PRICE_AGE_MS", 2000):
                return None
            if ctx["entry_move_from_first"] < env_float("PGG2_BIRTH_FANOUT_MIN_ENTRY_MOVE", 0.45):
                return None
            if ctx["entry_move_from_first"] > env_float("PGG2_BIRTH_FANOUT_MAX_ENTRY_MOVE", 1.50):
                return None
            wave_base_move = float(features.get("wave_base_move") or 1.0)
            if wave_base_move < env_float("PGG2_BIRTH_FANOUT_MIN_WAVE_BASE_MOVE", 0.45):
                return None
            birth_buy = float(ctx.get("birth1500_buy_sol") or ctx["post1500_buy_sol"])
            birth_unique = int(ctx.get("birth1500_unique_buyers") or ctx["post1500_unique_buyers"])
            birth_top = float(ctx.get("birth1500_top_share") or ctx["post1500_top_share"])
            birth_sell = float(ctx.get("birth1500_sell_sol") or ctx["post1500_sell_sol"])
            if birth_buy < env_float("PGG2_BIRTH_FANOUT_MIN_BUY_SOL", 9.0):
                return None
            if birth_unique < env_int("PGG2_BIRTH_FANOUT_MIN_BUYERS", 11):
                return None
            if birth_top > env_float("PGG2_BIRTH_FANOUT_MAX_TOP_SHARE", 0.32):
                return None
            if birth_sell > max(
                0.010,
                birth_buy * env_float("PGG2_BIRTH_FANOUT_MAX_SELL_RATIO", 0.08),
            ):
                return None
            elite_nofollow = (
                env_bool("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_ENABLED", False)
                and birth_buy >= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_BUY_SOL", 11.0)
                and birth_unique >= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_BUYERS", 11)
                and birth_top <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_TOP_SHARE", 0.30)
                and ctx["first_price_age_ms"] <= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_FIRST_PRICE_AGE_MS", 1300)
                and ctx["entry_move_from_first"] <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_ENTRY_MOVE", 1.12)
                and ctx.get("pre_price_buy_sol", 0.0) >= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_PRE_PRICE_BUY_SOL", 8.0)
                and ctx.get("pre_price_unique_buyers", 0) >= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_PRE_PRICE_BUYERS", 4)
                and ctx.get("pre_price_top_share", 1.0) <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_PRE_PRICE_TOP", 0.36)
            )
            if elite_nofollow:
                ctx["confirm_buy_sol"] = 0.0
                ctx["confirm_sell_sol"] = 0.0
                ctx["confirm_unique_buyers"] = 0
                ctx["confirm_top_share"] = 0.0
                ctx["confirm_ms"] = 0
                ctx["birth_entry_profile"] = "elite_nofollow"
                watch_ctx = dict(ctx)
            else:
                self.birth_fanout_watch[event.mint] = {
                    "ts_ms": event.ts_ms,
                    "price": price,
                    "ctx": dict(ctx),
                    "features": self.slim_features(features),
                }
                self.logger.decision(
                    "birth_fanout_watch",
                    event.mint,
                    {
                        "lane": "birth_fanout",
                        "reason": (
                            f"watch birth1500={birth_buy:.3f}/{birth_unique} "
                            f"top={birth_top:.2f} pre={ctx.get('pre_price_buy_sol', 0.0):.3f} "
                            f"move={ctx['entry_move_from_first']:.2f}x"
                        ),
                        "features": self.slim_features(features),
                    },
                )
                return None

        if ctx.get("birth_entry_profile") == "elite_nofollow":
            scout = env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_SOL", 0.020)
        else:
            scout = env_float("PGG2_BIRTH_FANOUT_SOL", max(0.0005, self.config.scout_sol * 0.50))
            full_follow_ok = (
                float(ctx.get("pre_price_buy_sol") or 0.0)
                >= env_float("PGG2_BIRTH_FANOUT_FOLLOW_FULL_MIN_PRE_PRICE_BUY_SOL", 7.5)
            )
            if not full_follow_ok:
                scout = min(scout, env_float("PGG2_BIRTH_FANOUT_FOLLOW_WEAK_SOL", 0.020))
        full_scout = min(self.config.max_position_sol, max(0.0005, scout))
        scout = full_scout
        target = full_scout
        if env_bool("PGG2_LAYERED_RISK_ENABLED", False):
            scout = max(0.0005, full_scout * env_float("PGG2_LAYERED_ENTRY_FRACTION", 0.80))
        score = (
            120.0
            + min(60.0, float(ctx.get("birth1500_buy_sol") or ctx["post1500_buy_sol"]) * 5.0)
            + min(45.0, int(ctx.get("birth1500_unique_buyers") or ctx["post1500_unique_buyers"]) * 4.0)
            + min(35.0, ctx.get("confirm_buy_sol", 0.0) * 8.0)
            + max(0.0, ctx["entry_move_from_first"] - 1.0) * 80.0
            - max(0.0, float(ctx.get("birth1500_top_share") or ctx["post1500_top_share"]) - 0.35) * 55.0
        )
        reason = (
            f"birth_fanout confirm={ctx.get('confirm_ms', 0)}ms first_age={ctx['first_price_age_ms']}ms "
            f"birth1500={float(ctx.get('birth1500_buy_sol') or ctx['post1500_buy_sol']):.3f}/"
            f"{int(ctx.get('birth1500_unique_buyers') or ctx['post1500_unique_buyers'])} "
            f"top={float(ctx.get('birth1500_top_share') or ctx['post1500_top_share']):.2f} "
            f"pre={ctx.get('pre_price_buy_sol', 0.0):.3f} "
            f"profile={ctx.get('birth_entry_profile', 'follow_confirm')} "
            f"follow={ctx.get('confirm_buy_sol', 0.0):.3f}/{ctx.get('confirm_unique_buyers', 0)} "
            f"move={ctx['entry_move_from_first']:.2f}x"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="birth_fanout",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update(
            {
                "birth_fanout": ctx,
                "birth_fanout_watch": watch_ctx,
                "entry_size_reason": "birth_fanout_probe",
                "entry_probe_sol": scout,
            }
        )
        self.birth_fanout_watch.pop(event.mint, None)
        return plan

    def stealth_arm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Aggressive PGG2 stealth-arm lane.

        Fast quote-log validation found a blind spot at the wave-arm layer:
        fresh 3-buyer arms with 5-8 SOL bought, no sells, and a temporarily
        unpopulated vSOL field produced many 2x+ moves, including the current
        BaZp 8x miss. It is noisy, so this lane is tiny-size and live-quote
        loss-clamped; it must not pollute the cleaner birth/reclaim lanes.
        """
        if not env_bool("PGG2_STEALTH_ARM_ENABLED", False):
            return None
        if not event.is_buy or event.mint in self.stealth_arm_seen:
            return None
        if not features.get("wave_armed") or features.get("complete"):
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        s700 = features["s700"]
        buy700 = float(s700.get("buy_sol") or 0.0)
        uniq700 = int(s700.get("unique_buyers") or 0)
        top700 = float(s700.get("top_buy_share") or 1.0)
        sell700 = float(s700.get("sell_sol") or 0.0)
        sell1500 = float(features["s1500"].get("sell_sol") or 0.0)
        move700 = float(features.get("move700") or 1.0)
        wave_base = float(features.get("wave_base_move") or 1.0)
        vsol = float(features.get("vsol_sol") or 0.0)
        if buy700 < env_float("PGG2_STEALTH_ARM_MIN_BUY700", 5.0):
            return None
        if buy700 > env_float("PGG2_STEALTH_ARM_MAX_BUY700", 8.0):
            return None
        if uniq700 != env_int("PGG2_STEALTH_ARM_UNIQ700", 3):
            return None
        if top700 < env_float("PGG2_STEALTH_ARM_MIN_TOP700", 0.45):
            return None
        if top700 > env_float("PGG2_STEALTH_ARM_MAX_TOP700", 0.56):
            return None
        if sell700 + sell1500 > env_float("PGG2_STEALTH_ARM_MAX_SELL_SOL", 0.001):
            return None
        if move700 < env_float("PGG2_STEALTH_ARM_MIN_MOVE700", 0.98):
            return None
        if move700 > env_float("PGG2_STEALTH_ARM_MAX_MOVE700", 1.08):
            return None
        if wave_base > env_float("PGG2_STEALTH_ARM_MAX_BASE_MOVE", 1.05):
            return None
        if vsol > env_float("PGG2_STEALTH_ARM_MAX_VSOL", 1.0):
            return None

        scout = min(
            self.config.max_position_sol,
            max(0.0005, env_float("PGG2_STEALTH_ARM_SOL", 0.010)),
        )
        score = 95.0 + min(40.0, buy700 * 4.0) + max(0.0, move700 - 1.0) * 180.0
        reason = (
            f"stealth_arm b700={buy700:.3f}/{uniq700} top={top700:.2f} "
            f"m700={move700:.3f} base={wave_base:.2f} vsol={vsol:.2f}"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="stealth_arm",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update({"entry_size_reason": "stealth_arm_probe", "entry_probe_sol": scout})
        return plan

    def spark3_arm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 spark3 arm lane.

        Fast event-log validation found a live blind spot in unstruck wave arms:
        3 unique buyers, 4.5-7.5 SOL bought, zero sells, fresh base, and a
        controlled/vertical move already underway. It caught current-run misses
        that reached 3.34x and 1.58x while staying positive across stored quote
        tapes under a harsh tiny-loss proxy. This lane is intentionally tiny and
        separately quote-clamped so it cannot displace the proven lanes.
        """
        spark3_enabled = env_bool("PGG2_SPARK3_ARM_ENABLED", False)
        spark3_shadow = env_bool("PGG2_SPARK3_ARM_SHADOW_ONLY", False)
        if not (spark3_enabled or spark3_shadow):
            return None
        if not event.is_buy or event.mint in self.spark3_arm_seen:
            return None
        if not features.get("wave_armed") or features.get("complete"):
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        s700 = features["s700"]
        buy700 = float(s700.get("buy_sol") or 0.0)
        uniq700 = int(s700.get("unique_buyers") or 0)
        top700 = float(s700.get("top_buy_share") or 1.0)
        sell700 = float(s700.get("sell_sol") or 0.0)
        sell1500 = float(features["s1500"].get("sell_sol") or 0.0)
        move700 = float(features.get("move700") or 1.0)
        wave_base = float(features.get("wave_base_move") or 1.0)
        if uniq700 != env_int("PGG2_SPARK3_ARM_UNIQ700", 3):
            return None
        if buy700 < env_float("PGG2_SPARK3_ARM_MIN_BUY700", 4.5):
            return None
        if buy700 > env_float("PGG2_SPARK3_ARM_MAX_BUY700", 7.5):
            return None
        if top700 < env_float("PGG2_SPARK3_ARM_MIN_TOP700", 0.30):
            return None
        if top700 > env_float("PGG2_SPARK3_ARM_MAX_TOP700", 0.66):
            return None
        if sell700 + sell1500 > env_float("PGG2_SPARK3_ARM_MAX_SELL_SOL", 0.001):
            return None
        if wave_base > env_float("PGG2_SPARK3_ARM_MAX_BASE_MOVE", 1.05):
            return None
        if move700 < env_float("PGG2_SPARK3_ARM_MIN_MOVE700", 1.15):
            return None
        if move700 > env_float("PGG2_SPARK3_ARM_MAX_MOVE700", 999999999999.0):
            return None

        scout = min(
            self.config.max_position_sol,
            max(0.0005, env_float("PGG2_SPARK3_ARM_SOL", 0.010)),
        )
        score = 105.0 + min(35.0, buy700 * 4.0) + min(220.0, max(0.0, move700 - 1.0) * 180.0)
        reason = (
            f"spark3_arm b700={buy700:.3f}/{uniq700} top={top700:.2f} "
            f"m700={move700:.3f} base={wave_base:.2f}"
        )
        if spark3_shadow:
            self.spark3_arm_seen.add(event.mint)
            self.spark3_arms[event.mint] = {
                "ts_ms": event.ts_ms,
                "base_price": price,
                "min_price": price,
                "max_price": price,
                "reason": reason,
                "features": self.slim_features(features),
            }
            self.logger.decision(
                "spark3_candidate",
                event.mint,
                {
                    "lane": "spark3_arm",
                    "reason": reason,
                    "score": score,
                    "features": self.slim_features(features),
                },
            )
            log(f"PGG2-SPARK3-CANDIDATE {short_addr(event.mint)} {reason} score={score:.1f}")
            return None
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="spark3_arm",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update({"entry_size_reason": "spark3_arm_probe", "entry_probe_sol": scout})
        return plan

    def spark3_breakout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        if not env_bool("PGG2_SPARK3_BREAKOUT_ENABLED", False):
            return None
        if not event.is_buy or event.mint in self.spark3_breakout_seen:
            return None
        arm = self.spark3_arms.get(event.mint)
        if not arm:
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        arm_age_ms = event.ts_ms - int(arm.get("ts_ms") or 0)
        if arm_age_ms < 0 or arm_age_ms > env_int("PGG2_SPARK3_BREAKOUT_MAX_DELAY_MS", 30000):
            return None
        price = float(features.get("price") or 0.0)
        base_price = float(arm.get("base_price") or 0.0)
        if price <= 0 or base_price <= 0:
            return None
        arm["min_price"] = min(float(arm.get("min_price") or base_price), price)
        arm["max_price"] = max(float(arm.get("max_price") or base_price), price)
        pre_confirm_hold = float(arm.get("min_price") or base_price) / base_price
        if pre_confirm_hold < env_float("PGG2_SPARK3_BREAKOUT_MIN_PRE_CONFIRM_HOLD", 0.92):
            self.spark3_breakout_seen.add(event.mint)
            self.logger.decision(
                "spark3_breakout_reject",
                event.mint,
                {
                    "lane": "spark3_breakout",
                    "reason": f"pre_confirm_dump hold={pre_confirm_hold:.3f}",
                    "features": self.slim_features(features),
                },
            )
            return None
        breakout_mult = price / base_price
        if price > env_float("PGG2_SPARK3_BREAKOUT_MAX_ABS_PRICE", 0.010):
            return None
        if breakout_mult < env_float("PGG2_SPARK3_BREAKOUT_MIN_MULT", 1.32):
            return None
        confirm_ms = env_int("PGG2_SPARK3_BREAKOUT_CONFIRM_MS", 1000)
        hold_ratio = env_float("PGG2_SPARK3_BREAKOUT_CONFIRM_MIN_HOLD", 0.92)
        watch = self.spark3_breakout_watch.get(event.mint)
        if watch:
            watch_age_ms = event.ts_ms - int(watch.get("ts_ms") or 0)
            if watch_age_ms < confirm_ms:
                return None
            watch_price = float(watch.get("price") or 0.0)
            if watch_price <= 0 or price < watch_price * hold_ratio:
                self.spark3_breakout_seen.add(event.mint)
                self.spark3_breakout_watch.pop(event.mint, None)
                self.logger.decision(
                    "spark3_breakout_reject",
                    event.mint,
                    {
                        "lane": "spark3_breakout",
                        "reason": f"failed_hold age={watch_age_ms}ms hold={price / max(watch_price, 1e-18):.3f}",
                        "features": self.slim_features(features),
                    },
                )
                return None
            if breakout_mult > env_float("PGG2_SPARK3_BREAKOUT_CONFIRM_MAX_MULT", 4.50):
                self.spark3_breakout_seen.add(event.mint)
                self.spark3_breakout_watch.pop(event.mint, None)
                self.logger.decision(
                    "spark3_breakout_reject",
                    event.mint,
                    {
                        "lane": "spark3_breakout",
                        "reason": f"confirm_overextended break={breakout_mult:.3f}x",
                        "features": self.slim_features(features),
                    },
                )
                return None
            watch_features = dict(watch.get("features") or {})
            scout = min(
                self.config.max_position_sol,
                max(0.0005, env_float("PGG2_SPARK3_BREAKOUT_SOL", 0.050)),
            )
            score = 160.0 + min(160.0, max(0.0, breakout_mult - 1.0) * 220.0)
            reason = (
                f"spark3_breakout arm_age={arm_age_ms}ms confirm={watch_age_ms}ms break={breakout_mult:.3f}x "
                f"watch={float(watch.get('breakout_mult') or 0.0):.3f}x from={short_addr(event.mint)}"
            )
            plan = StrikePlan(
                mint=event.mint,
                ts_ms=event.ts_ms,
                lane="spark3_breakout",
                reason=reason,
                score=score,
                scout_sol=scout,
                target_sol=scout,
                price=price,
                needs_curve_fill=False,
                features=self.slim_features(features),
            )
            plan.features.update(
                {
                    "spark3_arm": arm,
                    "spark3_breakout_mult": breakout_mult,
                    "spark3_breakout_watch_features": watch_features,
                    "entry_size_reason": "spark3_breakout",
                    "entry_probe_sol": scout,
                }
            )
            self.spark3_breakout_watch.pop(event.mint, None)
            return plan
        sell700 = float(features["s700"].get("sell_sol") or 0.0)
        sell1500 = float(features["s1500"].get("sell_sol") or 0.0)
        buy700 = float(features["s700"].get("buy_sol") or 0.0)
        uniq700 = int(features["s700"].get("unique_buyers") or 0)
        top700 = float(features["s700"].get("top_buy_share") or 1.0)
        hhi700 = float(features["s700"].get("buyer_hhi") or 1.0)
        if uniq700 < env_int("PGG2_SPARK3_BREAKOUT_MIN_UNIQ700", 4):
            return None
        if top700 > env_float("PGG2_SPARK3_BREAKOUT_MAX_TOP700", 0.74):
            return None
        if hhi700 > env_float("PGG2_SPARK3_BREAKOUT_MAX_HHI700", 0.62):
            return None
        if sell700 + sell1500 > max(
            env_float("PGG2_SPARK3_BREAKOUT_MAX_SELL_SOL", 0.050),
            buy700 * env_float("PGG2_SPARK3_BREAKOUT_MAX_SELL_RATIO", 0.08),
        ):
            return None
        if breakout_mult > env_float("PGG2_SPARK3_BREAKOUT_MAX_MULT", 1.60):
            return None
        self.spark3_breakout_watch[event.mint] = {
            "ts_ms": event.ts_ms,
            "price": price,
            "breakout_mult": breakout_mult,
            "features": self.slim_features(features),
        }
        self.logger.decision(
            "spark3_breakout_watch",
            event.mint,
            {
                "lane": "spark3_breakout",
                "reason": f"watch break={breakout_mult:.3f}x",
                "features": self.slim_features(features),
            },
        )
        return None

    def _trade_guards_pass(self, event: "PumpEvent") -> bool:
        """Phase 2A adaptive guards. Return False to block this entry attempt.

        Two guards:
        1. Hour-block: PGG2_BLOCK_HOURS_UTC = "18,19,20" — comma list of UTC hours
           where the bot will refuse new entries. Phase 1 hourly heatmap showed
           18:00-21:00 UTC are the worst hours (-$2.91 combined across 22 runs).
        2. Circuit breaker: paused until self.circuit_breaker_until_ts. Set on a
           streak of consecutive losses by the close handler.
        """
        block_hours_str = env_str("PGG2_BLOCK_HOURS_UTC", "")
        if block_hours_str:
            try:
                block_hours = {int(h.strip()) for h in block_hours_str.split(",") if h.strip()}
                if block_hours:
                    from datetime import datetime, timezone
                    current_hour = datetime.fromtimestamp(event.ts_ms / 1000, tz=timezone.utc).hour
                    if current_hour in block_hours:
                        return False
            except Exception:
                pass
        if self.circuit_breaker_until_ts and event.ts_ms < self.circuit_breaker_until_ts:
            return False
        return True

    def curve_lag_reveal_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 curve-lag reveal lane.

        The live tape showed real moonshots that were armed before the bonding
        curve cache had a price (`has_curve=false`, `price=0`). The original
        lanes then waited for second-wave confirmation and often disarmed the
        mint as late/top-heavy after the move had already started.

        This lane does not buy the unpriced launch bundle. It waits until a real
        curve price appears, then requires post-price follow-through breadth.
        That keeps it distinct from the noisy fast-probe idea.
        """
        if not env_bool("PGG2_CURVE_LAG_REVEAL_ENABLED", True):
            return None
        if not self._trade_guards_pass(event):
            return None
        if not event.is_buy or event.mint in self.curve_lag_reveal_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        has_curve = bool(features.get("has_curve"))
        allow_price_hint = env_bool("PGG2_PRICED_BREAKOUT_ALLOW_PRICE_HINT", True)
        if features.get("complete") or (not has_curve and not allow_price_hint):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        arm = self.wave_arms.get(event.mint)
        if not arm or not arm.armed_without_curve:
            return None
        self.refresh_wave_arm(event.mint, features)
        if arm.first_price <= 0 or arm.first_price_ts_ms <= 0:
            return None

        arm_age_ms = int(features["ts_ms"] - arm.armed_ts_ms)
        first_price_age_ms = int(features["ts_ms"] - arm.first_price_ts_ms)
        if arm_age_ms > env_int("PGG2_CURVE_LAG_MAX_ARM_AGE_MS", 6000):
            return None
        if first_price_age_ms > env_int("PGG2_CURVE_LAG_MAX_FIRST_PRICE_AGE_MS", 4000):
            return None
        if arm.initial_slot_buyers < env_int("PGG2_CURVE_LAG_MIN_INITIAL_BUYERS", 3):
            return None
        if arm.initial_slot_buy_sol < env_float("PGG2_CURVE_LAG_MIN_INITIAL_SOL", 2.0):
            return None
        if arm.initial_slot_buy_sol > env_float("PGG2_CURVE_LAG_MAX_INITIAL_SOL", 10.0):
            return None
        if arm.initial_slot_top_share > env_float("PGG2_CURVE_LAG_MAX_INITIAL_TOP", 0.70):
            return None
        if arm.initial_sell_sol > max(0.010, arm.initial_buy_sol * env_float("PGG2_CURVE_LAG_MAX_INITIAL_SELL_RATIO", 0.04)):
            return None

        follow = self.event_window_stats(event.mint, arm.first_price_ts_ms + 1, int(features["ts_ms"]))
        follow_buy_sol = float(follow["buy_sol"])
        follow_unique = int(follow["unique_buyers"])
        follow_top = float(follow["top_buy_share"])
        follow_sell_sol = float(follow["sell_sol"])
        if follow_buy_sol < env_float("PGG2_CURVE_LAG_MIN_FOLLOW_SOL", 5.0):
            return None
        if follow_unique < env_int("PGG2_CURVE_LAG_MIN_FOLLOW_BUYERS", 2):
            return None
        if follow_top > env_float("PGG2_CURVE_LAG_MAX_FOLLOW_TOP", 0.75):
            return None
        if follow_sell_sol > max(0.010, follow_buy_sol * env_float("PGG2_CURVE_LAG_MAX_FOLLOW_SELL_RATIO", 0.15)):
            return None

        # The live tape showed curve-lag losses clustered in weak current 700ms
        # breadth. Keep the lane fast, but require the reveal to still have
        # real live buy pressure at the decision moment.
        s700_live = features.get("s700") or {}
        live_buy700 = float(s700_live.get("buy_sol") or 0.0)
        live_unique700 = int(s700_live.get("unique_buyers") or 0)
        live_top700 = float(s700_live.get("top_buy_share") or 1.0)
        if live_buy700 < env_float("PGG2_CURVE_LAG_MIN_LIVE_BUY700_SOL", 5.0):
            return None
        if live_unique700 < env_int("PGG2_CURVE_LAG_MIN_LIVE_BUYERS700", 5):
            return None
        if live_top700 > env_float("PGG2_CURVE_LAG_MAX_LIVE_TOP700", 0.70):
            return None

        entry_move_from_first = price / max(arm.first_price, 1e-18)
        if entry_move_from_first > env_float("PGG2_CURVE_LAG_MAX_ENTRY_MOVE", 1.25):
            return None

        scout = min(
            self.config.max_position_sol,
            max(0.0005, env_float("PGG2_CURVE_LAG_SOL", self.config.scout_sol)),
        )
        score = (
            120.0
            + min(55.0, follow_buy_sol * 6.0)
            + min(35.0, follow_unique * 4.0)
            + min(40.0, live_buy700 * 4.0)
            + max(0.0, entry_move_from_first - 1.0) * 70.0
            - max(0.0, follow_top - 0.45) * 45.0
        )
        reason = (
            f"curve_lag_reveal arm={arm_age_ms}ms first_age={first_price_age_ms}ms "
            f"init={arm.initial_slot_buy_sol:.2f}/{arm.initial_slot_buyers} top={arm.initial_slot_top_share:.2f} "
            f"follow={follow_buy_sol:.2f}/{follow_unique} top={follow_top:.2f} "
            f"live700={live_buy700:.2f}/{live_unique700} top={live_top700:.2f} "
            f"move={entry_move_from_first:.2f}x"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="curve_lag_reveal",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update(
            {
                "arm_first_price": arm.first_price,
                "arm_first_price_ts_ms": arm.first_price_ts_ms,
                "entry_move_from_first": entry_move_from_first,
                "curve_lag_follow": follow,
                "curve_lag_live_buy700": live_buy700,
                "curve_lag_live_unique700": live_unique700,
                "curve_lag_live_top700": live_top700,
                "entry_size_reason": "curve_lag_reveal_probe",
                "entry_probe_sol": scout,
            }
        )
        return plan

    def preprice_reveal_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 preprice-reveal lane.

        Current quote tape showed a blind spot: a broad zero-price launch bundle
        arms the mint, first real curve price appears within ~1s, then the token
        moves before post-price confirmation reaches curve_lag thresholds. This
        lane buys the first priced continuation after a broad pre-price cluster.
        """
        if not env_bool("PGG2_PREPRICE_REVEAL_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.preprice_reveal_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        has_curve = bool(features.get("has_curve"))
        allow_price_hint = env_bool("PGG2_PRICED_BREAKOUT_ALLOW_PRICE_HINT", True)
        if features.get("complete") or (not has_curve and not allow_price_hint):
            if env_bool("PGG2_PRICED_BREAKOUT_DEBUG", False) and event.sol >= 0.10:
                log(
                    f"PGG2-PRICED-BREAKOUT-SKIP {short_addr(event.mint)} no_curve "
                    f"has_curve={int(bool(features.get('has_curve')))} price={float(features.get('price') or 0.0):.9e} "
                    f"buy={event.sol:.3f}"
                )
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            if env_bool("PGG2_PRICED_BREAKOUT_DEBUG", False) and event.sol >= 0.10:
                log(f"PGG2-PRICED-BREAKOUT-SKIP {short_addr(event.mint)} zero_price buy={event.sol:.3f}")
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            if env_bool("PGG2_PRICED_BREAKOUT_DEBUG", False) and event.sol >= 0.10:
                log(f"PGG2-PRICED-BREAKOUT-SKIP {short_addr(event.mint)} no_tape_price buy={event.sol:.3f}")
            return None
        first_price_ts, first_price = tape.prices[0]
        if first_price <= 0:
            return None
        anchor_ts = tape.first_create_ms or tape.first_seen_ms or first_price_ts
        first_price_delay_ms = max(0, first_price_ts - anchor_ts)
        first_price_age_ms = max(0, int(features["ts_ms"]) - first_price_ts)
        if first_price_delay_ms < env_int("PGG2_PREPRICE_REVEAL_MIN_FIRST_PRICE_DELAY_MS", 150):
            return None
        if first_price_delay_ms > env_int("PGG2_PREPRICE_REVEAL_MAX_FIRST_PRICE_DELAY_MS", 1200):
            return None
        if first_price_age_ms > env_int("PGG2_PREPRICE_REVEAL_MAX_FIRST_PRICE_AGE_MS", 700):
            return None

        pre = self.event_window_stats(event.mint, anchor_ts, first_price_ts)
        pre_buy_sol = float(pre.get("buy_sol") or 0.0)
        pre_sell_sol = float(pre.get("sell_sol") or 0.0)
        pre_unique = int(pre.get("unique_buyers") or 0)
        pre_top = float(pre.get("top_buy_share") or 1.0)
        if pre_buy_sol < env_float("PGG2_PREPRICE_REVEAL_MIN_PRE_BUY_SOL", 14.0):
            return None
        if pre_unique < env_int("PGG2_PREPRICE_REVEAL_MIN_PRE_BUYERS", 12):
            return None
        if pre_top > env_float("PGG2_PREPRICE_REVEAL_MAX_PRE_TOP", 0.30):
            return None
        if pre_sell_sol > max(0.010, pre_buy_sol * env_float("PGG2_PREPRICE_REVEAL_MAX_PRE_SELL_RATIO", 0.08)):
            return None

        vsol = float(features.get("vsol_sol") or 0.0)
        entry_move = price / max(first_price, 1e-18)
        if event.sol < env_float("PGG2_PREPRICE_REVEAL_MIN_CURRENT_BUY_SOL", 0.03):
            return None
        if vsol < env_float("PGG2_PREPRICE_REVEAL_MIN_VSOL", 35.0):
            return None
        if entry_move > env_float("PGG2_PREPRICE_REVEAL_MAX_ENTRY_MOVE", 1.15):
            return None

        scout = min(self.config.max_position_sol, env_float("PGG2_PREPRICE_REVEAL_SOL", self.config.scout_sol))
        score = (
            130.0
            + min(60.0, pre_buy_sol * 2.0)
            + min(42.0, pre_unique * 3.0)
            + min(20.0, event.sol * 40.0)
            - max(0.0, pre_top - 0.30) * 55.0
        )
        reason = (
            f"preprice_reveal delay={first_price_delay_ms}ms first_age={first_price_age_ms}ms "
            f"pre={pre_buy_sol:.2f}/{pre_unique} top={pre_top:.2f} "
            f"cur_buy={event.sol:.3f} "
            f"move={entry_move:.2f}x vsol={vsol:.2f}"
        )
        reveal_features = self.slim_features(features)
        reveal_features.update(
            {
                "preprice_first_price": first_price,
                "preprice_first_price_ts_ms": first_price_ts,
                "preprice_first_price_delay_ms": first_price_delay_ms,
                "preprice_first_price_age_ms": first_price_age_ms,
                "preprice_pre": pre,
                "preprice_entry_move": entry_move,
                "entry_size_reason": "preprice_reveal",
                "entry_probe_sol": scout,
            }
        )
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="preprice_reveal",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=reveal_features,
        )

    def engagement_driven_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Phase 18 2026-05-08: engagement-driven entry lane.

        Fires on mints with proven community engagement (livestreaming with
        viewers OR active KOTH status). These are typically older mints
        (2-30+ min old) outside priced_snap's 60s entry window. The engagement
        signals — livestream viewers, chat replies, KOTH rotation — are
        retail-uncomputable from raw on-chain data and represent organic
        interest distinguishable from bot pile-on.

        Different criteria than priced_snap:
        - No age cap (mint can be any age)
        - Smaller stake (0.025 SOL default — entering older/post-pump position)
        - Loose price-action criteria (don't require entry_move 1.18-2.50)
        - Tighter buy/uniq requirement (organic only, RugCheck still gates)
        """
        if not env_bool("PGG2_ENGAGEMENT_DRIVEN_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.engagement_driven_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        eng_poller = getattr(self, "engagement_poller", None)
        if eng_poller is None:
            return None
        # Must show meaningful engagement
        min_viewers = env_int("PGG2_ENGAGEMENT_MIN_VIEWERS", 10)
        min_replies = env_int("PGG2_ENGAGEMENT_MIN_REPLIES", 3)
        is_engaged = eng_poller.is_engaged(event.mint, min_viewers=min_viewers, min_replies=min_replies)
        is_koth = eng_poller.is_koth(event.mint)
        if not (is_engaged or is_koth):
            return None
        # Phase 20: ALLOW post-migration tokens (complete=True)
        # Most engaged tokens have already graduated to PumpSwap. The broker
        # routes complete=True mints to PumpSwap swap building. Don't reject.
        # Only require has_curve OR complete (one of them is true)
        if not bool(features.get("has_curve")) and not features.get("complete"):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        # Need SOME activity but not the strict priced_snap thresholds
        buy1500 = float(features.get("buy1500") or 0.0)
        if buy1500 < env_float("PGG2_ENGAGEMENT_MIN_BUY1500", 1.0):
            return None
        uniq1500 = int(features.get("uniq1500") or 0)
        if uniq1500 < env_int("PGG2_ENGAGEMENT_MIN_UNIQ1500", 3):
            return None
        # Reject if currently dumping
        sell1500 = float(features.get("sell1500") or 0.0)
        sell_ratio = sell1500 / max(buy1500, 0.001)
        if sell_ratio > env_float("PGG2_ENGAGEMENT_MAX_SELL_RATIO", 0.20):
            return None
        # Phase 20: minimum mint age (must be PAST the 91% rug zone)
        age_ms = int(features.get("age_ms") or 0)
        min_age_ms = env_int("PGG2_ENGAGEMENT_MIN_AGE_MS", 60000)  # 60 sec default
        max_age_ms = env_int("PGG2_ENGAGEMENT_MAX_AGE_MS", 600000)  # 10 min cap
        if age_ms < min_age_ms or age_ms > max_age_ms:
            return None
        # Phase 20: require recent buying (active flow, not dead mint)
        last_buy_age = features.get("last_buy_age_ms", 999999)
        if last_buy_age is not None and last_buy_age > env_int("PGG2_ENGAGEMENT_MAX_LAST_BUY_AGE_MS", 1500):
            return None
        # Pull engagement details for log + features
        eng_info = eng_poller.get_engagement(event.mint) or {}
        viewers = int(eng_info.get("num_participants") or 0)
        replies = int(eng_info.get("reply_count") or 0)
        is_currently_live = bool(eng_info.get("is_currently_live"))
        usd_mc = float(eng_info.get("usd_market_cap") or 0)

        # RugCheck gate
        rug_safe = True
        rug_score = 0
        rug_reason = "skipped"
        rugchecker = getattr(self, "rugcheck_client", None)
        if rugchecker is not None and env_bool("PGG2_RUGCHECK_GATE_ENABLED", True):
            try:
                rug_safe, rug_score, rug_reason = rugchecker.is_safe_sync(event.mint)
            except Exception:
                rug_safe = True
                rug_reason = "exception_failopen"
            if not rug_safe:
                log(f"PGG2-RUGCHECK-REJECT mint={event.mint[:8]} lane=engagement_driven score={rug_score} reason={rug_reason}")
                return None

        # Smaller stake — engaged mints are post-pump phase, lower expected return
        full_scout = min(self.config.max_position_sol,
                         env_float("PGG2_ENGAGEMENT_LANE_SOL", 0.025))
        scout = full_scout
        target = full_scout

        score = 250.0 + min(50.0, viewers * 1.5) + min(30.0, replies * 2.0) + (50.0 if is_koth else 0.0)
        reason = (
            f"engagement_driven viewers={viewers} replies={replies} "
            f"live={is_currently_live} koth={is_koth} buy1500={buy1500:.2f} mc=${usd_mc:.0f} "
            f"rug={rug_score}"
        )
        snap_features = self.slim_features(features)
        snap_features.update({
            "entry_size_reason": "engagement_driven",
            "entry_probe_sol": scout,
            "engagement_viewers": viewers,
            "engagement_replies": replies,
            "engagement_currently_live": is_currently_live,
            "engagement_koth": is_koth,
            "engagement_usd_mc": usd_mc,
            "rugcheck_score": rug_score,
            "rugcheck_reason": rug_reason,
        })
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="engagement_driven",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=snap_features,
        )

    def bounce_buy_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Phase 21 2026-05-08: bounce-buy lane (option B).

        Fires when a tape shows a >=30% dump from a recent local peak within
        the last 60s and the current event is a buy (someone is stepping in
        to catch the falling knife — we ride alongside).

        Why: pump.fun bonding-curve dumps tend to overshoot. After panic
        sellers exhaust, price often bounces 5-10% within 30-90s. Buying
        engagement-pump exhaust is structurally INVERSE to chasing engagement.

        Exits: managed by manage_position via a dedicated bounce_buy block —
        +5% profit bank, -7% catastrophic stop, 90s timebox.
        """
        if not env_bool("PGG2_BOUNCE_BUY_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.bounce_buy_seen:
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None

        tape = self.tapes.get(event.mint)
        if tape is None:
            return None
        if tape.peak_price <= 0 or tape.last_price <= 0:
            return None

        # Drop check: how far below peak are we?
        off_peak = tape.last_price / tape.peak_price
        min_drop = env_float("PGG2_BOUNCE_BUY_MIN_DROP_PCT", 0.30)  # >= 30% dump
        max_drop = env_float("PGG2_BOUNCE_BUY_MAX_DROP_PCT", 0.65)  # < 65% (above 65% is rug)
        drop_pct = 1.0 - off_peak
        if drop_pct < min_drop:
            return None
        if drop_pct >= max_drop:
            return None  # too deep — likely rug, not bounce

        # Recency check: peak must be within last N seconds
        max_age_since_peak = env_float("PGG2_BOUNCE_BUY_MAX_AGE_SINCE_PEAK_SEC", 60.0)
        age_since_peak_sec = tape.time_since_peak_sec(event.ts_ms)
        if age_since_peak_sec > max_age_since_peak:
            return None
        if age_since_peak_sec < 2.0:
            return None  # peak too fresh — let the dump complete first

        price = float(features.get("price") or tape.last_price or 0.0)
        if price <= 0:
            return None

        # Has-curve check (don't bounce migrated tokens — different liquidity dynamics)
        if not bool(features.get("has_curve")) and not features.get("complete"):
            return None
        only_pre_migration = env_bool("PGG2_BOUNCE_BUY_ONLY_PRE_MIGRATION", True)
        if only_pre_migration and bool(features.get("complete")):
            return None

        # Mint age — skip ultra-fresh (dumps in first 30s are often creator dumps)
        age_ms = int(features.get("age_ms") or 0)
        if age_ms < env_int("PGG2_BOUNCE_BUY_MIN_AGE_MS", 60000):  # 60s minimum
            return None
        if age_ms > env_int("PGG2_BOUNCE_BUY_MAX_AGE_MS", 1800000):  # 30 min max
            return None

        # Need recent buy activity (someone else is also stepping in)
        s700 = features.get("s700") or {}
        recent_buyers = int(s700.get("unique_buyers") or 0)
        if recent_buyers < env_int("PGG2_BOUNCE_BUY_MIN_RECENT_BUYERS", 4):
            return None

        # Phase 22: real buy volume needed (not whale-only). Phase 21 losses
        # had buy700 < 4 SOL with 2 buyers — thin liquidity gets eaten by slippage.
        min_buy700 = env_float("PGG2_BOUNCE_BUY_MIN_BUY700_SOL", 3.0)
        buy700 = float(s700.get("buy_sol") or features.get("buy700") or 0.0)
        if buy700 < min_buy700:
            return None

        # Phase 22: whale concentration filter. Phase 21 losses had top700 0.5-0.9.
        max_top700 = env_float("PGG2_BOUNCE_BUY_MAX_TOP700", 0.40)
        top700 = float(s700.get("top_buy_share") or features.get("top_share700") or 0.0)
        if top700 > max_top700:
            return None

        # Don't enter if sells are still dominating (tightened from 1.50 → 0.30)
        s1500 = features.get("s1500") or {}
        buy1500 = float(s1500.get("buy_sol") or 0.0)
        sell1500 = float(s1500.get("sell_sol") or 0.0)
        sell_ratio = sell1500 / max(buy1500, 0.001)
        if sell_ratio > env_float("PGG2_BOUNCE_BUY_MAX_SELL_RATIO", 0.30):
            return None  # sells still way ahead — wait

        # Phase 22: top1500 whale filter (universal block also catches but be explicit)
        max_top1500 = env_float("PGG2_BOUNCE_BUY_MAX_TOP1500", 0.40)
        top1500 = float(s1500.get("top_buy_share") or features.get("top_share1500") or 0.0)
        if top1500 > max_top1500:
            return None

        # RugCheck gate
        rug_safe = True
        rug_score = 0
        rug_reason = "skipped"
        rugchecker = getattr(self, "rugcheck_client", None)
        if rugchecker is not None and env_bool("PGG2_RUGCHECK_GATE_ENABLED", True):
            try:
                rug_safe, rug_score, rug_reason = rugchecker.is_safe_sync(event.mint)
            except Exception:
                rug_safe = True
                rug_reason = "exception_failopen"
            if not rug_safe:
                log(f"PGG2-BOUNCE-RUGCHECK-REJECT mint={event.mint[:8]} score={rug_score} reason={rug_reason}")
                self.bounce_buy_seen.add(event.mint)
                return None

        # Stake — moderate size since bounce is high-conviction inverse signal
        full_scout = min(self.config.max_position_sol,
                         env_float("PGG2_BOUNCE_BUY_LANE_SOL", 0.040))

        score = 200.0 + drop_pct * 100.0 + recent_buyers * 5.0
        reason = (
            f"bounce_buy drop={drop_pct:.1%} age_since_peak={age_since_peak_sec:.0f}s "
            f"buyers700={recent_buyers} sell_ratio={sell_ratio:.2f} rug={rug_score}"
        )
        snap_features = self.slim_features(features)
        snap_features.update({
            "entry_size_reason": "bounce_buy",
            "entry_probe_sol": full_scout,
            "bounce_peak_price": tape.peak_price,
            "bounce_last_price": tape.last_price,
            "bounce_drop_pct": drop_pct,
            "bounce_age_since_peak_sec": age_since_peak_sec,
            "bounce_recent_buyers": recent_buyers,
            "bounce_sell_ratio": sell_ratio,
            "rugcheck_score": rug_score,
            "rugcheck_reason": rug_reason,
        })
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="bounce_buy",
            reason=reason,
            score=score,
            scout_sol=full_scout,
            target_sol=full_scout,
            price=price,
            needs_curve_fill=False,
            features=snap_features,
        )

    def priced_snap_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Immediate priced-breakout lane.

        Raw-tape forensics showed the existing priced_breakout confirmation wait
        was the wrong shape for the live runners: the signal is already visible
        at the first clean 1.18x-1.75x priced break, and waiting 750ms often
        turns it into a late/tight-stop entry. This lane enters immediately but
        only on clean recent breadth and low sell pressure.
        """
        if not env_bool("PGG2_PRICED_SNAP_ENABLED", False):
            return None
        if not self._trade_guards_pass(event):
            return None
        if not event.is_buy or event.mint in self.priced_snap_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if features.get("complete") or not bool(features.get("has_curve")):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            return None
        first_price_ts, first_price = tape.prices[0]
        first_price = float(first_price or 0.0)
        if first_price <= 0:
            return None

        entry_move = price / first_price
        age_sec = (event.ts_ms - int(first_price_ts)) / 1000.0
        if age_sec < env_float("PGG2_PRICED_SNAP_MIN_AGE_SEC", 0.15):
            return None
        if age_sec > env_float("PGG2_PRICED_SNAP_MAX_AGE_SEC", 45.0):
            return None
        if entry_move < env_float("PGG2_PRICED_SNAP_MIN_MOVE", 1.18):
            return None
        if entry_move > env_float("PGG2_PRICED_SNAP_MAX_MOVE", 1.75):
            return None

        buy1500 = float(features.get("buy1500") or 0.0)
        sell1500 = float(features.get("sell1500") or 0.0)
        uniq1500 = int(features.get("uniq1500") or 0)
        top1500 = float(features.get("top_share1500") or 1.0)
        sell_ratio = sell1500 / max(buy1500, 0.001)
        # Phase 15 2026-05-08: ENGAGEMENT BOOST (pump.fun frontend signals).
        # Mints with active livestreams + chat replies + KOTH status get
        # relaxed entry filters. These engagement signals can't be computed
        # from raw on-chain data — they're the genuinely-new signal layer.
        engaged = False
        koth = False
        engage_relax = 1.0
        eng_poller = getattr(self, "engagement_poller", None)
        if eng_poller is not None:
            try:
                if eng_poller.is_engaged(event.mint, min_viewers=10, min_replies=5):
                    engaged = True
                    engage_relax = env_float("PGG2_ENGAGEMENT_RELAX", 1.30)
                if eng_poller.is_koth(event.mint, max_age_sec=120.0):
                    koth = True
                    engage_relax = max(engage_relax, env_float("PGG2_KOTH_RELAX", 1.50))
            except Exception:
                pass
        min_buy1500 = env_float("PGG2_PRICED_SNAP_MIN_BUY1500", 6.0) / engage_relax
        min_uniq1500 = max(2, int(env_int("PGG2_PRICED_SNAP_MIN_UNIQ1500", 4) / engage_relax))
        max_top1500 = min(0.95, env_float("PGG2_PRICED_SNAP_MAX_TOP1500", 0.55) * engage_relax)
        if buy1500 < min_buy1500:
            return None
        if uniq1500 < min_uniq1500:
            return None
        if top1500 > max_top1500:
            return None
        # Phase 12 2026-05-08: ANTI-BOT SHARE FILTER (Marino arXiv 2602.14860).
        # Tokens dominated by bot activity in first 30-60s have LOWER graduation
        # probability per peer-reviewed on-chain study. Inverts every retail
        # bot's selection bias. Reject candidates where first 5-20 buys look
        # algorithmically generated (regular intervals, identical amounts,
        # slot concentration). Smart-wallet boost is REMOVED — research showed
        # smart-wallet copy is structurally lossy.
        if env_bool("PGG2_ANTIBOT_FILTER_ENABLED", True):
            tape_for_bot = self.tapes.get(event.mint)
            bot_share = compute_bot_share(tape_for_bot)
            bot_share_max = env_float("PGG2_ANTIBOT_MAX_SHARE", 0.40)
            if bot_share > bot_share_max:
                return None
        if sell1500 > max(0.010, buy1500 * env_float("PGG2_PRICED_SNAP_MAX_SELL_RATIO1500", 0.08)):
            return None
        if event.sol < env_float("PGG2_PRICED_SNAP_MIN_CURRENT_BUY_SOL", 0.03):
            return None
        vsol = float(features.get("vsol_sol") or 0.0)
        min_vsol = env_float("PGG2_PRICED_SNAP_MIN_VSOL", 0.0)
        if min_vsol > 0 and vsol < min_vsol:
            return None
        # Phase 10 2026-05-08: VOLUME-SUSTAIN FILTER (Marino arXiv 2602.14860).
        # Bundle-and-bail anti-pattern: t=0-5s velocity is high (triggers our
        # buy1500 filter), then t=5-25s velocity dies. Every Phase 5/6/7/8 loss
        # autopsy showed the same signature — `last_buy_age_ms` was high right
        # at our strike moment (buyers had paused ≥1.5s before we struck).
        # Rejecting strikes when buyers have already paused stops us from
        # entering AT THE TOP of a brief wave that's already dead.
        max_last_buy_age = env_int("PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS", 600)
        last_buy_age = features.get("last_buy_age_ms", 999999)
        if last_buy_age is not None and last_buy_age > max_last_buy_age:
            return None
        # Phase 10: vSol sweet-spot gate. Marino: 25-45% bonded (vSol 28-50)
        # has 16x graduation odds vs 0-25%. Below 28 SOL is variance-paid;
        # above 50 SOL the late-buyer math breaks down per the (vSol/115)²
        # economic-breakeven curve.
        vsol_min = env_float("PGG2_PRICED_SNAP_MIN_VSOL_SWEET", 0.0)
        vsol_max = env_float("PGG2_PRICED_SNAP_MAX_VSOL_SWEET", 999.0)
        if vsol_min > 0 and (vsol < vsol_min or vsol > vsol_max):
            return None

        full_scout = min(self.config.max_position_sol, env_float("PGG2_PRICED_SNAP_SOL", self.config.scout_sol))
        scout = full_scout
        target = full_scout
        slot_buy_sol = float(features.get("slot_buy_sol") or 0.0)
        slot_buyers = int(features.get("slot_buyers") or 0)
        slot_top_share = float(features.get("slot_top_share") or 1.0)
        vertical_snap = (
            slot_buy_sol >= env_float("PGG2_PRICED_SNAP_VERTICAL_MIN_SLOT_BUY_SOL", 5.0)
            and slot_buyers >= env_int("PGG2_PRICED_SNAP_VERTICAL_MIN_SLOT_BUYERS", 4)
            and float(features.get("move700") or 0.0) >= env_float("PGG2_PRICED_SNAP_VERTICAL_MIN_MOVE700", 1.0)
            and slot_top_share <= env_float("PGG2_PRICED_SNAP_VERTICAL_MAX_SLOT_TOP", 0.58)
        )
        elite_snap = (
            buy1500 >= env_float("PGG2_PRICED_SNAP_ELITE_MIN_BUY1500", 999999.0)
            and uniq1500 >= env_int("PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500", 999999)
            and top1500 <= env_float("PGG2_PRICED_SNAP_ELITE_MAX_TOP1500", 0.0)
            and age_sec <= env_float("PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC", 0.0)
            and slot_top_share <= env_float("PGG2_PRICED_SNAP_ELITE_MAX_SLOT_TOP", 1.0)
        )
        if env_bool("PGG2_LAYERED_RISK_ENABLED", False):
            entry_fraction = env_float("PGG2_LAYERED_ENTRY_FRACTION", 0.80)
            if vertical_snap:
                entry_fraction = env_float("PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION", 0.30)
            if elite_snap:
                entry_fraction = env_float("PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION", entry_fraction)
            else:
                entry_fraction = env_float("PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION", entry_fraction)
            scout = max(0.0005, full_scout * entry_fraction)
        # Phase 3 2026-05-08: anti-martingale stake scaling.
        # After consecutive losses, scale stake DOWN (preserve capital during bad
        # tape). After consecutive wins, scale stake UP (compound during good
        # tape). Edge cases handled:
        #   - exactly at threshold: scaling fires (>=, not >)
        #   - both streaks zero: no-op (factor = 1.0)
        #   - circuit breaker pre-empts entry; this only runs if guards pass
        #   - clamp to [PGG2_LIVE_MIN_TRADE_SOL, PGG2_LIVE_MAX_TRADE_SOL] so the
        #     final stake never under/overflows live execution bounds
        am_factor = 1.0
        am_label = ""
        if env_bool("PGG2_ANTI_MARTINGALE_ENABLED", False):
            loss_streak_n = env_int("PGG2_ANTI_MARTINGALE_LOSS_STREAK", 2)
            win_streak_n = env_int("PGG2_ANTI_MARTINGALE_WIN_STREAK", 2)
            if loss_streak_n > 0 and self.consecutive_losses >= loss_streak_n:
                am_factor = env_float("PGG2_ANTI_MARTINGALE_LOSS_SCALE", 0.50)
                am_label = f"loss_streak_{self.consecutive_losses}"
            elif win_streak_n > 0 and self.consecutive_wins >= win_streak_n:
                am_factor = env_float("PGG2_ANTI_MARTINGALE_WIN_SCALE", 1.30)
                am_label = f"win_streak_{self.consecutive_wins}"
        if am_factor != 1.0:
            scaled = scout * am_factor
            am_min = env_float("PGG2_LIVE_MIN_TRADE_SOL", 0.0005)
            am_max = env_float("PGG2_LIVE_MAX_TRADE_SOL", self.config.max_position_sol)
            scout = max(am_min, min(scaled, am_max))
            target = max(am_min, min(target * am_factor, am_max))
        score = (
            150.0
            + min(55.0, buy1500 * 5.5)
            + min(45.0, uniq1500 * 5.0)
            + max(0.0, entry_move - 1.0) * 120.0
            - max(0.0, top1500 - 0.40) * 55.0
            - sell_ratio * 95.0
        )
        # Phase 15A 2026-05-08: RugCheck pre-buy gate.
        # Last filter — only call when ALL cheap filters have passed (saves API quota).
        # 200ms blocking call; fails OPEN on timeout/error so RugCheck downtime
        # doesn't kill the bot. Caches results 5min so we don't re-call per mint.
        rug_safe = True
        rug_score = 0
        rug_reason = "skipped"
        rugchecker = getattr(self, "rugcheck_client", None)
        if rugchecker is not None and env_bool("PGG2_RUGCHECK_GATE_ENABLED", True):
            try:
                rug_safe, rug_score, rug_reason = rugchecker.is_safe_sync(event.mint)
            except Exception as exc:
                log(f"PGG2-RUGCHECK error {type(exc).__name__}: {exc} — failopen")
                rug_safe = True
                rug_reason = "exception_failopen"
            if not rug_safe:
                # Reject this strike — log so we can verify it's working
                log(
                    f"PGG2-RUGCHECK-REJECT mint={event.mint[:8]} score={rug_score} reason={rug_reason}"
                )
                return None

        reason = (
            f"priced_snap move={entry_move:.2f}x age={age_sec:.1f}s "
            f"b1500={buy1500:.2f}/{uniq1500} top={top1500:.2f} "
            f"cur_buy={event.sol:.2f} sellr={sell_ratio:.2f} vsol={vsol:.2f} "
            f"rug={rug_score} eng={'Y' if engaged else 'N'}{'+koth' if koth else ''}"
        )
        snap_features = self.slim_features(features)
        snap_features.update(
            {
                "entry_size_reason": "priced_snap",
                "entry_probe_sol": scout,
                "priced_snap_first_price": first_price,
                "priced_snap_first_price_ts_ms": int(first_price_ts),
                "priced_snap_entry_move": entry_move,
                "priced_snap_age_sec": age_sec,
                "priced_snap_sell_ratio1500": sell_ratio,
                "priced_snap_vertical": vertical_snap,
                "priced_snap_elite": elite_snap,
                "rugcheck_score": rug_score,
                "rugcheck_reason": rug_reason,
                "engagement_engaged": engaged,
                "engagement_koth": koth,
                "engagement_relax": engage_relax,
                "anti_martingale_factor": am_factor,
                "anti_martingale_label": am_label,
                "consecutive_losses": self.consecutive_losses,
                "consecutive_wins": self.consecutive_wins,
            }
        )
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="priced_snap",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=snap_features,
        )

    def priced_breakout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 priced-breakout lane.

        This is the live blind-spot lane found from quote/raw logs: some mints
        never form the old wave/reclaim plan, but once a real curve price exists
        they cleanly break 1.35x from first price and continue to 1.8x-8x. The
        lane buys only after priced confirmation, with recent breadth and no
        sell pressure; live quote preflight still decides whether execution is
        clean enough to actually open.
        """
        if not env_bool("PGG2_PRICED_BREAKOUT_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.priced_breakout_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        has_curve = bool(features.get("has_curve"))
        allow_price_hint = env_bool("PGG2_PRICED_BREAKOUT_ALLOW_PRICE_HINT", True)
        if features.get("complete") or (not has_curve and not allow_price_hint):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            return None
        first_price_ts, first_price = tape.prices[0]
        first_price = float(first_price or 0.0)
        if first_price <= 0:
            return None
        entry_move = price / first_price
        age_sec = (event.ts_ms - int(first_price_ts)) / 1000.0
        if age_sec < env_float("PGG2_PRICED_BREAKOUT_MIN_AGE_SEC", 0.25):
            return None
        if age_sec > env_float("PGG2_PRICED_BREAKOUT_MAX_AGE_SEC", 20.0):
            return None

        buy1500 = float(features.get("buy1500") or 0.0)
        sell1500 = float(features.get("sell1500") or 0.0)
        uniq1500 = int(features.get("uniq1500") or 0)
        top1500 = float(features.get("top_share1500") or 1.0)
        vsol = float(features.get("vsol_sol") or 0.0)
        sell_ratio = sell1500 / max(buy1500, 0.001)
        vsol_ok = vsol >= env_float("PGG2_PRICED_BREAKOUT_MIN_VSOL", 30.0) or (allow_price_hint and not has_curve)
        breadth_breakout = (
            buy1500 >= env_float("PGG2_PRICED_BREAKOUT_MIN_BUY1500", 3.0)
            and uniq1500 >= env_int("PGG2_PRICED_BREAKOUT_MIN_UNIQ1500", 4)
            and top1500 <= env_float("PGG2_PRICED_BREAKOUT_MAX_TOP1500", 0.60)
            and sell1500 <= max(0.010, buy1500 * env_float("PGG2_PRICED_BREAKOUT_MAX_SELL_RATIO1500", 0.08))
            and vsol_ok
        )
        whale_breakout = (
            env_bool("PGG2_PRICED_BREAKOUT_WHALE_PROFILE_ENABLED", True)
            and age_sec <= env_float("PGG2_PRICED_BREAKOUT_WHALE_MAX_AGE_SEC", 6.0)
            and event.sol >= env_float("PGG2_PRICED_BREAKOUT_WHALE_MIN_CURRENT_BUY_SOL", 0.20)
            and buy1500 >= env_float("PGG2_PRICED_BREAKOUT_WHALE_MIN_BUY1500", 0.20)
            and uniq1500 >= env_int("PGG2_PRICED_BREAKOUT_WHALE_MIN_UNIQ1500", 3)
            and top1500 <= env_float("PGG2_PRICED_BREAKOUT_WHALE_MAX_TOP1500", 0.75)
            and sell1500 <= max(0.005, buy1500 * env_float("PGG2_PRICED_BREAKOUT_WHALE_MAX_SELL_RATIO1500", 0.03))
            and (vsol >= env_float("PGG2_PRICED_BREAKOUT_WHALE_MIN_VSOL", 40.0) or (allow_price_hint and not has_curve))
        )
        if env_bool("PGG2_PRICED_BREAKOUT_DEBUG", False) and entry_move >= env_float("PGG2_PRICED_BREAKOUT_MIN_MOVE", 1.35):
            log(
                f"PGG2-PRICED-BREAKOUT-CHECK {short_addr(event.mint)} move={entry_move:.2f} age={age_sec:.1f}s "
                f"cur={event.sol:.3f} b1500={buy1500:.2f}/{uniq1500} top={top1500:.2f} "
                f"sell={sell1500:.3f} vsol={vsol:.2f} breadth={int(breadth_breakout)} whale={int(whale_breakout)}"
            )
        if not (breadth_breakout or whale_breakout):
            if event.mint in self.priced_breakout_watch:
                self.priced_breakout_seen.add(event.mint)
                self.priced_breakout_watch.pop(event.mint, None)
                self.logger.decision(
                    "priced_breakout_reject",
                    event.mint,
                    {"reason": "confirm_breadth_failed", "features": self.slim_features(features)},
                )
            return None
        if features.get("last_buy_age_ms", 999999) > env_int("PGG2_PRICED_BREAKOUT_MAX_LAST_BUY_AGE_MS", 450):
            return None

        profile = "breadth" if breadth_breakout else "whale"
        watch = self.priced_breakout_watch.get(event.mint)
        if not watch:
            if entry_move < env_float("PGG2_PRICED_BREAKOUT_MIN_MOVE", 1.35):
                return None
            if entry_move > env_float("PGG2_PRICED_BREAKOUT_MAX_MOVE", 1.85):
                return None
            self.priced_breakout_watch[event.mint] = {
                "ts_ms": event.ts_ms,
                "price": price,
                "entry_move": entry_move,
                "profile": profile,
            }
            self.logger.decision(
                "priced_breakout_watch",
                event.mint,
                {
                    "reason": f"watch {profile} move={entry_move:.3f}x",
                    "features": self.slim_features(features),
                },
            )
            return None

        watch_age_ms = event.ts_ms - int(watch["ts_ms"])
        if watch_age_ms > env_int("PGG2_PRICED_BREAKOUT_CONFIRM_MAX_WATCH_MS", 2500):
            self.priced_breakout_seen.add(event.mint)
            self.priced_breakout_watch.pop(event.mint, None)
            self.logger.decision(
                "priced_breakout_reject",
                event.mint,
                {"reason": f"confirm_timeout age={watch_age_ms}ms", "features": self.slim_features(features)},
            )
            return None
        if watch_age_ms < env_int("PGG2_PRICED_BREAKOUT_CONFIRM_MS", 750):
            return None
        watch_price = float(watch["price"])
        hold_ratio = price / max(watch_price, 1e-18)
        if hold_ratio < env_float("PGG2_PRICED_BREAKOUT_CONFIRM_MIN_HOLD", 0.97):
            self.priced_breakout_seen.add(event.mint)
            self.priced_breakout_watch.pop(event.mint, None)
            self.logger.decision(
                "priced_breakout_reject",
                event.mint,
                {
                    "reason": f"confirm_failed_hold age={watch_age_ms}ms hold={hold_ratio:.3f}",
                    "features": self.slim_features(features),
                },
            )
            return None
        if entry_move > env_float("PGG2_PRICED_BREAKOUT_CONFIRM_MAX_ENTRY_MOVE", 1.85):
            self.priced_breakout_seen.add(event.mint)
            self.priced_breakout_watch.pop(event.mint, None)
            self.logger.decision(
                "priced_breakout_reject",
                event.mint,
                {
                    "reason": f"confirm_overextended move={entry_move:.3f}x",
                    "features": self.slim_features(features),
                },
            )
            return None

        scout = min(self.config.max_position_sol, env_float("PGG2_PRICED_BREAKOUT_SOL", self.config.scout_sol))
        score = (
            145.0
            + min(50.0, buy1500 * 5.0)
            + min(42.0, uniq1500 * 5.0)
            + max(0.0, entry_move - 1.0) * 95.0
            - max(0.0, top1500 - 0.45) * 40.0
            - sell_ratio * 80.0
        )
        reason = (
            f"priced_breakout {profile} confirm={watch_age_ms}ms move={entry_move:.2f}x "
            f"watch={float(watch['entry_move']):.2f}x age={age_sec:.1f}s "
            f"b1500={buy1500:.2f}/{uniq1500} top={top1500:.2f} "
            f"cur_buy={event.sol:.2f} sellr={sell_ratio:.2f} vsol={vsol:.2f}"
        )
        breakout_features = self.slim_features(features)
        breakout_features.update(
            {
                "entry_size_reason": "priced_breakout",
                "entry_probe_sol": scout,
                "priced_breakout_first_price": first_price,
                "priced_breakout_first_price_ts_ms": int(first_price_ts),
                "priced_breakout_entry_move": entry_move,
                "priced_breakout_age_sec": age_sec,
                "priced_breakout_profile": profile,
                "priced_breakout_watch_move": float(watch["entry_move"]),
                "priced_breakout_confirm_ms": watch_age_ms,
                "priced_breakout_confirm_hold": hold_ratio,
            }
        )
        self.priced_breakout_watch.pop(event.mint, None)
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="priced_breakout",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=breakout_features,
        )

    def late_swarm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Late broad-swarm lane.

        Quote/raw runs showed a second blind spot: some real runners do not look
        tradable in the first 20 seconds, then a broad late swarm arrives
        (example: D3K3 with ~29 SOL / 15 buyers / top 0.14 around 87s). This
        lane is intentionally narrow: big breadth, low concentration, no sell
        pressure, and a live executable quote guard still has final veto.
        """
        if not env_bool("PGG2_LATE_SWARM_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.late_swarm_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if features.get("complete") or not features.get("has_curve"):
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            return None
        first_price_ts, first_price = tape.prices[0]
        first_price = float(first_price or 0.0)
        if first_price <= 0:
            return None
        age_sec = (event.ts_ms - int(first_price_ts)) / 1000.0
        if age_sec < env_float("PGG2_LATE_SWARM_MIN_AGE_SEC", 20.0):
            return None
        if age_sec > env_float("PGG2_LATE_SWARM_MAX_AGE_SEC", 180.0):
            return None
        entry_move = price / first_price
        if entry_move < env_float("PGG2_LATE_SWARM_MIN_MOVE", 1.05):
            return None
        if entry_move > env_float("PGG2_LATE_SWARM_MAX_MOVE", 3.20):
            return None

        s3000 = features.get("s3000") or {}
        buy3000 = float(features.get("buy3000") or s3000.get("buy_sol") or 0.0)
        sell3000 = float(features.get("sell3000") or s3000.get("sell_sol") or 0.0)
        uniq3000 = int(features.get("uniq3000") or s3000.get("unique_buyers") or 0)
        top3000 = float(features.get("top_share3000") or s3000.get("top_buy_share") or 1.0)
        sell_ratio = sell3000 / max(buy3000, 0.001)
        vsol = float(features.get("vsol_sol") or 0.0)
        if buy3000 < env_float("PGG2_LATE_SWARM_MIN_BUY3000", 20.0):
            return None
        if uniq3000 < env_int("PGG2_LATE_SWARM_MIN_UNIQ3000", 12):
            return None
        if top3000 > env_float("PGG2_LATE_SWARM_MAX_TOP3000", 0.22):
            return None
        if sell3000 > max(0.02, buy3000 * env_float("PGG2_LATE_SWARM_MAX_SELL_RATIO3000", 0.02)):
            return None
        if vsol < env_float("PGG2_LATE_SWARM_MIN_VSOL", 35.0):
            return None
        if features.get("last_buy_age_ms", 999999) > env_int("PGG2_LATE_SWARM_MAX_LAST_BUY_AGE_MS", 500):
            return None

        scout = min(self.config.max_position_sol, env_float("PGG2_LATE_SWARM_SOL", 0.020))
        score = (
            190.0
            + min(80.0, buy3000 * 2.5)
            + min(70.0, uniq3000 * 4.0)
            + max(0.0, entry_move - 1.0) * 55.0
            - top3000 * 55.0
            - sell_ratio * 160.0
        )
        reason = (
            f"late_swarm move={entry_move:.2f}x age={age_sec:.1f}s "
            f"b3000={buy3000:.2f}/{uniq3000} top={top3000:.2f} "
            f"sellr={sell_ratio:.3f} vsol={vsol:.2f}"
        )
        swarm_features = self.slim_features(features)
        swarm_features.update(
            {
                "entry_size_reason": "late_swarm",
                "entry_probe_sol": scout,
                "late_swarm_entry_move": entry_move,
                "late_swarm_age_sec": age_sec,
                "late_swarm_buy3000": buy3000,
                "late_swarm_uniq3000": uniq3000,
                "late_swarm_top3000": top3000,
            }
        )
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="late_swarm",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=swarm_features,
        )

    def curve_arm_scout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        if not env_bool("PGG2_CURVE_ARM_SCOUT_ENABLED", False):
            return None
        if not event.is_buy or event.mint in self.curve_arm_scout_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if features.get("complete") or not features.get("has_curve"):
            return None
        if int(features.get("wave_arm_age_ms") or 0) > env_int("PGG2_CURVE_ARM_MAX_ARM_AGE_MS", 900):
            return None
        buy1500 = float(features.get("buy1500") or 0.0)
        uniq1500 = int(features.get("uniq1500") or 0)
        top1500 = float(features.get("top_share1500") or 1.0)
        sell1500 = float(features.get("sell1500") or 0.0)
        vsol = float(features.get("vsol_sol") or 0.0)
        move700 = float(features.get("move700") or 1.0)
        if buy1500 < env_float("PGG2_CURVE_ARM_MIN_BUY1500", 2.50):
            return None
        if buy1500 > env_float("PGG2_CURVE_ARM_MAX_BUY1500", 4.00):
            return None
        if uniq1500 < env_int("PGG2_CURVE_ARM_MIN_UNIQ1500", 3):
            return None
        if top1500 > env_float("PGG2_CURVE_ARM_MAX_TOP1500", 0.40):
            return None
        if sell1500 > env_float("PGG2_CURVE_ARM_MAX_SELL1500", 0.001):
            return None
        if vsol < env_float("PGG2_CURVE_ARM_MIN_VSOL", 40.0):
            return None
        if move700 > env_float("PGG2_CURVE_ARM_MAX_MOVE700", 1.03):
            return None
        scout = min(self.config.max_position_sol, env_float("PGG2_CURVE_ARM_SCOUT_SOL", self.config.scout_sol))
        score = float(features.get("score") or 0.0)
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        reason = (
            f"curve_arm_scout b1500={buy1500:.3f}/{uniq1500} top={top1500:.2f} "
            f"vsol={vsol:.2f} move700={move700:.3f}"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="curve_arm_scout",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update({"entry_size_reason": "curve_arm_scout", "entry_probe_sol": scout})
        return plan

    def raw_momentum_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Quote-only blind-spot lane for older/unarmed tape momentum.

        Raw momentum is intentionally two-step:
          1. arm when the 10s tape shows broad momentum
          2. buy only after price confirms continuation from that arm

        The direct-buy version caught too many first-spike fakeouts. This keeps
        the moonshot blind-spot detector, but refuses to buy until the mint proves
        the spike is continuing instead of rolling over.
        """
        if not env_bool("PGG2_RAW_MOMENTUM_ENABLED", False):
            return None
        if not event.is_buy or event.mint in self.raw_momentum_seen:
            return None
        if features.get("complete"):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            return None
        first_price = float(tape.prices[0][1] or 0.0)
        if first_price <= 0:
            return None
        window_ms = env_int("PGG2_RAW_MOMENTUM_WINDOW_MS", 10000)
        stats = self.event_window_stats(event.mint, event.ts_ms - window_ms, event.ts_ms)
        buy_sol = float(stats.get("buy_sol") or 0.0)
        sell_sol = float(stats.get("sell_sol") or 0.0)
        unique_buyers = int(stats.get("unique_buyers") or 0)
        top_share = float(stats.get("top_buy_share") or 1.0)
        sell_ratio = sell_sol / max(buy_sol, 0.001)
        entry_move = price / first_price
        vsol = float(features.get("vsol_sol") or 0.0)
        if buy_sol < env_float("PGG2_RAW_MOMENTUM_MIN_BUY_SOL", 8.0):
            return None
        if unique_buyers < env_int("PGG2_RAW_MOMENTUM_MIN_UNIQ", 8):
            return None
        if top_share < env_float("PGG2_RAW_MOMENTUM_MIN_TOP", 0.25):
            return None
        if top_share > env_float("PGG2_RAW_MOMENTUM_MAX_TOP", 0.35):
            return None
        if sell_ratio > env_float("PGG2_RAW_MOMENTUM_MAX_SELL_RATIO", 0.15):
            return None
        if entry_move < env_float("PGG2_RAW_MOMENTUM_MIN_MOVE", 1.15):
            return None
        if entry_move > env_float("PGG2_RAW_MOMENTUM_MAX_MOVE", 1.70):
            return None
        if vsol < env_float("PGG2_RAW_MOMENTUM_MIN_VSOL", 35.0):
            return None
        arm = self.raw_momentum_arms.get(event.mint)
        if not arm:
            self.raw_momentum_arms[event.mint] = {
                "ts_ms": event.ts_ms,
                "price": price,
                "buy_sol": buy_sol,
                "sell_sol": sell_sol,
                "unique_buyers": unique_buyers,
                "top_share": top_share,
                "sell_ratio": sell_ratio,
                "entry_move": entry_move,
                "vsol": vsol,
            }
            log(
                f"PGG2-RAW-MOMENTUM-ARM {short_addr(event.mint)} "
                f"buy10s={buy_sol:.2f}/{unique_buyers} top={top_share:.2f} "
                f"move={entry_move:.2f}x vsol={vsol:.2f}"
            )
            return None
        arm_age = event.ts_ms - int(arm.get("ts_ms") or event.ts_ms)
        if arm_age > env_int("PGG2_RAW_MOMENTUM_CONFIRM_MAX_AGE_MS", 6500):
            self.raw_momentum_seen.add(event.mint)
            self.raw_momentum_arms.pop(event.mint, None)
            log(f"PGG2-RAW-MOMENTUM-EXPIRE {short_addr(event.mint)} age_ms={arm_age}")
            return None
        arm_price = float(arm.get("price") or 0.0)
        if arm_price <= 0:
            self.raw_momentum_seen.add(event.mint)
            self.raw_momentum_arms.pop(event.mint, None)
            return None
        confirm_mult = price / arm_price
        s700 = features["s700"]
        s1500 = features["s1500"]
        buy700 = float(s700.get("buy_sol") or 0.0)
        sell700 = float(s700.get("sell_sol") or 0.0)
        sell_ratio700 = sell700 / max(buy700, 0.001)
        if confirm_mult < env_float("PGG2_RAW_MOMENTUM_CONFIRM_MULT", 1.10):
            return None
        if features["last_buy_age_ms"] > env_int("PGG2_RAW_MOMENTUM_CONFIRM_MAX_LAST_BUY_MS", 450):
            return None
        if sell_ratio700 > env_float("PGG2_RAW_MOMENTUM_CONFIRM_MAX_SELL_RATIO700", 0.10):
            return None
        if int(s700.get("unique_buyers") or 0) < env_int("PGG2_RAW_MOMENTUM_CONFIRM_MIN_UNIQ700", 3):
            return None
        if float(s1500.get("buy_sol") or 0.0) < env_float("PGG2_RAW_MOMENTUM_CONFIRM_MIN_BUY1500", 3.5):
            return None
        scout = min(self.config.max_position_sol, env_float("PGG2_RAW_MOMENTUM_SOL", self.config.scout_sol))
        reason = (
            f"raw_momentum buy10s={buy_sol:.2f}/{unique_buyers} top={top_share:.2f} "
            f"sellr={sell_ratio:.2f} move={entry_move:.2f}x "
            f"confirm={confirm_mult:.2f}x age={arm_age}ms vsol={vsol:.2f}"
        )
        raw_features = self.slim_features(features)
        raw_features.update(
            {
                "raw_arm_age_ms": arm_age,
                "raw_confirm_mult": confirm_mult,
                "raw_buy_sol_10s": buy_sol,
                "raw_sell_sol_10s": sell_sol,
                "raw_unique_buyers_10s": unique_buyers,
                "raw_top_share_10s": top_share,
                "raw_sell_ratio_10s": sell_ratio,
                "raw_entry_move": entry_move,
                "entry_size_reason": "raw_momentum",
                "entry_probe_sol": scout,
            }
        )
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="raw_momentum",
            reason=reason,
            score=float(features.get("score") or 0.0),
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=raw_features,
        )

    def whale_spark_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """Delayed-swarm lane after a huge first buy.

        Current quote tape showed several 2x+ moves that start with a 10+ SOL
        first buyer while the curve cache is still unpriced, then a broad priced
        burst roughly 8-12 seconds later. The normal raw-momentum lane rejects
        them as top-heavy because the first whale dominates the whole 10s tape.
        This lane ignores the initial whale for entry and requires fresh recent
        breadth before buying.
        """
        if not env_bool("PGG2_WHALE_SPARK_ENABLED", False):
            return None
        if not event.is_buy or event.mint in self.whale_spark_seen:
            return None
        if self.recent_profit_reentry_locked(event.mint, event.ts_ms):
            return None
        if features.get("complete"):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        first_buy_sol = float(features.get("first_buy_sol") or 0.0)
        if first_buy_sol < env_float("PGG2_WHALE_SPARK_MIN_FIRST_BUY_SOL", 10.0):
            return None
        if first_buy_sol > env_float("PGG2_WHALE_SPARK_MAX_FIRST_BUY_SOL", 25.0):
            return None
        first_buyer = str(features.get("first_buyer") or "")
        trusted_raw = env_str("PGG2_WHALE_SPARK_TRUSTED_FIRST_BUYERS", "")
        trusted = {x.strip() for x in trusted_raw.split(",") if x.strip()}
        if trusted and first_buyer not in trusted:
            return None
        age_ms = int(features.get("age_ms") or 0)
        if age_ms < env_int("PGG2_WHALE_SPARK_MIN_AGE_MS", 7500):
            return None
        if age_ms > env_int("PGG2_WHALE_SPARK_MAX_AGE_MS", 25000):
            return None
        tape = self.tapes.get(event.mint)
        if not tape or not tape.prices:
            return None
        first_price = float(tape.prices[0][1] or 0.0)
        if first_price <= 0:
            return None
        entry_move = price / first_price
        if entry_move > env_float("PGG2_WHALE_SPARK_MAX_ENTRY_MOVE", 1.35):
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        buy700 = float(s700.get("buy_sol") or 0.0)
        buy1500 = float(s1500.get("buy_sol") or 0.0)
        uniq700 = int(s700.get("unique_buyers") or 0)
        uniq1500 = int(s1500.get("unique_buyers") or 0)
        top1500 = float(s1500.get("top_buy_share") or 1.0)
        sell1500 = float(s1500.get("sell_sol") or 0.0)
        sell_ratio = sell1500 / max(buy1500, 0.001)
        vsol = float(features.get("vsol_sol") or 0.0)
        if buy700 < env_float("PGG2_WHALE_SPARK_MIN_BUY700", 4.0):
            return None
        if buy1500 < env_float("PGG2_WHALE_SPARK_MIN_BUY1500", 8.0):
            return None
        if uniq700 < env_int("PGG2_WHALE_SPARK_MIN_UNIQ700", 3):
            return None
        if uniq1500 < env_int("PGG2_WHALE_SPARK_MIN_UNIQ1500", 5):
            return None
        if top1500 > env_float("PGG2_WHALE_SPARK_MAX_TOP1500", 0.38):
            return None
        if sell_ratio > env_float("PGG2_WHALE_SPARK_MAX_SELL_RATIO1500", 0.12):
            return None
        if features["last_buy_age_ms"] > env_int("PGG2_WHALE_SPARK_MAX_LAST_BUY_MS", 450):
            return None
        if vsol < env_float("PGG2_WHALE_SPARK_MIN_VSOL", 35.0):
            return None
        scout = min(self.config.max_position_sol, env_float("PGG2_WHALE_SPARK_SOL", self.config.scout_sol))
        reason = (
            f"whale_spark first={first_buy_sol:.2f} age={age_ms}ms "
            f"b1500={buy1500:.2f}/{uniq1500} top={top1500:.2f} "
            f"move={entry_move:.2f}x vsol={vsol:.2f}"
        )
        spark_features = self.slim_features(features)
        spark_features.update(
            {
                "whale_spark_first_buyer": first_buyer,
                "whale_spark_first_buy_sol": first_buy_sol,
                "whale_spark_entry_move": entry_move,
                "entry_size_reason": "whale_spark",
                "entry_probe_sol": scout,
            }
        )
        return StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="whale_spark",
            reason=reason,
            score=float(features.get("score") or 0.0),
            scout_sol=scout,
            target_sol=scout,
            price=price,
            needs_curve_fill=False,
            features=spark_features,
        )

    def early_ignition_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        if not env_bool("PGG2_EARLY_IGNITION_ENABLED", True):
            return None
        if not event.is_buy or event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        if not features.get("wave_armed") or not features.get("has_curve") or features.get("complete"):
            return None
        price = float(features.get("price") or 0.0)
        age_ms = int(features.get("age_ms") or 0)
        arm_age_ms = int(features.get("wave_arm_age_ms") or 0)
        if arm_age_ms < env_int("PGG2_EARLY_MIN_ARM_AGE_MS", 500):
            return None
        if arm_age_ms > env_int("PGG2_EARLY_MAX_ARM_AGE_MS", 2300):
            return None
        if age_ms > env_int("PGG2_EARLY_MAX_TOKEN_AGE_MS", 4200):
            return None
        arm = self.wave_arms.get(event.mint)
        if not arm or price <= 0 or arm.first_price <= 0:
            return None
        base_move = price / max(arm.first_price, 1e-18)
        if base_move < env_float("PGG2_EARLY_MIN_BASE_MOVE", 1.08):
            return None
        if base_move > env_float("PGG2_EARLY_MAX_BASE_MOVE", 1.80):
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        if s700["buy_sol"] < env_float("PGG2_EARLY_MIN_BUY700_SOL", 2.00):
            return None
        if s700["unique_buyers"] < env_int("PGG2_EARLY_MIN_BUYERS700", 3):
            return None
        if s700["top_buy_share"] > env_float("PGG2_EARLY_MAX_TOP700", 0.70):
            return None
        if features["slot_buyers"] < env_int("PGG2_EARLY_MIN_SLOT_BUYERS", 3):
            return None
        if features["slot_buy_sol"] < env_float("PGG2_EARLY_MIN_SLOT_SOL", 1.0):
            return None
        if features["slot_top_share"] > env_float("PGG2_EARLY_MAX_SLOT_TOP", 0.65):
            return None
        if features["move700"] < env_float("PGG2_EARLY_MIN_MOVE700", 1.035):
            return None
        if features["last_buy_age_ms"] > env_int("PGG2_EARLY_MAX_LAST_BUY_AGE_MS", 160):
            return None
        if features["last_sell_age_ms"] < env_int("PGG2_EARLY_MIN_LAST_SELL_AGE_MS", 850):
            return None
        if s700["sell_sol"] > max(0.004, s700["buy_sol"] * env_float("PGG2_EARLY_MAX_SELL700_RATIO", 0.025)):
            return None
        if s1500["sell_sol"] > max(0.006, s1500["buy_sol"] * env_float("PGG2_EARLY_MAX_SELL1500_RATIO", 0.045)):
            return None
        fresh700 = self.fresh_wave_stats(event.mint, int(features["ts_ms"]), arm, env_int("PGG2_EARLY_FRESH_WINDOW_MS", 700))
        if fresh700["fresh_buy_sol"] < env_float("PGG2_EARLY_MIN_FRESH_BUY_SOL", 0.40):
            return None
        if fresh700["fresh_unique"] < env_int("PGG2_EARLY_MIN_FRESH_BUYERS", 1):
            return None
        if fresh700["fresh_top_share"] > env_float("PGG2_EARLY_MAX_FRESH_TOP", 0.82):
            return None
        score = 120.0
        score += min(35.0, s700["buy_sol"] * 7.0)
        score += min(25.0, s700["unique_buyers"] * 5.0)
        score += max(0.0, base_move - 1.0) * 160.0
        score += max(0.0, features["move700"] - 1.0) * 280.0
        score -= max(0.0, s700["top_buy_share"] - 0.50) * 50.0
        scout = min(self.config.max_position_sol, max(0.0005, env_float("PGG2_EARLY_SCOUT_SOL", self.config.scout_sol)))
        target = min(self.config.max_position_sol, max(scout, env_float("PGG2_EARLY_TARGET_SOL", self.config.max_position_sol)))
        features["entry_size_reason"] = "early_ignition_probe"
        features["entry_probe_sol"] = scout
        reason = (
            f"early_ignition arm={arm_age_ms}ms base={base_move:.2f}x "
            f"b700={s700['buy_sol']:.2f}/{s700['unique_buyers']} "
            f"fresh={fresh700['fresh_buy_sol']:.2f}/{fresh700['fresh_unique']} "
            f"top={s700['top_buy_share']:.2f}"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="early_ignition",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update(
            {
                "arm_first_price": arm.first_price,
                "arm_peak_price": arm.peak_price,
                "base_move": base_move,
                "fresh700": fresh700,
                "entry_size_reason": "early_ignition_probe",
                "entry_probe_sol": scout,
            }
        )
        return plan

    def late_ignition_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 staggered ignition lane for mints the first-burst arm never saw."""
        if not env_bool("PGG2_LATE_IGNITION_ENABLED", False):
            return None
        if not event.is_buy or features.get("complete"):
            return None
        if features.get("wave_armed"):
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        age_ms = int(features.get("age_ms") or 0)
        if age_ms < env_int("PGG2_LATE_MIN_TOKEN_AGE_MS", 1800):
            return None
        if age_ms > env_int("PGG2_LATE_MAX_TOKEN_AGE_MS", 60000):
            return None
        if features["last_buy_age_ms"] > env_int("PGG2_LATE_MAX_LAST_BUY_AGE_MS", 180):
            return None

        s700 = features["s700"]
        s1500 = features["s1500"]
        if s1500["unique_buyers"] < env_int("PGG2_LATE_MIN_BUYERS_1500", 4):
            return None
        if s1500["buy_sol"] < env_float("PGG2_LATE_MIN_BUY1500_SOL", 0.45):
            return None
        if s1500["buy_sol"] > env_float("PGG2_LATE_MAX_BUY1500_SOL", 5.50):
            return None
        if s700["buy_sol"] < env_float("PGG2_LATE_MIN_BUY700_SOL", 0.18):
            return None
        if s1500["top_buy_share"] > env_float("PGG2_LATE_MAX_TOP1500", 0.72):
            return None
        if s1500["buyer_hhi"] > env_float("PGG2_LATE_MAX_HHI1500", 0.40):
            return None
        if s1500["sell_sol"] > max(0.020, s1500["buy_sol"] * env_float("PGG2_LATE_MAX_SELL1500_RATIO", 0.30)):
            return None
        if s700["sell_sol"] > max(0.012, s700["buy_sol"] * env_float("PGG2_LATE_MAX_SELL700_RATIO", 0.22)):
            return None
        if features["move700"] < env_float("PGG2_LATE_MIN_MOVE700", 1.06):
            return None
        if features["move1500"] < env_float("PGG2_LATE_MIN_MOVE1500", 1.10):
            return None
        if features["off_peak"] < env_float("PGG2_LATE_MIN_OFF_PEAK", 0.90):
            return None

        scout = min(
            self.config.max_position_sol,
            max(0.0005, env_float("PGG2_LATE_SCOUT_SOL", max(0.0005, self.config.scout_sol * 0.50))),
        )
        target = min(
            self.config.max_position_sol,
            max(scout, env_float("PGG2_LATE_TARGET_SOL", self.config.max_position_sol)),
        )
        score = 78.0
        score += min(30.0, s1500["buy_sol"] * 7.0)
        score += min(28.0, s1500["unique_buyers"] * 5.0)
        score += max(0.0, features["move700"] - 1.0) * 360.0
        score += max(0.0, features["move1500"] - 1.0) * 160.0
        score -= max(0.0, s1500["top_buy_share"] - 0.55) * 45.0
        score -= max(0.0, s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) - 0.16) * 55.0
        reason = (
            f"late_ignition age={age_ms} b1500={s1500['buy_sol']:.3f}/{s1500['unique_buyers']} "
            f"m700={features['move700']:.3f} m1500={features['move1500']:.3f} "
            f"sell1500={s1500['sell_sol'] / max(s1500['buy_sol'], 0.001):.2f}"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="late_ignition",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update({"entry_size_reason": "late_ignition_probe", "entry_probe_sol": scout})
        return plan

    def breadth_ignition_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """PGG2 breadth ignition lane.

        Fast-validator rule family:
        age <= 6s, buy1500 0.45..25 SOL, uniq1500 >= 6, sells <= 10%.
        This captures raw moonshot breadth that the first-burst arm misses. It is
        probe-only by default; scale-up is separately gated off unless enabled.
        """
        if not env_bool("PGG2_BREADTH_IGNITION_ENABLED", True):
            return None
        if not event.is_buy or features.get("complete"):
            return None
        if features.get("wave_armed"):
            return None
        if event.mint in self.breadth_ignition_seen:
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            return None
        price = float(features.get("price") or 0.0)
        if price <= 0:
            return None
        age_ms = int(features.get("age_ms") or 0)
        if age_ms < env_int("PGG2_BREADTH_MIN_TOKEN_AGE_MS", 600):
            return None
        if age_ms > env_int("PGG2_BREADTH_MAX_TOKEN_AGE_MS", 6000):
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        if s1500["buy_sol"] < env_float("PGG2_BREADTH_MIN_BUY1500_SOL", 0.45):
            return None
        if features["first_buy_sol"] < env_float("PGG2_BREADTH_MIN_FIRST_BUY_SOL", 0.10):
            return None
        if s1500["buy_sol"] > env_float("PGG2_BREADTH_MAX_BUY1500_SOL", 3.50):
            return None
        if s700["buy_sol"] < env_float("PGG2_BREADTH_MIN_BUY700_SOL", 0.10):
            return None
        if s1500["unique_buyers"] < env_int("PGG2_BREADTH_MIN_BUYERS1500", 6):
            return None
        if s1500["top_buy_share"] > env_float("PGG2_BREADTH_MAX_TOP1500", 0.32):
            return None
        if features["slot_buyers"] < env_int("PGG2_BREADTH_MIN_SLOT_BUYERS", 3):
            return None
        if s1500["buyer_hhi"] > env_float("PGG2_BREADTH_MAX_HHI1500", 1.01):
            return None
        if s1500["sell_sol"] > max(0.010, s1500["buy_sol"] * env_float("PGG2_BREADTH_MAX_SELL1500_RATIO", 0.10)):
            return None
        if features["move700"] < env_float("PGG2_BREADTH_MIN_MOVE700", 1.0):
            return None
        if features["move1500"] < env_float("PGG2_BREADTH_MIN_MOVE1500", 1.0):
            return None

        scout = min(
            self.config.max_position_sol,
            max(0.0005, env_float("PGG2_BREADTH_SCOUT_SOL", max(0.0005, self.config.scout_sol * 0.50))),
        )
        target = min(
            self.config.max_position_sol,
            max(scout, env_float("PGG2_BREADTH_TARGET_SOL", self.config.max_position_sol)),
        )
        score = 88.0
        score += min(35.0, s1500["buy_sol"] * 5.0)
        score += min(35.0, s1500["unique_buyers"] * 4.0)
        score += max(0.0, features["move700"] - 1.0) * 220.0
        score -= max(0.0, s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) - 0.05) * 80.0
        reason = (
            f"breadth_ignition age={age_ms} b1500={s1500['buy_sol']:.3f}/{s1500['unique_buyers']} "
            f"top={s1500['top_buy_share']:.2f} hhi={s1500['buyer_hhi']:.2f} "
            f"sell1500={s1500['sell_sol'] / max(s1500['buy_sol'], 0.001):.2f}"
        )
        self.breadth_ignition_seen.add(event.mint)
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="breadth_ignition",
            reason=reason,
            score=score,
            scout_sol=scout,
            target_sol=target,
            price=price,
            needs_curve_fill=False,
            features=self.slim_features(features),
        )
        plan.features.update({"entry_size_reason": "breadth_ignition_probe", "entry_probe_sol": scout})
        return plan

    def _loss_dna_block_reason(self, features: dict[str, Any]) -> Optional[str]:
        """Phase 22 2026-05-08: universal pre-strike block.

        Derived from Phase 21 dissection of 22 losses vs 12 wins:
        - top_share1500 > 0.50 was present in 8 losses, 0 wins (cleanest filter)
        - move250 < 0.92 = price already dumping in last 250ms (block chase)
        - sell_ratio_1500 > 0.30 = sells dominating buys (toxic flow)
        - uniq700 < 2 + top700 = 1.0 = single-buyer pump artifact

        Phase 27 2026-05-09 additions from 19:00-19:23 UTC dissection (W/L=2/13):
        - move700 > 1.30 was in losses (chase), wins had move700 <= 1.20
        - move1500 > 1.25 was in losses (chase deep), wins had <= 1.15

        These run AHEAD of every lane probe so we don't even consider entries
        with terminal-loss features.
        """
        # Whale concentration in 1500ms window — single most predictive loss filter
        max_top1500 = env_float("PGG2_BLOCK_MAX_TOP1500", 0.50)
        top_share1500 = float(features.get("top_share1500") or 0.0)
        if top_share1500 > max_top1500:
            return f"top1500={top_share1500:.2f}>{max_top1500:.2f}"
        # Phase 27: chase late filter — high recent move = price already at peak
        max_move700 = env_float("PGG2_BLOCK_MAX_MOVE700", 1.30)
        move700 = float(features.get("move700") or 1.0)
        if max_move700 > 0 and move700 > max_move700:
            return f"chase_move700={move700:.2f}>{max_move700:.2f}"
        # Phase 28: whale-led buy filter — winners had ~2 SOL/buyer, losers ~1 SOL/buyer.
        # Retail-cluster buys are floors that collapse; whale-led buys carry the move.
        min_buy_per_buyer = env_float("PGG2_BLOCK_MIN_BUY_PER_BUYER_700", 1.5)
        buy700 = float(features.get("buy700") or 0.0)
        uniq700 = int(features.get("uniq700") or 0)
        if min_buy_per_buyer > 0 and uniq700 >= 1:
            avg_buy = buy700 / max(uniq700, 1)
            if avg_buy < min_buy_per_buyer:
                return f"retail_cluster avg_buy_700={avg_buy:.2f}<{min_buy_per_buyer:.2f}"
        # Phase 28: fresh-mint age cap — winners median 3.5s, losers 4.3s mint age
        max_age_ms = env_int("PGG2_BLOCK_MAX_AGE_MS", 6000)
        age_ms = int(features.get("age_ms") or 0)
        if max_age_ms > 0 and age_ms > max_age_ms:
            return f"stale_mint age={age_ms}ms>{max_age_ms}ms"
        max_move1500 = env_float("PGG2_BLOCK_MAX_MOVE1500", 1.25)
        move1500 = float(features.get("move1500") or 1.0)
        if max_move1500 > 0 and move1500 > max_move1500:
            return f"chase_move1500={move1500:.2f}>{max_move1500:.2f}"
        # Phase 27: high whale concentration in 700ms (single buyer pumping)
        max_top700 = env_float("PGG2_BLOCK_MAX_TOP700", 0.55)
        top700 = float(features.get("top_share700") or 0.0)
        if max_top700 > 0 and top700 > max_top700:
            return f"top700={top700:.2f}>{max_top700:.2f}"
        # Already dumping in last 250ms (caught the top)
        min_move250 = env_float("PGG2_BLOCK_MIN_MOVE250", 0.92)
        move250 = float(features.get("move250") or 1.0)
        if move250 > 0 and move250 < min_move250:
            return f"move250={move250:.2f}<{min_move250:.2f}"
        # Sells dominating last 1500ms
        max_sell_ratio_1500 = env_float("PGG2_BLOCK_MAX_SELL_RATIO_1500", 0.50)
        s1500 = features.get("s1500") or {}
        buy_1500 = float(s1500.get("buy_sol") or features.get("buy1500") or 0.0)
        sell_1500 = float(s1500.get("sell_sol") or features.get("sell1500") or 0.0)
        if buy_1500 > 0:
            sr = sell_1500 / buy_1500
            if sr > max_sell_ratio_1500:
                return f"sell_ratio1500={sr:.2f}>{max_sell_ratio_1500:.2f}"
        # Single-buyer pump artifact in last 700ms (J6WHcSTC -$5 loss had this)
        max_top700_for_single = env_float("PGG2_BLOCK_TOP700_SINGLE_BUYER", 0.95)
        uniq700 = int(features.get("uniq700") or 0)
        top700 = float(features.get("top_share700") or 0.0)
        if uniq700 <= 1 and top700 >= max_top700_for_single:
            return f"single_buyer top700={top700:.2f} uniq700={uniq700}"
        return None

    async def maybe_plan_strike(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        ts_ms = event.ts_ms
        features = self.feature_snapshot(event.mint, ts_ms)
        if not features:
            return
        self.maybe_arm_first_burst(event, features)
        features = self.feature_snapshot(event.mint, ts_ms) or features
        # Phase 22 2026-05-08: universal entry blocker derived from Phase 21
        # loss dissection. These features were 100% absent in winners and
        # present in 8+ losses each. Block before any lane probes.
        if env_bool("PGG2_LOSS_DNA_BLOCK_ENABLED", True):
            block_reason = self._loss_dna_block_reason(features)
            if block_reason:
                if env_bool("PGG2_LOSS_DNA_BLOCK_LOG", False):
                    log(f"PGG2-LOSS-DNA-BLOCK mint={event.mint[:8]} {block_reason}")
                return
        spark3_plan = self.spark3_arm_ready(event, features)
        if spark3_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": spark3_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": spark3_plan.lane,
                    "reason": spark3_plan.reason,
                    "score": spark3_plan.score,
                    "scout_sol": spark3_plan.scout_sol,
                    "target_sol": spark3_plan.target_sol,
                    "needs_curve_fill": spark3_plan.needs_curve_fill,
                    "features": spark3_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(spark3_plan, float(features.get("price") or 0.0))
            self.spark3_arm_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": spark3_plan.lane, "features": self.slim_features(features)})
            return
        stealth_plan = self.stealth_arm_ready(event, features)
        if stealth_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": stealth_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": stealth_plan.lane,
                    "reason": stealth_plan.reason,
                    "score": stealth_plan.score,
                    "scout_sol": stealth_plan.scout_sol,
                    "target_sol": stealth_plan.target_sol,
                    "needs_curve_fill": stealth_plan.needs_curve_fill,
                    "features": stealth_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(stealth_plan, float(features.get("price") or 0.0))
            self.stealth_arm_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": stealth_plan.lane, "features": self.slim_features(features)})
            return
        spark3_breakout_plan = self.spark3_breakout_ready(event, features)
        if spark3_breakout_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": spark3_breakout_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": spark3_breakout_plan.lane,
                    "reason": spark3_breakout_plan.reason,
                    "score": spark3_breakout_plan.score,
                    "scout_sol": spark3_breakout_plan.scout_sol,
                    "target_sol": spark3_breakout_plan.target_sol,
                    "needs_curve_fill": spark3_breakout_plan.needs_curve_fill,
                    "features": spark3_breakout_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(spark3_breakout_plan, float(features.get("price") or 0.0))
            self.spark3_breakout_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": spark3_breakout_plan.lane, "features": self.slim_features(features)})
            return
        preprice_plan = self.preprice_reveal_ready(event, features)
        if preprice_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": preprice_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": preprice_plan.lane,
                    "reason": preprice_plan.reason,
                    "score": preprice_plan.score,
                    "scout_sol": preprice_plan.scout_sol,
                    "target_sol": preprice_plan.target_sol,
                    "needs_curve_fill": preprice_plan.needs_curve_fill,
                    "features": preprice_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(preprice_plan, float(features.get("price") or 0.0))
            self.preprice_reveal_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": preprice_plan.lane, "features": self.slim_features(features)})
            return
        # Phase 21 2026-05-08: BOUNCE_BUY lane — fires when a tape shows
        # a recent local peak followed by a >=30% dump within 60s. Buys the
        # panic flush, targets a +5% bounce. Structurally inverse to the
        # engagement-chase logic that lost 0/5 this session.
        bounce_plan = self.bounce_buy_ready(event, features)
        if bounce_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": bounce_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": bounce_plan.lane,
                    "reason": bounce_plan.reason,
                    "score": bounce_plan.score,
                    "scout_sol": bounce_plan.scout_sol,
                    "target_sol": bounce_plan.target_sol,
                    "needs_curve_fill": bounce_plan.needs_curve_fill,
                    "features": bounce_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(bounce_plan, float(features.get("price") or 0.0))
            self.bounce_buy_seen.add(event.mint)
            self.bounce_buy_ts[event.mint] = ts_ms
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": bounce_plan.lane, "features": self.slim_features(features)})
                log(
                    f"PGG2-BOUNCE-OPEN mint={event.mint[:8]} price={bounce_plan.price:.6e} "
                    f"peak={features.get('bounce_peak_price', 0):.6e} drop={features.get('bounce_drop_pct', 0):.1%} "
                    f"age_since_peak={features.get('bounce_age_since_peak_sec', 0):.0f}s"
                )
            return
        # Phase 18 2026-05-08: ENGAGEMENT-DRIVEN lane (highest priority for engaged mints)
        engagement_plan = self.engagement_driven_ready(event, features)
        if engagement_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": engagement_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": engagement_plan.lane,
                    "reason": engagement_plan.reason,
                    "score": engagement_plan.score,
                    "scout_sol": engagement_plan.scout_sol,
                    "target_sol": engagement_plan.target_sol,
                    "needs_curve_fill": engagement_plan.needs_curve_fill,
                    "features": engagement_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(engagement_plan, float(features.get("price") or 0.0))
            self.engagement_driven_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": engagement_plan.lane, "features": self.slim_features(features)})
            return
        snap_plan = self.priced_snap_ready(event, features)
        if snap_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": snap_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": snap_plan.lane,
                    "reason": snap_plan.reason,
                    "score": snap_plan.score,
                    "scout_sol": snap_plan.scout_sol,
                    "target_sol": snap_plan.target_sol,
                    "needs_curve_fill": snap_plan.needs_curve_fill,
                    "features": snap_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(snap_plan, float(features.get("price") or 0.0))
            self.priced_snap_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": snap_plan.lane, "features": self.slim_features(features)})
            return
        breakout_plan = self.priced_breakout_ready(event, features)
        if breakout_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": breakout_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": breakout_plan.lane,
                    "reason": breakout_plan.reason,
                    "score": breakout_plan.score,
                    "scout_sol": breakout_plan.scout_sol,
                    "target_sol": breakout_plan.target_sol,
                    "needs_curve_fill": breakout_plan.needs_curve_fill,
                    "features": breakout_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(breakout_plan, float(features.get("price") or 0.0))
            self.priced_breakout_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": breakout_plan.lane, "features": self.slim_features(features)})
            return
        late_swarm_plan = self.late_swarm_ready(event, features)
        if late_swarm_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": late_swarm_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": late_swarm_plan.lane,
                    "reason": late_swarm_plan.reason,
                    "score": late_swarm_plan.score,
                    "scout_sol": late_swarm_plan.scout_sol,
                    "target_sol": late_swarm_plan.target_sol,
                    "needs_curve_fill": late_swarm_plan.needs_curve_fill,
                    "features": late_swarm_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(late_swarm_plan, float(features.get("price") or 0.0))
            self.late_swarm_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": late_swarm_plan.lane, "features": self.slim_features(features)})
            return
        birth_plan = self.birth_fanout_ready(event, features)
        if birth_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": birth_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": birth_plan.lane,
                    "reason": birth_plan.reason,
                    "score": birth_plan.score,
                    "scout_sol": birth_plan.scout_sol,
                    "target_sol": birth_plan.target_sol,
                    "needs_curve_fill": birth_plan.needs_curve_fill,
                    "features": birth_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(birth_plan, float(features.get("price") or 0.0))
            self.birth_fanout_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": birth_plan.lane, "features": self.slim_features(features)})
            return
        curve_lag_plan = self.curve_lag_reveal_ready(event, features)
        if curve_lag_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": curve_lag_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": curve_lag_plan.lane,
                    "reason": curve_lag_plan.reason,
                    "score": curve_lag_plan.score,
                    "scout_sol": curve_lag_plan.scout_sol,
                    "target_sol": curve_lag_plan.target_sol,
                    "needs_curve_fill": curve_lag_plan.needs_curve_fill,
                    "features": curve_lag_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(curve_lag_plan, float(features.get("price") or 0.0))
            self.curve_lag_reveal_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": curve_lag_plan.lane, "features": self.slim_features(features)})
            return
        curve_arm_plan = self.curve_arm_scout_ready(event, features)
        if curve_arm_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": curve_arm_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": curve_arm_plan.lane,
                    "reason": curve_arm_plan.reason,
                    "score": curve_arm_plan.score,
                    "scout_sol": curve_arm_plan.scout_sol,
                    "target_sol": curve_arm_plan.target_sol,
                    "needs_curve_fill": curve_arm_plan.needs_curve_fill,
                    "features": curve_arm_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(curve_arm_plan, float(features.get("price") or 0.0))
            self.curve_arm_scout_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": curve_arm_plan.lane, "features": self.slim_features(features)})
            return
        whale_plan = self.whale_spark_ready(event, features)
        if whale_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": whale_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": whale_plan.lane,
                    "reason": whale_plan.reason,
                    "score": whale_plan.score,
                    "scout_sol": whale_plan.scout_sol,
                    "target_sol": whale_plan.target_sol,
                    "needs_curve_fill": whale_plan.needs_curve_fill,
                    "features": whale_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(whale_plan, float(features.get("price") or 0.0))
            self.whale_spark_seen.add(event.mint)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": whale_plan.lane, "features": self.slim_features(features)})
            return
        raw_plan = self.raw_momentum_ready(event, features)
        if raw_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": raw_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": raw_plan.lane,
                    "reason": raw_plan.reason,
                    "score": raw_plan.score,
                    "scout_sol": raw_plan.scout_sol,
                    "target_sol": raw_plan.target_sol,
                    "needs_curve_fill": raw_plan.needs_curve_fill,
                    "features": raw_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(raw_plan, float(features.get("price") or 0.0))
            self.raw_momentum_seen.add(event.mint)
            self.raw_momentum_arms.pop(event.mint, None)
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": raw_plan.lane, "features": self.slim_features(features)})
            return
        early_plan = self.early_ignition_ready(event, features)
        if early_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": early_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": early_plan.lane,
                    "reason": early_plan.reason,
                    "score": early_plan.score,
                    "scout_sol": early_plan.scout_sol,
                    "target_sol": early_plan.target_sol,
                    "needs_curve_fill": early_plan.needs_curve_fill,
                    "features": early_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(early_plan, float(features.get("price") or 0.0))
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": early_plan.lane, "features": self.slim_features(features)})
            return
        late_plan = self.late_ignition_ready(event, features)
        if late_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": late_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": late_plan.lane,
                    "reason": late_plan.reason,
                    "score": late_plan.score,
                    "scout_sol": late_plan.scout_sol,
                    "target_sol": late_plan.target_sol,
                    "needs_curve_fill": late_plan.needs_curve_fill,
                    "features": late_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(late_plan, float(features.get("price") or 0.0))
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": late_plan.lane, "features": self.slim_features(features)})
            return
        breadth_plan = self.breadth_ignition_ready(event, features)
        if breadth_plan:
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": breadth_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": breadth_plan.lane,
                    "reason": breadth_plan.reason,
                    "score": breadth_plan.score,
                    "scout_sol": breadth_plan.scout_sol,
                    "target_sol": breadth_plan.target_sol,
                    "needs_curve_fill": breadth_plan.needs_curve_fill,
                    "features": breadth_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(breadth_plan, float(features.get("price") or 0.0))
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": breadth_plan.lane, "features": self.slim_features(features)})
            return
        plan, why = self.second_wave_ready(event, features)
        if not plan:
            if why in {
                "arm_expired",
                "initial_cluster_sold",
                "exhausted_reclaim_no_headroom",
                "prior_profit_reentry_block",
                "early_vertical_vacuum",
                "weak_top_heavy_vacuum",
                "overextended_clean_reclaim_vacuum",
                "late_whale_drag",
            }:
                self.logger.decision("wave_disarm", event.mint, {"reason": why, "features": self.slim_features(features)})
            return
        ok, reason = self.broker.can_strike(event.mint, ts_ms)
        if not ok:
            self.logger.decision(
                "strike_skipped",
                event.mint,
                {"reason": reason, "lane": plan.lane, "features": self.slim_features(features)},
            )
            return
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
                "features": plan.features,
            },
        )
        pos = self.broker.queue_or_fill(plan, float(features.get("price") or 0.0))
        if pos:
            self.init_position_follow(pos, trusted=True, entry_features=features)
            self.logger.decision("open", event.mint, {"lane": plan.lane, "features": self.slim_features(features)})

    async def manage_existing(self, mint: str, ts_ms: int) -> None:
        if mint not in self.broker.positions and mint not in self.broker.pending:
            return
        await super().manage_existing(mint, ts_ms)

    def close_position(self, mint: str, ts_ms: int, price: float, reason: str, features: dict[str, Any], killed: bool) -> None:
        before_pnl = self.broker.stats.realized_pnl_sol
        pnl = self.broker.close(mint, ts_ms, price, reason, killed)
        if pnl is None:
            # Live direct execution can reject or fail an attempted close while
            # the position remains open. Do not emit a fake close decision.
            return
        self.logger.decision(
            "close",
            mint,
            {"reason": reason, "pnl_sol": pnl, "killed": killed, "features": self.slim_features(features)},
        )
        if mint in self.broker.positions:
            # Live/quote mode can reject a paper sell signal when the executable
            # Raptor quote is not good enough yet. Keep the follow state intact
            # so the next tick can still manage the open position correctly.
            return
        pnl = self.broker.stats.realized_pnl_sol - before_pnl
        if pnl > env_float("PIGGY_PROFIT_REENTRY_MIN_PNL_SOL", 0.0):
            self.profitable_closes[mint] = {
                "ts_ms": float(ts_ms),
                "pnl_sol": pnl,
                "peak_mult": float(self.broker.stats.best_mult),
            }
        # Phase 2A 2026-05-08: consecutive-loss circuit breaker. Tracks losing
        # streaks and triggers a session pause to break out of bad-tape windows.
        # Phase 1 analysis showed run 191401 had 12-15 consecutive losses streaks
        # (e.g. 20:09-20:22 lost 0.089 SOL in 12 trades) — a circuit breaker
        # would have caught these and saved several SOL.
        if pnl < 0:
            self.consecutive_losses += 1
            # Phase 3 2026-05-08: any loss resets the win streak. A scratch (pnl
            # ~0) counts as neither — only strict positive PnL increments wins.
            self.consecutive_wins = 0
            max_losses = env_int("PGG2_CIRCUIT_BREAKER_LOSSES", 0)
            pause_sec = env_int("PGG2_CIRCUIT_BREAKER_PAUSE_SEC", 0)
            if max_losses > 0 and pause_sec > 0 and self.consecutive_losses >= max_losses:
                self.circuit_breaker_until_ts = ts_ms + pause_sec * 1000
                log(
                    f"PGG2-CIRCUIT-BREAKER tripped after {self.consecutive_losses} "
                    f"consecutive losses, pausing for {pause_sec}s"
                )
        elif pnl > 0:
            if self.consecutive_losses > 0:
                self.consecutive_losses = 0
            self.consecutive_wins += 1
        # pnl == 0 (true scratch): leave both streaks unchanged. This keeps the
        # anti-martingale state stable through pure round-trip break-evens that
        # quote_profit_bank/min-out can occasionally produce.
        self.position_follow.pop(mint, None)

    @staticmethod
    def pending_fill_ready(pending: Any, features: dict[str, Any]) -> tuple[bool, str]:
        if not pending:
            return False, "missing_pending"
        if features["last_buy_age_ms"] > 600:
            return False, "cluster_stalled"
        if features["s700"]["sell_sol"] > max(0.004, features["s700"]["buy_sol"] * 0.06):
            return False, "pre_fill_sell"
        if features["off_peak"] < 0.93:
            return False, "pre_fill_off_peak"
        if features["s700"]["unique_buyers"] >= 3 and features["s700"]["buy_sol"] >= 0.90:
            return True, "cluster_live"
        return False, "waiting_cluster_live"

    async def manage_position(self, pos: Any, ts_ms: int, price: float, features: dict[str, Any]) -> None:
        mint = pos.mint
        prev_peak = pos.peak_mult
        mult = pos.update(price)
        self.broker.stats.best_mult = max(self.broker.stats.best_mult, pos.peak_mult)

        # ════════════════════════════════════════════════════════════════════
        # PHASE 25 2026-05-08 — UNIVERSAL REAL-PRICE EXIT GOVERNOR
        # ════════════════════════════════════════════════════════════════════
        # Pre-empts every other exit (OG kill_*, moonshot_trail, scale_out,
        # min_hold_panic, etc). Uses ONLY the on-chain real price, not the
        # broker's simulated sell-back quote. Bounded losses, ride real moves.
        if (env_bool("PGG2_PHASE25_UNIVERSAL_EXIT_ENABLED", True)
                and pos.lane not in {"engagement_driven", "bounce_buy"}):
            stop_mult = env_float("PGG2_PHASE25_STOP_MULT", 0.95)        # -5% real
            cat_mult = env_float("PGG2_PHASE25_CATASTROPHIC_MULT", 0.60)  # -40% rug guard
            trail_arm = env_float("PGG2_PHASE25_TRAIL_ARM_PEAK", 1.08)    # arm trail at +8%
            trail_drop = env_float("PGG2_PHASE25_TRAIL_DROP", 0.94)       # 6% drop from peak
            min_hold_sec = env_float("PGG2_PHASE25_MIN_HOLD_SEC", 4.0)    # let entry settle
            timebox_sec = env_float("PGG2_PHASE25_TIMEBOX_SEC", 60.0)
            timebox_min_mult = env_float("PGG2_PHASE25_TIMEBOX_MIN_MULT", 1.02)
            age = pos.age_sec(ts_ms)
            real_peak = pos.peak_mult

            # Catastrophic (real -40%) — fires immediately
            if mult <= cat_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"phase25_catastrophic mult={mult:.3f}",
                    features, killed=True,
                )
                return

            # Min hold floor — let position settle past entry slippage tick
            if age < min_hold_sec:
                return  # don't fire any other exits either

            # Hard stop at -5% real
            if mult <= stop_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"phase25_stop mult={mult:.3f} age={age:.1f}s",
                    features, killed=True,
                )
                return

            # Trailing peak lock (only after peak armed at +8%)
            if real_peak >= trail_arm:
                trail_floor = real_peak * trail_drop
                if mult <= trail_floor:
                    self.close_position(
                        mint, ts_ms, price,
                        f"phase25_trail peak={real_peak:.3f} mult={mult:.3f}",
                        features, killed=False,
                    )
                    return

            # Timebox — at 60s, if no meaningful pop, accept small loss
            if age >= timebox_sec and mult < timebox_min_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"phase25_timebox age={age:.0f}s mult={mult:.3f}",
                    features, killed=(mult < 1.0),
                )
                return

            # Otherwise HOLD — block all other exit logic
            return
        # ════════════════════════════════════════════════════════════════════


        # Phase 8 2026-05-08: track when peak last advanced — used for stall exit.
        if pos.peak_mult > prev_peak:
            pos.peak_advance_ts = ts_ms
        elif pos.peak_advance_ts == 0:
            # Initialize at first call (positions opened before this code lacked it)
            pos.peak_advance_ts = ts_ms
        self.add_follow_features(pos, features)

        # Phase 20 2026-05-08: SURVIVOR TRADER tight exits for engagement_driven lane.
        # These positions are ALREADY past the 91% rug zone (mint age > 60s + active
        # livestream/community). Don't ride for moonshots — take small profits fast,
        # cut losses tight. Net: low-variance positive expectancy.
        # Phase 21 2026-05-08: bounce_buy exits — quick scalp on a +5% bounce.
        if pos.lane == "bounce_buy":
            profit_mult = env_float("PGG2_BOUNCE_PROFIT_BANK_MULT", 1.05)
            stop_mult = env_float("PGG2_BOUNCE_STOPLOSS_MULT", 0.93)
            timebox_sec = env_float("PGG2_BOUNCE_TIMEBOX_SEC", 90.0)
            if mult >= profit_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"bounce_profit mult={mult:.3f}",
                    features, killed=False,
                )
                return
            if mult <= stop_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"bounce_stoploss mult={mult:.3f}",
                    features, killed=True,
                )
                return
            if pos.age_sec(ts_ms) >= timebox_sec:
                self.close_position(
                    mint, ts_ms, price,
                    f"bounce_timebox age={pos.age_sec(ts_ms):.0f}s mult={mult:.3f}",
                    features, killed=(mult < 1.0),
                )
                return
            return

        if pos.lane == "engagement_driven":
            # Phase 20F 2026-05-08: take-profit-and-run + no-red voluntary close.
            # Three layers stacked:
            #  1. Partial-bank ladder — sell chunks at +4% / +10% / +20%, ride tail
            #  2. Trailing peak lock — once tail armed, close on 1.5% drop from peak
            #  3. No-red voluntary close — never close negative unless absolute timebox
            tier1_peak = env_float("PGG2_ENGAGEMENT_TIER1_PEAK", 1.04)
            tier1_frac = env_float("PGG2_ENGAGEMENT_TIER1_FRAC", 0.40)
            tier2_peak = env_float("PGG2_ENGAGEMENT_TIER2_PEAK", 1.10)
            tier2_frac = env_float("PGG2_ENGAGEMENT_TIER2_FRAC", 0.50)  # 50% of remaining
            tier3_peak = env_float("PGG2_ENGAGEMENT_TIER3_PEAK", 1.20)
            tier3_frac = env_float("PGG2_ENGAGEMENT_TIER3_FRAC", 0.667)  # 67% of remaining
            trail_arm_peak = env_float("PGG2_ENGAGEMENT_TRAIL_ARM_PEAK", 1.04)
            trail_drop = env_float("PGG2_ENGAGEMENT_TRAIL_DROP", 0.985)
            absolute_timebox_sec = env_float("PGG2_ENGAGEMENT_ABSOLUTE_TIMEBOX_SEC", 300.0)
            # Hard catastrophic floor — only fires below this, nothing above closes red voluntarily
            catastrophic_mult = env_float("PGG2_ENGAGEMENT_CATASTROPHIC_MULT", 0.70)

            # Migration handling — only force close if entry was pre-migration
            entry_complete = bool(pos.entry_features.get("complete", False))
            if features.get("complete") and not entry_complete:
                self.close_position(mint, ts_ms, price, "engagement_migration", features, killed=False)
                return

            # 1. Partial-bank ladder — fires once each, in order
            step = int(getattr(pos, "scale_out_step", 0) or 0)
            if step == 0 and pos.peak_mult >= tier1_peak and mult >= 1.0:
                sold = self.broker.partial(mint, tier1_frac, price,
                                           f"engagement_tier1 peak={pos.peak_mult:.3f}")
                if sold is not None:
                    pos.scale_out_step = 1
                    log(f"PGG2-ENGAGE-TIER1 mint={mint[:8]} bank={tier1_frac:.0%} at peak={pos.peak_mult:.3f}")
            elif step == 1 and pos.peak_mult >= tier2_peak and mult >= 1.0:
                sold = self.broker.partial(mint, tier2_frac, price,
                                           f"engagement_tier2 peak={pos.peak_mult:.3f}")
                if sold is not None:
                    pos.scale_out_step = 2
                    log(f"PGG2-ENGAGE-TIER2 mint={mint[:8]} bank tier2 at peak={pos.peak_mult:.3f}")
            elif step == 2 and pos.peak_mult >= tier3_peak and mult >= 1.0:
                sold = self.broker.partial(mint, tier3_frac, price,
                                           f"engagement_tier3 peak={pos.peak_mult:.3f}")
                if sold is not None:
                    pos.scale_out_step = 3
                    log(f"PGG2-ENGAGE-TIER3 mint={mint[:8]} bank tier3 at peak={pos.peak_mult:.3f}")

            # 2. Trailing peak lock — applies once peak armed; close tail on 1.5% drop
            if pos.peak_mult >= trail_arm_peak:
                trail_floor = pos.peak_mult * trail_drop
                if mult <= trail_floor and mult >= 1.0:
                    self.close_position(
                        mint, ts_ms, price,
                        f"engagement_trail_lock peak={pos.peak_mult:.3f} mult={mult:.3f}",
                        features, killed=False,
                    )
                    return

            # 3. Catastrophic floor — only kick if very deep red (prevents hold-forever zombies)
            if mult <= catastrophic_mult:
                self.close_position(
                    mint, ts_ms, price,
                    f"engagement_catastrophic mult={mult:.3f}",
                    features, killed=True,
                )
                return

            # 4. Absolute timebox safety — force close after long hold regardless of color
            if pos.age_sec(ts_ms) >= absolute_timebox_sec:
                self.close_position(
                    mint, ts_ms, price,
                    f"engagement_absolute_timebox age={pos.age_sec(ts_ms):.0f}s mult={mult:.3f}",
                    features, killed=(mult < 1.0),
                )
                return

            # No-red voluntary close: positions in 0.70-1.00 range hold and wait
            return

        # Phase 6 2026-05-08: MINIMUM-HOLD floor.
        # Phase 5 autopsy on 5 losses: every loss had the same signature —
        # bot entered, price stalled or dipped within 0.7-5.2s, ZERO buyer
        # activity in last 1.5s, bot panicked out, mint then pumped 1.5-5.5x
        # in the next 60s. The killer case: 2QzC17Tx exited via quote_loss_clamp
        # at 0.71s for -$0.30, mint went 5.474x post-close. quote_loss_clamp
        # at sub-second is structurally broken — cost model can't possibly
        # know the trade's fate that early.
        #
        # Fix: for priced_snap and curve_lag_reveal positions, enforce a min
        # hold time. The moonshot LATCH still runs during min-hold (so a fast
        # pump can take over and trail). Only panic-dump or migration closes
        # the position during the hold. All other exits are deferred.
        in_min_hold = False
        if env_bool("PGG2_MIN_HOLD_ENABLED", True) and pos.lane in {"priced_snap", "curve_lag_reveal"}:
            # Phase 7 2026-05-08: TIERED hold based on early signal.
            # Phase 6 autopsy: 2iStU7GGt8b1sY peaked 1.16x at 5.7s, then exited
            # at 17s. The mint's TRUE peak was 7.97x at 276s post-entry. The 12s
            # min-hold was 4 minutes too short. Solution: if position shows life
            # (peak >= 1.10) within first 10s, extend hold to 90s; otherwise
            # cut at standard 12s as a dud. This catches multi-minute pumps
            # that grind up after initial flat consolidation.
            base_hold = env_float("PGG2_MIN_HOLD_SEC", 12.0)
            life_window = env_float("PGG2_MIN_HOLD_LIFE_WINDOW_SEC", 10.0)
            life_peak = env_float("PGG2_MIN_HOLD_LIFE_PEAK", 1.10)
            extended_hold = env_float("PGG2_MIN_HOLD_EXTENDED_SEC", 90.0)
            age_sec_check = pos.age_sec(ts_ms)
            # Decide effective hold: extended if life-shown by life_window, else base.
            effective_hold = base_hold
            if age_sec_check >= life_window:
                # Past the life-window: lock the decision based on whether peak
                # crossed life_peak by now.
                if pos.peak_mult >= life_peak:
                    effective_hold = extended_hold
            else:
                # Still inside the life-window: tentatively extend if already showing life.
                if pos.peak_mult >= life_peak:
                    effective_hold = extended_hold
            if age_sec_check < effective_hold:
                in_min_hold = True
                panic_floor = env_float("PGG2_MIN_HOLD_PANIC_FLOOR", 0.50)
                if mult <= panic_floor:
                    self.close_position(
                        mint, ts_ms, price,
                        f"min_hold_panic mult={mult:.3f} age={age_sec_check:.1f}s",
                        features, killed=False,
                    )
                    return
                if features["complete"]:
                    self.close_position(mint, ts_ms, price, "min_hold_migration", features, killed=False)
                    return
                # During min-hold: latch moonshot mode early if peak crosses,
                # so the rider can take over with its own exits. If not latched,
                # we'll skip all other exit checks below and just hold.
                if env_bool("PGG2_MOONSHOT_RIDE_ENABLED", True) and not pos.moonshot_mode:
                    arm_peak_mh = env_float("PGG2_MOONSHOT_RIDE_PEAK", 1.30)
                    arm_window_mh = env_float("PGG2_MOONSHOT_RIDE_WINDOW_SEC", 30.0)
                    if (
                        pos.peak_mult >= arm_peak_mh
                        and pos.age_sec(ts_ms) <= arm_window_mh
                    ):
                        pos.moonshot_mode = True
                        pos.moonshot_arm_ts = ts_ms
                        pos.moonshot_arm_peak = pos.peak_mult
                        log(
                            f"PGG2-MOONSHOT-RIDE-ARMED mint={mint[:8]} lane={pos.lane} "
                            f"peak={pos.peak_mult:.3f} mult={mult:.3f} age={pos.age_sec(ts_ms):.1f}s (in_min_hold)"
                        )
                # If we latched during min-hold, fall through to the moonshot
                # rider block below — it has its own trail/timeout logic.
                if pos.moonshot_mode:
                    pass  # fall through to moonshot rider
                # Phase 7+ 2026-05-08: ONLY block exits for trades that have
                # actually shown life. Phase 7 LOSS #1 (ZYLhenuGv) had peak < 1.10
                # and dumped to 0.413 within 5s — min-hold blocked hard_break,
                # turning a -$0.50 loss into -$2.00. Hard_break already has
                # peak < 1.18 gate + buyer-grace, so for true duds it fires
                # exactly when it should. Only protect trades showing life.
                elif pos.peak_mult >= life_peak:
                    # Showing life but not yet latched: let it develop. Block
                    # all other exits, hold the position.
                    return
                # else: peak < life_peak — let hard_break / quote_loss_clamp /
                # layered_no_follow do their normal job. They'll catch duds
                # at -8% to -10% loss instead of waiting for -50% panic floor.

        # Phase 3+ 2026-05-08: MOONSHOT-RIDER.
        # Trajectory audit across 22 live runs (483 trades): 97 mints ran 2x+
        # post-bot-exit, 43 ran 3x+, ALL of which the bot left on the table.
        # quote_profit_bank cashes at avg 1.39x peak while mints avg-3x post-close.
        # priced_snap_scout_profit_protect locks 1.29 from 1.43 peak; mints go to 1.72 avg.
        # Even the best winning lane (moonshot_pop_after_sell) exits at 1.4x while mints avg 3x post.
        #
        # Fix: once peak crosses 1.30x within the first 30s, LATCH moonshot mode.
        # In moonshot mode, replace ALL small-profit exits with a single wide trail
        # (close when last_mult <= peak_mult * 0.50). Migration and a hard 90s
        # timeout remain as safety nets. This rides the rare 3x-8x runners.
        #
        # Edge cases:
        # - Whipsaw 1.30 → 0.65: trail catches at 0.65 (50% of peak). One bad case.
        # - Stuck at peak: 90s safety timeout closes flat-ish.
        # - Re-pump 1.30 → 1.50 → 1.80: peak ratchets, trail follows (0.90 floor at 1.80).
        # - Mega rug from 1.30 to 0.10: trail catches at 0.65 first.
        # - Peak < 1.30 forever: moonshot never latches, normal exits apply.
        if env_bool("PGG2_MOONSHOT_RIDE_ENABLED", True):
            arm_peak = env_float("PGG2_MOONSHOT_RIDE_PEAK", 1.30)
            arm_window = env_float("PGG2_MOONSHOT_RIDE_WINDOW_SEC", 30.0)
            if (
                not pos.moonshot_mode
                and pos.peak_mult >= arm_peak
                and pos.age_sec(ts_ms) <= arm_window
            ):
                pos.moonshot_mode = True
                pos.moonshot_arm_ts = ts_ms
                pos.moonshot_arm_peak = pos.peak_mult
                log(
                    f"PGG2-MOONSHOT-RIDE-ARMED mint={mint[:8]} lane={pos.lane} "
                    f"peak={pos.peak_mult:.3f} mult={mult:.3f} age={pos.age_sec(ts_ms):.1f}s"
                )
            if pos.moonshot_mode:
                # Phase 19 2026-05-08: LATCH-TIME SCALE-OUT.
                # Phase 18 data showed: 8 of 9 losses had peak < 1.30, the
                # one catastrophe (FjP7 -$2.52) hit peak 1.15 then cascade-
                # dumped to 0.44 in a single tick. The moonshot rider's
                # trail at 0.90 fires AFTER cascade — too late.
                # Fix: when latch fires (peak >= 1.15), IMMEDIATELY sell
                # 30% to bank profit before any cascade can hit. This
                # converts FjP7-style -$2.52 disasters into ~-$1.50 losses.
                if (
                    pos.scale_out_step == 0
                    and env_bool("PGG2_LATCH_SCALE_OUT_ENABLED", True)
                    and pos.peak_mult >= env_float("PGG2_LATCH_SCALE_OUT_PEAK", 1.15)
                    and pos.peak_mult < env_float("PGG2_SCALE_OUT_TIER1_PEAK", 1.50)
                ):
                    sold = self.broker.partial(
                        mint,
                        env_float("PGG2_LATCH_SCALE_OUT_FRACTION", 0.30),
                        price,
                        f"moonshot_latch_lock peak={pos.peak_mult:.2f}",
                    )
                    if sold:
                        pos.scale_out_step = 1  # advance past tier 0 so tier 1 doesn't double-fire
                        log(
                            f"PGG2-LATCH-LOCK mint={mint[:8]} peak={pos.peak_mult:.2f} "
                            f"sold 30% at latch — protects vs cascade dump"
                        )
                        return
                # Phase 8 2026-05-08: TIERED SCALE-OUT (research-derived).
                # MemeTrans paper (41k tokens, 2026): scale-out exits beat single-shot
                # by +56% loss-mitigation. Marino paper: 60% of post-migration tokens
                # drop -80% in 20 min — locking partial profits at peak >= 2x is
                # mathematically dominant. Cascade dumps on bonding curves can move
                # 60%+ in single ticks; tighter trails alone aren't enough.
                #
                # Tier 1 — peak >= 2.0: sell 60% of remaining (lock $2-3)
                # Tier 2 — peak >= 4.0: sell 50% of remaining (lock $5-10 more)
                # After tiers: trail what's left with normal tiered trail
                # Stall exit — if peak hasn't advanced in 20s and mult >= 1.15,
                # take 75% (the flatline-then-rug pattern from research)
                if env_bool("PGG2_SCALE_OUT_ENABLED", True):
                    if (
                        pos.scale_out_step == 0
                        and pos.peak_mult >= env_float("PGG2_SCALE_OUT_TIER1_PEAK", 2.0)
                    ):
                        sold = self.broker.partial(
                            mint,
                            env_float("PGG2_SCALE_OUT_TIER1_FRACTION", 0.60),
                            price,
                            f"moonshot_scale_out_t1 peak={pos.peak_mult:.2f}",
                        )
                        if sold:
                            pos.scale_out_step = 1
                            log(
                                f"PGG2-SCALE-OUT-T1 mint={mint[:8]} peak={pos.peak_mult:.2f} "
                                f"sold tier1 fraction"
                            )
                            return
                    if (
                        pos.scale_out_step == 1
                        and pos.peak_mult >= env_float("PGG2_SCALE_OUT_TIER2_PEAK", 4.0)
                    ):
                        sold = self.broker.partial(
                            mint,
                            env_float("PGG2_SCALE_OUT_TIER2_FRACTION", 0.50),
                            price,
                            f"moonshot_scale_out_t2 peak={pos.peak_mult:.2f}",
                        )
                        if sold:
                            pos.scale_out_step = 2
                            log(
                                f"PGG2-SCALE-OUT-T2 mint={mint[:8]} peak={pos.peak_mult:.2f} "
                                f"sold tier2 fraction"
                            )
                            return
                    # Phase 14 2026-05-08: TIER 3 scale-out for early aggressive
                    # capture. Wave Surfer architecture: lock most profit early
                    # at 1.80x while holding final 25% for moonshot tail.
                    if (
                        pos.scale_out_step == 2
                        and pos.peak_mult >= env_float("PGG2_SCALE_OUT_TIER3_PEAK", 99.0)
                    ):
                        sold = self.broker.partial(
                            mint,
                            env_float("PGG2_SCALE_OUT_TIER3_FRACTION", 0.50),
                            price,
                            f"moonshot_scale_out_t3 peak={pos.peak_mult:.2f}",
                        )
                        if sold:
                            pos.scale_out_step = 3
                            log(
                                f"PGG2-SCALE-OUT-T3 mint={mint[:8]} peak={pos.peak_mult:.2f} "
                                f"sold tier3 fraction"
                            )
                            return

                # Stall exit: peak hasn't advanced in N seconds and we're up >15%
                if env_bool("PGG2_STALL_EXIT_ENABLED", True) and pos.scale_out_step >= 1:
                    stall_sec = env_float("PGG2_STALL_EXIT_SEC", 20.0)
                    stall_min_mult = env_float("PGG2_STALL_EXIT_MIN_MULT", 1.15)
                    time_since_peak = (ts_ms - pos.peak_advance_ts) / 1000.0 if pos.peak_advance_ts else 0
                    if (
                        time_since_peak >= stall_sec
                        and mult >= stall_min_mult
                        and pos.scale_out_step < 99
                    ):
                        sold = self.broker.partial(
                            mint, 0.75, price,
                            f"moonshot_stall_exit stall={time_since_peak:.0f}s mult={mult:.2f}",
                        )
                        if sold:
                            pos.scale_out_step = 99  # mark stall done
                            log(
                                f"PGG2-STALL-EXIT mint={mint[:8]} stall={time_since_peak:.0f}s "
                                f"mult={mult:.2f} peak={pos.peak_mult:.2f}"
                            )
                            return

                # Tiered trail: peak >= 3.0 → 0.75 (lock 75%+) | peak >= 2.0 → 0.80
                # | peak >= 1.60 → 0.85 | peak < 1.60 → 0.90.
                # Phase 7C: tightened from 0.40/0.60/0.80/0.85 to lock more on cascade dumps.
                peak = pos.peak_mult
                if peak >= env_float("PGG2_MOONSHOT_RIDE_TIER3_PEAK", 3.0):
                    trail_frac = env_float("PGG2_MOONSHOT_RIDE_TIER3_TRAIL", 0.40)
                elif peak >= env_float("PGG2_MOONSHOT_RIDE_TIER2_PEAK", 2.0):
                    trail_frac = env_float("PGG2_MOONSHOT_RIDE_TIER2_TRAIL", 0.50)
                elif peak >= env_float("PGG2_MOONSHOT_RIDE_TIER1_PEAK", 1.60):
                    trail_frac = env_float("PGG2_MOONSHOT_RIDE_TIER1_TRAIL", 0.80)
                else:
                    trail_frac = env_float("PGG2_MOONSHOT_RIDE_TIER0_TRAIL", 0.85)
                trail_floor = peak * trail_frac
                age_sec = pos.age_sec(ts_ms)
                hard_timeout = env_float("PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC", 90.0)
                # Phase 5 2026-05-08: min-hold post-latch. After the latch fires,
                # ignore the regular trail for `min_hold_sec` to avoid a tight dip
                # immediately after latch from triggering exit. Only a panic dump
                # (mult <= peak * panic_trail) breaks the min-hold.
                min_hold_sec = env_float("PGG2_MOONSHOT_RIDE_MIN_HOLD_SEC", 5.0)
                panic_trail = env_float("PGG2_MOONSHOT_RIDE_PANIC_TRAIL", 0.30)
                time_since_latch = (ts_ms - pos.moonshot_arm_ts) / 1000.0
                if time_since_latch < min_hold_sec:
                    # In min-hold window: only exit on extreme dump or migration.
                    if mult <= peak * panic_trail:
                        self.close_position(
                            mint, ts_ms, price,
                            f"moonshot_panic peak={peak:.2f} mult={mult:.2f}",
                            features, killed=False,
                        )
                        return
                    if features["complete"]:
                        self.close_position(mint, ts_ms, price, "moonshot_migration_complete", features, killed=False)
                        return
                    return
                if mult <= trail_floor:
                    self.close_position(
                        mint, ts_ms, price,
                        f"moonshot_trail peak={peak:.2f} trail={trail_frac:.2f} mult={mult:.2f}",
                        features, killed=False,
                    )
                    return
                if features["complete"]:
                    self.close_position(mint, ts_ms, price, "moonshot_migration_complete", features, killed=False)
                    return
                if age_sec >= hard_timeout:
                    self.close_position(
                        mint, ts_ms, price,
                        f"moonshot_hard_timeout peak={peak:.2f}",
                        features, killed=False,
                    )
                    return
                # In moonshot mode: bypass all other exit checks, let the trade run.
                return

        quote_loss_clamp = getattr(self.broker, "quote_loss_clamp_reason", None)
        if quote_loss_clamp and self.moonshot_lane(pos.lane):
            quote_action = quote_loss_clamp(pos, ts_ms)
            if quote_action:
                self.close_position(
                    mint,
                    ts_ms,
                    price,
                    quote_action,
                    features,
                    killed=(quote_action == "quote_loss_clamp"),
                )
                return

        quote_profit_bank = getattr(self.broker, "quote_profit_bank_reason", None)
        if quote_profit_bank and self.moonshot_lane(pos.lane):
            quote_exit = quote_profit_bank(pos, ts_ms)
            if quote_exit:
                self.close_position(mint, ts_ms, price, quote_exit, features, killed=False)
                return

        if (
            pos.lane == "priced_snap"
            and pos.state in {"SCOUT", "SCALE1"}
            and env_bool("PGG2_PEAK_LOCK_ENABLED", True)
        ):
            # Phase 3 2026-05-08: PEAK-LOCK trailing stop — ratchets a profit floor
            # upward as peak rises. Replaces the old scout_profit_protect / scale_profit_protect
            # which had a "min_mult >= 1.08" gate that DIDN'T fire when peak retreated
            # past that floor — so price could fall from 1.43x peak through 1.08 and
            # only exit via hard_break at 0.92, becoming a loss despite hitting 1.43x.
            # Phase 2B data: 67 scout_profit_protect trades exited at LOSS despite avg
            # peak of 1.428x. PEAK-LOCK preserves capital once profit is seen.
            #
            # Edge cases handled:
            # - Peak never reaches lock_low_peak: this block doesn't fire, hard_break catches
            # - Peak reaches 1.18 then drops fast: floor = max(1.05, peak*0.92) = 1.085, exit there
            # - Peak reaches 2.0 then drops: floor = max(1.30, 2.0*0.85) = 1.70, lock 70% profit
            # - Peak reaches 1.30 then re-pumps to 1.50: peak_mult tracks max, floor ratchets up
            # - mult fluctuates around floor: exit on first cross-below, no whipsaw re-entry
            peak = pos.peak_mult
            if peak >= env_float("PGG2_PEAK_LOCK_HIGH_PEAK", 1.60):
                floor = max(
                    env_float("PGG2_PEAK_LOCK_HIGH_FLOOR", 1.30),
                    peak * env_float("PGG2_PEAK_LOCK_HIGH_TRAIL", 0.85),
                )
                lock_tier = "high"
            elif peak >= env_float("PGG2_PEAK_LOCK_MID_PEAK", 1.30):
                floor = max(
                    env_float("PGG2_PEAK_LOCK_MID_FLOOR", 1.15),
                    peak * env_float("PGG2_PEAK_LOCK_MID_TRAIL", 0.88),
                )
                lock_tier = "mid"
            elif peak >= env_float("PGG2_PEAK_LOCK_LOW_PEAK", 1.18):
                floor = max(
                    env_float("PGG2_PEAK_LOCK_LOW_FLOOR", 1.05),
                    peak * env_float("PGG2_PEAK_LOCK_LOW_TRAIL", 0.92),
                )
                lock_tier = "low"
            else:
                floor = 0.0  # no lock active — trade hasn't shown profit yet
                lock_tier = ""
            if floor > 0 and mult < floor:
                state_tag = "scale" if pos.state == "SCALE1" else "scout"
                reason = f"priced_snap_{state_tag}_peak_lock_{lock_tier}"
                self.close_position(mint, ts_ms, price, reason, features, killed=False)
                return

        kill = self.piggy_kill_reason(pos, features)
        if kill:
            defer_paper_kill = getattr(self.broker, "defer_paper_kill_reason", None)
            if defer_paper_kill and defer_paper_kill(pos, kill, ts_ms):
                return
            self.close_position(mint, ts_ms, price, kill, features, killed=True)
            return
        if features["complete"]:
            self.close_position(mint, ts_ms, price, "migration_complete", features, killed=False)
            return

        if pos.state == "SCOUT":
            if self.moonshot_lane(pos.lane):
                sell_pressure = features["s700"]["sell_sol"] > 0 or features["s1500"]["sell_sol"] / max(features["s1500"]["buy_sol"], 0.001) >= 0.10
                probe_sized = pos.target_sol > 0 and pos.cost_sol <= (
                    pos.target_sol * env_float("PIGGY_PROBE_SIZED_MAX_TARGET_RATIO", 0.50)
                )
                if pos.peak_mult >= 1.55 and mult <= pos.peak_mult * 0.84:
                    self.close_position(mint, ts_ms, price, "moonshot_decay_55", features, killed=False)
                    return
                if pos.peak_mult >= 1.32 and sell_pressure and mult >= 1.12:
                    self.close_position(mint, ts_ms, price, "moonshot_pop_after_sell", features, killed=False)
                    return
                if (
                    pos.lane == "birth_fanout"
                    and pos.peak_mult >= env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_PEAK", 1.20)
                    and mult >= env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_MULT", 1.10)
                    and features["s700"]["sell_sol"] >= max(
                        env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_SELL_SOL", 0.35),
                        features["s700"]["buy_sol"] * env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_SELL_RATIO", 0.18),
                    )
                ):
                    self.close_position(mint, ts_ms, price, "birth_fanout_sell_slam_bank", features, killed=False)
                    return
                if (
                    pos.lane in {"birth_fanout", "whale_spark"}
                    and pos.peak_mult >= env_float("PGG2_BIRTH_FANOUT_POP_BANK_PEAK", 1.10)
                    and mult >= env_float("PGG2_BIRTH_FANOUT_POP_BANK_MIN_MULT", 1.04)
                    and mult <= pos.peak_mult * env_float("PGG2_BIRTH_FANOUT_POP_BANK_TRAIL", 0.98)
                ):
                    self.close_position(mint, ts_ms, price, f"{pos.lane}_pop_bank", features, killed=False)
                    return
                if pos.lane == "priced_snap":
                    first_pop_min_mult = env_float("PGG2_PRICED_SNAP_FIRSTPOP_MIN_MULT", 1.32)
                elif pos.lane == "priced_breakout":
                    first_pop_min_mult = env_float("PGG2_PRICED_BREAKOUT_FIRSTPOP_MIN_MULT", 1.12)
                else:
                    first_pop_min_mult = 1.02
                if (
                    pos.lane not in {"birth_fanout", "whale_spark"}
                    and pos.peak_mult >= 1.08
                    and sell_pressure
                    and mult >= first_pop_min_mult
                ):
                    self.close_position(mint, ts_ms, price, "first_pop_sell_exit", features, killed=False)
                    return
                if (
                    pos.lane in {"priced_breakout", "priced_snap"}
                    and pos.peak_mult >= env_float(
                        "PGG2_PRICED_SNAP_POP_BANK_PEAK" if pos.lane == "priced_snap" else "PGG2_PRICED_BREAKOUT_POP_BANK_PEAK",
                        1.25,
                    )
                    and mult >= env_float(
                        "PGG2_PRICED_SNAP_POP_BANK_MIN_MULT" if pos.lane == "priced_snap" else "PGG2_PRICED_BREAKOUT_POP_BANK_MIN_MULT",
                        1.12,
                    )
                    and mult <= pos.peak_mult * env_float(
                        "PGG2_PRICED_SNAP_POP_BANK_TRAIL" if pos.lane == "priced_snap" else "PGG2_PRICED_BREAKOUT_POP_BANK_TRAIL",
                        0.90,
                    )
                ):
                    self.close_position(mint, ts_ms, price, f"{pos.lane}_pop_bank", features, killed=False)
                    return
                if (
                    probe_sized
                    and pos.lane == "reclaim_wave"
                    and int(features.get("age_ms") or 0) >= env_int("PIGGY_LATE_RECLAIM_POP_BANK_AGE_MS", 30000)
                    and mult >= env_float("PIGGY_LATE_RECLAIM_POP_BANK_MULT", 1.10)
                ):
                    self.close_position(mint, ts_ms, price, "late_reclaim_probe_pop_bank", features, killed=False)
                    return
                if (
                    pos.lane == "early_ignition"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_EARLY_NO_FOLLOW_AFTER_SEC", 5.0)
                    and pos.peak_mult < env_float("PGG2_EARLY_NO_FOLLOW_MIN_PEAK", 1.075)
                    and mult <= env_float("PGG2_EARLY_NO_FOLLOW_MULT", 0.995)
                    and (
                        not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PGG2_EARLY_NO_FOLLOW_LAST_BUY_MS", 600)
                    )
                ):
                    self.close_position(mint, ts_ms, price, "early_ignition_no_follow", features, killed=True)
                    return
                if (
                    pos.lane == "late_ignition"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_LATE_NO_FOLLOW_AFTER_SEC", 4.0)
                    and pos.peak_mult < env_float("PGG2_LATE_NO_FOLLOW_MIN_PEAK", 1.06)
                    and mult <= env_float("PGG2_LATE_NO_FOLLOW_MULT", 0.99)
                    and (
                        not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PGG2_LATE_NO_FOLLOW_LAST_BUY_MS", 500)
                    )
                ):
                    self.close_position(mint, ts_ms, price, "late_ignition_no_follow", features, killed=True)
                    return
                if (
                    pos.lane == "breadth_ignition"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_BREADTH_NO_FOLLOW_AFTER_SEC", 4.0)
                    and pos.peak_mult < env_float("PGG2_BREADTH_NO_FOLLOW_MIN_PEAK", 1.06)
                    and mult <= env_float("PGG2_BREADTH_NO_FOLLOW_MULT", 0.99)
                    and (
                        not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PGG2_BREADTH_NO_FOLLOW_LAST_BUY_MS", 500)
                    )
                ):
                    self.close_position(mint, ts_ms, price, "breadth_ignition_no_follow", features, killed=True)
                    return
                birth_entry_feats = (self.position_follow.get(mint) or {}).get("entry_features") or {}
                birth_broad_recovery = (
                    pos.lane == "birth_fanout"
                    and float(birth_entry_feats.get("buy1500") or 0.0) >= env_float("PGG2_BIRTH_FANOUT_RECOVERY_MIN_BUY1500", 5.0)
                    and int(birth_entry_feats.get("uniq1500") or 0) >= env_int("PGG2_BIRTH_FANOUT_RECOVERY_MIN_UNIQ1500", 10)
                    and float(birth_entry_feats.get("top_share1500") or 1.0) <= env_float("PGG2_BIRTH_FANOUT_RECOVERY_MAX_TOP1500", 0.42)
                )
                if (
                    pos.lane == "birth_fanout"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_AFTER_SEC", 4.0)
                    and pos.peak_mult < env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_MIN_PEAK", 1.06)
                    and mult <= env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_MULT", 0.99)
                    and (
                        not birth_broad_recovery
                        or not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PGG2_BIRTH_FANOUT_NO_FOLLOW_LAST_BUY_MS", 500)
                    )
                ):
                    self.close_position(mint, ts_ms, price, "birth_fanout_no_follow", features, killed=True)
                    return
                if (
                    pos.lane == "curve_lag_reveal"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_CURVE_LAG_NO_FOLLOW_AFTER_SEC", 5.0)
                    and pos.peak_mult < env_float("PGG2_CURVE_LAG_NO_FOLLOW_MIN_PEAK", 1.08)
                    and mult <= env_float("PGG2_CURVE_LAG_NO_FOLLOW_MULT", 0.985)
                    and (
                        not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PGG2_CURVE_LAG_NO_FOLLOW_LAST_BUY_MS", 600)
                    )
                ):
                    self.close_position(mint, ts_ms, price, "curve_lag_no_follow", features, killed=True)
                    return
                scale = self.scale1_reason(pos, features)
                if scale:
                    target_after_scale = min(pos.target_sol, self.config.max_position_sol)
                    add_sol = max(0.0, target_after_scale - pos.cost_sol)
                    scaled = self.broker.scale(mint, add_sol, price, "SCALE1", scale)
                    if scaled:
                        self.logger.decision("scale1", mint, {"add_sol": add_sol, "reason": scale, "features": self.slim_features(features)})
                    return
                if pos.age_sec(ts_ms) >= env_float("PIGGY_MOON_FAIL_SEC", 18.0) and pos.peak_mult < 1.18:
                    self.close_position(mint, ts_ms, price, "moonshot_failed_no_pop", features, killed=True)
                    return
                if pos.age_sec(ts_ms) >= env_float("PIGGY_MOON_TIMEBOX_SEC", 55.0):
                    self.close_position(mint, ts_ms, price, "moonshot_timebox", features, killed=False)
                    return
                return
            scale = self.scale1_reason(pos, features)
            if scale:
                target_after_scale = min(pos.target_sol * 0.68, self.config.max_position_sol)
                add_sol = max(0.0, target_after_scale - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "SCALE1", scale)
                if scaled:
                    self.logger.decision("scale1", mint, {"add_sol": add_sol, "reason": scale, "features": self.slim_features(features)})
                return
            if pos.age_sec(ts_ms) >= 1.15 and pos.peak_mult < 1.075:
                self.close_position(mint, ts_ms, price, "scratch_no_markup", features, killed=True)
                return
            if pos.age_sec(ts_ms) >= 2.40 and pos.peak_mult < 1.16:
                self.close_position(mint, ts_ms, price, "scratch_no_second_wave", features, killed=True)
                return

        if pos.state == "SCALE1":
            if not pos.derisk_done and self.derisk_gate(pos, features):
                self.broker.partial(mint, 0.62, price, "finance_runner_on_first_pop")
                self.logger.decision("derisk", mint, {"features": self.slim_features(features)})
                return
            scale2 = self.scale2_reason(pos, features)
            if scale2:
                add_sol = max(0.0, pos.target_sol - pos.cost_sol)
                scaled = self.broker.scale(mint, add_sol, price, "RUNNER_FULL", scale2)
                if scaled:
                    self.logger.decision("scale2", mint, {"add_sol": add_sol, "reason": scale2, "features": self.slim_features(features)})
                return
            if pos.age_sec(ts_ms) >= 3.25 and pos.peak_mult < 1.22:
                self.close_position(mint, ts_ms, price, "scale_stalled_before_pop", features, killed=True)
                return

        if pos.state in {"RUNNER", "RUNNER_FULL"}:
            if pos.peak_mult >= 3.0 and mult <= pos.peak_mult * 0.76:
                self.close_position(mint, ts_ms, price, "runner_3x_decay", features, killed=False)
                return
            if pos.peak_mult >= 1.65 and mult <= pos.peak_mult * 0.82:
                self.close_position(mint, ts_ms, price, "runner_flow_decay", features, killed=False)
                return
            if features["last_buy_age_ms"] >= 1450 and features["s1500"]["sell_sol"] > 0:
                self.close_position(mint, ts_ms, price, "runner_stalled_after_sell", features, killed=False)
                return
            if pos.age_sec(ts_ms) >= 55.0:
                self.close_position(mint, ts_ms, price, "runner_timebox", features, killed=False)
                return

        if pos.age_sec(ts_ms) >= 75.0:
            self.close_position(mint, ts_ms, price, "hard_75s_time_stop", features, killed=False)

    def piggy_kill_reason(self, pos: Any, features: dict[str, Any]) -> Optional[str]:
        s250 = features["s250"]
        s700 = features["s700"]
        if SameBlockPiggybackBot.moonshot_lane(pos.lane):
            age_sec = pos.age_sec(features["ts_ms"])
            full_sized_entry = pos.target_sol > 0 and pos.scout_sol >= (
                pos.target_sol * env_float("PIGGY_FULL_SIZE_SCOUT_TARGET_RATIO", 0.95)
            )
            birth_profile = ""
            if pos.lane == "birth_fanout":
                follow_entry = (self.position_follow.get(pos.mint) or {}).get("entry_features") or {}
                plan_entry = getattr(pos, "entry_features", None) or {}
                entry_features = follow_entry if follow_entry else plan_entry
                birth_ctx = entry_features.get("birth_fanout") if isinstance(entry_features, dict) else {}
                if not birth_ctx and isinstance(plan_entry, dict):
                    birth_ctx = plan_entry.get("birth_fanout")
                if not isinstance(birth_ctx, dict):
                    birth_ctx = {}
                birth_profile = str(
                    birth_ctx.get("birth_entry_profile")
                    or (entry_features.get("birth_entry_profile") if isinstance(entry_features, dict) else "")
                    or ""
                )
                if not birth_profile:
                    reason_parts = str(getattr(pos, "reason", "") or "").split()
                    for part in reason_parts:
                        if part.startswith("profile="):
                            birth_profile = part.split("=", 1)[1].strip()
                            break
            if pos.lane == "raw_momentum" and pos.last_mult <= env_float("PGG2_RAW_MOMENTUM_HARD_BREAK_MULT", 0.95):
                return "kill_raw_momentum_hard_break"
            if pos.lane == "priced_snap" and pos.last_mult <= env_float("PGG2_PRICED_SNAP_HARD_BREAK_MULT", 0.92):
                # PGG2: path tests on attack dry-live tapes showed priced_snap
                # needs room for shallow dump-then-pump recoveries, but should
                # not wait for the generic 0.88 rug stop on failed snaps.
                # Phase 3 2026-05-08: only fire hard_break when trade never showed
                # profit (peak < PEAK-LOCK low tier 1.18). PEAK-LOCK takes over for
                # trades that ratcheted up — analysis showed hard_break was -$23.46
                # net because it fired on trades after they had peaked at 1.428x avg.
                # Phase 5 2026-05-08: ALSO defer hard_break for the first N seconds
                # if buyers are still actively adding (last_buy_age_ms <= window).
                # Phase 4 dry data: 3 of 6 losses had post-exit peaks of 3.06x, 3.65x,
                # and 3.82x — bot exited the dip at -$0.40 each, then mint pumped 3-4x.
                # Buyer flow during that dip would have signaled "hold" if measured.
                peak_floor = env_float("PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW", 1.18)
                if pos.peak_mult < peak_floor:
                    grace_enabled = env_bool("PGG2_HARD_BREAK_GRACE_ENABLED", True)
                    grace_sec = env_float("PGG2_HARD_BREAK_GRACE_SEC", 8.0)
                    grace_buy_window_ms = env_int("PGG2_HARD_BREAK_GRACE_BUY_AGE_MS", 1500)
                    if grace_enabled and age_sec < grace_sec:
                        last_buy_ms = features.get("last_buy_age_ms", 999999)
                        # Buyer activity within grace_buy_window means flow is alive,
                        # the dip is just a pullback. Don't kill.
                        if last_buy_ms is not None and last_buy_ms <= grace_buy_window_ms:
                            return None
                    return "kill_priced_snap_hard_break"
            if env_bool("PGG2_LAYERED_RISK_ENABLED", False):
                if (
                    pos.lane == "priced_snap"
                    and age_sec >= env_float("PGG2_PRICED_SNAP_LAYERED_FAIL_AFTER_SEC", 4.0)
                    and pos.peak_mult <= env_float("PGG2_PRICED_SNAP_LAYERED_FAIL_MAX_PEAK", 1.12)
                    and pos.last_mult <= env_float("PGG2_PRICED_SNAP_LAYERED_FAIL_MAX_MULT", 0.99)
                    and features.get("last_post_open_sig_buy_age_ms", 999999)
                    >= env_int("PGG2_PRICED_SNAP_LAYERED_FAIL_MIN_LAST_BUY_MS", 400)
                ):
                    return "kill_priced_snap_layered_no_follow"
                if (
                    pos.lane == "birth_fanout"
                    and age_sec >= env_float("PGG2_BIRTH_FANOUT_LAYERED_FAIL_AFTER_SEC", 0.75)
                    and pos.peak_mult <= env_float("PGG2_BIRTH_FANOUT_LAYERED_FAIL_MAX_PEAK", 1.03)
                    and pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_LAYERED_FAIL_MAX_MULT", 1.05)
                    and features.get("last_post_open_sig_buy_age_ms", 999999)
                    >= env_int("PGG2_BIRTH_FANOUT_LAYERED_FAIL_MIN_LAST_BUY_MS", 400)
                ):
                    return "kill_birth_fanout_layered_no_follow"
            if pos.lane == "priced_breakout" and pos.last_mult <= env_float("PGG2_PRICED_BREAKOUT_HARD_BREAK_MULT", 0.94):
                return "kill_priced_breakout_hard_break"
            if pos.lane == "birth_fanout":
                if birth_profile == "elite_nofollow":
                    # Elite no-follow birth probes are intentionally small and early.
                    # The live tape showed a clean elite probe dip to 0.90x in 0.33s,
                    # then rip to 2.33x. Give only this 0.02 SOL profile room to
                    # survive the first launch shakeout; keep a panic cut for true rugs.
                    if pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_PANIC_BREAK_MULT", 0.72):
                        return "kill_birth_fanout_elite_panic_break"
                    if (
                        age_sec >= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_HARD_GRACE_SEC", 1.25)
                        and pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_HARD_BREAK_MULT", 0.82)
                    ):
                        return "kill_birth_fanout_elite_hard_break"
                elif (
                    birth_profile == "follow_confirm"
                    and features["s1500"]["sell_sol"] <= env_float("PGG2_BIRTH_FANOUT_FOLLOW_GRACE_MAX_SELL_SOL", 0.001)
                    and features["s700"]["sell_sol"] <= env_float("PGG2_BIRTH_FANOUT_FOLLOW_GRACE_MAX_SELL_SOL", 0.001)
                ):
                    # Full-size follow-confirm births sometimes dip before the
                    # delayed second burst. If there are no sells, a sub-2.8s
                    # hard break is usually price discovery noise, not a rug.
                    if pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_FOLLOW_PANIC_BREAK_MULT", 0.82):
                        return "kill_birth_fanout_follow_panic_break"
                    if (
                        age_sec >= env_float("PGG2_BIRTH_FANOUT_FOLLOW_HARD_GRACE_SEC", 2.75)
                        and pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_FOLLOW_HARD_BREAK_MULT", 0.90)
                    ):
                        return "kill_birth_fanout_follow_hard_break"
                elif pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_HARD_BREAK_MULT", 0.92):
                    return "kill_birth_fanout_hard_break"
            if (
                pos.state == "SCOUT"
                and birth_profile != "elite_nofollow"
                and age_sec <= env_float("PIGGY_EARLY_FAIL_SEC", 8.0)
                and pos.peak_mult < env_float("PIGGY_EARLY_FAIL_MIN_PEAK", 1.005)
                and pos.last_mult <= env_float("PIGGY_EARLY_FAIL_MULT", 0.90)
                and not features["flow_live"]
                and features["last_sell_age_ms"] <= env_int("PIGGY_EARLY_FAIL_SELL_AGE_MS", 700)
                and features["s1500"]["sell_sol"] > 0
            ):
                return "kill_early_entry_failed"
            if (
                pos.state == "SCOUT"
                and full_sized_entry
                and pos.lane not in {
                    "birth_fanout",
                    "curve_lag_reveal",
                    "preprice_reveal",
                    "reclaim_wave",
                    "second_wave_after_cluster",
                }
                and age_sec >= env_float("PIGGY_FULL_NO_POP_AFTER_SEC", 3.25)
                and age_sec <= env_float("PIGGY_FULL_NO_POP_UNTIL_SEC", 10.0)
                and pos.peak_mult < env_float("PIGGY_FULL_NO_POP_MIN_PEAK", 1.035)
                and pos.last_mult <= env_float("PIGGY_FULL_NO_POP_MULT", 0.995)
                and (
                    not features["flow_live"]
                    or features["last_buy_age_ms"] >= env_int("PIGGY_FULL_NO_POP_MAX_LAST_BUY_AGE_MS", 850)
                )
            ):
                return "kill_full_no_pop"
            if (
                pos.state == "SCOUT"
                and env_bool("PIGGY_NO_FOLLOW_CAP_ON", True)
                and features.get("post_open_follow_trusted", False)
                and age_sec >= env_float("PIGGY_NO_FOLLOW_CAP_AFTER_SEC", 6.0)
                and age_sec <= env_float("PIGGY_NO_FOLLOW_CAP_UNTIL_SEC", 18.0)
                and pos.peak_mult < env_float("PIGGY_NO_FOLLOW_CAP_MIN_PEAK", 1.08)
                and features.get("post_open_sig_buy_sol", 0.0) < env_float("PIGGY_NO_FOLLOW_CAP_MIN_SIG_BUY_SOL", 0.10)
                and pos.last_mult <= env_float("PIGGY_NO_FOLLOW_CAP_MULT", 0.985)
                and (
                    pos.last_mult >= env_float("PIGGY_NO_FOLLOW_CAP_BOUNCE_MULT", 0.955)
                    or age_sec >= env_float("PIGGY_NO_FOLLOW_CAP_FORCE_SEC", 12.0)
                )
            ):
                return "kill_no_followthrough_bounce"
            if pos.lane != "birth_fanout" and pos.last_mult <= env_float("PIGGY_MOON_HARD_BREAK_MULT", 0.88):
                return "kill_moon_hard_break"
            return None
        if pos.last_mult <= 0.80:
            return "kill_fast_break"
        if s250["sell_sol"] >= max(0.006, s250["buy_sol"] * 0.42) and features["move250"] < 1.002:
            return "kill_first_distribution"
        if s700["sell_sol"] >= max(0.012, s700["buy_sol"] * 0.30) and features["move700"] < 1.006:
            return "kill_unabsorbed_sell"
        if features["s1500"]["top_buyer_flip"] >= 0.18 and pos.peak_mult < 1.55:
            return "kill_cluster_wallet_selling"
        if (
            pos.state in {"SCOUT", "SCALE1"}
            and pos.lane != "reclaim_wave"
            and features["buy_stall"]
            and pos.last_mult < 1.18
        ):
            return "kill_buyer_stall"
        if features["off_peak"] < 0.82 and pos.age_sec(features["ts_ms"]) <= 5.0:
            return "kill_markup_failed"
        return None

    def scale1_reason(self, pos: Any, features: dict[str, Any]) -> Optional[str]:
        if pos.scale1_done:
            return None
        s250 = features["s250"]
        s700 = features["s700"]
        if (
            env_bool("PGG2_LAYERED_RISK_ENABLED", False)
            and pos.lane in {"priced_snap", "birth_fanout"}
            and pos.target_sol > 0
            and pos.cost_sol < pos.target_sol * env_float("PGG2_LAYERED_SCALE_MIN_REMAINING_RATIO", 0.995)
            and pos.age_sec(features["ts_ms"]) <= env_float("PGG2_LAYERED_SCALE_MAX_AGE_SEC", 8.0)
            and pos.peak_mult >= env_float("PGG2_LAYERED_SCALE_MIN_PEAK", 1.0)
            and pos.last_mult >= env_float("PGG2_LAYERED_SCALE_MIN_MULT", 1.0)
            and features.get("post_open_sig_buy_sol", 0.0) >= env_float("PGG2_LAYERED_SCALE_MIN_POST_BUY_SOL", 0.25)
            and features.get("post_open_sig_buy_count", 0) >= env_int("PGG2_LAYERED_SCALE_MIN_POST_BUY_COUNT", 2)
        ):
            return "layered_post_entry_follow_confirm"
        entry_features = (self.position_follow.get(pos.mint) or {}).get("entry_features") or {}
        if entry_features and SameBlockPiggybackBot.moonshot_lane(pos.lane):
            # PGG2 precision guard: do not convert a scout into a full-size
            # position when the original entry was narrow or top-heavy. Today's
            # PGG2 loss opened with uniq700=2/top700=0.75, then scaled to 0.20
            # SOL and dumped. On the 7h control tape this blocks the bad scaled
            # loser while keeping the real scaled winner.
            entry_uniq700 = int(entry_features.get("uniq700") or 0)
            entry_top700 = float(entry_features.get("top_share700") or 1.0)
            if entry_uniq700 < env_int("PGG2_SCALE_MIN_ENTRY_UNIQ700", 4):
                return None
            if entry_top700 > env_float("PGG2_SCALE_MAX_ENTRY_TOP700", 0.55):
                return None
        probe_sized = pos.target_sol > 0 and pos.cost_sol <= (
            pos.target_sol * env_float("PIGGY_PROBE_SIZED_MAX_TARGET_RATIO", 0.50)
        )
        if (
            pos.lane == "breadth_ignition"
            and not env_bool("PGG2_BREADTH_SCALE_ENABLED", False)
        ):
            return None
        if (
            pos.lane == "birth_fanout"
            and not env_bool("PGG2_BIRTH_FANOUT_SCALE_ENABLED", False)
        ):
            return None
        if (
            pos.lane == "stealth_arm"
            and not env_bool("PGG2_STEALTH_ARM_SCALE_ENABLED", False)
        ):
            return None
        if (
            pos.lane == "spark3_arm"
            and not env_bool("PGG2_SPARK3_ARM_SCALE_ENABLED", False)
        ):
            return None
        if (
            pos.lane == "spark3_breakout"
            and not env_bool("PGG2_SPARK3_BREAKOUT_SCALE_ENABLED", False)
        ):
            return None
        if probe_sized and SameBlockPiggybackBot.moonshot_lane(pos.lane):
            if int(features.get("age_ms") or 0) >= env_int("PIGGY_PROBE_SCALE_MAX_TOKEN_AGE_MS", 15000):
                return None
            if s700["buy_sol"] < env_float("PIGGY_PROBE_SCALE_MIN_BUY700_SOL", 5.0):
                return None
            if s700["unique_buyers"] < env_int("PIGGY_PROBE_SCALE_MIN_BUYERS700", 8):
                return None
        if (
            pos.age_sec(features["ts_ms"]) <= 2.80
            and pos.last_mult >= 1.075
            and s250["sells"] == 0
            and s700["sell_sol"] <= max(0.005, s700["buy_sol"] * 0.05)
            and s700["unique_buyers"] >= 4
            and s700["buy_sol"] >= 1.40
            and s700["top_buy_share"] <= 0.66
            and features["last_buy_age_ms"] <= 380
            and features["move700"] >= 1.010
        ):
            return "cluster_still_marking_up"
        if (
            pos.age_sec(features["ts_ms"]) <= 1.80
            and pos.last_mult >= 1.22
            and features["s1500"]["buy_sol"] >= 2.50
            and features["s1500"]["unique_buyers"] >= 5
            and features["s1500"]["sell_sol"] / max(features["s1500"]["buy_sol"], 0.001) <= 0.05
        ):
            return "violent_markup_no_distribution"
        return None

    @staticmethod
    def derisk_gate(pos: Any, features: dict[str, Any]) -> bool:
        if pos.last_mult >= 1.55:
            return True
        if pos.last_mult >= 1.32 and features["last_buy_age_ms"] >= 550:
            return True
        if pos.last_mult >= 1.28 and features["s700"]["sell_sol"] > 0:
            return True
        return False

    @staticmethod
    def scale2_reason(pos: Any, features: dict[str, Any]) -> Optional[str]:
        if pos.scale2_done or pos.derisk_done:
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        if (
            pos.last_mult >= 1.42
            and s700["buy_sol"] >= 1.0
            and s1500["unique_buyers"] >= 6
            and s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) <= 0.10
            and features["last_buy_age_ms"] <= 400
        ):
            return "second_cluster_wave_before_derisk"
        return None

    @staticmethod
    def slim_features(features: dict[str, Any]) -> dict[str, Any]:
        out = BirthFirstSniper.slim_features(features)
        out.update(
            {
                "cluster_score": features.get("cluster_score", 0.0),
                "slot_buyers": features.get("slot_buyers", 0),
                "slot_buy_sol": features.get("slot_buy_sol", 0.0),
                "slot_top_share": features.get("slot_top_share", 0.0),
                "last_buy_age_ms": features.get("last_buy_age_ms", 999999),
                "last_sell_age_ms": features.get("last_sell_age_ms", 999999),
                "buy_stall": features.get("buy_stall", False),
                "flow_live": features.get("flow_live", False),
                "post_open_follow_trusted": features.get("post_open_follow_trusted", False),
                "post_open_sig_buy_sol": features.get("post_open_sig_buy_sol", 0.0),
                "post_open_sig_buy_count": features.get("post_open_sig_buy_count", 0),
                "post_open_sig_buyers": features.get("post_open_sig_buyers", 0),
                "last_post_open_sig_buy_age_ms": features.get("last_post_open_sig_buy_age_ms", 999999),
                "entry_size_reason": features.get("entry_size_reason", ""),
                "entry_probe_sol": features.get("entry_probe_sol", 0.0),
                "wave_armed": features.get("wave_armed", False),
                "wave_arm_age_ms": features.get("wave_arm_age_ms", 0),
                "wave_base_move": features.get("wave_base_move", 1.0),
                "buyer_hhi700": features["s700"].get("buyer_hhi", 0.0),
                "top_share700": features["s700"].get("top_buy_share", 0.0),
            }
        )
        return out

    def report_if_due(self, force: bool = False) -> None:
        if not force and time.time() - self.last_report_at < self.config.report_sec:
            return
        self.last_report_at = time.time()
        ts = now_ms()
        open_bits = [
            f"{short_addr(m)} {p.state} {p.last_mult:.2f}x pk={p.peak_mult:.2f} cost={p.cost_sol:.4f}"
            for m, p in self.broker.positions.items()
        ]
        st = self.broker.stats
        log(
            f"PIGGY-STATUS creates={st.creates} trades={st.trades} buys/sells={st.buys}/{st.sells} "
            f"plans={st.strike_plans} pend={len(self.broker.pending)} scouts={st.scouts} "
            f"scale1={st.scale1} scale2={st.scale2} partials={st.partials} closes={st.closes} "
            f"W/L={st.wins}/{st.losses} kills={st.kills} best={st.best_mult:.2f}x "
            f"realized={st.realized_pnl_sol:+.6f} open_pnl={self.broker.open_pnl():+.6f} SOL "
            f"open={len(self.broker.positions)} [{', '.join(open_bits) if open_bits else 'none'}] "
            f"shreds={st.shreds} bc={self.bc.updates} reconn={st.reconnects}"
        )
        if env_bool("PIGGY_SAVE_STATE_ON_STATUS", True):
            self.broker.save_state()

    @staticmethod
    def _engagement_synth_features(
        ts_ms: int,
        price: float,
        complete: bool,
        vsol_sol: float,
        age_ms: int,
    ) -> dict[str, Any]:
        """Phase 20C: build a features dict for poll-driven entries.

        Tape-derived fields (s700/s1500 stats, move250/700/1500, etc.) are
        absent because there's no shred-driven tape for these mints. Pad the
        keys that slim_features hard-codes so logger.decision doesn't KeyError.
        """
        empty_window = {
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "unique_buyers": 0,
            "tracked_buyers": 0,
            "buys": 0,
            "sells": 0,
            "top_buy_share": 0.0,
            "top_buyer_flip": 0.0,
            "buyer_hhi": 0.0,
            "sell_ratio": 0.0,
            "f_lt_50ms": 0.0,
            "price_change": 0.0,
        }
        return {
            "ts_ms": ts_ms,
            "price": price,
            "has_curve": not complete,
            "complete": complete,
            "vsol_sol": vsol_sol,
            "age_ms": age_ms,
            "buy_age_ms": 0,
            "first_buy_sol": 0.0,
            "first_buyer": "",
            "creator": "",
            "create_version": "",
            "is_mayhem": False,
            "move250": 1.0,
            "move700": 1.0,
            "move1500": 1.0,
            "sell_ratio700": 0.0,
            "concentration1500": 0.0,
            "off_peak": 1.0,
            "time_since_peak": 0.0,
            "score": 0.0,
            "s250": dict(empty_window),
            "s700": dict(empty_window),
            "s1500": dict(empty_window),
            "s3000": dict(empty_window),
            "s8000": dict(empty_window),
            "buy250": 0.0,
            "sell250": 0.0,
            "uniq250": 0,
            "top_share250": 0.0,
            "buy700": 0.0,
            "sell700": 0.0,
            "uniq700": 0,
            "top_share700": 0.0,
            "buy1500": 0.0,
            "sell1500": 0.0,
            "uniq1500": 0,
            "top_share1500": 0.0,
            "buy3000": 0.0,
            "sell3000": 0.0,
            "uniq3000": 0,
            "top_share3000": 0.0,
            "buy8000": 0.0,
            "sell8000": 0.0,
            "uniq8000": 0,
            "top_share8000": 0.0,
            "cluster_score": 0.0,
            "cluster_width_ok": False,
            "flow_live": False,
            "buy_stall": False,
            "wave_prev_peak": 0.0,
            "wave_armed": False,
            "wave_arm_age_ms": 0,
            "wave_base_move": 1.0,
            "slot_buyers": 0,
            "slot_buy_sol": 0.0,
            "slot_top_share": 0.0,
            "last_buy_age_ms": 0,
            "last_sell_age_ms": 999999,
        }

    def _engagement_poll_price(self, mint_str: str) -> tuple[float, bool, float]:
        """Phase 20C 2026-05-08: on-chain price fetch for poll-driven strikes.

        Reads the bonding curve account directly via the broker. If the curve
        is complete (post-migration), falls back to the PumpSwap pool reserves.
        Returns (price, complete, vsol_sol). On any error returns (0, False, 0).
        Price scale: lamports per raw token unit (matches CurvePoint.price).
        """
        broker = self.broker
        if not hasattr(broker, "bonding_curve"):
            return (0.0, False, 0.0)
        try:
            from solders.pubkey import Pubkey
            mint = Pubkey.from_string(mint_str)
        except Exception as exc:
            log(f"PGG2-POLL-PRICE-PUBKEY-ERR mint={mint_str[:8]} {type(exc).__name__}: {exc}")
            return (0.0, False, 0.0)
        try:
            curve = broker.bonding_curve(mint)
        except Exception as exc:
            log(f"PGG2-POLL-PRICE-CURVE-ERR mint={mint_str[:8]} {type(exc).__name__}: {exc}")
            return (0.0, False, 0.0)
        if not curve.complete:
            if curve.virtual_token_reserves <= 0:
                return (0.0, False, 0.0)
            price = float(curve.virtual_sol_reserves) / float(curve.virtual_token_reserves)
            return (price, False, curve.virtual_sol_reserves / 1_000_000_000.0)
        if not hasattr(broker, "pumpswap_pool"):
            return (0.0, True, 0.0)
        try:
            pool = broker.pumpswap_pool(mint)
            base_reserve = broker.token_account_balance_raw(pool.pool_base_token_account)
            quote_reserve = broker.token_account_balance_raw(pool.pool_quote_token_account)
        except Exception as exc:
            log(f"PGG2-POLL-PRICE-POOL-ERR mint={mint_str[:8]} {type(exc).__name__}: {exc}")
            return (0.0, True, 0.0)
        if base_reserve <= 0:
            return (0.0, True, 0.0)
        price = float(quote_reserve) / float(base_reserve)
        return (price, True, quote_reserve / 1_000_000_000.0)

    async def engagement_poll_strike_loop(self) -> None:
        """Phase 20C 2026-05-08: poll-driven strike trigger.

        Why this exists: the shred stream we subscribe to only carries pump.fun
        bonding-curve events. The engagement poller surfaces engaged mints via
        the pump.fun frontend API, but most of those mints have already migrated
        to PumpSwap — so their buy events never flow through our shred stream
        and on_event never fires engagement_driven_ready for them. Phase 20B
        produced 0 strikes in 11 minutes despite 200+ creates because of this
        structural mismatch.

        This loop closes the gap: every N seconds it ranks the engagement
        poller's tracked mints (KOTH first, then live with most viewers), runs
        engagement + age + RugCheck gates, fetches a fresh on-chain price via
        the broker, builds a synthetic StrikePlan with lane="engagement_driven",
        and fires it through broker.queue_or_fill. The engagement_driven exit
        logic in manage_position then takes over.
        """
        if self.engagement_poller is None:
            return
        if not env_bool("PGG2_ENGAGEMENT_POLL_STRIKE_ENABLED", True):
            log("PHASE20C: engagement_poll_strike_loop disabled")
            return
        if not hasattr(self.broker, "bonding_curve"):
            log("PHASE20C: broker has no bonding_curve method — loop disabled")
            return

        poll_sec = env_float("PGG2_ENGAGEMENT_POLL_STRIKE_SEC", 5.0)
        max_per_iter = env_int("PGG2_ENGAGEMENT_POLL_STRIKE_MAX_PER_ITER", 3)
        log(f"PHASE20C: engagement_poll_strike_loop starting poll={poll_sec}s max_per_iter={max_per_iter}")

        warmup = env_float("PGG2_ENGAGEMENT_POLL_STRIKE_WARMUP_SEC", 8.0)
        await asyncio.sleep(warmup)

        min_viewers = env_int("PGG2_ENGAGEMENT_POLL_MIN_VIEWERS",
                              env_int("PGG2_ENGAGEMENT_MIN_VIEWERS", 10))
        min_replies = env_int("PGG2_ENGAGEMENT_POLL_MIN_REPLIES",
                              env_int("PGG2_ENGAGEMENT_MIN_REPLIES", 3))
        # Phase 20C: poll loop has its own age window because the original
        # PGG2_ENGAGEMENT_MAX_AGE_MS was tuned for shred-driven entries on
        # fresh mints. Engaged tokens from the frontend API are typically
        # hours-to-days old (active livestreams, not freshly minted).
        min_age_ms = env_int("PGG2_ENGAGEMENT_POLL_MIN_AGE_MS",
                             env_int("PGG2_ENGAGEMENT_MIN_AGE_MS", 30000))
        max_age_ms = env_int("PGG2_ENGAGEMENT_POLL_MAX_AGE_MS", 86400000)
        diag_every = env_int("PGG2_ENGAGEMENT_POLL_DIAG_EVERY", 6)
        iter_count = 0

        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(poll_sec)
                engaged = dict(self.engagement_poller._engaged or {})
                if not engaged:
                    continue
                ts_ms = now_ms()
                koth_mint = self.engagement_poller._koth_mint

                def rank(item: tuple[str, dict[str, Any]]) -> float:
                    mint, info = item
                    koth_bonus = 1_000_000.0 if mint == koth_mint else 0.0
                    live = 100_000.0 if info.get("is_currently_live") else 0.0
                    viewers = float(info.get("num_participants") or 0) * 100.0
                    replies = float(info.get("reply_count") or 0)
                    return koth_bonus + live + viewers + replies

                # Phase 20D 2026-05-08: bias toward fresh pre-migration mints.
                # First closed-trade signal (3 trades): the only winner was
                # complete=0 (89s old, 16 viewers); both losers were complete=1
                # mature PumpSwap tokens. Rank pre-migration FIRST.
                require_fresh = env_bool("PGG2_ENGAGEMENT_POLL_FRESH_ONLY", False)
                fresh_max_age = env_int("PGG2_ENGAGEMENT_POLL_FRESH_MAX_AGE_MS", 1800000)

                def rank2(item: tuple[str, dict[str, Any]]) -> float:
                    mint, info = item
                    koth_bonus = 1_000_000.0 if mint == koth_mint else 0.0
                    is_complete = bool(info.get("complete"))
                    fresh_bonus = 0.0 if is_complete else 500_000.0
                    created_ts = int(info.get("created_timestamp") or 0)
                    age_ms_ = ts_ms - created_ts if created_ts else 999999999
                    fresh_recent_bonus = 200_000.0 if age_ms_ < fresh_max_age else 0.0
                    live = 100_000.0 if info.get("is_currently_live") else 0.0
                    viewers = float(info.get("num_participants") or 0) * 100.0
                    replies = float(info.get("reply_count") or 0)
                    return koth_bonus + fresh_bonus + fresh_recent_bonus + live + viewers + replies

                ranked = sorted(engaged.items(), key=rank2, reverse=True)
                full_scout = min(self.config.max_position_sol,
                                 env_float("PGG2_ENGAGEMENT_LANE_SOL", 0.025))
                fired = 0
                considered = 0
                rej = {"dup": 0, "engagement": 0, "age": 0, "rugcheck": 0, "no_price": 0, "complete": 0}
                iter_count += 1
                # Phase 20D: re-eligibility cooldown — reset engagement_driven_seen
                # entries older than this so good mints can re-fire after a closed
                # position. Use position close timestamp via recent_profit_reentry_locked.
                seen_cooldown_ms = env_int("PGG2_ENGAGEMENT_POLL_SEEN_COOLDOWN_MS", 300000)  # 5 min
                if hasattr(self, "_engagement_seen_ts"):
                    expired = [m for m, t in self._engagement_seen_ts.items()
                               if ts_ms - t > seen_cooldown_ms]
                    for m in expired:
                        self.engagement_driven_seen.discard(m)
                        del self._engagement_seen_ts[m]
                else:
                    self._engagement_seen_ts = {}

                consider_cap = max_per_iter * 8
                for mint, info in ranked:
                    if fired >= max_per_iter:
                        break
                    considered += 1
                    if considered > consider_cap:
                        break
                    # Dedup: skip if pipeline already sees this mint
                    if mint in self.broker.positions or mint in self.broker.pending:
                        rej["dup"] += 1
                        continue
                    if mint in self.engagement_driven_seen:
                        rej["dup"] += 1
                        continue
                    if self.recent_profit_reentry_locked(mint, ts_ms):
                        rej["dup"] += 1
                        continue
                    # Engagement gate (engaged or KOTH)
                    is_engaged = self.engagement_poller.is_engaged(
                        mint, min_viewers=min_viewers, min_replies=min_replies
                    )
                    is_koth = self.engagement_poller.is_koth(mint)
                    if not (is_engaged or is_koth):
                        rej["engagement"] += 1
                        continue
                    # Age gate from frontend created_timestamp (already ms epoch)
                    created_ts = int(info.get("created_timestamp") or 0)
                    if created_ts <= 0:
                        rej["age"] += 1
                        continue
                    age_ms = ts_ms - created_ts
                    if age_ms < min_age_ms or age_ms > max_age_ms:
                        rej["age"] += 1
                        continue
                    # Phase 20D: optionally hard-filter out post-migration tokens
                    # (the losing cluster from the first 3-trade signal)
                    if require_fresh and bool(info.get("complete")):
                        rej["complete"] += 1
                        continue
                    # RugCheck gate
                    rug_safe = True
                    rug_score = 0
                    rug_reason = "skipped"
                    rugchecker = getattr(self, "rugcheck_client", None)
                    if rugchecker is not None and env_bool("PGG2_RUGCHECK_GATE_ENABLED", True):
                        try:
                            rug_safe, rug_score, rug_reason = rugchecker.is_safe_sync(mint)
                        except Exception:
                            rug_safe = True
                            rug_reason = "exception_failopen"
                        if not rug_safe:
                            log(f"PGG2-POLL-RUGCHECK-REJECT mint={mint[:8]} score={rug_score} reason={rug_reason}")
                            self.engagement_driven_seen.add(mint)
                            rej["rugcheck"] += 1
                            continue
                    # Fetch on-chain price
                    price, complete, vsol_sol = self._engagement_poll_price(mint)
                    if price <= 0:
                        log(f"PGG2-POLL-NO-PRICE mint={mint[:8]} complete={complete}")
                        rej["no_price"] += 1
                        continue

                    viewers = int(info.get("num_participants") or 0)
                    replies = int(info.get("reply_count") or 0)
                    is_currently_live = bool(info.get("is_currently_live"))
                    usd_mc = float(info.get("usd_market_cap") or 0)
                    score = 250.0 + min(50.0, viewers * 1.5) + min(30.0, replies * 2.0) + (50.0 if is_koth else 0.0)
                    reason = (
                        f"engagement_poll viewers={viewers} replies={replies} "
                        f"live={is_currently_live} koth={is_koth} complete={complete} "
                        f"mc=${usd_mc:.0f} rug={rug_score} age={age_ms // 1000}s"
                    )
                    snap_features: dict[str, Any] = self._engagement_synth_features(
                        ts_ms=ts_ms,
                        price=price,
                        complete=complete,
                        vsol_sol=vsol_sol,
                        age_ms=age_ms,
                    )
                    snap_features.update({
                        "engagement_viewers": viewers,
                        "engagement_replies": replies,
                        "engagement_currently_live": is_currently_live,
                        "engagement_koth": is_koth,
                        "engagement_usd_mc": usd_mc,
                        "engagement_poll_driven": True,
                        "rugcheck_score": rug_score,
                        "rugcheck_reason": rug_reason,
                        "entry_size_reason": "engagement_poll",
                        "entry_probe_sol": full_scout,
                    })
                    plan = StrikePlan(
                        mint=mint,
                        ts_ms=ts_ms,
                        lane="engagement_driven",
                        reason=reason,
                        score=score,
                        scout_sol=full_scout,
                        target_sol=full_scout,
                        price=price,
                        needs_curve_fill=False,
                        features=snap_features,
                    )
                    ok, can_reason = self.broker.can_strike(mint, ts_ms)
                    if not ok:
                        self.logger.decision(
                            "strike_skipped",
                            mint,
                            {"reason": can_reason, "lane": plan.lane, "features": snap_features},
                        )
                        continue
                    self.logger.decision(
                        "strike_plan",
                        mint,
                        {
                            "lane": plan.lane,
                            "reason": plan.reason,
                            "score": plan.score,
                            "scout_sol": plan.scout_sol,
                            "target_sol": plan.target_sol,
                            "needs_curve_fill": plan.needs_curve_fill,
                            "features": plan.features,
                        },
                    )
                    pos = self.broker.queue_or_fill(plan, price)
                    self.engagement_driven_seen.add(mint)
                    self._engagement_seen_ts[mint] = ts_ms
                    if pos:
                        self.init_position_follow(pos, trusted=True, entry_features=snap_features)
                        self.logger.decision("open", mint, {"lane": plan.lane, "features": snap_features})
                        log(
                            f"PGG2-POLL-STRIKE OPENED mint={mint[:8]} price={price:.6e} "
                            f"viewers={viewers} replies={replies} koth={int(is_koth)} "
                            f"complete={int(complete)} age={age_ms // 1000}s"
                        )
                    fired += 1
                if iter_count % max(1, diag_every) == 0:
                    log(
                        f"PHASE20D-DIAG iter={iter_count} engaged={len(engaged)} "
                        f"considered={considered} fired={fired} rej_dup={rej['dup']} "
                        f"rej_engage={rej['engagement']} rej_age={rej['age']} "
                        f"rej_rug={rej['rugcheck']} rej_noprice={rej['no_price']} "
                        f"rej_complete={rej['complete']}"
                    )
            except asyncio.CancelledError:
                log("PHASE20C: engagement_poll_strike_loop cancelled")
                return
            except Exception as exc:
                log(f"PHASE20C: engagement_poll_strike_loop error {type(exc).__name__}: {exc}")
                await asyncio.sleep(2.0)

    async def engagement_manage_loop(self) -> None:
        """Phase 20C 2026-05-08: manage open engagement_driven positions.

        Poll-driven entries don't have a tape (no shred events flow), so the
        regular heartbeat_loop's feature_snapshot returns None for them and
        manage_position never runs. This loop fetches fresh on-chain prices
        for engagement_driven positions and applies the tight 10/-5/60s exit
        logic directly.
        """
        if not env_bool("PGG2_ENGAGEMENT_MANAGE_LOOP_ENABLED", True):
            return
        if not hasattr(self.broker, "bonding_curve"):
            return
        manage_sec = env_float("PGG2_ENGAGEMENT_MANAGE_SEC", 3.0)
        log(f"PHASE20C: engagement_manage_loop starting poll={manage_sec}s")
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(manage_sec)
                ts_ms = now_ms()
                for mint, pos in list(self.broker.positions.items()):
                    if pos.lane != "engagement_driven":
                        continue
                    # Skip if heartbeat_loop already manages this one (tape exists)
                    tape = self.tapes.get(mint)
                    if tape and tape.last_price > 0:
                        continue
                    price, complete, vsol_sol = self._engagement_poll_price(mint)
                    if price <= 0:
                        continue
                    age_ms = ts_ms - int(pos.opened_ts_ms)
                    features = self._engagement_synth_features(
                        ts_ms=ts_ms,
                        price=price,
                        complete=complete,
                        vsol_sol=vsol_sol,
                        age_ms=age_ms,
                    )
                    features["engagement_poll_driven"] = True
                    await self.manage_position(pos, ts_ms, price, features)
            except asyncio.CancelledError:
                log("PHASE20C: engagement_manage_loop cancelled")
                return
            except Exception as exc:
                log(f"PHASE20C: engagement_manage_loop error {type(exc).__name__}: {exc}")
                await asyncio.sleep(2.0)

    async def run(self) -> None:
        if not self.config.paper_trading and not self.config.live_enabled:
            raise RuntimeError("Live execution is gated. Set PGG2_EXECUTION_MODE=quote first, then explicit live gates.")
        execution_mode = env_str("PGG2_EXECUTION_MODE", "paper").lower()
        mode = "DRY_LIVE" if execution_mode == "dry_live" else ("PAPER" if self.config.paper_trading else execution_mode.upper())
        log(
            f"PIGGY: starting {mode} scout={self.config.scout_sol:.4f} max_pos={self.config.max_position_sol:.4f} "
            f"cluster_age={self.config.birth_max_age_ms}ms max_open={self.config.max_open_positions}"
        )
        await super().run()

    def inject_replay_curve(self, row: dict[str, Any], ts_ms: int) -> None:
        curve = row.get("bonding_curve") or ""
        price = float(row.get("curve_price") or 0.0)
        vsol_sol = float(row.get("vsol_sol") or 0.0)
        if not curve or price <= 0:
            return
        vsol_lamports = max(1, int(vsol_sol * 1_000_000_000))
        vtoken = max(1, int(vsol_lamports / price))
        self.bc.by_curve[curve].append(CurvePoint(ts_ms, vsol_lamports, vtoken, bool(row.get("complete"))))
        self.bc.updates += 1

    @staticmethod
    def row_to_event(row: dict[str, Any], ts_ms: int) -> PumpEvent:
        side = row.get("side") or ""
        return PumpEvent(
            ts_ms=ts_ms,
            recv_ns=now_ns(),
            sig=str(row.get("sig") or ""),
            slot=int(row.get("slot") or 0),
            signer=str(row.get("signer") or ""),
            kind=str(row.get("kind") or "trade"),
            mint=str(row.get("mint") or ""),
            bonding_curve=str(row.get("bonding_curve") or ""),
            is_buy=side == "buy",
            sol_lamports=int(float(row.get("sol") or 0.0) * 1_000_000_000),
            token_amount=int(float(row.get("token_amount") or 0.0)),
            user=str(row.get("user") or ""),
            creator=str(row.get("creator") or ""),
            create_version=str(row.get("create_version") or ""),
            is_mayhem=bool(row.get("is_mayhem") or False),
            tracked=bool(row.get("tracked") or False),
            instruction_kind=str(row.get("instruction_kind") or side),
        )

    async def replay_raw_log(self, path: Path) -> None:
        log(f"PIGGY-REPLAY: reading {path}")
        first_ts: Optional[int] = None
        rows = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                src_ts = int(row.get("ts_ms") or now_ms())
                if first_ts is None:
                    first_ts = src_ts
                ts_ms = now_ms() + (src_ts - first_ts)
                event = self.row_to_event(row, ts_ms)
                if not event.mint:
                    continue
                if event.bonding_curve:
                    self.bc.remember(event.mint, event.bonding_curve)
                self.inject_replay_curve(row, ts_ms)
                await self.on_event(event)
                await self.heartbeat_once(ts_ms)
                rows += 1
        for _ in range(150):
            await self.heartbeat_once(now_ms())
        for mint, pos in list(self.broker.positions.items()):
            features = self.feature_snapshot(mint, now_ms())
            price = float(features.get("price") or pos.last_price) if features else pos.last_price
            if features and price > 0:
                self.close_position(mint, now_ms(), price, "replay_end_mark", features, killed=False)
            elif price > 0:
                self.broker.close(mint, now_ms(), price, "replay_end_mark", killed=False)
        self.report_if_due(force=True)
        self.broker.save_state()
        log(f"PIGGY-REPLAY: rows={rows} state={self.config.state_file} decisions={self.config.decisions_file}")

    async def heartbeat_once(self, ts: int) -> None:
        for mint in list(self.broker.pending.keys()):
            features = self.feature_snapshot(mint, ts)
            price = float(features.get("price") or 0.0) if features else 0.0
            pending = self.broker.pending.get(mint)
            if price > 0 and features and pending:
                ready, why = self.pending_fill_ready(pending, features)
                if ready:
                    pos = self.broker.fill_pending(mint, ts, price)
                    if pos:
                        self.logger.decision("pending_fill", mint, {"reason": why, "features": self.slim_features(features)})
                    continue
            if pending and ts >= pending.expires_ts_ms:
                reason = "no_curve_price" if price <= 0 else "cluster_expired"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Same-block piggyback paper bot for pump.fun")
    parser.add_argument("--ws", default="", help="Solana Tracker RPC WebSocket URL")
    parser.add_argument("--state", default="", help="State JSON path")
    parser.add_argument("--raw-log", default="", help="Raw event JSONL path")
    parser.add_argument("--decisions", default="", help="Decision JSONL path")
    parser.add_argument("--snipers", default="", help="Tracked wallet file for bonus only")
    parser.add_argument("--run-seconds", type=float, default=0.0, help="Stop after N seconds; 0 runs forever")
    parser.add_argument("--print-events", action="store_true", help="Print every parsed create/trade")
    parser.add_argument("--replay-raw", default="", help="Replay an existing raw-event JSONL instead of opening WS")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config = piggy_config(args)
    bot = SameBlockPiggybackBot(config)
    if args.replay_raw:
        await bot.replay_raw_log(Path(args.replay_raw))
        return
    await bot.run()


if __name__ == "__main__":
    asyncio.run(async_main())
