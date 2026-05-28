#!/usr/bin/env python3
"""V245 fast in-process V244 oracle.

Avoids subprocess-per-candidate latency. Reuses one broker/cache, applies the
V244 static LUT, tries small exact-quote rounding buffers, and promotes only
single-tx bundles with positive exact simulated wallet delta.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _load_env, _make_broker
from pgg2_v224_pumpswap_multipool_builder import build_explicit_multipool_tx
from pgg2_direct_pump import as_pubkey

READ_RPC = os.environ.get("V245_READ_RPC", "https://public.rpc.solanavibestation.com")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def rows(path: pathlib.Path, limit: int, max_per_mint: int) -> list[dict[str, Any]]:
    out = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if raw.strip():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    out.sort(key=lambda r: int(r.get("edge_lamports", 0)), reverse=True)
    if max_per_mint <= 0:
        return out[:limit]
    picked: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in out:
        mint = str(row.get("mint") or "")
        count = counts.get(mint, 0)
        if count >= max_per_mint:
            continue
        picked.append(row)
        counts[mint] = count + 1
        if len(picked) >= limit:
            break
    return picked


def _redact_rpc(url: str) -> str:
    if "helius-rpc.com" in url:
        return "helius"
    if "solanavibestation.com" in url:
        return "solanavibestation"
    if "rpcfast.com" in url:
        return "rpcfast"
    return url.split("?", 1)[0].replace("https://", "").replace("http://", "")


def _sim_bundle_rpcs() -> list[str]:
    vals: list[str] = []
    for raw in (
        os.environ.get("V245_SIM_BUNDLE_RPCS", ""),
        os.environ.get("V245_SIM_BUNDLE_RPC", ""),
    ):
        for item in raw.split(","):
            item = item.strip()
            if item:
                vals.append(item)
    helius_url = (os.environ.get("HELIUS_RPC_URL") or os.environ.get("SOLANA_RPC_URL") or "").strip()
    if "helius-rpc.com" in helius_url:
        vals.append(helius_url)
    helius_key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if helius_key:
        vals.append(f"https://mainnet.helius-rpc.com/?api-key={helius_key}")
    vals.append("https://public.rpc.solanavibestation.com")
    out: list[str] = []
    seen: set[str] = set()
    for val in vals:
        if val not in seen:
            out.append(val)
            seen.add(val)
    return out


def _standard_sim_rpcs() -> list[str]:
    vals: list[str] = []
    for raw in (
        os.environ.get("V245_STANDARD_SIM_RPCS", ""),
        os.environ.get("V245_READ_RPC", ""),
        os.environ.get("HELIUS_RPC_URL", ""),
        os.environ.get("SOLANA_RPC_URL", ""),
    ):
        for item in raw.split(","):
            item = item.strip()
            if item:
                vals.append(item)
    helius_key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if helius_key:
        vals.append(f"https://mainnet.helius-rpc.com/?api-key={helius_key}")
    vals.append("https://api.mainnet-beta.solana.com")
    out: list[str] = []
    seen: set[str] = set()
    for val in vals:
        if val not in seen:
            out.append(val)
            seen.add(val)
    return out


def _rpc_call(rpc_url: str, method: str, params: list[Any], timeout: float = 20.0) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def _simulate_transaction_one(tx_b64: str, payer: str, standard_rpcs: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    for rpc_url in standard_rpcs:
        try:
            bal_out = _rpc_call(rpc_url, "getBalance", [payer, {"commitment": "processed"}], timeout=12.0)
            if bal_out.get("error"):
                errors.append(f"{_redact_rpc(rpc_url)}:getBalance:{str(bal_out['error'])[:160]}")
                continue
            pre_lamports = int(((bal_out.get("result") or {}).get("value") or 0))
            sim_out = _rpc_call(
                rpc_url,
                "simulateTransaction",
                [
                    tx_b64,
                    {
                        "encoding": "base64",
                        "sigVerify": False,
                        "replaceRecentBlockhash": True,
                        "commitment": "processed",
                        "accounts": {"encoding": "base64", "addresses": [payer]},
                    },
                ],
                timeout=20.0,
            )
            if sim_out.get("error"):
                errors.append(f"{_redact_rpc(rpc_url)}:simulateTransaction:{str(sim_out['error'])[:180]}")
                continue
            value = sim_out.get("result", {}).get("value", {})
            accounts = value.get("accounts") or []
            if not accounts or not accounts[0]:
                errors.append(f"{_redact_rpc(rpc_url)}:simulateTransaction:no_account_capture")
                continue
            post_lamports = int(accounts[0].get("lamports") or 0)
            return {
                "ok": value.get("err") is None,
                "summary": "succeeded" if value.get("err") is None else "failed",
                "wallet_delta_lamports": post_lamports - pre_lamports,
                "tx_errs": [value.get("err")],
                "units": [value.get("unitsConsumed")],
                "rpc": _redact_rpc(rpc_url) + ":simulateTransaction",
            }
        except urllib.error.HTTPError as exc:
            errors.append(f"{_redact_rpc(rpc_url)}:HTTP{exc.code}")
        except Exception as exc:
            errors.append(f"{_redact_rpc(rpc_url)}:{type(exc).__name__}:{str(exc)[:160]}")
    raise RuntimeError("; ".join(errors[-5:]) or "all_standard_sim_rpcs_failed")


def _build_static_tip_tx_b64(broker: Any, tip_account: str, lamports: int) -> str:
    return broker.compile_tx(
        [
            transfer(
                TransferParams(
                    from_pubkey=as_pubkey(broker.public_key),
                    to_pubkey=as_pubkey(tip_account),
                    lamports=int(lamports),
                )
            )
        ]
    )


def simulate_bundle_exact(txs_b64: list[str], sim_rpcs: list[str], standard_rpcs: list[str]) -> dict[str, Any]:
    payer = str(VersionedTransaction.from_bytes(base64.b64decode(txs_b64[0])).message.account_keys[0])
    acct = {"addresses": [payer], "encoding": "base64"}
    acct_configs = [acct for _ in txs_b64]
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateBundle",
        "params": [
            {"encodedTransactions": txs_b64},
            {
                "preExecutionAccountsConfigs": acct_configs,
                "postExecutionAccountsConfigs": acct_configs,
                "transactionEncoding": "base64",
                "skipSigVerify": True,
                "replaceRecentBlockhash": True,
            },
        ],
    }
    errors: list[str] = []
    for rpc_url in sim_rpcs:
        try:
            req = urllib.request.Request(rpc_url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            out = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            if out.get("error"):
                errors.append(f"{_redact_rpc(rpc_url)}:{str(out['error'])[:240]}")
                continue
            rpc_label = _redact_rpc(rpc_url)
            break
        except urllib.error.HTTPError as exc:
            errors.append(f"{_redact_rpc(rpc_url)}:HTTP{exc.code}")
            continue
        except Exception as exc:
            errors.append(f"{_redact_rpc(rpc_url)}:{type(exc).__name__}:{str(exc)[:160]}")
            continue
    else:
        if len(txs_b64) != 1:
            raise RuntimeError("; ".join(errors[-5:]) or "all_bundle_sim_rpcs_failed")
        tx_b64 = txs_b64[0]
        return _simulate_transaction_one(tx_b64, payer, standard_rpcs)
    value = ((out.get("result") or {}).get("value") or {})
    txres = value.get("transactionResults") or []
    delta = None
    if txres:
        pre = txres[0].get("preExecutionAccounts") or []
        post = txres[-1].get("postExecutionAccounts") or []
        if pre and post and pre[0] and post[0]:
            delta = int(post[0]["lamports"]) - int(pre[0]["lamports"])
    return {
        "ok": value.get("summary") == "succeeded",
        "summary": value.get("summary"),
        "wallet_delta_lamports": delta,
        "tx_errs": [x.get("err") for x in txres],
        "units": [x.get("unitsConsumed") for x in txres],
        "rpc": rpc_label,
    }


def simulate_one(tx_b64: str, sim_rpcs: list[str], standard_rpcs: list[str]) -> dict[str, Any]:
    return simulate_bundle_exact([tx_b64], sim_rpcs, standard_rpcs)


def _first_instruction_error(tx_errs: Any) -> tuple[int | None, int | None]:
    for err in tx_errs or []:
        if not isinstance(err, dict):
            continue
        data = err.get("InstructionError")
        if not isinstance(data, list) or len(data) < 2:
            continue
        try:
            ix = int(data[0])
        except Exception:
            ix = None
        detail = data[1]
        code = None
        if isinstance(detail, dict) and "Custom" in detail:
            try:
                code = int(detail["Custom"])
            except Exception:
                code = None
        return ix, code
    return None, None


def _instruction_phase(ix: int | None) -> str:
    # V224 single-tx layout with compute budget disabled:
    # 0 create base ATA, 1 create WSOL ATA, 2 transfer, 3 sync, 4 buy, 5 sell.
    return {4: "buy", 5: "sell"}.get(ix, "other")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-jsonl", required=True)
    ap.add_argument("--lut-json", default="/root/piggy/data/v244_static_lut.json")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--max-per-mint", type=int, default=2)
    ap.add_argument("--tip-lamports", type=int, default=1000)
    ap.add_argument("--min-profit-lamports", type=int, default=6000)
    ap.add_argument("--min-positive-delta-lamports", type=int, default=1)
    ap.add_argument("--buy-mode", choices=["exact_quote_in", "exact_base_out"], default="exact_base_out")
    ap.add_argument("--quote-cushion-lamports", type=int, default=10)
    ap.add_argument("--quote-cushions", default="")
    ap.add_argument("--buffers", default="2,3,4,6,8,12,16,24,32")
    ap.add_argument("--out-best-json", default="/root/piggy/data/v245_best_single_tx.json")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm-live", default="")
    args = ap.parse_args()

    os.environ["HELIUS_RPC_URL"] = READ_RPC
    os.environ["SOLANA_RPC_URL"] = READ_RPC
    os.environ["V224_BUY_MODE"] = args.buy_mode
    os.environ["V224_COMPUTE_BUDGET_MODE"] = "none"
    os.environ["V224_QUOTE_IN_REMAINING"] = "1"
    os.environ["V224_EXACT_BASE_REMAINING"] = "1"
    os.environ["V224_ADDRESS_LOOKUP_TABLE_JSON"] = args.lut_json
    # Atomic multipool scalps must not initialize the PumpSwap volume PDA.
    # That PDA rent is not recovered in the single transaction and turns
    # otherwise positive exact simulations into wallet-delta losses.
    os.environ["PGG2_DIRECT_TRACK_VOLUME"] = "0"
    os.environ["PGG2_DIRECT_PRIORITY_FEE_SOL"] = "0"
    os.environ["PGG2_DIRECT_COMPUTE_UNIT_PRICE_MICROLAMPORTS"] = "0"
    _load_env()
    sim_rpcs = _sim_bundle_rpcs()
    standard_rpcs = _standard_sim_rpcs()
    broker = _make_broker()
    broker.rpc_url = READ_RPC
    broker.refresh_blockhash_cache()
    candidates = rows(pathlib.Path(args.candidates_jsonl), args.limit, args.max_per_mint)
    bundle_tip_lamports = max(0, int(args.tip_lamports))
    tip_tx_fee_est_lamports = int(os.environ.get("V245_TIP_TX_FEE_EST_LAMPORTS", "5000") or 5000)
    embedded_tip_lamports = 0
    buffers = [int(x) for x in args.buffers.split(",") if x.strip()]
    if args.buy_mode == "exact_base_out":
        raw_qcs = args.quote_cushions or str(int(args.quote_cushion_lamports))
        buffers = [int(x) for x in raw_qcs.split(",") if x.strip()]
    log(
        f"PGG2-V245-FAST-START candidates={len(candidates)} buy_mode={args.buy_mode} "
        f"buffers={buffers} buffer_kind={'quote_cushion' if args.buy_mode == 'exact_base_out' else 'quote_in_rounding'} "
        f"tip={args.tip_lamports} min_profit={args.min_profit_lamports} "
        f"max_per_mint={args.max_per_mint} "
        f"sim_rpcs={[ _redact_rpc(x) for x in sim_rpcs ]} "
        f"standard_sim_rpcs={[ _redact_rpc(x) for x in standard_rpcs ]}"
    )
    best = None
    structurally_bad_mints: dict[str, str] = {}
    for idx, cand in enumerate(candidates, start=1):
        mint_key = str(cand.get("mint") or "")
        if mint_key in structurally_bad_mints:
            log(
                f"PGG2-V245-MINT-SKIP idx={idx} mint={mint_key[:4]}.. "
                f"reason={structurally_bad_mints[mint_key]}"
            )
            continue
        candidate_structural_reason = ""
        for buf in buffers:
            if args.buy_mode == "exact_base_out":
                os.environ["V224_EXACT_BASE_QUOTE_CUSHION_LAMPORTS"] = str(buf)
            else:
                os.environ["V224_QUOTE_IN_NET_LAMPORT_BUFFER"] = str(buf)
            try:
                meta = build_explicit_multipool_tx(
                    broker=broker,
                    mint=str(cand["mint"]),
                    buy_pool_key=str(cand["buy_pool"]),
                    sell_pool_key=str(cand["sell_pool"]),
                    size_lamports=int(cand["size_lamports"]),
                    min_profit_lamports=int(args.min_profit_lamports)
                    + int(bundle_tip_lamports)
                    + int(tip_tx_fee_est_lamports),
                    fee_buffer_lamports=0,
                    projection_buffer_lamports=0,
                    tip_lamports=int(embedded_tip_lamports),
                )
            except Exception as exc:
                err = f"{type(exc).__name__}:{str(exc)[:160]}"
                if buf == buffers[0]:
                    log(f"PGG2-V245-BUILD-BLOCK idx={idx} buf={buf} mint={str(cand.get('mint'))[:4]}.. edge={cand.get('edge_lamports')} err={err}")
                if args.buy_mode != "exact_base_out":
                    break
                if "not_executable" in err or "exact_base_buy_zero" in err:
                    break
                continue
            tx_b64 = str(meta.pop("tx_b64"))
            txs_b64 = [tx_b64]
            if bundle_tip_lamports > 0:
                tip_account = os.environ.get("PGG2_JITO_TIP_ACCOUNT") or os.environ.get("JITO_TIP_ACCOUNT") or ""
                if not tip_account:
                    from pgg2_v109_no_send_live_bundle_validation import _ensure_tip_account

                    tip_account = _ensure_tip_account()
                tip_tx_b64 = _build_static_tip_tx_b64(broker, tip_account, bundle_tip_lamports)
                txs_b64.append(tip_tx_b64)
                meta["bundle_tip_lamports"] = int(bundle_tip_lamports)
                meta["tip_tx_fee_est_lamports"] = int(tip_tx_fee_est_lamports)
                meta["tip_tx_raw_len"] = len(base64.b64decode(tip_tx_b64))
            projected_edge = int(meta.get("projected_edge_lamports") or 0)
            if projected_edge < int(args.min_positive_delta_lamports):
                log(
                    f"PGG2-V245-PROJECTED-BELOW-MIN idx={idx} buf={buf} "
                    f"mint={mint_key[:4]}.. projected={projected_edge} "
                    f"min_positive={int(args.min_positive_delta_lamports)}"
                )
                break
            raw_len = len(base64.b64decode(tx_b64))
            if raw_len > 1232:
                log(f"PGG2-V245-SIZE-BLOCK idx={idx} buf={buf} mint={str(cand.get('mint'))[:4]}.. raw_len={raw_len}")
                continue
            try:
                sim = simulate_bundle_exact(txs_b64, sim_rpcs, standard_rpcs)
            except Exception as exc:
                log(f"PGG2-V245-SIM-RPC-BLOCK idx={idx} buf={buf} mint={str(cand.get('mint'))[:4]}.. err={type(exc).__name__}:{str(exc)[:160]}")
                continue
            log(
                f"PGG2-V245-EXACT-SIM idx={idx} buf={buf} mint={str(cand.get('mint'))[:4]}.. "
                f"edge={cand.get('edge_lamports')} projected={meta.get('projected_edge_lamports')} raw_len={raw_len} "
                f"delta={sim.get('wallet_delta_lamports')} ok={int(bool(sim.get('ok')))} "
                f"rpc={sim.get('rpc')} errs={sim.get('tx_errs')}"
            )
            delta = sim.get("wallet_delta_lamports")
            if sim.get("ok") and delta is not None and int(delta) >= int(args.min_positive_delta_lamports):
                best = {"candidate": cand, "meta": meta, "sim": sim, "txs_b64": txs_b64, "buffer": buf}
                break
            if sim.get("tx_errs"):
                ix, code = _first_instruction_error(sim.get("tx_errs"))
                phase = _instruction_phase(ix)
                err_text = str(sim.get("tx_errs"))
                # PumpSwap 6004/6040 are slippage/math failures for the specific
                # encoded amount, not a bad mint/pool layout. Keep testing the
                # configured cushions instead of suppressing the mint for the
                # rest of the cycle.
                if ix in (4, 5) and code in (6004, 6040):
                    log(
                        f"PGG2-V245-SLIPPAGE-BUFFER-MISS idx={idx} buf={buf} "
                        f"mint={mint_key[:4]}.. phase={phase} code={code}"
                    )
                    continue
                # Only true account/layout-style errors should arm a mint skip.
                if ix in (4, 5) and code in (6014, 6015):
                    candidate_structural_reason = f"{phase}_ix{ix}_custom{code}"
                    break
                if "6040" not in err_text:
                    break
        if best:
            break
        if candidate_structural_reason:
            structurally_bad_mints[mint_key] = candidate_structural_reason
            log(
                f"PGG2-V245-MINT-STRUCTURAL-SKIP-ARMED idx={idx} mint={mint_key[:4]}.. "
                f"reason={candidate_structural_reason}"
            )
    if not best:
        log("PGG2-V245-NO-EXACT-POSITIVE-SINGLE-TX")
        return 1
    pathlib.Path(args.out_best_json).write_text(json.dumps(best, indent=2, sort_keys=True), encoding="utf-8")
    log(
        f"PGG2-V245-EXACT-POSITIVE-SINGLE-TX mint={str(best['candidate'].get('mint'))[:4]}.. "
        f"delta={best['sim'].get('wallet_delta_lamports')} buffer={best['buffer']} out={args.out_best_json}"
    )
    if args.live:
        if args.confirm_live != "I_ACCEPT_V245_SINGLE_TX_ATOMIC_RISK":
            raise RuntimeError("missing_live_confirmation")
        from pgg2_v108_jito_bundle_sender import send_bundle, wait_bundle_status

        res = send_bundle(best["txs_b64"], dry_run=False)
        status = wait_bundle_status(str(res.get("bundle_id")), timeout_sec=20)
        log(f"PGG2-V245-LIVE-SEND-RESULT bundle_id={res.get('bundle_id')} status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
