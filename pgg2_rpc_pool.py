"""Multi-endpoint Solana RPC pool with per-endpoint token-bucket rate limiting,
sliding-window health tracking, and round-robin failover.

Solves the "fisherman sleeping" problem: a single 5-10 req/s RPC endpoint hits
its limit after ~40s of bursty traffic, latency degrades 40x, candidates_seen
counter freezes. Combining multiple endpoints with rate-bucket dispatch keeps
sustainable throughput high (e.g., 25 req/s across 3 endpoints).

Configuration via env var PGG2_RPC_POOL_ENDPOINTS — pipe-separated entries of
the form NAME=URL@RATE_PER_SEC, e.g.:

  PGG2_RPC_POOL_ENDPOINTS="st=https://rpc-mainnet.solanatracker.io/?api_key=K@5|helius=https://mainnet.helius-rpc.com/?api-key=K@10|helius_beta=https://beta.helius-rpc.com/?api-key=K@10"

If env var is absent, falls back to single endpoint via PGG2_LIVE_RPC_URL /
SOLANATRACKER_RPC_HTTP for backward compatibility.

Per-endpoint state tracked in-process (single broker instance assumed). Thread-
safe via a single lock around endpoint selection + bucket refill.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass
class _Endpoint:
    name: str
    url: str
    rate_per_sec: float
    headers: dict[str, str] = field(default_factory=dict)
    tokens: float = 0.0
    last_refill_ts: float = 0.0
    cooldown_until_ts: float = 0.0
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0
    total_throttles: int = 0
    last_latency_ms: int = 0


class RPCPool:
    """Dispatch JSON-RPC requests across N endpoints, each rate-limited."""

    def __init__(
        self,
        endpoints: list[_Endpoint],
        timeout_sec: float = 4.0,
        cooldown_on_error_sec: float = 2.0,
        cooldown_on_throttle_sec: float = 10.0,
        max_retries: int = 3,
        logger: Optional[Any] = None,
    ) -> None:
        if not endpoints:
            raise ValueError("RPCPool needs at least 1 endpoint")
        self._endpoints = endpoints
        self._timeout_sec = float(timeout_sec)
        self._cooldown_on_error = float(cooldown_on_error_sec)
        self._cooldown_on_throttle = float(cooldown_on_throttle_sec)
        self._max_retries = int(max_retries)
        self._lock = threading.RLock()
        self._logger = logger or (lambda *a, **kw: None)
        now = time.time()
        for ep in self._endpoints:
            ep.tokens = ep.rate_per_sec
            ep.last_refill_ts = now

    @property
    def endpoint_names(self) -> list[str]:
        return [ep.name for ep in self._endpoints]

    def stats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                ep.name: {
                    "url_host": ep.url.split("/")[2] if "//" in ep.url else ep.url,
                    "rate": ep.rate_per_sec,
                    "tokens": round(ep.tokens, 2),
                    "cooldown_remaining": max(0.0, ep.cooldown_until_ts - time.time()),
                    "requests": ep.total_requests,
                    "errors": ep.total_errors,
                    "throttles": ep.total_throttles,
                    "last_latency_ms": ep.last_latency_ms,
                }
                for ep in self._endpoints
            }

    def _refill_and_select(self) -> Optional[_Endpoint]:
        """Refill all buckets, return the endpoint with the most tokens that's
        not in cooldown. Returns None if all endpoints are exhausted/cooled."""
        now = time.time()
        best: Optional[_Endpoint] = None
        with self._lock:
            for ep in self._endpoints:
                if now < ep.cooldown_until_ts:
                    continue
                elapsed = now - ep.last_refill_ts
                if elapsed > 0:
                    ep.tokens = min(ep.rate_per_sec, ep.tokens + elapsed * ep.rate_per_sec)
                    ep.last_refill_ts = now
                if ep.tokens < 1.0:
                    continue
                if best is None or ep.tokens > best.tokens:
                    best = ep
            if best is not None:
                best.tokens -= 1.0
                best.total_requests += 1
        return best

    def _post_json(self, ep: _Endpoint, payload: dict[str, Any]) -> tuple[Any, int]:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json"}
        headers.update(ep.headers)
        req = Request(ep.url, data=data, headers=headers, method="POST")
        t0 = time.time()
        with urlopen(req, timeout=self._timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
        latency_ms = int((time.time() - t0) * 1000)
        return (json.loads(raw) if raw else {}), latency_ms

    def call(self, method: str, params: list[Any]) -> Any:
        """Make a JSON-RPC call across the pool, retrying on transient errors."""
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        last_exc: Optional[Exception] = None
        tried: set[str] = set()
        for attempt in range(self._max_retries):
            ep: Optional[_Endpoint] = None
            # Try to pick an endpoint we haven't tried yet
            for _ in range(len(self._endpoints) * 2):
                candidate = self._refill_and_select()
                if candidate is None:
                    break
                if candidate.name not in tried:
                    ep = candidate
                    break
                # Already tried this attempt — return token, briefly cool it
                with self._lock:
                    candidate.tokens += 1.0
                # Yield to let another endpoint refill
                time.sleep(0.01)
            if ep is None:
                # All endpoints exhausted/cooling — short sleep and retry the loop
                time.sleep(0.02)
                tried.clear()
                continue
            tried.add(ep.name)
            try:
                out, latency_ms = self._post_json(ep, payload)
                with self._lock:
                    ep.last_latency_ms = latency_ms
                    ep.consecutive_errors = 0
                if "error" in out:
                    err = out["error"]
                    err_code = err.get("code") if isinstance(err, dict) else None
                    # JSON-RPC rate-limit codes vary; -32005 = limit exceeded (common)
                    if err_code in (-32005, -32007, -32008, -32014):
                        with self._lock:
                            ep.total_throttles += 1
                            ep.cooldown_until_ts = time.time() + self._cooldown_on_throttle
                        self._logger(
                            f"PGG2-RPC-POOL-THROTTLE endpoint={ep.name} method={method} "
                            f"err_code={err_code} cooldown_s={self._cooldown_on_throttle}"
                        )
                        last_exc = RuntimeError(f"rpc {method} json-rpc rate limit: {err}")
                        continue
                    raise RuntimeError(f"rpc {method} json-rpc error: {err}")
                return out.get("result")
            except HTTPError as exc:
                body_text = ""
                try:
                    body_text = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                with self._lock:
                    ep.total_errors += 1
                    ep.consecutive_errors += 1
                    if exc.code == 429:
                        ep.total_throttles += 1
                        ep.cooldown_until_ts = time.time() + self._cooldown_on_throttle
                        self._logger(
                            f"PGG2-RPC-POOL-THROTTLE endpoint={ep.name} method={method} "
                            f"http=429 cooldown_s={self._cooldown_on_throttle} body={body_text[:120]}"
                        )
                    elif exc.code in (500, 502, 503, 504):
                        ep.cooldown_until_ts = time.time() + self._cooldown_on_error
                        self._logger(
                            f"PGG2-RPC-POOL-SERVER-ERR endpoint={ep.name} method={method} "
                            f"http={exc.code} cooldown_s={self._cooldown_on_error}"
                        )
                last_exc = RuntimeError(f"http {exc.code} {exc.reason}: {body_text}")
                if exc.code not in (429, 500, 502, 503, 504):
                    # Non-retryable HTTP error
                    raise last_exc
                continue
            except Exception as exc:
                with self._lock:
                    ep.total_errors += 1
                    ep.consecutive_errors += 1
                    if ep.consecutive_errors >= 3:
                        ep.cooldown_until_ts = time.time() + self._cooldown_on_error
                        self._logger(
                            f"PGG2-RPC-POOL-ENDPOINT-COOLED endpoint={ep.name} "
                            f"consecutive_errors={ep.consecutive_errors} "
                            f"cooldown_s={self._cooldown_on_error}"
                        )
                        ep.consecutive_errors = 0
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError(f"rpc {method} pool exhausted after {self._max_retries} attempts")


def parse_endpoints_env(env_value: str) -> list[_Endpoint]:
    """Parse PGG2_RPC_POOL_ENDPOINTS into _Endpoint list.

    Format: name=url@rate[,header_k=header_v]*  (pipe-separated)
    Example: "st=https://rpc-mainnet.solanatracker.io/?api_key=K@5|helius=https://mainnet.helius-rpc.com/?api-key=K@10"
    """
    out: list[_Endpoint] = []
    for raw in env_value.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        # Split off name=
        if "=" not in raw:
            continue
        name, rest = raw.split("=", 1)
        # rest = url@rate[,headers...]
        url_rate, _, header_blob = rest.partition(",")
        if "@" not in url_rate:
            url, rate = url_rate, "5"
        else:
            url, _, rate = url_rate.rpartition("@")
        try:
            rate_f = float(rate)
        except ValueError:
            rate_f = 5.0
        headers: dict[str, str] = {}
        if header_blob:
            for h in header_blob.split(";"):
                if "=" in h:
                    k, v = h.split("=", 1)
                    headers[k.strip()] = v.strip()
        out.append(_Endpoint(name=name.strip(), url=url.strip(), rate_per_sec=rate_f, headers=headers))
    return out


def build_pool_from_env(
    logger: Optional[Any] = None,
    timeout_sec: float = 4.0,
) -> Optional[RPCPool]:
    """Read PGG2_RPC_POOL_ENDPOINTS env and build an RPCPool. Returns None if
    the env var is empty or unparseable (caller should fall back to single-URL)."""
    raw = os.environ.get("PGG2_RPC_POOL_ENDPOINTS", "").strip()
    if not raw:
        return None
    endpoints = parse_endpoints_env(raw)
    if not endpoints:
        return None
    return RPCPool(endpoints=endpoints, timeout_sec=timeout_sec, logger=logger)


if __name__ == "__main__":
    # Smoke test
    pool = RPCPool(
        endpoints=[
            _Endpoint(name="dummy1", url="https://invalid.example/", rate_per_sec=5.0),
            _Endpoint(name="dummy2", url="https://invalid.example/", rate_per_sec=10.0),
        ],
        logger=lambda *a, **kw: print(*a, **kw),
        max_retries=1,
    )
    print("Endpoints:", pool.endpoint_names)
    print("Stats:", pool.stats())
    # Should fail with connection error since URLs are fake
    try:
        pool.call("getHealth", [])
    except Exception as exc:
        print(f"Expected failure: {type(exc).__name__}: {exc}")
    print("After call:", pool.stats())
