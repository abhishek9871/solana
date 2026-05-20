"""V63 Final PnL Accounting (Phase 4).

Computes Stage A pass/fail using the formula:

    final_wallet_delta = wallet_after_close_account - wallet_before_buy

This is the only PnL accounting that catches ALL real wallet effects of a
trade, including:
  - The trade itself (buy + sell + ATA rent prepay + ATA rent recovery)
  - Tx fees (each base fee = 25_000 lamports for non-priority txs)
  - Compute-budget priority fees
  - SWQOS tips
  - Failed-retry tx fees burned by V62B sends that confirm late as Custom
    errors (e.g. Pump program rejects with "already closed" Custom 3012 when
    a sibling retry already cleared the position)
  - Helius Sender SWQOS-only tips
  - V63 standalone CloseAccount fees (if used)

Why broker.transaction_wallet_delta_sol summation misses the above:
  - Each call queries only ONE signature
  - Failed-retry sigs are not tracked at the harness level after V62B owns them
  - The bot's `actual_pnl = buy_delta + sell_delta` sums two delts; in the
    V62B RUN1 trade the broker delta sum was -510_461 lamports but the
    actual wallet delta was -560_461 lamports (50_000 lamport diff = 2x
    failed-retry V62B fees).

Usage:

    snap = V63FinalPnL.record_before(rpc_url, wallet)
    # ... buy + V62B sell + V63 close-account ...
    final = snap.compute_final(rpc_url, broker_delta_buy_sell, rent_recovered)
    if final.pass_:
        log("PGG2-V63-FINAL-PNL pass=true ...")

Logs:
  - PGG2-V63-FINAL-PNL
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional


LAMPORTS_PER_SOL = 1_000_000_000


def _rpc_call(rpc_url: str, method: str, params: list) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_balance_lamports(rpc_url: str, wallet: str) -> int:
    r = _rpc_call(rpc_url, "getBalance", [wallet])
    return int(r.get("result", {}).get("value", 0))


@dataclass
class V63FinalPnLSnapshot:
    """Snapshot taken BEFORE the buy. Stored on the position rec."""
    wallet_before_buy_lamports: int
    rpc_url: str
    wallet: str
    captured_ts_ms: int

    @classmethod
    def record_before(cls, rpc_url: str, wallet: str) -> "V63FinalPnLSnapshot":
        lam = _get_balance_lamports(rpc_url, wallet)
        return cls(
            wallet_before_buy_lamports=lam,
            rpc_url=rpc_url,
            wallet=wallet,
            captured_ts_ms=int(time.time() * 1000),
        )

    def compute_final(
        self,
        broker_delta_buy_sol: float,
        broker_delta_sell_sol: float,
        rent_recovered_sol: float,
        v63_status: Optional[str],
    ) -> "V63FinalPnLResult":
        wallet_after = _get_balance_lamports(self.rpc_url, self.wallet)
        final_delta_lam = wallet_after - self.wallet_before_buy_lamports
        final_delta_sol = float(final_delta_lam) / LAMPORTS_PER_SOL
        broker_sum_sol = float(broker_delta_buy_sol) + float(broker_delta_sell_sol)
        # Difference between authoritative wallet delta and broker-summed delta
        # is unattributed loss/gain: e.g. retry fees burnt, leftover positions
        # being included, or sells that landed after our broker call.
        unattributed_sol = final_delta_sol - (broker_sum_sol + rent_recovered_sol)
        return V63FinalPnLResult(
            wallet_before_lamports=self.wallet_before_buy_lamports,
            wallet_after_lamports=wallet_after,
            final_wallet_delta_lamports=final_delta_lam,
            final_wallet_delta_sol=final_delta_sol,
            broker_delta_buy_sol=float(broker_delta_buy_sol),
            broker_delta_sell_sol=float(broker_delta_sell_sol),
            broker_sum_sol=broker_sum_sol,
            rent_recovered_sol=float(rent_recovered_sol),
            unattributed_sol=unattributed_sol,
            v63_status=v63_status or "unknown",
            pass_=final_delta_sol >= 0.0,
        )


@dataclass
class V63FinalPnLResult:
    wallet_before_lamports: int
    wallet_after_lamports: int
    final_wallet_delta_lamports: int
    final_wallet_delta_sol: float
    broker_delta_buy_sol: float
    broker_delta_sell_sol: float
    broker_sum_sol: float
    rent_recovered_sol: float
    unattributed_sol: float
    v63_status: str
    pass_: bool

    def log_line(self, mint_short: str) -> str:
        return (
            f"PGG2-V63-FINAL-PNL mint={mint_short} "
            f"wallet_before={self.wallet_before_lamports} "
            f"wallet_after={self.wallet_after_lamports} "
            f"final_wallet_delta_sol={self.final_wallet_delta_sol:+.9f} "
            f"broker_delta_buy_sol={self.broker_delta_buy_sol:+.9f} "
            f"broker_delta_sell_sol={self.broker_delta_sell_sol:+.9f} "
            f"broker_sum_sol={self.broker_sum_sol:+.9f} "
            f"rent_recovered_sol={self.rent_recovered_sol:+.9f} "
            f"unattributed_sol={self.unattributed_sol:+.9f} "
            f"v63_status={self.v63_status} "
            f"pass={'true' if self.pass_ else 'false'}"
        )


if __name__ == "__main__":
    print("=== V63 Final PnL self-test ===")
    print("V63FinalPnLSnapshot and V63FinalPnLResult are dataclass containers.")
    print("Invoke via:")
    print("  snap = V63FinalPnLSnapshot.record_before(rpc_url, wallet)")
    print("  final = snap.compute_final(broker_buy_sol, broker_sell_sol, rent_sol, v63_status)")
    print()
    # Replay RUN1 numbers (post-hoc, as if we had recorded the snapshot)
    print("RUN1 replay (illustrative):")
    print("  wallet_before  = 107_659_494 lam")
    print("  wallet_after   = 107_099_033 lam")
    print("  final_delta    = -560_461 lam = -0.000560461 SOL")
    print("  broker_buy     = -0.007104080")
    print("  broker_sell    = +0.006593619")
    print("  broker_sum     = -0.000510461")
    print("  rent_recovered = 0 (already in sell tx atomically)")
    print("  unattributed   = -0.000050000  ← 2x V62B failed-retry fees (25k+25k)")
    print("  v63_status     = already_closed_in_sell_tx")
    print("  pass           = false (-0.000560 SOL < 0)")
