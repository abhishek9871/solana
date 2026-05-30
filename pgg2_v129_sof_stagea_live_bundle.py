#!/usr/bin/env python3
"""V129 Stage A live atomic bundle runner.

Uses the free SOF raw-shred feed and sends at most one Jito atomic bundle:
our buy -> raw external buy -> our guarded sell+close -> tip.

No directional fallback. No Helius Sender fallback. No public RPC fallback.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pgg2_v108_bundle_builder import build_bundle_plan_no_send, plan_to_json  # type: ignore
from pgg2_v108_bundle_profit_model import select_best_size  # type: ignore
from pgg2_v108_external_tx_decoder import decode_external_pump_buy  # type: ignore
from pgg2_v108_jito_bundle_sender import send_bundle, wait_bundle_status, warm_bundle_endpoints  # type: ignore
from pgg2_v109_no_send_live_bundle_validation import (  # type: ignore
    _ensure_tip_account,
    _force_buyback_pair_from_external,
    _load_env,
    _make_broker,
    _rpc_call,
)
from pgg2_v129_sof_no_send_bundle_validation import _parse_pump_raw_line, _sof_env  # type: ignore


LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_PROGRAMS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EP3N7jczwt9hQxH4TtYzeCk",
]
PUMP_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BC_DISC = bytes([0x17, 0xB7, 0xF8, 0x37, 0x60, 0xD8, 0xAC, 0x60])
BC_DISC_B58 = "4y6pru6YvC7"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _wallet_balance_lamports(owner: str) -> int:
    result = _rpc_call("getBalance", [owner, {"commitment": "processed"}], timeout=3.0)
    return int((result or {}).get("value") or 0)


def _token_accounts(owner: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for program_id in TOKEN_PROGRAMS:
        try:
            result = _rpc_call(
                "getTokenAccountsByOwner",
                [
                    owner,
                    {"programId": program_id},
                    {"encoding": "jsonParsed", "commitment": "processed"},
                ],
                timeout=4.0,
            )
        except Exception:
            continue
        for row in (result or {}).get("value") or []:
            try:
                info = row["account"]["data"]["parsed"]["info"]
                amount = int((info.get("tokenAmount") or {}).get("amount") or 0)
                out.append(
                    {
                        "pubkey": str(row.get("pubkey") or ""),
                        "mint": str(info.get("mint") or ""),
                        "amount": amount,
                        "program": program_id,
                    }
                )
            except Exception:
                continue
    return out


def _stagea_entrypoints(args: argparse.Namespace) -> list[str]:
    raw = os.environ.get("V129_SOF_ENTRYPOINTS", "").strip() or str(args.entrypoint)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        ep = item.strip()
        if ep and ep not in seen:
            seen.add(ep)
            out.append(ep)
    return out or [str(args.entrypoint)]


def _sof_env_for(args: argparse.Namespace, *, entrypoint: str, idx: int) -> dict[str, str]:
    ns = SimpleNamespace(**vars(args))
    ns.entrypoint = entrypoint
    span = max(100, int(getattr(args, "port_end", 14299)) - int(getattr(args, "port_start", 14200)) + 1)
    ns.port_start = int(getattr(args, "port_start", 14200)) + idx * span
    ns.port_end = int(getattr(args, "port_end", 14299)) + idx * span
    env = _sof_env(ns)
    bind_base = int(os.environ.get("V129_SOF_BIND_BASE_PORT", "8001") or 8001)
    env["V128_SOF_BIND"] = f"0.0.0.0:{bind_base + idx}"
    return env


def _helius_ws_url() -> str:
    rpc = os.environ.get("HELIUS_RPC_URL") or ""
    if "api-key=" in rpc:
        key = rpc.split("api-key=", 1)[1].split("&", 1)[0]
        return f"wss://mainnet.helius-rpc.com/?api-key={key}"
    api = os.environ.get("HELIUS_API_KEY", "")
    return f"wss://mainnet.helius-rpc.com/?api-key={api}" if api else "wss://api.mainnet-beta.solana.com/"


def _decode_curve_update(value: dict[str, Any]) -> Any | None:
    try:
        from pgg2_direct_pump import as_pubkey  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore

        bc = str(value.get("pubkey") or "")
        account = value.get("account") or {}
        acc_data = account.get("data")
        if isinstance(acc_data, list):
            acc_data = acc_data[0]
        if not bc or not isinstance(acc_data, str):
            return None
        raw = base64.b64decode(acc_data)
        if len(raw) < 49 or raw[:8] != BC_DISC:
            return None
        creator = str(Pubkey.from_bytes(raw[49:81])) if len(raw) >= 81 else ""
        return SimpleNamespace(
            key=as_pubkey(bc),
            virtual_token_reserves=int.from_bytes(raw[8:16], "little"),
            virtual_sol_reserves=int.from_bytes(raw[16:24], "little"),
            real_token_reserves=int.from_bytes(raw[24:32], "little"),
            real_sol_reserves=int.from_bytes(raw[32:40], "little"),
            token_total_supply=int.from_bytes(raw[40:48], "little"),
            complete=bool(raw[48]),
            creator=creator,
            ts_ms=_now_ms(),
        )
    except Exception:
        return None


def _start_curve_cache(curve_cache: dict[str, Any], stop_event: threading.Event) -> threading.Thread:
    async def run() -> None:
        import websockets  # type: ignore

        ws_url = _helius_ws_url()
        backoff = 0.5
        while not stop_event.is_set():
            try:
                redacted = ws_url.split("api-key=", 1)[0] + "api-key=..." if "api-key=" in ws_url else ws_url
                _log(f"PGG2-V129-CURVE-CACHE-CONNECT url={redacted}")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=60,
                    max_queue=8192,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 12912,
                                "method": "programSubscribe",
                                "params": [
                                    PUMP_PROGRAM_ID_STR,
                                    {
                                        "commitment": "processed",
                                        "encoding": "base64",
                                        "filters": [{"memcmp": {"offset": 0, "bytes": BC_DISC_B58}}],
                                    },
                                ],
                            }
                        )
                    )
                    _log("PGG2-V129-CURVE-CACHE-SUBSCRIBED source=helius_programSubscribe")
                    backoff = 0.5
                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            if str(msg.get("method") or "") != "programNotification":
                                continue
                            value = (((msg.get("params") or {}).get("result") or {}).get("value") or {})
                            curve = _decode_curve_update(value)
                            if curve and not bool(getattr(curve, "complete", False)):
                                curve_cache[str(curve.key)] = curve
                        except Exception:
                            continue
            except Exception as exc:
                _log(f"PGG2-V129-CURVE-CACHE-WARN err={type(exc).__name__}:{exc}")
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 1.5)

    def target() -> None:
        asyncio.run(run())

    t = threading.Thread(target=target, name="v129_curve_cache", daemon=True)
    t.start()
    return t


def _prepare_bundle_plan(
    *,
    broker: Any,
    decoded: Any,
    event_ts_ms: int,
    curve_cache: dict[str, Any],
) -> tuple[Any | None, str]:
    from pgg2_direct_pump import as_pubkey  # type: ignore

    t0 = _now_ms()
    curve = None
    curve_source = "none"
    cache_max_age_ms = int(os.environ.get("V129_CURVE_CACHE_MAX_AGE_MS", "500") or 500)
    cached = curve_cache.get(str(getattr(decoded, "bonding_curve", "") or ""))
    if cached is not None:
        cache_ts = int(getattr(cached, "ts_ms", 0) or 0)
        cache_age_ms = int(event_ts_ms) - cache_ts
        if 0 <= cache_age_ms <= cache_max_age_ms:
            curve = cached
            curve_source = "cache"
            _log(
                f"PGG2-V129-CURVE-CACHE-HIT mint={decoded.mint[:4]}.. "
                f"curve={str(getattr(decoded, 'bonding_curve', ''))[:4]}.. age_ms={cache_age_ms}"
            )
        else:
            _log(
                f"PGG2-V129-CURVE-CACHE-STALE mint={decoded.mint[:4]}.. "
                f"age_ms={cache_age_ms} max_ms={cache_max_age_ms}"
            )
    if curve is None:
        if os.environ.get("V129_REQUIRE_CURVE_CACHE", "1").lower() in {"1", "true", "yes"}:
            return None, "curve_cache_miss_live"
        try:
            curve = broker.bonding_curve(as_pubkey(decoded.mint))
            curve_source = "rpc_fallback"
            _log(f"PGG2-V129-CURVE-RPC-FALLBACK mint={decoded.mint[:4]}..")
        except Exception as exc:
            return None, f"curve_read_failed:{type(exc).__name__}"
    t_curve = _now_ms()

    best = select_best_size(
        mint=decoded.mint,
        vsol_lamports=int(curve.virtual_sol_reserves),
        vtok_raw=int(curve.virtual_token_reserves),
        external_sol_lamports=max(1, int(decoded.sol_lamports)),
    )
    if not best or not best.passed:
        return None, "bundle_profit_negative_or_tip_exceeds_edge"
    if not _force_buyback_pair_from_external(broker, decoded):
        return None, "no_buyback_pair_in_external_raw_tx"

    vsol_after_our = int(curve.virtual_sol_reserves) + int(best.size_lamports)
    vtok_after_our = max(1, int(curve.virtual_token_reserves) - int(best.our_tokens_raw))
    vsol_after_external = vsol_after_our + max(1, int(decoded.sol_lamports))
    vtok_after_external = max(1, vtok_after_our - int(best.external_tokens_raw))
    try:
        plan = build_bundle_plan_no_send(
            broker=broker,
            decoded_external=decoded,
            profit_result=best,
            vsol_after_external=vsol_after_external,
            vtok_after_external=vtok_after_external,
            buy_curve_snapshot=curve,
            snapshot_ts_ms=int(getattr(curve, "ts_ms", 0) or t_curve),
            creator=str(getattr(curve, "creator", "")),
        )
    except Exception as exc:
        return None, f"bundle_build_failed:{type(exc).__name__}:{exc}"
    _log(
        f"PGG2-V129-STAGEA-BUNDLE-READY elapsed_ms={_now_ms() - event_ts_ms} "
        f"curve_ms={t_curve - t0} curve_source={curve_source} {plan_to_json(plan)}"
    )
    return plan, "bundle_ready"


def _send_stagea_bundle(plan: Any, *, dry_run: bool = False) -> dict[str, Any]:
    _log(
        f"PGG2-V129-STAGEA-BUNDLE-SEND mint={str(plan.mint)[:4]}.. "
        f"size_lamports={int(plan.selected_size_lamports)} "
        f"projected_profit_lamports={int(plan.projected_profit_lamports):+} dry_run={int(dry_run)}"
    )
    try:
        result = send_bundle(
            [plan.our_buy_b64, plan.external_buy_b64, plan.our_sell_close_b64, plan.tip_b64],
            dry_run=dry_run,
        )
    except Exception as exc:
        err = f"{type(exc).__name__}:{exc}"
        _log(f"PGG2-V129-STAGEA-BUNDLE-SEND-BLOCK err={err[:400]}")
        if "already processed transaction" in err:
            return {"status": "send_rejected_already_processed", "error": err}
        return {"status": "send_error", "error": err}
    if dry_run:
        return {"status": "dry_run_ready", "result": result}
    bundle_id = str(result.get("bundle_id") or "")
    if not bundle_id:
        return {"status": "send_returned_no_bundle_id", "result": result}
    status = wait_bundle_status(bundle_id, timeout_sec=20.0, poll_sec=0.35)
    return {"status": str(status.get("status") or "unknown"), "bundle_id": bundle_id, "raw": status}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=int, default=600)
    ap.add_argument("--entrypoint", default="67.213.122.69:8000")
    ap.add_argument("--port-start", type=int, default=14200)
    ap.add_argument("--port-end", type=int, default=14299)
    ap.add_argument("--min-external-lamports", type=int, default=100_000_000)
    ap.add_argument("--max-build-attempts", type=int, default=100)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("V129_STAGEA_DRY_RUN", "0").lower() in {"1", "true", "yes"},
    )
    args = ap.parse_args()

    _load_env()
    _ensure_tip_account()
    warm_bundle_endpoints()
    os.environ.setdefault("PGG2_DIRECT_BLOCKHASH_CACHE_MS", "10000")
    broker = _make_broker()
    try:
        broker.refresh_blockhash_cache()
        _log("PGG2-V129-BLOCKHASH-WARM ok=1 ttl_ms=10000")
    except Exception as exc:
        _log(f"PGG2-V129-BLOCKHASH-WARM ok=0 err={type(exc).__name__}:{exc}")
    owner = str(broker.public_key)
    start_bal = _wallet_balance_lamports(owner)
    start_tokens = _token_accounts(owner)
    nonzero_start = [x for x in start_tokens if int(x.get("amount") or 0) > 0]
    if nonzero_start:
        _log(f"PGG2-V129-STAGEA-ABORT reason=nonzero_token_accounts count={len(nonzero_start)}")
        return 2
    _log(
        f"PGG2-V129-STAGEA-START owner={owner[:4]}.. balance_sol={start_bal / LAMPORTS_PER_SOL:.9f} "
        f"entrypoint={args.entrypoint} min_external_lamports={args.min_external_lamports} "
        f"dry_run={int(bool(args.dry_run))}"
    )

    curve_cache: dict[str, Any] = {}
    stop_curve_cache = threading.Event()
    _start_curve_cache(curve_cache, stop_curve_cache)
    warmup_ms = int(os.environ.get("V129_CURVE_CACHE_WARMUP_MS", "1500") or 1500)
    if warmup_ms > 0:
        time.sleep(warmup_ms / 1000.0)
        _log(f"PGG2-V129-CURVE-CACHE-WARMUP-DONE ms={warmup_ms} cached={len(curve_cache)}")

    counters: Counter[str] = Counter()
    seen: set[str] = set()
    started = time.time()
    feed_queue: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=20000)
    feed_procs: list[subprocess.Popen[str]] = []
    feed_threads: list[threading.Thread] = []
    entrypoints = _stagea_entrypoints(args)
    max_feeds = max(1, int(os.environ.get("V129_SOF_MAX_FEEDS", str(len(entrypoints))) or len(entrypoints)))
    sof_restart_on_exit = os.environ.get("V129_SOF_RESTART_ON_EXIT", "1").lower() in {"1", "true", "yes"}
    sof_max_restarts = int(os.environ.get("V129_SOF_MAX_RESTARTS", "4") or 4)
    sof_restart_count = 0

    def launch_sof_feeds() -> None:
        nonlocal feed_procs, feed_threads
        feed_procs = []
        feed_threads = []
        for idx, entrypoint in enumerate(entrypoints[:max_feeds]):
            launch_one_sof_feed(idx, entrypoint)

    def launch_one_sof_feed(idx: int, entrypoint: str) -> None:
        env = _sof_env_for(args, entrypoint=entrypoint, idx=idx)
        _log(
            f"PGG2-V129-SOF-FEED-START idx={idx} entrypoint={entrypoint} "
            f"bind={env.get('V128_SOF_BIND')} ports={env.get('V128_SOF_PORT_START')}-{env.get('V128_SOF_PORT_END')}"
        )
        proc = subprocess.Popen(
            ["/root/piggy/v128_sof_raw_ingest_probe/target/release/pgg2_v128_sof_raw_ingest_probe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd="/root/piggy/v128_sof_raw_ingest_probe",
        )
        feed_procs.append(proc)

        def pump_stdout(p: subprocess.Popen[str], feed_idx: int) -> None:
            try:
                assert p.stdout is not None
                for raw_line in p.stdout:
                    try:
                        feed_queue.put((feed_idx, raw_line), timeout=0.25)
                    except queue.Full:
                        pass
            except Exception:
                return

        t = threading.Thread(target=pump_stdout, args=(proc, idx), name=f"v129_sof_feed_{idx}", daemon=True)
        t.start()
        feed_threads.append(t)

    launch_sof_feeds()

    final_status: dict[str, Any] = {"status": "no_bundle_sent"}
    last_send_attempt_ms = 0
    min_send_interval_ms = int(os.environ.get("V129_MIN_SEND_INTERVAL_MS", "1100") or 1100)
    max_bundle_ready_age_ms = int(os.environ.get("V129_MAX_BUNDLE_READY_AGE_MS", "150") or 150)
    echo_raw_sof = os.environ.get("V129_ECHO_RAW_SOF", "0").lower() in {"1", "true", "yes"}
    async_bundle_send = (
        os.environ.get("V129_ASYNC_BUNDLE_SEND", "0").lower()
        in {"1", "true", "yes", "on"}
    ) and not bool(args.dry_run)
    async_max_pending = max(1, int(os.environ.get("V129_ASYNC_MAX_PENDING_SENDS", "2") or 2))
    async_final_wait_sec = float(os.environ.get("V129_ASYNC_FINAL_WAIT_SEC", "22") or 22)
    send_executor: concurrent.futures.ThreadPoolExecutor | None = None
    pending_sends: list[concurrent.futures.Future[dict[str, Any]]] = []
    if async_bundle_send:
        send_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=async_max_pending,
            thread_name_prefix="v129_bundle_send",
        )
        _log(
            f"PGG2-V129-ASYNC-BUNDLE-SEND enabled=1 "
            f"max_pending={async_max_pending} final_wait_sec={async_final_wait_sec:.1f}"
        )

    def _record_send_status(status_obj: dict[str, Any], *, async_result: bool = False) -> str:
        nonlocal final_status
        status = str(status_obj.get("status") or "")
        final_status = status_obj
        if async_result:
            _log(f"PGG2-V129-ASYNC-BUNDLE-SEND-RESULT status={status}")
        if status == "dry_run_ready":
            counters["bundle_dry_run_ready"] += 1
        elif status == "send_rejected_already_processed":
            counters["bundle_send_rejected_already_processed"] += 1
        elif status == "send_error":
            counters["bundle_send_error"] += 1
        elif status:
            counters[f"bundle_status:{status}"] += 1
        return status

    def _harvest_async_sends(*, wait: bool = False) -> None:
        if not pending_sends:
            return
        for fut in list(pending_sends):
            if not wait and not fut.done():
                continue
            try:
                status_obj = fut.result(timeout=0.01 if not wait else None)
            except concurrent.futures.TimeoutError:
                continue
            except Exception as exc:
                status_obj = {"status": "send_error", "error": f"{type(exc).__name__}:{exc}"}
            try:
                pending_sends.remove(fut)
            except ValueError:
                pass
            _record_send_status(status_obj, async_result=True)

    try:
        while time.time() - started <= args.max_seconds:
            _harvest_async_sends(wait=False)
            if str(final_status.get("status") or "") in {"landed", "failed", "invalid"}:
                break
            try:
                feed_idx, line = feed_queue.get(timeout=0.25)
            except queue.Empty:
                if all(p.poll() is not None for p in feed_procs):
                    counters["sof_feed_all_exited"] += 1
                    remaining = args.max_seconds - (time.time() - started)
                    if sof_restart_on_exit and sof_restart_count < sof_max_restarts and remaining > 10:
                        sof_restart_count += 1
                        counters["sof_feed_restarts"] += 1
                        _log(
                            f"PGG2-V129-SOF-FEED-RESTART count={sof_restart_count} "
                            f"max={sof_max_restarts} remaining_sec={remaining:.1f}"
                        )
                        launch_sof_feeds()
                        continue
                    break
                continue
            if time.time() - started > args.max_seconds:
                counters["timeout"] += 1
                break
            if "PGG2-V128-SOF-PUMP-RAW-TX" not in line:
                if echo_raw_sof:
                    sys.stdout.write(f"[sof{feed_idx}] {line}")
                continue
            if echo_raw_sof:
                sys.stdout.write(f"[sof{feed_idx}] {line}")
            counters["pump_raw_lines"] += 1
            rec = _parse_pump_raw_line(line)
            if not rec:
                counters["parse_fail"] += 1
                continue
            sig = str(rec["signature"])
            if sig in seen:
                counters["duplicate"] += 1
                continue
            seen.add(sig)
            counters[f"feed{feed_idx}_unique"] += 1
            event_ts = _now_ms()
            try:
                decoded = decode_external_pump_buy(
                    str(rec["raw_tx_b64"]),
                    expected_sig=sig,
                    source="v129_stagea_sof",
                    slot=int(rec["slot"]),
                )
            except Exception as exc:
                counters[f"decode_block:{type(exc).__name__}:{str(exc)[:48]}"] += 1
                continue
            counters["decoded_buy"] += 1
            _log(
                f"PGG2-V129-STAGEA-RAW-BUY mint={decoded.mint[:4]}.. "
                f"sig={decoded.signature[:16]} sol_lamports={decoded.sol_lamports}"
            )
            if int(decoded.sol_lamports) < int(args.min_external_lamports):
                counters["external_size_below_min"] += 1
                continue
            if counters["build_attempts"] >= int(args.max_build_attempts):
                counters["build_attempt_cap"] += 1
                continue
            counters["build_attempts"] += 1
            plan, reason = _prepare_bundle_plan(
                broker=broker,
                decoded=decoded,
                event_ts_ms=event_ts,
                curve_cache=curve_cache,
            )
            if plan is None:
                counters[f"bundle_block:{reason}"] += 1
                _log(f"PGG2-V129-STAGEA-BUNDLE-BLOCK reason={reason}")
                continue
            ready_age_ms = _now_ms() - event_ts
            if ready_age_ms > max_bundle_ready_age_ms:
                counters["bundle_ready_stale"] += 1
                _log(
                    f"PGG2-V129-STAGEA-BUNDLE-STALE-BLOCK "
                    f"ready_age_ms={ready_age_ms} max_ms={max_bundle_ready_age_ms}"
                )
                continue
            since_last_send_ms = _now_ms() - last_send_attempt_ms if last_send_attempt_ms else 999999
            if since_last_send_ms < min_send_interval_ms:
                counters["bundle_send_rate_limited_locally"] += 1
                _log(
                    f"PGG2-V129-STAGEA-BUNDLE-LOCAL-RATE-BLOCK "
                    f"since_last_send_ms={since_last_send_ms} min_ms={min_send_interval_ms}"
                )
                continue
            if async_bundle_send and len(pending_sends) >= async_max_pending:
                counters["bundle_send_async_pending_full"] += 1
                _log(
                    f"PGG2-V129-ASYNC-BUNDLE-PENDING-BLOCK "
                    f"pending={len(pending_sends)} max_pending={async_max_pending}"
                )
                continue
            last_send_attempt_ms = _now_ms()
            if async_bundle_send and send_executor is not None:
                pending_sends.append(
                    send_executor.submit(_send_stagea_bundle, plan, dry_run=False)
                )
                counters["bundle_send_async_submitted"] += 1
                _log(
                    f"PGG2-V129-ASYNC-BUNDLE-SEND-SUBMITTED "
                    f"pending={len(pending_sends)}"
                )
                continue
            status_obj = _send_stagea_bundle(plan, dry_run=bool(args.dry_run))
            status = _record_send_status(status_obj)
            if status == "dry_run_ready":
                break
            if status == "send_rejected_already_processed":
                continue
            if status == "send_error":
                continue
            break
    finally:
        stop_curve_cache.set()
        for proc in feed_procs:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if async_bundle_send:
            deadline = time.time() + max(0.0, async_final_wait_sec)
            while pending_sends and time.time() < deadline:
                _harvest_async_sends(wait=False)
                if str(final_status.get("status") or "") in {"landed", "failed", "invalid"}:
                    break
                time.sleep(0.05)
            _harvest_async_sends(wait=False)
            if pending_sends:
                counters["bundle_send_async_unresolved"] += len(pending_sends)
                _log(f"PGG2-V129-ASYNC-BUNDLE-UNRESOLVED pending={len(pending_sends)}")
            if send_executor is not None:
                send_executor.shutdown(wait=False, cancel_futures=True)

    time.sleep(2.0)
    end_bal = _wallet_balance_lamports(owner)
    end_tokens = _token_accounts(owner)
    nonzero_end = [x for x in end_tokens if int(x.get("amount") or 0) > 0]
    delta = end_bal - start_bal
    _log(
        "PGG2-V129-STAGEA-FINAL "
        + " ".join(f"{k}={v}" for k, v in counters.most_common(30))
        + f" bundle_status={final_status.get('status')} wallet_delta_lamports={delta:+} "
        + f"wallet_delta_sol={delta / LAMPORTS_PER_SOL:+.9f} nonzero_tokens={len(nonzero_end)}"
    )
    if nonzero_end:
        _log("PGG2-V129-STAGEA-FAIL reason=token_residual")
        return 3
    if delta < 0 and final_status.get("status") == "landed":
        _log("PGG2-V129-STAGEA-FAIL reason=negative_landed_wallet_delta")
        return 4
    if final_status.get("status") in {
        "landed",
        "dry_run_ready",
        "no_bundle_sent",
        "timeout",
        "failed",
        "invalid",
        "send_rejected_already_processed",
        "send_error",
    }:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
