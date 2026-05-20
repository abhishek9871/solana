"""V53 risk intelligence module — SolanaTracker Data API.

Fetches risk fields for a mint via https://data.solanatracker.io/tokens/{mint}.
Caches 30s per mint, rate-limited to 1 req/sec / 100 req/min globally.
"""
from __future__ import annotations
import json
import time
from collections import deque
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

ST_DATA_BASE = "https://data.solanatracker.io"
CACHE_TTL_S = 30.0
MIN_INTERVAL_S = 1.05  # ~1 req/sec
MAX_PER_MIN = 100


class V53RiskChecker:
    def __init__(self, api_key: str):
        if not api_key or len(api_key) < 8:
            raise ValueError("v53_risk_api_key_invalid")
        self._key = api_key
        self._cache: dict[str, tuple[float, dict]] = {}
        self._last_call_ts = 0.0
        self._minute_window: deque = deque()

    def _rate_allow(self) -> bool:
        now = time.time()
        if now - self._last_call_ts < MIN_INTERVAL_S:
            return False
        while self._minute_window and now - self._minute_window[0] > 60.0:
            self._minute_window.popleft()
        return len(self._minute_window) < MAX_PER_MIN

    def _record_call(self) -> None:
        now = time.time()
        self._last_call_ts = now
        self._minute_window.append(now)

    def fetch(self, mint: str) -> dict:
        """Returns dict with normalized risk fields. On error: {"ok":False, "error":<reason>}."""
        cached = self._cache.get(mint)
        now = time.time()
        if cached and now - cached[0] < CACHE_TTL_S:
            d = dict(cached[1])
            d["cache_age_ms"] = int((now - cached[0]) * 1000)
            d["cached"] = True
            return d
        if not self._rate_allow():
            return {"ok": False, "error": "rate_limited", "cached": False}
        self._record_call()
        try:
            req = urlreq.Request(f"{ST_DATA_BASE}/tokens/{mint}", headers={"x-api-key": self._key})
            with urlreq.urlopen(req, timeout=8.0) as r:
                data = json.loads(r.read())
        except HTTPError as e:
            return {"ok": False, "error": f"http_{e.code}", "cached": False}
        except (URLError, json.JSONDecodeError, Exception) as e:
            return {"ok": False, "error": f"net_{type(e).__name__}", "cached": False}

        risk = data.get("risk") or {}
        sn = risk.get("snipers") or {}
        ins = risk.get("insiders") or {}
        bnd = risk.get("bundlers") or {}
        dv = risk.get("dev") or {}
        risks_raw = risk.get("risks") or []
        normalized = {
            "ok": True,
            "cached": False,
            "cache_age_ms": 0,
            "fetched_at_ts": now,
            "score": risk.get("score"),
            "rugged": bool(risk.get("rugged")),
            "snipers_count": sn.get("count") if isinstance(sn, dict) else None,
            "snipers_pct": sn.get("totalPercentage") if isinstance(sn, dict) else None,
            "insiders_count": ins.get("count") if isinstance(ins, dict) else None,
            "insiders_pct": ins.get("totalPercentage") if isinstance(ins, dict) else None,
            "bundlers_count": bnd.get("count") if isinstance(bnd, dict) else None,
            "bundlers_pct": bnd.get("totalPercentage") if isinstance(bnd, dict) else None,
            "dev_pct": dv.get("percentage") if isinstance(dv, dict) else None,
            "top10_pct": risk.get("top10"),
            "holders": data.get("holders"),
            "danger_names": [rr.get("name", "") for rr in risks_raw if rr.get("level") == "danger"],
            "warning_names": [rr.get("name", "") for rr in risks_raw if rr.get("level") == "warning"],
            "raw_risks": risks_raw,
        }
        self._cache[mint] = (now, normalized)
        return normalized


def evaluate_risk_veto(features: dict, rules: dict | None = None) -> tuple[bool, list[str]]:
    """Return (pass, blockers). pass=True means no veto fired."""
    if not features.get("ok"):
        return False, [f"v53_unavailable:{features.get('error','unknown')}"]
    r = rules or DEFAULT_RULES
    fired: list[str] = []

    if features.get("rugged") is True:
        fired.append("v53_rugged")

    bp = features.get("bundlers_pct")
    if bp is not None and bp > r["bundlers_pct_max"]:
        fired.append(f"v53_bundlers_pct_gt_{r['bundlers_pct_max']}")

    dp = features.get("dev_pct")
    if dp is not None and dp > r["dev_pct_max"]:
        fired.append(f"v53_dev_pct_gt_{r['dev_pct_max']}")

    sp = features.get("snipers_pct")
    if sp is not None and sp > r["snipers_pct_max"]:
        fired.append(f"v53_snipers_pct_gt_{r['snipers_pct_max']}")

    ip = features.get("insiders_pct")
    if ip is not None and ip > r["insiders_pct_max"]:
        fired.append(f"v53_insiders_pct_gt_{r['insiders_pct_max']}")

    hc = features.get("holders")
    if hc is not None and hc < r["holders_min"]:
        fired.append(f"v53_holders_lt_{r['holders_min']}")

    dangers = set(features.get("danger_names") or [])
    for name in r["block_danger_names"]:
        if name in dangers:
            fired.append(f"v53_danger:{name}")

    cache_age = features.get("cache_age_ms", 0)
    if cache_age > r["max_cache_age_ms"]:
        fired.append("v53_data_stale")

    return (len(fired) == 0), fired


DEFAULT_RULES = {
    "bundlers_pct_max": 25.0,        # loosened — was blocking borderline mints like DLjK at 15.47% that V47 stack accepted
    "dev_pct_max": 2.0,
    "snipers_pct_max": 10.0,
    "insiders_pct_max": 10.0,
    "holders_min": 8,                 # loosened — fresh pumps often have 5-9 holders early; 20 was over-blocking
    "block_danger_names": [
        "High Bundler Holdings",
        "Rug Pull",
        "Mint authority active",
        "Freeze authority active",
    ],
    "max_cache_age_ms": 30_000,
}
