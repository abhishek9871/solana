"""
Same Block Piggyback Bot — CORRECTED for $36 Capital

CHANGES FROM ORIGINAL (marked with # FIX: or # NEW: throughout):
  1. Bankroll-aware position sizing (2-4% per trade, max 8%)
  2. Only 3 lanes kept: birth_fanout, curve_lag_reveal, early_ignition
  3. Entry filters dramatically tightened (more buyers, more SOL, less concentration)
  4. Hard stop loss at -12% per position (0.88x) with no grace periods
  5. Trailing stop for winners (lets moonshots run)
  6. Loss circuit breaker (stops trading after consecutive losses)
  7. All scaling DISABLED for small capital
  8. Faster time stops (8s no-pop kill, 45s hard limit)
  9. Session PnL tracking and daily loss limit
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


# ---------------------------------------------------------------------------
# FIX: Completely rewritten config defaults for $36 (0.21 SOL) bankroll
# ---------------------------------------------------------------------------
def piggy_config(args: argparse.Namespace) -> BaseConfig:
    load_dotenv()
    base = BaseConfig.from_env(args)
    execution_mode = env_str("PGG2_EXECUTION_MODE", "paper").lower()
    paper_mode = execution_mode in {"paper", "dry_live"} and env_bool("PIGGY_PAPER_TRADING", True)

    # FIX: Detect bankroll from env or default to 0.21 SOL ($36)
    bankroll_sol = env_float("PIGGY_BANKROLL_SOL", 0.21)

    # FIX: Position sizing relative to bankroll
    # Probe: 2% of bankroll, Max position: 6% of bankroll
    probe_sol = max(0.003, bankroll_sol * env_float("PIGGY_PROBE_BANKROLL_PCT", 0.02))
    max_pos_sol = max(0.005, bankroll_sol * env_float("PIGGY_MAX_POS_BANKROLL_PCT", 0.06))

    return replace(
        base,
        paper_trading=paper_mode,
        live_enabled=execution_mode in {"quote", "live"} and env_bool("PGG2_ENABLE_LIVE", False),
        report_sec=env_float("PIGGY_REPORT_SEC", 3.0),
        heartbeat_sec=env_float("PIGGY_HEARTBEAT_SEC", 0.020),
        curve_max_age_ms=env_int("PIGGY_CURVE_MAX_AGE_MS", 650),
        max_tape_age_sec=env_int("PIGGY_MAX_TAPE_AGE_SEC", 90),
        # FIX: Tiny scout for small bankroll
        scout_sol=env_float("PIGGY_SCOUT_SOL", probe_sol),
        # FIX: Tiny max position for small bankroll
        max_position_sol=env_float("PIGGY_MAX_POSITION_SOL", max_pos_sol),
        # FIX: Only 1 open position at a time with $36
        max_open_positions=env_int("PIGGY_MAX_OPEN_POSITIONS", 1),
        # FIX: Fewer pending strikes — be selective
        max_pending_strikes=env_int("PIGGY_MAX_PENDING_STRIKES", 2),
        # FIX: Longer cooldown between strikes for selectivity
        min_seconds_between_strikes=env_float("PIGGY_MIN_SECONDS_BETWEEN_STRIKES", 0.5),
        # FIX: Longer cooldown after any trade
        cooldown_sec=env_float("PIGGY_COOLDOWN_SEC", 15.0),
        paper_drag_bps=env_float("PIGGY_PAPER_DRAG_BPS", 280.0),
        birth_max_age_ms=env_int("PIGGY_MAX_AGE_MS", 1350),
        first_buy_max_age_ms=env_int("PIGGY_FIRST_BUY_MAX_AGE_MS", 900),
        pending_fill_ttl_ms=env_int("PIGGY_PENDING_FILL_TTL_MS", 650),
        # FIX: Tighter first-buy range to filter noise
        first_buy_min_sol=env_float("PIGGY_FIRST_BUY_MIN_SOL", 0.50),
        first_buy_max_sol=env_float("PIGGY_FIRST_BUY_MAX_SOL", 4.00),
        two_wallet_buy_sol=env_float("PIGGY_MIN_BUY_700_SOL", 1.20),
        velocity_buy_sol=env_float("PIGGY_MIN_BUY_1200_SOL", 1.75),
        max_initial_sell_ratio=env_float("PIGGY_MAX_INITIAL_SELL_RATIO", 0.04),
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
        self.preprice_reveal_seen: set[str] = set()
        self.priced_snap_seen: set[str] = set()
        self.priced_breakout_watch: dict[str, dict[str, Any]] = {}
        self.priced_breakout_seen: set[str] = set()
        self.late_swarm_seen: set[str] = set()
        self.curve_arm_scout_seen: set[str] = set()
        self.raw_momentum_seen: set[str] = set()
        self.raw_momentum_arms: dict[str, dict[str, Any]] = {}
        self.whale_spark_seen: set[str] = set()

        # NEW: Bankroll and loss tracking
        self.bankroll_sol = env_float("PIGGY_BANKROLL_SOL", 0.21)
        self.session_start_sol = self.bankroll_sol
        self.session_pnl_sol = 0.0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.last_loss_ts_ms = 0
        self.last_win_ts_ms = 0
        self.daily_loss_sol = 0.0
        self.trades_today = 0
        self.circuit_breaker_active = False
        self.circuit_breaker_until_ms = 0

    # NEW: Bankroll-aware position sizing
    def bankroll_sized_sol(self, conviction: str = "probe") -> float:
        """Return position size in SOL based on current bankroll and conviction level.

        conviction: 'probe' (2%), 'standard' (4%), 'high' (6%), 'max' (8%)
        """
        pct_map = {
            "probe": env_float("PIGGY_PROBE_BANKROLL_PCT", 0.02),
            "standard": env_float("PIGGY_STANDARD_BANKROLL_PCT", 0.04),
            "high": env_float("PIGGY_HIGH_BANKROLL_PCT", 0.06),
            "max": env_float("PIGGY_MAX_BANKROLL_PCT", 0.08),
        }
        pct = pct_map.get(conviction, 0.02)

        # Scale down after consecutive losses
        if self.consecutive_losses >= 2:
            pct *= env_float("PIGGY_LOSS_SCALE_FACTOR", 0.50)
        if self.consecutive_losses >= 4:
            pct *= env_float("PIGGY_DEEP_LOSS_SCALE_FACTOR", 0.25)

        sol = max(0.003, self.bankroll_sol * pct)
        return min(sol, self.config.max_position_sol)

    # NEW: Circuit breaker — stop trading after too many losses
    def check_circuit_breaker(self, ts_ms: int) -> tuple[bool, str]:
        """Return (allowed_to_trade, reason)."""
        # Check time-based circuit breaker
        if self.circuit_breaker_active:
            if ts_ms < self.circuit_breaker_until_ms:
                remaining_sec = (self.circuit_breaker_until_ms - ts_ms) / 1000.0
                return False, f"circuit_breaker_active {remaining_sec:.0f}s remaining"
            self.circuit_breaker_active = False

        # Daily loss limit
        daily_limit = self.bankroll_sol * env_float("PIGGY_DAILY_LOSS_PCT", 0.20)
        if self.daily_loss_sol <= -daily_limit:
            return False, f"daily_loss_limit {self.daily_loss_sol:.4f} <= -{daily_limit:.4f}"

        # Consecutive loss circuit breaker
        if self.consecutive_losses >= env_int("PIGGY_CIRCUIT_BREAKER_LOSSES", 4):
            cooldown_ms = env_int("PIGGY_CIRCUIT_BREAKER_COOLDOWN_MS", 300000)  # 5 min
            if ts_ms - self.last_loss_ts_ms < cooldown_ms:
                return False, f"consecutive_loss_breaker {self.consecutive_losses} losses"

        return True, "ok"

    # NEW: Trailing stop multiplier based on peak
    @staticmethod
    def trailing_stop_mult(peak_mult: float) -> float:
        """Return the trailing stop as a fraction of peak_mult.

        At 1.5x peak: trail at 0.88 of peak (exit if drops below 1.32x)
        At 2.0x peak: trail at 0.82 of peak (exit if drops below 1.64x)
        At 3.0x peak: trail at 0.75 of peak (exit if drops below 2.25x)
        At 5.0x peak: trail at 0.70 of peak (exit if drops below 3.50x)
        """
        if peak_mult >= 5.0:
            return env_float("PIGGY_TRAIL_5X", 0.70)
        if peak_mult >= 3.0:
            return env_float("PIGGY_TRAIL_3X", 0.75)
        if peak_mult >= 2.0:
            return env_float("PIGGY_TRAIL_2X", 0.82)
        if peak_mult >= 1.5:
            return env_float("PIGGY_TRAIL_15X", 0.88)
        return 0.0  # No trailing stop below 1.5x

    @staticmethod
    def moonshot_lane(lane: str) -> bool:
        # FIX: Only keep the 3 proven early-entry lanes
        return lane in {
            "birth_fanout",
            "curve_lag_reveal",
            "early_ignition",
            # Legacy lanes kept for compatibility but will be disabled in build_strike_plan
            "second_wave_after_cluster",
            "reclaim_wave",
            "late_ignition",
            "breadth_ignition",
            "stealth_arm",
            "spark3_arm",
            "spark3_breakout",
            "preprice_reveal",
            "priced_snap",
            "priced_breakout",
            "late_swarm",
            "curve_arm_scout",
            "raw_momentum",
            "whale_spark",
        }

    # ACTIVE_LANES: Only these 3 lanes will generate strikes
    ACTIVE_LANES = {"birth_fanout", "curve_lag_reveal", "early_ignition"}

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
        # FIX: Use bankroll-aware sizing instead of fixed config
        return self.bankroll_sized_sol("probe")

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
        grace_ms = env_int("PIGGY_FOLLOW_GRACE_MS", 500)  # FIX: Reduced from 1000
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
        # FIX: Tighter arm requirements — need more buyers and lower concentration
        broad_cluster = (
            s700["unique_buyers"] >= env_int("PIGGY_ARM_MIN_BUYERS_700", 5)  # was 3
            and s700["buy_sol"] >= env_float("PIGGY_ARM_MIN_BUY_SOL_700", 2.00)  # was 1.20
            and s700["top_buy_share"] <= env_float("PIGGY_ARM_MAX_TOP_SHARE", 0.55)  # was 0.74
        )
        same_slot = (
            features["slot_buyers"] >= env_int("PIGGY_ARM_MIN_SLOT_BUYERS", 5)  # was 3
            and features["slot_buy_sol"] >= env_float("PIGGY_ARM_MIN_SLOT_SOL", 2.00)  # was 1.20
            and features["slot_top_share"] <= env_float("PIGGY_ARM_MAX_SLOT_TOP_SHARE", 0.55)  # was 0.76
        )
        if not (broad_cluster or same_slot):
            return
        # FIX: Stricter sell ratio for arming
        if s1500["sell_sol"] > max(0.005, s1500["buy_sol"] * env_float("PIGGY_ARM_MAX_SELL_RATIO", 0.04)):  # was 0.08
            return
        if features["off_peak"] < env_float("PIGGY_ARM_MIN_OFF_PEAK", 0.93):  # was 0.91
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
        # FIX: Disable second_wave lane entirely — too late for small capital
        if not env_bool("PGG2_SECOND_WAVE_ENABLED", False):
            return None, "second_wave_disabled"
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
        sells_ok = fresh_sells <= max(0.010, fresh_buy * env_float("PIGGY_SECOND_MAX_FRESH_SELL_RATIO", 0.10))  # FIX: was 0.15
        breadth_ok = (
            fresh_unique >= env_int("PIGGY_SECOND_MIN_FRESH_BUYERS", 4)  # FIX: was 2
            and fresh_buy >= env_float("PIGGY_SECOND_MIN_FRESH_SOL", 2.00)  # FIX: was 1.15
            and fresh700["fresh_top_share"] <= env_float("PIGGY_SECOND_MAX_FRESH_TOP_SHARE", 0.65)  # FIX: was 0.84
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
            s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) <= env_float("PIGGY_SECOND_MAX_TAPE_SELL_RATIO", 0.15)  # FIX: was 0.22
            and s700["top_buy_share"] <= env_float("PIGGY_SECOND_MAX_TOP_SHARE_700", 0.65)  # FIX: was 0.88
            and features["last_buy_age_ms"] <= env_int("PIGGY_SECOND_MAX_LAST_BUY_AGE_MS", 180)  # FIX: was 220
        )
        if not sells_ok:
            return None, "fresh_sells"
        if fresh700["old_buyer_buy_sol"] > max(
            env_float("PIGGY_SECOND_MAX_OLD_BUYER_SOL", 0.60),
            fresh_buy * env_float("PIGGY_SECOND_MAX_OLD_BUYER_MULT", 1.0),
        ) and not reclaim_strength:
            return None, "old_cluster_dominates"
        if (
            fresh700["fresh_top_share"] > env_float("PIGGY_SECOND_SOFT_TOP_SHARE", 0.60)  # FIX: was 0.75
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
        # FIX: Use bankroll-aware sizing
        scout = self.bankroll_sized_sol("probe")
        full_entry_sol = self.bankroll_sized_sol("max")
        size_reason = str(features.get("entry_size_reason") or full_reason or "probe_then_confirm")
        if full_reason:
            scout = full_entry_sol
            size_reason = full_reason
        if size_reason == "probe_late_reclaim_confirm":
            scout = min(scout, self.bankroll_sized_sol("probe") * 0.50)
        features["entry_size_reason"] = size_reason
        features["entry_probe_sol"] = scout
        quality = max(0.0, min(1.0, (score - 90.0) / 55.0))
        target = scout  # FIX: No scaling — target equals scout for small capital
        if base_move >= 1.30 and fresh1500["fresh_buy_sol"] >= 1.25:
            target = max(target, self.bankroll_sized_sol("high"))
        target = min(self.config.max_position_sol, max(scout, target))
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

    # ---------------------------------------------------------------------------
    # FIX: build_strike_plan — Only use the 3 proven early-entry lanes
    # All other lanes disabled for small capital
    # ---------------------------------------------------------------------------
    def build_strike_plan(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        # NEW: Circuit breaker check — don't enter new trades if on a losing streak
        allowed, cb_reason = self.check_circuit_breaker(event.ts_ms)
        if not allowed:
            return None

        self.maybe_arm_first_burst(event, features)

        # FIX: Only 3 active lanes for small capital. Each is an early-entry
        # lane that catches moonshots BEFORE the price has moved significantly.
        # Late-entry lanes (breakout, swarm, reclaim, wave) are disabled because
        # by the time they trigger, insiders are already selling.

        # Lane 1: birth_fanout — broad launch buying with fresh follow-through
        plan = self.birth_fanout_ready(event, features)
        if plan:
            return plan

        # Lane 2: curve_lag_reveal — token armed before price, buys on first priced breadth
        plan = self.curve_lag_reveal_ready(event, features)
        if plan:
            return plan

        # Lane 3: early_ignition — armed token with immediate markup and breadth
        plan = self.early_ignition_ready(event, features)
        if plan:
            return plan

        # ALL OTHER LANES DISABLED for small capital:
        # - second_wave: too late, insiders already positioned
        # - reclaim_wave: catching falling knives
        # - late_ignition: too late for small bankroll
        # - breadth_ignition: too noisy, too many false positives
        # - stealth_arm: experimental, not proven profitable
        # - spark3_arm/breakout: experimental
        # - preprice_reveal: requires 14+ SOL pre-buy (rare)
        # - priced_snap: late entry after 1.18x+ move
        # - priced_breakout: late entry after 1.35x+ move
        # - late_swarm: entering at 20-180 seconds is way too late
        # - curve_arm_scout: unproven
        # - raw_momentum: unproven
        # - whale_spark: depends on single whale, too risky

        return None

    # ---------------------------------------------------------------------------
    # FIX: birth_fanout_ready — DRAMATICALLY tightened filters
    # This is the #1 lane. We require massive breadth and near-zero sells.
    # ---------------------------------------------------------------------------
    def birth_fanout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
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
        confirm_ms = env_int("PGG2_BIRTH_FANOUT_CONFIRM_MS", 300)  # FIX: was 250
        max_confirm_ms = env_int("PGG2_BIRTH_FANOUT_CONFIRM_MAX_MS", 900)  # FIX: was 850
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
            # FIX: Stricter confirmation requirements
            if (
                confirm_buy < env_float("PGG2_BIRTH_FANOUT_CONFIRM_MIN_BUY_SOL", 1.00)  # was 0.50
                or confirm_unique < env_int("PGG2_BIRTH_FANOUT_CONFIRM_MIN_BUYERS", 3)  # was 1
                or confirm_top > env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_TOP_SHARE", 0.55)  # was 1.0
                or confirm_sell > max(
                    env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_SELL_SOL", 0.030),  # was 0.075
                    confirm_buy * env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_SELL_RATIO", 0.04),  # was 0.08
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
            # FIX: Lower max entry move to avoid chasing
            if ctx["entry_move_from_first"] > env_float("PGG2_BIRTH_FANOUT_CONFIRM_MAX_ENTRY_MOVE", 1.45):  # was 1.70
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
            # FIX: Lower max entry move — don't chase
            if ctx["entry_move_from_first"] > env_float("PGG2_BIRTH_FANOUT_MAX_ENTRY_MOVE", 1.30):  # was 1.50
                return None
            wave_base_move = float(features.get("wave_base_move") or 1.0)
            if wave_base_move < env_float("PGG2_BIRTH_FANOUT_MIN_WAVE_BASE_MOVE", 0.45):
                return None
            birth_buy = float(ctx.get("birth1500_buy_sol") or ctx["post1500_buy_sol"])
            birth_unique = int(ctx.get("birth1500_unique_buyers") or ctx["post1500_unique_buyers"])
            birth_top = float(ctx.get("birth1500_top_share") or ctx["post1500_top_share"])
            birth_sell = float(ctx.get("birth1500_sell_sol") or ctx["post1500_sell_sol"])
            # FIX: MUCH tighter birth requirements
            # Original: 9 SOL / 11 buyers / 0.32 top — these are too loose
            # Sybil wallets can easily create 11 buyers with 9 SOL
            # Need: 15+ SOL, 15+ buyers, top share < 0.25, near-zero sells
            if birth_buy < env_float("PGG2_BIRTH_FANOUT_MIN_BUY_SOL", 15.0):  # FIX: was 9.0
                return None
            if birth_unique < env_int("PGG2_BIRTH_FANOUT_MIN_BUYERS", 15):  # FIX: was 11
                return None
            if birth_top > env_float("PGG2_BIRTH_FANOUT_MAX_TOP_SHARE", 0.25):  # FIX: was 0.32
                return None
            if birth_sell > max(
                0.005,  # FIX: was 0.010
                birth_buy * env_float("PGG2_BIRTH_FANOUT_MAX_SELL_RATIO", 0.03),  # FIX: was 0.08
            ):
                return None

            # Elite no-follow profile (immediate entry without confirmation)
            elite_nofollow = (
                env_bool("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_ENABLED", False)
                and birth_buy >= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_BUY_SOL", 18.0)  # FIX: was 11.0
                and birth_unique >= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_BUYERS", 15)  # FIX: was 11
                and birth_top <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_TOP_SHARE", 0.22)  # FIX: was 0.30
                and ctx["first_price_age_ms"] <= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_FIRST_PRICE_AGE_MS", 1300)
                and ctx["entry_move_from_first"] <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_ENTRY_MOVE", 1.10)
                and ctx.get("pre_price_buy_sol", 0.0) >= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_PRE_PRICE_BUY_SOL", 12.0)  # FIX: was 8.0
                and ctx.get("pre_price_unique_buyers", 0) >= env_int("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MIN_PRE_PRICE_BUYERS", 8)  # FIX: was 4
                and ctx.get("pre_price_top_share", 1.0) <= env_float("PGG2_BIRTH_FANOUT_ELITE_NOFOLLOW_MAX_PRE_PRICE_TOP", 0.28)  # FIX: was 0.36
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

        # FIX: Use bankroll-aware sizing
        if ctx.get("birth_entry_profile") == "elite_nofollow":
            scout = self.bankroll_sized_sol("high")
        else:
            scout = self.bankroll_sized_sol("probe")
            full_follow_ok = (
                float(ctx.get("pre_price_buy_sol") or 0.0)
                >= env_float("PGG2_BIRTH_FANOUT_FOLLOW_FULL_MIN_PRE_PRICE_BUY_SOL", 12.0)  # FIX: was 7.5
            )
            if full_follow_ok:
                scout = self.bankroll_sized_sol("standard")
        scout = min(self.config.max_position_sol, max(0.003, scout))
        target = scout  # FIX: No scaling — target = scout for small capital
        score = (
            120.0
            + min(60.0, float(ctx.get("birth1500_buy_sol") or ctx["post1500_buy_sol"]) * 5.0)
            + min(45.0, int(ctx.get("birth1500_unique_buyers") or ctx["post1500_unique_buyers"]) * 4.0)
            + min(35.0, ctx.get("confirm_buy_sol", 0.0) * 8.0)
            + max(0.0, ctx["entry_move_from_first"] - 1.0) * 80.0
            - max(0.0, float(ctx.get("birth1500_top_share") or ctx["post1500_top_share"]) - 0.25) * 55.0  # FIX: was -0.35
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
                "birth_fanout_watch": watch_ctx if 'watch_ctx' in dir() else {},
                "entry_size_reason": "birth_fanout_probe",
                "entry_probe_sol": scout,
            }
        )
        self.birth_fanout_watch.pop(event.mint, None)
        return plan

    # ---------------------------------------------------------------------------
    # FIX: curve_lag_reveal_ready — Tighter filters
    # ---------------------------------------------------------------------------
    def curve_lag_reveal_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        if not env_bool("PGG2_CURVE_LAG_REVEAL_ENABLED", True):
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
        if arm_age_ms > env_int("PGG2_CURVE_LAG_MAX_ARM_AGE_MS", 5000):  # FIX: was 6000
            return None
        if first_price_age_ms > env_int("PGG2_CURVE_LAG_MAX_FIRST_PRICE_AGE_MS", 3000):  # FIX: was 4000
            return None
        # FIX: Require more initial buyers and SOL
        if arm.initial_slot_buyers < env_int("PGG2_CURVE_LAG_MIN_INITIAL_BUYERS", 5):  # was 3
            return None
        if arm.initial_slot_buy_sol < env_float("PGG2_CURVE_LAG_MIN_INITIAL_SOL", 3.0):  # was 2.0
            return None
        if arm.initial_slot_buy_sol > env_float("PGG2_CURVE_LAG_MAX_INITIAL_SOL", 10.0):
            return None
        if arm.initial_slot_top_share > env_float("PGG2_CURVE_LAG_MAX_INITIAL_TOP", 0.55):  # FIX: was 0.70
            return None
        if arm.initial_sell_sol > max(0.005, arm.initial_buy_sol * env_float("PGG2_CURVE_LAG_MAX_INITIAL_SELL_RATIO", 0.02)):  # FIX: was 0.04
            return None

        follow = self.event_window_stats(event.mint, arm.first_price_ts_ms + 1, int(features["ts_ms"]))
        follow_buy_sol = float(follow["buy_sol"])
        follow_unique = int(follow["unique_buyers"])
        follow_top = float(follow["top_buy_share"])
        follow_sell_sol = float(follow["sell_sol"])
        # FIX: Stricter follow-through requirements
        if follow_buy_sol < env_float("PGG2_CURVE_LAG_MIN_FOLLOW_SOL", 7.0):  # was 5.0
            return None
        if follow_unique < env_int("PGG2_CURVE_LAG_MIN_FOLLOW_BUYERS", 4):  # was 2
            return None
        if follow_top > env_float("PGG2_CURVE_LAG_MAX_FOLLOW_TOP", 0.55):  # was 0.75
            return None
        if follow_sell_sol > max(0.005, follow_buy_sol * env_float("PGG2_CURVE_LAG_MAX_FOLLOW_SELL_RATIO", 0.08)):  # was 0.15
            return None

        s700_live = features.get("s700") or {}
        live_buy700 = float(s700_live.get("buy_sol") or 0.0)
        live_unique700 = int(s700_live.get("unique_buyers") or 0)
        live_top700 = float(s700_live.get("top_buy_share") or 1.0)
        # FIX: Stricter live breadth
        if live_buy700 < env_float("PGG2_CURVE_LAG_MIN_LIVE_BUY700_SOL", 7.0):  # was 5.0
            return None
        if live_unique700 < env_int("PGG2_CURVE_LAG_MIN_LIVE_BUYERS700", 6):  # was 5
            return None
        if live_top700 > env_float("PGG2_CURVE_LAG_MAX_LIVE_TOP700", 0.55):  # was 0.70
            return None

        entry_move_from_first = price / max(arm.first_price, 1e-18)
        if entry_move_from_first > env_float("PGG2_CURVE_LAG_MAX_ENTRY_MOVE", 1.20):  # FIX: was 1.25
            return None

        # FIX: Use bankroll-aware sizing
        scout = self.bankroll_sized_sol("probe")
        score = (
            120.0
            + min(55.0, follow_buy_sol * 6.0)
            + min(35.0, follow_unique * 4.0)
            + min(40.0, live_buy700 * 4.0)
            + max(0.0, entry_move_from_first - 1.0) * 70.0
            - max(0.0, follow_top - 0.35) * 45.0  # FIX: was -0.45
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
            target_sol=scout,  # FIX: No scaling
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

    # ---------------------------------------------------------------------------
    # FIX: early_ignition_ready — Tighter filters, bankroll sizing
    # ---------------------------------------------------------------------------
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
        if arm_age_ms > env_int("PGG2_EARLY_MAX_ARM_AGE_MS", 2000):  # FIX: was 2300
            return None
        if age_ms > env_int("PGG2_EARLY_MAX_TOKEN_AGE_MS", 3500):  # FIX: was 4200
            return None
        arm = self.wave_arms.get(event.mint)
        if not arm or price <= 0 or arm.first_price <= 0:
            return None
        base_move = price / max(arm.first_price, 1e-18)
        if base_move < env_float("PGG2_EARLY_MIN_BASE_MOVE", 1.08):
            return None
        if base_move > env_float("PGG2_EARLY_MAX_BASE_MOVE", 1.50):  # FIX: was 1.80
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        # FIX: Require more breadth
        if s700["buy_sol"] < env_float("PGG2_EARLY_MIN_BUY700_SOL", 3.00):  # was 2.00
            return None
        if s700["unique_buyers"] < env_int("PGG2_EARLY_MIN_BUYERS700", 5):  # was 3
            return None
        if s700["top_buy_share"] > env_float("PGG2_EARLY_MAX_TOP700", 0.55):  # was 0.70
            return None
        if features["slot_buyers"] < env_int("PGG2_EARLY_MIN_SLOT_BUYERS", 4):  # was 3
            return None
        if features["slot_buy_sol"] < env_float("PGG2_EARLY_MIN_SLOT_SOL", 2.0):  # was 1.0
            return None
        if features["slot_top_share"] > env_float("PGG2_EARLY_MAX_SLOT_TOP", 0.50):  # was 0.65
            return None
        if features["move700"] < env_float("PGG2_EARLY_MIN_MOVE700", 1.035):
            return None
        if features["last_buy_age_ms"] > env_int("PGG2_EARLY_MAX_LAST_BUY_AGE_MS", 160):
            return None
        if features["last_sell_age_ms"] < env_int("PGG2_EARLY_MIN_LAST_SELL_AGE_MS", 850):
            return None
        # FIX: Near-zero sell tolerance
        if s700["sell_sol"] > max(0.002, s700["buy_sol"] * env_float("PGG2_EARLY_MAX_SELL700_RATIO", 0.015)):  # was 0.025
            return None
        if s1500["sell_sol"] > max(0.003, s1500["buy_sol"] * env_float("PGG2_EARLY_MAX_SELL1500_RATIO", 0.025)):  # was 0.045
            return None
        fresh700 = self.fresh_wave_stats(event.mint, int(features["ts_ms"]), arm, env_int("PGG2_EARLY_FRESH_WINDOW_MS", 700))
        # FIX: Require fresh follow-through
        if fresh700["fresh_buy_sol"] < env_float("PGG2_EARLY_MIN_FRESH_BUY_SOL", 1.00):  # was 0.40
            return None
        if fresh700["fresh_unique"] < env_int("PGG2_EARLY_MIN_FRESH_BUYERS", 2):  # was 1
            return None
        if fresh700["fresh_top_share"] > env_float("PGG2_EARLY_MAX_FRESH_TOP", 0.60):  # was 0.82
            return None
        score = 120.0
        score += min(35.0, s700["buy_sol"] * 7.0)
        score += min(25.0, s700["unique_buyers"] * 5.0)
        score += max(0.0, base_move - 1.0) * 160.0
        score += max(0.0, features["move700"] - 1.0) * 280.0
        score -= max(0.0, s700["top_buy_share"] - 0.50) * 50.0
        # FIX: Use bankroll-aware sizing
        scout = self.bankroll_sized_sol("probe")
        target = scout  # FIX: No scaling
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

    # =========================================================================
    # DISABLED LANES — kept for compatibility but always return None
    # =========================================================================

    def stealth_arm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Too noisy for small capital."""
        return None

    def spark3_arm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Experimental, unproven."""
        return None

    def spark3_breakout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Experimental, unproven."""
        return None

    def preprice_reveal_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Requires 14+ SOL pre-buy which is rare."""
        return None

    def priced_snap_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Late entry after 1.18x+ move."""
        return None

    def priced_breakout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Late entry after 1.35x+ move — insiders already selling."""
        return None

    def late_swarm_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Entering at 20-180s is too late for small capital."""
        return None

    def curve_arm_scout_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Unproven."""
        return None

    def raw_momentum_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Unproven."""
        return None

    def whale_spark_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Depends on single whale, too risky."""
        return None

    def late_ignition_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Too late for small capital."""
        return None

    def breadth_ignition_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """DISABLED: Too many false positives."""
        return None

    # =========================================================================
    # STRIKE EXECUTION — with circuit breaker
    # =========================================================================

    async def maybe_plan_strike(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        ts_ms = event.ts_ms
        features = self.feature_snapshot(event.mint, ts_ms)
        if not features:
            return
        self.maybe_arm_first_burst(event, features)
        features = self.feature_snapshot(event.mint, ts_ms) or features

        # NEW: Check circuit breaker before any strike
        allowed, cb_reason = self.check_circuit_breaker(ts_ms)
        if not allowed:
            return

        plan = self.build_strike_plan(event, features)
        if not plan:
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

        # Mark seen based on lane
        if plan.lane == "birth_fanout":
            self.birth_fanout_seen.add(event.mint)
        elif plan.lane == "curve_lag_reveal":
            self.curve_lag_reveal_seen.add(event.mint)

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
            return
        self.logger.decision(
            "close",
            mint,
            {"reason": reason, "pnl_sol": pnl, "killed": killed, "features": self.slim_features(features)},
        )
        if mint in self.broker.positions:
            return
        pnl_delta = self.broker.stats.realized_pnl_sol - before_pnl

        # NEW: Track session PnL for circuit breaker
        self.session_pnl_sol += pnl_delta
        self.daily_loss_sol += min(0.0, pnl_delta)
        self.trades_today += 1
        self.bankroll_sol += pnl_delta  # Update bankroll

        # NEW: Track consecutive wins/losses
        if pnl_delta > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self.last_win_ts_ms = ts_ms
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self.last_loss_ts_ms = ts_ms

        # NEW: Activate circuit breaker after consecutive losses
        if self.consecutive_losses >= 3:
            cooldown_ms = env_int("PIGGY_CIRCUIT_BREAKER_COOLDOWN_MS", 300000)  # 5 min
            self.circuit_breaker_active = True
            self.circuit_breaker_until_ms = ts_ms + cooldown_ms
            log(
                f"PIGGY-CIRCUIT-BREAKER activated after {self.consecutive_losses} consecutive losses. "
                f"Cooldown {cooldown_ms / 1000:.0f}s. session_pnl={self.session_pnl_sol:.4f} SOL"
            )

        if pnl_delta > env_float("PIGGY_PROFIT_REENTRY_MIN_PNL_SOL", 0.0):
            self.profitable_closes[mint] = {
                "ts_ms": float(ts_ms),
                "pnl_sol": pnl_delta,
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

    # ---------------------------------------------------------------------------
    # FIX: manage_position — Completely rewritten exit logic for small capital
    #
    # KEY CHANGES:
    # 1. Hard stop at -12% (0.88x) — no exceptions, no grace periods
    # 2. Trailing stop for winners — lets moonshots run
    # 3. Faster "no follow" kills
    # 4. NO scaling — can't afford it at $36
    # 5. Faster time stops
    # ---------------------------------------------------------------------------
    async def manage_position(self, pos: Any, ts_ms: int, price: float, features: dict[str, Any]) -> None:
        mint = pos.mint
        mult = pos.update(price)
        self.broker.stats.best_mult = max(self.broker.stats.best_mult, pos.peak_mult)
        self.add_follow_features(pos, features)

        # --- QUOTE-LEVEL GUARDS (unchanged) ---
        quote_loss_clamp = getattr(self.broker, "quote_loss_clamp_reason", None)
        if quote_loss_clamp and self.moonshot_lane(pos.lane):
            quote_action = quote_loss_clamp(pos, ts_ms)
            if quote_action:
                self.close_position(mint, ts_ms, price, quote_action, features, killed=(quote_action == "quote_loss_clamp"))
                return

        quote_profit_bank = getattr(self.broker, "quote_profit_bank_reason", None)
        if quote_profit_bank and self.moonshot_lane(pos.lane):
            quote_exit = quote_profit_bank(pos, ts_ms)
            if quote_exit:
                self.close_position(mint, ts_ms, price, quote_exit, features, killed=False)
                return

        # --- MIGRATION ---
        if features["complete"]:
            self.close_position(mint, ts_ms, price, "migration_complete", features, killed=False)
            return

        # --- FIX: UNIVERSAL HARD STOP at -12% (0.88x) ---
        # No grace periods, no exceptions. With $36 capital, a -12% loss is
        # manageable. A -20% loss starts to hurt. Cut immediately.
        hard_stop_mult = env_float("PIGGY_HARD_STOP_MULT", 0.88)
        if mult <= hard_stop_mult:
            self.close_position(mint, ts_ms, price, "hard_stop_loss", features, killed=True)
            return

        # --- FIX: TRAILING STOP for winners ---
        # Once the position peaks at 1.5x+, use a trailing stop that tightens
        # as the peak grows. This lets moonshots run while locking in profits.
        trail = self.trailing_stop_mult(pos.peak_mult)
        if trail > 0 and mult <= pos.peak_mult * trail:
            self.close_position(mint, ts_ms, price, f"trailing_stop peak={pos.peak_mult:.2f}x trail={trail:.2f}", features, killed=False)
            return

        # --- KILL REASONS ---
        kill = self.piggy_kill_reason(pos, features)
        if kill:
            self.close_position(mint, ts_ms, price, kill, features, killed=True)
            return

        # --- POSITION STATE MANAGEMENT ---
        age_sec = pos.age_sec(ts_ms)

        if pos.state == "SCOUT":
            # FIX: NO SCALING for small capital. All positions stay at probe size.
            # The original code tried to scale from 0.02 to 0.20 SOL, which is
            # insane with $36. We keep positions at probe size and let the
            # trailing stop manage the exit.

            # Sell pressure exits — exit FAST on any selling
            sell_pressure = (
                features["s700"]["sell_sol"] > 0
                or features["s1500"]["sell_sol"] / max(features["s1500"]["buy_sol"], 0.001) >= 0.06  # FIX: was 0.10
            )

            # FIX: Lower pop thresholds — take profits earlier but let trailing stop ride
            if pos.peak_mult >= 1.40 and mult <= pos.peak_mult * 0.88:
                self.close_position(mint, ts_ms, price, "pop_and_decay_40", features, killed=False)
                return
            if pos.peak_mult >= 1.25 and sell_pressure and mult >= 1.10:
                self.close_position(mint, ts_ms, price, "pop_with_sell_pressure", features, killed=False)
                return

            # Lane-specific "no follow" kills — FIX: Much faster
            no_follow_sec = env_float("PIGGY_NO_FOLLOW_AFTER_SEC", 4.0)  # FIX: was 5-6
            no_follow_min_peak = env_float("PIGGY_NO_FOLLOW_MIN_PEAK", 1.06)
            no_follow_mult = env_float("PIGGY_NO_FOLLOW_MULT", 0.98)

            if pos.lane in {"birth_fanout", "curve_lag_reveal", "early_ignition"}:
                if (
                    age_sec >= no_follow_sec
                    and pos.peak_mult < no_follow_min_peak
                    and mult <= no_follow_mult
                    and (
                        not features["flow_live"]
                        or features["last_buy_age_ms"] >= env_int("PIGGY_NO_FOLLOW_LAST_BUY_MS", 400)
                    )
                ):
                    self.close_position(mint, ts_ms, price, f"{pos.lane}_no_follow", features, killed=True)
                    return

            # Birth fanout sell-slam bank
            if (
                pos.lane == "birth_fanout"
                and pos.peak_mult >= env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_PEAK", 1.15)  # FIX: was 1.20
                and mult >= env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_MULT", 1.06)  # FIX: was 1.10
                and features["s700"]["sell_sol"] >= max(
                    env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_SELL_SOL", 0.20),  # FIX: was 0.35
                    features["s700"]["buy_sol"] * env_float("PGG2_BIRTH_FANOUT_SELL_SLAM_BANK_MIN_SELL_RATIO", 0.10),  # FIX: was 0.18
                )
            ):
                self.close_position(mint, ts_ms, price, "birth_fanout_sell_slam_bank", features, killed=False)
                return

            # FIX: Faster time stops
            # If position hasn't popped in 8 seconds, exit (was 18)
            if age_sec >= env_float("PIGGY_MOON_FAIL_SEC", 8.0) and pos.peak_mult < 1.15:  # FIX: was 18.0 / 1.18
                self.close_position(mint, ts_ms, price, "moonshot_failed_no_pop", features, killed=True)
                return

            # FIX: Hard time limit of 45 seconds (was 75)
            if age_sec >= env_float("PIGGY_MOON_TIMEBOX_SEC", 45.0):
                if mult >= 1.02:
                    self.close_position(mint, ts_ms, price, "moonshot_timebox_profit", features, killed=False)
                else:
                    self.close_position(mint, ts_ms, price, "moonshot_timebox", features, killed=True)
                return

            return

        # SCALE1 / SCALE2 states — FIX: Should not be reached since we don't scale
        # But handle gracefully if they are
        if pos.state == "SCALE1":
            # Don't scale further, just manage as a runner
            pass

        if pos.state in {"RUNNER", "RUNNER_FULL", "SCALE1"}:
            # Trailing stop handles exits for runners
            # Additional safety: sell stall detection
            if features["last_buy_age_ms"] >= 1200 and features["s1500"]["sell_sol"] > 0:
                self.close_position(mint, ts_ms, price, "runner_stalled_after_sell", features, killed=False)
                return
            if age_sec >= 55.0:
                self.close_position(mint, ts_ms, price, "runner_timebox", features, killed=False)
                return

        # FIX: Hard time stop at 45 seconds
        if age_sec >= env_float("PIGGY_HARD_TIME_STOP_SEC", 45.0):
            self.close_position(mint, ts_ms, price, "hard_time_stop", features, killed=False)

    # ---------------------------------------------------------------------------
    # FIX: piggy_kill_reason — Simplified and hardened for small capital
    # ---------------------------------------------------------------------------
    def piggy_kill_reason(self, pos: Any, features: dict[str, Any]) -> Optional[str]:
        s250 = features["s250"]
        s700 = features["s700"]
        age_sec = pos.age_sec(features["ts_ms"])

        # FIX: Universal early failed-entry kill
        # If position is underwater with no follow-through and sells present, kill it
        if (
            age_sec <= env_float("PIGGY_EARLY_FAIL_SEC", 5.0)
            and pos.peak_mult < env_float("PIGGY_EARLY_FAIL_MIN_PEAK", 1.02)
            and pos.last_mult <= env_float("PIGGY_EARLY_FAIL_MULT", 0.92)
            and not features["flow_live"]
            and features["last_sell_age_ms"] <= env_int("PIGGY_EARLY_FAIL_SELL_AGE_MS", 700)
            and features["s1500"]["sell_sol"] > 0
        ):
            return "kill_early_entry_failed"

        # FIX: Any sell in first 3 seconds = instant kill (was more lenient)
        if (
            age_sec <= 3.0
            and s250["sell_sol"] > max(0.002, s250["buy_sol"] * 0.03)  # FIX: was 0.42
            and features["move250"] < 1.005
        ):
            return "kill_instant_distribution"

        # FIX: Unabsorbed sell — tighter threshold
        if s700["sell_sol"] >= max(0.008, s700["buy_sol"] * 0.15) and features["move700"] < 1.01:  # FIX: was 0.30
            return "kill_unabsorbed_sell"

        # Top buyer flipping
        if features["s1500"]["top_buyer_flip"] >= 0.15 and pos.peak_mult < 1.40:  # FIX: was 0.18 / 1.55
            return "kill_cluster_wallet_selling"

        # Buyer stall
        if (
            pos.state in {"SCOUT", "SCALE1"}
            and features["buy_stall"]
            and pos.last_mult < 1.12  # FIX: was 1.18
        ):
            return "kill_buyer_stall"

        # FIX: Faster off-peak kill
        if features["off_peak"] < 0.85 and age_sec <= 4.0:  # FIX: was 0.82 / 5.0
            return "kill_markup_failed"

        # Lane-specific kills
        if pos.lane == "birth_fanout":
            # Birth fanout gets a small grace period but tight stop
            if pos.last_mult <= env_float("PGG2_BIRTH_FANOUT_HARD_BREAK_MULT", 0.90):  # FIX: was 0.92
                return "kill_birth_fanout_hard_break"

        if pos.lane == "curve_lag_reveal":
            if pos.last_mult <= env_float("PGG2_CURVE_LAG_HARD_BREAK_MULT", 0.90):
                return "kill_curve_lag_hard_break"

        if pos.lane == "early_ignition":
            if pos.last_mult <= env_float("PGG2_EARLY_IGNITION_HARD_BREAK_MULT", 0.90):
                return "kill_early_ignition_hard_break"

        # FIX: No-follow kill for all moonshot lanes
        if (
            pos.state == "SCOUT"
            and age_sec >= env_float("PIGGY_NO_FOLLOW_CAP_AFTER_SEC", 5.0)
            and age_sec <= env_float("PIGGY_NO_FOLLOW_CAP_UNTIL_SEC", 15.0)
            and pos.peak_mult < env_float("PIGGY_NO_FOLLOW_CAP_MIN_PEAK", 1.06)
            and features.get("post_open_sig_buy_sol", 0.0) < env_float("PIGGY_NO_FOLLOW_CAP_MIN_SIG_BUY_SOL", 0.10)
            and pos.last_mult <= env_float("PIGGY_NO_FOLLOW_CAP_MULT", 0.98)
        ):
            return "kill_no_followthrough"

        return None

    # ---------------------------------------------------------------------------
    # FIX: scale1_reason — DISABLED for small capital
    # With $36, scaling from probe to full position is too risky.
    # All positions stay at probe size; the trailing stop manages the exit.
    # ---------------------------------------------------------------------------
    def scale1_reason(self, pos: Any, features: dict[str, Any]) -> Optional[str]:
        if not env_bool("PIGGY_SCALING_ENABLED", False):
            return None
        # If somehow enabled, use the original logic but with tighter guards
        if pos.scale1_done:
            return None
        # FIX: Require much stronger signal to scale
        s700 = features["s700"]
        if s700["unique_buyers"] < 8:  # FIX: was 4
            return None
        if s700["top_buy_share"] > 0.45:  # FIX: was 0.66
            return None
        if (
            pos.age_sec(features["ts_ms"]) <= 2.80
            and pos.last_mult >= 1.10
            and s250["sells"] == 0
            and s700["sell_sol"] <= max(0.003, s700["buy_sol"] * 0.03)
            and s700["unique_buyers"] >= 8
            and s700["buy_sol"] >= 2.50
            and features["last_buy_age_ms"] <= 300
            and features["move700"] >= 1.020
        ):
            return "cluster_still_marking_up_tight"
        return None

    @staticmethod
    def derisk_gate(pos: Any, features: dict[str, Any]) -> bool:
        # FIX: Higher thresholds for derisking — let positions run more
        if pos.last_mult >= 1.80:  # was 1.55
            return True
        if pos.last_mult >= 1.50 and features["last_buy_age_ms"] >= 550:  # was 1.32
            return True
        if pos.last_mult >= 1.40 and features["s700"]["sell_sol"] > 0:  # was 1.28
            return True
        return False

    @staticmethod
    def scale2_reason(pos: Any, features: dict[str, Any]) -> Optional[str]:
        # FIX: Disabled — no second scale for small capital
        if not env_bool("PIGGY_SCALING_ENABLED", False):
            return None
        if pos.scale2_done or pos.derisk_done:
            return None
        s700 = features["s700"]
        s1500 = features["s1500"]
        if (
            pos.last_mult >= 1.60  # FIX: was 1.42
            and s700["buy_sol"] >= 1.0
            and s1500["unique_buyers"] >= 8  # FIX: was 6
            and s1500["sell_sol"] / max(s1500["buy_sol"], 0.001) <= 0.05  # FIX: was 0.10
            and features["last_buy_age_ms"] <= 300  # FIX: was 400
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
        # NEW: Include bankroll and circuit breaker info in status
        log(
            f"PIGGY-STATUS creates={st.creates} trades={st.trades} buys/sells={st.buys}/{st.sells} "
            f"plans={st.strike_plans} pend={len(self.broker.pending)} scouts={st.scouts} "
            f"scale1={st.scale1} scale2={st.scale2} partials={st.partials} closes={st.closes} "
            f"W/L={st.wins}/{st.losses} kills={st.kills} best={st.best_mult:.2f}x "
            f"realized={st.realized_pnl_sol:+.6f} open_pnl={self.broker.open_pnl():+.6f} SOL "
            f"bankroll={self.bankroll_sol:.4f} SOL session_pnl={self.session_pnl_sol:+.6f} SOL "
            f"consec_w/l={self.consecutive_wins}/{self.consecutive_losses} "
            f"circuit={'ACTIVE' if self.circuit_breaker_active else 'off'} "
            f"open={len(self.broker.positions)} [{', '.join(open_bits) if open_bits else 'none'}] "
            f"shreds={st.shreds} bc={self.bc.updates} reconn={st.reconnects}"
        )
        if env_bool("PIGGY_SAVE_STATE_ON_STATUS", True):
            self.broker.save_state()

    async def run(self) -> None:
        if not self.config.paper_trading and not self.config.live_enabled:
            raise RuntimeError("Live execution is gated. Set PGG2_EXECUTION_MODE=quote first, then explicit live gates.")
        execution_mode = env_str("PGG2_EXECUTION_MODE", "paper").lower()
        mode = "DRY_LIVE" if execution_mode == "dry_live" else ("PAPER" if self.config.paper_trading else execution_mode.upper())
        log(
            f"PIGGY: starting {mode} scout={self.config.scout_sol:.4f} max_pos={self.config.max_position_sol:.4f} "
            f"cluster_age={self.config.birth_max_age_ms}ms max_open={self.config.max_open_positions} "
            f"bankroll={self.bankroll_sol:.4f} SOL "
            f"active_lanes=birth_fanout,curve_lag_reveal,early_ignition"
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
        log(
            f"PIGGY-REPLAY: rows={rows} state={self.config.state_file} decisions={self.config.decisions_file} "
            f"session_pnl={self.session_pnl_sol:+.6f} SOL bankroll={self.bankroll_sol:.4f} SOL"
        )

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