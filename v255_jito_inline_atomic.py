#!/usr/bin/env python3
"""V255 exact-positive PumpSwap atomic route via single-tx Jito bundle.

V252 proved the route is real, but Helius Sender's 5000-lamport SWQOS tip
keeps many candidates just below break-even. V255 keeps the same atomic
buy->sell->close transaction, moves delivery to Jito sendBundle, and embeds a
single 1000-lamport Jito tip inside that same transaction. That removes the
extra Sender tip drag without introducing an unhedged position.

Fail-closed rule: no send unless exact simulation of the payer wallet delta is
positive for the final signed transaction.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _load_env, _make_broker
from pgg2_v224_pumpswap_multipool_builder import build_explicit_multipool_tx
from v245_fast_single_tx_oracle import (
    _first_instruction_error,
    _instruction_phase,
    _redact_rpc,
    _sim_bundle_rpcs,
    _standard_sim_rpcs,
    rows,
    simulate_one,
)


READ_RPC = os.environ.get("V255_READ_RPC", "https://public.rpc.solanavibestation.com")
WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
# Jito bundle auction requires the transaction to write-lock one of Jito's
# tip accounts. The Helius/Sender tip account is not accepted by sendBundle.
JITO_TIP_ACCOUNT = "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"
HELIUS_TIP_ACCOUNT = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
HELIUS_SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
JITO_REGION_BUNDLE_URLS = [
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def rpc_call(rpc_url: str, method: str, params: list[Any], timeout: float = 20.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out.get("result")


def _region(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc or url
    return host.split(".", 1)[0]


def _bundle_urls() -> list[str]:
    raw = os.environ.get("PGG2_BUNDLE_SEND_URLS", "").strip()
    vals = [x.strip().rstrip("/") for x in raw.split(",") if x.strip()] if raw else list(JITO_REGION_BUNDLE_URLS)
    out: list[str] = []
    seen: set[str] = set()
    for val in vals:
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _tx_bundle_only_url(bundle_url: str) -> str:
    url = bundle_url.rstrip("/")
    for suffix in ("/api/v1/bundles", "/api/v1/getTipAccounts", "/api/v1/getBundleStatuses", "/api/v1/getInflightBundleStatuses"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/") + "/api/v1/transactions?bundleOnly=true"


def send_jito_transaction_bundle_only(tx_b64: str) -> dict[str, Any]:
    urls = [_tx_bundle_only_url(u) for u in _bundle_urls()]
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64"}],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    log(
        "PGG2-V255-JITO-TX-BUNDLEONLY-SEND "
        f"endpoints={','.join(_region(u) for u in urls)}"
    )

    def send_one(url: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                ms = int((time.perf_counter() - started) * 1000)
                if parsed.get("error"):
                    return {"ok": False, "region": _region(url), "ms": ms, "error": str(parsed["error"])[:240]}
                return {
                    "ok": True,
                    "region": _region(url),
                    "ms": ms,
                    "sig": str(parsed.get("result") or ""),
                    "bundle_id": str(resp.headers.get("x-bundle-id") or ""),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:240]
            return {"ok": False, "region": _region(url), "ms": int((time.perf_counter() - started) * 1000), "error": f"HTTP{exc.code}:{body}"}
        except Exception as exc:
            return {"ok": False, "region": _region(url), "ms": int((time.perf_counter() - started) * 1000), "error": f"{type(exc).__name__}:{str(exc)[:220]}"}

    errors: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(urls))) as ex:
        futs = [ex.submit(send_one, url) for url in urls]
        for fut in concurrent.futures.as_completed(futs, timeout=3.0):
            res = fut.result()
            if res.get("ok"):
                log(
                    f"PGG2-V255-JITO-TX-BUNDLEONLY-RESULT region={res['region']} "
                    f"ms={res['ms']} sig={res.get('sig')} bundle_id={res.get('bundle_id')}"
                )
                successes.append(res)
                continue
            errors.append(res)
            log(
                f"PGG2-V255-JITO-TX-BUNDLEONLY-ERR region={res.get('region')} "
                f"ms={res.get('ms')} err={str(res.get('error'))[:240]}"
            )
    if successes:
        return successes[0]
    raise RuntimeError("jito_tx_bundleonly_all_failed:" + ";".join(f"{e.get('region')}:{e.get('error')}" for e in errors[:8]))


def beam_url() -> str:
    _load_env()
    explicit = os.environ.get("RPCFAST_BEAM_URL", "").strip()
    if explicit:
        return explicit
    key = os.environ.get("RPCFAST_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing_rpcfast_api_key")
    return "https://beam.rpcfast.com/?api_key=" + key


def rpcfast_rpc_url() -> str:
    _load_env()
    explicit = os.environ.get("RPCFAST_RPC_URL", "").strip()
    if explicit:
        return explicit
    key = os.environ.get("RPCFAST_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing_rpcfast_api_key")
    return "https://solana-rpc.rpcfast.com/?api_key=" + key


def send_beam_transaction(tx_b64: str) -> dict[str, Any]:
    url = beam_url()
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    log("PGG2-V255-BEAM-SEND endpoint=rpcfast_beam")
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            ms = int((time.perf_counter() - started) * 1000)
            if parsed.get("error"):
                raise RuntimeError(str(parsed["error"])[:300])
            sig = str(parsed.get("result") or "")
            log(f"PGG2-V255-BEAM-RESULT ms={ms} sig={sig}")
            return {"ok": True, "sig": sig, "ms": ms, "transport": "rpcfast_beam"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"beam_http_{exc.code}:{body}") from exc


def send_rpcfast_rpc(tx_b64: str) -> dict[str, Any]:
    url = rpcfast_rpc_url()
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    log("PGG2-V255-RPCFAST-RPC-SEND endpoint=rpcfast_direct skipPreflight=1 maxRetries=0")
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            ms = int((time.perf_counter() - started) * 1000)
            if parsed.get("error"):
                raise RuntimeError(str(parsed["error"])[:300])
            sig = str(parsed.get("result") or "")
            log(f"PGG2-V255-RPCFAST-RPC-RESULT ms={ms} sig={sig}")
            return {"ok": True, "sig": sig, "ms": ms, "transport": "rpcfast_rpc"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"rpcfast_rpc_http_{exc.code}:{body}") from exc


def send_helius_sender(tx_b64: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    log("PGG2-V255-HELIUS-SENDER-SEND endpoint=sender_swqos")
    started = time.perf_counter()
    try:
        req = urllib.request.Request(HELIUS_SENDER_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            ms = int((time.perf_counter() - started) * 1000)
            if parsed.get("error"):
                raise RuntimeError(str(parsed["error"])[:300])
            sig = str(parsed.get("result") or "")
            log(f"PGG2-V255-HELIUS-SENDER-RESULT ms={ms} sig={sig}")
            return {"ok": True, "sig": sig, "ms": ms, "transport": "helius_sender_swqos"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"helius_sender_http_{exc.code}:{body}") from exc


def wait_signature(sig: str, seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + max(1.0, float(seconds))
    last: Any = None
    while time.time() < deadline:
        try:
            out = rpc_call(READ_RPC, "getSignatureStatuses", [[sig], {"searchTransactionHistory": False}], timeout=8.0)
            val = (out.get("value") or [None])[0]
            last = val
            log(f"PGG2-V255-SIG-STATUS sig={sig} status={val}")
            if val and (val.get("err") is not None or val.get("confirmationStatus") in ("processed", "confirmed", "finalized")):
                return {"sig": sig, "status": val}
        except Exception as exc:
            last = f"{type(exc).__name__}:{str(exc)[:160]}"
            log(f"PGG2-V255-SIG-STATUS-ERR sig={sig} err={last}")
        time.sleep(0.5)
    return {"sig": sig, "status": "timeout", "last": last}


def setup_env(args: argparse.Namespace) -> None:
    _load_env()
    os.environ["HELIUS_RPC_URL"] = READ_RPC
    os.environ["SOLANA_RPC_URL"] = READ_RPC
    os.environ["V224_BUY_MODE"] = str(args.buy_mode)
    os.environ["V224_COMPUTE_BUDGET_MODE"] = "none"
    os.environ["V224_QUOTE_IN_REMAINING"] = "1"
    os.environ["V224_EXACT_BASE_REMAINING"] = "1"
    os.environ["V224_ADDRESS_LOOKUP_TABLE_JSON"] = str(args.lut_json)
    os.environ["PGG2_DIRECT_TRACK_VOLUME"] = "0"
    os.environ["PGG2_DIRECT_PRIORITY_FEE_SOL"] = "0"
    os.environ["PGG2_DIRECT_COMPUTE_UNIT_PRICE_MICROLAMPORTS"] = "0"
    os.environ["V224_CLOSE_USER_VOLUME"] = "1"
    if args.transport == "helius_sender_swqos":
        os.environ["PGG2_JITO_TIP_ACCOUNT"] = HELIUS_TIP_ACCOUNT
    else:
        os.environ["PGG2_JITO_TIP_ACCOUNT"] = JITO_TIP_ACCOUNT
    os.environ.setdefault("PGG2_BUNDLE_SEND_URLS", ",".join(JITO_REGION_BUNDLE_URLS))
    os.environ.setdefault("PGG2_BUNDLE_HTTP_TIMEOUT_SEC", "1.5")
    os.environ.setdefault("PGG2_BUNDLE_RACE_TIMEOUT_SEC", "2.0")


def build_best(args: argparse.Namespace) -> dict[str, Any] | None:
    sim_rpcs = _sim_bundle_rpcs()
    standard_rpcs = _standard_sim_rpcs()
    broker = _make_broker()
    broker.rpc_url = READ_RPC
    broker.refresh_blockhash_cache()
    cands = rows(pathlib.Path(args.candidates_jsonl), int(args.limit), int(args.max_per_mint))
    buffers = [int(x) for x in str(args.quote_cushions).split(",") if x.strip()]
    if not buffers:
        buffers = [int(args.quote_cushion_lamports)]
    tips = [int(x) for x in str(args.tip_ladder_lamports).split(",") if x.strip()]
    if not tips:
        tips = [int(args.tip_lamports)]
    tips = sorted(set(max(0, int(x)) for x in tips), reverse=True)
    log(
        "PGG2-V255-START "
        f"candidates={len(cands)} buy_mode={args.buy_mode} inline_tip_ladder={tips} "
        f"min_profit={args.min_profit_lamports} min_delta={args.min_positive_delta_lamports} "
        f"buffers={buffers} sim_rpcs={[ _redact_rpc(x) for x in sim_rpcs ]}"
    )
    structural_bad: dict[str, str] = {}
    best: dict[str, Any] | None = None
    good_enough_tip = int(args.good_enough_tip_lamports)
    good_enough_delta = int(args.good_enough_delta_lamports)
    deadline = time.time() + float(args.search_seconds) if float(args.search_seconds) > 0 else 0.0
    for idx, cand in enumerate(cands, 1):
        if deadline and time.time() >= deadline:
            if best is not None:
                log(
                    "PGG2-V255-SEARCH-DEADLINE-USE-BEST "
                    f"idx={idx} delta={best['sim'].get('wallet_delta_lamports')} "
                    f"target={good_enough_delta}"
                )
                return best
            log(f"PGG2-V255-SEARCH-DEADLINE-NO-BEST idx={idx} target={good_enough_delta}")
            break
        mint_key = str(cand.get("mint") or "")
        if mint_key in structural_bad:
            log(f"PGG2-V255-MINT-SKIP idx={idx} mint={mint_key[:4]}.. reason={structural_bad[mint_key]}")
            continue
        structural_reason = ""
        for tip in tips:
          for buf in buffers:
            os.environ["V224_EXACT_BASE_QUOTE_CUSHION_LAMPORTS"] = str(buf)
            try:
                meta = build_explicit_multipool_tx(
                    broker=broker,
                    mint=str(cand["mint"]),
                    buy_pool_key=str(cand["buy_pool"]),
                    sell_pool_key=str(cand["sell_pool"]),
                    size_lamports=int(cand["size_lamports"]),
                    min_profit_lamports=int(args.min_profit_lamports),
                    fee_buffer_lamports=0,
                    projection_buffer_lamports=0,
                    tip_lamports=int(tip),
                )
            except Exception as exc:
                err = f"{type(exc).__name__}:{str(exc)[:180]}"
                if tip == tips[0] and buf == buffers[0]:
                    log(
                        f"PGG2-V255-BUILD-BLOCK idx={idx} tip={tip} buf={buf} mint={mint_key[:4]}.. "
                        f"edge={cand.get('edge_lamports')} err={err}"
                    )
                if "not_executable" in err or "exact_base_buy_zero" in err:
                    break
                continue
            tx_b64 = str(meta["tx_b64"])
            raw_len = len(base64.b64decode(tx_b64))
            if raw_len > 1232:
                log(f"PGG2-V255-SIZE-BLOCK idx={idx} tip={tip} buf={buf} mint={mint_key[:4]}.. raw_len={raw_len}")
                continue
            try:
                sim = simulate_one(tx_b64, sim_rpcs, standard_rpcs)
            except Exception as exc:
                log(f"PGG2-V255-SIM-BLOCK idx={idx} tip={tip} buf={buf} mint={mint_key[:4]}.. err={type(exc).__name__}:{str(exc)[:180]}")
                continue
            delta = sim.get("wallet_delta_lamports")
            projected_edge = int(meta.get("projected_edge_lamports") or 0)
            base_tx_fee = int(os.environ.get("V255_BASE_TX_FEE_LAMPORTS", "5000") or 5000)
            trade_delta_no_rent = projected_edge - base_tx_fee
            rent_or_cleanup_delta = (
                int(delta) - int(trade_delta_no_rent)
                if delta is not None
                else None
            )
            log(
                f"PGG2-V255-EXACT-SIM idx={idx} tip={tip} buf={buf} mint={mint_key[:4]}.. "
                f"raw_edge={cand.get('edge_lamports')} projected={projected_edge} "
                f"raw_len={raw_len} delta={delta} trade_delta_no_rent={trade_delta_no_rent} "
                f"rent_or_cleanup_delta={rent_or_cleanup_delta} ok={int(bool(sim.get('ok')))} "
                f"rpc={sim.get('rpc')} errs={sim.get('tx_errs')}"
            )
            if (
                sim.get("ok")
                and delta is not None
                and int(delta) >= int(args.min_positive_delta_lamports)
                and int(trade_delta_no_rent) >= int(args.min_trade_delta_lamports)
            ):
                candidate_best = {
                    "candidate": cand,
                    "meta": meta,
                    "sim": sim,
                    "trade_delta_no_rent_lamports": int(trade_delta_no_rent),
                    "rent_or_cleanup_delta_lamports": rent_or_cleanup_delta,
                    "buffer": buf,
                    "tip_lamports": tip,
                    "tx_b64": tx_b64,
                    "idx": idx,
                }
                if (
                    best is None
                    or int(candidate_best["tip_lamports"]) > int(best["tip_lamports"])
                    or (
                        int(candidate_best["tip_lamports"]) == int(best["tip_lamports"])
                        and int(candidate_best["trade_delta_no_rent_lamports"])
                        > int(best["trade_delta_no_rent_lamports"])
                    )
                ):
                    best = candidate_best
                    log(
                        f"PGG2-V255-BEST-UPDATE idx={idx} tip={tip} buf={buf} "
                        f"mint={mint_key[:4]}.. delta={delta} trade_delta_no_rent={trade_delta_no_rent}"
                    )
                    if (
                        int(tip) >= good_enough_tip
                        and int(delta) >= good_enough_delta
                        and int(trade_delta_no_rent) >= int(args.good_enough_trade_delta_lamports)
                    ):
                        log(
                            f"PGG2-V255-BEST-GOOD-ENOUGH idx={idx} tip={tip} "
                            f"tip_threshold={good_enough_tip} delta={delta} "
                            f"delta_threshold={good_enough_delta} "
                            f"trade_delta_no_rent={trade_delta_no_rent} "
                            f"trade_delta_threshold={int(args.good_enough_trade_delta_lamports)}"
                        )
                        return best
                break
            if sim.get("tx_errs"):
                ix, code = _first_instruction_error(sim.get("tx_errs"))
                phase = _instruction_phase(ix)
                if ix in (4, 5) and code in (6004, 6040):
                    log(f"PGG2-V255-SLIPPAGE-BUFFER-MISS idx={idx} tip={tip} buf={buf} mint={mint_key[:4]}.. phase={phase} code={code}")
                    continue
                if ix in (4, 5) and code in (6014, 6015):
                    structural_reason = f"{phase}_ix{ix}_custom{code}"
                    break
                break
        if structural_reason:
            structural_bad[mint_key] = structural_reason
            log(f"PGG2-V255-MINT-STRUCTURAL-SKIP-ARMED idx={idx} mint={mint_key[:4]}.. reason={structural_reason}")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-jsonl", default="/root/piggy/data/v223_v246_broad.jsonl")
    ap.add_argument("--lut-json", default="/root/piggy/data/v244_static_lut.json")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--max-per-mint", type=int, default=4)
    ap.add_argument("--tip-lamports", type=int, default=1000)
    ap.add_argument("--tip-ladder-lamports", default="4000,3500,3000,2500,2000,1500,1000")
    ap.add_argument("--good-enough-tip-lamports", type=int, default=4500)
    ap.add_argument("--good-enough-delta-lamports", type=int, default=0)
    ap.add_argument("--search-seconds", type=float, default=0.0)
    ap.add_argument("--min-profit-lamports", type=int, default=1)
    ap.add_argument("--min-positive-delta-lamports", type=int, default=1)
    ap.add_argument(
        "--min-trade-delta-lamports",
        type=int,
        default=0,
        help="Minimum route trade delta after base tx fee, excluding ATA rent/cleanup recovery.",
    )
    ap.add_argument(
        "--good-enough-trade-delta-lamports",
        type=int,
        default=0,
        help="Search-stop threshold for no-rent route trade delta.",
    )
    ap.add_argument("--buy-mode", choices=["exact_quote_in", "exact_base_out"], default="exact_base_out")
    ap.add_argument("--quote-cushion-lamports", type=int, default=10)
    ap.add_argument("--quote-cushions", default="1,2,3,4,6,8,10,12,16,24,32")
    ap.add_argument("--out-best-json", default="/root/piggy/data/v255_best_jito_inline_atomic.json")
    ap.add_argument(
        "--transport",
        choices=[
            "send_bundle",
            "send_transaction_bundle_only",
            "beam_send_transaction",
            "helius_sender_swqos",
            "rpcfast_rpc",
        ],
        default="send_transaction_bundle_only",
    )
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm-live", default="")
    args = ap.parse_args()
    if int(args.min_trade_delta_lamports) <= 0:
        args.min_trade_delta_lamports = int(args.min_positive_delta_lamports)
    if int(args.good_enough_trade_delta_lamports) <= 0:
        args.good_enough_trade_delta_lamports = int(args.good_enough_delta_lamports)

    setup_env(args)
    pre = int(rpc_call(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    log(f"PGG2-V255-PREFLIGHT-WALLET pre_lamports={pre}")
    best = build_best(args)
    if not best:
        log("PGG2-V255-NO-EXACT-POSITIVE")
        return 1
    pathlib.Path(args.out_best_json).write_text(json.dumps(best, indent=2, sort_keys=True), encoding="utf-8")
    sig_preview = str(VersionedTransaction.from_bytes(base64.b64decode(str(best["tx_b64"]))).signatures[0])
    log(
        f"PGG2-V255-EXACT-POSITIVE mint={str(best['candidate'].get('mint'))[:4]}.. "
        f"delta={best['sim'].get('wallet_delta_lamports')} buffer={best['buffer']} "
        f"tip_inline={best.get('tip_lamports')} sig={sig_preview}"
    )
    if not args.live:
        return 0
    if args.confirm_live != "I_ACCEPT_V255_JITO_INLINE_ATOMIC_RISK":
        raise RuntimeError("missing_live_confirmation")
    bundle_id = ""
    sig = sig_preview
    if args.transport == "send_bundle":
        from pgg2_v108_jito_bundle_sender import send_bundle, wait_bundle_status, warm_bundle_endpoints

        warm_bundle_endpoints()
        res = send_bundle([str(best["tx_b64"])], dry_run=False)
        bundle_id = str(res.get("bundle_id") or "")
        status = wait_bundle_status(bundle_id, timeout_sec=20.0, poll_sec=0.5) if bundle_id else {"status": "no_bundle_id"}
    else:
        if args.transport == "beam_send_transaction":
            res = send_beam_transaction(str(best["tx_b64"]))
        elif args.transport == "helius_sender_swqos":
            res = send_helius_sender(str(best["tx_b64"]))
        elif args.transport == "rpcfast_rpc":
            res = send_rpcfast_rpc(str(best["tx_b64"]))
        else:
            res = send_jito_transaction_bundle_only(str(best["tx_b64"]))
        sig = str(res.get("sig") or sig_preview)
        bundle_id = str(res.get("bundle_id") or "")
        status = wait_signature(sig, seconds=20.0)
    post = int(rpc_call(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    log(f"PGG2-V255-LIVE-SEND-RESULT transport={args.transport} sig={sig} bundle_id={bundle_id} status={status}")
    log(f"PGG2-V255-FINAL-WALLET pre={pre} post={post} delta={post - pre}")
    if post < pre:
        log("PGG2-V255-HARD-FAIL reason=negative_wallet_delta")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
