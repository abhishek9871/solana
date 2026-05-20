"""V51 - Helius holder-quality checker (pre-entry veto support).

Phase 2 of V51. Queries Helius standard JSON-RPC for getTokenLargestAccounts,
getTokenSupply, and getAccountInfo, computes holder-concentration features
after subtracting structural BC/PumpSwap PDAs, and returns a feature dict
the V51 holder-veto evaluator consumes.

Constraints:
  - Helius API key read from env (NEVER logged or echoed).
  - 30s TTL cache per mint.
  - 60-call/min rate limit (configurable).
  - Excluded structural holders: pump.fun bonding-curve PDA + its ATA,
    PumpSwap pool PDA + its base/quote vaults (if migrated).
  - Returns features dict with all required fields and `ok` flag.
  - Static-grep clean: no sendTransaction patterns.

The token-2022 program id is also returned via getAccountInfo on the mint
itself (`owner` field) -- the gate file evaluates it separately.
"""
from __future__ import annotations

import asyncio
import os
import re as _re_self
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp

# Static-grep self check -- forbidden send patterns must NOT appear.
_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _src):
        sys.stderr.write(
            f"V51-HOLDER-QUALITY-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


# Known program / system constants. We bytes-derive the bonding-curve and
# associated-curve PDAs locally so this module never depends on heavy
# Solana SDK paths -- but we do import solders lazily for the derivation.

# Pump.fun program id.
PUMP_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# PumpSwap (after migration) program id.
PUMPSWAP_PROGRAM_ID_STR = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# SPL Token classic program id (mint owners for pump.fun classic tokens).
SPL_TOKEN_PROGRAM_ID_STR = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Token-2022 program id.
TOKEN_2022_PROGRAM_ID_STR = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Associated Token Program.
ASSOCIATED_TOKEN_PROGRAM_ID_STR = (
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _derive_bc_pda(mint: str) -> Optional[str]:
    """Derive the pump.fun bonding-curve PDA address for a mint."""
    try:
        from solders.pubkey import Pubkey  # local import
        program_id = Pubkey.from_string(PUMP_PROGRAM_ID_STR)
        mint_pk = Pubkey.from_string(mint)
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)], program_id
        )
        return str(pda)
    except Exception:
        return None


def _derive_associated_token_address(
    owner: str, mint: str, token_program: str = SPL_TOKEN_PROGRAM_ID_STR,
) -> Optional[str]:
    """Derive the SPL ATA for owner+mint under the given token program."""
    try:
        from solders.pubkey import Pubkey  # local import
        ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID_STR)
        owner_pk = Pubkey.from_string(owner)
        mint_pk = Pubkey.from_string(mint)
        tp_pk = Pubkey.from_string(token_program)
        ata, _bump = Pubkey.find_program_address(
            [bytes(owner_pk), bytes(tp_pk), bytes(mint_pk)],
            ata_program,
        )
        return str(ata)
    except Exception:
        return None


def _derive_pumpswap_pool_pda(mint: str, base_or_quote: str = "base") -> Optional[str]:
    """Derive a candidate PumpSwap pool authority PDA. We probe but do not
    assume migration; if the LP doesn't exist we silently skip exclusion.

    PumpSwap pool seeds vary across versions. We compute a best-effort
    candidate using ['pool', mint_pk] which is the common bonding-curve
    migration pool seed. If no on-chain account exists, no harm done.
    """
    try:
        from solders.pubkey import Pubkey  # local import
        program_id = Pubkey.from_string(PUMPSWAP_PROGRAM_ID_STR)
        mint_pk = Pubkey.from_string(mint)
        pda, _bump = Pubkey.find_program_address(
            [b"pool", bytes(mint_pk)], program_id
        )
        return str(pda)
    except Exception:
        return None


# --------------------------------------------------------------------------
# V51HolderQualityChecker
# --------------------------------------------------------------------------
class V51HolderQualityChecker:
    """Helius-RPC-backed holder concentration checker.

    Usage:
        ck = V51HolderQualityChecker(helius_api_key=KEY)
        feats = await ck.check_mint("MINT...")
        if feats["ok"]: ...
    """

    def __init__(
        self,
        helius_api_key: str,
        *,
        ttl_s: int = 30,
        rate_limit_per_min: int = 60,
        request_timeout_s: float = 4.0,
    ) -> None:
        self._helius_api_key = (helius_api_key or "").strip()
        self.ttl_s = int(ttl_s)
        self.rate_limit_per_min = int(rate_limit_per_min)
        self.request_timeout_s = float(request_timeout_s)
        # mint -> (ts_ms, features dict)
        self._cache: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        # rolling 60s call timestamps (ms)
        self._call_ts_ms: List[int] = []
        # Build URL ONCE — the key is held in memory only (never logged).
        # We do not include the key in any returned dict.
        if not self._helius_api_key:
            self._helius_url = ""
        else:
            self._helius_url = (
                f"https://mainnet.helius-rpc.com/?api-key={self._helius_api_key}"
            )

    # -- public API ---------------------------------------------------

    async def check_mint(self, mint: str) -> Dict[str, Any]:
        """Return a holder-features dict.

        Schema (all keys present even on error):
          ok: bool
          top1_pct, top3_pct, top5_pct, top10_pct: float (0..100)
          holder_count_nonzero: int
          largest_holder_amount_raw: int
          token_program: str (mint owner program id, '' on error)
          excluded_accounts: list[str]
          holder_check_age_ms: int (0 = fresh, >0 = cached)
          error: Optional[str]
          ts_ms: int (when this check executed)
        """
        if not self._helius_url:
            return self._error_features("helius_api_key_missing")
        # Cache lookup.
        now_ms = _now_ms()
        cached = self._cache.get(str(mint))
        if cached is not None:
            cached_ts, cached_feats = cached
            age_ms = now_ms - cached_ts
            if age_ms < self.ttl_s * 1000:
                feats = dict(cached_feats)
                feats["holder_check_age_ms"] = int(age_ms)
                return feats

        # Rate limit (rolling 60s window).
        self._call_ts_ms = [t for t in self._call_ts_ms if t > now_ms - 60_000]
        if len(self._call_ts_ms) >= self.rate_limit_per_min:
            return self._error_features("rate_limit_exceeded")
        self._call_ts_ms.append(now_ms)

        t0 = time.time()
        try:
            feats = await self._fetch_and_compute(mint)
        except Exception as exc:
            err = f"{type(exc).__name__}:{exc}"
            _log(
                f"PGG2-V51-HOLDER-CHECK-ERROR mint={_short(mint)} err={err}"
            )
            feats = self._error_features(err)
        ms_taken = (time.time() - t0) * 1000.0

        # Cache only successful checks (we want to retry on transient errors).
        if feats.get("ok"):
            self._cache[str(mint)] = (now_ms, dict(feats))
        feats["holder_check_age_ms"] = 0  # fresh
        feats["ts_ms"] = now_ms

        _log(
            f"PGG2-V51-HOLDER-CHECK mint={_short(mint)} "
            f"ms_taken={ms_taken:.1f} "
            f"top1={feats.get('top1_pct', 0.0):.2f} "
            f"top3={feats.get('top3_pct', 0.0):.2f} "
            f"top5={feats.get('top5_pct', 0.0):.2f} "
            f"top10={feats.get('top10_pct', 0.0):.2f} "
            f"count={feats.get('holder_count_nonzero', 0)} "
            f"token_program={feats.get('token_program', '')} "
            f"ok={feats.get('ok')} cached=false "
            f"excluded={len(feats.get('excluded_accounts') or [])}"
        )
        return feats

    # -- internal -----------------------------------------------------

    def _error_features(self, err: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": err,
            "top1_pct": 0.0,
            "top3_pct": 0.0,
            "top5_pct": 0.0,
            "top10_pct": 0.0,
            "holder_count_nonzero": 0,
            "largest_holder_amount_raw": 0,
            "token_program": "",
            "excluded_accounts": [],
            "holder_check_age_ms": 0,
            "ts_ms": _now_ms(),
        }

    async def _fetch_and_compute(self, mint: str) -> Dict[str, Any]:
        """POST 3 RPC requests in parallel, then compute features."""
        url = self._helius_url
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            largest_task = self._rpc_post(
                s, url, "getTokenLargestAccounts",
                [mint, {"commitment": "processed"}],
            )
            supply_task = self._rpc_post(
                s, url, "getTokenSupply",
                [mint, {"commitment": "processed"}],
            )
            account_info_task = self._rpc_post(
                s, url, "getAccountInfo",
                [mint, {"encoding": "base64", "commitment": "processed"}],
            )
            largest, supply, account_info = await asyncio.gather(
                largest_task, supply_task, account_info_task,
                return_exceptions=True,
            )

        if isinstance(largest, Exception):
            return self._error_features(
                f"largest_accounts_exc:{type(largest).__name__}"
            )
        if isinstance(supply, Exception):
            return self._error_features(
                f"supply_exc:{type(supply).__name__}"
            )
        if isinstance(account_info, Exception):
            return self._error_features(
                f"account_info_exc:{type(account_info).__name__}"
            )

        # Parse supply.
        total_supply_raw = 0
        try:
            supply_val = ((supply or {}).get("result") or {}).get("value") or {}
            total_supply_raw = int(supply_val.get("amount") or 0)
        except Exception:
            return self._error_features("supply_parse_fail")
        if total_supply_raw <= 0:
            return self._error_features("supply_zero_or_missing")

        # Parse mint owner (token program).
        token_program = ""
        try:
            value = ((account_info or {}).get("result") or {}).get("value") or {}
            token_program = str(value.get("owner") or "")
        except Exception:
            token_program = ""

        # Parse top-20 largest accounts.
        try:
            raw_list = (
                ((largest or {}).get("result") or {}).get("value") or []
            )
        except Exception:
            return self._error_features("largest_accounts_parse_fail")

        # Derive structural-holder exclusion set.
        excluded: Set[str] = set()
        bc_pda = _derive_bc_pda(mint)
        if bc_pda:
            excluded.add(bc_pda)
            # The bonding-curve ATA holds the tokens during the curve phase.
            # The token program for pump.fun is classic SPL Token by default,
            # but if mint owner is token-2022, use that for ATA derivation.
            tp_for_ata = (
                token_program if token_program in {
                    SPL_TOKEN_PROGRAM_ID_STR, TOKEN_2022_PROGRAM_ID_STR,
                } else SPL_TOKEN_PROGRAM_ID_STR
            )
            bc_ata = _derive_associated_token_address(bc_pda, mint, tp_for_ata)
            if bc_ata:
                excluded.add(bc_ata)
        # PumpSwap pool PDA + its ATAs (best-effort; only excludes if present).
        ps_pool = _derive_pumpswap_pool_pda(mint)
        if ps_pool:
            excluded.add(ps_pool)
            tp_for_ata = (
                token_program if token_program in {
                    SPL_TOKEN_PROGRAM_ID_STR, TOKEN_2022_PROGRAM_ID_STR,
                } else SPL_TOKEN_PROGRAM_ID_STR
            )
            ps_ata = _derive_associated_token_address(ps_pool, mint, tp_for_ata)
            if ps_ata:
                excluded.add(ps_ata)

        # Build the cleaned holder list.
        cleaned: List[Tuple[str, int]] = []
        for row in raw_list:
            try:
                addr = str((row or {}).get("address") or "")
                amount_raw = int((row or {}).get("amount") or 0)
            except Exception:
                continue
            if not addr or amount_raw <= 0:
                continue
            if addr in excluded:
                continue
            cleaned.append((addr, amount_raw))

        # Sort descending by amount, just in case.
        cleaned.sort(key=lambda r: r[1], reverse=True)

        # Compute percentages (all denominated against total_supply, which
        # is mint supply BEFORE excluding structural holders -- this is the
        # honest "fraction of all tokens held by external dump-ready
        # wallets" metric).
        def _cum_pct(n: int) -> float:
            if n <= 0:
                return 0.0
            head = cleaned[:n]
            total = sum(a for _, a in head)
            return float(total) / float(total_supply_raw) * 100.0

        top1_pct = _cum_pct(1)
        top3_pct = _cum_pct(3)
        top5_pct = _cum_pct(5)
        top10_pct = _cum_pct(10)
        holder_count_nonzero = len(cleaned)
        largest_holder_amount_raw = int(cleaned[0][1]) if cleaned else 0

        return {
            "ok": True,
            "error": None,
            "top1_pct": float(top1_pct),
            "top3_pct": float(top3_pct),
            "top5_pct": float(top5_pct),
            "top10_pct": float(top10_pct),
            "holder_count_nonzero": int(holder_count_nonzero),
            "largest_holder_amount_raw": int(largest_holder_amount_raw),
            "token_program": token_program,
            "excluded_accounts": sorted(excluded),
            "holder_check_age_ms": 0,
            "total_supply_raw": int(total_supply_raw),
            "ts_ms": _now_ms(),
        }

    async def _rpc_post(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str,
        params: List[Any],
        *,
        req_id: int = 1,
    ) -> Dict[str, Any]:
        """Single JSON-RPC POST. Returns the full response dict."""
        body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        async with session.post(
            url,
            json=body,
            headers={"Content-Type": "application/json"},
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"http_{r.status}_{method}")
            data = await r.json()
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(
                    f"rpc_error_{method}:{data.get('error')}"
                )
            return data


# --------------------------------------------------------------------------
# Convenience: Token-2022 detection (synchronous, cached) used by the
# pre-entry Token-2022 veto. The heavy holder check returns token_program;
# this is for a fast path that ONLY needs token_program when holder data is
# unavailable.
# --------------------------------------------------------------------------
class V51Token2022Checker:
    """Cached owner-program lookup for a mint.

    Caches mint -> (ts_ms, owner_program_str). 30s TTL by default.
    """

    def __init__(
        self,
        helius_api_key: str,
        *,
        ttl_s: int = 30,
        request_timeout_s: float = 4.0,
    ) -> None:
        self._helius_api_key = (helius_api_key or "").strip()
        self.ttl_s = int(ttl_s)
        self.request_timeout_s = float(request_timeout_s)
        self._cache: Dict[str, Tuple[int, str]] = {}
        if self._helius_api_key:
            self._helius_url = (
                f"https://mainnet.helius-rpc.com/?api-key={self._helius_api_key}"
            )
        else:
            self._helius_url = ""

    async def owner_program(self, mint: str) -> str:
        if not self._helius_url:
            return ""
        now_ms = _now_ms()
        cached = self._cache.get(str(mint))
        if cached is not None:
            cts, owner = cached
            if now_ms - cts < self.ttl_s * 1000:
                _log(
                    f"PGG2-V51-TOKEN2022-CHECK mint={_short(mint)} "
                    f"owner_program={owner} "
                    f"is_token_2022={owner == TOKEN_2022_PROGRAM_ID_STR} "
                    f"cached=true"
                )
                return owner
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [
                        str(mint),
                        {
                            "encoding": "base64",
                            "commitment": "processed",
                        },
                    ],
                }
                async with s.post(
                    self._helius_url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as r:
                    if r.status != 200:
                        return ""
                    data = await r.json()
                    value = (
                        ((data or {}).get("result") or {}).get("value") or {}
                    )
                    owner = str(value.get("owner") or "")
        except Exception:
            return ""
        if owner:
            self._cache[str(mint)] = (now_ms, owner)
        _log(
            f"PGG2-V51-TOKEN2022-CHECK mint={_short(mint)} "
            f"owner_program={owner} "
            f"is_token_2022={owner == TOKEN_2022_PROGRAM_ID_STR} "
            f"cached=false"
        )
        return owner


def is_token_2022(owner_program: str) -> bool:
    return str(owner_program or "") == TOKEN_2022_PROGRAM_ID_STR


__all__ = [
    "V51HolderQualityChecker",
    "V51Token2022Checker",
    "is_token_2022",
    "TOKEN_2022_PROGRAM_ID_STR",
    "SPL_TOKEN_PROGRAM_ID_STR",
    "PUMP_PROGRAM_ID_STR",
    "PUMPSWAP_PROGRAM_ID_STR",
]
