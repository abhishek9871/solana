"""V51 replay - run holder-quality + Token-2022 check against known V50A/B/C mints.

Phase 5 of V51. For each known mint from the four prior live trades:
  - V50A loser:  GXaRd5F1RUUTPDvDeFppTP31u9Dx4UkBsJm7Lz2Fpump  (lost SOL)
  - V50B winner: 61Ph76cbGL2hMidG1x5fW37DXpJAQ3XEq3psNGwHpump (won SOL)
  - V50C stuck:  9Cc2QxvPBKJi1GQBQb7ezUCVaFCewUxp6Fd8FDjopump (Token-2022, stuck)

Optional fourth: JBdV (off-session) - found only as a fragment in tx
signature 3MmGXLW...JBdVA2s..., not a mint. We document this as
"no full mint recoverable from logs" and skip.

For each mint:
  1. Call V51HolderQualityChecker.check_mint(mint)
  2. Call V51Token2022Checker.owner_program(mint) as cross-check
  3. evaluate_holder_veto(features, rules)
  4. Report features + veto result + per-blocker breakdown

Output: /root/piggy/V51_HOLDER_REPLAY_REPORT.md

Pass condition: V50A and V50C losers blocked; V50B winner ideally preserved.
Static-grep clean.
"""
from __future__ import annotations

import asyncio
import json
import os
import re as _re_self
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Static-grep self-check.
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
            f"V51-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)

# Load env so HELIUS_API_KEY is available.
def _load_env_file(path: str = "/root/piggy/.env") -> None:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_env_file()

sys.path.insert(0, "/root/piggy")

from pgg2_v51_holder_quality import (  # noqa: E402
    V51HolderQualityChecker,
    V51Token2022Checker,
    TOKEN_2022_PROGRAM_ID_STR,
)
from pgg2_v51_holder_veto import (  # noqa: E402
    evaluate_holder_veto,
    load_rules,
    parse_token2022_whitelist_env,
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# Known mints from prior live trades.
KNOWN_MINTS: List[Dict[str, Any]] = [
    {
        "mint": "GXaRd5F1RUUTPDvDeFppTP31u9Dx4UkBsJm7Lz2Fpump",
        "label": "V50A loser",
        "v50_outcome": "buy+sell confirmed, NEGATIVE PnL",
        "v50_pnl_sol": -0.005237,
        "expected_v51_block": True,
        "expected_block_reason": "high holder concentration OR check fail",
    },
    {
        "mint": "61Ph76cbGL2hMidG1x5fW37DXpJAQ3XEq3psNGwHpump",
        "label": "V50B winner",
        "v50_outcome": "buy+sell confirmed, +0.000525983 SOL",
        "v50_pnl_sol": +0.000525983,
        "expected_v51_block": False,
        "expected_block_reason": "should pass (clean win)",
    },
    {
        "mint": "9Cc2QxvPBKJi1GQBQb7ezUCVaFCewUxp6Fd8FDjopump",
        "label": "V50C stuck Token-2022",
        "v50_outcome": "Token-2022, stuck with residual 77987 tokens, "
                       "-0.007 SOL drawdown",
        "v50_pnl_sol": -0.007129080,
        "expected_v51_block": True,
        "expected_block_reason": "Token-2022 (rules.block_token_2022=true)",
    },
]


async def replay_mint(
    mint_info: Dict[str, Any],
    holder_checker: V51HolderQualityChecker,
    t22_checker: V51Token2022Checker,
    rules: Dict[str, Any],
    t22_whitelist: List[str],
) -> Dict[str, Any]:
    mint = str(mint_info["mint"])
    _log(f"PGG2-V51-REPLAY-START mint={mint} label={mint_info['label']}")

    # Holder check (includes mint owner / token_program).
    features = await holder_checker.check_mint(mint)
    # Cross-check Token-2022 via dedicated checker (separate RPC call).
    owner_via_t22 = await t22_checker.owner_program(mint)

    # If owner discrepancy, the holder_check is likely the source of truth.
    if owner_via_t22 and features.get("token_program") \
            and owner_via_t22 != features.get("token_program"):
        _log(
            f"PGG2-V51-REPLAY-WARN mint={mint} "
            f"token_program_mismatch holder={features.get('token_program')} "
            f"t22_check={owner_via_t22}"
        )
        # Prefer the t22-check's freshest read.
        features["token_program"] = owner_via_t22

    # Evaluate veto.
    passed, blockers = evaluate_holder_veto(
        features, rules, token2022_whitelist=t22_whitelist, mint=mint,
    )

    out = {
        "mint": mint,
        "label": mint_info["label"],
        "v50_outcome": mint_info["v50_outcome"],
        "v50_pnl_sol": mint_info["v50_pnl_sol"],
        "expected_v51_block": bool(mint_info["expected_v51_block"]),
        "expected_block_reason": str(mint_info["expected_block_reason"]),
        "features": features,
        "v51_passed": bool(passed),
        "v51_blocked": not bool(passed),
        "v51_blockers": list(blockers),
    }
    _log(
        f"PGG2-V51-REPLAY-RESULT mint={mint} "
        f"label={mint_info['label']} "
        f"v51_passed={passed} "
        f"blockers={','.join(blockers) if blockers else 'none'}"
    )
    return out


def write_report(results: List[Dict[str, Any]], out_path: str) -> str:
    lines: List[str] = []
    lines.append("# V51 Holder-Veto Replay Report\n\n")
    lines.append(
        f"Generated: `{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}`\n\n"
    )

    # Per-mint table.
    lines.append("## Per-Mint Results\n\n")
    lines.append(
        "| mint | label | V50 outcome | V50 PnL | "
        "top1% | top3% | top5% | top10% | count | token_prog | "
        "v51_passed | v51_blockers |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    for r in results:
        f = dict(r.get("features") or {})
        tp_short = str(f.get("token_program") or "")[:12]
        blockers_str = ",".join(r.get("v51_blockers") or []) or "none"
        lines.append(
            f"| `{r['mint'][:12]}` | {r['label']} | {r['v50_outcome'][:40]} | "
            f"{r['v50_pnl_sol']:+.9f} | "
            f"{float(f.get('top1_pct', 0.0)):.2f} | "
            f"{float(f.get('top3_pct', 0.0)):.2f} | "
            f"{float(f.get('top5_pct', 0.0)):.2f} | "
            f"{float(f.get('top10_pct', 0.0)):.2f} | "
            f"{int(f.get('holder_count_nonzero', 0))} | "
            f"`{tp_short}` | {r['v51_passed']} | {blockers_str} |\n"
        )
    lines.append("\n")

    # Hard outputs requested by the spec.
    lines.append("## Hard Outputs (spec-required)\n\n")
    for r in results:
        mint_short = r["mint"][:12]
        feats = r.get("features") or {}
        is_blocked = not bool(r.get("v51_passed"))
        which = ",".join(r.get("v51_blockers") or []) or "none"
        agree = (
            (r["expected_v51_block"] and is_blocked)
            or (not r["expected_v51_block"] and not is_blocked)
        )
        lines.append(
            f"- **{r['label']}** (`{mint_short}`): "
            f"V51 blocked = `{is_blocked}` "
            f"(veto: `{which}`); expected_block=`{r['expected_v51_block']}` "
            f"({r['expected_block_reason']}); "
            f"outcome_agrees_with_v50={agree}\n"
        )
        # Full feature dump per mint (for tuning visibility).
        lines.append(
            f"  - features: ok=`{feats.get('ok')}` "
            f"error=`{feats.get('error')}` "
            f"top1=`{float(feats.get('top1_pct',0)):.4f}` "
            f"top3=`{float(feats.get('top3_pct',0)):.4f}` "
            f"top5=`{float(feats.get('top5_pct',0)):.4f}` "
            f"top10=`{float(feats.get('top10_pct',0)):.4f}` "
            f"count=`{feats.get('holder_count_nonzero')}` "
            f"largest_raw=`{feats.get('largest_holder_amount_raw')}` "
            f"supply=`{feats.get('total_supply_raw')}` "
            f"token_program=`{feats.get('token_program')}`\n"
        )

    # Aggregates.
    losers_blocked = sum(
        1 for r in results
        if r["expected_v51_block"] and not r.get("v51_passed")
    )
    losers_total = sum(1 for r in results if r["expected_v51_block"])
    winners_preserved = sum(
        1 for r in results
        if not r["expected_v51_block"] and r.get("v51_passed")
    )
    winners_total = sum(1 for r in results if not r["expected_v51_block"])

    lines.append("\n## Aggregates\n\n")
    lines.append(f"- losers_blocked: `{losers_blocked}/{losers_total}`\n")
    lines.append(
        f"- winners_preserved: `{winners_preserved}/{winners_total}`\n"
    )

    pass_overall = (
        losers_blocked >= losers_total
    )  # ideally winners_preserved == winners_total too, but losers are critical

    lines.append("\n## Verdict\n\n")
    lines.append(
        f"- losers_blocked target: `{losers_total}` "
        f"actual: `{losers_blocked}` "
        f"-> `{'PASS' if losers_blocked >= losers_total else 'FAIL'}`\n"
    )
    lines.append(
        f"- winners_preserved target: `{winners_total}` "
        f"actual: `{winners_preserved}` "
        f"-> `{'PASS' if winners_preserved >= winners_total else 'WARN_TUNING_TIGHT'}`\n"
    )
    overall_verdict = "PASS" if pass_overall else "FAIL"
    if pass_overall and winners_preserved < winners_total:
        overall_verdict = "PASS (with tuning warning: V50B winner blocked too)"
    lines.append(
        f"\n### VERDICT: **{overall_verdict}**\n\n"
    )

    # Off-session JBdV note.
    lines.append("## Off-Session JBdV Note\n\n")
    lines.append(
        "The off-session JBdV identifier appears in /root/piggy/logs/ ONLY "
        "as a fragment of transaction signature "
        "`3MmGXLWvNR13b5Bc6RXtmUAvFS1bR5MjUNPnQVnB89ceYMWYVJBdVA2smvzbbGSR8UAsAFAvazQSmn3Fcojfovfc`. "
        "It is NOT a mint address. The associated mint is `36bt..pump` "
        "(see PGG2-LIVE-SELL log at 2026-05-08 01:02:56); the full mint "
        "address is no longer recoverable from preserved logs. We document "
        "this and skip the replay for this trade.\n\n"
    )

    Path(out_path).write_text("".join(lines), encoding="utf-8")
    json_path = Path(out_path).with_suffix(".json")
    json_path.write_text(json.dumps({
        "results": results,
        "losers_blocked": losers_blocked,
        "losers_total": losers_total,
        "winners_preserved": winners_preserved,
        "winners_total": winners_total,
        "verdict": overall_verdict,
        "generated_ts": int(time.time()),
    }, indent=2), encoding="utf-8")
    return out_path


async def amain() -> int:
    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        _log("PGG2-V51-REPLAY-ABORT HELIUS_API_KEY missing")
        return 2

    rules_path = os.environ.get(
        "PGG2_V51_HOLDER_RULES_PATH",
        "/root/piggy/data/v51_holder_quality_rules.json",
    )
    rules = load_rules(rules_path)
    t22_whitelist = parse_token2022_whitelist_env()

    holder_checker = V51HolderQualityChecker(
        helius_api_key=api_key,
        ttl_s=int(os.environ.get("PGG2_V51_HOLDER_TTL_S", "30") or 30),
        rate_limit_per_min=int(
            os.environ.get("PGG2_V51_HOLDER_RATE_PER_MIN", "60") or 60
        ),
    )
    t22_checker = V51Token2022Checker(
        helius_api_key=api_key,
        ttl_s=int(os.environ.get("PGG2_V51_HOLDER_TTL_S", "30") or 30),
    )

    results: List[Dict[str, Any]] = []
    for info in KNOWN_MINTS:
        r = await replay_mint(
            info, holder_checker, t22_checker, rules, t22_whitelist,
        )
        results.append(r)
        # tiny throttle between mints to be polite
        await asyncio.sleep(0.20)

    out_path = os.environ.get(
        "PGG2_V51_REPLAY_OUT_MD",
        "/root/piggy/V51_HOLDER_REPLAY_REPORT.md",
    )
    p = write_report(results, out_path)
    _log(f"PGG2-V51-REPLAY-DONE wrote={p}")
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except SystemExit as se:
        return int(se.code or 0)


if __name__ == "__main__":
    sys.exit(main())
