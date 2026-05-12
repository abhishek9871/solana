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
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from birth_first_sniper import (
    BASE_DIR,
    DATA_DIR,
    SOL_MINT,
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


# v33 — canonical rule + policy identifiers. Old aliases like
# v33_quote_edge_150_C and mined_quote_edge_pnl_ge_150 must
# resolve to these in every runtime log so the preregistered-rule hash
# remains the only source of truth.
RULE_V33_QUOTE_EDGE_150_C = "v33_quote_edge_150_C"
POLICY_V33_C_MOONSHOT = "C_moonshot_hold_protected_clamp_v33"
PNL_MODEL_VERSION_V33 = "v33_route_aware"


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


class QuoteManager:
    """v32 — centralized quote service. Single source of truth for runtime
    sell-quote requests so the risk worker, event-driven manage path, and
    shadow lab can share a single in-flight quote per key. Returns an
    explicit `quote_status` so callers cannot mistake a rate-limited no-op
    for protection.

    Status values:
      `fresh_network_quote`  — this caller drove the network fetch
      `cache_hit`            — recent cached quote (within TTL)
      `rate_limited_no_quote`— another request is in flight and no cache
      `error`                — broker raised
    """

    def __init__(self, broker: Any) -> None:
        self.broker = broker
        self.lock = threading.RLock()
        self.cache: dict[tuple, dict[str, Any]] = {}
        self.in_flight: set[tuple] = set()
        self.cache_ttl_ms = env_int("PGG2_QUOTE_MGR_CACHE_TTL_MS", 300)
        # latency-aware: if the most recent network quote was older than this
        # AND we are not currently in-flight, callers should trigger a refresh.
        self.refresh_after_ms = env_int("PGG2_QUOTE_MGR_REFRESH_AFTER_MS", 200)

    def get_sell_quote(
        self,
        mint: str,
        sell_amount: Any,
        slippage: float,
        pair_source: str = "unknown",
    ) -> dict[str, Any]:
        # round amount to 6 decimals so risk and event paths share the key
        try:
            amt_key = round(float(sell_amount), 6) if isinstance(sell_amount, (int, float)) else sell_amount
        except Exception:
            amt_key = sell_amount
        key = (mint, "sell", amt_key, pair_source)
        now_ms = int(time.time() * 1000)
        with self.lock:
            cached = self.cache.get(key)
            if cached is not None:
                age_ms = now_ms - int(cached["ts_ms"])
                if age_ms <= self.cache_ttl_ms:
                    log(
                        f"PGG2-QUOTE-MGR-CACHE-HIT mint={short_addr(mint)} side=sell "
                        f"amount={amt_key} age_ms={age_ms} latency_ms={cached.get('latency_ms')}"
                    )
                    return {
                        "status": "cache_hit",
                        "quote": cached["quote"],
                        "out": cached["out"],
                        "ts_ms": cached["ts_ms"],
                        "latency_ms": cached["latency_ms"],
                        "age_ms": age_ms,
                    }
            if key in self.in_flight:
                log(
                    f"PGG2-QUOTE-MGR-RATE-LIMITED mint={short_addr(mint)} side=sell "
                    f"amount={amt_key} reason=in_flight_no_cache"
                )
                return {"status": "rate_limited_no_quote", "reason": "in_flight_no_cache"}
            self.in_flight.add(key)
        log(
            f"PGG2-QUOTE-MGR-REQ mint={short_addr(mint)} side=sell "
            f"amount={amt_key} pair_source={pair_source}"
        )
        start_ts = time.time()
        try:
            quote = self.broker.build_swap(mint, SOL_MINT, sell_amount, slippage)
            out = float(self.broker.rate_amount_out(quote))
            end_ts = time.time()
            latency_ms = int((end_ts - start_ts) * 1000)
            entry = {
                "quote": quote,
                "out": out,
                "ts_ms": int(end_ts * 1000),
                "latency_ms": latency_ms,
            }
            with self.lock:
                self.cache[key] = entry
                self.in_flight.discard(key)
            log(
                f"PGG2-QUOTE-MGR-NETWORK-RESULT mint={short_addr(mint)} side=sell "
                f"amount={amt_key} latency_ms={latency_ms} out={out:.6f}"
            )
            return {"status": "fresh_network_quote", **entry}
        except Exception as exc:
            with self.lock:
                self.in_flight.discard(key)
            log(
                f"PGG2-QUOTE-MGR-ERROR mint={short_addr(mint)} side=sell "
                f"amount={amt_key} {type(exc).__name__}: {exc}"
            )
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


class RiskWorker:
    """v31/v32 — bounded risk worker. Single owner of quote-based exits for
    canary/pilot positions. Routes all quote reads through QuoteManager.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.tracked: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.poll_sleep_sec: float = env_float("PGG2_RISK_WORKER_POLL_SEC", 0.020)
        self.stale_max_ms: int = env_int("PGG2_RISK_QUOTE_STALE_MS", 2000)
        self.allow_overlap: bool = env_bool("PGG2_RISK_ALLOW_OVERLAP_QUOTES", False)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.loop = loop
        self.thread = threading.Thread(target=self._run, daemon=True, name="risk_worker")
        self.thread.start()
        log(
            f"PGG2-RISK-WORKER-START poll_sec={self.poll_sleep_sec} stale_max_ms={self.stale_max_ms} "
            f"allow_overlap={int(self.allow_overlap)}"
        )

    def add_position(
        self, mint: str, lane: str, rule_id: str, opened_ts_ms: int, last_price: float
    ) -> None:
        with self.lock:
            self.tracked[mint] = {
                "lane": lane,
                "rule_id": rule_id,
                "opened_ts_ms": opened_ts_ms,
                "last_price": last_price,
                "in_flight": False,
            }

    def remove_position(self, mint: str) -> None:
        with self.lock:
            self.tracked.pop(mint, None)

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    items = list(self.tracked.items())
                for mint, ctx in items:
                    if self.stop_event.is_set():
                        break
                    pos = self.bot.broker.positions.get(mint)
                    if pos is None:
                        self.remove_position(mint)
                        continue
                    if ctx.get("in_flight") and not self.allow_overlap:
                        continue
                    ctx["in_flight"] = True
                    try:
                        self._tick(mint, ctx, pos)
                    finally:
                        ctx["in_flight"] = False
                time.sleep(self.poll_sleep_sec)
        except Exception as exc:
            log(f"PGG2-RISK-WORKER-FATAL {type(exc).__name__}: {exc}")

    def _tick(self, mint: str, ctx: dict[str, Any], pos: Any) -> None:
        """v32 — risk worker is the SINGLE OWNER of quote-based exits for
        risk-managed positions. Routes sell-quote reads through QuoteManager
        and decides bank/clamp itself using the unified pnl basis.
        """
        broker = self.bot.broker
        qm: Optional[QuoteManager] = getattr(self.bot, "quote_manager", None)
        if qm is None:
            log(f"PGG2-RISK-WORKER-ERR mint={short_addr(mint)} quote_manager_missing")
            return
        # decide sell amount from recorded quote_shadow_tokens (locked)
        known_tokens = float(broker.quote_shadow_tokens.get(mint, pos.remaining_tokens) or 0.0)
        if known_tokens <= 0:
            return
        remaining_fraction = pos.remaining_tokens / max(pos.tokens_bought, 1e-18)
        sell_amount = round(known_tokens * remaining_fraction, 9)
        pair_source = ""
        if hasattr(broker, "last_pair_info"):
            try:
                pair_source = broker.last_pair_info(mint).get("pair_source", "")
            except Exception:
                pair_source = ""
        result = qm.get_sell_quote(mint, sell_amount, broker.sell_slippage, pair_source=pair_source or "unknown")
        status = result.get("status")
        wall_ms = int(time.time() * 1000)
        age_ms = wall_ms - getattr(pos, "opened_ts_ms", wall_ms)
        # If the quote is a no-op (rate-limited or error), the worker is BLIND.
        if status in ("rate_limited_no_quote", "error"):
            log(
                f"PGG2-RISK-BLIND mint={short_addr(mint)} status={status} age_ms={age_ms}"
            )
            log(
                f"PGG2-RISK-QUOTE-EVAL mint={short_addr(mint)} quote_status={status} "
                f"trigger=blind age_ms={age_ms}"
            )
            return
        # v32 — compute route-aware all-in PnL. The old fixed-overhead formula
        # subtracted 0.00235 SOL of phantom recoverable ATA rent and made
        # round-trip-zero positions look like guaranteed losses.
        cost = float(pos.cost_sol)
        out = float(result.get("out") or 0.0)
        route = "pump_bc"
        try:
            quote_dict = result.get("quote") or {}
            route = str(quote_dict.get("route", "pump_bc"))
        except Exception:
            pass
        pnl_dict = broker.quote_all_in_pnl(
            route=route,
            cost_sol=cost,
            quote_out=out,
            quote_metadata={
                "buy_fee_sol": 0.0,
                "sell_fee_sol": float((quote_dict.get("rate") or {}).get("fee", 0.0)) if quote_dict else 0.0,
            },
            execution_context={"ata_recoverable": True},
        )
        net_pnl = float(pnl_dict["all_in_pnl"])
        log(
            f"PGG2-PNL-BREAKDOWN mint={short_addr(mint)} route={pnl_dict['route']} "
            f"cost={pnl_dict['cost_sol']:.6f} quote_out={pnl_dict['quote_out']:.6f} "
            f"gross={pnl_dict['gross_quote_pnl']:+.6f} extra={pnl_dict['extra_overhead_not_in_quote']:.6f} "
            f"all_in={pnl_dict['all_in_pnl']:+.6f} basis={pnl_dict['pnl_basis']}"
        )
        quote_age_ms = int(result.get("age_ms") or 0)
        quote_latency_ms = int(result.get("latency_ms") or 0)
        in_flight_for_key = 1 if status == "fresh_network_quote" else 0
        # thresholds per lane
        bank_threshold = env_float("PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_PNL_SOL", 0.00060)
        clamp_threshold = -env_float(
            "PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_FLOOR_SOL", 0.00150
        )
        timebox_ms = env_int("PGG2_DRYLIVE_PILOT_TIMEBOX_MS", 5000)
        absolute_ms = env_int("PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS", 10000)
        trigger = "none"
        close_reason = ""
        if age_ms >= absolute_ms:
            trigger = "max_hold"
            close_reason = "risk_worker_max_hold"
        elif net_pnl >= bank_threshold:
            trigger = "bank"
            close_reason = "risk_worker_bank"
        elif net_pnl <= clamp_threshold:
            trigger = "clamp"
            close_reason = "risk_worker_clamp"
        elif age_ms >= timebox_ms and net_pnl < 0.0:
            trigger = "timebox"
            close_reason = "risk_worker_timebox"
        log(
            f"PGG2-RISK-QUOTE-EVAL mint={short_addr(mint)} quote_status={status} "
            f"quote_age_ms={quote_age_ms} quote_latency_ms={quote_latency_ms} "
            f"quote_out={out:.6f} net_pnl={net_pnl:+.6f} trigger={trigger} "
            f"in_flight_for_key={in_flight_for_key}"
        )
        if close_reason:
            killed = trigger in {"clamp", "max_hold"}
            self._schedule_close(mint, close_reason, killed=killed)

    def _schedule_close(self, mint: str, reason: str, killed: bool) -> None:
        # v32 — close idempotency. Skip if already scheduled.
        with self.lock:
            ctx = self.tracked.get(mint)
            if ctx is None:
                # v33 — explicit skip log so post-close re-entries are visible.
                log(
                    f"PGG2-RISK-CLOSE-SKIP mint={short_addr(mint)} "
                    f"reason=position_gone_or_already_closed"
                )
                return
            if ctx.get("close_scheduled"):
                log(
                    f"PGG2-RISK-CLOSE-SKIP mint={short_addr(mint)} "
                    f"reason=already_scheduled prior_reason={ctx.get('close_reason')}"
                )
                return
            ctx["close_scheduled"] = True
            ctx["close_reason"] = reason
            ctx_rule_id = str(ctx.get("rule_id", RULE_V33_QUOTE_EDGE_150_C))
        log(
            f"PGG2-RISK-CLOSE-REQUEST mint={short_addr(mint)} reason={reason} killed={int(bool(killed))}"
        )
        pos = self.bot.broker.positions.get(mint)
        if pos is None or self.loop is None:
            return
        price = float(pos.last_price)
        # v33 — defensive: features dict carries all the keys downstream
        # decision-logging consumers expect (slim_features reads these and
        # would otherwise KeyError on the pilot close path).
        features = {
            "price": price,
            "has_curve": False,
            "complete": False,
            "cost_model_route": "pump_bc",
            "cost_model_confidence": "proven",
            "pnl_model_version": PNL_MODEL_VERSION_V33,
            "rule_id": ctx_rule_id,
            "policy_id": POLICY_V33_C_MOONSHOT,
        }

        def _do_close() -> None:
            try:
                self.bot.close_position(
                    mint, int(time.time() * 1000), price, reason, features, killed=killed
                )
                log(f"PGG2-RISK-CLOSE-ACK mint={short_addr(mint)} reason={reason}")
            except Exception as exc:
                log(
                    f"PGG2-RISK-CLOSE-FAIL mint={short_addr(mint)} "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                # v33 — clear broker risk-owned registry on every close
                # outcome (success OR fail) so non-risk callers can resume.
                try:
                    if hasattr(self.bot.broker, "clear_risk_owned"):
                        self.bot.broker.clear_risk_owned(mint)
                except Exception:
                    pass
                self.remove_position(mint)

        try:
            self.loop.call_soon_threadsafe(_do_close)
        except Exception as exc:
            log(
                f"PGG2-RISK-CLOSE-SCHEDULE-FAIL mint={short_addr(mint)} "
                f"{type(exc).__name__}: {exc}"
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
        self.rug_bounce_seen: set[str] = set()  # v29m
        self.priced_breakout_watch: dict[str, dict[str, Any]] = {}
        self.priced_breakout_seen: set[str] = set()
        self.late_swarm_seen: set[str] = set()
        self.curve_arm_scout_seen: set[str] = set()
        self.raw_momentum_seen: set[str] = set()
        self.raw_momentum_arms: dict[str, dict[str, Any]] = {}
        self.whale_spark_seen: set[str] = set()
        # v30 — executable shadow lab
        self._shadow_last_record_ms: dict[str, int] = {}
        self._shadow_lab_path: Path = Path(
            env_str(
                "PGG2_SHADOW_LAB_PATH",
                str(DATA_DIR / "pgg2_executable_shadow_lab.jsonl"),
            )
        )
        self._shadow_active_tasks: int = 0
        self._canary_entries: int = 0
        self._canary_attempted: bool = False
        # v30 — drylive pilot tracker
        self._pilot_entries: int = 0
        self._pilot_attempted: bool = False
        self._pilot_session_loss_sol: float = 0.0
        self._pilot_mints_seen: set[str] = set()
        # v30 — pilot manage tracking (per-mint last quote pnl + last_mark_ms)
        self._pilot_last_pnl: dict[str, float] = {}
        self._pilot_last_mark_ms: dict[str, int] = {}
        # v32 — centralized QuoteManager for runtime sell-quote sharing.
        self.quote_manager: Optional[QuoteManager] = (
            QuoteManager(self.broker) if hasattr(self, "broker") and self.broker is not None else None
        )
        # v31 — bounded risk worker (thread). Started in run() once the loop exists.
        self._risk_worker = RiskWorker(self)

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
            "shadow_lab_canary",  # v30 — needed so manage_position dispatches clamp/bank for canary
            "v33_quote_edge_150_C",  # v30 — pilot lane
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

    # v30 — executable shadow lab ===========================================
    # Ghost-records any candidate that satisfied a lane's entry condition,
    # rates the broker quote round-trip immediately and at +250/500/1000/
    # 2000/5000/10000ms, and writes a JSONL record. Discovers which setups
    # are *executable* winners without risking SOL on uncertain lanes.

    def _shadow_lab_enabled(self) -> bool:
        if not env_bool("PGG2_SHADOW_LAB_ENABLED", True):
            return False
        broker = getattr(self, "broker", None)
        return bool(broker and hasattr(broker, "build_swap"))

    def record_shadow_candidate(
        self,
        event: PumpEvent,
        features: dict[str, Any],
        plan: StrikePlan,
        plan_meta: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self._shadow_lab_enabled():
            return
        ts_ms = int(event.ts_ms or now_ms())
        cooldown_ms = env_int("PGG2_SHADOW_LAB_COOLDOWN_MS", 30000)
        last = self._shadow_last_record_ms.get(event.mint, 0)
        # canary path bypasses cooldown so we can race to fire exactly once
        if plan.lane != "shadow_lab_canary" and ts_ms - last < cooldown_ms:
            return
        max_concurrent = env_int("PGG2_SHADOW_LAB_MAX_CONCURRENT", 4)
        if self._shadow_active_tasks >= max_concurrent:
            return
        self._shadow_last_record_ms[event.mint] = ts_ms
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._shadow_candidate_task(event, features, plan, ts_ms, plan_meta or {}),
            name=f"shadow:{plan.lane}:{short_addr(event.mint)}",
        )

    async def _shadow_candidate_task(
        self,
        event: PumpEvent,
        features: dict[str, Any],
        plan: StrikePlan,
        ts_ms: int,
        plan_meta: dict[str, Any],
    ) -> None:
        self._shadow_active_tasks += 1
        try:
            await self._shadow_candidate_run(event, features, plan, ts_ms, plan_meta)
        except Exception as exc:
            log(f"SHADOW-LAB-ERR {short_addr(event.mint)} {type(exc).__name__}: {exc}")
        finally:
            self._shadow_active_tasks -= 1

    async def _shadow_candidate_run(
        self,
        event: PumpEvent,
        features: dict[str, Any],
        plan: StrikePlan,
        ts_ms: int,
        plan_meta: dict[str, Any],
    ) -> None:
        broker = self.broker
        amount = float(plan.scout_sol)
        broker_name = type(broker).__name__ if broker is not None else "?"
        record: dict[str, Any] = {
            "ts_ms": ts_ms,
            "mint": event.mint,
            "lane_candidate": plan.lane,
            "plan_reason": plan.reason,
            "plan_score": float(plan.score),
            "scout_sol": amount,
            "target_sol": float(plan.target_sol),
            "actual_entry_allowed": bool(plan_meta.get("actual_entry_allowed", False)),
            "actual_entry_blocker": str(plan_meta.get("actual_entry_blocker", "")),
            "trigger_features": dict(plan_meta.get("trigger_features", {})),
            "broker": broker_name,
            "broker_mode": getattr(broker, "mode", ""),
            "event_sig": getattr(event, "sig", None),
            "event_sol": float(getattr(event, "sol", 0.0) or 0.0),
            "event_is_buy": bool(getattr(event, "is_buy", False)),
            "age_ms": int(features.get("age_ms") or 0),
            "price": float(features.get("price") or 0.0),
            "vsol": float(features.get("vsol") or 0.0),
            "curve_complete": bool(features.get("curve_complete") or False),
            "slot_buyers": int(features.get("slot_buyers") or 0),
            "slot_buy_sol": float(features.get("slot_buy_sol") or 0.0),
            "slot_top_share": float(features.get("slot_top_share") or 0.0),
            "last_buy_age_ms": int(features.get("last_buy_age_ms") or 0),
            "last_sell_age_ms": int(features.get("last_sell_age_ms") or 0),
        }
        for window in ("s1", "s3", "s5", "s15", "s30", "s60"):
            for stat in ("buys", "sells", "buy_sol", "sell_sol", "unique_buyers", "top_share"):
                key = f"{window}_{stat}"
                value = features.get(key)
                if value is not None:
                    record[key] = float(value) if isinstance(value, (int, float)) else value

        # v30 quote coverage repair — prewarm the Pump buyback pair from the
        # current event signature before the broker tries to build the buy
        # tx. This populates the cache so pump_buy_remaining_metas no longer
        # raises on fresh mints. Safe in every mode (read-only RPC + cache).
        event_sig = getattr(event, "sig", None)
        if event_sig and hasattr(broker, "prewarm_pump_buyback_pair_from_sig"):
            try:
                prewarmed = await asyncio.to_thread(
                    broker.prewarm_pump_buyback_pair_from_sig, event.mint, event_sig
                )
                record["pair_prewarm_attempted"] = True
                record["pair_prewarm_success"] = bool(prewarmed)
            except Exception as exc:
                record["pair_prewarm_error"] = f"{type(exc).__name__}: {exc}"

        record["direct_quote_success"] = False
        record["direct_sell_quote_success"] = False
        record["economic_quote_success"] = False
        record["economic_quote_source"] = "none"
        record["execution_eligible"] = False

        # v30 — buy-quote with curve_missing retry ladder. Some pump.fun mints
        # are unquoteable at t=0 (no bonding curve account yet) but become
        # quoteable a few hundred ms later. We retry only when the initial
        # error is curve_missing so we do not amplify other failure modes.
        entry, quote_tokens, retry_meta = await self._direct_buy_quote_with_retry(
            broker, event.mint, amount, record
        )

        # v30 — Pump v2 probe. Quote/sim only. Hard-blocked in real-live mode.
        if env_bool("PGG2_DIRECT_PUMP_V2_PROBE", False) and hasattr(broker, "probe_pump_v2_buy"):
            try:
                probe = await asyncio.to_thread(broker.probe_pump_v2_buy, event.mint, amount)
                record.update(probe)
            except Exception as exc:
                record["v2_probe_attempted"] = True
                record["v2_probe_error"] = f"{type(exc).__name__}: {exc}"
        if entry is None or quote_tokens <= 0:
            # economic fallback (Raptor) for diagnostic visibility only
            economic_quote_tokens = await self._try_raptor_economic_fallback(
                broker, SOL_MINT, event.mint, amount, broker.buy_slippage, "buy", record
            )
            if economic_quote_tokens > 0:
                quote_tokens = economic_quote_tokens
                record["entry_quote_tokens"] = quote_tokens
            else:
                record["label"] = "blocked_by_no_quote"
                self._write_shadow_record(record)
                return
        try:
            impact = 0.0
            try:
                impact = float(broker.rate_price_impact(entry)) if entry else 0.0
            except Exception:
                pass
            record["entry_quote_tokens"] = quote_tokens
            record["entry_quote_impact"] = impact
            record["direct_quote_success"] = quote_tokens > 0 and entry is not None
            if hasattr(broker, "last_pair_info"):
                record.update(broker.last_pair_info(event.mint))
        except Exception:
            pass

        overhead = float(getattr(broker, "quote_roundtrip_overhead_sol", 0.0) or 0.0)
        record["overhead_sol"] = overhead

        try:
            t1 = time.perf_counter()
            reverse = await asyncio.to_thread(
                broker.build_swap, event.mint, SOL_MINT, round(quote_tokens, 9), broker.sell_slippage
            )
            record["reverse_quote_ms"] = (time.perf_counter() - t1) * 1000.0
            immediate_out = float(broker.rate_amount_out(reverse))
            record["direct_sell_quote_success"] = immediate_out > 0
        except Exception as exc:
            err_str = f"{type(exc).__name__}: {exc}"
            record["reverse_quote_error"] = err_str
            record["reverse_quote_error_class"] = self._classify_quote_error(err_str)
            record["no_quote_side"] = "sell"
            economic_out = await self._try_raptor_economic_fallback(
                broker, event.mint, SOL_MINT, round(quote_tokens, 9), broker.sell_slippage, "sell", record
            )
            if economic_out > 0:
                immediate_out = economic_out
            else:
                record["label"] = "blocked_by_no_quote"
                self._write_shadow_record(record)
                return
        if immediate_out <= 0:
            record["no_quote_side"] = "sell"
            record["no_quote_reason"] = "amountOut_le_zero"
            record["immediate_reverse_out"] = 0.0
            record["label"] = "blocked_by_no_quote"
            self._write_shadow_record(record)
            return

        record["execution_eligible"] = bool(
            record.get("direct_quote_success") and record.get("direct_sell_quote_success")
        )
        immediate_pnl = immediate_out - overhead - amount
        record["immediate_reverse_out"] = immediate_out
        record["immediate_pnl"] = immediate_pnl  # legacy alias retained
        record["legacy_immediate_pnl"] = immediate_pnl
        # v33 — canonical route-aware fields. Compute the all-in immediate
        # PnL using the broker's CostModel so callers can ignore the legacy
        # overhead constant.
        if hasattr(broker, "quote_all_in_pnl"):
            try:
                econ = broker.quote_all_in_pnl(
                    route="pump_bc",
                    cost_sol=amount,
                    quote_out=immediate_out,
                    execution_context={"ata_recoverable": True},
                )
                record["all_in_immediate_pnl"] = float(econ["all_in_pnl"])
                record["cost_model_route"] = econ.get("route", "pump_bc")
                record["pnl_model_version"] = "v33_route_aware"
                record["cost_model_confidence"] = "proven"
            except Exception:
                record["pnl_model_version"] = "v33_route_aware_failed"
                record["cost_model_confidence"] = "unknown"
        else:
            record["pnl_model_version"] = "v32_legacy"
            record["cost_model_confidence"] = "legacy"

        # v30 — canary actual entry path (env-gated; refuses real live).
        # Promote lane to shadow_lab_canary ONLY when we successfully reach
        # the canary attempt — keeps lane labels clean in the lab.
        canary_pos = None
        if (
            env_bool("PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY", False)
            and env_bool("PGG2_ACTUAL_ENTRY_MASTER_ENABLED", False)
            and self._canary_safe_to_attempt()
        ):
            canary_plan = StrikePlan(
                mint=plan.mint,
                ts_ms=plan.ts_ms,
                lane="shadow_lab_canary",
                reason="canary_only",
                score=0.0,
                scout_sol=plan.scout_sol,
                target_sol=plan.target_sol,
                price=amount / max(quote_tokens, 1e-18),
                needs_curve_fill=False,
                features=dict(features),
            )
            canary_pos = self._try_canary_entry(
                event, features, canary_plan, quote_tokens, immediate_pnl,
                entry_quote=entry, immediate_out=immediate_out, record=record,
            )
            if canary_pos:
                record["lane_candidate"] = "shadow_lab_canary"
                record["plan_reason"] = "canary_only"
                record["actual_entry_allowed"] = True
                record["actual_entry_blocker"] = "canary_only"
        record["canary_opened"] = bool(canary_pos)

        # v30 — v33_quote_edge_150_C PILOT actual-entry path.
        # Hard-refuses live mode AND master switch. Strict entry; quote-locked.
        pilot_pos = None
        if env_bool("PGG2_DRYLIVE_PILOT_ENABLED", False) and not canary_pos:
            pilot_pos = self._try_pilot_entry(
                event, features, plan, quote_tokens, immediate_pnl, record,
                entry_quote=entry, immediate_out=immediate_out,
            )
            if pilot_pos:
                record["lane_candidate"] = "v33_quote_edge_150_C"
                record["plan_reason"] = "drylive_pilot"
                record["actual_entry_allowed"] = True
                record["actual_entry_blocker"] = ""
        record["pilot_opened"] = bool(pilot_pos)

        future: list[dict[str, Any]] = []
        delays_ms = env_str(
            "PGG2_SHADOW_LAB_SELL_DELAYS_MS", "250,500,1000,2000,5000,10000"
        )
        try:
            delay_list = [int(x.strip()) for x in delays_ms.split(",") if x.strip()]
        except Exception:
            delay_list = [250, 500, 1000, 2000, 5000, 10000]
        best_pnl = immediate_pnl
        worst_pnl = immediate_pnl
        time_to_best = 0
        prev_t = 0
        for delay_ms in delay_list:
            await asyncio.sleep(max(0.0, (delay_ms - prev_t) / 1000.0))
            prev_t = delay_ms
            # v33 — active-position quote exclusivity. If the pilot has
            # opened on this mint, stop racing the risk worker for sell
            # quotes; the lab loop yields the wire to the risk worker.
            try:
                if broker.is_risk_owned(event.mint):
                    broker.log_risk_owned_block(event.mint, "shadow_lab", "future_sells")
                    future.append({"t_ms": delay_ms, "skipped_reason": "risk_owned"})
                    break
            except Exception:
                pass
            try:
                q = await asyncio.to_thread(
                    broker.build_swap,
                    event.mint,
                    SOL_MINT,
                    round(quote_tokens, 9),
                    broker.sell_slippage,
                )
                out = float(broker.rate_amount_out(q))
                pnl = out - overhead - amount
                future.append({"t_ms": delay_ms, "out": out, "pnl": pnl})
                if pnl > best_pnl:
                    best_pnl = pnl
                    time_to_best = delay_ms
                if pnl < worst_pnl:
                    worst_pnl = pnl
            except Exception as exc:
                future.append({"t_ms": delay_ms, "error": f"{type(exc).__name__}: {exc}"})
        record["future_sells"] = future
        # v33 — DELAYED-ENTRY SCANNER. Schedule fresh buy+sell quote snapshots
        # at standard delays so we can find rules whose ALL-IN immediate PnL
        # is positive at a LATER entry time, not at t=0. Each snapshot is a
        # fresh round-trip quote pair (does NOT reuse the t=0 buy quote).
        if env_bool("PGG2_SHADOW_DELAYED_ENTRY_SCANNER", True):
            delays_str = env_str(
                "PGG2_SHADOW_DELAYED_ENTRY_MS", "250,500,750,1000,1500,2000,3000,5000"
            )
            try:
                delay_list = [int(x.strip()) for x in delays_str.split(",") if x.strip()]
            except Exception:
                delay_list = [250, 500, 750, 1000, 1500, 2000, 3000, 5000]
            delayed_snapshots: list[dict[str, Any]] = []
            prev_t = 0
            for delay_ms in delay_list:
                await asyncio.sleep(max(0.0, (delay_ms - prev_t) / 1000.0))
                prev_t = delay_ms
                # v33 — active-position quote exclusivity. If the pilot has
                # opened on this mint, stop sampling delayed entries; the
                # risk worker owns the quote stream from here.
                try:
                    if broker.is_risk_owned(event.mint):
                        broker.log_risk_owned_block(event.mint, "shadow_lab", "delayed_scanner")
                        delayed_snapshots.append({
                            "delayed_entry_ms": delay_ms,
                            "skipped_reason": "risk_owned",
                            "known_before_entry": True,
                        })
                        break
                except Exception:
                    pass
                snap_start = time.time()
                try:
                    buy_q = await asyncio.to_thread(
                        broker.build_swap, SOL_MINT, event.mint, amount, broker.buy_slippage
                    )
                    buy_t = float(broker.rate_amount_out(buy_q))
                    buy_lat_ms = int((time.time() - snap_start) * 1000)
                    sim_needed_snap = bool(
                        env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False)
                        and broker.quote_simulate
                        and not env_bool("PGG2_DIRECT_SKIP_SIM_IF_CACHED", True)
                    )
                    if buy_t <= 0:
                        delayed_snapshots.append({
                            "delayed_entry_ms": delay_ms,
                            "buy_quote_tokens": 0.0,
                            "buy_quote_latency_ms": buy_lat_ms,
                            "sim_needed": sim_needed_snap,
                            "known_before_entry": True,
                            "cost_model_confidence": "proven",
                            "error": "buy_quote_zero",
                        })
                        continue
                    sell_start = time.time()
                    sell_q = await asyncio.to_thread(
                        broker.build_swap,
                        event.mint,
                        SOL_MINT,
                        round(buy_t, 9),
                        broker.sell_slippage,
                    )
                    sell_out = float(broker.rate_amount_out(sell_q))
                    sell_lat_ms = int((time.time() - sell_start) * 1000)
                    econ_snap = broker.quote_all_in_pnl(
                        route="pump_bc",
                        cost_sol=amount,
                        quote_out=sell_out,
                        execution_context={"ata_recoverable": True},
                    )
                    pair_source_snap = ""
                    if hasattr(broker, "last_pair_info"):
                        try:
                            pair_source_snap = broker.last_pair_info(event.mint).get(
                                "pair_source", ""
                            )
                        except Exception:
                            pair_source_snap = ""
                    delayed_snapshots.append({
                        "delayed_entry_ms": delay_ms,
                        "buy_quote_tokens": buy_t,
                        "buy_quote_latency_ms": buy_lat_ms,
                        "sell_quote_out": sell_out,
                        "sell_quote_latency_ms": sell_lat_ms,
                        "sim_needed": sim_needed_snap,
                        "pair_source": pair_source_snap,
                        "all_in_immediate_pnl_at_delay": float(econ_snap["all_in_pnl"]),
                        "gross_quote_pnl_at_delay": float(econ_snap["gross_quote_pnl"]),
                        "cost_model_confidence": "proven",
                        "known_before_entry": True,
                    })
                except Exception as exc:
                    delayed_snapshots.append({
                        "delayed_entry_ms": delay_ms,
                        "error": f"{type(exc).__name__}: {exc}",
                        "known_before_entry": True,
                    })
            record["delayed_snapshots"] = delayed_snapshots

        record["best_executable_pnl"] = best_pnl
        record["worst_executable_pnl"] = worst_pnl
        # v33 — canonical route-aware lookahead fields (LOOK-AHEAD ONLY for
        # diagnostic comparison; never used to justify entries).
        record["legacy_best_pnl"] = best_pnl
        record["legacy_worst_pnl"] = worst_pnl
        if hasattr(broker, "quote_all_in_pnl"):
            try:
                # rebuild future_sells under route-aware accounting
                rebuilt = []
                best_all_in = float("-inf")
                worst_all_in = float("inf")
                best_t = 0
                for fs in future:
                    out_sol = float(fs.get("out") or 0.0)
                    if out_sol <= 0:
                        rebuilt.append(fs)
                        continue
                    e = broker.quote_all_in_pnl(
                        route="pump_bc", cost_sol=amount, quote_out=out_sol,
                        execution_context={"ata_recoverable": True},
                    )
                    aip = float(e["all_in_pnl"])
                    new_fs = dict(fs)
                    new_fs["all_in_pnl"] = aip
                    rebuilt.append(new_fs)
                    if aip > best_all_in:
                        best_all_in = aip
                        best_t = int(fs.get("t_ms") or 0)
                    if aip < worst_all_in:
                        worst_all_in = aip
                if best_all_in == float("-inf"):
                    best_all_in = float(record.get("all_in_immediate_pnl") or 0.0)
                if worst_all_in == float("inf"):
                    worst_all_in = float(record.get("all_in_immediate_pnl") or 0.0)
                record["future_sells"] = rebuilt
                record["all_in_best_pnl_lookahead"] = best_all_in
                record["all_in_worst_pnl"] = worst_all_in
                record["all_in_time_to_best_ms"] = best_t
            except Exception:
                pass
        record["time_to_best_ms"] = time_to_best

        moonshot_threshold = env_float("PGG2_SHADOW_LAB_MOONSHOT_PNL_RATIO", 1.0)
        loss_threshold = env_float("PGG2_SHADOW_LAB_FAKE_PUMP_LOSS_RATIO", 0.5)
        if best_pnl > amount * moonshot_threshold:
            record["label"] = "missed_moonshot"
        elif best_pnl > 0:
            record["label"] = "executable_win"
        elif worst_pnl < -amount * loss_threshold:
            record["label"] = "fake_pump"
        else:
            record["label"] = "executable_loss"

        self._write_shadow_record(record)

    def _classify_shadow_lane(
        self, event: PumpEvent, features: dict[str, Any]
    ) -> tuple[str, bool, str, dict[str, bool]]:
        """v30 — classify a permissive observation into a lane family so the
        shadow lab can compare lane-by-lane.

        Returns (lane_candidate, actual_entry_allowed_in_v30, blocker, trigger_features).
        actual_entry_allowed reflects whether v30 strategy config would let this
        family fire a real entry; the canary path overrides this for one trade.
        """
        age_ms = int(features.get("age_ms") or 0)
        event_sol = float(getattr(event, "sol", 0.0) or 0.0)
        slot_buyers = int(features.get("slot_buyers") or 0)
        slot_buy_sol = float(features.get("slot_buy_sol") or 0.0)
        slot_top_share = float(features.get("slot_top_share") or 0.0)
        s5_buys = int(features.get("s5_buys") or 0)
        s5_sell_sol = float(features.get("s5_sell_sol") or 0.0)
        s5_buy_sol = float(features.get("s5_buy_sol") or 0.0)
        last_buy_age_ms = int(features.get("last_buy_age_ms") or 0)
        last_sell_age_ms = int(features.get("last_sell_age_ms") or 0)

        # 1) priced_snap-like: fresh mint with broad burst in last 1.5s
        if (
            age_ms <= 6000
            and slot_buyers >= 3
            and slot_buy_sol >= 5.0
            and slot_top_share <= 0.55
        ):
            triggers = {
                "fresh_age_le_6s": True,
                "slot_buyers_ge_3": True,
                "slot_buy_sol_ge_5": True,
                "slot_top_le_0.55": True,
            }
            # tighter v30 priced_snap config: BUY1500>=7, UNIQ1500>=8, TOP1500<=0.37
            allowed = slot_buy_sol >= 7.0 and slot_buyers >= 8 and slot_top_share <= 0.37
            blocker = "" if allowed else "v30_priced_snap_filter_not_matched"
            return ("priced_snap_like", allowed, blocker, triggers)

        # 2) delayed_confirmation_pump: 6–30s old, still buying, sells low
        if (
            age_ms > 6000 and age_ms <= 30000
            and last_buy_age_ms < 1500
            and s5_buys >= 3
            and s5_sell_sol < max(s5_buy_sol, 0.05) * 0.30
        ):
            triggers = {
                "age_6_to_30s": True,
                "last_buy_recent": True,
                "s5_buys_ge_3": True,
                "sells_subdued": True,
            }
            return (
                "delayed_confirmation_pump",
                False,
                "no_actual_entry_lane_in_v30",
                triggers,
            )

        # 3) rug_bounce-like: dropped from peak, still seeing buyers
        peak_price = float(features.get("peak_price") or 0.0)
        last_price = float(features.get("price") or 0.0)
        if (
            peak_price > 0
            and last_price > 0
            and last_price < peak_price * 0.80
            and s5_buys >= 1
            and age_ms > 3000
        ):
            triggers = {
                "drop_ge_20pct": True,
                "s5_buys_ge_1": True,
                "age_gt_3s": True,
            }
            return (
                "rug_bounce_like",
                False,
                "frozen_lane_rug_bounce_diagnostics_only",
                triggers,
            )

        # 4) curve_lag_reveal-like (lane disabled at root in v30)
        if age_ms <= 8000 and slot_buy_sol >= 3.0 and slot_top_share <= 0.70:
            triggers = {
                "fresh_age_le_8s": True,
                "slot_buy_sol_ge_3": True,
                "slot_top_le_0.70": True,
            }
            return (
                "curve_lag_reveal_shadow",
                False,
                "frozen_lane_curve_lag_disabled_root",
                triggers,
            )

        # 5) raw momentum
        if event_sol >= 3.0 and age_ms <= 60000:
            triggers = {"event_sol_ge_3": True, "age_le_60s": True}
            return ("raw_momentum_shadow", False, "no_actual_entry_lane_in_v30", triggers)

        # 6) generic catch-all
        return (
            "generic_observation",
            False,
            "below_all_classifier_thresholds",
            {"fallback": True},
        )

    def _maybe_shadow_observe(self, event: PumpEvent, features: dict[str, Any]) -> None:
        """v30 — targeted sampler. Drops `generic_observation` and uses
        per-family event_sol thresholds so promising families get more density
        without flooding the broker with toxic samples.
        Does not open positions; does not affect any actual-entry logic.
        Canary mode (env-gated) may open exactly one dry-live position for
        P0 machinery validation.
        """
        if not env_bool("PGG2_SHADOW_OBSERVE_ENABLED", True):
            return
        if not getattr(event, "is_buy", False):
            return
        age_ms = int(features.get("age_ms") or 0)
        max_age_ms = env_int("PGG2_SHADOW_OBSERVE_MAX_AGE_MS", 30000)
        if age_ms > max_age_ms:
            return
        event_sol = float(getattr(event, "sol", 0.0) or 0.0)
        scout_sol = env_float("PGG2_SHADOW_OBSERVE_SCOUT_SOL", 0.015)

        lane, actual_entry_allowed, blocker, triggers = self._classify_shadow_lane(
            event, features
        )

        # v30 — per-family minimum event_sol. generic_observation is disabled.
        family_min_sol = {
            "priced_snap_like": env_float("PGG2_SHADOW_OBSERVE_MIN_SOL_PRICED_SNAP", 1.0),
            "delayed_confirmation_pump": env_float("PGG2_SHADOW_OBSERVE_MIN_SOL_DELAYED", 1.0),
            "rug_bounce_like": env_float("PGG2_SHADOW_OBSERVE_MIN_SOL_BOUNCE", 0.5),
            "curve_lag_reveal_shadow": env_float("PGG2_SHADOW_OBSERVE_MIN_SOL_CURVE_LAG", 1.0),
            "raw_momentum_shadow": env_float("PGG2_SHADOW_OBSERVE_MIN_SOL_RAW_MOMENTUM", 3.0),
            "generic_observation": float("inf"),  # disabled
        }
        min_sol = family_min_sol.get(lane, float("inf"))
        if event_sol < min_sol:
            return

        # per-(family, mint) cooldown so we don't redundantly sample the
        # same mint as the same family.
        cooldown_ms = env_int("PGG2_SHADOW_LAB_FAMILY_COOLDOWN_MS", 30000)
        ts_ms = int(event.ts_ms or now_ms())
        if not hasattr(self, "_shadow_family_last_ms"):
            self._shadow_family_last_ms: dict[tuple[str, str], int] = {}
        last = self._shadow_family_last_ms.get((lane, event.mint), 0)
        if ts_ms - last < cooldown_ms:
            return
        self._shadow_family_last_ms[(lane, event.mint)] = ts_ms

        # family count tracker for periodic heartbeat
        if not hasattr(self, "_shadow_family_counts"):
            self._shadow_family_counts: Counter = Counter()
            self._shadow_family_last_heartbeat_ms: int = 0
        self._shadow_family_counts[lane] = self._shadow_family_counts.get(lane, 0) + 1
        heartbeat_ms = env_int("PGG2_SHADOW_LAB_FAMILY_HEARTBEAT_MS", 300000)
        if ts_ms - self._shadow_family_last_heartbeat_ms >= heartbeat_ms:
            counts = ", ".join(f"{k}={v}" for k, v in sorted(self._shadow_family_counts.items()))
            log(f"SHADOW-LAB-FAMILY-COUNTS {counts}")
            self._shadow_family_last_heartbeat_ms = ts_ms

        plan = StrikePlan(
            mint=event.mint,
            ts_ms=int(event.ts_ms or now_ms()),
            lane=lane,
            reason=blocker or "permissive_observation",
            score=0.0,
            scout_sol=scout_sol,
            target_sol=scout_sol,
            price=float(features.get("price") or 0.0),
            needs_curve_fill=False,
            features=dict(features),
        )
        plan_meta = {
            "actual_entry_allowed": actual_entry_allowed,
            "actual_entry_blocker": blocker,
            "trigger_features": triggers,
        }
        self.record_shadow_candidate(event, features, plan, plan_meta=plan_meta)

    def _canary_safe_to_attempt(self) -> bool:
        """v30 — hard guard. Canary may only run in quote/dry-live mode and
        only up to PGG2_SHADOW_LAB_CANARY_MAX_ENTRIES openings per process."""
        broker = getattr(self, "broker", None)
        if broker is None:
            return False
        # mode must be quote/dry-live; real live is forbidden
        if getattr(broker, "mode", "") == "live":
            return False
        if getattr(broker, "quote_only", False) is not True:
            return False
        max_entries = env_int("PGG2_SHADOW_LAB_CANARY_MAX_ENTRIES", 1)
        if self._canary_entries >= max_entries:
            return False
        if self._canary_attempted:
            # we only race once; subsequent attempts wait for the existing
            # canary slot to fill or fail
            return False
        return True

    def _try_canary_entry(
        self,
        event: PumpEvent,
        features: dict[str, Any],
        plan: StrikePlan,
        quote_tokens: float,
        immediate_pnl: float,
        entry_quote: Optional[dict[str, Any]] = None,
        immediate_out: float = 0.0,
        record: Optional[dict[str, Any]] = None,
    ) -> Optional[Any]:
        """v30 — quote-locked canary open. Uses the broker's
        open_quote_shadow_from_quote so decision tokens == position tokens.
        Spawns the wall-clock risk supervisor on success.
        """
        if plan.lane != "shadow_lab_canary":
            return None
        if not env_bool("PGG2_ACTUAL_ENTRY_MASTER_ENABLED", False):
            log(
                f"PGG2-ACTUAL-ENTRY-BLOCKED reason=master_disabled "
                f"lane=shadow_lab_canary mint={short_addr(event.mint)}"
            )
            return None
        if not self._canary_safe_to_attempt():
            return None
        if quote_tokens <= 0 or entry_quote is None:
            return None
        max_immediate_loss = env_float(
            "PGG2_SHADOW_LAB_CANARY_MAX_IMMEDIATE_LOSS_SOL", 0.005
        )
        if immediate_pnl < -max_immediate_loss:
            return None
        # v32 — canary must also pass route-aware economic block.
        if hasattr(self.broker, "quote_all_in_pnl"):
            econ = self.broker.quote_all_in_pnl(
                route="pump_bc",
                cost_sol=float(plan.scout_sol),
                quote_out=float(immediate_out),
                execution_context={"ata_recoverable": True},
            )
            all_in = float(econ["all_in_pnl"])
            log(
                f"PGG2-PNL-BREAKDOWN canary mint={short_addr(event.mint)} "
                f"gross={econ['gross_quote_pnl']:+.6f} extra={econ['extra_overhead_not_in_quote']:.6f} "
                f"all_in={all_in:+.6f} basis={econ['pnl_basis']}"
            )
            min_all_in = env_float("PGG2_CANARY_MIN_ALL_IN_PNL_SOL", 0.0)
            if all_in < min_all_in:
                log(
                    f"PGG2-PREENTRY-ECON-BLOCK canary mint={short_addr(event.mint)} "
                    f"reason=immediate_all_in_pnl_negative all_in={all_in:+.6f} "
                    f"min={min_all_in:+.6f}"
                )
                return None
        record = record or {}
        quote_built_ts_ms = int(record.get("quote_built_ts_ms") or 0)
        now_ms_local = int(time.time() * 1000)
        quote_age_ms = now_ms_local - quote_built_ts_ms if quote_built_ts_ms else 99999
        max_age_ms = env_int("PGG2_MAX_ENTRY_QUOTE_AGE_MS", 150)
        if quote_age_ms > max_age_ms:
            log(
                f"PGG2-SHADOW-CANARY-SKIP {short_addr(event.mint)} reason=stale_quote "
                f"age_ms={quote_age_ms} max={max_age_ms}"
            )
            return None
        # mark attempted BEFORE the open call to make this strictly one-shot
        self._canary_attempted = True
        log(
            f"PGG2-SHADOW-CANARY-BUY {short_addr(event.mint)} amount={plan.scout_sol:.6f} "
            f"quote_tokens={quote_tokens:.6f} immediate_pnl={immediate_pnl:+.6f} "
            f"quote_age_ms={quote_age_ms} reason=canary_p0_validation_locked"
        )
        try:
            ok, _r = self.broker.can_strike(event.mint, int(event.ts_ms or now_ms()))
        except Exception as exc:
            log(f"PGG2-SHADOW-CANARY-CAN-STRIKE-FAIL {type(exc).__name__}: {exc}")
            self._canary_attempted = False
            return None
        if not ok:
            log("PGG2-SHADOW-CANARY-SKIP can_strike_returned_false")
            self._canary_attempted = False
            return None
        entry_context = {
            "quote_id": f"canary:{event.mint[:8]}:{quote_built_ts_ms}",
            "quote_age_ms": quote_age_ms,
            "immediate_pnl_at_decision": immediate_pnl,
        }
        try:
            pos = self.broker.open_quote_shadow_from_quote(
                plan, entry_quote, quote_tokens, immediate_out, entry_context
            )
        except Exception as exc:
            log(f"PGG2-SHADOW-CANARY-OPEN-FAIL {type(exc).__name__}: {exc}")
            self._canary_attempted = False
            return None
        if pos:
            # token equality sanity check
            if abs(float(pos.tokens_bought) - float(quote_tokens)) > 1e-6:
                log(
                    f"PGG2-POSITION-TOKEN-MISMATCH-FATAL canary mint={short_addr(event.mint)} "
                    f"pos_tokens={pos.tokens_bought} expected={quote_tokens}"
                )
                self.broker.positions.pop(event.mint, None)
                self._canary_attempted = False
                return None
            self._canary_entries += 1
            try:
                self.init_position_follow(pos, trusted=True, entry_features=features)
            except Exception as exc:
                log(f"PGG2-SHADOW-CANARY-FOLLOW-INIT-FAIL {type(exc).__name__}: {exc}")
            log(
                f"PGG2-SHADOW-CANARY-OPEN {short_addr(event.mint)} "
                f"cost={getattr(pos, 'cost_sol', 0.0):.6f} "
                f"tokens={getattr(pos, 'tokens_bought', 0.0):.6f}"
            )
            # v33 — active-position quote exclusivity for canary too.
            try:
                self.broker.mark_risk_owned(event.mint)
            except Exception as exc:
                log(f"PGG2-RISK-OWNED-MARK-FAIL {type(exc).__name__}: {exc}")
            # v31 — register with risk worker thread
            try:
                self._risk_worker.add_position(
                    event.mint,
                    "shadow_lab_canary",
                    "shadow_lab_canary",
                    int(getattr(pos, "opened_ts_ms", int(time.time() * 1000))),
                    float(getattr(pos, "last_price", 0.0)),
                )
            except Exception as exc:
                log(f"PGG2-RISK-WORKER-REGISTER-FAIL {type(exc).__name__}: {exc}")
        else:
            log(
                f"PGG2-SHADOW-CANARY-OPEN-NULL {short_addr(event.mint)} "
                f"(open_quote_shadow_from_quote returned None)"
            )
            self._canary_attempted = False  # allow retry on next candidate
        return pos

    async def _position_risk_supervisor(self, mint: str, rule_id: str) -> None:
        """v30 — wall-clock supervisor. Polls clamp/bank independently of the
        event-driven manage loop so the configured 100ms cadence is actually
        enforced.
        """
        broker = getattr(self, "broker", None)
        if broker is None:
            return
        interval_ms = env_int("PGG2_RISK_SUPERVISOR_INTERVAL_MS", 100)
        first_tick_delay_ms = env_int("PGG2_RISK_SUPERVISOR_FIRST_TICK_MS", 50)
        max_lifetime_ms = env_int("PGG2_RISK_SUPERVISOR_MAX_LIFETIME_MS", 11000)
        spawn_ts = time.time()
        # first-tick deadline
        first_tick_wall = spawn_ts + (first_tick_delay_ms / 1000.0)
        await asyncio.sleep(max(0.0, first_tick_wall - time.time()))
        last_tick_ts = time.time()
        ticks = 0
        try:
            while True:
                if time.time() - spawn_ts > (max_lifetime_ms / 1000.0):
                    log(f"PGG2-RISK-SUPERVISOR-EXIT mint={short_addr(mint)} reason=max_lifetime")
                    return
                pos = broker.positions.get(mint)
                if pos is None:
                    return  # position closed by another path
                tick_ts = time.time()
                wall_ms = int(tick_ts * 1000)
                delay_ms = (tick_ts - last_tick_ts) * 1000.0
                expected_ms = interval_ms if ticks > 0 else first_tick_delay_ms
                if delay_ms > expected_ms * 1.5:
                    log(
                        f"PGG2-RISK-TICK-LATE mint={short_addr(mint)} delay_ms={delay_ms:.0f} "
                        f"expected_ms={expected_ms}"
                    )
                last_tick_ts = tick_ts
                ticks += 1
                age_ms = wall_ms - getattr(pos, "opened_ts_ms", wall_ms)
                # call clamp first
                try:
                    clamp_action = broker.quote_loss_clamp_reason(pos, wall_ms)
                except Exception as exc:
                    log(f"PGG2-RISK-TICK-ERR mint={short_addr(mint)} clamp {type(exc).__name__}: {exc}")
                    clamp_action = ""
                if clamp_action:
                    log(
                        f"PGG2-RISK-TICK mint={short_addr(mint)} age_ms={age_ms} "
                        f"trigger=clamp ticks={ticks}"
                    )
                    self.close_position(
                        mint, wall_ms, pos.last_price, clamp_action, {}, killed=True
                    )
                    return
                # bank
                try:
                    bank_action = broker.quote_profit_bank_reason(pos, wall_ms)
                except Exception as exc:
                    log(f"PGG2-RISK-TICK-ERR mint={short_addr(mint)} bank {type(exc).__name__}: {exc}")
                    bank_action = ""
                if bank_action:
                    log(
                        f"PGG2-RISK-TICK mint={short_addr(mint)} age_ms={age_ms} "
                        f"trigger=bank ticks={ticks}"
                    )
                    self.close_position(
                        mint, wall_ms, pos.last_price, bank_action, {}, killed=False
                    )
                    return
                # heartbeat tick (no action)
                if ticks % 5 == 1:
                    log(
                        f"PGG2-RISK-TICK mint={short_addr(mint)} age_ms={age_ms} trigger=none "
                        f"ticks={ticks}"
                    )
                await asyncio.sleep(interval_ms / 1000.0)
        except Exception as exc:
            log(f"PGG2-RISK-SUPERVISOR-FATAL mint={short_addr(mint)} {type(exc).__name__}: {exc}")

    def _pilot_safe_to_attempt(self) -> bool:
        """Strict guard for the drylive pilot entry path. Refuses real-live."""
        if not env_bool("PGG2_DRYLIVE_PILOT_ENABLED", False):
            return False
        broker = getattr(self, "broker", None)
        if broker is None:
            return False
        if getattr(broker, "mode", "") == "live":
            return False
        if getattr(broker, "quote_only", False) is not True:
            return False
        max_entries = env_int("PGG2_DRYLIVE_PILOT_MAX_ENTRIES", 3)
        if self._pilot_entries >= max_entries:
            return False
        session_loss_cap = env_float("PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL", 0.006)
        if self._pilot_session_loss_sol <= -session_loss_cap:
            return False
        return True

    def _try_pilot_entry(
        self,
        event: PumpEvent,
        features: dict[str, Any],
        plan: StrikePlan,
        quote_tokens: float,
        immediate_pnl: float,
        record: dict[str, Any],
        entry_quote: Optional[dict[str, Any]] = None,
        immediate_out: float = 0.0,
    ) -> Optional[Any]:
        """v30 — controlled dry-live pilot for rule
        `v33_quote_edge_150_C`. Quote-locked entry; supervisor
        spawned after open.
        """
        if not env_bool("PGG2_ACTUAL_ENTRY_MASTER_ENABLED", False):
            log(
                f"PGG2-ACTUAL-ENTRY-BLOCKED reason=master_disabled "
                f"lane=v33_quote_edge_150_C mint={short_addr(event.mint)}"
            )
            return None
        if not self._pilot_safe_to_attempt():
            return None
        if not record.get("execution_eligible"):
            log(f"PGG2-PILOT-PREENTRY-BLOCK mint={short_addr(event.mint)} blocker=fallback_only")
            return None
        if not record.get("direct_quote_success") or not record.get("direct_sell_quote_success"):
            log(f"PGG2-PILOT-PREENTRY-BLOCK mint={short_addr(event.mint)} blocker=fallback_only")
            return None
        if entry_quote is None:
            log(f"PGG2-PILOT-PREENTRY-BLOCK mint={short_addr(event.mint)} blocker=no_quote_object")
            return None
        # quote age check
        quote_built_ts_ms = int(record.get("quote_built_ts_ms") or 0)
        now_ms_local = int(time.time() * 1000)
        quote_age_ms = now_ms_local - quote_built_ts_ms if quote_built_ts_ms else 99999
        max_age_ms = env_int("PGG2_MAX_ENTRY_QUOTE_AGE_MS", 150)
        if quote_age_ms > max_age_ms:
            log(
                f"PGG2-PILOT-PREENTRY-BLOCK mint={short_addr(event.mint)} blocker=stale_quote "
                f"age_ms={quote_age_ms} max={max_age_ms}"
            )
            return None
        # projected session loss check
        max_allowed_loss = env_float("PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_FLOOR_SOL", 0.00150)
        session_cap = env_float("PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL", 0.006)
        projected = self._pilot_session_loss_sol - max_allowed_loss
        if projected < -session_cap:
            log(
                f"PGG2-PILOT-PREENTRY-BLOCK mint={short_addr(event.mint)} blocker=projected_session_loss_cap "
                f"current={self._pilot_session_loss_sol:+.6f} max_loss={max_allowed_loss:.6f} "
                f"projected={projected:+.6f} cap={-session_cap:+.6f}"
            )
            return None
        # v32 — fast-quote requirement for actual entry. Sim-select-based
        # buys take 700-1700ms and cannot be risk-managed within a 1.5mSOL
        # loss budget. Block if pair_source starts with "sim_selected:".
        # Shadow lab still runs the slow path for research; only actual entry
        # is gated.
        if env_bool("PGG2_ACTUAL_ENTRY_REQUIRE_FAST_QUOTE", True):
            pair_source_str = str(record.get("pair_source", "unknown"))
            if pair_source_str.startswith("sim_selected:"):
                log(
                    f"PGG2-PREENTRY-FASTQUOTE-BLOCK mint={short_addr(event.mint)} "
                    f"reason=sim_needed pair_source={pair_source_str}"
                )
                return None
        # v32 — route-aware economic block. Compute all-in PnL using the new
        # CostModel and refuse to open if it is negative (or below floor).
        immediate_out_for_econ = float(record.get("immediate_reverse_out") or immediate_out)
        if hasattr(self.broker, "quote_all_in_pnl"):
            econ = self.broker.quote_all_in_pnl(
                route="pump_bc",
                cost_sol=float(plan.scout_sol),
                quote_out=immediate_out_for_econ,
                quote_metadata={"sell_fee_sol": 0.0},
                execution_context={"ata_recoverable": True},
            )
            all_in = float(econ["all_in_pnl"])
            record["pnl_all_in_at_entry"] = all_in
            record["pnl_basis"] = econ["pnl_basis"]
            log(
                f"PGG2-PNL-BREAKDOWN mint={short_addr(event.mint)} route=pump_bc "
                f"cost={econ['cost_sol']:.6f} quote_out={econ['quote_out']:.6f} "
                f"gross={econ['gross_quote_pnl']:+.6f} extra={econ['extra_overhead_not_in_quote']:.6f} "
                f"all_in={all_in:+.6f} basis={econ['pnl_basis']}"
            )
            min_all_in = env_float("PGG2_PREENTRY_MIN_ALL_IN_PNL_SOL", 0.0)
            if all_in < min_all_in:
                log(
                    f"PGG2-PREENTRY-ECON-BLOCK mint={short_addr(event.mint)} "
                    f"reason=immediate_all_in_pnl_negative all_in={all_in:+.6f} "
                    f"min={min_all_in:+.6f}"
                )
                return None
            floor = env_float("PGG2_PREENTRY_LATENCY_ADJUSTED_FLOOR_SOL", 0.0)
            if floor > 0 and all_in < floor:
                log(
                    f"PGG2-PREENTRY-ECON-BLOCK mint={short_addr(event.mint)} "
                    f"reason=below_latency_adjusted_floor all_in={all_in:+.6f} "
                    f"floor={floor:+.6f}"
                )
                return None
        # v31 — latency feasibility gate
        if env_bool("PGG2_LATENCY_FEASIBILITY_ENABLED", True):
            pair_source = str(record.get("pair_source", "unknown"))
            broker = self.broker
            buy_p95 = None
            sell_p95 = None
            if hasattr(broker, "quote_latency_percentile"):
                buy_p95 = broker.quote_latency_percentile("buy", pair_source, 95)
                sell_p95 = broker.quote_latency_percentile("sell", pair_source, 95)
            max_p95_ms = env_float("PGG2_LATENCY_MAX_P95_MS", 1000.0)
            if buy_p95 is not None and buy_p95 > max_p95_ms:
                log(
                    f"PGG2-PREENTRY-LATENCY-BLOCK mint={short_addr(event.mint)} "
                    f"blocker=quote_latency_p95_too_high side=buy source={pair_source} "
                    f"buy_p95_ms={buy_p95:.0f} max={max_p95_ms:.0f}"
                )
                return None
            if sell_p95 is not None and sell_p95 > max_p95_ms:
                log(
                    f"PGG2-PREENTRY-LATENCY-BLOCK mint={short_addr(event.mint)} "
                    f"blocker=quote_latency_p95_too_high side=sell source={pair_source} "
                    f"sell_p95_ms={sell_p95:.0f} max={max_p95_ms:.0f}"
                )
                return None
        # immediate executable pnl >= -0.00150 SOL
        if immediate_pnl < env_float("PGG2_DRYLIVE_PILOT_MIN_IMMEDIATE_PNL_SOL", -0.00150):
            return None
        lane_candidate = record.get("lane_candidate", "")
        # exclude raw_momentum and generic_observation paths
        if lane_candidate in {"raw_momentum_shadow", "generic_observation"}:
            return None
        # recovered within <= 250ms, OR immediate direct quote success at t=0
        recovered = bool(record.get("quote_recovered"))
        first_q_ms = int(record.get("first_quoteable_ms") or -1)
        if recovered:
            if not (0 <= first_q_ms <= 250):
                return None
        # buy quote impact safety threshold (conservative default if unknown)
        buy_impact = float(record.get("entry_quote_impact") or 0.0)
        impact_max = env_float("PGG2_DRYLIVE_PILOT_MAX_BUY_IMPACT", 0.005)
        if buy_impact > impact_max:
            log(
                f"PGG2-DRYLIVE-PILOT-SKIP {short_addr(event.mint)} reason=buy_impact_high "
                f"impact={buy_impact:.6f} max={impact_max:.6f}"
            )
            return None
        # v30 fix — reject high single-buyer concentration. AFYY..9Cip lost
        # -0.00461 SOL on 2026-05-11 with slot_top_share=0.60 (one buyer
        # dominating 60% of slot volume). Coordinated-dump signature must not
        # enter.
        slot_top_share = float(record.get("slot_top_share") or 0.0)
        slot_top_max = env_float("PGG2_DRYLIVE_PILOT_MAX_SLOT_TOP_SHARE", 0.50)
        if slot_top_share > slot_top_max:
            log(
                f"PGG2-DRYLIVE-PILOT-SKIP {short_addr(event.mint)} reason=slot_top_high "
                f"slot_top={slot_top_share:.3f} max={slot_top_max:.3f}"
            )
            return None
        # v30 fix — require at least 2 distinct slot buyers
        slot_buyers = int(record.get("slot_buyers") or 0)
        min_slot_buyers = env_int("PGG2_DRYLIVE_PILOT_MIN_SLOT_BUYERS", 3)
        if slot_buyers < min_slot_buyers:
            log(
                f"PGG2-DRYLIVE-PILOT-SKIP {short_addr(event.mint)} reason=slot_buyers_low "
                f"slot_buyers={slot_buyers} min={min_slot_buyers}"
            )
            return None
        # no no-quote-token fallback and no economic-only fallback
        if record.get("economic_quote_source") not in (None, "none", "direct"):
            return None
        # per-mint pilot cooldown: don't re-enter same mint
        if event.mint in self._pilot_mints_seen:
            return None
        # Build pilot plan
        pilot_scout = env_float("PGG2_DRYLIVE_PILOT_SOL", 0.015)
        ts_ms = int(event.ts_ms or now_ms())
        pilot_plan = StrikePlan(
            mint=event.mint,
            ts_ms=ts_ms,
            lane="v33_quote_edge_150_C",
            reason="drylive_pilot",
            score=0.0,
            scout_sol=pilot_scout,
            target_sol=pilot_scout,
            price=pilot_scout / max(quote_tokens, 1e-18),
            needs_curve_fill=False,
            features=dict(features),
        )
        self._pilot_attempted = True
        self._pilot_mints_seen.add(event.mint)
        log(
            f"PGG2-DRYLIVE-PILOT-BUY rule_id={RULE_V33_QUOTE_EDGE_150_C} "
            f"policy_id={POLICY_V33_C_MOONSHOT} "
            f"pnl_model_version={PNL_MODEL_VERSION_V33} "
            f"mint={short_addr(event.mint)} amount={pilot_scout:.6f} "
            f"quote_tokens={quote_tokens:.6f} immediate_out={record.get('immediate_reverse_out', 0.0):.6f} "
            f"immediate_pnl={immediate_pnl:+.6f} buy_impact={buy_impact:.6f} "
            f"pair_source={record.get('pair_source', 'none')} "
            f"first_quoteable_ms={first_q_ms} "
            f"entry_features={{age_ms:{record.get('age_ms')},slot_buyers:{record.get('slot_buyers')},slot_buy_sol:{record.get('slot_buy_sol')},slot_top:{record.get('slot_top_share')}}}"
        )
        try:
            ok, _r = self.broker.can_strike(event.mint, ts_ms)
        except Exception as exc:
            log(f"PGG2-DRYLIVE-PILOT-CAN-STRIKE-FAIL {type(exc).__name__}: {exc}")
            return None
        if not ok:
            log(f"PGG2-DRYLIVE-PILOT-SKIP {short_addr(event.mint)} reason=can_strike_false")
            return None
        # v30 — atomic locked-quote open. Decision quote_tokens == position tokens.
        entry_context = {
            "quote_id": f"pilot:{event.mint[:8]}:{quote_built_ts_ms}",
            "quote_age_ms": quote_age_ms,
            "immediate_pnl_at_decision": immediate_pnl,
            "buy_impact": buy_impact,
        }
        try:
            pos = self.broker.open_quote_shadow_from_quote(
                pilot_plan, entry_quote, quote_tokens, immediate_out, entry_context
            )
        except Exception as exc:
            log(f"PGG2-DRYLIVE-PILOT-OPEN-FAIL {type(exc).__name__}: {exc}")
            return None
        if pos:
            # token equality sanity check
            if abs(float(pos.tokens_bought) - float(quote_tokens)) > 1e-6:
                log(
                    f"PGG2-POSITION-TOKEN-MISMATCH-FATAL pilot mint={short_addr(event.mint)} "
                    f"pos_tokens={pos.tokens_bought} expected={quote_tokens}"
                )
                # try to close immediately to avoid further damage
                try:
                    self.broker.positions.pop(event.mint, None)
                except Exception:
                    pass
                return None
            self._pilot_entries += 1
            try:
                self.init_position_follow(pos, trusted=True, entry_features=features)
            except Exception as exc:
                log(f"PGG2-DRYLIVE-PILOT-FOLLOW-INIT-FAIL {type(exc).__name__}: {exc}")
            log(
                f"PGG2-DRYLIVE-PILOT-OPEN mint={short_addr(event.mint)} "
                f"cost={getattr(pos, 'cost_sol', 0.0):.6f} "
                f"tokens={getattr(pos, 'tokens_bought', 0.0):.6f} "
                f"entries_used={self._pilot_entries} session_loss={self._pilot_session_loss_sol:+.6f} "
                f"rule_id={RULE_V33_QUOTE_EDGE_150_C} policy_id={POLICY_V33_C_MOONSHOT} "
                f"pnl_model_version={PNL_MODEL_VERSION_V33}"
            )
            # v33 — active-position quote exclusivity. Shadow lab + delayed
            # scanner consult is_risk_owned and skip parallel quotes from
            # this point until close clears the flag.
            try:
                self.broker.mark_risk_owned(event.mint)
            except Exception as exc:
                log(f"PGG2-RISK-OWNED-MARK-FAIL {type(exc).__name__}: {exc}")
            # v31 — register with the threaded risk worker (replaces async supervisor)
            try:
                self._risk_worker.add_position(
                    event.mint,
                    RULE_V33_QUOTE_EDGE_150_C,
                    RULE_V33_QUOTE_EDGE_150_C,
                    int(getattr(pos, "opened_ts_ms", int(time.time() * 1000))),
                    float(getattr(pos, "last_price", 0.0)),
                )
            except Exception as exc:
                log(f"PGG2-RISK-WORKER-REGISTER-FAIL {type(exc).__name__}: {exc}")
        else:
            log(
                f"PGG2-DRYLIVE-PILOT-OPEN-NULL mint={short_addr(event.mint)} "
                f"(open_quote_shadow_from_quote returned None)"
            )
        return pos

    @staticmethod
    def quote_net_pnl(cost: float, quote_out: float, overhead: float, fees: float = 0.0) -> float:
        """v30 — unified PnL basis. Mirrors broker.RaptorLiveBroker.quote_net_pnl."""
        return float(quote_out) - float(overhead) - float(fees) - float(cost)

    async def _direct_buy_quote_with_retry(
        self,
        broker: Any,
        mint: str,
        amount: float,
        record: dict[str, Any],
    ) -> tuple[Optional[dict[str, Any]], float, dict[str, Any]]:
        """v30 — try the direct buy quote, retrying after short delays when the
        initial error is curve_missing (mint with no bonding curve account yet).

        Returns (entry_quote_dict_or_None, quote_tokens, meta_dict).
        meta_dict has: initial_direct_quote_error_class, initial_direct_quote_error,
        quote_recovered, first_quoteable_ms.
        """
        delays_str = env_str("PGG2_SHADOW_LAB_CURVE_RETRY_MS", "0,100,250,500,1000,2000")
        try:
            delays = [int(x.strip()) for x in delays_str.split(",") if x.strip()]
        except Exception:
            delays = [0, 100, 250, 500, 1000, 2000]
        if not env_bool("PGG2_SHADOW_LAB_CURVE_MISSING_RETRY", True):
            delays = [0]
        meta: dict[str, Any] = {"quote_recovered": False}
        initial_err = None
        initial_class = None
        prev = 0
        for i, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(max(0.0, (delay - prev) / 1000.0))
                prev = delay
                # only retry when initial error is curve_missing
                if initial_class != "curve_missing":
                    break
            try:
                t0 = time.perf_counter()
                entry = await asyncio.to_thread(
                    broker.build_swap, SOL_MINT, mint, amount, broker.buy_slippage
                )
                elapsed = (time.perf_counter() - t0) * 1000.0
                quote_built_ts_ms = int(time.time() * 1000)
                quote_tokens = float(broker.rate_amount_out(entry))
                if quote_tokens > 0:
                    record["entry_quote_ms"] = elapsed
                    record["quote_built_ts_ms"] = quote_built_ts_ms
                    if i > 0:
                        # recovered
                        record["quote_recovered"] = True
                        record["first_quoteable_ms"] = delay
                        record["initial_direct_quote_error_class"] = initial_class
                        record["initial_direct_quote_error"] = initial_err
                        record["recovered_quote_tokens"] = quote_tokens
                        log(
                            f"SHADOW-LAB-QUOTE-RECOVERED mint={short_addr(str(mint))} "
                            f"first_quoteable_ms={delay} tokens={quote_tokens:.6f}"
                        )
                        meta["quote_recovered"] = True
                        meta["first_quoteable_ms"] = delay
                    else:
                        record["quote_recovered"] = False
                    return entry, quote_tokens, meta
                # amountOut <= 0 — treat as not quoteable; do not retry
                record["entry_quote_ms"] = elapsed
                record["entry_quote_tokens"] = quote_tokens
                record["no_quote_side"] = "buy"
                record["no_quote_reason"] = "amountOut_le_zero"
                record["direct_quote_error_class"] = "amount_out_zero"
                return None, 0.0, meta
            except Exception as exc:
                err_str = f"{type(exc).__name__}: {exc}"
                cls = self._classify_quote_error(err_str)
                if i == 0:
                    initial_err = err_str
                    initial_class = cls
                    record["initial_direct_quote_error"] = err_str
                    record["initial_direct_quote_error_class"] = cls
                    record["entry_quote_error"] = err_str
                    record["direct_quote_error_class"] = cls
                    record["no_quote_side"] = "buy"
                if cls != "curve_missing":
                    return None, 0.0, meta
        return None, 0.0, meta

    @staticmethod
    def _classify_quote_error(err_str: str) -> str:
        s = (err_str or "").lower()
        if "no confirmed pump buyback" in s or "buyback" in s:
            return "missing_pair"
        if "no_buyback_pair_simulated_ok" in s:
            return "sim_failed"
        if "bonding curve" in s and "missing" in s:
            return "curve_missing"
        if "pumpswap" in s and ("pool" in s or "pair" in s):
            return "pool_missing"
        if "no confirmed pump buyback/social pair observed" in s:
            return "missing_pair"
        if "gettransaction" in s or "transaction not found" in s or "not found" in s:
            return "rpc_missing_tx"
        if "simulat" in s and "fail" in s:
            return "sim_failed"
        if "blockhash" in s or "rpc" in s.split()[0:1]:
            return "rpc_error"
        return "other"

    async def _try_raptor_economic_fallback(
        self,
        broker: Any,
        from_mint: str,
        to_mint: str,
        amount: Any,
        slippage: float,
        side: str,
        record: dict[str, Any],
    ) -> float:
        """v30 — economic fallback via the Raptor (parent class) build_swap.
        Used only for the executable shadow lab — never to execute a real
        trade. Marks the record so the lab clearly separates direct executable
        from economic-only quotes.
        """
        if not env_bool("PGG2_SHADOW_LAB_RAPTOR_FALLBACK", True):
            return 0.0
        # Only run when broker is in quote/dry-live mode (extra safety).
        if getattr(broker, "mode", "") == "live":
            return 0.0
        if getattr(broker, "quote_only", False) is not True:
            return 0.0
        try:
            from pgg2_live_raptor import RaptorLiveBroker  # local import
        except Exception:
            return 0.0
        try:
            t0 = time.perf_counter()
            q = await asyncio.to_thread(
                RaptorLiveBroker.build_swap, broker, from_mint, to_mint, amount, slippage
            )
            ms = (time.perf_counter() - t0) * 1000.0
            out = float(broker.rate_amount_out(q))
            if out > 0:
                record["economic_quote_success"] = True
                record["economic_quote_source"] = "raptor_fallback"
                record[f"economic_{side}_quote_ms"] = ms
                record[f"economic_{side}_quote_out"] = out
                log(
                    f"SHADOW-LAB-ECON-FALLBACK {short_addr(str(record.get('mint')))} side={side} "
                    f"out={out:.6f} ms={ms:.0f}"
                )
                return out
        except Exception as exc:
            record[f"economic_{side}_quote_error"] = f"{type(exc).__name__}: {exc}"
        return 0.0

    def _write_shadow_record(self, record: dict[str, Any]) -> None:
        try:
            path = self._shadow_lab_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            label = record.get("label", "?")
            log(
                f"SHADOW-LAB-REC {short_addr(record['mint'])} lane={record.get('lane_candidate')} "
                f"label={label} pnl_model=v33_route_aware "
                f"all_in_immediate_pnl={record.get('all_in_immediate_pnl', 0.0):+.6f} "
                f"all_in_best_pnl_lookahead={record.get('all_in_best_pnl_lookahead', 0.0):+.6f}"
                f"@{record.get('all_in_time_to_best_ms', record.get('time_to_best_ms', 0))}ms "
                f"legacy_immediate_pnl={record.get('legacy_immediate_pnl', record.get('immediate_pnl', 0.0)):+.6f}"
            )
        except Exception as exc:
            log(f"SHADOW-LAB-WRITE-ERR {type(exc).__name__}: {exc}")

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
        plan = self.rug_bounce_ready(event, features)  # v29m — profit FROM rugs
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
        # v29 — outsmart gate (cashback + v5 spike + s1 seller filters)
        passed, _reason = self._outsmart_gate(event, features, "curve_lag_reveal")
        if not passed:
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

    def rug_bounce_ready(self, event: PumpEvent, features: dict[str, Any]) -> Optional[StrikePlan]:
        """v29m (2026-05-10) — RUG BOUNCE HUNTER lane.

        Profit FROM rugs instead of fighting them. When a mint pumps and
        rugs (50%+ drop from peak in seconds), there's a structural bounce
        as panic-buyers and dip-bots rush in. Enter at the floor, exit
        on the +15-25% bounce. This is the OPPOSITE of priced_snap:
        we WANT to see the rug happen first, then enter the recovery.
        """
        if not env_bool("PGG2_RUG_BOUNCE_ENABLED", True):
            return None
        # v29m diagnostics — count where we reject
        if not hasattr(self, "_rug_bounce_diag"):
            self._rug_bounce_diag = {"checks": 0, "not_buy": 0, "seen": 0,
                                     "in_pos": 0, "complete": 0, "no_tape": 0,
                                     "no_prices": 0, "price_zero": 0, "no_peak": 0,
                                     "drop_too_small": 0, "peak_too_recent": 0,
                                     "peak_too_old": 0, "no_pump": 0,
                                     "low_buys": 0, "sells_dom": 0, "fired": 0}
        self._rug_bounce_diag["checks"] += 1
        if self._rug_bounce_diag["checks"] % 30 == 0:
            d = self._rug_bounce_diag
            log(
                f"PGG2-RUG-BOUNCE-DIAG checks={d['checks']} not_buy={d['not_buy']} "
                f"seen={d['seen']} in_pos={d['in_pos']} no_tape={d['no_tape']} "
                f"no_prices={d['no_prices']} no_peak={d['no_peak']} "
                f"drop_too_small={d['drop_too_small']} peak_too_recent={d['peak_too_recent']} "
                f"peak_too_old={d['peak_too_old']} no_pump={d['no_pump']} "
                f"low_buys={d['low_buys']} sells_dom={d['sells_dom']} fired={d['fired']}"
            )
        if not event.is_buy:
            self._rug_bounce_diag["not_buy"] += 1
            return None
        if event.mint in self.rug_bounce_seen:
            self._rug_bounce_diag["seen"] += 1
            return None
        if event.mint in self.broker.positions or event.mint in self.broker.pending:
            self._rug_bounce_diag["in_pos"] += 1
            return None
        if features.get("complete"):
            self._rug_bounce_diag["complete"] += 1
            return None
        tape = self.tapes.get(event.mint)
        if not tape:
            self._rug_bounce_diag["no_tape"] += 1
            return None
        if len(tape.prices) < 2:  # was 5; loosened
            self._rug_bounce_diag["no_prices"] += 1
            return None
        ts_ms = event.ts_ms
        current_price = float(features.get("price") or 0.0)
        if current_price <= 0:
            self._rug_bounce_diag["price_zero"] += 1
            return None
        # Find peak in last 60s window
        cutoff_ts = ts_ms - 60_000
        peak_price = 0.0
        peak_ts = 0
        first_price_in_window = 0.0
        for (pts, pp) in tape.prices:
            try:
                pp_f = float(pp or 0.0)
            except Exception:
                continue
            if pts < cutoff_ts:
                continue
            if first_price_in_window == 0.0:
                first_price_in_window = pp_f
            if pp_f > peak_price:
                peak_price = pp_f
                peak_ts = int(pts)
        if peak_price <= 0 or first_price_in_window <= 0:
            self._rug_bounce_diag["no_peak"] += 1
            return None
        drop_ratio = current_price / peak_price
        max_remaining = 1.0 - env_float("PGG2_RUG_BOUNCE_MIN_DROP", 0.40)
        if drop_ratio > max_remaining:
            self._rug_bounce_diag["drop_too_small"] += 1
            return None
        # v29n — also reject if drop is TOO deep (mint is dying, no bounce)
        min_remaining = 1.0 - env_float("PGG2_RUG_BOUNCE_MAX_DROP", 0.65)
        if drop_ratio < min_remaining:
            self._rug_bounce_diag.setdefault("drop_too_deep", 0)
            self._rug_bounce_diag["drop_too_deep"] += 1
            return None
        elapsed_since_peak_s = (ts_ms - peak_ts) / 1000.0
        if elapsed_since_peak_s < env_float("PGG2_RUG_BOUNCE_MIN_PEAK_AGE_S", 3.0):
            self._rug_bounce_diag["peak_too_recent"] += 1
            return None
        if elapsed_since_peak_s > env_float("PGG2_RUG_BOUNCE_MAX_PEAK_AGE_S", 30.0):
            self._rug_bounce_diag["peak_too_old"] += 1
            return None
        if peak_price < first_price_in_window * env_float("PGG2_RUG_BOUNCE_MIN_PUMP_RATIO", 1.30):
            self._rug_bounce_diag["no_pump"] += 1
            return None
        s5 = tape.stats(5000, ts_ms)
        min_s5_buys = env_int("PGG2_RUG_BOUNCE_MIN_S5_BUYS", 3)
        if int(s5.buys) < min_s5_buys:
            self._rug_bounce_diag["low_buys"] += 1
            return None
        if s5.sell_sol > s5.buy_sol * env_float("PGG2_RUG_BOUNCE_MAX_S5_SELL_RATIO", 0.5):
            self._rug_bounce_diag["sells_dom"] += 1
            return None
        # v29o (2026-05-11) — WHALE-TRIGGER: only fire if THIS event is a
        # whale buy (>= 0.5 SOL). Retail dust buyers panic-sell on first
        # red candle. Whales committing 0.5+ SOL on a rugged mint signal
        # real conviction → bounce more likely.
        min_event_sol = env_float("PGG2_RUG_BOUNCE_MIN_EVENT_SOL", 0.5)
        if event.sol < min_event_sol:
            self._rug_bounce_diag.setdefault("not_whale", 0)
            self._rug_bounce_diag["not_whale"] += 1
            return None
        self._rug_bounce_diag["fired"] += 1
        scout = env_float("PGG2_RUG_BOUNCE_SCOUT_SOL", 0.010)
        target = env_float("PGG2_RUG_BOUNCE_TARGET_SOL", 0.010)
        reason = (
            f"rug_bounce drop={drop_ratio:.2f} peak_age={elapsed_since_peak_s:.1f}s "
            f"peak_pump={peak_price/first_price_in_window:.2f}x s5_buys={int(s5.buys)} "
            f"s5_sells={int(s5.sells)}"
        )
        log(f"PGG2-RUG-BOUNCE-FIRE mint={event.mint[:8]} {reason}")
        self.rug_bounce_seen.add(event.mint)
        return StrikePlan(
            mint=event.mint,
            ts_ms=ts_ms,
            lane="bounce_buy",
            reason=reason,
            score=80.0,
            scout_sol=scout,
            target_sol=target,
            price=current_price,
            needs_curve_fill=False,
            features={
                "entry_size_reason": "rug_bounce",
                "entry_probe_sol": scout,
                "rug_bounce_drop_ratio": drop_ratio,
                "rug_bounce_peak_age_s": elapsed_since_peak_s,
                "rug_bounce_pump_ratio": peak_price / first_price_in_window,
            },
        )

    def _outsmart_gate(self, event: PumpEvent, features: dict[str, Any], lane: str) -> tuple[bool, str]:
        """v29 (2026-05-10) — combined edges 1, 2(alt), 3 to outsmart competitors.

        Edges:
          1) Cashback filter — only trade Pump.fun cashback-eligible coins
             (bonding curve byte 82). Free positive-EV rebate per memory note.
          2) Last-1s seller filter — block if sell pressure dominates the
             most recent 1s window (acute reversal already started).
          3) Velocity_5s spike filter — block if last 5s spiked >15%
             (orchestrated pump-and-dump bait, not organic momentum).
        Returns (passed, reason). Caller should skip if passed=False.
        """
        mint = event.mint
        ts_ms = event.ts_ms
        diag_key = f"_outsmart_diag_{lane}"
        if not hasattr(self, "_outsmart_diag"):
            self._outsmart_diag: dict[str, dict[str, int]] = {}
        bucket = self._outsmart_diag.setdefault(lane, {"checks": 0, "no_cashback": 0,
                                                       "v5_spike": 0, "s1_seller": 0,
                                                       "cashback_check_failed": 0,
                                                       "passed": 0})
        bucket["checks"] += 1

        # Edge 1: Cashback eligibility (default ON; can be disabled via env)
        if env_bool("PGG2_OUTSMART_CASHBACK_ONLY", True):
            try:
                cache = getattr(self, "_outsmart_cashback_cache", None)
                if cache is None:
                    self._outsmart_cashback_cache = {}
                    cache = self._outsmart_cashback_cache
                if mint in cache:
                    eligible = cache[mint]
                else:
                    from solders.pubkey import Pubkey  # type: ignore
                    pk = Pubkey.from_string(mint)
                    curve = self.broker.bonding_curve(pk)
                    eligible = bool(getattr(curve, "cashback_enabled", False))
                    cache[mint] = eligible
                if not eligible:
                    bucket["no_cashback"] += 1
                    return False, "no_cashback"
            except Exception:
                bucket["cashback_check_failed"] += 1
                return False, "cashback_check_failed"

        tape = self.tapes.get(mint)
        if tape is None:
            bucket["passed"] += 1
            return True, "no_tape"

        # Edge 3: Velocity_5s spike filter + v29h ACCELERATION filter
        # FEATSNAP analysis (2026-05-10) showed:
        #   - All fresh-mint losers had v5==v15==v30 (degenerate, no buildup)
        #   - The 3DPB -68% loser had v5=1.006 < v15=1.347 (REVERSING pump)
        # Filter: require v5 / v15 > min ratio = ACCELERATING upward.
        # This single test rejects both reversers and stale snaps.
        if env_bool("PGG2_OUTSMART_V5_SPIKE_BLOCK", True):
            s5 = tape.stats(5000, ts_ms)
            s15 = tape.stats(15000, ts_ms)
            current_price = float(features.get("price") or 0.0)
            first_5s = s5.first_price if s5.first_price > 0 else current_price
            first_15s = s15.first_price if s15.first_price > 0 else current_price
            if first_5s > 0 and current_price > 0:
                v5 = current_price / first_5s
                max_v5 = env_float("PGG2_OUTSMART_MAX_V5", 1.50)
                if v5 > max_v5:
                    bucket["v5_spike"] += 1
                    return False, f"v5_spike={v5:.3f}"
            # v29h — ACCELERATION filter: v5 / v15 must exceed min ratio
            if env_bool("PGG2_OUTSMART_ACCEL_FILTER_ENABLED", True):
                if first_5s > 0 and first_15s > 0 and current_price > 0:
                    v5 = current_price / first_5s
                    v15 = current_price / first_15s
                    accel_ratio = v5 / v15 if v15 > 0 else 1.0
                    min_accel = env_float("PGG2_OUTSMART_MIN_ACCEL_RATIO", 1.02)
                    if accel_ratio < min_accel:
                        bucket.setdefault("decel", 0)
                        bucket["decel"] += 1
                        return False, f"decel v5={v5:.3f} v15={v15:.3f} ratio={accel_ratio:.3f}"

        # Edge 2 (alt): Last-1s seller dominance filter
        if env_bool("PGG2_OUTSMART_S1_SELLER_BLOCK", True):
            s1 = tape.stats(1000, ts_ms)
            min_seller_share = env_float("PGG2_OUTSMART_MAX_S1_SELL_SHARE", 0.5)
            if s1.sells > 0 and s1.sell_sol > s1.buy_sol * min_seller_share:
                bucket["s1_seller"] += 1
                return False, f"s1_seller sells={int(s1.sells)} sol={s1.sell_sol:.2f}"

        # v29k (2026-05-10) — ZERO-SELLER orchestrated-launch trap.
        # FEATSNAP data: 4/4 mints with s5_sells==0 lost (Dnvz -22%, C41p -23%,
        # BhLk -13%, EPth -11%). Zero sellers = pumpers coordinating buys
        # but not yet dumped. Real organic pumps always have SOME sellers
        # (profit-takers, panic sells). Zero = trap waiting to spring.
        if env_bool("PGG2_OUTSMART_REQUIRE_SELLERS", True):
            s5_for_sells = tape.stats(5000, ts_ms)
            min_s5_sells = env_int("PGG2_OUTSMART_MIN_S5_SELLS", 1)
            if int(s5_for_sells.sells) < min_s5_sells:
                bucket.setdefault("zero_sellers", 0)
                bucket["zero_sellers"] += 1
                return False, f"zero_sellers s5_sells={int(s5_for_sells.sells)}"

        bucket["passed"] += 1
        # Diag log every N checks
        if bucket["checks"] % env_int("PGG2_OUTSMART_DIAG_EVERY", 100) == 0:
            log(
                f"PGG2-OUTSMART-DIAG lane={lane} checks={bucket['checks']} "
                f"no_cashback={bucket['no_cashback']} v5_spike={bucket['v5_spike']} "
                f"s1_seller={bucket['s1_seller']} "
                f"cashback_fail={bucket['cashback_check_failed']} passed={bucket['passed']}"
            )
        return True, "passed"

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
        # v29 — outsmart gate (cashback + v5 spike + s1 seller filters)
        passed, reason = self._outsmart_gate(event, features, "priced_snap")
        if not passed:
            return None
        # v29i (2026-05-10) — REJECT orchestrated-looking pumps. FEATSNAP
        # data showed all 3 catastrophic rugs (5eZz -24%, A9Aa -26%,
        # HffUignx -16%) had vertical=1 OR elite=1. These "textbook perfect"
        # setups (huge volume in <1.5s, distributed perfectly) are
        # bot-orchestrated honeypots. Real organic pumps are messier.
        # Block them by default; can be disabled via env.
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
        if buy1500 < env_float("PGG2_PRICED_SNAP_MIN_BUY1500", 6.0):
            return None
        if uniq1500 < env_int("PGG2_PRICED_SNAP_MIN_UNIQ1500", 4):
            return None
        if top1500 > env_float("PGG2_PRICED_SNAP_MAX_TOP1500", 0.55):
            return None
        if sell1500 > max(0.010, buy1500 * env_float("PGG2_PRICED_SNAP_MAX_SELL_RATIO1500", 0.08)):
            return None
        if event.sol < env_float("PGG2_PRICED_SNAP_MIN_CURRENT_BUY_SOL", 0.03):
            return None
        vsol = float(features.get("vsol_sol") or 0.0)
        min_vsol = env_float("PGG2_PRICED_SNAP_MIN_VSOL", 0.0)
        if min_vsol > 0 and vsol < min_vsol:
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
        # v29i — reject elite/vertical orchestrated setups (they all rugged)
        if env_bool("PGG2_OUTSMART_REJECT_ELITE", True) and elite_snap:
            return None
        if env_bool("PGG2_OUTSMART_REJECT_VERTICAL", True) and vertical_snap:
            return None
        if env_bool("PGG2_LAYERED_RISK_ENABLED", False):
            entry_fraction = env_float("PGG2_LAYERED_ENTRY_FRACTION", 0.80)
            if vertical_snap:
                entry_fraction = env_float("PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION", 0.30)
            if elite_snap:
                entry_fraction = env_float("PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION", entry_fraction)
            else:
                entry_fraction = env_float("PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION", entry_fraction)
            scout = max(0.0005, full_scout * entry_fraction)
        score = (
            150.0
            + min(55.0, buy1500 * 5.5)
            + min(45.0, uniq1500 * 5.0)
            + max(0.0, entry_move - 1.0) * 120.0
            - max(0.0, top1500 - 0.40) * 55.0
            - sell_ratio * 95.0
        )
        reason = (
            f"priced_snap move={entry_move:.2f}x age={age_sec:.1f}s "
            f"b1500={buy1500:.2f}/{uniq1500} top={top1500:.2f} "
            f"cur_buy={event.sol:.2f} sellr={sell_ratio:.2f} vsol={vsol:.2f}"
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
            }
        )
        # v29f (2026-05-10) — comprehensive feature snapshot for post-trade
        # analysis. Goal: identify the discriminating features between winners
        # and losers so we can build a precise filter.
        try:
            ts_ms = event.ts_ms
            s5 = tape.stats(5000, ts_ms)
            s15 = tape.stats(15000, ts_ms)
            s30 = tape.stats(30000, ts_ms)
            s60 = tape.stats(60000, ts_ms)
            cashback = self._outsmart_cashback_cache.get(event.mint, "?") if hasattr(self, "_outsmart_cashback_cache") else "?"
            fp5 = s5.first_price if s5.first_price > 0 else price
            fp15 = s15.first_price if s15.first_price > 0 else price
            fp30 = s30.first_price if s30.first_price > 0 else price
            v5 = price / fp5 if fp5 > 0 else 1.0
            v15 = price / fp15 if fp15 > 0 else 1.0
            v30 = price / fp30 if fp30 > 0 else 1.0
            log(
                f"PGG2-FEATSNAP-FIRE mint={event.mint[:8]} lane=priced_snap "
                f"score={score:.1f} entry_move={entry_move:.3f} age_sec={age_sec:.1f} "
                f"price={price:.6e} cashback={cashback} "
                f"buy1500={buy1500:.2f} uniq1500={uniq1500} top1500={top1500:.3f} "
                f"sell_ratio1500={sell_ratio:.3f} cur_buy_sol={event.sol:.3f} vsol={vsol:.3f} "
                f"slot_buy_sol={slot_buy_sol:.2f} slot_buyers={slot_buyers} slot_top={slot_top_share:.3f} "
                f"v5={v5:.3f} v15={v15:.3f} v30={v30:.3f} "
                f"s5_buys={int(s5.buys)} s5_sells={int(s5.sells)} s5_buy_sol={s5.buy_sol:.2f} s5_sell_sol={s5.sell_sol:.2f} "
                f"s15_buys={int(s15.buys)} s15_sells={int(s15.sells)} s15_buy_sol={s15.buy_sol:.2f} s15_sell_sol={s15.sell_sol:.2f} "
                f"s30_buys={int(s30.buys)} s30_sells={int(s30.sells)} s30_buy_sol={s30.buy_sol:.2f} s30_sell_sol={s30.sell_sol:.2f} "
                f"s60_buys={int(s60.buys)} s60_sells={int(s60.sells)} s60_buy_sol={s60.buy_sol:.2f} s60_sell_sol={s60.sell_sol:.2f} "
                f"s60_uniq_buyers={int(s60.unique_buyers)} s60_top_buy_share={s60.top_buy_share:.3f} "
                f"vertical={int(vertical_snap)} elite={int(elite_snap)}"
            )
        except Exception as exc:
            log(f"PGG2-FEATSNAP-ERR mint={event.mint[:8]} {type(exc).__name__}: {exc}")
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

    async def maybe_plan_strike(self, event: PumpEvent, curve: Optional[CurvePoint]) -> None:
        ts_ms = event.ts_ms
        features = self.feature_snapshot(event.mint, ts_ms)
        if not features:
            return
        self.maybe_arm_first_burst(event, features)
        features = self.feature_snapshot(event.mint, ts_ms) or features
        # v30 — permissive shadow observation tap. Runs BEFORE lane evaluation
        # so the lab always gets executable-quote samples, even when no lane
        # entry criterion fires. Pure observer, never opens a position.
        self._maybe_shadow_observe(event, features)
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
        # v29m — rug bounce hunter (profit FROM rugs)
        # v30 — FROZEN: diagnostics-only. Lane fires "strike_shadow" so the
        # executable shadow lab can rate quotes, but no actual entry until the
        # shadow data shows positive net executable PnL across >=30 samples.
        rug_bounce_plan = self.rug_bounce_ready(event, features)
        if rug_bounce_plan:
            actual_entry = env_bool("PGG2_RUG_BOUNCE_ACTUAL_ENTRY_ENABLED", False)
            if not actual_entry:
                self.logger.decision(
                    "strike_shadow",
                    event.mint,
                    {
                        "lane": rug_bounce_plan.lane,
                        "reason": rug_bounce_plan.reason,
                        "score": rug_bounce_plan.score,
                        "scout_sol": rug_bounce_plan.scout_sol,
                        "target_sol": rug_bounce_plan.target_sol,
                        "features": rug_bounce_plan.features,
                    },
                )
                self.record_shadow_candidate(event, features, rug_bounce_plan)
                return
            ok, reason = self.broker.can_strike(event.mint, ts_ms)
            if not ok:
                self.logger.decision(
                    "strike_skipped",
                    event.mint,
                    {"reason": reason, "lane": rug_bounce_plan.lane, "features": self.slim_features(features)},
                )
                return
            self.logger.decision(
                "strike_plan",
                event.mint,
                {
                    "lane": rug_bounce_plan.lane,
                    "reason": rug_bounce_plan.reason,
                    "score": rug_bounce_plan.score,
                    "scout_sol": rug_bounce_plan.scout_sol,
                    "target_sol": rug_bounce_plan.target_sol,
                    "needs_curve_fill": rug_bounce_plan.needs_curve_fill,
                    "features": rug_bounce_plan.features,
                },
            )
            pos = self.broker.queue_or_fill(rug_bounce_plan, float(features.get("price") or 0.0))
            if pos:
                self.init_position_follow(pos, trusted=True, entry_features=features)
                self.logger.decision("open", event.mint, {"lane": rug_bounce_plan.lane, "features": self.slim_features(features)})
            return
        snap_plan = self.priced_snap_ready(event, features)
        if snap_plan:
            # v30 — shadow-record EVERY priced_snap candidate alongside the
            # actual entry, so the lab can compare lane families on equal
            # footing. Sampling is governed by PGG2_SHADOW_LAB_COOLDOWN_MS.
            if env_bool("PGG2_SHADOW_LAB_TAP_PRICED_SNAP", True):
                self.record_shadow_candidate(event, features, snap_plan)
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
            # v30 — FROZEN: diagnostics-only (was net loser in prior runs).
            actual_entry = env_bool("PGG2_CURVE_LAG_REVEAL_ACTUAL_ENTRY_ENABLED", False)
            if not actual_entry:
                self.logger.decision(
                    "strike_shadow",
                    event.mint,
                    {
                        "lane": curve_lag_plan.lane,
                        "reason": curve_lag_plan.reason,
                        "score": curve_lag_plan.score,
                        "scout_sol": curve_lag_plan.scout_sol,
                        "target_sol": curve_lag_plan.target_sol,
                        "features": curve_lag_plan.features,
                    },
                )
                self.record_shadow_candidate(event, features, curve_lag_plan)
                self.curve_lag_reveal_seen.add(event.mint)
                return
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
        # capture lane + cost before close removes the position
        pre_pos = self.broker.positions.get(mint)
        pilot_lane = bool(pre_pos and getattr(pre_pos, "lane", "") == RULE_V33_QUOTE_EDGE_150_C)
        pre_cost = float(getattr(pre_pos, "cost_sol", 0.0)) if pre_pos else 0.0
        pre_opened_ts = int(getattr(pre_pos, "opened_ts_ms", ts_ms)) if pre_pos else ts_ms
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
        # v33 — clear active-position quote exclusivity on every successful
        # close path, not just the risk-worker one (manage_position's kill /
        # migration_complete / etc. all flow through here).
        try:
            if hasattr(self.broker, "clear_risk_owned"):
                self.broker.clear_risk_owned(mint)
        except Exception:
            pass
        # v30 — pilot SELL telemetry + session loss tracking
        if pilot_lane:
            hold_ms = ts_ms - pre_opened_ts
            net_pnl = float(pnl)
            # all-in pnl approximation = net_pnl (quote close already subtracts overhead)
            all_in = net_pnl
            self._pilot_session_loss_sol += min(0.0, net_pnl)
            trigger = "unknown"
            if reason in ("quote_loss_clamp",): trigger = "clamp"
            elif reason in ("quote_profit_bank",): trigger = "bank"
            elif "timebox" in reason: trigger = "timebox"
            elif "max_hold" in reason: trigger = "absolute_max_hold"
            elif "quote_gap" in reason: trigger = "quote_gap"
            elif "risk_worker_bank" in reason: trigger = "bank"
            elif "risk_worker_clamp" in reason: trigger = "clamp"
            elif "risk_worker_scratch" in reason: trigger = "scratch"
            elif "risk_worker_timebox" in reason: trigger = "timebox"
            elif "risk_worker_max_hold" in reason: trigger = "absolute_max_hold"
            log(
                f"PGG2-DRYLIVE-PILOT-SELL rule_id={RULE_V33_QUOTE_EDGE_150_C} "
                f"policy_id={POLICY_V33_C_MOONSHOT} "
                f"mint={short_addr(mint)} reason={reason} trigger={trigger} "
                f"legacy_pnl={net_pnl:+.6f} all_in_pnl={all_in:+.6f} hold_ms={hold_ms} "
                f"session_loss_legacy={self._pilot_session_loss_sol:+.6f} "
                f"pnl_model_version={PNL_MODEL_VERSION_V33} entries_used={self._pilot_entries}"
            )
        pnl = self.broker.stats.realized_pnl_sol - before_pnl
        if pnl > env_float("PIGGY_PROFIT_REENTRY_MIN_PNL_SOL", 0.0):
            self.profitable_closes[mint] = {
                "ts_ms": float(ts_ms),
                "pnl_sol": pnl,
                "peak_mult": float(self.broker.stats.best_mult),
            }
        self.position_follow.pop(mint, None)

    def _pilot_mark_and_timebox(
        self, pos: Any, ts_ms: int, price: float, features: dict[str, Any]
    ) -> bool:
        """v30 — emit PILOT-MARK and enforce timebox + absolute-max-hold for
        the dry-live pilot lane. Returns True if the position was closed by
        the timebox guard so manage_position should stop processing.
        """
        mint = pos.mint
        age_ms = ts_ms - getattr(pos, "opened_ts_ms", ts_ms)
        mark_interval = env_int("PGG2_DRYLIVE_PILOT_MARK_INTERVAL_MS", 250)
        last_mark = self._pilot_last_mark_ms.get(mint, 0)
        if ts_ms - last_mark >= mark_interval:
            self._pilot_last_mark_ms[mint] = ts_ms
            last_quoted = self._pilot_last_pnl.get(mint, 0.0)
            log(
                f"PGG2-DRYLIVE-PILOT-MARK rule_id={RULE_V33_QUOTE_EDGE_150_C} "
                f"policy_id={POLICY_V33_C_MOONSHOT} "
                f"mint={short_addr(mint)} age_ms={age_ms} mult={pos.last_mult:.3f} "
                f"peak_mult={pos.peak_mult:.3f} last_quoted_pnl={last_quoted:+.6f}"
            )
        # absolute max hold
        absolute_ms = env_int("PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS", 10000)
        if age_ms >= absolute_ms:
            self.close_position(
                mint, ts_ms, price, "drylive_pilot_absolute_max_hold", features, killed=True
            )
            return True
        # timebox 5000ms
        timebox_ms = env_int("PGG2_DRYLIVE_PILOT_TIMEBOX_MS", 5000)
        if age_ms >= timebox_ms:
            self.close_position(
                mint, ts_ms, price, "drylive_pilot_timebox", features, killed=False
            )
            return True
        return False

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

        # v30 — drylive pilot mark log + timebox guard. Emits live-equivalence
        # telemetry on every manage cycle when the position lane is the pilot.
        if pos.lane == "v33_quote_edge_150_C":
            if self._pilot_mark_and_timebox(pos, ts_ms, price, features):
                return  # closed by timebox/absolute-max

        # v32 — when risk worker owns quote exits, do NOT let the event-driven
        # manage loop independently quote/close. The risk worker is the single
        # source of truth for bank/clamp/timebox on these lanes.
        risk_owned_lanes = {"shadow_lab_canary", "v33_quote_edge_150_C"}
        if (
            env_bool("PGG2_RISK_WORKER_OWNS_QUOTE_EXIT", True)
            and pos.lane in risk_owned_lanes
        ):
            # safe to return; risk worker is polling. Only suppress quote-based
            # exits — non-quote emergency rules (e.g. piggy_kill_reason for
            # creator-rug) are out of scope of this gate and may still fire below
            # if added later. For now we fully cede control.
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
        ):
            if pos.state == "SCALE1":
                protect_peak = env_float("PGG2_PRICED_SNAP_SCALE_PROTECT_PEAK", 1.25)
                protect_min_mult = env_float("PGG2_PRICED_SNAP_SCALE_PROTECT_MIN_MULT", 1.10)
                protect_trail = env_float("PGG2_PRICED_SNAP_SCALE_PROTECT_TRAIL", 0.90)
                protect_reason = "priced_snap_scale_profit_protect"
            else:
                protect_peak = env_float("PGG2_PRICED_SNAP_SCOUT_PROTECT_PEAK", 1.18)
                protect_min_mult = env_float("PGG2_PRICED_SNAP_SCOUT_PROTECT_MIN_MULT", 1.08)
                protect_trail = env_float("PGG2_PRICED_SNAP_SCOUT_PROTECT_TRAIL", 0.92)
                protect_reason = "priced_snap_scout_profit_protect"
            if (
                pos.peak_mult >= protect_peak
                and mult >= protect_min_mult
                and mult <= pos.peak_mult * protect_trail
            ):
                self.close_position(mint, ts_ms, price, protect_reason, features, killed=False)
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
        # v33 — risk-worker close paths pass a minimal feature dict (no
        # bonding-curve snapshot, no s700/s1500). Use `.get` with safe
        # defaults so close decision logging never raises KeyError.
        s700 = features.get("s700") or {}
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
                "buyer_hhi700": s700.get("buyer_hhi", 0.0),
                "top_share700": s700.get("top_buy_share", 0.0),
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
        all_in_realized = float(getattr(st, "realized_all_in_pnl_sol", 0.0))
        legacy_realized = float(getattr(st, "legacy_realized_pnl_sol", st.realized_pnl_sol))
        log(
            f"PIGGY-STATUS creates={st.creates} trades={st.trades} buys/sells={st.buys}/{st.sells} "
            f"plans={st.strike_plans} pend={len(self.broker.pending)} scouts={st.scouts} "
            f"scale1={st.scale1} scale2={st.scale2} partials={st.partials} closes={st.closes} "
            f"W/L={st.wins}/{st.losses} kills={st.kills} best={st.best_mult:.2f}x "
            f"realized_all_in={all_in_realized:+.6f} legacy_realized={legacy_realized:+.6f} "
            f"pnl_model=v33_route_aware open_pnl={self.broker.open_pnl():+.6f} SOL "
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
            f"cluster_age={self.config.birth_max_age_ms}ms max_open={self.config.max_open_positions}"
        )
        # v31 — boot the dedicated risk worker thread now that we have a loop.
        if env_bool("PGG2_RISK_WORKER_ENABLED", True):
            self._risk_worker.start(asyncio.get_running_loop())
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
