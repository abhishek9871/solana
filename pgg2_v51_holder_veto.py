"""V51 - Holder-quality + Token-2022 pre-entry veto evaluator.

Phase 3 of V51. Consumes a `features` dict from V51HolderQualityChecker and
a `rules` dict from data/v51_holder_quality_rules.json. Returns
(pass: bool, blockers: list[str]).

Veto rules (any fires -> block):
  - top1_pct > rules.top1_pct_max
  - top3_pct > rules.top3_pct_max
  - top5_pct > rules.top5_pct_max
  - top10_pct > rules.top10_pct_max
  - holder_count_nonzero < rules.min_meaningful_holders
  - token_program == Token-2022 and rules.block_token_2022 is true
  - holder_check_age_ms > rules.max_holder_check_age_ms
  - features.ok is False and rules.block_on_check_unavailable is true

Static-grep clean: no sendTransaction patterns.
"""
from __future__ import annotations

import json
import os
import re as _re_self
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Static-grep self check -- forbidden send patterns must NOT appear.
_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _src):
        sys.stderr.write(
            f"V51-HOLDER-VETO-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


TOKEN_2022_PROGRAM_ID_STR = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


DEFAULT_RULES: Dict[str, Any] = {
    "version": "v51_v1",
    "thresholds": {
        "top1_pct_max": 18,
        "top3_pct_max": 35,
        "top5_pct_max": 50,
        "top10_pct_max": 65,
        "min_meaningful_holders": 4,
        "max_holder_check_age_ms": 5000,
        "block_token_2022": True,
        "block_on_check_unavailable": True,
    },
}


def load_rules(path: str = "") -> Dict[str, Any]:
    """Load V51 holder-veto rules JSON from disk. Returns DEFAULT_RULES on
    any failure (so the runner never crashes on a missing config file).
    """
    if not path:
        path = os.environ.get(
            "PGG2_V51_HOLDER_RULES_PATH",
            "/root/piggy/data/v51_holder_quality_rules.json",
        )
    try:
        text = Path(path).read_text(encoding="utf-8")
        rules = json.loads(text)
        # Soft-merge over defaults so any missing key falls back.
        merged = dict(DEFAULT_RULES)
        merged.update(rules or {})
        thr = dict(DEFAULT_RULES["thresholds"])
        thr.update(((rules or {}).get("thresholds") or {}))
        merged["thresholds"] = thr
        return merged
    except Exception as exc:
        _log(
            f"PGG2-V51-RULES-LOAD-WARN path={path} "
            f"using_defaults err={type(exc).__name__}:{exc}"
        )
        return dict(DEFAULT_RULES)


def evaluate_holder_veto(
    features: Dict[str, Any],
    rules: Dict[str, Any],
    *,
    token2022_whitelist: List[str] = None,
    mint: str = "",
) -> Tuple[bool, List[str]]:
    """Evaluate veto. Returns (passed, blockers).

    `features` is the dict returned by V51HolderQualityChecker.check_mint.
    `rules` is the loaded rules JSON dict.
    `token2022_whitelist` is the optional list of mints allowed past the
    Token-2022 gate (env: PGG2_V51_TOKEN2022_WHITELIST, comma-separated).
    """
    blockers: List[str] = []
    thr = dict((rules or {}).get("thresholds") or DEFAULT_RULES["thresholds"])
    wl = set(token2022_whitelist or [])

    # 1. RPC failure / check unavailable.
    if not bool(features.get("ok")):
        if bool(thr.get("block_on_check_unavailable", True)):
            blockers.append("v51_holder_check_unavailable")
        # Even if not blocking on unavailable, we still cannot evaluate the
        # other rules meaningfully — bail with the single blocker.
        _log_gate(features, blockers, mint, passed=not blockers)
        return (len(blockers) == 0), blockers

    # 2. Holder concentration thresholds.
    top1 = float(features.get("top1_pct") or 0.0)
    top3 = float(features.get("top3_pct") or 0.0)
    top5 = float(features.get("top5_pct") or 0.0)
    top10 = float(features.get("top10_pct") or 0.0)
    count = int(features.get("holder_count_nonzero") or 0)

    if top1 > float(thr.get("top1_pct_max", 18)):
        blockers.append("v51_top1_pct_gt_max")
    if top3 > float(thr.get("top3_pct_max", 35)):
        blockers.append("v51_top3_pct_gt_max")
    if top5 > float(thr.get("top5_pct_max", 50)):
        blockers.append("v51_top5_pct_gt_max")
    if top10 > float(thr.get("top10_pct_max", 65)):
        blockers.append("v51_top10_pct_gt_max")
    if count < int(thr.get("min_meaningful_holders", 4)):
        blockers.append("v51_lt_min_meaningful_holders")

    # 3. Holder check staleness.
    age = int(features.get("holder_check_age_ms") or 0)
    max_age = int(thr.get("max_holder_check_age_ms", 5000))
    if age > max_age:
        blockers.append("v51_holder_check_stale")

    # 4. Token-2022 block.
    token_program = str(features.get("token_program") or "")
    if bool(thr.get("block_token_2022", True)) and \
       token_program == TOKEN_2022_PROGRAM_ID_STR and \
       mint not in wl:
        blockers.append("v51_token_2022_blocked")

    passed = len(blockers) == 0
    _log_gate(features, blockers, mint, passed=passed)
    return passed, blockers


def _log_gate(
    features: Dict[str, Any], blockers: List[str], mint: str, *, passed: bool,
) -> None:
    _log(
        f"PGG2-V51-HOLDER-GATE mint={_short(mint)} "
        f"top1={float(features.get('top1_pct') or 0.0):.2f} "
        f"top3={float(features.get('top3_pct') or 0.0):.2f} "
        f"top5={float(features.get('top5_pct') or 0.0):.2f} "
        f"top10={float(features.get('top10_pct') or 0.0):.2f} "
        f"count={int(features.get('holder_count_nonzero') or 0)} "
        f"token_prog={features.get('token_program') or ''} "
        f"pass={str(bool(passed)).lower()} "
        f"blockers={','.join(blockers) if blockers else 'none'}"
    )


def parse_token2022_whitelist_env() -> List[str]:
    raw = os.environ.get("PGG2_V51_TOKEN2022_WHITELIST", "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()]


__all__ = [
    "evaluate_holder_veto",
    "load_rules",
    "parse_token2022_whitelist_env",
    "DEFAULT_RULES",
    "TOKEN_2022_PROGRAM_ID_STR",
]
