#!/usr/bin/env python3
"""V108 bundle profit model for raw-external-buy scalping.

Pure math: our buy -> external buy -> our sell+close. No sends.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable, Optional


LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
PUMP_FEE_BPS = 100
ATA_RENT_LAMPORTS = 2_039_280


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def quote_buy_tokens_raw(sol_lamports: int, vsol: int, vtok: int, fee_bps: int = PUMP_FEE_BPS) -> int:
    net = sol_lamports * (10_000 - fee_bps) // 10_000
    return max(0, (net * vtok) // max(vsol + net, 1))


def quote_sell_sol_lamports(tokens_raw: int, vsol: int, vtok: int, fee_bps: int = PUMP_FEE_BPS) -> int:
    gross = (tokens_raw * vsol) // max(vtok + tokens_raw, 1)
    return max(0, gross * (10_000 - fee_bps) // 10_000)


@dataclass
class V108ProfitResult:
    passed: bool
    reason: str
    size_lamports: int
    our_tokens_raw: int
    external_tokens_raw: int
    sell_after_external_lamports: int
    bundle_profit_lamports: int
    min_required_lamports: int
    jito_tip_lamports: int
    tip_fraction: float
    components: dict[str, int]


def evaluate_bundle_profit(
    *,
    mint: str,
    vsol_lamports: int,
    vtok_raw: int,
    external_sol_lamports: int,
    size_lamports: int,
    jito_tip_lamports: int = 1_000,
    min_profit_lamports: int = 30_000,
    tip_fraction_cap: float = 0.20,
    projection_buffer_lamports: int = 30_000,
    priority_fee_lamports_per_tx: int = 5_000,
    base_fee_lamports_per_tx: int = 5_000,
    tx_count: int = 4,
) -> V108ProfitResult:
    our_tokens = quote_buy_tokens_raw(size_lamports, vsol_lamports, vtok_raw)
    vsol_after_our = vsol_lamports + size_lamports
    vtok_after_our = max(1, vtok_raw - our_tokens)
    external_tokens = quote_buy_tokens_raw(external_sol_lamports, vsol_after_our, vtok_after_our)
    vsol_after_external = vsol_after_our + external_sol_lamports
    vtok_after_external = max(1, vtok_after_our - external_tokens)
    sell_out = quote_sell_sol_lamports(our_tokens, vsol_after_external, vtok_after_external)

    buy_commit = size_lamports + ATA_RENT_LAMPORTS
    rent_recovered = ATA_RENT_LAMPORTS
    fee_total = (
        priority_fee_lamports_per_tx * tx_count
        + base_fee_lamports_per_tx * tx_count
        + jito_tip_lamports
        + projection_buffer_lamports
    )
    profit = sell_out + rent_recovered - buy_commit - fee_total
    tip_fraction = (jito_tip_lamports / profit) if profit > 0 else 0.0
    reasons: list[str] = []
    if profit < min_profit_lamports:
        reasons.append(f"profit_below_floor:{profit}<{min_profit_lamports}")
    if profit > 0 and tip_fraction > tip_fraction_cap:
        reasons.append(f"tip_fraction_too_high:{tip_fraction:.4f}>{tip_fraction_cap:.4f}")
    if external_sol_lamports <= 0:
        reasons.append("external_buy_amount_unavailable")
    if our_tokens <= 0:
        reasons.append("our_buy_zero_tokens")
    passed = not reasons
    reason = ",".join(reasons) if reasons else "ok"
    components = {
        "vsol_lamports": int(vsol_lamports),
        "vtok_raw": int(vtok_raw),
        "external_sol_lamports": int(external_sol_lamports),
        "buy_commit_lamports": int(buy_commit),
        "rent_recovered_lamports": int(rent_recovered),
        "fee_total_lamports": int(fee_total),
        "projection_buffer_lamports": int(projection_buffer_lamports),
    }
    _log(
        f"PGG2-V108-BUNDLE-PROFIT-CHECK mint={mint[:4]}.. "
        f"size_lamports={size_lamports} external_sol_lamports={external_sol_lamports} "
        f"sell_after_external_lamports={sell_out} profit_lamports={profit:+} "
        f"min_required_lamports={min_profit_lamports} tip_lamports={jito_tip_lamports} "
        f"pass={int(passed)} reason={reason}"
    )
    _log(
        f"PGG2-V108-BUNDLE-PROFIT-{'PASS' if passed else 'BLOCK'} "
        f"mint={mint[:4]}.. size_lamports={size_lamports} profit_lamports={profit:+} reason={reason}"
    )
    return V108ProfitResult(
        passed=passed,
        reason=reason,
        size_lamports=int(size_lamports),
        our_tokens_raw=int(our_tokens),
        external_tokens_raw=int(external_tokens),
        sell_after_external_lamports=int(sell_out),
        bundle_profit_lamports=int(profit),
        min_required_lamports=int(min_profit_lamports),
        jito_tip_lamports=int(jito_tip_lamports),
        tip_fraction=float(tip_fraction),
        components=components,
    )


def select_best_size(
    *,
    mint: str,
    vsol_lamports: int,
    vtok_raw: int,
    external_sol_lamports: int,
    sizes_lamports: Iterable[int] = (1_000_000, 1_500_000, 2_000_000, 3_000_000, 5_000_000),
    jito_tip_lamports: int = 1_000,
) -> Optional[V108ProfitResult]:
    best: Optional[V108ProfitResult] = None
    for size in sizes_lamports:
        res = evaluate_bundle_profit(
            mint=mint,
            vsol_lamports=vsol_lamports,
            vtok_raw=vtok_raw,
            external_sol_lamports=external_sol_lamports,
            size_lamports=size,
            jito_tip_lamports=jito_tip_lamports,
        )
        if res.passed:
            best = res
    return best
