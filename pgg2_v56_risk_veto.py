"""V56 SolanaTracker risk veto.

Called AFTER v48 candidate decides, BEFORE v50b SWQOS send.
Token-2022 alone is NOT a blocker (routed to Pump v2 path instead).

Budget: 1 req/sec, 30s cache, 10 req per 5 min.

Veto when:
  rugged=true, holders<MIN, bundlers>=MAX, dev>MAX,
  snipers>MAX, insiders>MAX,
  dangers contain blacklisted names.

Log: PGG2-V56-RISK-VETO mint=.. holders=.. bundlers_pct=.. dev_pct=..
     snipers_pct=.. insiders_pct=.. dangers=.. is_token_2022=..
     pass=.. blocker=..
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_VETO_DANGERS_DEFAULT = (
    "High Bundler Holdings",
    "Rug Pull",
    "Mint authority active",
    "Freeze authority active",
    "Dev Holdings",
)


@dataclass
class RiskCheckResult:
    mint: str
    holders: int
    bundlers_pct: float
    dev_pct: float
    snipers_pct: float
    insiders_pct: float
    dangers: list[str]
    is_token_2022: bool
    pass_: bool
    blocker: Optional[str]
    api_status: str  # "ok", "budget_exhausted", "error:<reason>", "cached"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class V56RiskVeto:
    def __init__(self) -> None:
        # SolanaTracker has TWO keys: SOLANATRACKER_DATA_API_KEY (Data API)
        # and SOLANATRACKER_API_KEY (RPC). Prefer the Data-specific one.
        self.api_key = (
            os.environ.get("SOLANATRACKER_DATA_API_KEY")
            or os.environ.get("SOLANATRACKER_API_KEY")
            or ""
        )
        self.base_url = os.environ.get(
            "SOLANATRACKER_DATA_API", "https://data.solanatracker.io"
        )

        # Thresholds (per spec)
        self.min_holders = _envi("PGG2_V56_RISK_MIN_HOLDERS", 20)
        self.max_bundlers_pct = _envf("PGG2_V56_RISK_MAX_BUNDLERS_PCT", 15.0)
        self.max_dev_pct = _envf("PGG2_V56_RISK_MAX_DEV_PCT", 1.0)
        self.max_snipers_pct = _envf("PGG2_V56_RISK_MAX_SNIPERS_PCT", 10.0)
        self.max_insiders_pct = _envf("PGG2_V56_RISK_MAX_INSIDERS_PCT", 10.0)
        self.veto_dangers = set(
            os.environ.get(
                "PGG2_V56_RISK_VETO_DANGERS", ",".join(_VETO_DANGERS_DEFAULT)
            ).split(",")
        )

        # Budget
        self.req_budget_max = _envi("PGG2_V56_RISK_REQ_BUDGET_MAX", 10)
        self.req_budget_window_sec = _envf("PGG2_V56_RISK_REQ_BUDGET_WINDOW_SEC", 300.0)
        self.req_min_gap_sec = _envf("PGG2_V56_RISK_REQ_MIN_GAP_SEC", 1.0)
        self.cache_ttl_sec = _envf("PGG2_V56_RISK_CACHE_TTL_SEC", 30.0)

        # Fail-open by default: API errors do NOT block trades.
        # Operator can flip to fail-closed via env.
        self.fail_open_on_api_error = _envb("PGG2_V56_RISK_FAIL_OPEN_ON_API_ERROR", True)

        # State
        self._cache: dict[str, tuple[float, RiskCheckResult]] = {}
        self._req_times: list[float] = []
        self._last_req_ts: float = 0.0

    # ---- internals ----

    def _budget_ok(self) -> bool:
        now = time.time()
        cutoff = now - self.req_budget_window_sec
        self._req_times = [t for t in self._req_times if t > cutoff]
        return len(self._req_times) < self.req_budget_max

    def _enforce_min_gap(self) -> None:
        gap = time.time() - self._last_req_ts
        if gap < self.req_min_gap_sec:
            time.sleep(self.req_min_gap_sec - gap)

    def _fetch(self, mint: str) -> tuple[Optional[dict[str, Any]], str]:
        if not self._budget_ok():
            return None, "budget_exhausted"
        self._enforce_min_gap()
        url = f"{self.base_url.rstrip('/')}/tokens/{mint}"
        req = urllib.request.Request(url)
        if self.api_key:
            req.add_header("x-api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                data = json.loads(raw)
        except urllib.error.HTTPError as e:
            return None, f"http_{e.code}"
        except urllib.error.URLError as e:
            return None, f"url_{e.reason}"
        except Exception as e:  # noqa: BLE001
            return None, f"err_{type(e).__name__}"
        now = time.time()
        self._last_req_ts = now
        self._req_times.append(now)
        return data, "ok"

    # ---- public ----

    def check(self, mint: str) -> RiskCheckResult:
        # cache lookup
        now = time.time()
        c = self._cache.get(mint)
        if c is not None and now - c[0] < self.cache_ttl_sec:
            r = c[1]
            return RiskCheckResult(
                mint=r.mint, holders=r.holders, bundlers_pct=r.bundlers_pct,
                dev_pct=r.dev_pct, snipers_pct=r.snipers_pct,
                insiders_pct=r.insiders_pct, dangers=list(r.dangers),
                is_token_2022=r.is_token_2022, pass_=r.pass_,
                blocker=r.blocker, api_status="cached",
            )

        data, status = self._fetch(mint)
        if data is None:
            # Budget exhausted or API error
            pass_ = self.fail_open_on_api_error
            blocker = None if pass_ else f"api_{status}"
            result = RiskCheckResult(
                mint=mint, holders=0, bundlers_pct=0.0, dev_pct=0.0,
                snipers_pct=0.0, insiders_pct=0.0, dangers=[],
                is_token_2022=False, pass_=pass_,
                blocker=blocker, api_status=status,
            )
            # Do NOT cache API errors so we retry sooner
            return result

        # Parse response defensively
        risk = data.get("risk") or {}
        score = risk.get("score") or {}
        holders = int(data.get("holders") or risk.get("totalHolders") or 0)
        bundlers_pct = float(
            (risk.get("bundlers") or {}).get("totalPercentage") or 0.0
        )
        dev_pct = float((risk.get("dev") or {}).get("percentage") or 0.0)
        snipers_pct = float(
            (risk.get("snipers") or {}).get("totalPercentage") or 0.0
        )
        insiders_pct = float(
            (risk.get("insiders") or {}).get("totalPercentage") or 0.0
        )
        rugged = bool(data.get("rugged") or risk.get("rugged"))
        dangers_raw = risk.get("danger") or risk.get("risks") or []
        dangers = []
        for d in dangers_raw:
            if isinstance(d, dict):
                n = d.get("name") or d.get("title") or ""
            else:
                n = str(d)
            if n:
                dangers.append(n)

        token = data.get("token") or {}
        program_id = (
            token.get("programId")
            or token.get("program_id")
            or token.get("owner")
            or ""
        )
        is_t22 = program_id == TOKEN_2022_PROGRAM

        # Apply vetoes (in order, first match wins)
        blocker: Optional[str] = None
        if rugged:
            blocker = "rugged"
        elif holders > 0 and holders < self.min_holders:
            # holders=0 likely means "API didn't populate"; do not block on that alone
            blocker = f"holders={holders}<{self.min_holders}"
        elif bundlers_pct >= self.max_bundlers_pct:
            blocker = f"bundlers={bundlers_pct:.1f}>={self.max_bundlers_pct}"
        elif dev_pct > self.max_dev_pct:
            blocker = f"dev={dev_pct:.2f}>{self.max_dev_pct}"
        elif snipers_pct > self.max_snipers_pct:
            blocker = f"snipers={snipers_pct:.1f}>{self.max_snipers_pct}"
        elif insiders_pct > self.max_insiders_pct:
            blocker = f"insiders={insiders_pct:.1f}>{self.max_insiders_pct}"
        else:
            for d in dangers:
                if d in self.veto_dangers:
                    blocker = f"danger:{d}"
                    break

        result = RiskCheckResult(
            mint=mint, holders=holders, bundlers_pct=bundlers_pct,
            dev_pct=dev_pct, snipers_pct=snipers_pct,
            insiders_pct=insiders_pct, dangers=dangers,
            is_token_2022=is_t22, pass_=(blocker is None),
            blocker=blocker, api_status="ok",
        )
        self._cache[mint] = (now, result)
        return result

    def format_log_line(self, r: RiskCheckResult) -> str:
        short = r.mint[:4] + ".." + r.mint[-4:] if len(r.mint) > 10 else r.mint
        dlist = ";".join(r.dangers) if r.dangers else "-"
        return (
            f"PGG2-V56-RISK-VETO {short} "
            f"holders={r.holders} bundlers_pct={r.bundlers_pct:.1f} "
            f"dev_pct={r.dev_pct:.2f} snipers_pct={r.snipers_pct:.1f} "
            f"insiders_pct={r.insiders_pct:.1f} dangers={dlist} "
            f"is_token_2022={int(r.is_token_2022)} "
            f"pass={int(r.pass_)} blocker={r.blocker or '-'} "
            f"api={r.api_status}"
        )


_SINGLETON: Optional[V56RiskVeto] = None


def get_veto() -> V56RiskVeto:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V56RiskVeto()
    return _SINGLETON


def check_and_log(mint: str, log_fn=print) -> RiskCheckResult:
    """Convenience: check + emit log line. Returns RiskCheckResult."""
    veto = get_veto()
    r = veto.check(mint)
    log_fn(veto.format_log_line(r))
    return r


if __name__ == "__main__":
    # CLI smoke test: python -m pgg2_v56_risk_veto <mint>
    import sys
    if len(sys.argv) < 2:
        print("usage: python pgg2_v56_risk_veto.py <mint>")
        sys.exit(2)
    r = check_and_log(sys.argv[1])
    sys.exit(0 if r.pass_ else 1)
