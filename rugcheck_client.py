"""RugCheck.xyz pre-buy safety gate.

Phase 15A 2026-05-08: real-time API call before each strike. Reject if
risk score >= threshold OR mint authority not renounced.

Endpoint: https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary
Free tier: ~30 req/min unauth, sufficient for our $40 bot.
Latency: ~300ms typical from Hetzner Ashburn.
Cache TTL: 5 min (data doesn't change rapidly post-mint).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

try:
    import aiohttp
except Exception:
    aiohttp = None


RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
DEFAULT_REJECT_SCORE = 4  # research-derived threshold
DEFAULT_TIMEOUT_SEC = 0.45  # blocking call budget — fail-open if slower
DEFAULT_CACHE_TTL_SEC = 300


class RugCheckClient:
    """Async pre-buy safety check. Caches results in-memory."""

    def __init__(self, log_fn=None, reject_score: int = DEFAULT_REJECT_SCORE,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 cache_ttl_sec: float = DEFAULT_CACHE_TTL_SEC):
        self.log = log_fn or (lambda msg: print(msg))
        self.reject_score = reject_score
        self.timeout_sec = timeout_sec
        self.cache_ttl_sec = cache_ttl_sec
        # mint -> (ts_ms, is_safe, score, reason)
        self._cache: dict[str, tuple[int, bool, int, str]] = {}
        self._stats = {
            "calls": 0,
            "cache_hits": 0,
            "rejects": 0,
            "passes": 0,
            "errors": 0,
            "timeouts": 0,
        }

    def stats(self) -> dict:
        return dict(self._stats)

    async def is_safe(self, mint: str) -> tuple[bool, int, str]:
        """Return (is_safe, score, reason). Fail-open if RugCheck unreachable."""
        if not mint:
            return (True, 0, "no_mint")
        now_ms = int(time.time() * 1000)
        cached = self._cache.get(mint)
        if cached and (now_ms - cached[0]) <= self.cache_ttl_sec * 1000:
            self._stats["cache_hits"] += 1
            return (cached[1], cached[2], cached[3])

        if aiohttp is None:
            return (True, 0, "aiohttp_missing")

        url = RUGCHECK_URL.format(mint=mint)
        self._stats["calls"] += 1
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        self._stats["errors"] += 1
                        # Fail-open on non-200 — don't block strikes if RugCheck is down
                        return (True, 0, f"http_{resp.status}")
                    data = await resp.json()
        except asyncio.TimeoutError:
            self._stats["timeouts"] += 1
            return (True, 0, "timeout_failopen")
        except Exception as e:
            self._stats["errors"] += 1
            return (True, 0, f"err_{type(e).__name__}_failopen")

        # Prefer score_normalised (1-10 scale). Raw score is sometimes 100+
        score_norm = data.get("score_normalised")
        score_raw = data.get("score")
        if score_norm is not None:
            score = int(score_norm)
        elif score_raw is not None and score_raw <= 10:
            score = int(score_raw)
        else:
            score = 0
        risks = data.get("risks") or []
        risk_names = [r.get("name", "") for r in risks if isinstance(r, dict)]

        # Reject if score >= threshold
        if score >= self.reject_score:
            self._stats["rejects"] += 1
            self._cache[mint] = (now_ms, False, score, f"score_{score}")
            return (False, score, f"score_{score}")

        # Reject if mint authority not renounced
        for risk_name in risk_names:
            if "Mint Authority" in risk_name and "renounced" not in risk_name.lower():
                self._stats["rejects"] += 1
                reason = f"mint_authority_active"
                self._cache[mint] = (now_ms, False, score, reason)
                return (False, score, reason)

        # Reject if specific high-severity flags
        critical_risks = {"Freeze Authority", "Top 10 holders high"}
        for risk_name in risk_names:
            for crit in critical_risks:
                if crit in risk_name:
                    self._stats["rejects"] += 1
                    reason = f"risk_{crit.replace(' ', '_')}"
                    self._cache[mint] = (now_ms, False, score, reason)
                    return (False, score, reason)

        # Passed all checks
        self._stats["passes"] += 1
        self._cache[mint] = (now_ms, True, score, "ok")
        return (True, score, "ok")

    def is_safe_sync(self, mint: str) -> tuple[bool, int, str]:
        """Sync version using urllib.request — safe to call from inside event loops.
        Used by the strike-path filter where async/await isn't available."""
        if not mint:
            return (True, 0, "no_mint")
        now_ms = int(time.time() * 1000)
        cached = self._cache.get(mint)
        if cached and (now_ms - cached[0]) <= self.cache_ttl_sec * 1000:
            self._stats["cache_hits"] += 1
            return (cached[1], cached[2], cached[3])

        import urllib.request
        import urllib.error
        url = RUGCHECK_URL.format(mint=mint)
        self._stats["calls"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pgg2-bot/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                if resp.status != 200:
                    self._stats["errors"] += 1
                    return (True, 0, f"http_{resp.status}_failopen")
                raw = resp.read()
                data = json.loads(raw)
        except urllib.error.URLError as e:
            self._stats["timeouts"] += 1 if "timed out" in str(e) else 0
            self._stats["errors"] += 1
            return (True, 0, f"err_failopen")
        except Exception as e:
            self._stats["errors"] += 1
            return (True, 0, f"err_{type(e).__name__}_failopen")

        # Prefer score_normalised (1-10 scale). Raw score is sometimes 100+
        score_norm = data.get("score_normalised")
        score_raw = data.get("score")
        if score_norm is not None:
            score = int(score_norm)
        elif score_raw is not None and score_raw <= 10:
            score = int(score_raw)
        else:
            score = 0
        risks = data.get("risks") or []
        risk_names = [r.get("name", "") for r in risks if isinstance(r, dict)]

        if score >= self.reject_score:
            self._stats["rejects"] += 1
            self._cache[mint] = (now_ms, False, score, f"score_{score}")
            return (False, score, f"score_{score}")

        for risk_name in risk_names:
            if "Mint Authority" in risk_name and "renounced" not in risk_name.lower():
                self._stats["rejects"] += 1
                reason = "mint_authority_active"
                self._cache[mint] = (now_ms, False, score, reason)
                return (False, score, reason)

        critical_risks = {"Freeze Authority", "Top 10 holders high"}
        for risk_name in risk_names:
            for crit in critical_risks:
                if crit in risk_name:
                    self._stats["rejects"] += 1
                    reason = f"risk_{crit.replace(' ', '_')}"
                    self._cache[mint] = (now_ms, False, score, reason)
                    return (False, score, reason)

        self._stats["passes"] += 1
        self._cache[mint] = (now_ms, True, score, "ok")
        return (True, score, "ok")
