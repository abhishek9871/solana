#!/usr/bin/env python3
"""V246 wallet/token sanity check. No secrets, no sends."""
from __future__ import annotations

import json
import time
import urllib.request

WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
RPC = "https://api.mainnet-beta.solana.com"


def rpc(method: str, params: list[object]) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        out = json.loads(resp.read().decode("utf-8"))
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
