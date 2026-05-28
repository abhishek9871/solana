#!/usr/bin/env python3
"""V246 wallet/token sanity check. No secrets, no sends."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"


def _load_env() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _rpc_url() -> str:
    _load_env()
    if os.environ.get("V246_WALLET_RPC"):
        return os.environ["V246_WALLET_RPC"]
    if os.environ.get("V255_READ_RPC"):
        return os.environ["V255_READ_RPC"]
    if os.environ.get("SOLANA_RPC_URL"):
        return os.environ["SOLANA_RPC_URL"]
    return "https://public.rpc.solanavibestation.com"


RPC = _rpc_url()


def rpc(method: str, params: list[object]) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 or attempt >= 3:
                raise
            time.sleep(0.4 * (attempt + 1))
    else:
        raise RuntimeError(f"wallet_rpc_failed:{last_exc}")
    if out.get("error"):
        raise RuntimeError(str(out["error"])[:200])
    return out.get("result")


def main() -> int:
    bal = rpc("getBalance", [WALLET, {"commitment": "processed"}])
    toks = rpc(
        "getTokenAccountsByOwner",
        [
            WALLET,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ],
    )
    lamports = int((bal or {}).get("value") or 0) if isinstance(bal, dict) else 0
    vals = (toks or {}).get("value") or [] if isinstance(toks, dict) else []
    nonzero = []
    for row in vals:
        info = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        amount = (((info.get("tokenAmount") or {}).get("amount")) or "0")
        if str(amount) != "0":
            nonzero.append({"account": row.get("pubkey"), "mint": info.get("mint"), "amount": amount})
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"PGG2-V246-WALLET-CHECK balance_lamports={lamports} "
        f"balance_sol={lamports/1_000_000_000:.9f} token_accounts={len(vals)} nonzero_tokens={len(nonzero)}"
    )
    for item in nonzero[:10]:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"PGG2-V246-NONZERO-TOKEN account={str(item['account'])[:4]}.. "
            f"mint={str(item['mint'])[:4]}.. amount={item['amount']}"
        )
    return 2 if nonzero else 0


if __name__ == "__main__":
    raise SystemExit(main())
