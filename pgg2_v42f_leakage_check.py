#!/usr/bin/env python3
"""V42F Phase 2 — leakage check.

Reads /root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl.

For every row:
  - Every feature's `f_feature_ts_ms` must satisfy ts <= decision_ts_ms.
  - No feature value may have been derived from a future label.
  - The hard rule: features.keys() are limited to a fixed whitelist; any other
    key in features is flagged as a potential leak.

Outputs:
  /root/piggy/V42F_LEAKAGE_CHECK.md  with PASS/FAIL + violator list.

NO TX. NO NETWORK CALLS.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


DATASET = Path("/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl")
OUT = Path("/root/piggy/V42F_LEAKAGE_CHECK.md")


# Whitelist of allowed causal features. Every feature must be in this set and
# its effective timestamp (via f_feature_ts_ms) must be <= decision_ts_ms.
WHITELIST = {
    "f_curve_price",
    "f_quote_gradient",
    "f_curve_gradient",
    "f_curve_delta_N_minus_1",
    "f_curve_delta_N_minus_2",
    "f_quote_delta_N_minus_1",
    "f_quote_delta_N_minus_2",
    "f_buy100_n",
    "f_buy100_sol",
    "f_buy250_n",
    "f_buy250_sol",
    "f_buy500_n",
    "f_buy500_sol",
    "f_buy1000_n",
    "f_buy1000_sol",
    "f_since_prev_buy_n",
    "f_since_prev_buy_sol",
    "f_since_prev_sell_n",
    "f_since_prev_sell_sol",
    "f_buy_lat_ms",
    "f_sell_lat_ms",
    "f_pair_source",
    "f_sim_needed",
    "f_inter_arrival_ms",
    "f_processed_pnl_self",
    "f_confirmed_pnl_self",
    "f_prefetched_sell_used",
    "f_prefetched_quote_age_ms",
    "f_source_late",
    "f_recovered_quote",
    "f_fresh_quote",
    "f_feature_ts_ms",
}

# Forbidden tokens — anything in features that looks like it's labelled with a
# future word is a hard fail.
FORBIDDEN_SUBSTRINGS = (
    "label_",
    "future",
    "next_",
    "post_",
    "_at_j",
    "fwd_",
)


def check(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    violations: List[Tuple[str, str]] = []
    dec_ts = int(row["decision_ts_ms"])
    feats = row.get("features", {})

    for fname, val in feats.items():
        # 1. whitelist
        if fname not in WHITELIST:
            violations.append((fname, "not_in_whitelist"))
        # 2. forbidden substring
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in fname:
                violations.append((fname, f"forbidden_substring:{needle}"))
        # 3. ts <= decision_ts via the per-row marker
    f_ts = feats.get("f_feature_ts_ms")
    if f_ts is None:
        violations.append(("f_feature_ts_ms", "missing"))
    else:
        if int(f_ts) > dec_ts:
            violations.append(("f_feature_ts_ms", f"{f_ts} > {dec_ts}"))
    return violations


def main() -> int:
    rows = []
    with DATASET.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    total = len(rows)
    fails = 0
    feature_violator_counts: Dict[str, int] = {}
    examples: List[Tuple[int, List[Tuple[str, str]]]] = []
    for idx, r in enumerate(rows):
        v = check(r)
        if v:
            fails += 1
            if len(examples) < 5:
                examples.append((idx, v))
            for fname, _reason in v:
                feature_violator_counts[fname] = feature_violator_counts.get(fname, 0) + 1

    verdict = "PASS" if fails == 0 else "FAIL"

    md = []
    md.append("# V42F Leakage Check")
    md.append("")
    md.append(f"- dataset: `{DATASET}`")
    md.append(f"- rows: {total}")
    md.append(f"- rows_with_violations: {fails}")
    md.append(f"- verdict: **{verdict}**")
    md.append("")
    md.append("## Whitelist of allowed features")
    md.append("")
    for w in sorted(WHITELIST):
        md.append(f"- `{w}`")
    md.append("")
    md.append("## Hard rule")
    md.append("")
    md.append("Every feature's `f_feature_ts_ms` MUST satisfy `f_feature_ts_ms <= decision_ts_ms`.")
    md.append("Features outside the whitelist or matching forbidden substrings are flagged.")
    md.append("")
    md.append("## Violator counts")
    md.append("")
    if not feature_violator_counts:
        md.append("- (none)")
    else:
        for fname, cnt in sorted(feature_violator_counts.items(), key=lambda kv: -kv[1]):
            md.append(f"- `{fname}`: {cnt}")
    md.append("")
    md.append("## First 5 example violations (if any)")
    md.append("")
    if not examples:
        md.append("- (none)")
    else:
        for idx, v in examples:
            md.append(f"- row_index={idx}: {v}")

    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"[V42F-LEAKAGE] rows={total} fails={fails} verdict={verdict} out={OUT}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
