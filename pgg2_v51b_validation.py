"""V51B v2 broker validation — build, decode, simulate. No send."""
from __future__ import annotations
import base64, json, os, sys, time
from urllib import request as urlreq
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

sys.path.insert(0, "/root/piggy")
from pgg2_pump_v2_idl_constants import (
    BUY_V2_DISCRIMINATOR, SELL_V2_DISCRIMINATOR,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID, SYSTEM_PROGRAM_ID,
)


def create_idempotent_ata_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey, ata_pk: Pubkey) -> Instruction:
    """create_idempotent_associated_token_account: ATA program variant 1."""
    metas = [
        AccountMeta(payer, True, True),
        AccountMeta(ata_pk, False, True),
        AccountMeta(owner, False, False),
        AccountMeta(mint, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(token_program, False, False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes([1]), metas)
from pgg2_pump_v2_accounts import resolve_v2_accounts_sol_paired, V2AccountsError
from pgg2_pump_v2_builder import (
    build_buy_v2_ix, build_sell_v2_ix,
    decode_buy_v2_guard, decode_sell_v2_guard,
)


def load_env(path="/root/piggy/.env"):
    env = {}
    for line in open(path):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def rpc(url, method, params, timeout=8.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urlreq.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def find_live_pump_candidate(helius_url, st_http=None):
    """Find a currently-active pump.fun bonding curve mint by sampling recent
    Pump program transactions and extracting the most recent buy/sell mint."""
    # use Helius getSignaturesForAddress on the Pump program
    PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    r = rpc(helius_url, "getSignaturesForAddress", [PUMP, {"limit": 30}])
    sigs = [s["signature"] for s in r.get("result", [])][:30]
    for sig in sigs:
        try:
            tx = rpc(helius_url, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            v = tx.get("result")
            if not v: continue
            keys = v["transaction"]["message"]["accountKeys"]
            # Filter out the bonding curve mint by checking the post token balances
            for tb in v["meta"].get("postTokenBalances", []):
                mint = tb.get("mint", "")
                owner_prog = tb.get("programId", "")
                if mint.endswith("pump") and len(mint) >= 32:
                    # Verify the bonding curve still exists and not complete
                    mint_pk = Pubkey.from_string(mint)
                    try:
                        accs = resolve_v2_accounts_sol_paired(helius_url, mint_pk, Pubkey.from_string("Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"))
                        # Check bonding curve isn't complete
                        bc_pk = accs["bonding_curve"]
                        bcr = rpc(helius_url, "getAccountInfo", [str(bc_pk), {"encoding": "base64", "commitment": "processed"}])
                        bv = bcr.get("result", {}).get("value")
                        if not bv: continue
                        bdata = base64.b64decode(bv["data"][0])
                        if bool(bdata[48]):  # complete flag
                            continue
                        return mint
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def load_keypair():
    """Load wallet keypair from /root/piggy/live_wallet.key (base58 string)."""
    path = "/root/piggy/live_wallet.key"
    raw = open(path).read().strip()
    # Try base58 first (the actual format used)
    try:
        return Keypair.from_base58_string(raw)
    except Exception:
        pass
    try:
        return Keypair.from_json(raw)
    except Exception:
        pass
    try:
        arr = json.loads(raw)
        return Keypair.from_bytes(bytes(arr))
    except Exception:
        pass
    raise RuntimeError(f"no_keypair_loaded path={path}")


def main():
    env = load_env()
    api_key = env["HELIUS_API_KEY"]
    helius_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    kp = load_keypair()
    user = kp.pubkey()
    print(f"USER={user}")

    mint_str = find_live_pump_candidate(helius_url)
    if not mint_str:
        print("ERROR: no live pump candidate found")
        sys.exit(1)
    print(f"CANDIDATE_MINT={mint_str}")
    mint = Pubkey.from_string(mint_str)

    # Resolve accounts
    accs = resolve_v2_accounts_sol_paired(helius_url, mint, user)
    print(f"BASE_TOKEN_PROGRAM={accs['base_token_program']}")
    print(f"BONDING_CURVE={accs['bonding_curve']}")
    print(f"CREATOR={accs['_creator']}")
    print(f"ACCOUNTS_COUNT={len(accs) - 1}")  # minus _creator helper

    # Build buy_v2 for 0.005 SOL purchase. amount_base_raw is target tokens.
    # We need to read bonding curve to compute expected tokens.
    bcr = rpc(helius_url, "getAccountInfo", [str(accs["bonding_curve"]), {"encoding": "base64", "commitment": "processed"}])
    bdata = base64.b64decode(bcr["result"]["value"]["data"][0])
    vtok = int.from_bytes(bdata[8:16], "little")
    vsol = int.from_bytes(bdata[16:24], "little")
    print(f"BC_VTOK={vtok} VSOL={vsol}")

    # Target: buy 0.005 SOL of tokens. With ~1.05% protocol+creator fee,
    # net_to_curve ~= 0.005 * (1-0.0105) lamports
    SOL = 5_000_000  # 0.005 SOL in lamports
    net_for_curve = int(SOL * (1 - 0.0105))
    expected_tokens = net_for_curve * vtok // (vsol + net_for_curve)
    amount_base_raw = int(expected_tokens * 0.95)  # set buy target to 95% of expected (slippage)
    max_sol_cost = int(SOL * 1.10)  # accept up to 10% above 0.005 SOL incl fees
    print(f"EXPECTED_TOKENS={expected_tokens} TARGET_AMOUNT={amount_base_raw} MAX_SOL_COST={max_sol_cost}")

    buy_ix = build_buy_v2_ix(accs, amount_base_raw, max_sol_cost)
    print(f"BUY_IX_DATA_LEN={len(bytes(buy_ix.data))} ACCOUNT_METAS={len(buy_ix.accounts)}")
    # Decode
    decoded_buy = decode_buy_v2_guard(bytes(buy_ix.data))
    print(f"BUY_DECODED={decoded_buy}")
    assert decoded_buy["amount"] == amount_base_raw, "buy decode amount mismatch"
    assert decoded_buy["max_sol_cost"] == max_sol_cost, "buy decode max_sol_cost mismatch"
    print("BUY_V2_DECODE_PASS=true")

    # Build sell_v2 — for the hypothetical case where we sold all amount_base_raw tokens
    # min_sol_output = 50% of expected (very wide slippage for emergency)
    sell_amount = amount_base_raw
    sell_min_sol = max(1, int(SOL * 0.50))
    sell_ix = build_sell_v2_ix(accs, sell_amount, sell_min_sol)
    print(f"SELL_IX_DATA_LEN={len(bytes(sell_ix.data))} ACCOUNT_METAS={len(sell_ix.accounts)}")
    decoded_sell = decode_sell_v2_guard(bytes(sell_ix.data))
    print(f"SELL_DECODED={decoded_sell}")
    assert decoded_sell["amount"] == sell_amount
    assert decoded_sell["min_sol_output"] == sell_min_sol
    print("SELL_V2_DECODE_PASS=true")

    # Simulate the buy via Helius simulateTransaction
    blockhash_resp = rpc(helius_url, "getLatestBlockhash", [{"commitment": "processed"}])
    blockhash_str = blockhash_resp["result"]["value"]["blockhash"]
    bh = Hash.from_string(blockhash_str)

    cu_limit = set_compute_unit_limit(400_000)
    cu_price = set_compute_unit_price(100_000)
    # Prepend create_idempotent_ata for the user's base ATA (Token-2022 mint).
    create_base_ata = create_idempotent_ata_ix(
        payer=user, owner=user, mint=mint,
        token_program=accs["base_token_program"],
        ata_pk=accs["associated_base_user"],
    )
    msg = MessageV0.try_compile(user, [cu_limit, cu_price, create_base_ata, buy_ix], [], bh)
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()
    print(f"BUY_TX_LEN={len(tx_bytes)}")

    sim = rpc(helius_url, "simulateTransaction", [tx_b64, {"encoding": "base64", "commitment": "processed", "sigVerify": False, "replaceRecentBlockhash": True}])
    sim_v = sim.get("result", {}).get("value", {})
    err = sim_v.get("err")
    logs = sim_v.get("logs", [])
    units = sim_v.get("unitsConsumed", 0)
    print(f"BUY_SIM_ERR={err}")
    print(f"BUY_SIM_UNITS={units}")
    if err:
        print("BUY_SIM_LAST_LOGS:")
        for l in logs[-15:]:
            print("  " + l)
    else:
        print("BUY_V2_SIM_PASS=true")
        print("BUY_SIM_LAST_LOGS:")
        for l in logs[-8:]:
            print("  " + l)

    return 0


if __name__ == "__main__":
    sys.exit(main())
