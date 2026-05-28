#!/usr/bin/env python3
"""V108 Jito bundle sender wrapper.

This file implements the only permitted send surface for V108: Jito
`sendBundle`. It has an explicit dry-run mode and no RPC/Sender fallback.
"""
from __future__ import annotations

import json
import os
import concurrent.futures
import http.client
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Iterable


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _load_env() -> None:
    p = "/root/piggy/.env"
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def jito_block_engine_url() -> str:
    _load_env()
    return (
        os.environ.get("PGG2_JITO_BLOCK_ENGINE_URL")
        or os.environ.get("JITO_BLOCK_ENGINE_URL")
        or "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    ).rstrip("/")


def jito_tip_account() -> str:
    _load_env()
    return (os.environ.get("PGG2_JITO_TIP_ACCOUNT") or os.environ.get("JITO_TIP_ACCOUNT") or "").strip()


STATIC_JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

REGIONAL_JITO_BUNDLE_ENDPOINTS = [
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles",
]

_CONN_LOCK = threading.Lock()
_CONNS: dict[tuple[str, int], http.client.HTTPSConnection] = {}


def _redact_text(text: str) -> str:
    out = re.sub(r"([?&](?:api_key|api-key|token|key)=)[^&\\s]+", r"\1...", str(text), flags=re.I)
    for name in ("RPCFAST_API_KEY", "SHYFT_API_KEY", "HELIUS_API_KEY", "SOLANATRACKER_DATA_API_KEY"):
        val = os.environ.get(name)
        if val:
            out = out.replace(val, "...")
    return out


def _redact_url(url: str) -> str:
    return _redact_text(url)


def _region(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc or url
    return host.split(".", 1)[0]


def _bundle_send_urls() -> list[str]:
    configured = os.environ.get("PGG2_BUNDLE_SEND_URLS", "").strip()
    if configured:
        urls = [x.strip().rstrip("/") for x in configured.split(",") if x.strip()]
    else:
        urls = [jito_block_engine_url()]
    if os.environ.get("PGG2_JITO_RACE_REGIONS", "0").lower() in {"1", "true", "yes"}:
        urls += REGIONAL_JITO_BUNDLE_ENDPOINTS
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url.rstrip("/"))
    return out


def _endpoint_for(url: str, path: str) -> str:
    url = url.rstrip("/")
    for suffix in ("/api/v1/bundles", "/api/v1/getTipAccounts", "/api/v1/getBundleStatuses", "/api/v1/getInflightBundleStatuses"):
        if url.endswith(suffix):
            return url[: -len(suffix)] + path
    return url.rstrip("/") + path


def _get_conn(url: str) -> tuple[http.client.HTTPSConnection, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError(f"unsupported_endpoint_scheme:{parsed.scheme}")
    port = parsed.port or 443
    key = (parsed.hostname or "", port)
    if not key[0]:
        raise RuntimeError("bad_endpoint_host")
    with _CONN_LOCK:
        conn = _CONNS.get(key)
        if conn is None:
            conn = http.client.HTTPSConnection(key[0], port, timeout=float(os.environ.get("PGG2_BUNDLE_HTTP_TIMEOUT_SEC", "1.5") or 1.5))
            _CONNS[key] = conn
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return conn, path


def _rpc_persistent(url: str, method: str, params: list[Any], timeout: float = 1.5) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, separators=(",", ":")).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(2):
        conn, path = _get_conn(url)
        try:
            conn.timeout = timeout
            conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"{method}_http_{resp.status}:{_redact_text(body[:800])}")
            parsed = json.loads(body)
            if parsed.get("error"):
                raise RuntimeError(f"{method}_error:{_redact_text(str(parsed['error']))}")
            return parsed.get("result")
        except Exception as exc:
            last_exc = exc
            parsed = urllib.parse.urlparse(url)
            key = (parsed.hostname or "", parsed.port or 443)
            with _CONN_LOCK:
                old = _CONNS.pop(key, None)
            try:
                if old:
                    old.close()
            except Exception:
                pass
            if attempt == 0:
                continue
    raise RuntimeError(_redact_text(str(last_exc or "rpc_persistent_failed")))


def _rpc(url: str, method: str, params: list[Any], timeout: float = 6.0) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"{method}_http_{exc.code}:{_redact_text(body)}") from exc
    if parsed.get("error"):
        raise RuntimeError(f"{method}_error:{parsed['error']}")
    return parsed.get("result")


def _endpoint(path: str) -> str:
    url = jito_block_engine_url()
    for suffix in ("/api/v1/bundles", "/api/v1/getTipAccounts", "/api/v1/getBundleStatuses", "/api/v1/getInflightBundleStatuses"):
        if url.endswith(suffix):
            return url[: -len(suffix)] + path
    return url.rstrip("/") + path


def get_tip_accounts() -> list[str]:
    url = _endpoint("/api/v1/getTipAccounts")
    try:
        result = _rpc(url, "getTipAccounts", [])
        if isinstance(result, list) and result:
            return [str(x) for x in result]
    except Exception:
        pass
    return list(STATIC_JITO_TIP_ACCOUNTS)


def warm_bundle_endpoints() -> None:
    """Open HTTPS connections to bundle endpoints before the first hot packet."""
    urls = _bundle_send_urls()
    for url in urls:
        warm_url = _endpoint_for(url, "/api/v1/getTipAccounts")
        started = time.perf_counter()
        try:
            _rpc_persistent(warm_url, "getTipAccounts", [], timeout=1.5)
            ms = int((time.perf_counter() - started) * 1000)
            _log(f"PGG2-V108-JITO-ENDPOINT-WARM region={_region(url)} ms={ms} endpoint={_redact_url(url)}")
        except Exception as exc:
            ms = int((time.perf_counter() - started) * 1000)
            _log(f"PGG2-V108-JITO-ENDPOINT-WARM-ERR region={_region(url)} ms={ms} err={_redact_text(type(exc).__name__ + ':' + str(exc))[:240]}")


def get_inflight_bundle_statuses(bundle_ids: list[str]) -> Any:
    return _rpc(_endpoint("/api/v1/getInflightBundleStatuses"), "getInflightBundleStatuses", [bundle_ids])


def get_bundle_statuses(bundle_ids: list[str]) -> Any:
    return _rpc(_endpoint("/api/v1/getBundleStatuses"), "getBundleStatuses", [bundle_ids])


def wait_bundle_status(bundle_id: str, *, timeout_sec: float = 20.0, poll_sec: float = 0.5) -> dict[str, Any]:
    """Poll Jito status endpoints until the bundle lands/fails or times out."""
    deadline = time.time() + max(0.5, float(timeout_sec))
    last: dict[str, Any] = {"bundle_id": bundle_id, "status": "unknown"}
    while time.time() < deadline:
        try:
            inflight = get_inflight_bundle_statuses([bundle_id])
            vals = (inflight or {}).get("value") if isinstance(inflight, dict) else None
            if vals:
                row = vals[0] or {}
                status = str(row.get("status") or row.get("confirmationStatus") or "").lower()
                last = {"bundle_id": bundle_id, "status": status or "inflight", "raw": row}
                _log(f"PGG2-V108-JITO-BUNDLE-STATUS bundle_id={bundle_id} status={last['status']}")
                if status in {"landed", "failed", "invalid"}:
                    return last
        except Exception as exc:
            last = {"bundle_id": bundle_id, "status": "inflight_status_error", "error": f"{type(exc).__name__}:{exc}"}
        try:
            landed = get_bundle_statuses([bundle_id])
            vals = (landed or {}).get("value") if isinstance(landed, dict) else None
            if vals:
                row = vals[0] or {}
                err = row.get("err")
                status = str(row.get("confirmationStatus") or "landed").lower()
                last = {"bundle_id": bundle_id, "status": "landed" if err is None else "failed", "confirmationStatus": status, "raw": row}
                _log(f"PGG2-V108-JITO-BUNDLE-STATUS bundle_id={bundle_id} status={last['status']} confirmation={status} err={err}")
                return last
        except Exception as exc:
            last = {"bundle_id": bundle_id, "status": "bundle_status_error", "error": f"{type(exc).__name__}:{exc}"}
        time.sleep(max(0.1, float(poll_sec)))
    _log(f"PGG2-V108-JITO-BUNDLE-STATUS bundle_id={bundle_id} status=timeout")
    return {"bundle_id": bundle_id, "status": "timeout", "last": last}


def send_bundle(txs_b64: Iterable[str], *, dry_run: bool = True) -> dict[str, Any]:
    """Send a bundle if dry_run is false.

    V108 callers must use dry_run for validation. Live Stage A can set
    dry_run=False only after no-send bundle validation has passed.
    """
    txs = [str(x) for x in txs_b64 if str(x)]
    urls = _bundle_send_urls()
    if not txs:
        raise RuntimeError("empty_bundle")
    if len(txs) > 5:
        raise RuntimeError("bundle_too_large")
    _log(
        f"PGG2-V108-JITO-BUNDLE-SEND dry_run={int(dry_run)} tx_count={len(txs)} "
        f"endpoints={','.join(_region(u) for u in urls)}"
    )
    if dry_run:
        _log("PGG2-V108-JITO-BUNDLE-NOT-LANDED dry_run=1 reason=no_send_validation")
        return {"dry_run": True, "bundle_id": "", "tx_count": len(txs)}

    def send_one(url: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            bundle_id = _rpc_persistent(url, "sendBundle", [txs, {"encoding": "base64"}], timeout=float(os.environ.get("PGG2_BUNDLE_HTTP_TIMEOUT_SEC", "1.5") or 1.5))
            ms = int((time.perf_counter() - started) * 1000)
            return {"ok": True, "bundle_id": bundle_id, "url": url, "region": _region(url), "ms": ms}
        except Exception as exc:
            ms = int((time.perf_counter() - started) * 1000)
            return {"ok": False, "url": url, "region": _region(url), "ms": ms, "error": _redact_text(type(exc).__name__ + ":" + str(exc))}

    workers = min(len(urls), int(os.environ.get("PGG2_BUNDLE_RACE_WORKERS", "5") or 5))
    errors: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(send_one, url) for url in urls]
        for fut in concurrent.futures.as_completed(futs, timeout=float(os.environ.get("PGG2_BUNDLE_RACE_TIMEOUT_SEC", "2.0") or 2.0)):
            res = fut.result()
            if res.get("ok"):
                _log(f"PGG2-V108-JITO-BUNDLE-SEND-RESULT region={res['region']} ms={res['ms']} status=submitted")
                _log(f"PGG2-V108-JITO-BUNDLE-STATUS bundle_id={res['bundle_id']} status=submitted")
                return {"dry_run": False, "bundle_id": res["bundle_id"], "tx_count": len(txs), "endpoint": res["url"], "region": res["region"], "send_ms": res["ms"]}
            errors.append(res)
            _log(f"PGG2-V108-JITO-BUNDLE-SEND-ERR region={res['region']} ms={res['ms']} err={str(res.get('error'))[:240]}")
    summary = ";".join(f"{e.get('region')}:{e.get('ms')}ms:{str(e.get('error'))[:160]}" for e in errors[:8])
    raise RuntimeError(f"sendBundle_all_endpoints_failed:{summary}")


def main() -> int:
    _load_env()
    url = jito_block_engine_url()
    acct = jito_tip_account()
    _log(f"PGG2-V108-JITO-CONFIG endpoint={url} tip_account_present={int(bool(acct))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
