"""V42C — PumpPortal FREE-ONLY optional adapter.

Strictly free endpoints per the user's memory note
(`pumpportal_wss_integration_may2026.md`):
  - subscribeNewToken
  - subscribeMigration

Will NEVER subscribe to paid streams (subscribeTokenTrade,
subscribeAccountTrade, etc.).

Activation requires:
  PGG2_PUMPPORTAL_ENABLED=1
  PGG2_PUMPPORTAL_API_KEY=<key>   (NOT used to authenticate the free streams,
                                   but its presence is taken as evidence
                                   that the operator has an account, in
                                   case the host wants to track usage)

If either is missing, this module emits:
  PGG2-PUMPPORTAL-FREE-SOURCE-STATUS status=unavailable_no_credentials

When enabled, the module connects to wss://pumpportal.fun/api/data and
subscribes to the two free streams. It surfaces a callback per event:

  cb(event_kind, payload_dict)

where event_kind in {"new_token", "migration"}.

NO TX. NO SENDS. Read-only.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Optional


PUMPPORTAL_WSS = "wss://pumpportal.fun/api/data"


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y", "t")


class PumpPortalFreeAdapter:
    def __init__(
        self,
        logger: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self._logger = logger or (lambda *a, **kw: None)
        self._on_event = on_event
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    def is_enabled(self) -> bool:
        return _env_bool("PGG2_PUMPPORTAL_ENABLED", False) and bool(
            os.environ.get("PGG2_PUMPPORTAL_API_KEY", "").strip()
        )

    def emit_startup_status(self) -> None:
        try:
            if self.is_enabled():
                self._logger("PGG2-PUMPPORTAL-FREE-SOURCE-STATUS status=live_free")
            else:
                self._logger("PGG2-PUMPPORTAL-FREE-SOURCE-STATUS status=unavailable_no_credentials")
        except Exception:
            pass

    async def start(self) -> None:
        if not self.is_enabled():
            self.emit_startup_status()
            return
        self.emit_startup_status()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _run_loop(self) -> None:
        try:
            import websockets
        except Exception as exc:
            try:
                self._logger(f"PGG2-PUMPPORTAL-WS-IMPORT-ERR {type(exc).__name__}:{exc}")
            except Exception:
                pass
            return
        backoff = 2.0
        while not self._stopped:
            try:
                async with websockets.connect(
                    PUMPPORTAL_WSS,
                    ping_interval=20,
                    ping_timeout=60,
                    max_queue=2048,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    backoff = 2.0
                    # ONLY the two free streams. NEVER trade streams.
                    free_subs = [
                        {"method": "subscribeNewToken"},
                        {"method": "subscribeMigration"},
                    ]
                    for sub in free_subs:
                        await ws.send(json.dumps(sub))
                    try:
                        self._logger("PGG2-PUMPPORTAL-FREE-SOURCE-STATUS status=subscribed_free_only streams=newToken,migration")
                    except Exception:
                        pass
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        kind = self._classify(msg)
                        if not kind:
                            continue
                        if self._on_event is None:
                            continue
                        try:
                            await self._on_event(kind, msg)
                        except Exception:
                            continue
            except asyncio.CancelledError:
                return
            except Exception as exc:
                try:
                    self._logger(
                        f"PGG2-PUMPPORTAL-FREE-SOURCE-STATUS status=reconnect exc={type(exc).__name__}:{exc}"
                    )
                except Exception:
                    pass
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2.0, 30.0)

    @staticmethod
    def _classify(msg: dict[str, Any]) -> str:
        # Per PumpPortal docs (see local RESEARCH_A_pumpportal_wss.md mirror).
        # Inspect known shape; fall back to txType / method hints.
        tt = str(msg.get("txType") or msg.get("type") or "").lower()
        method = str(msg.get("method") or "").lower()
        if tt in ("create", "createtoken") or method == "subscribenewtoken":
            return "new_token"
        if tt in ("migrate", "migration") or method == "subscribemigration":
            return "migration"
        return ""


__all__ = ["PumpPortalFreeAdapter"]
