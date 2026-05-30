#!/usr/bin/env python3
"""V129 SOF-backed no-send V108 bundle validation.

Uses the free self-hosted SOF/gossip raw-shred path, extracts raw pump buy
transactions, and feeds them into the existing V108/V109 atomic bundle builder.

No live sends. No directional fallback.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from pgg2_v108_external_tx_decoder import decode_external_pump_buy  # type: ignore
from pgg2_v109_no_send_live_bundle_validation import (  # type: ignore
    _build_validation_for_raw,
    _ensure_tip_account,
    _load_env,
    _make_broker,
)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SIG_RE = re.compile(r"sig=([1-9A-HJ-NP-Za-km-z]{80,100})")
SLOT_RE = re.compile(r"slot=(\d+)")
RAW_RE = re.compile(r"raw_b64=([A-Za-z0-9+/=]+)")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _rpc_url() -> str:
    api = os.environ.get("HELIUS_API_KEY", "")
    return (
        os.environ.get("HELIUS_RPC_URL")
        or (f"https://mainnet.helius-rpc.com/?api-key={api}" if api else "")
        or os.environ.get("SOLANA_RPC_URL", "")
        or ""
    )


def _strict_sig_status(sig: str) -> tuple[str, str]:
    rpc = _rpc_url()
    if not rpc:
        return "rpc_unconfigured", "no_rpc_url"
    try:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": False}],
            }
        ).encode()
        req = urllib.request.Request(rpc, data=payload, headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            parsed = json.loads(resp.read().decode())
        if parsed.get("error"):
            return "rpc_error", str(parsed["error"])[:120]
        value = ((parsed.get("result") or {}).get("value") or [None])[0]
        if value is None:
            return "null", "-"
        return str(value.get("confirmationStatus") or "present_unknown"), "-"
    except Exception as exc:  # noqa: BLE001 - fail closed.
        return "rpc_error", type(exc).__name__


def _parse_pump_raw_line(line: str) -> dict[str, Any] | None:
    clean = ANSI_RE.sub("", line)
    sig = SIG_RE.search(clean)
    slot = SLOT_RE.search(clean)
    raw = RAW_RE.search(clean)
    if not (sig and slot and raw):
        return None
    return {
        "signature": sig.group(1),
        "slot": int(slot.group(1)),
        "raw_tx_b64": raw.group(1),
    }


def _sof_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "RUST_LOG": env.get(
                "RUST_LOG",
                "pgg2_v128_sof_raw_ingest_probe=info,sof=warn,solana_gossip=warn",
            ),
            "V128_SOF_PUMP_ONLY": env.get("V128_SOF_PUMP_ONLY", "1"),
            "V128_SOF_ENTRYPOINTS": args.entrypoint,
            "V128_SOF_PORT_START": str(args.port_start),
            "V128_SOF_PORT_END": str(args.port_end),
            "V128_SOF_SECONDS": str(args.max_seconds),
            "V128_SOF_PINNED": env.get("V128_SOF_PINNED", "1"),
            "V128_SOF_RUNTIME_SWITCH": env.get("V128_SOF_RUNTIME_SWITCH", "0"),
            "V128_SOF_BOOTSTRAP_MIN_PEERS": env.get("V128_SOF_BOOTSTRAP_MIN_PEERS", "1"),
            "V128_SOF_BOOTSTRAP_MAX_WAIT_MS": env.get("V128_SOF_BOOTSTRAP_MAX_WAIT_MS", "50000"),
            "V128_SOF_STABILIZE_MIN_PACKETS": env.get("V128_SOF_STABILIZE_MIN_PACKETS", "1"),
            "V128_SOF_STABILIZE_MS": env.get("V128_SOF_STABILIZE_MS", "500"),
        }
    )
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=int, default=120)
    ap.add_argument("--entrypoint", default="67.213.122.69:8000")
    ap.add_argument("--port-start", type=int, default=14200)
    ap.add_argument("--port-end", type=int, default=14299)
    ap.add_argument("--min-external-lamports", type=int, default=100_000_000)
    ap.add_argument("--max-build-attempts", type=int, default=20)
    ap.add_argument("--out-jsonl", default="/root/piggy/data/v129_sof_no_send_bundle_validation.jsonl")
    args = ap.parse_args()

    _load_env()
    _log("PGG2-V129-SOF-NO-SEND-START source=sof_gossip_raw_shreds")
    _log(
        "PGG2-V129-SOF-CONFIG "
        f"entrypoint={args.entrypoint} port_range={args.port_start}-{args.port_end} "
        f"min_external_lamports={args.min_external_lamports} max_build_attempts={args.max_build_attempts}"
    )
    _ensure_tip_account()

    out = Path(args.out_jsonl)
    out.parent.mkdir(parents=True, exist_ok=True)
    fp = out.open("w", encoding="utf-8")

    broker: Any | None = None
    counters: Counter[str] = Counter()
    seen: set[str] = set()
    started = time.time()
    proc = subprocess.Popen(
        ["/root/piggy/v128_sof_raw_ingest_probe/target/release/pgg2_v128_sof_raw_ingest_probe"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_sof_env(args),
        cwd="/root/piggy/v128_sof_raw_ingest_probe",
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.time() - started > args.max_seconds:
                counters["timeout"] += 1
                break
            sys.stdout.write(line)
            if "PGG2-V128-SOF-PUMP-RAW-TX" not in line:
                continue
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
            event_ts = _now_ms()
            try:
                decoded = decode_external_pump_buy(
                    str(rec["raw_tx_b64"]),
                    expected_sig=sig,
                    source="v129_sof",
                    slot=int(rec["slot"]),
                )
            except Exception as exc:
                counters[f"decode_block:{type(exc).__name__}:{str(exc)[:48]}"] += 1
                continue
            counters["decoded_buy"] += 1
            fp.write(json.dumps({"kind": "decoded_buy", "event_ts_ms": event_ts, **decoded.__dict__}, separators=(",", ":")) + "\n")
            fp.flush()
            _log(
                f"PGG2-V129-RAW-TX-SEEN source=sof mint={decoded.mint[:4]}.. "
                f"sig={decoded.signature[:16]} sol_lamports={decoded.sol_lamports}"
            )
            if int(decoded.sol_lamports) < args.min_external_lamports:
                counters["external_size_below_min"] += 1
                continue
            # Do not burn the prelanding window on an RPC status check before
            # bundle construction. V129 is a speed-path validator: the external
            # tx may land during one round trip. The authoritative no-send check
            # stays inside _build_validation_for_raw after the exact bundle is
            # built; if the external tx is already landed by then, it fails
            # closed with external_tx_landed_before_final_validation.
            if os.environ.get("V129_PREBUILD_STATUS_CHECK", "0").lower() in {"1", "true", "yes"}:
                status, status_err = _strict_sig_status(decoded.signature)
                if status == "rpc_error":
                    counters[f"status_rpc_error:{status_err}"] += 1
                    continue
                if status != "null":
                    counters[f"already_status:{status}"] += 1
                    continue
            if counters["build_attempts"] >= args.max_build_attempts:
                counters["build_attempt_cap"] += 1
                continue
            counters["build_attempts"] += 1
            if broker is None:
                broker = _make_broker()
            ok, reason = _build_validation_for_raw(broker=broker, decoded=decoded, event_ts_ms=event_ts)
            if ok:
                _log("PGG2-V129-SOF-NO-SEND-BUNDLE-READY-PASS")
                return 0
            counters[f"bundle_block:{reason}"] += 1
    finally:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        fp.close()

    _log(
        "PGG2-V129-SOF-NO-SEND-FINAL "
        + " ".join(f"{k}={v}" for k, v in counters.most_common(30))
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
