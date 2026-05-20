"""V59 empirical slippage model.

Returns a conservative per-leg + round-trip slippage budget based on trade size.
Built from V58 Stage A forensic (n=3, all sized at 0.005-0.015) which showed
buy slippage of ~1% consistently. Sample is too small for p95 calibration, so
the spec-mandated conservative fallback is used.

If/when more samples accumulate, this module can ingest live-fill data and
compute empirical p50/p90/p95 slippage per size tier.

Log:
  PGG2-V59-SLIPPAGE-MODEL size=.. budget=.. tier=.. samples=.. mode=..
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class SlippageBudget:
    size_sol: float
    tier: str
    budget_sol: float       # total slippage budget (covers both legs combined)
    samples: int
    mode: str               # "fallback" or "empirical"


class V59SlippageModel:
    """Conservative slippage budget per V59 spec fallback tier ladder.

    Source: V58 Stage A forensic (n=3) showed ~1% buy slippage. Spec mandates
    conservative fallback when sample is small. Empirical p95 not used until
    n>=20 per tier.
    """
    def __init__(self) -> None:
        # Per V59 spec defaults (env-overridable):
        self.budget_005 = _envf("PGG2_V59_SLIPPAGE_BUDGET_LE_005_SOL", 0.000700)
        self.budget_010 = _envf("PGG2_V59_SLIPPAGE_BUDGET_LE_010_SOL", 0.001000)
        self.budget_015 = _envf("PGG2_V59_SLIPPAGE_BUDGET_LE_015_SOL", 0.001500)
        # size > 0.015 disabled by returning very large budget
        self.budget_gt_015 = _envf("PGG2_V59_SLIPPAGE_BUDGET_GT_015_SOL", 1.000000)
        # Sample registry for future empirical calibration (Phase 2 extension)
        self._samples: dict[str, list[float]] = {
            "le_005": [],
            "le_010": [],
            "le_015": [],
            "gt_015": [],
        }

    def _tier(self, size_sol: float) -> str:
        if size_sol <= 0.0055:
            return "le_005"
        if size_sol <= 0.0105:
            return "le_010"
        if size_sol <= 0.0155:
            return "le_015"
        return "gt_015"

    def get_budget(self, size_sol: float) -> SlippageBudget:
        tier = self._tier(size_sol)
        budget_map = {
            "le_005": self.budget_005,
            "le_010": self.budget_010,
            "le_015": self.budget_015,
            "gt_015": self.budget_gt_015,
        }
        budget = budget_map[tier]
        samples = len(self._samples[tier])
        mode = "empirical" if samples >= 20 else "fallback"
        return SlippageBudget(
            size_sol=size_sol, tier=tier, budget_sol=budget,
            samples=samples, mode=mode,
        )

    def record_sample(self, size_sol: float, observed_slippage_sol: float) -> None:
        tier = self._tier(size_sol)
        self._samples[tier].append(float(observed_slippage_sol))

    def format_log_line(self, b: SlippageBudget) -> str:
        return (
            f"PGG2-V59-SLIPPAGE-MODEL size={b.size_sol:.4f} "
            f"tier={b.tier} budget={b.budget_sol:.6f} "
            f"samples={b.samples} mode={b.mode}"
        )


_SINGLETON: Optional[V59SlippageModel] = None


def get_model() -> V59SlippageModel:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V59SlippageModel()
    return _SINGLETON


if __name__ == "__main__":
    m = get_model()
    print("=== V59 Slippage Model — fallback budgets ===")
    for sz in (0.005, 0.010, 0.015, 0.020):
        b = m.get_budget(sz)
        print(m.format_log_line(b))
