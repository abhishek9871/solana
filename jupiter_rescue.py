"""Jupiter-aggregator universal rescue.

When direct PumpSwap/pump_bc sells revert (cashback overflow, Token-2022 transfer-
fee math, drained pool, post-upgrade account-layout drift, etc.), Jupiter routes
through whatever DEX has liquidity. This is the unconditional last-resort exit.

Free public API: https://lite-api.jup.ag/swap/v1/quote and /swap

Used by:
  * rescue_all_stuck.py — falls back here when broker.build_sell or send fails
  * pgg2_live_raptor.py emergency-close path — wired as fallback if env var
    PGG2_RESCUE_JUPITER_FALLBACK=1
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

WSOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"


def _http_get(url: str, params: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{url}?{qs}", headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, body: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def jupiter_quote_sell(mint: str, amount_raw: int, slippage_bps: int = 5000) -> dict[str, Any]:
    """Get Jupiter quote for selling `amount_raw` of `mint` to WSOL.

    slippage_bps default 5000 = 50% (rescue context: we want the position out).
    """
    params = {
        "inputMint": mint,
        "outputMint": WSOL_MINT,
        "amount": amount_raw,
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    return _http_get(JUP_QUOTE_URL, params)


def jupiter_swap_tx(quote: dict[str, Any], user_pubkey: str) -> dict[str, Any]:
    """Get a built+signed-by-payer-only swap tx from Jupiter."""
    body = {
        "userPublicKey": user_pubkey,
        "quoteResponse": quote,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    return _http_post(JUP_SWAP_URL, body)


def jupiter_rescue_one(
    *,
    mint: str,
    amount_raw: int,
    keypair_path: str,
    rpc_url: str,
    slippage_bps: int = 5000,
    skip_preflight: bool = True,
    confirm_timeout_sec: float = 60.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Sell `amount_raw` of `mint` via Jupiter aggregator. Returns (ok, signature_or_err, meta)."""
    try:
        from solders.keypair import Keypair  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
    except Exception as exc:
        return False, f"solders_import_failed: {exc}", {}

    try:
        with open(keypair_path, "rb") as fh:
            blob = fh.read().strip()
        # Format detection: JSON array of bytes, raw 64-byte secret, or base58 string
        try:
            arr = json.loads(blob.decode("utf-8"))
            if isinstance(arr, list):
                kp = Keypair.from_bytes(bytes(arr))
            else:
                raise ValueError("not_a_list")
        except (ValueError, UnicodeDecodeError):
            if len(blob) == 64:
                kp = Keypair.from_bytes(blob)
            else:
                try:
                    kp = Keypair.from_base58_string(blob.decode("ascii").strip())
                except Exception as b58_exc:
                    return False, f"keypair_base58_decode_failed: {b58_exc}", {}
        user = str(kp.pubkey())
    except Exception as exc:
        return False, f"keypair_load_failed: {type(exc).__name__}: {exc}", {}

    try:
        quote = jupiter_quote_sell(mint, amount_raw, slippage_bps=slippage_bps)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")[:200]
        except Exception:
            body = ""
        return False, f"jupiter_quote_http_{exc.code}: {body}", {}
    except Exception as exc:
        return False, f"jupiter_quote_failed: {type(exc).__name__}: {str(exc)[:160]}", {}

    out_amount = int(quote.get("outAmount", 0) or 0)
    if out_amount <= 0:
        return False, f"jupiter_quote_zero_out: {quote.get('errorMessage', 'no_routes')}", {"quote": quote}

    try:
        swap_resp = jupiter_swap_tx(quote, user)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")[:200]
        except Exception:
            body = ""
        return False, f"jupiter_swap_http_{exc.code}: {body}", {"quote": quote}
    except Exception as exc:
        return False, f"jupiter_swap_failed: {type(exc).__name__}: {str(exc)[:160]}", {"quote": quote}

    tx_b64 = swap_resp.get("swapTransaction")
    if not tx_b64:
        return False, f"jupiter_swap_no_tx: {swap_resp.get('errorMessage', 'unknown')}", {"swap": swap_resp}

    try:
        raw = base64.b64decode(tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        # Sign with our keypair (solders re-signs message hash)
        signed = VersionedTransaction(unsigned.message, [kp])
        signed_b64 = base64.b64encode(bytes(signed)).decode("ascii")
    except Exception as exc:
        return False, f"sign_failed: {type(exc).__name__}: {str(exc)[:160]}", {}

    payload = {
        "jsonrpc": "2.0",
        "id": str(int(time.time() * 1000)),
        "method": "sendTransaction",
        "params": [
            signed_b64,
            {
                "encoding": "base64",
                "skipPreflight": skip_preflight,
                "preflightCommitment": "processed",
                "maxRetries": 5,
            },
        ],
    }
    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            send_resp = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return False, f"send_failed: {type(exc).__name__}: {str(exc)[:160]}", {}

    if "error" in send_resp:
        return False, f"rpc_error: {send_resp['error']}", {}
    sig = send_resp.get("result", "")
    if not sig:
        return False, "rpc_no_signature", {}

    # Wait for confirmation
    deadline = time.time() + confirm_timeout_sec
    last_status: Optional[dict[str, Any]] = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                rpc_url,
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [[sig], {"searchTransactionHistory": False}],
                }).encode("utf-8"),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            status = ((body.get("result") or {}).get("value") or [None])[0]
            if status:
                last_status = status
                if status.get("err"):
                    return False, f"on_chain_err: {status.get('err')} sig={sig}", {"sig": sig, "status": status}
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return True, sig, {"sig": sig, "out_sol": out_amount / 1e9, "status": status}
        except Exception:
            pass
        time.sleep(0.7)

    return False, f"confirm_timeout sig={sig}", {"sig": sig, "last_status": last_status}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", required=True)
    ap.add_argument("--amount-raw", type=int, required=True)
    ap.add_argument("--keypair", default="/root/piggy/live_wallet.key")
    ap.add_argument("--rpc-url", default=os.environ.get("HELIUS_API_KEY") and f"https://mainnet.helius-rpc.com/?api-key={os.environ['HELIUS_API_KEY']}")
    ap.add_argument("--slippage-bps", type=int, default=5000)
    args = ap.parse_args()

    if not args.rpc_url:
        print("ERR: pass --rpc-url or set HELIUS_API_KEY env var", flush=True)
        return 2
    print(f"rescuing {args.mint[:10]}.. amount_raw={args.amount_raw} via Jupiter", flush=True)
    ok, sig_or_err, meta = jupiter_rescue_one(
        mint=args.mint,
        amount_raw=args.amount_raw,
        keypair_path=args.keypair,
        rpc_url=args.rpc_url,
        slippage_bps=args.slippage_bps,
    )
    if ok:
        out_sol = meta.get("out_sol", 0.0)
        print(f"OK: sig={sig_or_err} out_sol={out_sol:.6f}", flush=True)
        return 0
    print(f"FAIL: {sig_or_err}", flush=True)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
