#!/usr/bin/env python3
"""V285 PublicNode Yellowstone gRPC buy-train continuation no-send validator.

Observation only. It subscribes to pump.fun transactions through Yellowstone
gRPC, detects same-mint buy trains, simulates a post-train entry, applies later
same-mint events, and records whether the modeled wallet delta clears a target
profit. No wallet keypair is read and no transaction is sent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict, deque
from types import SimpleNamespace
from typing import Any, Iterator

import grpc  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.signature import Signature  # type: ignore

from pgg2_v129_sof_stagea_live_bundle import _load_env, _make_broker  # type: ignore


ROOT = "/root/piggy"
PROTO_DIR = os.path.join(ROOT, "yellowstone_proto")
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

import geyser_pb2  # type: ignore  # noqa: E402
import geyser_pb2_grpc  # type: ignore  # noqa: E402


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_BUY_EXACT_SOL_IN = bytes([56, 252, 116, 8, 158, 223, 205, 95])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
LAMPORTS_PER_SOL = 1_000_000_000
ATA_RENT_LAMPORTS = 2_039_280


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if len(s) > 10 else s


def _curve_copy(curve: Any) -> SimpleNamespace:
    return SimpleNamespace(
        key=getattr(curve, "key", None),
        virtual_token_reserves=int(curve.virtual_token_reserves),
        virtual_sol_reserves=int(curve.virtual_sol_reserves),
        real_token_reserves=int(curve.real_token_reserves),
        real_sol_reserves=int(curve.real_sol_reserves),
        token_total_supply=int(curve.token_total_supply),
        complete=bool(curve.complete),
        creator=getattr(curve, "creator", ""),
        is_mayhem=bool(getattr(curve, "is_mayhem", False)),
        cashback_enabled=bool(getattr(curve, "cashback_enabled", False)),
    )


def _apply_buy(curve: SimpleNamespace, tokens_raw: int, sol_lamports: int) -> None:
    curve.virtual_sol_reserves += max(0, int(sol_lamports))
    curve.real_sol_reserves += max(0, int(sol_lamports))
    curve.virtual_token_reserves = max(1, curve.virtual_token_reserves - max(0, int(tokens_raw)))
    curve.real_token_reserves = max(0, curve.real_token_reserves - max(0, int(tokens_raw)))


def _apply_sell(curve: SimpleNamespace, tokens_raw: int, sol_lamports: int) -> None:
    curve.virtual_sol_reserves = max(1, curve.virtual_sol_reserves - max(0, int(sol_lamports)))
    curve.real_sol_reserves = max(0, curve.real_sol_reserves - max(0, int(sol_lamports)))
    curve.virtual_token_reserves += max(0, int(tokens_raw))
    curve.real_token_reserves += max(0, int(tokens_raw))


def _request_iter(args: argparse.Namespace) -> Iterator[geyser_pb2.SubscribeRequest]:
    req = geyser_pb2.SubscribeRequest()
    flt = req.transactions["pump"]
    flt.vote = False
    flt.failed = False
    flt.account_include.append(PUMP_PROGRAM)
    req.commitment = geyser_pb2.PROCESSED
    yield req
    ping_id = 1
    while True:
        time.sleep(max(5, int(args.ping_seconds)))
        ping = geyser_pb2.SubscribeRequest()
        ping.ping.id = ping_id
        ping_id += 1
        yield ping


def _pubkey(raw: bytes) -> str:
    try:
        return str(Pubkey.from_bytes(raw))
    except Exception:
        return ""


def _decode_pump(update: geyser_pb2.SubscribeUpdate) -> dict[str, Any] | None:
    if not update.HasField("transaction"):
        return None
    info = update.transaction.transaction
    tx = info.transaction
    if not tx.signatures:
        return None
    sig = str(Signature.from_bytes(tx.signatures[0]))
    keys = [_pubkey(bytes(k)) for k in tx.message.account_keys]
    for ix in tx.message.instructions:
        try:
            program = keys[int(ix.program_id_index)]
        except Exception:
            continue
        if program != PUMP_PROGRAM:
            continue
        data = bytes(ix.data)
        if len(data) < 24:
            continue
        disc = data[:8]
        if disc not in {DISC_BUY, DISC_BUY_EXACT_SOL_IN, DISC_SELL}:
            continue
        accounts = list(bytes(ix.accounts))

        def acct(i: int) -> str:
            try:
                return keys[int(accounts[i])]
            except Exception:
                return ""

        first = int.from_bytes(data[8:16], "little")
        second = int.from_bytes(data[16:24], "little")
        if disc == DISC_SELL:
            return {
                "kind": "sell",
                "slot": int(update.transaction.slot),
                "sig": sig,
                "mint": acct(2),
                "curve": acct(3),
                "token_amount_raw": first,
                "sol_lamports": 0,
                "recv_ms": _now_ms(),
            }
        sol_lamports = first if disc == DISC_BUY_EXACT_SOL_IN else second
        return {
            "kind": "buy",
            "slot": int(update.transaction.slot),
            "sig": sig,
            "mint": acct(2),
            "curve": acct(3),
            "token_amount_raw": 0,
            "sol_lamports": int(sol_lamports),
            "recv_ms": _now_ms(),
        }
    return None


def _pnl_lamports(broker: Any, cand: dict[str, Any], global_cfg: Any, fee_buffer: int) -> int:
    sell_out, _sell_fee = broker.quote_pump_sell_sol(int(cand["our_tokens_raw"]), cand["sim_curve"], global_cfg)
    scout_lamports = int(cand["scout_lamports"])
    return int(sell_out + ATA_RENT_LAMPORTS - (scout_lamports + ATA_RENT_LAMPORTS) - int(fee_buffer))


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--endpoint", default=os.environ.get("PUBLICNODE_YELLOWSTONE_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"))
    ap.add_argument("--token-env", default="PUBLICNODE_X_TOKEN")
    ap.add_argument("--metadata-key", default=os.environ.get("PUBLICNODE_GRPC_METADATA_KEY", "x-token"))
    ap.add_argument("--ping-seconds", type=int, default=15)
    ap.add_argument("--scout-grid-sol", default=os.environ.get("V285_SCOUT_GRID_SOL", "0.010,0.020,0.050,0.100,0.150,0.200"))
    ap.add_argument("--min-current-buy-sol", type=float, default=float(os.environ.get("V285_MIN_CURRENT_BUY_SOL", "0.50")))
    ap.add_argument("--max-current-buy-sol", type=float, default=float(os.environ.get("V285_MAX_CURRENT_BUY_SOL", "8.0")))
    ap.add_argument("--min-prev-buys-250", type=int, default=int(os.environ.get("V285_MIN_PREV_BUYS_250", "2")))
    ap.add_argument("--min-prev-buy-sol-250", type=float, default=float(os.environ.get("V285_MIN_PREV_BUY_SOL_250", "1.50")))
    ap.add_argument("--min-prev-buyers-250", type=int, default=int(os.environ.get("V285_MIN_PREV_BUYERS_250", "2")))
    ap.add_argument("--min-prev-buys-1s", type=int, default=int(os.environ.get("V285_MIN_PREV_BUYS_1S", "4")))
    ap.add_argument("--min-prev-buy-sol-1s", type=float, default=float(os.environ.get("V285_MIN_PREV_BUY_SOL_1S", "5.0")))
    ap.add_argument("--min-prev-buyers-1s", type=int, default=int(os.environ.get("V285_MIN_PREV_BUYERS_1S", "3")))
    ap.add_argument("--max-top-share-1s", type=float, default=float(os.environ.get("V285_MAX_TOP_SHARE_1S", "0.70")))
    ap.add_argument("--max-prev-sells-1s", type=int, default=int(os.environ.get("V285_MAX_PREV_SELLS_1S", "0")))
    ap.add_argument("--hold-ms", type=int, default=int(os.environ.get("V285_HOLD_MS", "650")))
    ap.add_argument("--entry-wait-ms", type=int, default=int(os.environ.get("V285_ENTRY_WAIT_MS", "300")))
    ap.add_argument("--entry-after-buys", type=int, default=int(os.environ.get("V285_ENTRY_AFTER_BUYS", "0")))
    ap.add_argument("--entry-after-buy-sol", type=float, default=float(os.environ.get("V285_ENTRY_AFTER_BUY_SOL", "0")))
    ap.add_argument("--abort-on-pre-entry-sell", action="store_true")
    ap.add_argument("--min-profit-lamports", type=int, default=int(os.environ.get("V285_MIN_PROFIT_LAMPORTS", "6250000")))
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V285_FEE_BUFFER_LAMPORTS", "85000")))
    ap.add_argument("--max-candidates", type=int, default=int(os.environ.get("V285_MAX_CANDIDATES", "96")))
    ap.add_argument("--target-positives", type=int, default=int(os.environ.get("V285_TARGET_POSITIVES", "1")))
    ap.add_argument("--mint-cooldown-ms", type=int, default=int(os.environ.get("V285_MINT_COOLDOWN_MS", "2500")))
    ap.add_argument("--out-jsonl", default=os.environ.get("V285_OUT_JSONL", "/root/piggy/data/v285_grpc_buy_train_continuation_no_send.jsonl"))
    args = ap.parse_args()

    token = os.environ.get(str(args.token_env), "")
    if not token:
        _log("PGG2-V285-GRPC-FATAL missing_publicnode_token_env")
        return 2
    scout_grid_lamports = sorted(
        set(
            max(1, int(float(x.strip()) * LAMPORTS_PER_SOL))
            for x in str(args.scout_grid_sol).split(",")
            if x.strip()
        )
    )
    out_fp = open(args.out_jsonl, "w", encoding="utf-8")

    def _emit(rec: dict[str, Any]) -> None:
        out_fp.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
        out_fp.flush()

    broker = _make_broker()
    global_cfg = broker.pump_global()
    channel = grpc.secure_channel(str(args.endpoint), grpc.ssl_channel_credentials())
    stub = geyser_pb2_grpc.GeyserStub(channel)
    metadata = [(str(args.metadata_key), token)]
    hist: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=256))
    active: list[dict[str, Any]] = []
    mint_cooldown_until: dict[str, int] = {}
    seen_sigs: set[str] = set()
    counters: Counter[str] = Counter()
    started = time.time()
    _log(
        "PGG2-V285-GRPC-BUY-TRAIN-START "
        f"endpoint={args.endpoint} seconds={args.seconds} scout_grid_sol={args.scout_grid_sol} "
        f"min_current_buy_sol={args.min_current_buy_sol:.3f} min_prev_buys_1s={args.min_prev_buys_1s} "
        f"min_prev_buy_sol_1s={args.min_prev_buy_sol_1s:.3f} min_profit_lamports={args.min_profit_lamports} "
        f"entry_after_buys={args.entry_after_buys} entry_after_buy_sol={args.entry_after_buy_sol:.6f} "
        f"entry_wait_ms={args.entry_wait_ms} abort_on_pre_entry_sell={int(args.abort_on_pre_entry_sell)} "
        f"out={args.out_jsonl}"
    )

    def _entry_trigger_met(cand: dict[str, Any]) -> bool:
        if int(args.entry_after_buys) <= 0 and float(args.entry_after_buy_sol) <= 0.0:
            return True
        if int(cand["pre_entry_buys"]) < int(args.entry_after_buys):
            return False
        if int(cand["pre_entry_buy_lamports"]) < int(float(args.entry_after_buy_sol) * LAMPORTS_PER_SOL):
            return False
        return True

    def _enter_candidate(cand: dict[str, Any], now_ms: int) -> bool:
        if cand.get("entered"):
            return True
        scout_lamports = int(cand["scout_lamports"])
        our_tokens, _buy_fee = broker.quote_pump_buy_tokens(scout_lamports, cand["sim_curve"], global_cfg)
        if our_tokens <= 0:
            counters["block_entry_zero_tokens"] += 1
            if cand in active:
                active.remove(cand)
            return False
        cand["our_tokens_raw"] = int(our_tokens)
        cand["entered"] = True
        cand["entry_ms"] = int(now_ms)
        _apply_buy(cand["sim_curve"], int(our_tokens), scout_lamports)
        counters["candidate_entry_sim"] += 1
        _emit({
            "kind": "v285_entry_sim",
            "mint": cand["mint"],
            "scout_lamports": scout_lamports,
            "entry_delay_ms": int(now_ms) - int(cand["start_ms"]),
            "current_buy_sol": cand["current_buy_sol"],
            "prev_buys_1s": cand["prev_buys_1s"],
            "prev_buy_sol_1s": cand["prev_buy_sol_1s"],
            "prev_buyers_1s": cand["prev_buyers_1s"],
            "top_share_1s": cand["top_share_1s"],
            "pre_entry_buys": cand["pre_entry_buys"],
            "pre_entry_buy_sol": cand["pre_entry_buy_lamports"] / LAMPORTS_PER_SOL,
            "pre_entry_sells": cand["pre_entry_sells"],
            "ts_ms": now_ms,
        })
        _log(
            "PGG2-V285-CONTINUATION-ENTRY-SIM "
            f"mint={_short(cand['mint'])} scout_sol={scout_lamports/LAMPORTS_PER_SOL:.6f} "
            f"entry_delay_ms={int(now_ms)-int(cand['start_ms'])} "
            f"pre_entry_buys={cand['pre_entry_buys']} "
            f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
            f"pre_entry_sells={cand['pre_entry_sells']}"
        )
        return True

    try:
        for update in stub.Subscribe(_request_iter(args), metadata=metadata):
            now = _now_ms()
            if time.time() - started > int(args.seconds):
                counters["timeout"] += 1
                break
            counters["grpc_updates"] += 1
            rec = _decode_pump(update)
            if not rec:
                counters["grpc_non_pump_update"] += 1
            else:
                sig = str(rec["sig"])
                if sig in seen_sigs:
                    counters["duplicate"] += 1
                    rec = None
                else:
                    seen_sigs.add(sig)
                    counters[f"event_{rec['kind']}"] += 1

            for cand in list(active):
                if (not cand.get("entered")) and now - int(cand["start_ms"]) > int(args.entry_wait_ms):
                    _log(
                        "PGG2-V285-CONTINUATION-NO-ENTRY "
                        f"mint={_short(cand['mint'])} reason=entry_trigger_timeout "
                        f"pre_entry_buys={cand['pre_entry_buys']} "
                        f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                        f"pre_entry_sells={cand['pre_entry_sells']} "
                        f"current_buy_sol={cand['current_buy_sol']:.6f} "
                        f"prev_buys_1s={cand['prev_buys_1s']} prev_buy_sol_1s={cand['prev_buy_sol_1s']:.6f}"
                    )
                    counters["candidate_no_entry_timeout"] += 1
                    active.remove(cand)
                    mint_cooldown_until[str(cand["mint"])] = now + max(0, int(args.mint_cooldown_ms))
                    continue
                if cand.get("entered") and now - int(cand["entry_ms"]) > int(args.hold_ms):
                    pnl = _pnl_lamports(broker, cand, global_cfg, int(args.fee_buffer_lamports))
                    _log(
                        "PGG2-V285-CONTINUATION-END "
                        f"mint={_short(cand['mint'])} reason=hold_timeout scout_sol={int(cand['scout_lamports'])/LAMPORTS_PER_SOL:.6f} "
                        f"future_buys={cand['future_buys']} future_buy_sol={cand['future_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                        f"future_sells={cand['future_sells']} pnl_lamports={pnl:+} "
                        f"current_buy_sol={cand['current_buy_sol']:.6f} prev_buys_1s={cand['prev_buys_1s']} "
                        f"prev_buy_sol_1s={cand['prev_buy_sol_1s']:.6f} top_share_1s={cand['top_share_1s']:.4f}"
                    )
                    counters["candidate_timeout"] += 1
                    active.remove(cand)
                    mint_cooldown_until[str(cand["mint"])] = now + max(0, int(args.mint_cooldown_ms))
                    continue
                if not rec or rec["mint"] != cand["mint"]:
                    continue
                if rec["kind"] == "buy":
                    ext_tokens, _fee = broker.quote_pump_buy_tokens(int(rec["sol_lamports"]), cand["sim_curve"], global_cfg)
                    _apply_buy(cand["sim_curve"], ext_tokens, int(rec["sol_lamports"]))
                    if cand.get("entered"):
                        cand["future_buys"] += 1
                        cand["future_buy_lamports"] += int(rec["sol_lamports"])
                    else:
                        cand["pre_entry_buys"] += 1
                        cand["pre_entry_buy_lamports"] += int(rec["sol_lamports"])
                elif rec["kind"] == "sell":
                    sell_tokens = int(rec.get("token_amount_raw") or 0)
                    sell_out, _fee = broker.quote_pump_sell_sol(sell_tokens, cand["sim_curve"], global_cfg)
                    _apply_sell(cand["sim_curve"], sell_tokens, sell_out)
                    if cand.get("entered"):
                        cand["future_sells"] += 1
                    else:
                        cand["pre_entry_sells"] += 1
                        if args.abort_on_pre_entry_sell:
                            _log(
                                "PGG2-V285-CONTINUATION-NO-ENTRY "
                                f"mint={_short(cand['mint'])} reason=pre_entry_sell "
                                f"pre_entry_buys={cand['pre_entry_buys']} "
                                f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                f"pre_entry_sells={cand['pre_entry_sells']} "
                                f"current_buy_sol={cand['current_buy_sol']:.6f}"
                            )
                            counters["candidate_no_entry_pre_sell"] += 1
                            active.remove(cand)
                            mint_cooldown_until[str(cand["mint"])] = now + max(0, int(args.mint_cooldown_ms))
                            continue
                if (not cand.get("entered")) and _entry_trigger_met(cand):
                    if not _enter_candidate(cand, now):
                        continue
                if not cand.get("entered"):
                    continue
                pnl = _pnl_lamports(broker, cand, global_cfg, int(args.fee_buffer_lamports))
                if pnl >= int(args.min_profit_lamports):
                    counters["candidate_positive"] += 1
                    counters[f"candidate_positive_size_{int(cand['scout_lamports'])}"] += 1
                    _log(
                        "PGG2-V285-CONTINUATION-PASS "
                        f"mint={_short(cand['mint'])} pnl_lamports={pnl:+} scout_sol={int(cand['scout_lamports'])/LAMPORTS_PER_SOL:.6f} "
                        f"future_buys={cand['future_buys']} future_buy_sol={cand['future_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                        f"future_sells={cand['future_sells']} age_ms={now-int(cand['start_ms'])} "
                        f"current_buy_sol={cand['current_buy_sol']:.6f} prev_buys_1s={cand['prev_buys_1s']} "
                        f"prev_buy_sol_1s={cand['prev_buy_sol_1s']:.6f} top_share_1s={cand['top_share_1s']:.4f}"
                    )
                    _emit({
                        "kind": "v285_continuation_pass",
                        "mint": cand["mint"],
                        "pnl_lamports": int(pnl),
                        "scout_lamports": int(cand["scout_lamports"]),
                        "future_buys": int(cand["future_buys"]),
                        "future_buy_sol": cand["future_buy_lamports"] / LAMPORTS_PER_SOL,
                        "future_sells": int(cand["future_sells"]),
                        "age_ms": now - int(cand["start_ms"]),
                        "current_buy_sol": float(cand["current_buy_sol"]),
                        "prev_buys_1s": int(cand["prev_buys_1s"]),
                        "prev_buy_sol_1s": float(cand["prev_buy_sol_1s"]),
                        "prev_buyers_1s": int(cand["prev_buyers_1s"]),
                        "top_share_1s": float(cand["top_share_1s"]),
                        "ts_ms": now,
                    })
                    active.remove(cand)
                    mint_cooldown_until[str(cand["mint"])] = now + max(0, int(args.mint_cooldown_ms))
                    if int(args.target_positives) > 0 and counters["candidate_positive"] >= int(args.target_positives):
                        counters["target_positives_reached"] += 1
                        raise StopIteration

            if not rec:
                continue
            mint = str(rec["mint"])
            hist[mint].append(rec)
            if rec["kind"] != "buy":
                continue
            if int(rec["sol_lamports"]) < int(float(args.min_current_buy_sol) * LAMPORTS_PER_SOL):
                counters["block_small_current_buy"] += 1
                continue
            if int(rec["sol_lamports"]) > int(float(args.max_current_buy_sol) * LAMPORTS_PER_SOL):
                counters["block_large_current_buy"] += 1
                continue
            if int(mint_cooldown_until.get(mint, 0)) > now:
                counters["block_mint_cooldown"] += 1
                continue
            recent250 = [x for x in hist[mint] if now - int(x["recv_ms"]) <= 250]
            recent = [x for x in hist[mint] if now - int(x["recv_ms"]) <= 1000]
            prev_buys250 = [x for x in recent250 if x["kind"] == "buy" and x["sig"] != rec["sig"]]
            prev_buys = [x for x in recent if x["kind"] == "buy" and x["sig"] != rec["sig"]]
            prev_sells = [x for x in recent if x["kind"] == "sell"]
            if len(prev_buys250) < int(args.min_prev_buys_250):
                counters["block_prev_buys_250"] += 1
                continue
            if len(prev_buys) < int(args.min_prev_buys_1s):
                counters["block_prev_buys"] += 1
                continue
            prev_buy_sol_250 = sum(int(x["sol_lamports"]) for x in prev_buys250) / LAMPORTS_PER_SOL
            prev_buy_sol_1s = sum(int(x["sol_lamports"]) for x in prev_buys) / LAMPORTS_PER_SOL
            if prev_buy_sol_250 < float(args.min_prev_buy_sol_250):
                counters["block_prev_buy_sol_250"] += 1
                continue
            if prev_buy_sol_1s < float(args.min_prev_buy_sol_1s):
                counters["block_prev_buy_sol"] += 1
                continue
            prev_buyers_250 = len({str(x.get("sig")) for x in prev_buys250})
            prev_buyers_1s = len({str(x.get("sig")) for x in prev_buys})
            if prev_buyers_250 < int(args.min_prev_buyers_250):
                counters["block_prev_buyers_250"] += 1
                continue
            if prev_buyers_1s < int(args.min_prev_buyers_1s):
                counters["block_prev_buyers"] += 1
                continue
            by_sig: dict[str, int] = {}
            for x in prev_buys:
                by_sig[str(x.get("sig"))] = by_sig.get(str(x.get("sig")), 0) + int(x["sol_lamports"])
            top_share_1s = max(by_sig.values()) / max(1, sum(by_sig.values())) if by_sig else 1.0
            if top_share_1s > float(args.max_top_share_1s):
                counters["block_top_share"] += 1
                continue
            if len(prev_sells) > int(args.max_prev_sells_1s):
                counters["block_prev_sells"] += 1
                continue
            if len(active) >= int(args.max_candidates):
                counters["block_active_cap"] += 1
                continue
            try:
                from pgg2_direct_pump import as_pubkey  # type: ignore
                curve = _curve_copy(broker.bonding_curve(as_pubkey(mint)))
            except Exception as exc:
                counters[f"block_curve:{type(exc).__name__}"] += 1
                continue
            for scout_lamports in scout_grid_lamports:
                cand = {
                    "mint": mint,
                    "start_ms": now,
                    "entry_ms": 0,
                    "entered": False,
                    "our_tokens_raw": 0,
                    "scout_lamports": int(scout_lamports),
                    "sim_curve": _curve_copy(curve),
                    "future_buys": 0,
                    "future_sells": 0,
                    "future_buy_lamports": 0,
                    "pre_entry_buys": 0,
                    "pre_entry_sells": 0,
                    "pre_entry_buy_lamports": 0,
                    "current_buy_sol": int(rec["sol_lamports"]) / LAMPORTS_PER_SOL,
                    "prev_buys_1s": len(prev_buys),
                    "prev_buy_sol_1s": prev_buy_sol_1s,
                    "prev_buyers_1s": prev_buyers_1s,
                    "top_share_1s": top_share_1s,
                }
                active.append(cand)
                if _entry_trigger_met(cand):
                    _enter_candidate(cand, now)
            counters["candidate_start"] += 1
            _log(
                "PGG2-V285-CONTINUATION-CANDIDATE "
                f"mint={_short(mint)} current_buy_sol={int(rec['sol_lamports'])/LAMPORTS_PER_SOL:.6f} "
                f"prev_buys_250={len(prev_buys250)} prev_buy_sol_250={prev_buy_sol_250:.6f} "
                f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol_1s:.6f} "
                f"prev_buyers_1s={prev_buyers_1s} top_share_1s={top_share_1s:.4f} prev_sells_1s={len(prev_sells)}"
            )
    except StopIteration:
        pass
    except grpc.RpcError as exc:
        _log(f"PGG2-V285-GRPC-RPC-ERROR code={exc.code()} details={str(exc.details())[:240]}")
        counters["grpc_error"] += 1
    finally:
        _log("PGG2-V285-CONTINUATION-FINAL " + " ".join(f"{k}={v}" for k, v in counters.most_common(80)))
        try:
            out_fp.close()
        except Exception:
            pass
    return 0 if counters["candidate_positive"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
