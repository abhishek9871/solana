#!/usr/bin/env python3
"""V270 event-driven PumpSwap multipool scanner.

This is the frozen V223/V255/V256 lane with a faster candidate source:
PumpSwap pool -> PumpSwap pool, same mint, atomic buy/sell/close. It does not
sign, simulate, or send. It only writes V223-compatible candidate rows that
V255 exact simulation already understands.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import os
import time
from pathlib import Path
from typing import Any

import websockets

from pgg2_v219_atomic_route_dislocation_scanner import (
    LAMPORTS_PER_SOL,
    PUMP_AMM_PROGRAM,
    WSOL_MINT,
    Pool,
    log,
    parse_pool,
    rpc_url,
    short,
)
from v223_gpa_multipool_eval import (
    PUMP_AMM_FEE_CONFIG,
    account_data_from_value,
    chunks,
    effective_pumpswap_fees,
    fees_from_config_data,
    optimized_route_sizes,
    parse_pumpswap_fees,
    parse_sizes_sol,
    route_edge_for_size,
    rpc_call,
)


def now_ms() -> int:
    return int(time.time() * 1000)


def ws_url() -> str:
    http = rpc_url()
    if http.startswith("https://"):
        return "wss://" + http[len("https://") :]
    if http.startswith("http://"):
        return "ws://" + http[len("http://") :]
    return http


def account_data_from_notification(value: dict[str, Any]) -> bytes:
    account = value.get("account") or value
    data_field = account.get("data") or []
    if isinstance(data_field, list):
        data_field = data_field[0]
    if not isinstance(data_field, str):
        raise RuntimeError("missing_base64_data")
    return base64.b64decode(data_field)


def token_balance_from_raw(data: bytes) -> int:
    return int.from_bytes(data[64:72], "little") if len(data) >= 72 else 0


def token_balance_from_value(value: dict[str, Any] | None) -> int:
    return token_balance_from_raw(account_data_from_value(value))


async def subscribe_account(ws: Any, *, req_id: int, pubkey: str) -> None:
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "accountSubscribe",
                "params": [pubkey, {"encoding": "base64", "commitment": "processed"}],
            },
            separators=(",", ":"),
        )
    )


def parse_pool_from_gpa(row: dict[str, Any]) -> Pool | None:
    data = (row.get("account") or {}).get("data") or []
    if isinstance(data, list):
        data = data[0]
    if not isinstance(data, str):
        return None
    return parse_pool(str(row.get("pubkey") or ""), base64.b64decode(data))


def load_pools(max_mints: int) -> tuple[dict[str, Pool], dict[str, list[str]]]:
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
    by_mint: dict[str, list[str]] = collections.defaultdict(list)
    pools: dict[str, Pool] = {}
    for row in rows:
        try:
            pool = parse_pool_from_gpa(row)
        except Exception:
            continue
        if not pool or pool.quote_mint != str(WSOL_MINT):
            continue
        pools[pool.key] = pool
        by_mint[pool.base_mint].append(pool.key)
    multi = [(m, ks) for m, ks in by_mint.items() if len(ks) > 1]
    multi.sort(key=lambda kv: len(kv[1]), reverse=True)
    keep_mints = {m for m, _ks in multi[: int(max_mints)]}
    pools = {k: p for k, p in pools.items() if p.base_mint in keep_mints}
    by_mint = collections.defaultdict(list)
    for key, pool in pools.items():
        by_mint[pool.base_mint].append(key)
    log(
        f"PGG2-V270-POOL-LOAD raw_pools={len(rows)} selected_mints={len(by_mint)} "
        f"selected_pools={len(pools)}"
    )
    return pools, dict(by_mint)


def load_pools_from_seed(seed_jsonl: str, max_rows: int) -> tuple[dict[str, Pool], dict[str, list[str]]]:
    rows: list[dict[str, Any]] = []
    path = Path(seed_jsonl)
    if not path.exists():
        raise RuntimeError(f"seed_jsonl_missing:{seed_jsonl}")
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: int(r.get("edge_lamports", 0)), reverse=True)
    keys: list[str] = []
    for row in rows[: max(1, int(max_rows))]:
        for field in ("buy_pool", "sell_pool"):
            val = str(row.get(field) or "")
            if val:
                keys.append(val)
    keys = list(dict.fromkeys(keys))
    pools: dict[str, Pool] = {}
    for batch in chunks(keys, 75):
        res = rpc_call("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "processed"}], timeout=20.0)
        vals = (res or {}).get("value") or []
        for key, val in zip(batch, vals):
            raw = account_data_from_value(val)
            if not raw:
                continue
            try:
                pool = parse_pool(key, raw)
            except Exception:
                continue
            if pool.quote_mint == str(WSOL_MINT):
                pools[pool.key] = pool
        time.sleep(0.05)
    by_mint: dict[str, list[str]] = collections.defaultdict(list)
    for key, pool in pools.items():
        by_mint[pool.base_mint].append(key)
    by_mint = {m: ks for m, ks in by_mint.items() if len(ks) > 1}
    pools = {k: p for m, ks in by_mint.items() for k, p in [(k, pools[k]) for k in ks]}
    log(
        f"PGG2-V270-SEED-LOAD rows={len(rows)} seed_rows={max_rows} "
        f"active_mints={len(by_mint)} active_pools={len(pools)}"
    )
    return pools, dict(by_mint)


def cap_pools_for_account_subs(
    pools: dict[str, Pool],
    by_mint: dict[str, list[str]],
    max_account_subs: int,
    max_pools_per_mint: int,
) -> tuple[dict[str, Pool], dict[str, list[str]]]:
    max_pools = max(2, int(max_account_subs) // 2)
    selected_keys: list[str] = []
    per_mint = max(2, int(max_pools_per_mint))
    for mint, keys in sorted(by_mint.items(), key=lambda kv: len(kv[1]), reverse=True):
        if len(keys) < 2:
            continue
        take = keys[: min(len(keys), per_mint, max_pools - len(selected_keys))]
        if len(take) < 2:
            continue
        selected_keys.extend(take)
        if len(selected_keys) >= max_pools:
            break
    keep = set(selected_keys[:max_pools])
    capped_pools = {k: p for k, p in pools.items() if k in keep}
    capped_by: dict[str, list[str]] = collections.defaultdict(list)
    for key, pool in capped_pools.items():
        capped_by[pool.base_mint].append(key)
    capped_by = {m: ks for m, ks in capped_by.items() if len(ks) > 1}
    capped_pools = {k: p for m, ks in capped_by.items() for k, p in [(k, capped_pools[k]) for k in ks]}
    log(
        f"PGG2-V270-POOL-CAP max_account_subs={max_account_subs} "
        f"max_pools_per_mint={max_pools_per_mint} active_mints={len(capped_by)} "
        f"active_pools={len(capped_pools)}"
    )
    return capped_pools, dict(capped_by)


def load_reserves(pools: dict[str, Pool], batch_size: int, sleep_sec: float) -> None:
    accounts: list[str] = []
    for pool in pools.values():
        accounts.extend([pool.base_token_account, pool.quote_token_account])
    for batch in chunks(list(dict.fromkeys(accounts)), int(batch_size)):
        res = rpc_call("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "processed"}], timeout=20.0)
        vals = (res or {}).get("value") or []
        balances = {acct: token_balance_from_value(val) for acct, val in zip(batch, vals)}
        for pool in pools.values():
            if pool.base_token_account in balances:
                pool.base_reserve = int(balances[pool.base_token_account])
            if pool.quote_token_account in balances:
                pool.quote_reserve = int(balances[pool.quote_token_account])
        time.sleep(float(sleep_sec))


def load_mint_supplies(mints: list[str], batch_size: int, sleep_sec: float) -> dict[str, int]:
    out: dict[str, int] = {}
    for batch in chunks(mints, int(batch_size)):
        res = rpc_call("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "processed"}], timeout=20.0)
        vals = (res or {}).get("value") or []
        for mint, val in zip(batch, vals):
            raw = account_data_from_value(val)
            out[mint] = int.from_bytes(raw[36:44], "little") if len(raw) >= 44 else 0
        time.sleep(float(sleep_sec))
    return out


def load_fee_config():
    fallback = parse_pumpswap_fees()
    flat = (int(fallback.lp_fee_bps), int(fallback.protocol_fee_bps), int(fallback.creator_fee_bps))
    tiers: list[tuple[int, tuple[int, int, int]]] = []
    ok = False
    try:
        res = rpc_call("getAccountInfo", [str(PUMP_AMM_FEE_CONFIG), {"encoding": "base64", "commitment": "processed"}], timeout=8.0)
        flat, tiers = fees_from_config_data(account_data_from_value((res or {}).get("value")))
        ok = True
    except Exception as exc:
        log(f"PGG2-V270-FEE-CONFIG ok=0 err={type(exc).__name__}:{str(exc)[:80]}")
    log(f"PGG2-V270-FEE-CONFIG ok={int(ok)} flat={flat[0]},{flat[1]},{flat[2]} tiers={len(tiers)}")
    return fallback, flat, tiers


async def run(args: argparse.Namespace) -> int:
    base_sizes = parse_sizes_sol(args.sizes_sol)
    auto_min_size = int(float(args.auto_min_size_sol) * LAMPORTS_PER_SOL)
    auto_max_size = int(float(args.auto_max_size_sol) * LAMPORTS_PER_SOL)
    neighborhood_bps = [int(x) for x in str(args.auto_neighborhood_bps).split(",") if x.strip()]
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if int(args.truncate_output):
        out_path.write_text("", encoding="utf-8")

    if str(args.seed_jsonl).strip():
        pools, by_mint = load_pools_from_seed(str(args.seed_jsonl), int(args.seed_rows))
    else:
        pools, by_mint = load_pools(int(args.max_mints))
        pools, by_mint = cap_pools_for_account_subs(
            pools,
            by_mint,
            int(args.max_account_subs),
            int(args.max_pools_per_mint),
        )
    load_reserves(pools, int(args.batch_size), float(args.batch_sleep_sec))
    mint_supply = load_mint_supplies(list(by_mint), int(args.batch_size), float(args.batch_sleep_sec))
    fallback_fees, flat_fees, fee_tiers = load_fee_config()
    pool_fees: dict[str, tuple[int, int, int]] = {}
    for pool in pools.values():
        pool_fees[pool.key] = effective_pumpswap_fees(
            mint=pool.base_mint,
            pool=pool,
            mint_supply=int(mint_supply.get(pool.base_mint, 0)),
            flat_fees=flat_fees,
            tiers=fee_tiers,
            fallback=fallback_fees,
        )

    token_to_pool: dict[str, tuple[str, str]] = {}
    for pool in pools.values():
        token_to_pool[pool.base_token_account] = (pool.key, "base")
        token_to_pool[pool.quote_token_account] = (pool.key, "quote")

    counts = {
        "subscriptions": 0,
        "reserve_updates": 0,
        "route_checks": 0,
        "route_pass": 0,
        "best_edge": -10**18,
    }
    last_eval: dict[str, int] = {}

    def evaluate_pool_key(pool_key: str, why: str) -> None:
        now = now_ms()
        if now - int(last_eval.get(pool_key, 0)) < int(args.min_eval_interval_ms):
            return
        last_eval[pool_key] = now
        buy_pool = pools.get(pool_key)
        if not buy_pool:
            return
        peers = by_mint.get(buy_pool.base_mint, [])
        if len(peers) < 2 or buy_pool.base_reserve <= 0 or buy_pool.quote_reserve <= 0:
            return
        for sell_key in peers:
            if sell_key == pool_key:
                continue
            sell_pool = pools.get(sell_key)
            if not sell_pool or sell_pool.base_reserve <= 0 or sell_pool.quote_reserve <= 0:
                continue
            buy_fees = pool_fees.get(pool_key, flat_fees)
            sell_fees = pool_fees.get(sell_key, flat_fees)
            sizes = optimized_route_sizes(
                base_sizes=base_sizes,
                buy_pool=buy_pool,
                sell_pool=sell_pool,
                buy_fees=buy_fees,
                sell_fees=sell_fees,
                fee_buffer_lamports=int(args.fee_buffer_lamports),
                projection_buffer_lamports=int(args.projection_buffer_lamports),
                auto_min_size=auto_min_size,
                auto_max_size=auto_max_size,
                neighborhood_bps=neighborhood_bps,
            )
            for size in sizes:
                tokens, sell_out, edge = route_edge_for_size(
                    size=size,
                    buy_pool=buy_pool,
                    sell_pool=sell_pool,
                    buy_fees=buy_fees,
                    sell_fees=sell_fees,
                    fee_buffer_lamports=int(args.fee_buffer_lamports),
                    projection_buffer_lamports=int(args.projection_buffer_lamports),
                )
                counts["route_checks"] += 1
                counts["best_edge"] = max(int(counts["best_edge"]), int(edge))
                if edge < int(args.min_edge_lamports):
                    continue
                counts["route_pass"] += 1
                rec = {
                    "kind": "v270_pumpswap_multipool_ws_pass",
                    "ts_ms": now,
                    "why": why,
                    "mint": buy_pool.base_mint,
                    "buy_pool": buy_pool.key,
                    "sell_pool": sell_pool.key,
                    "size_lamports": int(size),
                    "tokens_raw": int(tokens),
                    "sell_out_lamports": int(sell_out),
                    "edge_lamports": int(edge),
                    "buy_pool_base": int(buy_pool.base_reserve),
                    "buy_pool_quote": int(buy_pool.quote_reserve),
                    "sell_pool_base": int(sell_pool.base_reserve),
                    "sell_pool_quote": int(sell_pool.quote_reserve),
                    "buy_fee_bps": sum(int(x) for x in buy_fees),
                    "sell_fee_bps": sum(int(x) for x in sell_fees),
                    "auto_optimized_sizes": True,
                    "passed": True,
                }
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
                log(
                    f"PGG2-V270-MULTIPOOL-PASS mint={short(buy_pool.base_mint)} why={why} "
                    f"buy_pool={short(buy_pool.key)} sell_pool={short(sell_pool.key)} "
                    f"size={size/LAMPORTS_PER_SOL:.6f} edge_lamports={int(edge):+}"
                )
                if int(args.stop_on_pass):
                    raise KeyboardInterrupt

    log(
        f"PGG2-V270-START seconds={args.seconds} max_mints={args.max_mints} "
        f"pools={len(pools)} min_edge={args.min_edge_lamports} sizes={args.sizes_sol} "
        f"auto_range={args.auto_min_size_sol}-{args.auto_max_size_sol}"
    )

    # Evaluate fresh snapshot once, then keep it live from token-account updates.
    for key in list(pools):
        evaluate_pool_key(key, "init_snapshot")

    pending_req: dict[int, tuple[str, str]] = {}
    sub_to_target: dict[int, tuple[str, str]] = {}
    started = time.time()
    req_id = 100
    async with websockets.connect(ws_url(), ping_interval=15, ping_timeout=10, max_size=32 * 1024 * 1024) as ws:
        for acct in token_to_pool:
            pending_req[req_id] = ("token", acct)
            await subscribe_account(ws, req_id=req_id, pubkey=acct)
            req_id += 1
            counts["subscriptions"] += 1
            if float(args.subscribe_delay_ms) > 0:
                await asyncio.sleep(float(args.subscribe_delay_ms) / 1000.0)
            if counts["subscriptions"] >= int(args.max_account_subs):
                break
        log(f"PGG2-V270-WS-SUBSCRIBED token_accounts={counts['subscriptions']}")

        while time.time() - started < int(args.seconds):
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed as exc:
                log(f"PGG2-V270-WS-CLOSED code={getattr(exc, 'code', '?')} reason={str(exc)[:160]}")
                break
            if "id" in msg and "result" in msg:
                target = pending_req.pop(int(msg["id"]), None)
                if target:
                    sub_to_target[int(msg["result"])] = target
                continue
            if "id" in msg and "error" in msg:
                pending_req.pop(int(msg["id"]), None)
                continue
            params = msg.get("params") or {}
            result = params.get("result") or {}
            sub = int(params.get("subscription") or -1)
            target = sub_to_target.get(sub)
            if not target:
                continue
            _kind, acct = target
            pool_key, side = token_to_pool.get(acct, ("", ""))
            pool = pools.get(pool_key)
            if not pool:
                continue
            try:
                bal = token_balance_from_raw(account_data_from_notification(result.get("value") or {}))
            except Exception:
                continue
            old_base, old_quote = pool.base_reserve, pool.quote_reserve
            if side == "base":
                pool.base_reserve = int(bal)
            else:
                pool.quote_reserve = int(bal)
            if old_base != pool.base_reserve or old_quote != pool.quote_reserve:
                counts["reserve_updates"] += 1
                evaluate_pool_key(pool_key, f"{side}_reserve_update")

    log(
        "PGG2-V270-FINAL "
        + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f" out_jsonl={out_path}"
    )
    return 0 if counts["route_pass"] > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=int(os.environ.get("V270_SECONDS", "120")))
    ap.add_argument("--max-mints", type=int, default=int(os.environ.get("V270_MAX_MINTS", "300")))
    ap.add_argument("--max-account-subs", type=int, default=int(os.environ.get("V270_MAX_ACCOUNT_SUBS", "200")))
    ap.add_argument("--max-pools-per-mint", type=int, default=int(os.environ.get("V270_MAX_POOLS_PER_MINT", "4")))
    ap.add_argument("--seed-jsonl", default=os.environ.get("V270_SEED_JSONL", ""))
    ap.add_argument("--seed-rows", type=int, default=int(os.environ.get("V270_SEED_ROWS", "80")))
    ap.add_argument("--sizes-sol", default=os.environ.get("V270_SIZES_SOL", "0.00005,0.0001,0.0002,0.0005,0.001,0.002,0.003,0.005,0.01,0.02,0.04,0.06"))
    ap.add_argument("--auto-min-size-sol", default=os.environ.get("V270_AUTO_MIN_SIZE_SOL", "0.00005"))
    ap.add_argument("--auto-max-size-sol", default=os.environ.get("V270_AUTO_MAX_SIZE_SOL", "0.06"))
    ap.add_argument("--auto-neighborhood-bps", default=os.environ.get("V270_AUTO_NEIGHBORHOOD_BPS", "25,50,100,200,500,1000"))
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V270_FEE_BUFFER_LAMPORTS", "0")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V270_PROJECTION_BUFFER_LAMPORTS", "0")))
    ap.add_argument("--min-edge-lamports", type=int, default=int(os.environ.get("V270_MIN_EDGE_LAMPORTS", "1")))
    ap.add_argument("--min-eval-interval-ms", type=int, default=int(os.environ.get("V270_MIN_EVAL_INTERVAL_MS", "20")))
    ap.add_argument("--batch-size", type=int, default=int(os.environ.get("V270_BATCH_SIZE", "50")))
    ap.add_argument("--batch-sleep-sec", type=float, default=float(os.environ.get("V270_BATCH_SLEEP_SEC", "0.08")))
    ap.add_argument("--subscribe-delay-ms", type=float, default=float(os.environ.get("V270_SUBSCRIBE_DELAY_MS", "20")))
    ap.add_argument("--out-jsonl", default=os.environ.get("V270_OUT_JSONL", "/root/piggy/data/v270_pumpswap_multipool_ws.jsonl"))
    ap.add_argument("--truncate-output", type=int, default=int(os.environ.get("V270_TRUNCATE_OUTPUT", "1")))
    ap.add_argument("--stop-on-pass", type=int, default=int(os.environ.get("V270_STOP_ON_PASS", "0")))
    try:
        return asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
