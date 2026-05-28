#!/usr/bin/env python3
"""V219 no-wallet atomic route-dislocation scanner.

This is not a directional bot and does not sign or send transactions.

It watches PumpSwap pool updates and evaluates whether a fully atomic closed
loop could be positive:

  A) buy on Pump bonding curve -> sell on PumpSwap
  B) buy on PumpSwap -> sell on Pump bonding curve

The scanner is deliberately fail-closed: it logs a PASS only when the modeled
final wallet delta is positive after base fees, Jito tip estimate, and a
projection buffer. Rent is not counted as profit because ATA rent is recovered
only if the bundle closes cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import websockets
from solders.pubkey import Pubkey


LAMPORTS_PER_SOL = 1_000_000_000
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_AMM_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsFnd2e3oKQkEwcEcvp3u1pob3n4T")
PUMP_AMM_GLOBAL = Pubkey.find_program_address([b"global_config"], PUMP_AMM_PROGRAM)[0]
BC_DISC = bytes([0x17, 0xB7, 0xF8, 0x37, 0x60, 0xD8, 0xAC, 0x60])


def now_ms() -> int:
    return int(time.time() * 1000)


def log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if s and len(s) > 10 else (s or "?")


def load_env() -> None:
    path = Path("/root/piggy/.env")
    try:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except PermissionError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def rpc_url() -> str:
    api = os.environ.get("HELIUS_API_KEY", "").strip()
    return (
        os.environ.get("HELIUS_RPC_URL", "").strip()
        or (f"https://mainnet.helius-rpc.com/?api-key={api}" if api else "")
        or os.environ.get("SOLANA_RPC_URL", "").strip()
        or "https://api.mainnet-beta.solana.com"
    )


def ws_url() -> str:
    http = rpc_url()
    if http.startswith("https://"):
        return "wss://" + http[len("https://") :]
    if http.startswith("http://"):
        return "ws://" + http[len("http://") :]
    return http


def rpc_call(method: str, params: list[Any], timeout: float = 4.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url(), data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if out.get("error"):
        raise RuntimeError(str(out["error"])[:300])
    return out.get("result")


def account_data(pubkey: str, timeout: float = 3.0) -> tuple[bytes, int]:
    res = rpc_call(
        "getAccountInfo",
        [pubkey, {"encoding": "base64", "commitment": "processed"}],
        timeout=timeout,
    )
    ctx_slot = int((res or {}).get("context", {}).get("slot") or 0)
    val = (res or {}).get("value")
    if not val:
        raise RuntimeError("account_missing")
    data = val.get("data") or []
    if isinstance(data, list):
        data = data[0]
    if not isinstance(data, str):
        raise RuntimeError("account_data_missing")
    return base64.b64decode(data), ctx_slot


def token_balance_raw(token_account: str) -> int:
    data, _slot = account_data(token_account, timeout=2.0)
    return int.from_bytes(data[64:72], "little") if len(data) >= 72 else 0


def pda(*seeds: bytes, program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), program)[0]


def u16(n: int) -> bytes:
    return int(n).to_bytes(2, "little")


@dataclass
class PumpFees:
    fee_bps: int
    creator_fee_bps: int

    @property
    def total(self) -> int:
        return max(0, int(self.fee_bps) + int(self.creator_fee_bps))


@dataclass
class PumpSwapFees:
    lp_fee_bps: int
    protocol_fee_bps: int
    creator_fee_bps: int

    @property
    def total(self) -> int:
        return max(0, int(self.lp_fee_bps) + int(self.protocol_fee_bps) + int(self.creator_fee_bps))


@dataclass
class Curve:
    key: str
    mint: str
    vtok: int
    vsol: int
    real_tok: int
    real_sol: int
    complete: bool
    creator: str


@dataclass
class Pool:
    key: str
    base_mint: str
    quote_mint: str
    base_token_account: str
    quote_token_account: str
    creator: str
    coin_creator: str
    is_mayhem: bool
    base_reserve: int = 0
    quote_reserve: int = 0


def parse_pump_fees() -> PumpFees:
    try:
        data, _slot = account_data(str(PUMP_GLOBAL), timeout=3.0)
        if len(data) >= 386:
            return PumpFees(
                fee_bps=int.from_bytes(data[105:113], "little"),
                creator_fee_bps=int.from_bytes(data[154:162], "little"),
            )
    except Exception as exc:
        log(f"PGG2-V219-FEE-FALLBACK route=pump_bc err={type(exc).__name__}:{str(exc)[:80]}")
    return PumpFees(100, 0)


def parse_pumpswap_fees() -> PumpSwapFees:
    try:
        data, _slot = account_data(str(PUMP_AMM_GLOBAL), timeout=3.0)
        if len(data) >= 321:
            return PumpSwapFees(
                lp_fee_bps=int.from_bytes(data[40:48], "little"),
                protocol_fee_bps=int.from_bytes(data[48:56], "little"),
                creator_fee_bps=int.from_bytes(data[313:321], "little"),
            )
    except Exception as exc:
        log(f"PGG2-V219-FEE-FALLBACK route=pumpswap err={type(exc).__name__}:{str(exc)[:80]}")
    return PumpSwapFees(20, 5, 5)


def parse_pool(key: str, data: bytes) -> Pool:
    if len(data) < 243:
        raise RuntimeError(f"pool_data_short:{len(data)}")
    return Pool(
        key=key,
        creator=str(Pubkey.from_bytes(data[11:43])),
        base_mint=str(Pubkey.from_bytes(data[43:75])),
        quote_mint=str(Pubkey.from_bytes(data[75:107])),
        base_token_account=str(Pubkey.from_bytes(data[139:171])),
        quote_token_account=str(Pubkey.from_bytes(data[171:203])),
        coin_creator=str(Pubkey.from_bytes(data[211:243])),
        is_mayhem=bool(data[243]) if len(data) > 243 else False,
    )


def fetch_curve(mint: str) -> Curve:
    mint_pk = Pubkey.from_string(mint)
    curve_key = pda(b"bonding-curve", bytes(mint_pk), program=PUMP_PROGRAM)
    data, _slot = account_data(str(curve_key), timeout=2.0)
    if len(data) < 49 or data[:8] != BC_DISC:
        raise RuntimeError(f"bad_curve_layout:{len(data)}")
    creator = str(Pubkey.from_bytes(data[49:81])) if len(data) >= 81 else str(Pubkey.default())
    return Curve(
        key=str(curve_key),
        mint=mint,
        vtok=int.from_bytes(data[8:16], "little"),
        vsol=int.from_bytes(data[16:24], "little"),
        real_tok=int.from_bytes(data[24:32], "little"),
        real_sol=int.from_bytes(data[32:40], "little"),
        complete=bool(data[48]),
        creator=creator,
    )


def ceil_div(a: int, b: int) -> int:
    return (int(a) + int(b) - 1) // int(b)


def pump_buy_tokens(size: int, curve: Curve, fees: PumpFees) -> int:
    total = fees.total
    net_sol = int(size) * 10_000 // max(10_000 + total, 1)
    fee_lamports = ceil_div(net_sol * fees.fee_bps, 10_000) + ceil_div(net_sol * fees.creator_fee_bps, 10_000)
    if net_sol + fee_lamports > size:
        net_sol -= net_sol + fee_lamports - size
    net_for_curve = max(0, net_sol - 1)
    tokens = net_for_curve * curve.vtok // max(curve.vsol + net_for_curve, 1)
    return max(0, min(tokens, curve.real_tok))


def pump_sell_lamports(tokens: int, curve: Curve, fees: PumpFees) -> int:
    gross = int(tokens) * curve.vsol // max(curve.vtok + int(tokens), 1)
    fee = ceil_div(gross * fees.fee_bps, 10_000) + ceil_div(gross * fees.creator_fee_bps, 10_000)
    return max(0, gross - fee)


def pumpswap_buy_tokens(size: int, pool: Pool, fees: PumpSwapFees) -> int:
    total = fees.total
    net_quote = int(size) * 10_000 // max(10_000 + total, 1)
    return max(0, net_quote * pool.base_reserve // max(pool.quote_reserve + net_quote, 1))


def pumpswap_sell_lamports(tokens: int, pool: Pool, fees: PumpSwapFees) -> int:
    total = fees.total
    gross = int(tokens) * pool.quote_reserve // max(pool.base_reserve + int(tokens), 1)
    fee = ceil_div(gross * total, 10_000)
    return max(0, gross - fee)


def edge_lamports(out_lamports: int, size_lamports: int, fee_buffer: int, projection_buffer: int) -> int:
    return int(out_lamports) - int(size_lamports) - int(fee_buffer) - int(projection_buffer)


def evaluate_pool(
    *,
    pool: Pool,
    curve: Curve,
    pump_fees: PumpFees,
    ps_fees: PumpSwapFees,
    sizes: list[int],
    fee_buffer: int,
    projection_buffer: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if pool.quote_mint != str(WSOL_MINT) or pool.base_mint != curve.mint:
        return out
    if pool.base_reserve <= 0 or pool.quote_reserve <= 0:
        return out
    if curve.complete:
        return out

    for size in sizes:
        bc_tokens = pump_buy_tokens(size, curve, pump_fees)
        ps_out = pumpswap_sell_lamports(bc_tokens, pool, ps_fees)
        edge_a = edge_lamports(ps_out, size, fee_buffer, projection_buffer)
        out.append(
            {
                "route": "pump_bc_buy_to_pumpswap_sell",
                "size_lamports": size,
                "tokens_raw": bc_tokens,
                "sell_out_lamports": ps_out,
                "edge_lamports": edge_a,
            }
        )

        ps_tokens = pumpswap_buy_tokens(size, pool, ps_fees)
        bc_out = pump_sell_lamports(ps_tokens, curve, pump_fees)
        edge_b = edge_lamports(bc_out, size, fee_buffer, projection_buffer)
        out.append(
            {
                "route": "pumpswap_buy_to_pump_bc_sell",
                "size_lamports": size,
                "tokens_raw": ps_tokens,
                "sell_out_lamports": bc_out,
                "edge_lamports": edge_b,
            }
        )
    return out


async def run(args: argparse.Namespace) -> int:
    load_env()
    pump_fees = parse_pump_fees()
    ps_fees = parse_pumpswap_fees()
    sizes = [int(float(x) * LAMPORTS_PER_SOL) for x in args.sizes_sol.split(",") if x.strip()]
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    counts: dict[str, int] = {
        "pool_updates": 0,
        "pool_parse_fail": 0,
        "curve_fetch_fail": 0,
        "reserve_fetch_fail": 0,
        "route_checks": 0,
        "route_pass": 0,
    }

    log(
        "PGG2-V219-START "
        f"source=pumpswap_programSubscribe seconds={args.seconds} sizes={args.sizes_sol} "
        f"pump_fee_bps={pump_fees.total} pumpswap_fee_bps={ps_fees.total} "
        f"fee_buffer_lamports={args.fee_buffer_lamports} projection_buffer_lamports={args.projection_buffer_lamports}"
    )

    sub_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "programSubscribe",
        "params": [
            str(PUMP_AMM_PROGRAM),
            {
                "encoding": "base64",
                "commitment": "processed",
                "filters": [{"memcmp": {"offset": 75, "bytes": str(WSOL_MINT)}}],
            },
        ],
    }

    async with websockets.connect(
        ws_url(),
        ping_interval=15,
        ping_timeout=10,
        max_size=32 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps(sub_req))
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        log(f"PGG2-V219-WS-SUBSCRIBED ok={int('result' in first)} id={first.get('result', '-')}")
        while time.time() - started < args.seconds:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                continue
            params = msg.get("params") or {}
            value = (params.get("result") or {}).get("value") or {}
            pubkey = str(value.get("pubkey") or "")
            account = value.get("account") or {}
            data_field = account.get("data") or []
            if isinstance(data_field, list):
                data_field = data_field[0]
            if not pubkey or not isinstance(data_field, str):
                continue
            counts["pool_updates"] += 1
            try:
                pool = parse_pool(pubkey, base64.b64decode(data_field))
            except Exception as exc:
                counts["pool_parse_fail"] += 1
                log(f"PGG2-V219-POOL-PARSE-FAIL pool={short(pubkey)} err={type(exc).__name__}:{str(exc)[:100]}")
                continue
            try:
                pool.base_reserve = token_balance_raw(pool.base_token_account)
                pool.quote_reserve = token_balance_raw(pool.quote_token_account)
            except Exception as exc:
                counts["reserve_fetch_fail"] += 1
                log(f"PGG2-V219-RESERVE-FETCH-FAIL mint={short(pool.base_mint)} pool={short(pool.key)} err={type(exc).__name__}:{str(exc)[:100]}")
                continue
            try:
                curve = fetch_curve(pool.base_mint)
            except Exception as exc:
                counts["curve_fetch_fail"] += 1
                log(f"PGG2-V219-CURVE-FETCH-FAIL mint={short(pool.base_mint)} pool={short(pool.key)} err={type(exc).__name__}:{str(exc)[:100]}")
                continue
            checks = evaluate_pool(
                pool=pool,
                curve=curve,
                pump_fees=pump_fees,
                ps_fees=ps_fees,
                sizes=sizes,
                fee_buffer=int(args.fee_buffer_lamports),
                projection_buffer=int(args.projection_buffer_lamports),
            )
            if not checks:
                continue
            best = max(checks, key=lambda r: int(r["edge_lamports"]))
            counts["route_checks"] += len(checks)
            pass_rows = [r for r in checks if int(r["edge_lamports"]) >= int(args.min_edge_lamports)]
            if pass_rows:
                counts["route_pass"] += len(pass_rows)
                row = max(pass_rows, key=lambda r: int(r["edge_lamports"]))
                rec = {
                    "kind": "v219_atomic_route_dislocation_pass",
                    "ts_ms": now_ms(),
                    "mint": curve.mint,
                    "pool": pool.key,
                    "curve": curve.key,
                    "pool_base_reserve": pool.base_reserve,
                    "pool_quote_reserve": pool.quote_reserve,
                    "curve_vsol": curve.vsol,
                    "curve_vtok": curve.vtok,
                    "curve_real_tok": curve.real_tok,
                    **row,
                }
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
                log(
                    f"PGG2-V219-ATOMIC-ROUTE-PASS mint={short(curve.mint)} pool={short(pool.key)} "
                    f"route={row['route']} size={row['size_lamports']/LAMPORTS_PER_SOL:.4f} "
                    f"edge_lamports={int(row['edge_lamports']):+} sell_out={int(row['sell_out_lamports'])}"
                )
                if args.stop_on_pass:
                    break
            elif args.verbose_blocks:
                log(
                    f"PGG2-V219-ATOMIC-ROUTE-BLOCK mint={short(curve.mint)} pool={short(pool.key)} "
                    f"best_route={best['route']} best_edge_lamports={int(best['edge_lamports']):+}"
                )

    log(
        "PGG2-V219-FINAL "
        + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f" out_jsonl={out_path}"
    )
    return 0 if counts["route_pass"] > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=int(os.environ.get("V219_SECONDS", "120")))
    ap.add_argument("--sizes-sol", default=os.environ.get("V219_SIZES_SOL", "0.001,0.0015,0.002,0.003,0.005"))
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V219_FEE_BUFFER_LAMPORTS", "90000")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V219_PROJECTION_BUFFER_LAMPORTS", "30000")))
    ap.add_argument("--min-edge-lamports", type=int, default=int(os.environ.get("V219_MIN_EDGE_LAMPORTS", "30000")))
    ap.add_argument("--out-jsonl", default=os.environ.get("V219_OUT_JSONL", "/root/piggy/data/v219_atomic_route_dislocation.jsonl"))
    ap.add_argument("--verbose-blocks", action="store_true", default=os.environ.get("V219_VERBOSE_BLOCKS", "0") == "1")
    ap.add_argument("--stop-on-pass", action="store_true", default=os.environ.get("V219_STOP_ON_PASS", "0") == "1")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
