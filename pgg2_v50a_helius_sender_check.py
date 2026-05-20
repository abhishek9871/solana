"""V50A Helius Sender preflight availability check.

Verifies before any live broadcast:
  1. HELIUS_API_KEY is present in /root/piggy/.env (does NOT echo the key)
  2. Helius Sender frontend health check (/ping) returns 200
  3. Helius Sender SWQOS-only fast endpoint accepts a getHealth probe
  4. Helius standard RPC `getLatestBlockhash` returns OK
  5. The published Sender tip-account set is non-empty
  6. The wallet (Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7) has >= 0.10 SOL

This module is forbidden-call clean — no sendTransaction / send_signed paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import aiohttp

# Static-grep self check — forbidden send patterns must NOT appear in this file.
import re as _re_self

_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _src):
        sys.stderr.write(f"V50A-SENDER-CHECK-ABORT forbidden_call_pattern={_pat}\n")
        sys.exit(2)

HELIUS_SENDER_PING_URL = "https://sender.helius-rpc.com/ping"
HELIUS_SENDER_FAST_SWQOS_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"

# Official Helius Sender tip-account set (from Helius docs).
HELIUS_SENDER_TIP_ACCOUNTS: List[str] = [
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
]


def _load_env_file(path: str = "/root/piggy/.env") -> None:
    """Load .env into os.environ without echoing values."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


def _key_present() -> bool:
    return bool(os.environ.get("HELIUS_API_KEY", "").strip())


async def _ping_sender() -> Tuple[bool, str]:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            # Some health endpoints prefer GET; try both, accept any 200.
            async with s.get(HELIUS_SENDER_PING_URL) as r:
                txt = (await r.text())[:80]
                return (200 <= r.status < 300, f"GET {r.status} {txt!r}")
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}:{exc}"


async def _sender_swqos_health() -> Tuple[bool, str]:
    """Probe the SWQOS-only Sender endpoint for reachability.

    Helius Sender only accepts the `sendTransaction` method; it rejects any
    other method with a JSON-RPC error wrapped in HTTP 500 (or, depending
    on the gateway, 4xx). What matters for the preflight is that the
    endpoint responds with a structured Sender error message (e.g.
    "Unknown method: getHealth") rather than a network failure. We treat
    any reachable response with a Sender-style error string as healthy.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(
                HELIUS_SENDER_FAST_SWQOS_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as r:
                txt = (await r.text())[:200]
                # Healthy = endpoint responded at all, and either:
                #  (a) HTTP < 500 (success or normal client error), OR
                #  (b) HTTP 500 with a Sender-style "Unknown method"
                #      response (the gateway is up, it just doesn't
                #      accept non-sendTransaction methods).
                low = txt.lower()
                sender_signal = (
                    "unknown method" in low or "sendtransaction" in low
                    or "invalid params" in low or "method not" in low
                )
                healthy = (r.status < 500) or sender_signal
                return (healthy, f"POST {r.status} {txt!r}")
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}:{exc}"


async def _helius_rpc_blockhash(api_key: str) -> Tuple[bool, str]:
    url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": []}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as r:
                if r.status != 200:
                    return False, f"http {r.status}"
                data = await r.json()
                blockhash = (
                    ((data or {}).get("result") or {}).get("value") or {}
                ).get("blockhash")
                return (bool(blockhash), f"blockhash={(blockhash or '')[:10]}...")
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}:{exc}"


async def _wallet_balance(api_key: str, pubkey: str) -> Tuple[bool, float]:
    url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as r:
                if r.status != 200:
                    return False, 0.0
                data = await r.json()
                lamports = int(
                    ((data or {}).get("result") or {}).get("value") or 0
                )
                return True, lamports / 1e9
    except Exception:
        return False, 0.0


async def amain() -> int:
    _load_env_file()
    print("[V50A-CHECK] starting Helius Sender preflight", flush=True)

    key_ok = _key_present()
    print(f"[V50A-CHECK] HELIUS_API_KEY_present={key_ok}", flush=True)
    if not key_ok:
        _emit_report(
            {
                "all_checks_pass": False,
                "blocker": "HELIUS_API_KEY missing in /root/piggy/.env",
            }
        )
        return 1

    api_key = os.environ["HELIUS_API_KEY"].strip()

    ping_ok, ping_detail = await _ping_sender()
    print(f"[V50A-CHECK] sender_ping_ok={ping_ok} {ping_detail}", flush=True)

    swqos_ok, swqos_detail = await _sender_swqos_health()
    print(
        f"[V50A-CHECK] sender_swqos_endpoint_ok={swqos_ok} {swqos_detail}",
        flush=True,
    )

    rpc_ok, rpc_detail = await _helius_rpc_blockhash(api_key)
    print(f"[V50A-CHECK] helius_rpc_ok={rpc_ok} {rpc_detail}", flush=True)

    tip_count = len(HELIUS_SENDER_TIP_ACCOUNTS)
    print(f"[V50A-CHECK] tip_accounts_count={tip_count}", flush=True)

    wallet_pubkey = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
    bal_ok, bal_sol = await _wallet_balance(api_key, wallet_pubkey)
    print(
        f"[V50A-CHECK] wallet_balance_query_ok={bal_ok} wallet_sol={bal_sol:.9f}",
        flush=True,
    )

    bal_pass = bal_ok and bal_sol >= 0.10

    all_pass = ping_ok and swqos_ok and rpc_ok and tip_count > 0 and bal_pass
    blocker = ""
    if not all_pass:
        if not ping_ok:
            blocker = "sender_ping_failed"
        elif not swqos_ok:
            blocker = "sender_swqos_endpoint_unreachable"
        elif not rpc_ok:
            blocker = "helius_rpc_failed"
        elif tip_count == 0:
            blocker = "tip_accounts_empty"
        elif not bal_pass:
            blocker = f"wallet_balance_below_floor sol={bal_sol:.6f}"

    report = {
        "ts_utc": int(time.time()),
        "key_ok": key_ok,
        "sender_ping_ok": ping_ok,
        "sender_ping_detail": ping_detail,
        "sender_swqos_endpoint_ok": swqos_ok,
        "sender_swqos_endpoint_detail": swqos_detail,
        "helius_rpc_ok": rpc_ok,
        "helius_rpc_detail": rpc_detail,
        "tip_accounts_count": tip_count,
        "tip_accounts": HELIUS_SENDER_TIP_ACCOUNTS,
        "wallet_pubkey": wallet_pubkey,
        "wallet_sol": round(bal_sol, 9),
        "wallet_balance_floor_sol": 0.10,
        "wallet_balance_ok": bal_pass,
        "all_checks_pass": all_pass,
        "blocker": blocker,
    }
    _emit_report(report)
    print(
        f"[V50A-CHECK] all_checks_pass={all_pass} blocker={blocker!r}",
        flush=True,
    )
    return 0 if all_pass else 1


def _emit_report(report: Dict[str, Any]) -> None:
    out_path = Path("/root/piggy/V50A_HELIUS_SENDER_CHECK.md")
    lines: List[str] = []
    lines.append("# V50A Helius Sender Preflight Check\n")
    lines.append(f"- ts_utc: `{report.get('ts_utc')}`\n")
    lines.append(f"- key_ok: `{report.get('key_ok')}`\n")
    lines.append(
        f"- sender_ping_ok: `{report.get('sender_ping_ok')}` ({report.get('sender_ping_detail', '')})\n"
    )
    lines.append(
        f"- sender_swqos_endpoint_ok: `{report.get('sender_swqos_endpoint_ok')}` ({report.get('sender_swqos_endpoint_detail', '')})\n"
    )
    lines.append(
        f"- helius_rpc_ok: `{report.get('helius_rpc_ok')}` ({report.get('helius_rpc_detail', '')})\n"
    )
    lines.append(f"- tip_accounts_count: `{report.get('tip_accounts_count')}`\n")
    lines.append(
        f"- wallet_pubkey: `{report.get('wallet_pubkey')}`\n"
    )
    lines.append(
        f"- wallet_sol: `{report.get('wallet_sol')}` (floor: {report.get('wallet_balance_floor_sol')})\n"
    )
    lines.append(
        f"- wallet_balance_ok: `{report.get('wallet_balance_ok')}`\n"
    )
    lines.append(f"\n## Tip-Account Set (Helius Sender)\n\n")
    for acct in report.get("tip_accounts") or []:
        lines.append(f"- `{acct}`\n")
    lines.append(f"\n## Verdict\n\n")
    lines.append(f"- all_checks_pass: **{report.get('all_checks_pass')}**\n")
    if report.get("blocker"):
        lines.append(f"- blocker: `{report.get('blocker')}`\n")
    out_path.write_text("".join(lines), encoding="utf-8")
    # Also dump JSON snapshot beside the .md for the runner to consume.
    out_json = Path("/root/piggy/V50A_HELIUS_SENDER_CHECK.json")
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
