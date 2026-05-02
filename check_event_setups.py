"""
Quick recon: what's currently tradeable on Binance USD-M for the event-driven setups
identified in report1/report2/report3 (GUN unlock, OMNI unlock, SUI unlock, BABY unlock, BTC FOMC).
Also include ORCA, SIGN for completeness.
"""
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError

c = BinanceFuturesClient()  # public endpoints, no key needed

CANDIDATES = [
    "BTCUSDT",     # FOMC today 18:00 UTC
    "ETHUSDT",     # FOMC alt
    "GUNUSDT",     # unlock Apr 30 ~13:00 UTC (tomorrow)
    "SUIUSDT",     # unlock May 1
    "OMNIUSDT",    # unlock May 2 (largest %, 23.25%)
    "BABYUSDT",    # unlock May 10
    "SIGNUSDT",    # unlock Apr 28 (already passed; check if tradeable for follow-through)
    "ORCAUSDT",    # squeeze candidate yesterday
]

print(f"=== Binance USD-M event-setup recon ===\n")
print(f"{'symbol':<14}{'status':<10}{'mark':>12}{'24h%':>10}{'fund_8h%':>12}{'spread%':>10}")
print("-" * 75)

for sym in CANDIDATES:
    try:
        info = c.get_symbol_info(sym)
        status = info.get("contractStatus") or info.get("status") or "?"
    except BinanceApiError:
        print(f"{sym:<14}NOT_LISTED")
        continue
    try:
        pi = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
        mark = float(pi["markPrice"])
        funding = float(pi.get("lastFundingRate", "0")) * 100
        # Assume 8h interval. (For SIGN/some volatile alts it's 1h or 4h, normalize would matter.)
        fund_norm_8h = funding  # default 8h interval; if 1h it'd be 8x this
    except BinanceApiError:
        mark = funding = fund_norm_8h = 0
    try:
        t = c.public_get("/fapi/v1/ticker/24hr", {"symbol": sym})
        chg = float(t["priceChangePercent"])
    except BinanceApiError:
        chg = 0
    try:
        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        spread = (ask - bid) / ((bid + ask) / 2) * 100 if bid + ask > 0 else 99
    except BinanceApiError:
        spread = 99

    print(f"{sym:<14}{status:<10}{mark:>12.4f}{chg:>+10.2f}{fund_norm_8h:>+12.4f}{spread:>+10.4f}")
