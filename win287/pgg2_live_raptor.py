from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from birth_first_sniper import (
    SOL_MINT,
    BotConfig,
    PaperBroker,
    PendingStrike,
    Position,
    StrikePlan,
    env_bool,
    env_float,
    env_int,
    env_str,
    log,
    short_addr,
)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
LAMPORTS_PER_SOL = 1_000_000_000


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 58)
        chars.append(_B58_ALPHABET[rem])
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + ("".join(reversed(chars)) if chars else "")


class RaptorLiveBroker(PaperBroker):
    """Small, hard-gated live broker for PGG2.

    Production mode must be explicit. Quote mode builds swap transactions but
    never signs/sends them. Live mode signs/sends only after an exact confirm
    phrase is present. The strategy code still decides entries/exits; this
    class only replaces paper fills with Raptor swaps and actual SOL balance
    accounting.
    """

    def __init__(self, config: BotConfig):
        super().__init__(config)
        self.mode = env_str("PGG2_EXECUTION_MODE", "quote").lower()
        self.quote_only = self.mode != "live"
        self.api_key = env_str("SOLANATRACKER_API_KEY") or env_str("SOLANATRACKER_RPC_KEY")
        self.swap_url = env_str("PGG2_RAPTOR_SWAP_URL", "https://swap-v2.solanatracker.io/swap")
        self.rpc_url = env_str("PGG2_LIVE_RPC_URL") or env_str("SOLANATRACKER_RPC_HTTP")
        if not self.rpc_url:
            self.rpc_url = "https://rpc-mainnet.solanatracker.io/"
            if self.api_key:
                self.rpc_url += f"?api_key={self.api_key}"
        self.timeout_sec = env_float("PGG2_LIVE_HTTP_TIMEOUT_SEC", 4.0)
        self.wallet_path = Path(env_str("PGG2_WALLET_KEYPAIR", "/root/piggy/live_wallet.key"))
        self.public_key = env_str("PGG2_LIVE_PUBKEY")
        self.keypair: Optional[Keypair] = None
        if self.wallet_path.is_file():
            self.keypair = self.load_keypair(self.wallet_path)
            self.public_key = str(self.keypair.pubkey())
        if not self.public_key:
            raise RuntimeError("PGG2 live/quote mode needs PGG2_WALLET_KEYPAIR or PGG2_LIVE_PUBKEY")
        if self.mode == "live":
            confirm = env_str("PGG2_LIVE_CONFIRM")
            if confirm != "I_ACCEPT_REAL_SOL_RISK":
                raise RuntimeError("PGG2 live mode blocked: set PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK")
            if not self.keypair:
                raise RuntimeError("PGG2 live mode needs a keypair file; pubkey alone is quote-only")
        self.max_trade_sol = env_float("PGG2_LIVE_MAX_TRADE_SOL", 0.005)
        self.min_trade_sol = min(self.max_trade_sol, env_float("PGG2_LIVE_MIN_TRADE_SOL", self.max_trade_sol))
        self.min_wallet_reserve_sol = env_float("PGG2_LIVE_MIN_WALLET_RESERVE_SOL", 0.040)
        self.max_session_loss_sol = env_float("PGG2_LIVE_MAX_SESSION_LOSS_SOL", 0.020)
        self.max_consecutive_losses = env_int("PGG2_LIVE_MAX_CONSECUTIVE_LOSSES", 2)
        self.consecutive_losses = 0
        self.buy_slippage = env_float("PGG2_LIVE_BUY_SLIPPAGE_PCT", 18.0)
        self.sell_slippage = env_float("PGG2_LIVE_SELL_SLIPPAGE_PCT", 22.0)
        self.priority_fee = env_str("PGG2_LIVE_PRIORITY_FEE", "auto")
        self.priority_level = env_str("PGG2_LIVE_PRIORITY_LEVEL", "high")
        self.tx_version = env_str("PGG2_LIVE_TX_VERSION", "legacy")
        self.simulate_before_send = env_bool("PGG2_LIVE_SIMULATE_BEFORE_SEND", True)
        self.quote_simulate = env_bool("PGG2_QUOTE_SIMULATE", True)
        self.quote_shadow_positions = env_bool("PGG2_QUOTE_SHADOW_POSITIONS", False)
        self.quote_roundtrip_overhead_sol = env_float("PGG2_QUOTE_ROUNDTRIP_OVERHEAD_SOL", 0.00235)
        self.quote_shadow_tokens: dict[str, float] = {}
        self.last_profit_quote_ms: dict[str, int] = {}
        self.last_loss_quote_ms: dict[str, int] = {}
        self.fast_paper_accounting = env_bool("PGG2_LIVE_FAST_PAPER_ACCOUNTING", False)
        self.confirm_timeout_sec = env_float("PGG2_LIVE_CONFIRM_TIMEOUT_SEC", 8.0)
        log(
            f"PGG2-LIVE: mode={self.mode.upper()} wallet={short_addr(self.public_key)} "
            f"min_trade={self.min_trade_sol:.4f} max_trade={self.max_trade_sol:.4f} "
            f"reserve={self.min_wallet_reserve_sol:.4f} "
            f"session_loss_cap={self.max_session_loss_sol:.4f}"
        )

    @staticmethod
    def load_keypair(path: Path) -> Keypair:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise RuntimeError(f"empty keypair file: {path}")
        try:
            if raw.startswith("["):
                return Keypair.from_json(raw)
            if raw.startswith("{"):
                obj = json.loads(raw)
                val = obj.get("privateKey") or obj.get("secretKey") or obj.get("key")
                if not val:
                    raise ValueError("JSON keypair object must contain privateKey/secretKey/key")
                if isinstance(val, list):
                    return Keypair.from_bytes(bytes(int(x) for x in val))
                return Keypair.from_base58_string(str(val).strip())
            return Keypair.from_base58_string(raw)
        except Exception as exc:
            raise RuntimeError(f"failed to load keypair {path}: {type(exc).__name__}") from exc

    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "user-agent": env_str(
                "PGG2_HTTP_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            ),
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        data = None
        merged_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            merged_headers.setdefault("content-type", "application/json")
        retries = max(0, env_int("PGG2_LIVE_HTTP_RETRIES", 2))
        base_sleep = max(0.0, env_float("PGG2_LIVE_HTTP_RETRY_BASE_SEC", 0.25))
        last_exc: Optional[HTTPError] = None
        for attempt in range(retries + 1):
            req = Request(url, data=data, headers=merged_headers, method=method.upper())
            try:
                with urlopen(req, timeout=self.timeout_sec) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except HTTPError as exc:
                last_exc = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                    body_text = exc.read().decode("utf-8", "replace")[:500]
                    raise RuntimeError(f"http {exc.code} {exc.reason}: {body_text}") from exc
                time.sleep(base_sleep * (2 ** attempt))
        if last_exc:
            body_text = last_exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"http {last_exc.code} {last_exc.reason}: {body_text}") from last_exc
        return {}

    def rpc(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        headers = self.headers()
        headers["content-type"] = "application/json"
        out = self.request_json("POST", self.rpc_url, body=payload, headers=headers)
        if "error" in out:
            raise RuntimeError(f"rpc {method} error: {out['error']}")
        return out.get("result")

    def balance_sol(self) -> float:
        lamports = int(self.rpc("getBalance", [self.public_key]).get("value") or 0)
        return lamports / LAMPORTS_PER_SOL

    def transaction_wallet_delta_sol(self, sig: str) -> float:
        deadline = time.time() + env_float("PGG2_LIVE_TX_META_TIMEOUT_SEC", 15.0)
        while time.time() < deadline:
            result = self.rpc(
                "getTransaction",
                [
                    sig,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if result:
                meta = result.get("meta") or {}
                tx = result.get("transaction") or {}
                message = tx.get("message") or {}
                keys = message.get("accountKeys") or []
                wallet_idx = None
                for idx, key in enumerate(keys):
                    pubkey = key.get("pubkey") if isinstance(key, dict) else str(key)
                    if pubkey == self.public_key:
                        wallet_idx = idx
                        break
                if wallet_idx is None:
                    raise RuntimeError(f"tx {sig} missing wallet account {short_addr(self.public_key)}")
                pre = meta.get("preBalances") or []
                post = meta.get("postBalances") or []
                if wallet_idx >= len(pre) or wallet_idx >= len(post):
                    raise RuntimeError(f"tx {sig} missing wallet balance index {wallet_idx}")
                return (int(post[wallet_idx]) - int(pre[wallet_idx])) / LAMPORTS_PER_SOL
            time.sleep(0.35)
        raise RuntimeError(f"tx {sig} metadata unavailable after confirmation")

    def build_swap(self, from_mint: str, to_mint: str, amount: Any, slippage: float) -> dict[str, Any]:
        params: dict[str, Any] = {
            "from": from_mint,
            "to": to_mint,
            "fromAmount": amount,
            "slippage": slippage,
            "payer": self.public_key,
            "priorityFee": self.priority_fee,
            "priorityFeeLevel": self.priority_level,
            "txVersion": self.tx_version,
        }
        if env_bool("PGG2_LIVE_ONLY_DIRECT_ROUTES", False):
            params["onlyDirectRoutes"] = "true"
        t0 = time.perf_counter()
        out = self.request_json("GET", self.swap_url, params=params, headers=self.headers())
        elapsed = (time.perf_counter() - t0) * 1000
        if not out.get("txn"):
            raise RuntimeError(f"swap response missing txn keys={list(out.keys())}")
        rate = out.get("rate") or {}
        log(
            f"PGG2-LIVE-QUOTE {short_addr(from_mint)}->{short_addr(to_mint)} amount={amount} "
            f"out={rate.get('amountOut')} min={rate.get('minAmountOut')} "
            f"impact={rate.get('priceImpact')} fee={rate.get('fee')} tx={self.tx_version} ms={elapsed:.0f}"
        )
        return out

    @staticmethod
    def rate_amount_out(quote: dict[str, Any]) -> float:
        try:
            return float((quote.get("rate") or {}).get("amountOut") or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def rate_price_impact(quote: dict[str, Any]) -> float:
        try:
            return float((quote.get("rate") or {}).get("priceImpact") or 0.0)
        except Exception:
            return 0.0

    def max_buy_impact_for_lane(self, lane: str) -> float:
        if lane == "priced_breakout":
            return env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_BUY_IMPACT", 0.075)
        if lane == "late_swarm":
            return env_float("PGG2_LIVE_LATE_SWARM_MAX_BUY_IMPACT", 0.075)
        if lane == "raw_momentum":
            return env_float("PGG2_LIVE_RAW_MOMENTUM_MAX_BUY_IMPACT", 0.075)
        if lane == "preprice_reveal":
            return env_float("PGG2_LIVE_PREPRICE_REVEAL_MAX_BUY_IMPACT", 0.080)
        if lane == "birth_fanout":
            return env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_BUY_IMPACT", 0.075)
        if lane == "stealth_arm":
            return env_float("PGG2_LIVE_STEALTH_ARM_MAX_BUY_IMPACT", 0.085)
        if lane == "spark3_arm":
            return env_float("PGG2_LIVE_SPARK3_ARM_MAX_BUY_IMPACT", 0.090)
        if lane == "curve_arm_scout":
            return env_float("PGG2_LIVE_CURVE_ARM_MAX_BUY_IMPACT", 0.080)
        if lane == "whale_spark":
            return env_float("PGG2_LIVE_WHALE_SPARK_MAX_BUY_IMPACT", 0.085)
        if lane in {"early_ignition", "late_ignition", "breadth_ignition", "curve_lag_reveal"}:
            return env_float("PGG2_LIVE_EXPLORATORY_MAX_BUY_IMPACT", 0.085)
        return env_float("PGG2_LIVE_MAX_BUY_PRICE_IMPACT", 0.095)

    def max_roundtrip_loss_for_lane(
        self,
        lane: str,
        amount: float,
        features: Optional[dict[str, Any]] = None,
    ) -> float:
        ratio = env_float("PGG2_LIVE_PREFLIGHT_MAX_ROUNDTRIP_LOSS_RATIO", 0.16)
        floor = env_float(
            "PGG2_LIVE_PREFLIGHT_ROUNDTRIP_ABS_FLOOR_SOL",
            self.quote_roundtrip_overhead_sol * 1.80,
        )
        if lane == "preprice_reveal":
            ratio = env_float("PGG2_LIVE_PREPRICE_REVEAL_MAX_ROUNDTRIP_LOSS_RATIO", 0.065)
            floor = env_float("PGG2_LIVE_PREPRICE_REVEAL_ROUNDTRIP_ABS_FLOOR_SOL", 0.00150)
        elif lane == "priced_breakout":
            ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_ROUNDTRIP_LOSS_RATIO", 0.060)
            floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_ROUNDTRIP_ABS_FLOOR_SOL", 0.00175)
            feats = features or {}
            profile = str(feats.get("priced_breakout_profile") or "")
            buy1500 = float(feats.get("buy1500") or 0.0)
            uniq1500 = int(feats.get("uniq1500") or 0)
            top1500 = float(feats.get("top_share1500") or 1.0)
            sell1500 = float(feats.get("sell1500") or 0.0)
            broad_breakout = profile == "breadth"
            if broad_breakout:
                # The 2026-05-06 quote run exposed the live blind spot: broad
                # priced breakouts with 9-19 buyers and low concentration were
                # blocked at ~0.00260 SOL preflight drag, then ran to 1.4x-2.3x.
                # Allow that real breadth to pay normal moonshot friction, while
                # thin whale breakouts remain strict.
                ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_BREADTH_MAX_ROUNDTRIP_LOSS_RATIO", 0.115)
                floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_BREADTH_ROUNDTRIP_ABS_FLOOR_SOL", 0.00300)
            elif (
                profile == "whale"
                and uniq1500 >= env_int("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MIN_UNIQ1500", 3)
                and top1500 <= env_float("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MAX_TOP1500", 0.65)
                and sell1500 <= max(0.005, buy1500 * env_float("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MAX_SELL_RATIO1500", 0.03))
            ):
                # Micro-cluster breakout: not enough SOL volume to qualify as
                # the broad profile yet, but not a single-wallet candle either.
                # 4Gjp in the live quote run was exactly this profile
                # (3 buyers, top 0.43), blocked at 0.00260 drag, then ran ~2.08x.
                ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MAX_ROUNDTRIP_LOSS_RATIO", 0.110)
                floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_ROUNDTRIP_ABS_FLOOR_SOL", 0.00280)
            elif profile == "whale":
                ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_WHALE_MAX_ROUNDTRIP_LOSS_RATIO", 0.035)
                floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_WHALE_ROUNDTRIP_ABS_FLOOR_SOL", 0.00125)
        elif lane == "late_swarm":
            ratio = env_float("PGG2_LIVE_LATE_SWARM_MAX_ROUNDTRIP_LOSS_RATIO", 0.11)
            floor = env_float("PGG2_LIVE_LATE_SWARM_ROUNDTRIP_ABS_FLOOR_SOL", 0.00275)
        elif lane == "raw_momentum":
            # Raw momentum is an attack lane found from live quote tapes. It only
            # pays when execution is clean enough for a fast moonshot to overcome
            # fees/slippage. The 2026-05-06 quote run showed three raw opens with
            # 0.0018-0.0033 SOL preflight drag; all three became losses. Keep the
            # signal, but block quote-poor raw entries before they can spend.
            ratio = env_float("PGG2_LIVE_RAW_MOMENTUM_MAX_ROUNDTRIP_LOSS_RATIO", 0.045)
            floor = env_float("PGG2_LIVE_RAW_MOMENTUM_ROUNDTRIP_ABS_FLOOR_SOL", 0.00125)
            feats = features or {}
            raw_buy10 = float(feats.get("raw_buy_sol_10s") or 0.0)
            raw_uniq10 = int(feats.get("raw_unique_buyers_10s") or 0)
            raw_top10 = float(feats.get("raw_top_share_10s") or 1.0)
            elite_raw_breadth = (
                env_bool("PGG2_LIVE_RAW_MOMENTUM_ELITE_ROUNDTRIP_EXCEPTION_ENABLED", True)
                and raw_buy10 >= env_float("PGG2_LIVE_RAW_MOMENTUM_ELITE_MIN_BUY10", 25.0)
                and raw_uniq10 >= env_int("PGG2_LIVE_RAW_MOMENTUM_ELITE_MIN_UNIQ10", 24)
                and raw_top10 <= env_float("PGG2_LIVE_RAW_MOMENTUM_ELITE_MAX_TOP10", 0.25)
            )
            if elite_raw_breadth:
                # Fresh quote data: elite broad swarms were the blocked moonshots
                # (Ebcu 2.08x, 7FoR 2.7x). Let those pay normal moonshot friction,
                # while the non-elite raw losers remain blocked by the strict cap.
                ratio = env_float("PGG2_LIVE_RAW_MOMENTUM_ELITE_MAX_ROUNDTRIP_LOSS_RATIO", 0.085)
                floor = env_float("PGG2_LIVE_RAW_MOMENTUM_ELITE_ROUNDTRIP_ABS_FLOOR_SOL", 0.00235)
        elif lane == "curve_lag_reveal":
            # Curve-lag is a fast moonshot lane, but live quote-shadow showed its
            # loser already had poor immediate roundtrip economics at entry. Keep
            # the signal, block only quotes where execution quality is bad enough
            # that one failed trade can erase multiple small moonshot wins.
            ratio = env_float("PGG2_LIVE_CURVE_LAG_MAX_ROUNDTRIP_LOSS_RATIO", 0.06)
            floor = env_float("PGG2_LIVE_CURVE_LAG_ROUNDTRIP_ABS_FLOOR_SOL", 0.00125)
        elif lane == "birth_fanout":
            # Birth fanout can identify real pumps, but quote runs showed that
            # high-drag fanout entries turn tiny moves into immediate losses.
            # Keep the lane, but require execution clean enough to overcome fees.
            ratio = env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_ROUNDTRIP_LOSS_RATIO", 0.075)
            floor = env_float("PGG2_LIVE_BIRTH_FANOUT_ROUNDTRIP_ABS_FLOOR_SOL", 0.00175)
            feats = features or {}
            birth_buy1500 = float(feats.get("buy1500") or 0.0)
            birth_uniq1500 = int(feats.get("uniq1500") or 0)
            birth_top1500 = float(feats.get("top_share1500") or 1.0)
            birth_sell1500 = float(feats.get("sell1500") or 0.0)
            birth_move1500 = float(feats.get("move1500") or 1.0)
            elite_birth_fanout = (
                env_bool("PGG2_LIVE_BIRTH_FANOUT_ELITE_ROUNDTRIP_EXCEPTION_ENABLED", True)
                and birth_buy1500 >= env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_MIN_BUY1500", 20.0)
                and birth_uniq1500 >= env_int("PGG2_LIVE_BIRTH_FANOUT_ELITE_MIN_UNIQ1500", 10)
                and birth_top1500 <= env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_MAX_TOP1500", 0.35)
                and birth_sell1500 <= env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_MAX_SELL1500", 0.001)
                and birth_move1500 <= env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_MAX_MOVE1500", 1.20)
            )
            if elite_birth_fanout:
                # E6uP quote tape: 60 SOL / 14 buyers / 0 sells was blocked at
                # 0.00259 SOL drag, then ran ~2x. Accept normal moonshot friction
                # only for this broad no-sell birth profile.
                ratio = env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_MAX_ROUNDTRIP_LOSS_RATIO", 0.11)
                floor = env_float("PGG2_LIVE_BIRTH_FANOUT_ELITE_ROUNDTRIP_ABS_FLOOR_SOL", 0.00275)
        elif lane == "stealth_arm":
            # Stealth-arm is intentionally aggressive and tiny. The edge comes
            # from asymmetric 2x-8x winners, so allow the fixed quote overhead on
            # 0.01 SOL probes while keeping absolute loss capped.
            ratio = env_float("PGG2_LIVE_STEALTH_ARM_MAX_ROUNDTRIP_LOSS_RATIO", 0.30)
            floor = env_float("PGG2_LIVE_STEALTH_ARM_ROUNDTRIP_ABS_FLOOR_SOL", 0.00300)
        elif lane == "spark3_arm":
            # Spark3 is the aggressive live moonshot arm lane. Fast validation
            # showed the signal is positive only if the probe stays tiny, so
            # allow normal quote overhead but keep the executable loss absolute.
            ratio = env_float("PGG2_LIVE_SPARK3_ARM_MAX_ROUNDTRIP_LOSS_RATIO", 0.30)
            floor = env_float("PGG2_LIVE_SPARK3_ARM_ROUNDTRIP_ABS_FLOOR_SOL", 0.00300)
        elif lane in {"curve_arm_scout", "whale_spark", "early_ignition", "late_ignition", "breadth_ignition"}:
            ratio = env_float("PGG2_LIVE_EXPLORATORY_MAX_ROUNDTRIP_LOSS_RATIO", ratio)
            floor = env_float("PGG2_LIVE_EXPLORATORY_ROUNDTRIP_ABS_FLOOR_SOL", floor)
        return max(floor, amount * ratio)

    def priced_breakout_runner_profile(self, features: Optional[dict[str, Any]], reason: str = "") -> bool:
        feats = features or {}
        profile = str(feats.get("priced_breakout_profile") or "")
        if not profile and "priced_breakout breadth" in reason:
            profile = "breadth"
        if not profile and "priced_breakout whale" in reason:
            profile = "whale"
        buy1500 = float(feats.get("buy1500") or 0.0)
        uniq1500 = int(feats.get("uniq1500") or 0)
        top1500 = float(feats.get("top_share1500") or 1.0)
        sell1500 = float(feats.get("sell1500") or 0.0)
        vsol = float(feats.get("vsol_sol") or 0.0)
        clean_sells = sell1500 <= max(
            0.005,
            buy1500 * env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_MAX_SELL_RATIO1500", 0.06),
        )
        strong_breadth = (
            profile == "breadth"
            and uniq1500 >= env_int("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_BREADTH_MIN_UNIQ1500", 4)
            and top1500 <= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_BREADTH_MAX_TOP1500", 0.50)
            and (
                buy1500 >= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_BREADTH_MIN_BUY1500", 6.0)
                or vsol >= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_BREADTH_MIN_VSOL", 35.0)
            )
        )
        strong_cluster = (
            profile == "whale"
            and uniq1500 >= env_int("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MIN_UNIQ1500", 3)
            and top1500 <= env_float("PGG2_LIVE_PRICED_BREAKOUT_CLUSTER_MAX_TOP1500", 0.65)
            and (
                buy1500 >= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_CLUSTER_MIN_BUY1500", 1.0)
                or vsol >= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_CLUSTER_MIN_VSOL", 35.0)
            )
        )
        return clean_sells and (strong_breadth or strong_cluster)

    def max_executable_loss_for_lane(
        self,
        lane: str,
        cost_sol: float,
        features: Optional[dict[str, Any]] = None,
        reason: str = "",
    ) -> float:
        """Maximum acceptable executable loss for fast live/quote lanes.

        This is deliberately stricter than the paper hard-break. Current quote
        tape showed the curve multiplier can print a moonshot while the actual
        Raptor sell quote is already deeply red. In live mode, executable PnL is
        the only truth.
        """
        ratio = env_float("PGG2_LIVE_MAX_EXECUTABLE_LOSS_RATIO", 0.10)
        floor = env_float("PGG2_LIVE_MAX_EXECUTABLE_LOSS_FLOOR_SOL", 0.00100)
        cap = env_float("PGG2_LIVE_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00225)
        if lane == "priced_breakout":
            ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_EXECUTABLE_LOSS_RATIO", ratio)
            floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
            cap = env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_EXECUTABLE_LOSS_CAP_SOL", cap)
            if self.priced_breakout_runner_profile(features, reason):
                # Runner-shaped priced breakouts need slightly more room than
                # ordinary quote losses. The missed live quote runners (9m9W,
                # 85Gr, 2CQT) were blocked/closed around -0.0026 to -0.00285
                # executable PnL, then ran 2x-4x. This cap keeps the loss tiny
                # while preventing the guard from choking the actual signal.
                ratio = env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_MAX_EXECUTABLE_LOSS_RATIO", 0.13)
                floor = env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
                cap = env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00325)
        elif lane == "late_swarm":
            ratio = env_float("PGG2_LIVE_LATE_SWARM_MAX_EXECUTABLE_LOSS_RATIO", ratio)
            floor = env_float("PGG2_LIVE_LATE_SWARM_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
            cap = env_float("PGG2_LIVE_LATE_SWARM_MAX_EXECUTABLE_LOSS_CAP_SOL", cap)
        elif lane == "birth_fanout":
            ratio = env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_EXECUTABLE_LOSS_RATIO", ratio)
            floor = env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
            cap = env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_EXECUTABLE_LOSS_CAP_SOL", cap)
        elif lane == "stealth_arm":
            ratio = env_float("PGG2_LIVE_STEALTH_ARM_MAX_EXECUTABLE_LOSS_RATIO", 0.30)
            floor = env_float("PGG2_LIVE_STEALTH_ARM_MAX_EXECUTABLE_LOSS_FLOOR_SOL", 0.00150)
            cap = env_float("PGG2_LIVE_STEALTH_ARM_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00300)
        elif lane == "spark3_arm":
            ratio = env_float("PGG2_LIVE_SPARK3_ARM_MAX_EXECUTABLE_LOSS_RATIO", 0.30)
            floor = env_float("PGG2_LIVE_SPARK3_ARM_MAX_EXECUTABLE_LOSS_FLOOR_SOL", 0.00150)
            cap = env_float("PGG2_LIVE_SPARK3_ARM_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00300)
        elif lane in {"reclaim_wave", "second_wave_after_cluster"}:
            feats = features or {}
            buy700 = float(feats.get("buy700") or 0.0)
            buy1500 = float(feats.get("buy1500") or 0.0)
            uniq700 = int(feats.get("uniq700") or 0)
            top700 = float(feats.get("top_share700") or 1.0)
            sell1500 = float(feats.get("sell1500") or 0.0)
            move700 = float(feats.get("move700") or 1.0)
            score = float(feats.get("score") or 0.0)
            vsol = float(feats.get("vsol_sol") or 0.0)
            wave_base = float(feats.get("wave_base_move") or 1.0)
            sell_ratio = sell1500 / max(buy1500, 0.001)
            explosive_no_sell = (
                env_bool("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_EXCEPTION_ENABLED", True)
                and buy700 >= env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MIN_BUY700", 8.0)
                and uniq700 >= env_int("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MIN_UNIQ700", 5)
                and top700 <= env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MAX_TOP700", 0.36)
                and sell1500 <= env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MAX_SELL1500", 0.010)
                and vsol >= env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MIN_VSOL", 45.0)
                and wave_base >= env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MIN_BASE_MOVE", 1.65)
            )
            if explosive_no_sell:
                # Quote-shadow evidence: B6WN had this profile, was blocked at
                # -0.00595 SOL immediate drag, then ran ~1.54x. The blocked loser
                # with similar breadth had 0.459 SOL sell pressure, so the exception
                # is deliberately no-sell only.
                ratio = env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MAX_EXECUTABLE_LOSS_RATIO", 0.13)
                floor = env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
                cap = env_float("PGG2_LIVE_RECLAIM_SECOND_HIGH_DRAG_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00650)
            clean_momentum_reclaim = (
                env_bool("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_EXCEPTION_ENABLED", True)
                and lane == "reclaim_wave"
                and score >= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_SCORE", 300.0)
                and buy700 >= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_BUY700", 8.0)
                and uniq700 >= env_int("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_UNIQ700", 6)
                and top700 <= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MAX_TOP700", 0.30)
                and sell_ratio <= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MAX_SELL_RATIO", 0.01)
                and move700 >= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_MOVE700", 1.30)
                and wave_base >= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_BASE", 1.15)
                and vsol >= env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MIN_VSOL", 38.0)
            )
            if clean_momentum_reclaim:
                # Fast quote-tape validation: 17 skipped candidates matched this
                # profile; 16 reached >=1.32x, 13 reached >=1.5x, none stayed
                # below 1.10x. This is the missing live moonshot lane.
                ratio = env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MAX_EXECUTABLE_LOSS_RATIO", 0.13)
                floor = env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
                cap = env_float("PGG2_LIVE_RECLAIM_CLEAN_MOMENTUM_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00650)
            late_base_reclaim = (
                env_bool("PGG2_LIVE_RECLAIM_LATE_BASE_EXCEPTION_ENABLED", True)
                and lane == "reclaim_wave"
                and wave_base >= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MIN_BASE", 2.30)
                and vsol >= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MIN_VSOL", 55.0)
                and buy700 >= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MIN_BUY700", 15.0)
                and uniq700 >= env_int("PGG2_LIVE_RECLAIM_LATE_BASE_MIN_UNIQ700", 5)
                and top700 <= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MAX_TOP700", 0.45)
                and move700 >= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MIN_MOVE700", 1.12)
                and sell_ratio <= env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MAX_SELL_RATIO", 0.08)
            )
            if late_base_reclaim:
                # Quote-tape fast validation: 5 skipped late-base reclaim
                # candidates matched this profile; all 5 reached >=1.8x and none
                # were opened. This catches the DdmY-style moonshot without opening
                # ordinary weak reclaim noise.
                ratio = env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MAX_EXECUTABLE_LOSS_RATIO", 0.13)
                floor = env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MAX_EXECUTABLE_LOSS_FLOOR_SOL", floor)
                cap = env_float("PGG2_LIVE_RECLAIM_LATE_BASE_MAX_EXECUTABLE_LOSS_CAP_SOL", 0.00650)
        return min(cap, max(floor, cost_sol * ratio))

    def quote_loss_clamp_reason(self, pos: Position, ts_ms: int) -> str:
        if not env_bool("PGG2_LIVE_QUOTE_LOSS_CLAMP_ENABLED", True):
            return ""
        if not (self.mode == "live" or self.quote_shadow_positions):
            return ""
        if pos.mint not in self.positions:
            return ""
        if pos.lane not in {"priced_breakout", "late_swarm", "birth_fanout", "stealth_arm", "spark3_arm", "raw_momentum", "curve_lag_reveal"}:
            return ""
        if pos.age_sec(ts_ms) < env_float("PGG2_LIVE_QUOTE_LOSS_CLAMP_MIN_AGE_SEC", 0.20):
            return ""
        last_ms = self.last_loss_quote_ms.get(pos.mint, 0)
        interval_ms = env_int("PGG2_LIVE_QUOTE_LOSS_CLAMP_INTERVAL_MS", 300)
        if ts_ms - last_ms < interval_ms:
            return ""
        self.last_loss_quote_ms[pos.mint] = ts_ms
        try:
            sell_amount: Any = "auto"
            if self.quote_only and self.quote_shadow_positions:
                quote_tokens = self.quote_shadow_tokens.get(pos.mint, pos.remaining_tokens)
                remaining_fraction = pos.remaining_tokens / max(pos.tokens_bought, 1e-18)
                sell_amount = round(quote_tokens * remaining_fraction, 9)
            quote = self.build_swap(pos.mint, SOL_MINT, sell_amount, self.sell_slippage)
            expected_out = self.rate_amount_out(quote)
            overhead = self.quote_roundtrip_overhead_sol if self.quote_shadow_positions else 0.0
            pnl = pos.realized_sol + expected_out - overhead - pos.cost_sol
            bank_need = env_float(
                "PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_MIN_PNL_SOL",
                env_float("PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_PNL_SOL", 0.00125),
            )
            if pos.lane == "birth_fanout":
                # Birth fanout is a live-execution lane, not a paper-mult lane.
                # Quote tape showed EMjP reach executable green (+0.000401 SOL)
                # and then reverse into -0.003731 SOL because the global bank
                # floor was too large for a 0.015 SOL adaptive entry.
                bank_need = env_float(
                    "PGG2_LIVE_BIRTH_FANOUT_ANY_PROFIT_BANK_MIN_PNL_SOL",
                    bank_need,
                )
            elif pos.lane == "stealth_arm":
                # Do not scalp this lane at the first tiny green quote. Its edge
                # comes from asymmetric 2x-8x follow-through; bank only when the
                # executable quote has paid enough to matter at 0.01 SOL size.
                bank_need = env_float(
                    "PGG2_LIVE_STEALTH_ARM_ANY_PROFIT_BANK_MIN_PNL_SOL",
                    max(bank_need, 0.00350),
                )
            elif pos.lane == "spark3_arm":
                # Spark3 winners need to pay for several tiny failed probes.
                # Bank only when executable PnL is meaningfully above overhead.
                bank_need = env_float(
                    "PGG2_LIVE_SPARK3_ARM_ANY_PROFIT_BANK_MIN_PNL_SOL",
                    max(bank_need, 0.00250),
                )
            if env_bool("PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_ENABLED", True) and pnl >= bank_need:
                log(
                    f"PGG2-LIVE-QUOTE-ANY-PROFIT-BANK {short_addr(pos.mint)} lane={pos.lane} "
                    f"quote_out={expected_out:.6f} pnl={pnl:+.6f} need={bank_need:.6f}"
                )
                return "quote_profit_bank"
            entry_features = getattr(pos, "entry_features", {}) or {}
            max_loss = self.max_executable_loss_for_lane(
                pos.lane,
                pos.cost_sol,
                entry_features,
                pos.reason,
            )
            if (
                pos.lane == "priced_breakout"
                and self.priced_breakout_runner_profile(entry_features, pos.reason)
                and pos.age_sec(ts_ms) < env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_LOSS_GRACE_SEC", 8.0)
                and pnl > -env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_EMERGENCY_MAX_LOSS_SOL", 0.010)
            ):
                log(
                    f"PGG2-LIVE-QUOTE-LOSS-GRACE {short_addr(pos.mint)} lane={pos.lane} "
                    f"quote_out={expected_out:.6f} pnl={pnl:+.6f} max_loss={max_loss:.6f} "
                    f"age={pos.age_sec(ts_ms):.2f}s"
                )
                return ""
            log(
                f"PGG2-LIVE-QUOTE-LOSS-CHECK {short_addr(pos.mint)} lane={pos.lane} "
                f"quote_out={expected_out:.6f} pnl={pnl:+.6f} max_loss={max_loss:.6f}"
            )
            if pnl <= -max_loss:
                return "quote_loss_clamp"
        except Exception as exc:
            log(f"PGG2-LIVE-QUOTE-LOSS-WARN {short_addr(pos.mint)} {type(exc).__name__}: {exc}")
        return ""

    def allow_fast_reentry_after_close(self, pos: Position, reason: str, pnl: float) -> bool:
        """Avoid letting an early birth close block the better same-mint signal.

        Birth fanout fires before the stronger second-wave/reclaim confirmation.
        In the quote tape, a birth close on EMjP was followed by improving reclaim
        features, but same-mint cooldown blocked the later setup. PGG2 already
        dedups birth_fanout via birth_fanout_seen, so skipping closed_recent here
        does not reopen the same birth entry; it only lets a later confirmed lane
        take over if the mint proves itself.
        """
        if not env_bool("PGG2_LIVE_ALLOW_FAST_REENTRY_AFTER_BIRTH_CLOSE", True):
            return False
        if pos.lane != "birth_fanout":
            return False
        return reason in {
            "quote_profit_bank",
            "quote_loss_clamp",
            "birth_fanout_pop_bank",
            "birth_fanout_no_follow",
        }

    def defer_paper_kill_reason(self, pos: Position, reason: str, ts_ms: int) -> bool:
        """Let strict priced-breakout runners breathe through early curve noise.

        Paper-mode hard breaks can fire while the executable quote is briefly
        underwater. The live quote tape showed this closed real runners before
        their quote recovered. Only defer for the runner profile and only inside
        the short quote-loss grace window; outside that, normal kills apply.
        """
        if not (self.mode == "live" or self.quote_shadow_positions):
            return False
        if pos.lane != "priced_breakout":
            return False
        if reason not in {"kill_priced_breakout_hard_break", "kill_full_no_pop", "first_pop_sell_exit"}:
            return False
        entry_features = getattr(pos, "entry_features", {}) or {}
        if not self.priced_breakout_runner_profile(entry_features, pos.reason):
            return False
        age_sec = pos.age_sec(ts_ms)
        if age_sec >= env_float("PGG2_LIVE_PRICED_BREAKOUT_RUNNER_LOSS_GRACE_SEC", 8.0):
            return False
        log(
            f"PGG2-LIVE-PAPER-KILL-DEFER {short_addr(pos.mint)} reason={reason} "
            f"age={age_sec:.2f}s"
        )
        return True

    def impact_size_steps(self, requested: float, min_trade: float) -> list[float]:
        configured = env_str("PGG2_LIVE_IMPACT_SIZE_STEPS_SOL", "")
        steps: list[float] = []
        if configured:
            for raw in configured.split(","):
                try:
                    step = float(raw.strip())
                except ValueError:
                    continue
                if min_trade <= step <= requested:
                    steps.append(step)
        if not steps:
            steps = [
                requested,
                requested * 0.80,
                requested * 0.70,
                requested * 0.60,
                requested * 0.50,
                min_trade,
            ]
        steps.append(requested)
        steps.append(min_trade)
        clean: list[float] = []
        seen: set[float] = set()
        for step in sorted((max(min_trade, min(requested, x)) for x in steps), reverse=True):
            rounded = round(step, 9)
            if rounded in seen:
                continue
            seen.add(rounded)
            clean.append(rounded)
        return clean

    def quote_entry_candidate(
        self,
        plan: StrikePlan,
        requested: float,
        min_trade: float,
    ) -> Optional[tuple[float, float, dict[str, Any], float]]:
        max_impact = self.max_buy_impact_for_lane(plan.lane)
        preflight_roundtrip = env_bool("PGG2_LIVE_PREFLIGHT_ROUNDTRIP_ENABLED", self.quote_shadow_positions)
        best_amount = 0.0
        best_impact = 999.0
        best_roundtrip_loss = 999.0
        for amount in self.impact_size_steps(requested, min_trade):
            quote = self.build_swap(SOL_MINT, plan.mint, amount, self.buy_slippage)
            impact = self.rate_price_impact(quote)
            best_amount = amount
            best_impact = min(best_impact, impact)
            if impact > max_impact:
                log(
                    f"PGG2-LIVE-BUY-IMPACT-TRY {short_addr(plan.mint)} lane={plan.lane} "
                    f"impact={impact:.4f} max={max_impact:.4f} amount={amount:.6f}"
                )
                continue
            quote_tokens = self.rate_amount_out(quote)
            roundtrip_loss = 0.0
            if preflight_roundtrip and quote_tokens > 0:
                reverse = self.build_swap(plan.mint, SOL_MINT, round(quote_tokens, 9), self.sell_slippage)
                immediate_out = self.rate_amount_out(reverse)
                roundtrip_loss = max(0.0, amount - immediate_out + self.quote_roundtrip_overhead_sol)
                best_roundtrip_loss = min(best_roundtrip_loss, roundtrip_loss)
                allowed_loss = self.max_roundtrip_loss_for_lane(plan.lane, amount, plan.features)
                if roundtrip_loss > allowed_loss:
                    log(
                        f"PGG2-LIVE-ROUNDTRIP-TRY {short_addr(plan.mint)} lane={plan.lane} "
                        f"loss={roundtrip_loss:.6f} max={allowed_loss:.6f} "
                        f"amount={amount:.6f} buy_impact={impact:.4f}"
                    )
                    continue
            if amount < requested:
                log(
                    f"PGG2-LIVE-BUY-ADAPTIVE-SIZE {short_addr(plan.mint)} lane={plan.lane} "
                    f"requested={requested:.6f} chosen={amount:.6f} impact={impact:.4f} "
                    f"roundtrip_loss={roundtrip_loss:.6f}"
                )
            return amount, impact, quote, roundtrip_loss
        rt = "" if best_roundtrip_loss == 999.0 else f" best_roundtrip_loss={best_roundtrip_loss:.6f}"
        log(
            f"PGG2-LIVE-BUY-EXECUTION-BLOCK {short_addr(plan.mint)} lane={plan.lane} "
            f"requested={requested:.6f} min_trade={min_trade:.6f} "
            f"best_amount={best_amount:.6f} best_impact={best_impact:.4f} "
            f"max_impact={max_impact:.4f}{rt}"
        )
        return None

    @staticmethod
    def profit_exit_reason(reason: str) -> bool:
        return reason in {
            "first_pop_sell_exit",
            "moonshot_pop_after_sell",
            "moonshot_decay_55",
            "quote_profit_bank",
            "late_reclaim_probe_pop_bank",
            "runner_3x_decay",
            "runner_flow_decay",
            "runner_stalled_after_sell",
        }

    def quote_profit_bank_reason(self, pos: Position, ts_ms: int) -> str:
        """Return an executable-profit exit reason when the live quote is green.

        Paper/curve multipliers are not enough in live mode. Current quote tape
        showed a preprice position reach a 1.54x curve peak, but no paper sell
        reason fired at the executable peak; by the time a normal exit reason
        fired, the Raptor quote was below break-even and the position later
        hard-broke. This method proactively checks the actual sell quote while
        curve price is strong and banks only if the executable quote clears the
        live profit floor.
        """
        if not env_bool("PGG2_LIVE_QUOTE_PROFIT_BANK_ENABLED", True):
            return ""
        if not (self.mode == "live" or self.quote_shadow_positions):
            return ""
        if pos.mint not in self.positions:
            return ""
        age_sec = pos.age_sec(ts_ms)
        if age_sec < env_float("PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_AGE_SEC", 0.35):
            return ""
        if pos.last_mult < env_float("PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_MULT", 1.18):
            return ""
        if pos.peak_mult < env_float("PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_PEAK", 1.25):
            return ""
        last_ms = self.last_profit_quote_ms.get(pos.mint, 0)
        interval_ms = env_int("PGG2_LIVE_QUOTE_PROFIT_BANK_INTERVAL_MS", 650)
        if ts_ms - last_ms < interval_ms:
            return ""
        self.last_profit_quote_ms[pos.mint] = ts_ms
        try:
            sell_amount: Any = "auto"
            if self.quote_only and self.quote_shadow_positions:
                quote_tokens = self.quote_shadow_tokens.get(pos.mint, pos.remaining_tokens)
                remaining_fraction = pos.remaining_tokens / max(pos.tokens_bought, 1e-18)
                sell_amount = round(quote_tokens * remaining_fraction, 9)
            quote = self.build_swap(pos.mint, SOL_MINT, sell_amount, self.sell_slippage)
            expected_out = self.rate_amount_out(quote)
            overhead = self.quote_roundtrip_overhead_sol if self.quote_shadow_positions else 0.0
            pnl = pos.realized_sol + expected_out - overhead - pos.cost_sol
            need = env_float(
                "PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_PNL_SOL",
                env_float("PGG2_LIVE_MIN_PROFIT_EXIT_SOL", overhead),
            )
            if pos.lane == "stealth_arm":
                need = env_float("PGG2_LIVE_STEALTH_ARM_PROFIT_BANK_MIN_PNL_SOL", max(need, 0.00350))
            elif pos.lane == "spark3_arm":
                need = env_float("PGG2_LIVE_SPARK3_ARM_PROFIT_BANK_MIN_PNL_SOL", max(need, 0.00250))
            log(
                f"PGG2-LIVE-QUOTE-PROFIT-CHECK {short_addr(pos.mint)} lane={pos.lane} "
                f"mult={pos.last_mult:.3f} peak={pos.peak_mult:.3f} "
                f"quote_out={expected_out:.6f} pnl={pnl:+.6f} need={need:.6f}"
            )
            if pnl >= need:
                return "quote_profit_bank"
        except Exception as exc:
            log(f"PGG2-LIVE-QUOTE-PROFIT-WARN {short_addr(pos.mint)} {type(exc).__name__}: {exc}")
        return ""

    def sign_transaction(self, txn_b64: str) -> tuple[str, str]:
        if not self.keypair:
            raise RuntimeError("cannot sign without keypair")
        tx = VersionedTransaction.from_bytes(base64.b64decode(txn_b64))
        signed = VersionedTransaction(tx.message, [self.keypair])
        raw = bytes(signed)
        return base64.b64encode(raw).decode("ascii"), b58encode(raw)

    def simulate_signed(self, signed_b64: str) -> bool:
        if not self.simulate_before_send:
            return True
        result = self.rpc(
            "simulateTransaction",
            [
                signed_b64,
                {
                    "commitment": "confirmed",
                    "encoding": "base64",
                    "replaceRecentBlockhash": True,
                    "sigVerify": False,
                },
            ],
        )
        value = (result or {}).get("value") or {}
        if value.get("err"):
            logs = value.get("logs") or []
            tail = " | ".join(str(x) for x in logs[-6:])
            log(f"PGG2-LIVE-SIM-FAIL err={value.get('err')} logs={tail[:500]}")
            return False
        log(f"PGG2-LIVE-SIM-OK units={value.get('unitsConsumed')} fee={value.get('fee')}")
        return True

    def send_signed(self, signed_b64: str) -> str:
        params = [
            signed_b64,
            {
                "encoding": "base64",
                "skipPreflight": env_bool("PGG2_LIVE_SKIP_PREFLIGHT", False),
                "preflightCommitment": "confirmed",
                "maxRetries": env_int("PGG2_LIVE_MAX_RETRIES", 3),
            },
        ]
        return str(self.rpc("sendTransaction", params))

    def wait_confirmed(self, sig: str) -> bool:
        deadline = time.time() + self.confirm_timeout_sec
        while time.time() < deadline:
            try:
                res = self.rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
                status = ((res or {}).get("value") or [None])[0]
                if status:
                    if status.get("err"):
                        log(f"PGG2-LIVE-TX-ERR sig={sig} err={status.get('err')}")
                        return False
                    if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                        return True
            except Exception as exc:
                log(f"PGG2-LIVE-CONFIRM-WARN sig={sig} {type(exc).__name__}: {exc}")
            time.sleep(0.35)
        log(f"PGG2-LIVE-CONFIRM-TIMEOUT sig={sig}")
        return False

    def lane_trade_bounds(self, lane: str, features: Optional[dict[str, Any]] = None) -> tuple[float, float]:
        max_trade = self.max_trade_sol
        min_trade = self.min_trade_sol
        if lane == "birth_fanout":
            # Birth fanout remains a useful early signal, but live/quote data shows
            # paper curve multipliers can overstate the executable Raptor exit.
            # Cap only this lane so entries still fire while its losses stay small.
            max_trade = min(max_trade, env_float("PGG2_LIVE_BIRTH_FANOUT_MAX_TRADE_SOL", 0.010))
            min_trade = min(max_trade, env_float("PGG2_LIVE_BIRTH_FANOUT_MIN_TRADE_SOL", 0.005))
        elif lane == "stealth_arm":
            max_trade = min(max_trade, env_float("PGG2_LIVE_STEALTH_ARM_MAX_TRADE_SOL", 0.010))
            min_trade = min(max_trade, env_float("PGG2_LIVE_STEALTH_ARM_MIN_TRADE_SOL", 0.010))
        elif lane == "spark3_arm":
            max_trade = min(max_trade, env_float("PGG2_LIVE_SPARK3_ARM_MAX_TRADE_SOL", 0.010))
            min_trade = min(max_trade, env_float("PGG2_LIVE_SPARK3_ARM_MIN_TRADE_SOL", 0.010))
        elif lane in {"early_ignition", "late_ignition"}:
            # Ignition lanes are useful for frequency, but they are exploratory
            # until follow-through arrives. Keep them live, just do not let one
            # failed ignition spend the same size as proven curve/second-wave flow.
            max_trade = min(max_trade, env_float("PGG2_LIVE_IGNITION_MAX_TRADE_SOL", 0.025))
            min_trade = min(max_trade, env_float("PGG2_LIVE_IGNITION_MIN_TRADE_SOL", 0.010))
        elif lane == "priced_breakout":
            # Priced breakout is the anti-blindness lane: it catches tokens that
            # have already repriced before the normal curve cache confirms them.
            # Live quote data shows 0.025 SOL can be too much size for this early
            # liquidity. Try smaller executable sizes first instead of rejecting
            # every valid signal on roundtrip drag.
            max_trade = min(max_trade, env_float("PGG2_LIVE_PRICED_BREAKOUT_MAX_TRADE_SOL", 0.025))
            min_trade = min(max_trade, env_float("PGG2_LIVE_PRICED_BREAKOUT_MIN_TRADE_SOL", 0.010))
        elif lane == "late_swarm":
            max_trade = min(max_trade, env_float("PGG2_LIVE_LATE_SWARM_MAX_TRADE_SOL", 0.020))
            min_trade = min(max_trade, env_float("PGG2_LIVE_LATE_SWARM_MIN_TRADE_SOL", 0.010))
        elif lane == "curve_lag_reveal":
            feats = features or {}
            wave_base_move = float(feats.get("wave_base_move") or 1.0)
            true_lag_discount = wave_base_move <= env_float("PGG2_LIVE_CURVE_LAG_FULL_MAX_WAVE_BASE_MOVE", 0.90)
            if not true_lag_discount:
                max_trade = min(max_trade, env_float("PGG2_LIVE_FLAT_CURVE_LAG_MAX_TRADE_SOL", 0.010))
                min_trade = min(max_trade, env_float("PGG2_LIVE_FLAT_CURVE_LAG_MIN_TRADE_SOL", 0.005))
        elif lane == "second_wave_after_cluster":
            feats = features or {}
            buy700 = float(feats.get("buy700") or 0.0)
            uniq700 = int(feats.get("uniq700") or 0)
            top_share700 = float(feats.get("top_share700") or 1.0)
            weak_second_wave = (
                buy700 < env_float("PGG2_LIVE_SECOND_WAVE_FULL_MIN_BUY700", 5.0)
                or uniq700 < env_int("PGG2_LIVE_SECOND_WAVE_FULL_MIN_UNIQ700", 4)
                or top_share700 > env_float("PGG2_LIVE_SECOND_WAVE_FULL_MAX_TOP700", 0.62)
            )
            if weak_second_wave:
                max_trade = min(max_trade, env_float("PGG2_LIVE_WEAK_SECOND_WAVE_MAX_TRADE_SOL", 0.010))
                min_trade = min(max_trade, env_float("PGG2_LIVE_WEAK_SECOND_WAVE_MIN_TRADE_SOL", 0.005))
        return max_trade, min_trade

    def live_edge_reject_reason(self, lane: str, features: Optional[dict[str, Any]] = None) -> str:
        if not env_bool("PGG2_LIVE_EDGE_FILTER_ENABLED", False):
            return ""
        feats = features or {}

        def f(name: str, default: float = 0.0) -> float:
            try:
                return float(feats.get(name, default) or default)
            except (TypeError, ValueError):
                return default

        if lane == "birth_fanout":
            # Live quote data showed broad birth_fanout was the biggest leak.
            # Keep only breadth-heavy fanouts where score or sustained 1500ms flow
            # supports the paper signal after executable quote overhead.
            uniq1500 = f("uniq1500")
            score = f("score")
            buy1500 = f("buy1500")
            buy700 = f("buy700")
            top1500 = f("top_share1500", 1.0)
            top700 = f("top_share700", 1.0)
            vsol = f("vsol_sol")
            sell1500 = f("sell1500")
            first_buy = f("first_buy_sol")
            move700 = f("move700", 1.0)
            live_unique700 = f("birth_fanout_live_unique700")
            live_top700 = f("birth_fanout_live_top700", 1.0)
            proven_birth_profile = (
                env_bool("PGG2_LIVE_EDGE_BIRTH_PROVEN_PROFILE_ENABLED", False)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MIN_UNIQ1500", 8.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MIN_BUY1500", 3.0)
                and max(top700, top1500) <= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MAX_TOP", 0.32)
                and first_buy >= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MIN_FIRST_BUY_SOL", 3.0)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MAX_SELL1500", 0.05)
                and move700 <= env_float("PGG2_LIVE_EDGE_BIRTH_PROVEN_MAX_MOVE700", 1.05)
            )
            live_breadth_birth = (
                env_bool("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_EXCEPTION_ENABLED", True)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MIN_UNIQ1500", 8.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MIN_BUY1500", 4.5)
                and top1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MAX_TOP1500", 0.50)
                and live_unique700 >= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MIN_LIVE700", 12.0)
                and live_top700 <= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MAX_LIVE_TOP700", 0.32)
                and first_buy >= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MIN_FIRST_BUY_SOL", 2.0)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MAX_SELL1500", 0.05)
                and move700 <= env_float("PGG2_LIVE_EDGE_BIRTH_LIVE_BREADTH_MAX_MOVE700", 1.08)
            )
            clean_early_birth = (
                env_bool("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_EXCEPTION_ENABLED", True)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MIN_BUY1500", 14.0)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MIN_UNIQ1500", 8.0)
                and max(top700, top1500) <= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MAX_TOP", 0.38)
                and first_buy >= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MIN_FIRST_BUY_SOL", 5.5)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MAX_SELL1500", 0.001)
                and move700 <= env_float("PGG2_LIVE_EDGE_BIRTH_CLEAN_EARLY_MAX_MOVE700", 1.08)
            )
            if env_bool("PGG2_LIVE_EDGE_BIRTH_REQUIRE_PROVEN_PROFILE", False):
                if proven_birth_profile or live_breadth_birth or clean_early_birth:
                    return ""
                return (
                    f"birth_not_proven_profile:u={uniq1500:.0f},buy={buy1500:.2f},"
                    f"top={max(top700, top1500):.2f},first={first_buy:.2f},sell={sell1500:.2f},"
                    f"m700={move700:.3f}"
                )
            if proven_birth_profile or live_breadth_birth or clean_early_birth:
                return ""
            broad_birth_moonshot = (
                env_bool("PGG2_LIVE_EDGE_BIRTH_BROAD_MOONSHOT_EXCEPTION_ENABLED", True)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_BROAD_MIN_UNIQ1500", 9.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_BROAD_MIN_BUY1500", 10.0)
                and buy1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_BROAD_MAX_BUY1500", 80.0)
                and max(top700, top1500) <= env_float("PGG2_LIVE_EDGE_BIRTH_BROAD_MAX_TOP", 0.40)
                and first_buy >= env_float("PGG2_LIVE_EDGE_BIRTH_BROAD_MIN_FIRST_BUY_SOL", 2.0)
            )
            if broad_birth_moonshot:
                return ""
            lean_birth_moonshot = (
                env_bool("PGG2_LIVE_EDGE_BIRTH_LEAN_MOONSHOT_EXCEPTION_ENABLED", True)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MIN_UNIQ1500", 8.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MIN_BUY1500", 3.5)
                and min(top700, top1500) <= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MAX_TOP", 0.32)
                and vsol >= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MIN_VSOL", 38.0)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MAX_SELL1500", 0.05)
                and (
                    score >= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_MIN_SCORE", 130.0)
                    or buy700 >= env_float("PGG2_LIVE_EDGE_BIRTH_LEAN_ALT_MIN_BUY700", 8.0)
                )
            )
            if lean_birth_moonshot:
                return ""
            if uniq1500 < env_float("PGG2_LIVE_EDGE_BIRTH_MIN_UNIQ1500", 14.0):
                return f"birth_uniq1500_low:{uniq1500:.0f}"
            if buy1500 < env_float("PGG2_LIVE_EDGE_BIRTH_HARD_MIN_BUY1500", 14.0):
                return f"birth_buy1500_low:{buy1500:.2f}"
            if not (
                score >= env_float("PGG2_LIVE_EDGE_BIRTH_MIN_SCORE", 230.0)
                or buy1500 >= env_float("PGG2_LIVE_EDGE_BIRTH_MIN_BUY1500", 15.5)
            ):
                return f"birth_conviction_low:score={score:.1f},buy1500={buy1500:.2f}"
            if top1500 > env_float("PGG2_LIVE_EDGE_BIRTH_MAX_TOP1500", 0.30):
                return f"birth_top1500_high:{top1500:.2f}"
            return ""

        if lane == "curve_lag_reveal":
            top700 = f("top_share700", 1.0)
            vsol = f("vsol_sol", 999.0)
            score = f("score")
            buy1500 = f("buy1500")
            buy700 = f("buy700")
            uniq700 = f("uniq700")
            move700 = f("move700", 1.0)
            broad_curve_lag = (
                env_bool("PGG2_LIVE_EDGE_CURVE_BROAD_LAG_EXCEPTION_ENABLED", True)
                and buy700 >= env_float("PGG2_LIVE_EDGE_CURVE_BROAD_MIN_BUY700", 8.0)
                and uniq700 >= env_float("PGG2_LIVE_EDGE_CURVE_BROAD_MIN_UNIQ700", 4.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_CURVE_BROAD_MAX_TOP700", 0.35)
                and vsol >= env_float("PGG2_LIVE_EDGE_CURVE_BROAD_MIN_VSOL", 30.0)
            )
            if broad_curve_lag:
                return ""
            if env_bool("PGG2_LIVE_EDGE_CURVE_REQUIRE_BROAD_ONLY", False):
                return (
                    f"curve_not_broad_profile:b700={buy700:.2f},u700={uniq700:.0f},"
                    f"top700={top700:.2f},vsol={vsol:.2f}"
                )
            strong_top = top700 >= env_float("PGG2_LIVE_EDGE_CURVE_STRONG_TOP700", 0.50)
            low_vsol_breadth = (
                top700 >= env_float("PGG2_LIVE_EDGE_CURVE_LOW_VSOL_MIN_TOP700", 0.3458)
                and vsol <= env_float("PGG2_LIVE_EDGE_CURVE_LOW_VSOL_MAX", 35.0)
            )
            lowtop_low_vsol_bootstrap = (
                env_bool("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_ENABLED", False)
                and score <= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MAX_SCORE", 105.0)
                and top700 >= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MIN_TOP700", 0.20)
                and top700 < env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MAX_TOP700", 0.30)
                and vsol <= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MAX_VSOL", 35.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MIN_BUY1500", 5.0)
                and f("uniq700") >= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MIN_UNIQ700", 7.0)
                and move700 <= env_float("PGG2_LIVE_EDGE_CURVE_LOWTOP_BOOTSTRAP_MAX_MOVE700", 0.95)
            )
            bootstrap_breadth = (
                score < env_float("PGG2_LIVE_EDGE_CURVE_BOOTSTRAP_MAX_SCORE", 100.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_CURVE_BOOTSTRAP_MIN_BUY1500", 17.5)
                and vsol <= env_float("PGG2_LIVE_EDGE_CURVE_BOOTSTRAP_MAX_VSOL", 35.0)
            )
            if not (strong_top or low_vsol_breadth or lowtop_low_vsol_bootstrap or bootstrap_breadth):
                return f"curve_quote_edge_low:top700={top700:.2f},vsol={vsol:.2f},buy1500={buy1500:.2f}"
            if (
                not lowtop_low_vsol_bootstrap
                and top700 < env_float("PGG2_LIVE_EDGE_CURVE_LOW_VSOL_MIN_TOP700", 0.3458)
                and move700 < env_float("PGG2_LIVE_EDGE_CURVE_MIN_MOVE700", 0.98)
            ):
                return f"curve_red_bootstrap:top700={top700:.2f},move700={move700:.3f}"
            if (
                not lowtop_low_vsol_bootstrap
                and
                score < env_float("PGG2_LIVE_EDGE_CURVE_BOOTSTRAP_MAX_SCORE", 100.0)
                and top700 < env_float("PGG2_LIVE_EDGE_CURVE_LOW_VSOL_MIN_TOP700", 0.3458)
                and vsol > env_float("PGG2_LIVE_EDGE_CURVE_BOOTSTRAP_MAX_VSOL_STRICT", 38.0)
            ):
                return f"curve_expensive_bootstrap:top700={top700:.2f},vsol={vsol:.2f}"
            return ""

        if lane == "breadth_ignition":
            # V2 live-only filter. Quote-shadow history showed raw breadth_ignition
            # is too weak after executable quote overhead. Keep only real breadth:
            # enough score, buy flow, unique buyers, positive 700ms movement, and
            # non-dominant slot flow.
            score = f("score")
            buy1500 = f("buy1500")
            uniq1500 = f("uniq1500")
            move700 = f("move700", 1.0)
            vsol = f("vsol_sol")
            slot_buyers = f("slot_buyers")
            slot_top = f("slot_top_share", 1.0)
            top1500 = f("top_share1500", 1.0)
            sell1500 = f("sell1500")
            low_score_flat_exception = (
                env_bool("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_FLAT_EXCEPTION_ENABLED", False)
                and score <= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MAX_SCORE", 120.0)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MIN_BUY1500", 2.50)
                and buy1500 <= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MAX_BUY1500", 3.20)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MIN_UNIQ1500", 8.0)
                and top1500 <= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MAX_TOP1500", 0.32)
                and vsol >= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MIN_VSOL", 45.0)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MAX_SELL1500", 0.001)
                and move700 >= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MIN_MOVE700", 0.95)
                and move700 <= env_float("PGG2_LIVE_EDGE_BREADTH_LOW_SCORE_MAX_MOVE700", 1.08)
            )
            if low_score_flat_exception:
                return ""
            if score < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_SCORE", 140.0):
                return f"breadth_score_low:{score:.1f}"
            if buy1500 < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_BUY1500", 3.0):
                return f"breadth_buy1500_low:{buy1500:.2f}"
            if uniq1500 < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_UNIQ1500", 8.0):
                return f"breadth_uniq1500_low:{uniq1500:.0f}"
            if move700 < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_MOVE700", 1.05):
                return f"breadth_move700_low:{move700:.3f}"
            if vsol < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_VSOL", 40.0):
                return f"breadth_vsol_low:{vsol:.2f}"
            if slot_buyers < env_float("PGG2_LIVE_EDGE_BREADTH_MIN_SLOT_BUYERS", 5.0):
                return f"breadth_slot_buyers_low:{slot_buyers:.0f}"
            if slot_top > env_float("PGG2_LIVE_EDGE_BREADTH_MAX_SLOT_TOP", 0.35):
                return f"breadth_slot_top_high:{slot_top:.2f}"
            return ""

        if lane == "reclaim_wave":
            uniq700 = f("uniq700")
            top700 = f("top_share700", 1.0)
            score = f("score")
            buy700 = f("buy700")
            buy1500 = f("buy1500")
            sell1500 = f("sell1500")
            move700 = f("move700", 1.0)
            wave_base = f("wave_base_move", 1.0)
            vsol = f("vsol_sol")
            sell_ratio = sell1500 / max(buy1500, 0.001)
            burst_reclaim = (
                env_bool("PGG2_LIVE_EDGE_RECLAIM_BURST_EXCEPTION_ENABLED", True)
                and score >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_SCORE", 135.0)
                and buy700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_BUY700", 2.5)
                and uniq700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_UNIQ700", 3.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MAX_TOP700", 0.60)
                and wave_base >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_BASE_MOVE", 1.35)
                and move700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_MOVE700", 1.06)
                and vsol >= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MIN_VSOL", 45.0)
                and sell_ratio <= env_float("PGG2_LIVE_EDGE_RECLAIM_BURST_MAX_SELL_RATIO", 0.05)
            )
            if burst_reclaim:
                return ""
            clean_momentum_reclaim = (
                env_bool("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_EXCEPTION_ENABLED", True)
                and score >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_SCORE", 300.0)
                and buy700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_BUY700", 8.0)
                and uniq700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_UNIQ700", 6.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MAX_TOP700", 0.30)
                and sell_ratio <= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MAX_SELL_RATIO", 0.01)
                and move700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_MOVE700", 1.30)
                and wave_base >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_BASE", 1.15)
                and vsol >= env_float("PGG2_LIVE_EDGE_RECLAIM_CLEAN_MOMENTUM_MIN_VSOL", 38.0)
            )
            if clean_momentum_reclaim:
                # Quote-tape fast validation: 17 skipped clean-momentum reclaim
                # candidates matched this profile; 16 reached >=1.32x and 13
                # reached >=1.5x. This removes the live filter blind spot without
                # touching noisy birth/high-top profiles.
                return ""
            late_base_reclaim = (
                env_bool("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_EXCEPTION_ENABLED", True)
                and wave_base >= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MIN_BASE", 2.30)
                and vsol >= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MIN_VSOL", 55.0)
                and buy700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MIN_BUY700", 15.0)
                and uniq700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MIN_UNIQ700", 5.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MAX_TOP700", 0.45)
                and move700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MIN_MOVE700", 1.12)
                and sell_ratio <= env_float("PGG2_LIVE_EDGE_RECLAIM_LATE_BASE_MAX_SELL_RATIO", 0.08)
            )
            if late_base_reclaim:
                # Quote-tape fast validation: 5 skipped late-base reclaim
                # candidates matched this profile; all 5 reached >=1.8x. DdmY
                # was blocked by this exact missing exception and then ran 2.85x.
                return ""
            explosive_reclaim = (
                env_bool("PGG2_LIVE_EDGE_RECLAIM_EXP_EXCEPTION_ENABLED", False)
                and score >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_SCORE", 240.0)
                and buy700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_BUY700", 5.5)
                and buy1500 >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_BUY1500", 6.0)
                and uniq700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_UNIQ700", 5.0)
                and wave_base >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_BASE_MOVE", 2.0)
                and move700 >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_MOVE700", 1.18)
                and vsol >= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MIN_VSOL", 55.0)
                and sell_ratio <= env_float("PGG2_LIVE_EDGE_RECLAIM_EXP_MAX_SELL_RATIO", 0.12)
            )
            if explosive_reclaim:
                return ""
            if env_bool("PGG2_LIVE_EDGE_RECLAIM_REQUIRE_EXPLOSIVE", False):
                return (
                    f"reclaim_not_explosive:score={score:.1f},b700={buy700:.2f},"
                    f"base={wave_base:.2f},m700={move700:.3f},vsol={vsol:.2f},sellr={sell_ratio:.2f}"
                )
            if move700 > env_float("PGG2_LIVE_EDGE_RECLAIM_MAX_MOVE700", 99.0):
                return f"reclaim_move700_high:{move700:.3f}"
            if uniq700 < env_float("PGG2_LIVE_EDGE_RECLAIM_MIN_UNIQ700", 7.0):
                return f"reclaim_uniq700_low:{uniq700:.0f}"
            if top700 > env_float("PGG2_LIVE_EDGE_RECLAIM_MAX_TOP700", 0.32):
                return f"reclaim_top700_high:{top700:.2f}"
            return ""

        if lane == "second_wave_after_cluster":
            uniq1500 = f("uniq1500")
            top700 = f("top_share700", 1.0)
            move700 = f("move700", 1.0)
            wave_base = f("wave_base_move", 1.0)
            score = f("score")
            vsol = f("vsol_sol")
            buy700 = f("buy700")
            sell1500 = f("sell1500")
            low_uniq_momentum_exception = (
                env_bool("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MOMENTUM_EXCEPTION_ENABLED", True)
                and score >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_SCORE", 180.0)
                and buy700 >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_BUY700", 5.0)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_UNIQ1500", 4.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MAX_TOP700", 0.38)
                and move700 >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_MOVE700", 1.10)
                and move700 <= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MAX_MOVE700", 1.25)
                and wave_base >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_BASE", 1.20)
                and vsol >= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MIN_VSOL", 40.0)
                and sell1500 <= env_float("PGG2_LIVE_EDGE_SECOND_LOW_UNIQ_MAX_SELL1500", 0.02)
            )
            if low_uniq_momentum_exception:
                return ""
            late_base_exception = (
                env_bool("PGG2_LIVE_EDGE_SECOND_LATE_BASE_EXCEPTION_ENABLED", False)
                and score >= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MIN_SCORE", 180.0)
                and wave_base >= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MIN_BASE", 1.50)
                and uniq1500 >= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MIN_UNIQ1500", 4.0)
                and top700 <= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MAX_TOP700", 0.41)
                and move700 <= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MAX_MOVE700", 1.25)
                and vsol >= env_float("PGG2_LIVE_EDGE_SECOND_LATE_BASE_MIN_VSOL", 40.0)
            )
            if late_base_exception:
                return ""
            if uniq1500 < env_float("PGG2_LIVE_EDGE_SECOND_MIN_UNIQ1500", 8.0):
                return f"second_uniq1500_low:{uniq1500:.0f}"
            if top700 > env_float("PGG2_LIVE_EDGE_SECOND_MAX_TOP700", 0.40):
                return f"second_top700_high:{top700:.2f}"
            if sell1500 > env_float("PGG2_LIVE_EDGE_SECOND_MAX_SELL1500", 0.001):
                return f"second_sell1500_high:{sell1500:.3f}"
            return ""

        if lane == "curve_arm_scout":
            buy1500 = f("buy1500")
            uniq1500 = f("uniq1500")
            top1500 = f("top_share1500", 1.0)
            sell1500 = f("sell1500")
            vsol = f("vsol_sol")
            move700 = f("move700", 1.0)
            if buy1500 < env_float("PGG2_LIVE_EDGE_CURVE_ARM_MIN_BUY1500", 2.50):
                return f"curve_arm_buy1500_low:{buy1500:.2f}"
            if buy1500 > env_float("PGG2_LIVE_EDGE_CURVE_ARM_MAX_BUY1500", 4.00):
                return f"curve_arm_buy1500_high:{buy1500:.2f}"
            if uniq1500 < env_float("PGG2_LIVE_EDGE_CURVE_ARM_MIN_UNIQ1500", 3.0):
                return f"curve_arm_uniq1500_low:{uniq1500:.0f}"
            if top1500 > env_float("PGG2_LIVE_EDGE_CURVE_ARM_MAX_TOP1500", 0.40):
                return f"curve_arm_top1500_high:{top1500:.2f}"
            if sell1500 > env_float("PGG2_LIVE_EDGE_CURVE_ARM_MAX_SELL1500", 0.001):
                return f"curve_arm_sell1500_high:{sell1500:.3f}"
            if vsol < env_float("PGG2_LIVE_EDGE_CURVE_ARM_MIN_VSOL", 40.0):
                return f"curve_arm_vsol_low:{vsol:.2f}"
            if move700 > env_float("PGG2_LIVE_EDGE_CURVE_ARM_MAX_MOVE700", 1.03):
                return f"curve_arm_move700_high:{move700:.3f}"
            return ""

        if lane in {"early_ignition", "late_ignition"}:
            score = f("score")
            buy1500 = f("buy1500")
            move700 = f("move700", 1.0)
            wave_base = f("wave_base_move", 1.0)
            sell1500 = f("sell1500")
            if lane == "early_ignition":
                if move700 > env_float("PGG2_LIVE_EDGE_EARLY_MAX_MOVE700", 1.25):
                    return f"early_chase_move700_high:{move700:.3f}"
                if wave_base > env_float("PGG2_LIVE_EDGE_EARLY_MAX_BASE_MOVE", 1.25):
                    return f"early_chase_base_high:{wave_base:.3f}"
                if sell1500 > env_float("PGG2_LIVE_EDGE_EARLY_MAX_SELL1500", 0.05):
                    return f"early_sell1500_high:{sell1500:.3f}"
            if score < env_float("PGG2_LIVE_EDGE_IGNITION_MIN_SCORE", 190.0):
                return f"ignition_score_low:{score:.1f}"
            if buy1500 < env_float("PGG2_LIVE_EDGE_IGNITION_MIN_BUY1500", 6.0):
                return f"ignition_buy1500_low:{buy1500:.2f}"
            return ""

        return ""

    def guarded_amount(
        self,
        requested_sol: float,
        lane: str = "",
        features: Optional[dict[str, Any]] = None,
        mint: str = "",
    ) -> Optional[float]:
        edge_reason = self.live_edge_reject_reason(lane, features)
        if edge_reason:
            mint_label = f" {short_addr(mint)}" if mint else ""
            log(f"PGG2-LIVE-EDGE-SKIP{mint_label} lane={lane or '?'} reason={edge_reason}")
            return None
        if self.stats.realized_pnl_sol <= -self.max_session_loss_sol:
            log(f"PGG2-LIVE-BLOCK session_loss_cap pnl={self.stats.realized_pnl_sol:+.6f}")
            return None
        if self.consecutive_losses >= self.max_consecutive_losses:
            log(f"PGG2-LIVE-BLOCK consecutive_losses={self.consecutive_losses}")
            return None
        requested = max(0.0, float(requested_sol))
        max_trade, min_trade = self.lane_trade_bounds(lane, features)
        amount = min(requested, max_trade)
        if amount < min_trade:
            log(
                f"PGG2-LIVE-BLOCK requested_below_min lane={lane or '?'} "
                f"requested={requested:.6f} min_trade={min_trade:.6f}"
            )
            return None
        if amount < requested:
            log(
                f"PGG2-LIVE-LANE-CAP lane={lane or '?'} "
                f"requested={requested:.6f} capped={amount:.6f}"
            )
        try:
            bal = self.balance_sol()
            if bal - amount < self.min_wallet_reserve_sol:
                log(f"PGG2-LIVE-BLOCK balance={bal:.6f} amount={amount:.6f} reserve={self.min_wallet_reserve_sol:.6f}")
                return None
        except Exception as exc:
            if not self.quote_only:
                raise
            log(f"PGG2-LIVE-BALANCE-WARN {type(exc).__name__}: {exc}")
        return amount

    def queue_or_fill(self, plan: StrikePlan, price: float) -> Optional[Position]:
        """Do not let rejected live quotes poison strike timing.

        The base paper broker marks ``last_strike_ts_ms`` before attempting the
        fill. In live/quote mode that is wrong: an edge-filtered or quote-blocked
        candidate is not a real entry, and it must not create global strike-rate
        or same-mint cooldown. Current quote tape showed this exact failure:
        an early blocked curve-lag quote caused later, better birth/reclaim
        entries on the same mint to be skipped.
        """
        ok, reason = self.can_strike(plan.mint, plan.ts_ms)
        if not ok:
            return None
        if price > 0 and plan.lane != "first_public_buy":
            pos = self.open_position(plan, price, plan.ts_ms)
            if pos:
                self.stats.strike_plans += 1
                self.last_strike_ts_ms = plan.ts_ms
            return pos

        self.stats.strike_plans += 1
        self.last_strike_ts_ms = plan.ts_ms
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

    def open_position(self, plan: StrikePlan, price: float, ts_ms: int) -> Optional[Position]:
        if price <= 0:
            return None
        requested = max(0.0005, min(plan.scout_sol, plan.target_sol))
        amount = self.guarded_amount(requested, plan.lane, plan.features, plan.mint)
        if amount is None:
            return None
        target_sol = min(plan.target_sol, self.max_trade_sol)
        try:
            _max_trade, min_trade = self.lane_trade_bounds(plan.lane, plan.features)
            candidate = self.quote_entry_candidate(plan, amount, min(amount, min_trade))
            if candidate is None:
                return None
            amount, impact, quote, roundtrip_loss = candidate
            quote_tokens = self.rate_amount_out(quote)
            if env_bool("PGG2_LIVE_FINAL_ENTRY_GUARD_ENABLED", True) and quote_tokens > 0:
                reverse = self.build_swap(plan.mint, SOL_MINT, round(quote_tokens, 9), self.sell_slippage)
                immediate_out = self.rate_amount_out(reverse)
                overhead = self.quote_roundtrip_overhead_sol if self.quote_shadow_positions else 0.0
                immediate_pnl = immediate_out - overhead - amount
                max_loss = self.max_executable_loss_for_lane(plan.lane, amount, plan.features, plan.reason)
                if immediate_pnl < -max_loss:
                    log(
                        f"PGG2-LIVE-FINAL-ENTRY-BLOCK {short_addr(plan.mint)} lane={plan.lane} "
                        f"amount={amount:.6f} quote_out={immediate_out:.6f} "
                        f"pnl={immediate_pnl:+.6f} max_loss={max_loss:.6f}"
                    )
                    return None
            if amount < requested or plan.lane == "birth_fanout":
                target_sol = amount
            if self.quote_only:
                if self.quote_simulate and self.keypair:
                    signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
                    self.simulate_signed(signed_b64)
                if self.quote_shadow_positions:
                    if quote_tokens <= 0:
                        raise RuntimeError("quote shadow buy missing positive amountOut")
                    fill_price = price * (1.0 + self.drag)
                    paper_tokens = amount / max(fill_price, 1e-18)
                    pos = Position(
                        mint=plan.mint,
                        state="SCOUT",
                        opened_ts_ms=ts_ms,
                        avg_price=fill_price,
                        tokens_bought=paper_tokens,
                        remaining_tokens=paper_tokens,
                        cost_sol=amount,
                        scout_sol=amount,
                        target_sol=target_sol,
                        lane=plan.lane,
                        reason=plan.reason,
                        entry_features=dict(plan.features),
                        peak_price=fill_price,
                        last_price=fill_price,
                    )
                    self.positions[plan.mint] = pos
                    self.quote_shadow_tokens[plan.mint] = quote_tokens
                    self.stats.scouts += 1
                    log(
                        f"PGG2-QUOTE-SHADOW-BUY {short_addr(plan.mint)} lane={plan.lane} "
                        f"cost={amount:.6f} quote_tokens={quote_tokens:.6f} fill={fill_price:.9e} "
                        f"score={plan.score:.1f} impact={impact:.4f} "
                        f"entry_roundtrip_loss={roundtrip_loss:.6f}"
                    )
                    self.save_state()
                    return pos
                log(f"PGG2-LIVE-QUOTE-ONLY-BUY {short_addr(plan.mint)} lane={plan.lane} amount={amount:.6f}")
                self.closed_recent[plan.mint] = ts_ms
                return None
            signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
            if not self.simulate_signed(signed_b64):
                self.closed_recent[plan.mint] = ts_ms
                return None
            sig = self.send_signed(signed_b64)
            if self.fast_paper_accounting:
                wallet_delta = -amount
                actual_cost = amount
            else:
                if not self.wait_confirmed(sig):
                    self.closed_recent[plan.mint] = ts_ms
                    return None
                wallet_delta = self.transaction_wallet_delta_sol(sig)
                actual_cost = max(amount, -wallet_delta)
            fill_price = price * (1.0 + self.drag)
            tokens = actual_cost / max(fill_price, 1e-18)
            pos = Position(
                mint=plan.mint,
                state="SCOUT",
                opened_ts_ms=ts_ms,
                avg_price=fill_price,
                tokens_bought=tokens,
                remaining_tokens=tokens,
                cost_sol=actual_cost,
                scout_sol=actual_cost,
                target_sol=target_sol,
                lane=plan.lane,
                reason=plan.reason,
                entry_features=dict(plan.features),
                peak_price=fill_price,
                last_price=fill_price,
            )
            self.positions[plan.mint] = pos
            self.stats.scouts += 1
            log(
                f"PGG2-LIVE-BUY {short_addr(plan.mint)} lane={plan.lane} cost={actual_cost:.6f} "
                f"wallet_delta={wallet_delta:+.6f} sig={sig} score={plan.score:.1f}"
            )
            self.save_state()
            return pos
        except Exception as exc:
            log(f"PGG2-LIVE-BUY-FAIL {short_addr(plan.mint)} {type(exc).__name__}: {exc}")
            if not self.quote_only:
                self.closed_recent[plan.mint] = ts_ms
            self.save_state()
            return None

    def scale(self, mint: str, add_sol: float, price: float, state: str, reason: str) -> Optional[Position]:
        log(f"PGG2-LIVE-SCALE-BLOCKED {short_addr(mint)} requested={add_sol:.6f} reason={reason}")
        return None

    def partial(self, mint: str, fraction: float, price: float, reason: str) -> Optional[Position]:
        log(f"PGG2-LIVE-PARTIAL-BLOCKED {short_addr(mint)} fraction={fraction:.2f} reason={reason}")
        return None

    def close(self, mint: str, ts_ms: int, price: float, reason: str, killed: bool) -> Optional[float]:
        pos = self.positions.get(mint)
        if not pos:
            return None
        if price > 0:
            pos.update(price)
        try:
            sell_amount: Any = "auto"
            if self.quote_only and self.quote_shadow_positions:
                quote_tokens = self.quote_shadow_tokens.get(mint, pos.remaining_tokens)
                remaining_fraction = pos.remaining_tokens / max(pos.tokens_bought, 1e-18)
                sell_amount = round(quote_tokens * remaining_fraction, 9)
            quote = self.build_swap(mint, SOL_MINT, sell_amount, self.sell_slippage)
            expected_out = self.rate_amount_out(quote)
            overhead = self.quote_roundtrip_overhead_sol if self.quote_shadow_positions else 0.0
            min_profit_out = env_float(
                "PGG2_LIVE_MIN_PROFIT_EXIT_SOL",
                overhead,
            )
            executable_pnl = pos.realized_sol + expected_out - overhead - pos.cost_sol
            if (
                (self.mode == "live" or self.quote_shadow_positions)
                and self.profit_exit_reason(reason)
                and expected_out > 0.0
                and expected_out
                < pos.cost_sol
                + min_profit_out
            ):
                defensive_sell = (
                    env_bool("PGG2_LIVE_PROFIT_SIGNAL_DEFENSIVE_SELL_ENABLED", False)
                    and pos.peak_mult >= env_float("PGG2_LIVE_PROFIT_SIGNAL_DEFENSIVE_MIN_PEAK", 1.25)
                    and executable_pnl
                    >= env_float("PGG2_LIVE_PROFIT_SIGNAL_DEFENSIVE_MIN_PNL_SOL", 0.0)
                )
                if defensive_sell:
                    log(
                        f"PGG2-LIVE-PROFIT-SIGNAL-DEFENSIVE-SELL {short_addr(mint)} reason={reason} "
                        f"quote_out={expected_out:.6f} pnl={executable_pnl:+.6f} "
                        f"peak={pos.peak_mult:.3f} need={pos.cost_sol + min_profit_out:.6f}"
                    )
                else:
                    log(
                        f"PGG2-LIVE-SELL-HOLD {short_addr(mint)} reason={reason} "
                        f"quote_out={expected_out:.6f} cost={pos.cost_sol:.6f} "
                        f"pnl={executable_pnl:+.6f} need={pos.cost_sol + min_profit_out:.6f}"
                    )
                    return None
            if self.quote_only:
                if self.quote_simulate and self.keypair:
                    signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
                    self.simulate_signed(signed_b64)
                if self.quote_shadow_positions:
                    self.positions.pop(mint, None)
                    self.quote_shadow_tokens.pop(mint, None)
                    proceeds = max(0.0, expected_out - overhead)
                    pnl = pos.realized_sol + proceeds - pos.cost_sol
                    self.stats.realized_pnl_sol += pnl
                    self.stats.closes += 1
                    if killed:
                        self.stats.kills += 1
                    if pnl >= 0:
                        self.stats.wins += 1
                        self.consecutive_losses = 0
                    else:
                        self.stats.losses += 1
                        self.consecutive_losses += 1
                    exec_mult = proceeds / max(pos.cost_sol, 1e-18)
                    self.stats.best_mult = max(self.stats.best_mult, pos.peak_mult, exec_mult)
                    if self.allow_fast_reentry_after_close(pos, reason, pnl):
                        log(
                            f"PGG2-LIVE-FAST-REENTRY-UNLOCK {short_addr(mint)} "
                            f"lane={pos.lane} reason={reason} pnl={pnl:+.6f}"
                        )
                    else:
                        self.closed_recent[mint] = ts_ms
                    log(
                        f"PGG2-QUOTE-SHADOW-SELL {short_addr(mint)} reason={reason} "
                        f"quote_out={expected_out:.6f} overhead={overhead:.6f} "
                        f"proceeds={proceeds:.6f} mult={exec_mult:.3f} pnl={pnl:+.6f} "
                        f"session={self.stats.realized_pnl_sol:+.6f}"
                    )
                    self.save_state()
                    return pnl
                log(f"PGG2-LIVE-QUOTE-ONLY-SELL {short_addr(mint)} reason={reason}")
                return None
            signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
            if not self.simulate_signed(signed_b64):
                return None
            sig = self.send_signed(signed_b64)
            if not self.wait_confirmed(sig):
                return None
            wallet_delta = self.transaction_wallet_delta_sol(sig)
            proceeds = max(0.0, wallet_delta)
            self.positions.pop(mint, None)
            pnl = pos.realized_sol + proceeds - pos.cost_sol
            self.stats.realized_pnl_sol += pnl
            self.stats.closes += 1
            if killed:
                self.stats.kills += 1
            if pnl >= 0:
                self.stats.wins += 1
                self.consecutive_losses = 0
            else:
                self.stats.losses += 1
                self.consecutive_losses += 1
            exec_mult = proceeds / max(pos.cost_sol, 1e-18)
            self.stats.best_mult = max(self.stats.best_mult, pos.peak_mult, exec_mult)
            if self.allow_fast_reentry_after_close(pos, reason, pnl):
                log(
                    f"PGG2-LIVE-FAST-REENTRY-UNLOCK {short_addr(mint)} "
                    f"lane={pos.lane} reason={reason} pnl={pnl:+.6f}"
                )
            else:
                self.closed_recent[mint] = ts_ms
            log(
                f"PGG2-LIVE-SELL {short_addr(mint)} reason={reason} sig={sig} "
                f"proceeds={proceeds:.6f} wallet_delta={wallet_delta:+.6f} "
                f"mult={exec_mult:.3f} pnl={pnl:+.6f} session={self.stats.realized_pnl_sol:+.6f}"
            )
            self.save_state()
            return pnl
        except Exception as exc:
            log(f"PGG2-LIVE-SELL-FAIL {short_addr(mint)} {type(exc).__name__}: {exc}")
            self.save_state()
            return None
