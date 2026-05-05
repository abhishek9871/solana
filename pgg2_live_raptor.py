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
        self.confirm_timeout_sec = env_float("PGG2_LIVE_CONFIRM_TIMEOUT_SEC", 8.0)
        log(
            f"PGG2-LIVE: mode={self.mode.upper()} wallet={short_addr(self.public_key)} "
            f"max_trade={self.max_trade_sol:.4f} reserve={self.min_wallet_reserve_sol:.4f} "
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
        headers = {"accept": "application/json"}
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
        req = Request(url, data=data, headers=merged_headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"http {exc.code} {exc.reason}: {body_text}") from exc
        return json.loads(raw) if raw else {}

    def rpc(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        out = self.request_json("POST", self.rpc_url, body=payload, headers={"content-type": "application/json"})
        if "error" in out:
            raise RuntimeError(f"rpc {method} error: {out['error']}")
        return out.get("result")

    def balance_sol(self) -> float:
        lamports = int(self.rpc("getBalance", [self.public_key]).get("value") or 0)
        return lamports / 1_000_000_000

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
                "skipPreflight": False,
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

    def guarded_amount(self, requested_sol: float) -> Optional[float]:
        if self.stats.realized_pnl_sol <= -self.max_session_loss_sol:
            log(f"PGG2-LIVE-BLOCK session_loss_cap pnl={self.stats.realized_pnl_sol:+.6f}")
            return None
        if self.consecutive_losses >= self.max_consecutive_losses:
            log(f"PGG2-LIVE-BLOCK consecutive_losses={self.consecutive_losses}")
            return None
        amount = max(0.0005, min(float(requested_sol), self.max_trade_sol))
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

    def open_position(self, plan: StrikePlan, price: float, ts_ms: int) -> Optional[Position]:
        if price <= 0:
            return None
        requested = max(0.0005, min(plan.scout_sol, plan.target_sol))
        amount = self.guarded_amount(requested)
        if amount is None:
            self.closed_recent[plan.mint] = ts_ms
            return None
        try:
            balance_before = self.balance_sol() if not self.quote_only else 0.0
            quote = self.build_swap(SOL_MINT, plan.mint, round(amount, 9), self.buy_slippage)
            if self.quote_only:
                if self.quote_simulate and self.keypair:
                    signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
                    self.simulate_signed(signed_b64)
                log(f"PGG2-LIVE-QUOTE-ONLY-BUY {short_addr(plan.mint)} lane={plan.lane} amount={amount:.6f}")
                self.closed_recent[plan.mint] = ts_ms
                return None
            signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
            if not self.simulate_signed(signed_b64):
                self.closed_recent[plan.mint] = ts_ms
                return None
            sig = self.send_signed(signed_b64)
            if not self.wait_confirmed(sig):
                self.closed_recent[plan.mint] = ts_ms
                return None
            balance_after = self.balance_sol()
            actual_cost = max(amount, balance_before - balance_after)
            tokens = actual_cost / max(price, 1e-18)
            pos = Position(
                mint=plan.mint,
                state="SCOUT",
                opened_ts_ms=ts_ms,
                avg_price=price,
                tokens_bought=tokens,
                remaining_tokens=tokens,
                cost_sol=actual_cost,
                scout_sol=actual_cost,
                target_sol=min(plan.target_sol, self.max_trade_sol),
                lane=plan.lane,
                reason=plan.reason,
                peak_price=price,
                last_price=price,
            )
            self.positions[plan.mint] = pos
            self.stats.scouts += 1
            log(
                f"PGG2-LIVE-BUY {short_addr(plan.mint)} lane={plan.lane} cost={actual_cost:.6f} "
                f"sig={sig} score={plan.score:.1f}"
            )
            self.save_state()
            return pos
        except Exception as exc:
            log(f"PGG2-LIVE-BUY-FAIL {short_addr(plan.mint)} {type(exc).__name__}: {exc}")
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
            balance_before = self.balance_sol()
            quote = self.build_swap(mint, SOL_MINT, "auto", self.sell_slippage)
            if self.quote_only:
                if self.quote_simulate and self.keypair:
                    signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
                    self.simulate_signed(signed_b64)
                log(f"PGG2-LIVE-QUOTE-ONLY-SELL {short_addr(mint)} reason={reason}")
                return None
            signed_b64, _signed_b58 = self.sign_transaction(str(quote["txn"]))
            if not self.simulate_signed(signed_b64):
                return None
            sig = self.send_signed(signed_b64)
            if not self.wait_confirmed(sig):
                return None
            balance_after = self.balance_sol()
            proceeds = max(0.0, balance_after - balance_before)
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
            self.stats.best_mult = max(self.stats.best_mult, pos.peak_mult)
            self.closed_recent[mint] = ts_ms
            log(
                f"PGG2-LIVE-SELL {short_addr(mint)} reason={reason} sig={sig} "
                f"proceeds={proceeds:.6f} pnl={pnl:+.6f} session={self.stats.realized_pnl_sol:+.6f}"
            )
            self.save_state()
            return pnl
        except Exception as exc:
            log(f"PGG2-LIVE-SELL-FAIL {short_addr(mint)} {type(exc).__name__}: {exc}")
            self.save_state()
            return None
