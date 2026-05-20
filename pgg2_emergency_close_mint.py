from __future__ import annotations

import json
import sys

from solders.pubkey import Pubkey

from birth_first_sniper import BotConfig, SOL_MINT
from pgg2_direct_pump import DirectPumpQuoteBroker


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: pgg2_emergency_close_mint.py <mint>", file=sys.stderr)
        return 2
    mint = sys.argv[1].strip()
    broker = DirectPumpQuoteBroker(BotConfig())
    mint_pk = Pubkey.from_string(mint)
    raw_before = broker.token_balance_raw(mint_pk)
    ui_before = broker.raw_to_ui(mint_pk, raw_before)
    if raw_before <= 0:
        print(json.dumps({"status": "no_tokens", "mint": mint, "raw_before": raw_before}))
        return 0
    quote = broker.build_swap(mint, SOL_MINT, "auto", 99.0)
    expected_out = broker.rate_amount_out(quote)
    signed_b64, _signed_b58 = broker.sign_transaction(str(quote["txn"]))
    sig = broker.send_signed_rpc(signed_b64)
    ok = broker.wait_confirmed(sig)
    wallet_delta = 0.0
    token_delta = 0.0
    if ok:
        try:
            wallet_delta = broker.transaction_wallet_delta_sol(sig)
        except Exception:
            wallet_delta = 0.0
        try:
            token_delta = broker.transaction_token_delta_ui(sig, mint)
        except Exception:
            token_delta = 0.0
    raw_after = 0
    try:
        raw_after = broker.token_balance_raw(mint_pk)
    except Exception:
        raw_after = 0
    print(
        json.dumps(
            {
                "status": "confirmed" if ok else "not_confirmed",
                "mint": mint,
                "sig": sig,
                "raw_before": raw_before,
                "ui_before": ui_before,
                "expected_out_sol": expected_out,
                "wallet_delta_sol": wallet_delta,
                "token_delta_ui": token_delta,
                "raw_after": raw_after,
            },
            sort_keys=True,
        )
    )
    return 0 if ok and raw_after == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
