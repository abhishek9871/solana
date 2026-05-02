"""
ORCA squeeze-continuation eligibility check per report1.md.
Pulls live Binance USD-M data and reports GO / NO-GO with reasons.
"""
from __future__ import annotations

import sys
import time
from decimal import Decimal

from trading_bot.binance_client import BinanceFuturesClient


SYMBOL = "ORCAUSDT"
ENTRY_TRIGGER = 1.660
ENTRY_MAX_CHASE = 1.682
HARD_STOP = 1.555
TARGET = 2.280
PRICE_FLOOR_15M = 1.500
FUND_THRESHOLD_PCT_8H = -0.15
ABORT_FUND_PCT_8H = -0.05
SPREAD_MAX_PCT = 0.12
SLIPPAGE_MAX_PCT = 0.35
MIN_24H_CHG_PCT = 15.0
MIN_OUTPERF_BTC_PCT = 15.0
MAX_BTC_15M_DROP_PCT = 1.5
MAX_BTC_2_PCT_15M_DROP = 2.0
MIN_24H_FUT_VOL_USD = 250_000_000


def pct(x: float) -> str:
    return f"{x:+.3f}%"


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def main() -> int:
    c = BinanceFuturesClient()

    print(f"=== {SYMBOL} squeeze eligibility check ===")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()

    # 1. Symbol exists?
    try:
        info = c.get_symbol_info(SYMBOL)
    except Exception as exc:
        print(f"FATAL: {SYMBOL} not on Binance USD-M: {exc}")
        return 2
    tick_size = "?"
    step_size = "?"
    min_notional = Decimal("0")
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick_size = f["tickSize"]
        elif f["filterType"] == "LOT_SIZE":
            step_size = f["stepSize"]
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            min_notional = Decimal(f.get("notional") or f.get("minNotional", "0"))
    contract_status = info.get("contractStatus") or info.get("status")
    print(f"Symbol: status={contract_status}  tick={tick_size}  step={step_size}  minNotional={min_notional}")
    if contract_status not in ("TRADING",):
        print(f"FATAL: contract status {contract_status} -- not tradeable")
        return 2

    # 2. Premium index (mark + funding)
    pi = c.public_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
    mark = float(pi["markPrice"])
    funding_rate_decimal = float(pi.get("lastFundingRate", "0"))
    next_funding_time = int(pi.get("nextFundingTime", 0))
    funding_pct_8h = funding_rate_decimal * 100  # already per-interval; assume 8h on Binance USD-M

    # Verify funding interval (some symbols are 4h)
    fund_info = c.public_get("/fapi/v1/fundingInfo", {})
    interval_hours = 8
    for entry in fund_info if isinstance(fund_info, list) else []:
        if entry.get("symbol") == SYMBOL:
            ah = entry.get("adjustedFundingRateCap")
            ih = entry.get("fundingIntervalHours")
            if ih:
                interval_hours = int(ih)
            break
    funding_pct_normalized_8h = funding_pct_8h * (8.0 / interval_hours)

    print(f"Mark price: ${mark:.4f}  funding={pct(funding_pct_8h)}/{interval_hours}h  "
          f"normalized={pct(funding_pct_normalized_8h)}/8h")

    # 3. 24h ticker (change, volume, high, low)
    t24 = c.public_get("/fapi/v1/ticker/24hr", {"symbol": SYMBOL})
    chg_24h_pct = float(t24["priceChangePercent"])
    vol_24h_usd = float(t24["quoteVolume"])
    high_24h = float(t24["highPrice"])
    low_24h = float(t24["lowPrice"])
    print(f"24h: change={pct(chg_24h_pct)}  quoteVol={fmt_money(vol_24h_usd)}  "
          f"H={high_24h:.4f}  L={low_24h:.4f}")

    # 4. BTC 24h + last 15m
    btc24 = c.public_get("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"})
    btc_chg_24h_pct = float(btc24["priceChangePercent"])
    btc_klines_15m = c.get_klines("BTCUSDT", "15m", limit=2)
    last_btc_15m = btc_klines_15m[-1]
    btc_15m_open = float(last_btc_15m[1])
    btc_15m_close = float(last_btc_15m[4])
    btc_15m_pct = (btc_15m_close / btc_15m_open - 1.0) * 100.0
    outperf_pct = chg_24h_pct - btc_chg_24h_pct
    print(f"BTC 24h: {pct(btc_chg_24h_pct)}  last 15m: {pct(btc_15m_pct)}  "
          f"ORCA outperf: {pct(outperf_pct)}")

    # 5. Open interest
    oi = c.public_get("/fapi/v1/openInterest", {"symbol": SYMBOL})
    oi_qty = float(oi["openInterest"])
    oi_usd = oi_qty * mark
    print(f"Open interest: {oi_qty:,.0f} ORCA = {fmt_money(oi_usd)}")

    # 6. Book ticker / spread
    bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
    bid = float(bt["bidPrice"])
    ask = float(bt["askPrice"])
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 999
    print(f"Bid: {bid:.4f}  Ask: {ask:.4f}  Mid: {mid:.4f}  Spread: {pct(spread_pct)}")

    # 7. 5m klines for EMA20 + last 5m close
    k5 = c.get_klines(SYMBOL, "5m", limit=30)
    closes_5m = [float(k[4]) for k in k5[:-1]]  # all CLOSED candles
    last_5m_close = closes_5m[-1] if closes_5m else mark
    ema20_5m = ema(closes_5m[-20:] if len(closes_5m) >= 20 else closes_5m, 20)
    print(f"5m last closed: {last_5m_close:.4f}  EMA20: {ema20_5m:.4f}  "
          f"above EMA20: {last_5m_close > ema20_5m}")

    # 8. 15m closed
    k15 = c.get_klines(SYMBOL, "15m", limit=2)
    last_15m_close = float(k15[-2][4]) if len(k15) >= 2 else float(k15[-1][4])
    print(f"15m last closed: {last_15m_close:.4f}  (floor: {PRICE_FLOOR_15M})")

    # ---------- ELIGIBILITY ----------
    print()
    print("=== ELIGIBILITY FILTERS ===")
    checks: list[tuple[str, bool, str]] = []

    checks.append((
        f"Funding <= {FUND_THRESHOLD_PCT_8H}%/8h",
        funding_pct_normalized_8h <= FUND_THRESHOLD_PCT_8H,
        f"actual {pct(funding_pct_normalized_8h)}/8h",
    ))
    checks.append((
        f"24h change > {MIN_24H_CHG_PCT}%",
        chg_24h_pct > MIN_24H_CHG_PCT,
        f"actual {pct(chg_24h_pct)}",
    ))
    checks.append((
        f"Outperforms BTC by >= {MIN_OUTPERF_BTC_PCT}% over 24h",
        outperf_pct >= MIN_OUTPERF_BTC_PCT,
        f"actual {pct(outperf_pct)}",
    ))
    checks.append((
        f"Spread <= {SPREAD_MAX_PCT}%",
        spread_pct <= SPREAD_MAX_PCT,
        f"actual {pct(spread_pct)}",
    ))
    checks.append((
        "5m last closed > 5m EMA20",
        last_5m_close > ema20_5m,
        f"close {last_5m_close:.4f} vs EMA20 {ema20_5m:.4f}",
    ))
    checks.append((
        f"15m last closed >= ${PRICE_FLOOR_15M}",
        last_15m_close >= PRICE_FLOOR_15M,
        f"actual {last_15m_close:.4f}",
    ))
    checks.append((
        f"BTC 15m drop <= {MAX_BTC_15M_DROP_PCT}%",
        btc_15m_pct >= -MAX_BTC_15M_DROP_PCT,
        f"actual {pct(btc_15m_pct)}",
    ))
    checks.append((
        f"24h futures vol >= {fmt_money(MIN_24H_FUT_VOL_USD)}",
        vol_24h_usd >= MIN_24H_FUT_VOL_USD,
        f"actual {fmt_money(vol_24h_usd)}",
    ))
    checks.append((
        f"Mark within entry zone (<= ${ENTRY_MAX_CHASE})",
        mark <= ENTRY_MAX_CHASE,
        f"mark {mark:.4f}",
    ))
    checks.append((
        f"Mark above hard stop (> ${HARD_STOP})",
        mark > HARD_STOP,
        f"mark {mark:.4f}",
    ))

    all_pass = True
    for name, ok, detail in checks:
        sym = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{sym}] {name}  --  {detail}")

    print()
    # ---- ABORT signals ----
    print("=== ABORT-IF-PRESENT ===")
    aborts: list[tuple[str, bool, str]] = []
    aborts.append((
        f"Funding has normalized to > {ABORT_FUND_PCT_8H}%/8h (squeeze gone)",
        funding_pct_normalized_8h > ABORT_FUND_PCT_8H,
        f"actual {pct(funding_pct_normalized_8h)}/8h",
    ))
    aborts.append((
        f"Mark already above $1.75 (chasing)",
        mark > 1.75,
        f"mark {mark:.4f}",
    ))
    aborts.append((
        f"BTC dropping > {MAX_BTC_2_PCT_15M_DROP}% in 15m",
        btc_15m_pct < -MAX_BTC_2_PCT_15M_DROP,
        f"actual {pct(btc_15m_pct)}",
    ))
    any_abort = False
    for name, hit, detail in aborts:
        sym = "ABORT" if hit else "ok"
        if hit:
            any_abort = True
        print(f"  [{sym}] {name}  --  {detail}")

    print()
    print("=== VERDICT ===")
    if all_pass and not any_abort:
        print("GO -- all eligibility filters pass, no abort signals.")
        print(f"  Entry trigger: 5m close > ${ENTRY_TRIGGER}, max chase ${ENTRY_MAX_CHASE}")
        print(f"  Hard stop: ${HARD_STOP}  Target: ${TARGET}")
        return 0
    else:
        if not all_pass:
            failing = [n for n, ok, _ in checks if not ok]
            print("NO-GO -- failing filters: " + ", ".join(failing))
        if any_abort:
            hit = [n for n, h, _ in aborts if h]
            print("ABORT signals present: " + ", ".join(hit))
        return 1


if __name__ == "__main__":
    sys.exit(main())
