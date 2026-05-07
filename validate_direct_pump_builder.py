from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from birth_first_sniper import DATA_DIR, SOL_MINT, BotConfig, env_str, load_dotenv, short_addr
from pgg2_direct_pump import DirectPumpQuoteBroker, PUMP_FEE_PROGRAM_ID, PUMP_PROGRAM_ID


def latest_open_mints(decisions: Path, limit: int) -> list[str]:
    seen: list[str] = []
    if not decisions.is_file():
        return seen
    with decisions.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "open" and row.get("mint"):
                mint = str(row["mint"])
                if mint not in seen:
                    seen.append(mint)
    return seen[-limit:]


def rpc_headers() -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": env_str(
            "PGG2_HTTP_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ),
    }
    api_key = env_str("SOLANATRACKER_API_KEY") or env_str("SOLANATRACKER_RPC_KEY")
    if api_key:
        headers["x-api-key"] = api_key
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def direct_rpc_url() -> str:
    rpc_url = env_str("PGG2_LIVE_RPC_URL") or env_str("SOLANATRACKER_RPC_HTTP")
    api_key = env_str("SOLANATRACKER_API_KEY") or env_str("SOLANATRACKER_RPC_KEY")
    if not rpc_url:
        rpc_url = "https://rpc-mainnet.solanatracker.io/"
        if api_key:
            rpc_url += f"?api_key={api_key}"
    return rpc_url


def rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    retries = max(0, int(os.environ.get("PGG2_LIVE_HTTP_RETRIES", "2")))
    base_sleep = max(0.0, float(os.environ.get("PGG2_LIVE_HTTP_RETRY_BASE_SEC", "0.25")))
    out: dict[str, Any] = {}
    for attempt in range(retries + 1):
        req = Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=rpc_headers(),
        )
        try:
            out = json.loads(urlopen(req, timeout=20).read())
            break
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                body = exc.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"http {exc.code} {exc.reason}: {body}") from exc
            time.sleep(base_sleep * (2 ** attempt))
    if out.get("error"):
        raise RuntimeError(f"rpc {method} error: {out['error']}")
    return out.get("result")


def observed_pairs_from_raw(raw_path: Path, mints: list[str], rpc_url: str) -> dict[str, tuple[str, str]]:
    sigs_by_mint: dict[str, list[str]] = {mint: [] for mint in mints}
    seen: set[str] = set()
    if not raw_path.is_file():
        return {}
    with raw_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            sig = str(row.get("sig") or "")
            if mint not in sigs_by_mint or not sig or sig in seen:
                continue
            if row.get("side") == "buy" and row.get("instruction_kind") in {"buy", "buy_exact_sol_in"}:
                sigs_by_mint[mint].append(sig)
                seen.add(sig)

    pairs: dict[str, tuple[str, str]] = {}
    pump_program = str(PUMP_PROGRAM_ID)
    fee_program = str(PUMP_FEE_PROGRAM_ID)
    max_sigs = int(os.environ.get("PGG2_DIRECT_OBSERVED_PAIR_MAX_SIGS", "6"))
    sleep_sec = float(os.environ.get("PGG2_DIRECT_OBSERVED_PAIR_RPC_SLEEP_SEC", "0.35"))
    for mint, sigs in sigs_by_mint.items():
        for sig in list(reversed(sigs))[:max_sigs]:
            try:
                tx = rpc_call(
                    rpc_url,
                    "getTransaction",
                    [
                        sig,
                        {
                            "encoding": "json",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
            except Exception as exc:
                print(f"{short_addr(mint)} observed_pair_fetch_warn {type(exc).__name__}: {exc}")
                time.sleep(max(0.0, sleep_sec))
                continue
            time.sleep(max(0.0, sleep_sec))
            if not tx:
                continue
            msg = ((tx.get("transaction") or {}).get("message") or {})
            meta = tx.get("meta") or {}
            keys = list(msg.get("accountKeys") or [])
            loaded = meta.get("loadedAddresses") or {}
            keys.extend(loaded.get("writable") or [])
            keys.extend(loaded.get("readonly") or [])
            for ix in msg.get("instructions") or []:
                pid_idx = ix.get("programIdIndex")
                if not isinstance(pid_idx, int) or pid_idx >= len(keys) or keys[pid_idx] != pump_program:
                    continue
                accounts = list(ix.get("accounts") or [])
                fee_positions = [
                    pos for pos, account_idx in enumerate(accounts)
                    if isinstance(account_idx, int) and account_idx < len(keys) and keys[account_idx] == fee_program
                ]
                if not fee_positions:
                    continue
                extras = [
                    keys[account_idx]
                    for account_idx in accounts[fee_positions[-1] + 1:]
                    if isinstance(account_idx, int) and account_idx < len(keys)
                ]
                if len(extras) >= 2:
                    pairs[mint] = (extras[0], extras[1])
                    break
            if mint in pairs:
                break
    return pairs


def write_pair_cache(path: Path, pairs: dict[str, tuple[str, str]]) -> None:
    rows: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                rows = raw
        except Exception:
            rows = {}
    now = time.time()
    for mint, pair in pairs.items():
        rows[mint] = {
            "buyback_fee_recipient": pair[0],
            "social_fee_pda": pair[1],
            "source": "observed_raw_rpc",
            "ts": now,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def make_config() -> BotConfig:
    tmp = Path(tempfile.gettempdir())
    return BotConfig(
        paper_trading=False,
        live_enabled=True,
        state_file=tmp / "pgg2_direct_validate_state.json",
        raw_events_file=tmp / "pgg2_direct_validate_raw.jsonl",
        decisions_file=tmp / "pgg2_direct_validate_decisions.jsonl",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate direct Pump/PumpSwap quote tx building without sending")
    parser.add_argument("--mint", action="append", default=[], help="Mint to validate. May be passed multiple times.")
    parser.add_argument("--decisions", default="", help="Decision JSONL to mine recent opened mints from.")
    parser.add_argument("--limit", type=int, default=3, help="Recent opened mints to validate when --mint is omitted.")
    parser.add_argument("--amount-sol", type=float, default=0.005, help="Buy quote amount in SOL.")
    parser.add_argument("--simulate", action="store_true", help="RPC simulate the signed buy transaction.")
    parser.add_argument("--raw", default="", help="Raw JSONL tape to mine observed Pump buyback/social-fee accounts from.")
    parser.add_argument("--write-cache", default="", help="Write observed Pump remaining-account pairs to this JSON cache.")
    args = parser.parse_args()

    load_dotenv()
    os.environ.setdefault("PGG2_EXECUTION_MODE", "quote")
    os.environ.setdefault("PGG2_ENABLE_LIVE", "1")
    os.environ.setdefault("PIGGY_PAPER_TRADING", "0")
    os.environ.setdefault("PGG2_LIVE_BROKER", "direct_pump")
    os.environ.setdefault("PGG2_QUOTE_SHADOW_POSITIONS", "1")
    os.environ.setdefault("PGG2_QUOTE_SIMULATE", "1")
    os.environ.setdefault("PGG2_LIVE_SIMULATE_BEFORE_SEND", "1")
    os.environ.setdefault("PGG2_LIVE_MAX_TRADE_SOL", str(args.amount_sol))
    os.environ.setdefault("PGG2_LIVE_MIN_TRADE_SOL", "0.0005")
    os.environ.setdefault("PGG2_LIVE_MIN_WALLET_RESERVE_SOL", "0.0")

    mints = list(args.mint)
    if not mints and args.decisions:
        mints = latest_open_mints(Path(args.decisions), args.limit)
    if not mints:
        raise SystemExit("no mints supplied; pass --mint or --decisions")

    observed_pairs: dict[str, tuple[str, str]] = {}
    if args.raw:
        observed_pairs = observed_pairs_from_raw(Path(args.raw), mints, direct_rpc_url())
    if args.write_cache and observed_pairs:
        write_pair_cache(Path(args.write_cache), observed_pairs)
        print(f"wrote_observed_pair_cache={args.write_cache} rows={len(observed_pairs)}")

    broker = DirectPumpQuoteBroker(make_config())
    print(f"wallet={short_addr(broker.public_key)} mode=direct_quote send=0 amount_sol={args.amount_sol:.6f}")
    print("mint route amount_out min_out fee fee_bps tx_bytes simulate observed_pair")
    ok_count = 0
    for mint in mints:
        sim = "SKIP"
        pair = observed_pairs.get(mint)
        old_recipient = os.environ.get("PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT")
        old_social = os.environ.get("PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA")
        if pair:
            os.environ["PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT"] = pair[0]
            os.environ["PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA"] = pair[1]
        try:
            quote = broker.build_swap(SOL_MINT, mint, args.amount_sol, broker.buy_slippage)
            signed_b64, _ = broker.sign_transaction(str(quote["txn"]))
            if args.simulate:
                sim = "OK" if broker.simulate_signed(signed_b64) else "FAIL"
            rate: dict[str, Any] = quote.get("rate") or {}
            print(
                f"{short_addr(mint)} {quote.get('route')} "
                f"{float(rate.get('amountOut') or 0):.9f} "
                f"{float(rate.get('minAmountOut') or 0):.9f} "
                f"{float(rate.get('fee') or 0):.9f} "
                f"{float(rate.get('feeBps') or 0):.1f} "
                f"{len(str(quote['txn']))} {sim} "
                f"{short_addr(pair[0]) + '/' + short_addr(pair[1]) if pair else '-'}"
            )
            if sim in {"OK", "SKIP"}:
                ok_count += 1
        except Exception as exc:
            print(f"{short_addr(mint)} ERROR {type(exc).__name__}: {exc}")
        finally:
            if old_recipient is None:
                os.environ.pop("PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT", None)
            else:
                os.environ["PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT"] = old_recipient
            if old_social is None:
                os.environ.pop("PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA", None)
            else:
                os.environ["PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA"] = old_social
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
