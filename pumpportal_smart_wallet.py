"""PumpPortal smart-wallet WebSocket client.

Subscribes to wss://pumpportal.fun/api/data subscribeAccountTrade for a list
of known smart wallets. Tracks recent buys per-mint. Exposes signal:
`is_smart_wallet_buying(mint, window_sec)` returns count of distinct smart
wallets that bought the mint in the time window.

Research source: Marino arXiv 2602.14860 + DeFade insider-network research:
when 2+ independently-funded alpha wallets buy the same mint within 5s,
that's the strongest documented composite signal for moonshot probability.

This module runs as an asyncio task alongside the main bot loop. It maintains
in-memory state (no persistence — restarts re-establish). The bot reads via
the public methods.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Optional

try:
    import websockets
except Exception:  # pragma: no cover — optional dep, fail loudly at runtime
    websockets = None


PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# Seed list of known smart wallets from prior session memory (777TGV9g,
# AJLyzkpM, DQApNebk demonstrated +$5-9 across 9-20 trades). Override via
# SMART_WALLET_LIST env (comma-separated) or SMART_WALLET_FILE (path to txt,
# one wallet per line).
DEFAULT_SMART_WALLETS = [
    "777TGV9gh38jovWqBp5Hv6vBs9NW1GjJfmZJYwzPBmaA",  # +$9.01 / 20 trades
    "AJLyzkpMfeQ4z6fyo5FcwzqTBZcW8HVMJPLtuojBmLqA",  # +$6.17 / 9 trades
    "DQApNebkrQqKoHe87jE1mHJEpAr8K3cN7zTBKzQqDPzL",  # +$5.86 / 17 trades
]


def _load_smart_wallets() -> list[str]:
    env_list = os.environ.get("SMART_WALLET_LIST", "").strip()
    if env_list:
        return [w.strip() for w in env_list.split(",") if w.strip()]
    file_path = os.environ.get("SMART_WALLET_FILE", "").strip()
    if file_path and os.path.exists(file_path):
        with open(file_path) as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return list(DEFAULT_SMART_WALLETS)


class SmartWalletTracker:
    """Background task that keeps a sliding window of smart-wallet buys per mint."""

    def __init__(self, log_fn=None, window_sec: float = 30.0):
        self.window_sec = window_sec
        self.log = log_fn or (lambda msg: print(msg))
        self.wallets: list[str] = _load_smart_wallets()
        # mint -> deque of (wallet, ts_ms) — pruned by window_sec
        self._buys: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._lock = asyncio.Lock()
        self._reconnect_delay = 2.0
        self._running = False
        self._stats = {
            "events_received": 0,
            "buys_recorded": 0,
            "last_event_ts_ms": 0,
            "reconnects": 0,
            "errors": 0,
        }

    def get_smart_buyers(self, mint: str, window_sec: Optional[float] = None) -> list[tuple[str, int]]:
        """Return list of (wallet, ts_ms) for smart wallets that bought this mint
        within `window_sec` (defaults to self.window_sec)."""
        win = window_sec if window_sec is not None else self.window_sec
        cutoff_ms = int(time.time() * 1000) - int(win * 1000)
        events = self._buys.get(mint)
        if not events:
            return []
        return [(w, t) for w, t in events if t >= cutoff_ms]

    def smart_buyer_count(self, mint: str, window_sec: Optional[float] = None) -> int:
        return len({w for w, _ in self.get_smart_buyers(mint, window_sec)})

    def stats(self) -> dict:
        return dict(self._stats, tracked_wallets=len(self.wallets), tracked_mints=len(self._buys))

    async def run(self) -> None:
        if websockets is None:
            self.log("PUMPPORTAL: websockets package missing — install with `pip install websockets`")
            return
        if not self.wallets:
            self.log("PUMPPORTAL: no smart wallets configured — exiting")
            return
        self._running = True
        self.log(f"PUMPPORTAL: starting WS client tracking {len(self.wallets)} smart wallets, window={self.window_sec:.0f}s")
        while self._running:
            try:
                async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    sub = {"method": "subscribeAccountTrade", "keys": self.wallets}
                    await ws.send(json.dumps(sub))
                    self.log(f"PUMPPORTAL: subscribed to {len(self.wallets)} wallets")
                    self._reconnect_delay = 2.0
                    async for raw in ws:
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                self._running = False
                self.log("PUMPPORTAL: cancelled")
                return
            except Exception as e:
                self._stats["errors"] += 1
                self._stats["reconnects"] += 1
                self.log(f"PUMPPORTAL: connection error {type(e).__name__}: {e} — reconnecting in {self._reconnect_delay:.1f}s")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 30.0)

    async def _handle_message(self, raw: str) -> None:
        self._stats["events_received"] += 1
        self._stats["last_event_ts_ms"] = int(time.time() * 1000)
        try:
            ev = json.loads(raw)
        except Exception:
            return
        # PumpPortal wraps trade events with various keys; we want the BUY events
        # for our subscribed wallets. Schema based on docs.pumpportal.fun:
        # {"signature":..., "mint":..., "traderPublicKey":..., "txType":"buy"|"sell",
        #  "tokenAmount":..., "solAmount":..., "marketCapSol":...}
        tx_type = (ev.get("txType") or "").lower()
        if tx_type != "buy":
            return
        mint = ev.get("mint")
        wallet = ev.get("traderPublicKey")
        if not mint or not wallet:
            return
        if wallet not in self.wallets:
            return
        ts_ms = int(time.time() * 1000)
        async with self._lock:
            dq = self._buys[mint]
            cutoff = ts_ms - int(self.window_sec * 1000)
            while dq and dq[0][1] < cutoff:
                dq.popleft()
            dq.append((wallet, ts_ms))
        self._stats["buys_recorded"] += 1
        self.log(f"PUMPPORTAL-SMART-BUY mint={mint[:8]} wallet={wallet[:8]} sol={ev.get('solAmount',0)} mc={ev.get('marketCapSol',0)}")

    async def prune(self) -> None:
        """Background pruner — drop mints whose deques are empty after window expires."""
        while self._running:
            await asyncio.sleep(self.window_sec)
            async with self._lock:
                cutoff = int(time.time() * 1000) - int(self.window_sec * 1000) * 2
                stale = []
                for mint, dq in self._buys.items():
                    while dq and dq[0][1] < cutoff:
                        dq.popleft()
                    if not dq:
                        stale.append(mint)
                for m in stale:
                    del self._buys[m]


def start_tracker(log_fn=None, window_sec: float = 30.0) -> SmartWalletTracker:
    """Convenience: instantiate the tracker (caller must schedule .run() and .prune() as tasks)."""
    return SmartWalletTracker(log_fn=log_fn, window_sec=window_sec)
