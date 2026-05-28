#!/usr/bin/env python3
"""V223 no-wallet PumpSwap multi-pool atomic evaluator.

Uses getProgramAccounts to find same-mint PumpSwap pools, then evaluates
current reserve dislocations. No wallet, no signing, no sends.
"""
from __future__ import annotations

import argparse
import base64
import collections
import http.client
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
    PUMP_PROGRAM,
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


PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
PUMP_AMM_FEE_CONFIG = Pubkey.find_program_address(
    [b"fee_config", bytes(PUMP_AMM_PROGRAM)], PUMP_FEE_PROGRAM
)[0]
DEFAULT_PUBKEY = str(Pubkey.default())


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
        except http.client.IncompleteRead as exc:
            last = f"IncompleteRead:{len(exc.partial)}"
            time.sleep(min(2.0, 0.2 * (attempt + 1)))
            continue
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
    raw = account_data_from_value(row)
    return int.from_bytes(raw[64:72], "little") if len(raw) >= 72 else 0


def account_data_from_value(row: dict[str, Any] | None) -> bytes:
    if not row:
        return b""
    data = row.get("data") or []
    if isinstance(data, list):
        data = data[0]
    if not isinstance(data, str):
        return b""
    return base64.b64decode(data)


def ceil_div(a: int, b: int) -> int:
    return (int(a) + int(b) - 1) // int(b)


def fees_from_config_data(data: bytes) -> tuple[tuple[int, int, int], list[tuple[int, tuple[int, int, int]]]]:
    if len(data) < 69:
        raise RuntimeError(f"fee_config_too_short len={len(data)}")
    off = 8
    off += 1  # bump
    off += 32  # admin

    def take_u64() -> int:
        nonlocal off
        v = int.from_bytes(data[off:off + 8], "little")
        off += 8
        return v

    def take_u128() -> int:
        nonlocal off
        v = int.from_bytes(data[off:off + 16], "little")
        off += 16
        return v

    flat = (take_u64(), take_u64(), take_u64())
    n = int.from_bytes(data[off:off + 4], "little")
    off += 4
    tiers: list[tuple[int, tuple[int, int, int]]] = []
    for _ in range(max(0, min(n, 128))):
        if off + 40 > len(data):
            break
        threshold = take_u128()
        fees = (take_u64(), take_u64(), take_u64())
        tiers.append((threshold, fees))
    return flat, tiers


def effective_pumpswap_fees(
    *,
    mint: str,
    pool: Pool,
    mint_supply: int,
    flat_fees: tuple[int, int, int],
    tiers: list[tuple[int, tuple[int, int, int]]],
    fallback: Any,
) -> tuple[int, int, int]:
    try:
        mint_pk = Pubkey.from_string(mint)
        pool_authority = Pubkey.find_program_address([b"pool-authority", bytes(mint_pk)], PUMP_PROGRAM)[0]
        if str(pool.creator) != str(pool_authority):
            fees = flat_fees
        elif tiers:
            market_cap = int(pool.quote_reserve) * int(mint_supply) // max(int(pool.base_reserve), 1)
            fees = tiers[0][1]
            for threshold, tier_fees in reversed(tiers):
                if market_cap >= int(threshold):
                    fees = tier_fees
                    break
        else:
            fees = flat_fees
        lp_bps, protocol_bps, creator_bps = (int(fees[0]), int(fees[1]), int(fees[2]))
        if str(pool.coin_creator) == DEFAULT_PUBKEY:
            creator_bps = 0
        return lp_bps, protocol_bps, creator_bps
    except Exception:
        creator_bps = 0 if str(pool.coin_creator) == DEFAULT_PUBKEY else int(fallback.creator_fee_bps)
        return int(fallback.lp_fee_bps), int(fallback.protocol_fee_bps), int(creator_bps)


def pumpswap_buy_tokens_current(size: int, pool: Pool, fees: tuple[int, int, int]) -> int:
    total = int(fees[0]) + int(fees[1]) + int(fees[2])
    effective_quote = int(size) * 10_000 // max(10_000 + total, 1)
    fee = (
        ceil_div(effective_quote * int(fees[0]), 10_000)
        + ceil_div(effective_quote * int(fees[1]), 10_000)
        + ceil_div(effective_quote * int(fees[2]), 10_000)
    )
    total_with_fees = int(effective_quote) + int(fee)
    if total_with_fees > int(size):
        effective_quote = max(0, int(effective_quote) - (total_with_fees - int(size)))
    input_amount = max(0, int(effective_quote) - 1)
    return max(0, int(input_amount) * int(pool.base_reserve) // max(int(pool.quote_reserve) + int(input_amount), 1))


def pumpswap_sell_lamports_current(tokens: int, pool: Pool, fees: tuple[int, int, int]) -> int:
    gross = int(tokens) * int(pool.quote_reserve) // max(int(pool.base_reserve) + int(tokens), 1)
    fee = (
        ceil_div(gross * int(fees[0]), 10_000)
        + ceil_div(gross * int(fees[1]), 10_000)
        + ceil_div(gross * int(fees[2]), 10_000)
    )
    return max(0, int(gross) - int(fee))


def chunks(xs: list[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def parse_sizes_sol(raw: str) -> list[int]:
    return [int(float(x) * LAMPORTS_PER_SOL) for x in raw.split(",") if x.strip()]


def route_edge_for_size(
    *,
    size: int,
    buy_pool: Pool,
    sell_pool: Pool,
    buy_fees: tuple[int, int, int],
    sell_fees: tuple[int, int, int],
    fee_buffer_lamports: int,
    projection_buffer_lamports: int,
) -> tuple[int, int, int]:
    tokens = pumpswap_buy_tokens_current(size, buy_pool, buy_fees)
    sell_out = pumpswap_sell_lamports_current(tokens, sell_pool, sell_fees)
    edge = edge_lamports(sell_out, size, fee_buffer_lamports, projection_buffer_lamports)
    return tokens, sell_out, edge


def optimized_route_sizes(
    *,
    base_sizes: list[int],
    buy_pool: Pool,
    sell_pool: Pool,
    buy_fees: tuple[int, int, int],
    sell_fees: tuple[int, int, int],
    fee_buffer_lamports: int,
    projection_buffer_lamports: int,
    auto_min_size: int,
    auto_max_size: int,
    neighborhood_bps: list[int],
) -> list[int]:
    sizes = {int(x) for x in base_sizes if int(x) > 0}
    lo = max(1, int(auto_min_size))
    hi = max(lo, int(auto_max_size))
    if hi <= lo:
        return sorted(sizes)

    def score(amount: int) -> int:
        return route_edge_for_size(
            size=max(1, int(amount)),
            buy_pool=buy_pool,
            sell_pool=sell_pool,
            buy_fees=buy_fees,
            sell_fees=sell_fees,
            fee_buffer_lamports=fee_buffer_lamports,
            projection_buffer_lamports=projection_buffer_lamports,
        )[2]

    left, right = lo, hi
    # PumpSwap two-pool arbitrage is effectively unimodal over useful sizes.
    # Ternary search finds the peak instead of relying on a lucky static grid.
    while right - left > 24:
        m1 = left + (right - left) // 3
        m2 = right - (right - left) // 3
        if score(m1) < score(m2):
            left = m1 + 1
        else:
            right = m2 - 1

    best = max(range(left, right + 1), key=score)
    sizes.add(best)
    for bps in neighborhood_bps:
        delta = max(1, best * int(bps) // 10_000)
        sizes.add(max(lo, best - delta))
        sizes.add(min(hi, best + delta))
    return sorted(s for s in sizes if lo <= s <= hi)


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-mints", type=int, default=int(os.environ.get("V223_MAX_MINTS", "250")))
    ap.add_argument("--sizes-sol", default=os.environ.get("V223_SIZES_SOL", "0.001,0.0015,0.002,0.003,0.005"))
    ap.add_argument("--auto-optimize-sizes", type=int, default=int(os.environ.get("V223_AUTO_OPTIMIZE_SIZES", "0")))
    ap.add_argument("--auto-min-size-sol", default=os.environ.get("V223_AUTO_MIN_SIZE_SOL", "0.00005"))
    ap.add_argument("--auto-max-size-sol", default=os.environ.get("V223_AUTO_MAX_SIZE_SOL", ""))
    ap.add_argument("--auto-neighborhood-bps", default=os.environ.get("V223_AUTO_NEIGHBORHOOD_BPS", "25,50,100,200,500,1000"))
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V223_FEE_BUFFER_LAMPORTS", "90000")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V223_PROJECTION_BUFFER_LAMPORTS", "30000")))
    ap.add_argument("--min-edge-lamports", type=int, default=int(os.environ.get("V223_MIN_EDGE_LAMPORTS", "30000")))
    ap.add_argument("--min-quote-reserve-lamports", type=int, default=int(os.environ.get("V223_MIN_QUOTE_RESERVE_LAMPORTS", "5000000")))
    ap.add_argument("--out-jsonl", default=os.environ.get("V223_OUT_JSONL", "/root/piggy/data/v223_gpa_multipool_eval.jsonl"))
    args = ap.parse_args()

    started = time.time()
    sizes = parse_sizes_sol(args.sizes_sol)
    auto_min_size = int(float(args.auto_min_size_sol) * LAMPORTS_PER_SOL)
    auto_max_size = (
        int(float(args.auto_max_size_sol) * LAMPORTS_PER_SOL)
        if str(args.auto_max_size_sol).strip()
        else max(sizes or [1])
    )
    auto_neighborhood_bps = [
        int(x) for x in str(args.auto_neighborhood_bps).split(",") if str(x).strip()
    ]
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

    mint_supply: dict[str, int] = {}
    for batch in chunks([m for m, _ps in selected], int(os.environ.get("V223_GETMULTIPLE_BATCH", "75") or 75)):
        res = rpc_call(
            "getMultipleAccounts",
            [batch, {"encoding": "base64", "commitment": "processed"}],
            timeout=20.0,
        )
        vals = (res or {}).get("value") or []
        for mint, val in zip(batch, vals):
            raw = account_data_from_value(val)
            mint_supply[mint] = int.from_bytes(raw[36:44], "little") if len(raw) >= 44 else 0
        time.sleep(float(os.environ.get("V223_BATCH_SLEEP_SEC", "0.035") or 0.035))

    fee_config_ok = False
    flat_fees = (int(fees.lp_fee_bps), int(fees.protocol_fee_bps), int(fees.creator_fee_bps))
    fee_tiers: list[tuple[int, tuple[int, int, int]]] = []
    try:
        res = rpc_call(
            "getAccountInfo",
            [str(PUMP_AMM_FEE_CONFIG), {"encoding": "base64", "commitment": "processed"}],
            timeout=8.0,
        )
        raw = account_data_from_value((res or {}).get("value"))
        flat_fees, fee_tiers = fees_from_config_data(raw)
        fee_config_ok = True
        log(
            f"PGG2-V223-FEE-CONFIG ok=1 flat={flat_fees[0]},{flat_fees[1]},{flat_fees[2]} "
            f"tiers={len(fee_tiers)}"
        )
    except Exception as exc:
        log(
            f"PGG2-V223-FEE-CONFIG ok=0 fallback={fees.lp_fee_bps},{fees.protocol_fee_bps},{fees.creator_fee_bps} "
            f"err={type(exc).__name__}:{str(exc)[:80]}"
        )

    pool_fees: dict[str, tuple[int, int, int]] = {}
    for mint, pools in selected:
        supply = int(mint_supply.get(mint, 0))
        for pool in pools:
            pool_fees[pool.key] = effective_pumpswap_fees(
                mint=mint,
                pool=pool,
                mint_supply=supply,
                flat_fees=flat_fees,
                tiers=fee_tiers if fee_config_ok else [],
                fallback=fees,
            )

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
                    buy_fees = pool_fees.get(
                        buy_pool.key,
                        (int(fees.lp_fee_bps), int(fees.protocol_fee_bps), int(fees.creator_fee_bps)),
                    )
                    sell_fees = pool_fees.get(
                        sell_pool.key,
                        (int(fees.lp_fee_bps), int(fees.protocol_fee_bps), int(fees.creator_fee_bps)),
                    )
                    pair_sizes = (
                        optimized_route_sizes(
                            base_sizes=sizes,
                            buy_pool=buy_pool,
                            sell_pool=sell_pool,
                            buy_fees=buy_fees,
                            sell_fees=sell_fees,
                            fee_buffer_lamports=int(args.fee_buffer_lamports),
                            projection_buffer_lamports=int(args.projection_buffer_lamports),
                            auto_min_size=auto_min_size,
                            auto_max_size=auto_max_size,
                            neighborhood_bps=auto_neighborhood_bps,
                        )
                        if int(args.auto_optimize_sizes)
                        else sizes
                    )
                    for size in pair_sizes:
                        tokens, sell_out, edge = route_edge_for_size(
                            size=size,
                            buy_pool=buy_pool,
                            sell_pool=sell_pool,
                            buy_fees=buy_fees,
                            sell_fees=sell_fees,
                            fee_buffer_lamports=int(args.fee_buffer_lamports),
                            projection_buffer_lamports=int(args.projection_buffer_lamports),
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
                            "buy_fee_bps": sum(int(x) for x in buy_fees),
                            "sell_fee_bps": sum(int(x) for x in sell_fees),
                            "fee_config_ok": bool(fee_config_ok),
                            "auto_optimized_sizes": bool(int(args.auto_optimize_sizes)),
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
