#!/usr/bin/env python3
"""V223 no-wallet PumpSwap multi-pool atomic evaluator.

Uses getProgramAccounts to find same-mint PumpSwap pools, then evaluates
current reserve dislocations. No wallet, no signing, no sends.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey

from pgg2_v219_atomic_route_dislocation_scanner import (
    LAMPORTS_PER_SOL,
    PUMP_AMM_PROGRAM,
    WSOL_MINT,
    Pool,
    edge_lamports,
    load_env,
    log,
    parse_pumpswap_fees,
    pumpswap_buy_tokens,
    pumpswap_sell_lamports,
    rpc_url,
    short,
)


def rpc_call(method: str, params: list[Any], timeout: float = 45.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = ""
    for attempt in range(8):
        req = urllib.request.Request(rpc_url(), data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            if out.get("error"):
                msg = str(out["error"])[:400]
                if "429" in msg or "rate" in msg.lower():
                    last = msg
                    time.sleep(min(2.0, 0.15 * (attempt + 1)))
                    continue
                raise RuntimeError(msg)
            return out.get("result")
        except urllib.error.HTTPError as exc:
            last = f"HTTPError:{exc.code}:{exc.reason}"
            if exc.code == 429:
                time.sleep(min(2.5, 0.2 * (attempt + 1)))
                continue
            raise
    raise RuntimeError(f"rpc_call_rate_limited method={method} last={last}")


def parse_pool_from_row(row: dict[str, Any]) -> Pool | None:
    data = (row.get("account") or {}).get("data") or []
    if isinstance(data, list):
        data = data[0]
    raw = base64.b64decode(data)
    if len(raw) < 243:
        return None
    return Pool(
        key=str(row.get("pubkey") or ""),
        creator=str(Pubkey.from_bytes(raw[11:43])),
        base_mint=str(Pubkey.from_bytes(raw[43:75])),
        quote_mint=str(Pubkey.from_bytes(raw[75:107])),
        base_token_account=str(Pubkey.from_bytes(raw[139:171])),
        quote_token_account=str(Pubkey.from_bytes(raw[171:203])),
        coin_creator=str(Pubkey.from_bytes(raw[211:243])),
        is_mayhem=bool(raw[243]) if len(raw) > 243 else False,
    )


def token_balance_from_account(row: dict[str, Any] | None) -> int:
    if not row:
        return 0
    data = row.get("data") or []
    if isinstance(data, list):
        data = data[0]
    if not isinstance(data, str):
        return 0
    raw = base64.b64decode(data)
    return int.from_bytes(raw[64:72], "little") if len(raw) >= 72 else 0


def chunks(xs: list[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-mints", type=int, default=int(os.environ.get("V223_MAX_MINTS", "250")))
    ap.add_argument("--sizes-sol", default=os.environ.get("V223_SIZES_SOL", "0.001,0.0015,0.002,0.003,0.005"))
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V223_FEE_BUFFER_LAMPORTS", "90000")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V223_PROJECTION_BUFFER_LAMPORTS", "30000")))
    ap.add_argument("--min-edge-lamports", type=int, default=int(os.environ.get("V223_MIN_EDGE_LAMPORTS", "30000")))
    ap.add_argument("--min-quote-reserve-lamports", type=int, default=int(os.environ.get("V223_MIN_QUOTE_RESERVE_LAMPORTS", "5000000")))
    ap.add_argument("--out-jsonl", default=os.environ.get("V223_OUT_JSONL", "/root/piggy/data/v223_gpa_multipool_eval.jsonl"))
    args = ap.parse_args()

    started = time.time()
    sizes = [int(float(x) * LAMPORTS_PER_SOL) for x in args.sizes_sol.split(",") if x.strip()]
    fees = parse_pumpswap_fees()
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log("PGG2-V223-GPA-START program=pumpswap quote=wsol")
    rows = rpc_call(
        "getProgramAccounts",
        [
            str(PUMP_AMM_PROGRAM),
            {
                "encoding": "base64",
                "commitment": "processed",
                "filters": [{"memcmp": {"offset": 75, "bytes": str(WSOL_MINT)}}],
                "dataSlice": {"offset": 0, "length": 244},
            },
        ],
        timeout=60.0,
    ) or []
    by: dict[str, list[Pool]] = collections.defaultdict(list)
    for row in rows:
        pool = parse_pool_from_row(row)
        if not pool or pool.quote_mint != str(WSOL_MINT):
            continue
        by[pool.base_mint].append(pool)
    multi = [(m, ps) for m, ps in by.items() if len(ps) > 1]
    multi.sort(key=lambda kv: len(kv[1]), reverse=True)
    selected = multi[: int(args.max_mints)]
    selected_pools = [p for _m, ps in selected for p in ps]
    acct_list: list[str] = []
    for p in selected_pools:
        acct_list.extend([p.base_token_account, p.quote_token_account])
    uniq_accts = list(dict.fromkeys(acct_list))
    log(
        f"PGG2-V223-GPA-LOADED pools={len(rows)} unique_mints={len(by)} multi_mints={len(multi)} "
        f"selected_mints={len(selected)} selected_pools={len(selected_pools)} token_accounts={len(uniq_accts)}"
    )

    balances: dict[str, int] = {}
    for batch in chunks(uniq_accts, int(os.environ.get("V223_GETMULTIPLE_BATCH", "75") or 75)):
        res = rpc_call(
            "getMultipleAccounts",
            [batch, {"encoding": "base64", "commitment": "processed"}],
            timeout=20.0,
        )
        vals = (res or {}).get("value") or []
        for acct, val in zip(batch, vals):
            balances[acct] = token_balance_from_account(val)
        time.sleep(float(os.environ.get("V223_BATCH_SLEEP_SEC", "0.035") or 0.035))

    for p in selected_pools:
        p.base_reserve = int(balances.get(p.base_token_account, 0))
        p.quote_reserve = int(balances.get(p.quote_token_account, 0))

    checks = 0
    passes = 0
    best_global: dict[str, Any] | None = None
    with out_path.open("a", encoding="utf-8") as fh:
        for mint, pools in selected:
            usable = [
                p
                for p in pools
                if p.base_reserve > 0 and p.quote_reserve >= int(args.min_quote_reserve_lamports)
            ]
            if len(usable) < 2:
                continue
            for buy_pool in usable:
                for sell_pool in usable:
                    if buy_pool.key == sell_pool.key:
                        continue
                    for size in sizes:
                        tokens = pumpswap_buy_tokens(size, buy_pool, fees)
                        sell_out = pumpswap_sell_lamports(tokens, sell_pool, fees)
                        edge = edge_lamports(
                            sell_out,
                            size,
                            int(args.fee_buffer_lamports),
                            int(args.projection_buffer_lamports),
                        )
                        checks += 1
                        row = {
                            "kind": "v223_pumpswap_multipool_eval",
                            "ts_ms": int(time.time() * 1000),
                            "mint": mint,
                            "buy_pool": buy_pool.key,
                            "sell_pool": sell_pool.key,
                            "size_lamports": size,
                            "tokens_raw": tokens,
                            "sell_out_lamports": sell_out,
                            "edge_lamports": edge,
                            "buy_pool_base": buy_pool.base_reserve,
                            "buy_pool_quote": buy_pool.quote_reserve,
                            "sell_pool_base": sell_pool.base_reserve,
                            "sell_pool_quote": sell_pool.quote_reserve,
                            "passed": bool(edge >= int(args.min_edge_lamports)),
                        }
                        if best_global is None or edge > int(best_global["edge_lamports"]):
                            best_global = row
                        if edge >= int(args.min_edge_lamports):
                            passes += 1
                            fh.write(json.dumps(row, sort_keys=True) + "\n")
                            log(
                                f"PGG2-V223-MULTIPOOL-PASS mint={short(mint)} "
                                f"buy_pool={short(buy_pool.key)} sell_pool={short(sell_pool.key)} "
                                f"size={size/LAMPORTS_PER_SOL:.4f} edge_lamports={edge:+}"
                            )

    if best_global:
        log(
            f"PGG2-V223-BEST mint={short(str(best_global['mint']))} "
            f"buy_pool={short(str(best_global['buy_pool']))} sell_pool={short(str(best_global['sell_pool']))} "
            f"size={int(best_global['size_lamports'])/LAMPORTS_PER_SOL:.4f} "
            f"edge_lamports={int(best_global['edge_lamports']):+}"
        )
    log(
        f"PGG2-V223-FINAL checks={checks} passes={passes} elapsed_ms={int((time.time()-started)*1000)} "
        f"out_jsonl={out_path}"
    )
    return 0 if passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
