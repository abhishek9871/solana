"""Pump.fun frontend-api-v3 engagement poller.

Phase 15B 2026-05-08: signals retail bots can't compute from raw on-chain data.
Polls pump.fun's own frontend for engagement metrics: livestream viewers,
chat reply count, currently-live status, market cap evolution.

Endpoints (no auth required as of May 2026):
  https://frontend-api-v3.pump.fun/coins/king-of-the-hill
  https://frontend-api-v3.pump.fun/coins/currently-live?offset=0&limit=N

Maintains in-memory state per mint:
  reply_count, num_participants (livestream viewers), is_currently_live,
  usd_market_cap, ath_market_cap, twitter, website, last_seen_ts
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

try:
    import aiohttp
except Exception:
    aiohttp = None


PUMP_FRONTEND_URL = "https://frontend-api-v3.pump.fun"
DEFAULT_POLL_SEC = 4.0
DEFAULT_LIMIT = 50


class PumpfunEngagementPoller:
    """Async poller of pump.fun frontend engagement endpoints."""

    def __init__(self, log_fn=None, poll_sec: float = DEFAULT_POLL_SEC,
                 limit: int = DEFAULT_LIMIT):
        self.log = log_fn or (lambda msg: print(msg))
        self.poll_sec = poll_sec
        self.limit = limit
        # mint -> dict of fields
        self._engaged: dict[str, dict] = {}
        self._koth_mint: Optional[str] = None
        self._koth_seen_ts_ms: int = 0
        self._running = False
        self._stats = {
            "polls_koth": 0,
            "polls_live": 0,
            "errors_koth": 0,
            "errors_live": 0,
            "engaged_max_count": 0,
        }
        self._reconnect_delay = 1.0

    def stats(self) -> dict:
        return dict(self._stats, tracked_mints=len(self._engaged), koth_mint=self._koth_mint)

    def is_engaged(self, mint: str, min_viewers: int = 10, min_replies: int = 0) -> bool:
        """Check if a mint has meaningful engagement signals."""
        info = self._engaged.get(mint)
        if not info:
            return False
        # Stale check — ignore if data is older than 60s
        if time.time() * 1000 - info.get("last_seen_ts", 0) > 60_000:
            return False
        viewers = int(info.get("num_participants") or 0)
        replies = int(info.get("reply_count") or 0)
        is_live = bool(info.get("is_currently_live"))
        if is_live and viewers >= min_viewers:
            return True
        if replies >= min_replies and replies > 5:
            return True
        return False

    def get_engagement(self, mint: str) -> Optional[dict]:
        """Return raw engagement info for a mint (or None if untracked)."""
        return self._engaged.get(mint)

    def is_koth(self, mint: str, max_age_sec: float = 60.0) -> bool:
        """True if this mint is currently the King of the Hill (was within max_age)."""
        if mint != self._koth_mint:
            return False
        return (time.time() * 1000 - self._koth_seen_ts_ms) <= max_age_sec * 1000

    async def run(self) -> None:
        if aiohttp is None:
            self.log("PUMPFUN-ENGAGE: aiohttp missing — exiting")
            return
        self._running = True
        self.log(f"PUMPFUN-ENGAGE: starting poller poll={self.poll_sec}s limit={self.limit}")
        while self._running:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=4.0),
                    headers={"User-Agent": "Mozilla/5.0 (compatible; pgg2-bot/1.0)"},
                ) as sess:
                    while self._running:
                        await self._poll_once(sess)
                        await asyncio.sleep(self.poll_sec)
            except asyncio.CancelledError:
                self._running = False
                self.log("PUMPFUN-ENGAGE: cancelled")
                return
            except Exception as e:
                self.log(f"PUMPFUN-ENGAGE: session error {type(e).__name__}: {e} — retrying {self._reconnect_delay:.1f}s")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 30.0)

    async def _poll_once(self, sess) -> None:
        # KOTH
        try:
            async with sess.get(f"{PUMP_FRONTEND_URL}/coins/king-of-the-hill") as r:
                self._stats["polls_koth"] += 1
                if r.status == 200:
                    txt = await r.text()
                    if txt.strip():
                        data = json.loads(txt)
                        if isinstance(data, dict) and data.get("mint"):
                            mint = data["mint"]
                            if mint != self._koth_mint:
                                self.log(f"PUMPFUN-KOTH change: {mint[:8]} replies={data.get('reply_count', 0)} viewers={data.get('num_participants', 0)}")
                            self._koth_mint = mint
                            self._koth_seen_ts_ms = int(time.time() * 1000)
                            self._update_mint(data)
                else:
                    self._stats["errors_koth"] += 1
        except Exception as e:
            self._stats["errors_koth"] += 1

        # Currently-live (livestream)
        try:
            url = f"{PUMP_FRONTEND_URL}/coins/currently-live?offset=0&limit={self.limit}&includeNsfw=false"
            async with sess.get(url) as r:
                self._stats["polls_live"] += 1
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for entry in data:
                            self._update_mint(entry)
                else:
                    self._stats["errors_live"] += 1
        except Exception as e:
            self._stats["errors_live"] += 1

        # Update max engaged count stat
        live_count = sum(1 for v in self._engaged.values() if v.get("is_currently_live"))
        if live_count > self._stats["engaged_max_count"]:
            self._stats["engaged_max_count"] = live_count

        # Prune stale entries (>5 min since last seen)
        cutoff = int(time.time() * 1000) - 300_000
        stale = [m for m, v in self._engaged.items() if v.get("last_seen_ts", 0) < cutoff]
        for m in stale:
            del self._engaged[m]

    def _update_mint(self, entry: dict) -> None:
        mint = entry.get("mint")
        if not mint:
            return
        info = {
            "reply_count": entry.get("reply_count") or 0,
            "num_participants": entry.get("num_participants") or 0,
            "is_currently_live": entry.get("is_currently_live") or False,
            "livestream_title": entry.get("livestream_title") or "",
            "twitter": entry.get("twitter") or "",
            "website": entry.get("website") or "",
            "usd_market_cap": entry.get("usd_market_cap") or 0,
            "ath_market_cap": entry.get("ath_market_cap") or 0,
            "is_cashback_enabled": entry.get("is_cashback_enabled") or False,
            "complete": entry.get("complete") or False,
            "created_timestamp": entry.get("created_timestamp") or 0,
            "last_seen_ts": int(time.time() * 1000),
        }
        self._engaged[mint] = info
