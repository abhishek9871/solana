"""V74 Helius Sender SWQOS adapter.

V73 buys were authorised, sent, and returned signatures from
`broker.send_signed()` — but the txs never landed on chain. Root cause:
the broker's default `send_signed` calls `mainnet.helius-rpc.com/?api-key=...`
which is a regular `sendTransaction` endpoint. With the V72 minimum
SWQOS tip of 0.000005 SOL, the tx cannot outcompete other pump.fun
snipers' txs in the leader queue and silently loses inclusion.

V74 fixes this by routing every live buy/sell tx through the official
Helius Sender SWQOS-only endpoint:

  https://sender.helius-rpc.com/fast?swqos_only=true

with the spec-required settings:

  * JSON-RPC `sendTransaction`
  * encoding `base64`
  * `skipPreflight=true`
  * `maxRetries=0`
  * tip exactly 0.000005 SOL (set elsewhere on the tx; this adapter
    validates that the tip is present but does not modify the tx).

The adapter exposes:
  * `make_sender(api_key)` — factory returning a `V74Sender` instance
  * `V74Sender.send_signed(signed_b64) -> sig` — drop-in replacement
    for broker.send_signed
  * `V74Sender.ping() -> bool` — keepwarm endpoint
  * `V74Sender.validate_endpoint() -> bool` — boot-time check

The pgg2_v71_live.py orchestrator monkey-patches `broker.send_signed`
to call `V74Sender.send_signed` when `PGG2_V74_MODE=1`. This is the
only sanctioned way to perform a live buy in V74 mode.

Hard rule: if endpoint URL does not literally contain
`sender.helius-rpc.com/fast?swqos_only=true` the adapter refuses
to send and raises a fatal exception.
"""
from __future__ import annotations

import base64 as _b64
import json
import os
import time
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUIRED_ENDPOINT_FRAGMENT = "sender.helius-rpc.com/fast"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class V74SenderEndpointError(RuntimeError):
    """Raised when the configured endpoint is not the Sender SWQOS URL."""


class V74Sender:
    """Helius Sender SWQOS-only `sendTransaction` adapter.

    Instances are inexpensive; create one per bot process.
    """

    def __init__(
        self,
        send_url: str,
        ping_url: Optional[str] = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.send_url = send_url
        self.ping_url = ping_url or "https://sender.helius-rpc.com/ping"
        self.log_fn = log_fn
        self._last_ping_ts: float = 0.0
        self._send_count: int = 0
        self._send_ok_count: int = 0
        self.validate_endpoint()
        log_fn(
            f"PGG2-V74-SENDER-ENDPOINT url={self.send_url} ping={self.ping_url}"
        )

    def validate_endpoint(self) -> bool:
        if REQUIRED_ENDPOINT_FRAGMENT not in self.send_url:
            raise V74SenderEndpointError(
                f"V74 send endpoint must contain "
                f"'{REQUIRED_ENDPOINT_FRAGMENT}'; got {self.send_url}"
            )
        # Require swqos_only=true query parameter for SWQOS-only routing.
        if "swqos_only=true" not in self.send_url.lower():
            raise V74SenderEndpointError(
                f"V74 send endpoint must carry "
                f"'swqos_only=true' query param; got {self.send_url}"
            )
        return True

    def send_signed(
        self, signed_b64: str, timeout_sec: float = 6.0
    ) -> str:
        """Send a signed transaction via Helius Sender SWQOS endpoint.

        Returns the transaction signature as base58 string. Raises on
        HTTP error or RPC error. Note: a successful return does NOT
        mean the tx landed — caller MUST confirm via signatureSubscribe
        before treating the tx as effective.
        """
        if REQUIRED_ENDPOINT_FRAGMENT not in self.send_url:
            raise V74SenderEndpointError(
                f"refusing to send to non-Sender endpoint {self.send_url}"
            )
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0xffff,
            "method": "sendTransaction",
            "params": [
                signed_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        }
        self._send_count += 1
        req = Request(
            self.send_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
        except HTTPError as exc:
            body_preview = ""
            try:
                body_preview = exc.read().decode("utf-8", "replace")[:500]
            except Exception as body_exc:
                body_preview = f"<body_read_failed:{type(body_exc).__name__}>"
            body_preview = body_preview.replace("\n", "\\n").replace("\r", "\\r")
            self.log_fn(
                f"PGG2-V74-SENDER-RESULT status=http_error code={exc.code} "
                f"body={body_preview!r}"
            )
            raise
        except URLError as exc:
            self.log_fn(
                f"PGG2-V74-SENDER-RESULT status=url_error reason={exc.reason}"
            )
            raise
        if "error" in data:
            err = data["error"]
            self.log_fn(
                f"PGG2-V74-SENDER-RESULT status=rpc_error err={err}"
            )
            raise RuntimeError(f"Sender RPC error: {err}")
        sig = data.get("result")
        if not sig:
            self.log_fn(
                f"PGG2-V74-SENDER-RESULT status=no_signature data={data}"
            )
            raise RuntimeError("Sender returned no signature")
        self._send_ok_count += 1
        self.log_fn(
            f"PGG2-V74-SENDER-SEND sig={sig} send_count={self._send_count} "
            f"ok_count={self._send_ok_count}"
        )
        return sig

    def ping(self, timeout_sec: float = 3.0) -> bool:
        """Keepwarm: lightweight GET to /ping. Logs failures but does
        not raise (callers should not block on transient ping fails).
        """
        try:
            req = Request(self.ping_url, method="GET")
            with urlopen(req, timeout=timeout_sec) as resp:
                resp.read()
            self.log_fn(f"PGG2-V74-SENDER-PING url={self.ping_url}")
            self._last_ping_ts = time.time()
            return True
        except Exception as exc:
            self.log_fn(
                f"PGG2-V74-SENDER-PING-FAIL err={type(exc).__name__}:{exc}"
            )
            return False


def make_sender(
    api_key: Optional[str] = None,
    log_fn: Callable[[str], None] = print,
) -> V74Sender:
    """Factory. Reads PGG2_V74_SENDER_URL or constructs from API key."""
    send_url = os.environ.get("PGG2_V74_SENDER_URL", "").strip()
    if not send_url:
        # Default: official Helius Sender SWQOS-only endpoint.
        send_url = "https://sender.helius-rpc.com/fast?swqos_only=true"
    ping_url = os.environ.get(
        "PGG2_V74_SENDER_PING_URL", "https://sender.helius-rpc.com/ping"
    )
    return V74Sender(send_url=send_url, ping_url=ping_url, log_fn=log_fn)


def install_into_broker(
    broker: Any, sender: V74Sender, log_fn: Callable[[str], None] = print
) -> Callable[[], None]:
    """Monkey-patch `broker.send_signed` to use the V74 Sender.

    Returns an `uninstall` callable that restores the original method.
    Idempotent: re-installing on top of an already-patched broker
    refers back to the original (un-patched) `send_signed`.
    """
    original_send_signed = getattr(broker, "_pgg2_v74_original_send_signed", None)
    if original_send_signed is None:
        original_send_signed = broker.send_signed
        setattr(
            broker,
            "_pgg2_v74_original_send_signed",
            original_send_signed,
        )

    def patched_send_signed(signed_b64: str) -> str:
        return sender.send_signed(signed_b64)

    broker.send_signed = patched_send_signed
    log_fn(
        f"PGG2-V74-SENDER-INSTALLED into_broker_id={id(broker)} "
        f"endpoint={sender.send_url}"
    )

    def uninstall() -> None:
        broker.send_signed = original_send_signed
        log_fn("PGG2-V74-SENDER-UNINSTALLED")

    return uninstall


async def keepwarm_loop(
    sender: V74Sender,
    interval_sec: float = 5.0,
    stop_event: Any = None,
    log_fn: Callable[[str], None] = print,
) -> None:
    """Run as an asyncio task. Pings the Sender /ping endpoint every
    `interval_sec` seconds. Stops when `stop_event.is_set()` is True
    or the task is cancelled.
    """
    import asyncio

    if stop_event is None:
        # Use a never-set event as default (run until cancelled).
        stop_event = asyncio.Event()
    while not stop_event.is_set():
        sender.ping()
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=interval_sec
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            return
