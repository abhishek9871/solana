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
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from birth_first_sniper_og_drylive import (
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


def piggy_config(args: argparse.Namespace) -> BaseConfig:
    load_dotenv()
    execution_mode = env_str("PGG2_EXECUTION_MODE", "paper").lower()
    paper_mode = execution_mode in {"paper", "dry_live"} and env_bool("PIGGY_PAPER_TRADING", True)
    base = BaseConfig.from_env(args)
    return replace(
        base,
        paper_trading=paper_mode,
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
        self.wave_arms: dict[str, WaveArm] = {}
        self.position_follow: dict[str, dict[str, Any]] = {}
        self.profitable_closes: dict[str, dict[str, float]] = {}
        self.breadth_ignition_seen: set[str] = set()
        self.birth_fanout_seen: set[str] = set()
        self.curve_lag_reveal_seen: set[str] = set()

    @staticmethod
    def moonshot_lane(lane: str) -> bool:
        return lane in {
            "second_wave_after_cluster",
            "reclaim_wave",
            "early_ignition",
            "late_ignition",
            "breadth_ignition",
            "birth_fanout",
            "curve_lag_reveal",
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
        if event.ts_ms < int(follow["opened_ts_ms"]) + grace_ms:
            return
        min_sig_sol = env_float("PIGGY_FOLLOW_SIG_BUY_SOL", 0.08)
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
        post1500 = self.event_window_stats(mint, first_price_ts, min(ts_ms, first_price_ts + 1500))
        return {
            "first_price_ts": first_price_ts,
            "first_price": first_price,
            "first_price_delay_ms": first_price_delay_ms,
            "first_price_age_ms": first_price_age_ms,
            "entry_move_from_first": price / max(first_price, 1e-18),
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
        plan = self.birth_fanout_ready(event, features)
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

        This is not a second-wave parameter tweak. It targets the blind spot
        found in raw tapes: first usable price appears after launch-bundle
        activity, then many distinct buyers fan out inside the first ~1.5s.
        The existing wave/reclaim logic often never strikes these mints.
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
        if ctx["first_price_delay_ms"] > env_int("PGG2_BIRTH_FANOUT_MAX_FIRST_PRICE_DELAY_MS", 5000):
            return None
        if ctx["first_price_age_ms"] > env_int("PGG2_BIRTH_FANOUT_MAX_FIRST_PRICE_AGE_MS", 1500):
            return None
        if ctx["entry_move_from_first"] > env_float("PGG2_BIRTH_FANOUT_MAX_ENTRY_MOVE", 1.50):
            return None
        if ctx["post1500_buy_sol"] < env_float("PGG2_BIRTH_FANOUT_MIN_BUY_SOL", 3.0):
            return None
        if ctx["post1500_unique_buyers"] < env_int("PGG2_BIRTH_FANOUT_MIN_BUYERS", 8):
            return None
        if ctx["post1500_top_share"] > env_float("PGG2_BIRTH_FANOUT_MAX_TOP_SHARE", 0.50):
            return None
        if ctx["post1500_sell_sol"] > max(
            0.010,
            ctx["post1500_buy_sol"] * env_float("PGG2_BIRTH_FANOUT_MAX_SELL_RATIO", 0.08),
        ):
            return None

        # Live tape filter: birth-fanout winners had sustained 700ms breadth at
        # entry, while most losses were thin or top-heavy in the immediate tape.
        s700_live = features.get("s700") or {}
        live_unique700 = int(s700_live.get("unique_buyers") or 0)
        live_top700 = float(s700_live.get("top_buy_share") or 1.0)
        if live_unique700 < env_int("PGG2_BIRTH_FANOUT_MIN_LIVE_BUYERS700", 7):
            return None
        if live_top700 > env_float("PGG2_BIRTH_FANOUT_MAX_LIVE_TOP700", 0.62):
            return None

        scout = env_float("PGG2_BIRTH_FANOUT_SOL", max(0.0005, self.config.scout_sol * 0.50))
        scout = min(self.config.max_position_sol, max(0.0005, scout))
        score = (
            120.0
            + min(60.0, ctx["post1500_buy_sol"] * 5.0)
            + min(45.0, ctx["post1500_unique_buyers"] * 4.0)
            + max(0.0, ctx["entry_move_from_first"] - 1.0) * 80.0
            - max(0.0, ctx["post1500_top_share"] - 0.35) * 55.0
        )
        reason = (
            f"birth_fanout first_age={ctx['first_price_age_ms']}ms "
            f"b1500={ctx['post1500_buy_sol']:.3f}/{ctx['post1500_unique_buyers']} "
            f"top={ctx['post1500_top_share']:.2f} live700={live_unique700} top={live_top700:.2f} "
            f"move={ctx['entry_move_from_first']:.2f}x"
        )
        plan = StrikePlan(
            mint=event.mint,
            ts_ms=event.ts_ms,
            lane="birth_fanout",
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
                "birth_fanout": ctx,
                "birth_fanout_live_unique700": live_unique700,
                "birth_fanout_live_top700": live_top700,
                "entry_size_reason": "birth_fanout_probe",
                "entry_probe_sol": scout,
            }
        )
        return plan

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
        if not event.is_buy or event.mint in self.curve_lag_reveal_seen:
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

    async def maybe_plan_strike(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        ts_ms = event.ts_ms
        features = self.feature_snapshot(event.mint, ts_ms)
        if not features:
            return
        self.maybe_arm_first_burst(event, features)
        features = self.feature_snapshot(event.mint, ts_ms) or features
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
        super().close_position(mint, ts_ms, price, reason, features, killed)
        pnl = self.broker.stats.realized_pnl_sol - before_pnl
        if pnl > env_float("PIGGY_PROFIT_REENTRY_MIN_PNL_SOL", 0.0):
            self.profitable_closes[mint] = {
                "ts_ms": float(ts_ms),
                "pnl_sol": pnl,
                "peak_mult": float(self.broker.stats.best_mult),
            }
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
        mult = pos.update(price)
        self.broker.stats.best_mult = max(self.broker.stats.best_mult, pos.peak_mult)
        self.add_follow_features(pos, features)

        kill = self.piggy_kill_reason(pos, features)
        if kill:
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
                if pos.peak_mult >= 1.08 and sell_pressure and mult >= 1.02:
                    self.close_position(mint, ts_ms, price, "first_pop_sell_exit", features, killed=False)
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
                if (
                    pos.lane == "birth_fanout"
                    and pos.age_sec(ts_ms) >= env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_AFTER_SEC", 4.0)
                    and pos.peak_mult < env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_MIN_PEAK", 1.06)
                    and mult <= env_float("PGG2_BIRTH_FANOUT_NO_FOLLOW_MULT", 0.99)
                    and (
                        not features["flow_live"]
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

    @staticmethod
    def piggy_kill_reason(pos: Any, features: dict[str, Any]) -> Optional[str]:
        s250 = features["s250"]
        s700 = features["s700"]
        if SameBlockPiggybackBot.moonshot_lane(pos.lane):
            age_sec = pos.age_sec(features["ts_ms"])
            full_sized_entry = pos.target_sol > 0 and pos.scout_sol >= (
                pos.target_sol * env_float("PIGGY_FULL_SIZE_SCOUT_TARGET_RATIO", 0.95)
            )
            if (
                pos.state == "SCOUT"
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
            if pos.last_mult <= env_float("PIGGY_MOON_HARD_BREAK_MULT", 0.88):
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

    async def run(self) -> None:
        if not self.config.paper_trading:
            raise RuntimeError("Live execution is gated. Validate paper/replay first, then wire the Raptor executor.")
        mode = "DRY_LIVE" if env_str("PGG2_EXECUTION_MODE", "paper").lower() == "dry_live" else "PAPER"
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
