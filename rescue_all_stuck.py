"""Rescue ALL stuck Token-2022 positions in the wallet. Loops over every mint
with non-zero balance and sells via DirectPumpQuoteBroker with 50% slippage."""
from __future__ import annotations
import os, sys, json, urllib.request, time

sys.path.insert(0, "/root/piggy")

# Load .env
for raw in open("/root/piggy/.env"):
    line = raw.strip()
    if "=" not in line or line.startswith("#"): continue
    k, _, v = line.partition("=")
    v = v.strip().strip('"').strip("'")
    os.environ.setdefault(k.strip(), v)

# Configure RPC pool
_st = os.environ.get("SOLANATRACKER_RPC_HTTP", "")
_helius = "https://mainnet.helius-rpc.com/?api-key=" + os.environ.get("HELIUS_API_KEY", "")
_beta = "https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
os.environ["PGG2_RPC_POOL_ENDPOINTS"] = f"st={_st}@5|helius={_helius}@10|helius_beta={_beta}@10"
os.environ["PGG2_EXECUTION_MODE"] = "live"
os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
os.environ["PGG2_DIRECT_LIVE_CONFIRM"] = "I_ACCEPT_DIRECT_PUMP_RISK"
os.environ["PGG2_DIRECT_SELL_SLIPPAGE"] = "0.50"
# ST RPC (and some pool endpoints) reject sendTransaction with preflight enabled.
# Rescue is a stuck position - skip preflight to get the tx through.
os.environ["PGG2_LIVE_SKIP_PREFLIGHT"] = "1"
os.environ["PGG2_LIVE_MAX_RETRIES"] = "5"

WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"

def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    req = urllib.request.Request(
        _helius,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=8).read())

# List stuck positions (Token-2022 + classic Token)
stuck = []
for prog in ("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
    r = rpc("getTokenAccountsByOwner", [WALLET, {"programId": prog}, {"encoding": "jsonParsed"}])
    for acc in r.get("result", {}).get("value", []):
        info = acc["account"]["data"]["parsed"]["info"]
        ui = info["tokenAmount"].get("uiAmount")
        if ui and ui > 0:
            stuck.append((info["mint"], info["tokenAmount"]["amount"]))

print(f"STUCK COUNT: {len(stuck)}", flush=True)
for m, a in stuck:
    print(f"  {m} raw={a}", flush=True)
print(flush=True)

if not stuck:
    bal = rpc("getBalance", [WALLET])
    sol = bal["result"]["value"] / 1e9
    print(f"NOTHING TO RESCUE — wallet SOL: {sol:.6f}", flush=True)
    sys.exit(0)

from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore
from birth_first_sniper import BotConfig  # type: ignore
from solders.pubkey import Pubkey  # type: ignore

broker = DirectPumpQuoteBroker(BotConfig())
recovered_sigs = []

def _jupiter_fallback(mint: str, tokens_raw: int) -> tuple[bool, str]:
    """Last-resort: route any unsellable mint through Jupiter aggregator."""
    try:
        from jupiter_rescue import jupiter_rescue_one  # type: ignore
    except Exception as exc:
        return False, f"jupiter_module_missing: {exc}"
    ok, sig_or_err, meta = jupiter_rescue_one(
        mint=mint,
        amount_raw=tokens_raw,
        keypair_path="/root/piggy/live_wallet.key",
        rpc_url=_helius,
        slippage_bps=5000,
    )
    if ok:
        return True, f"jupiter sig={sig_or_err[:32]}... out_sol={meta.get('out_sol', 0.0):.6f}"
    return False, f"jupiter: {sig_or_err}"


for mint, raw_amt in stuck:
    print(f"rescuing {mint}", flush=True)
    direct_ok = False
    try:
        mint_pk = Pubkey.from_string(mint)
        tokens_raw = broker.token_balance_raw(mint_pk)
        if tokens_raw <= 0:
            print(f"  zero tokens, skip", flush=True); continue
        sell_quote = broker.build_sell(mint, f"raw:{tokens_raw}", 0.50)
        expected = float(broker.rate_amount_out(sell_quote))
        if hasattr(broker, "retarget_sell_min_sol"):
            sell_quote = broker.retarget_sell_min_sol(sell_quote, mint, 0.000020)
        signed, sig_pre = broker.sign_transaction(str(sell_quote["txn"]))
        sent_sig = broker.send_signed(signed)
        recovered_sigs.append(sent_sig)
        print(f"  direct expected={expected:.6f} SOL sig={sent_sig[:32]}...", flush=True)
        direct_ok = True
        time.sleep(8)
        # Check direct path landed cleanly
        try:
            url = _helius
            req = urllib.request.Request(
                url,
                data=json.dumps({"jsonrpc":"2.0","id":1,"method":"getSignatureStatuses","params":[[sent_sig],{"searchTransactionHistory":False}]}).encode(),
                headers={"content-type":"application/json"}, method="POST")
            resp = json.loads(urllib.request.urlopen(req, timeout=6).read())
            status = ((resp.get("result") or {}).get("value") or [None])[0]
            if status and status.get("err"):
                print(f"  direct on-chain err: {status.get('err')} — falling back to Jupiter", flush=True)
                direct_ok = False
        except Exception:
            pass
    except Exception as exc:
        print(f"  direct FAIL: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        direct_ok = False

    if not direct_ok:
        # Re-fetch current balance (direct may have partially drained it)
        try:
            tokens_now = broker.token_balance_raw(Pubkey.from_string(mint))
        except Exception:
            tokens_now = raw_amt
        if tokens_now > 0:
            ok, msg = _jupiter_fallback(mint, tokens_now)
            print(f"  jupiter: {msg}", flush=True)

# Wait for confirmations
print(f"\nwaiting 10s for confirmations...", flush=True)
time.sleep(10)

# Final state
r = rpc("getTokenAccountsByOwner", [WALLET, {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}, {"encoding": "jsonParsed"}])
remaining = []
for acc in r.get("result", {}).get("value", []):
    info = acc["account"]["data"]["parsed"]["info"]
    ui = info["tokenAmount"].get("uiAmount")
    if ui and ui > 0:
        remaining.append(info["mint"])
print(f"remaining stuck after rescue: {len(remaining)}", flush=True)
for m in remaining:
    print(f"  {m}", flush=True)

bal = rpc("getBalance", [WALLET])
sol = bal["result"]["value"] / 1e9
print(f"wallet SOL after: {sol:.6f}", flush=True)
