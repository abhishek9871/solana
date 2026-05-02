"""Check production account state and current ORCA-class candidates."""
import os
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

print("=== PROD ACCOUNT STATE ===\n")
try:
    bals = c.get_balance()
    usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
    if usdt:
        print(f"USDT Wallet:    ${float(usdt.get('balance', 0)):.4f}")
        print(f"USDT Available: ${float(usdt.get('availableBalance', 0)):.4f}")
        print(f"USDT crossUnPnl:${float(usdt.get('crossUnPnl', 0)):.4f}")
except BinanceApiError as e:
    print(f"Balance fetch error: {e}")

# Open positions
try:
    positions = c.get_positions()
    open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
    print(f"\nOpen positions: {len(open_pos)}")
    for p in open_pos:
        print(f"  {p['symbol']}: amt={p['positionAmt']}, entry={p['entryPrice']}, unPnL={p['unRealizedProfit']}")
except BinanceApiError as e:
    print(f"Positions fetch error: {e}")

# Open orders
try:
    orders = c.get_open_orders()
    print(f"\nOpen orders: {len(orders)}")
except BinanceApiError as e:
    print(f"Orders fetch: {e}")

print("\n=== PROD SCANNER (top 10 ORCA-class candidates) ===\n")
tickers = c.get_24hr_tickers()
premiums = c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in premiums} if isinstance(premiums, list) else {}

candidates = []
for t in tickers:
    sym = t.get("symbol", "")
    if not sym.endswith("USDT"):
        continue
    try:
        vol = float(t["quoteVolume"])
        if vol < 50_000_000:
            continue
        chg_24h = float(t["priceChangePercent"])
        p = prem_map.get(sym)
        if not p:
            continue
        funding_raw = float(p.get("lastFundingRate", "0")) * 100
        funding_8h = funding_raw
        if abs(funding_raw) > 0.3:
            funding_8h = funding_raw * 8
        try:
            k1 = c.get_klines(sym, "1m", limit=2)
            if len(k1) < 2:
                continue
            prev_c = float(k1[-2][4])
            curr_c = float(k1[-1][4])
            if prev_c <= 0:
                continue
            vel_1m = (curr_c / prev_c - 1) * 100
        except Exception:
            continue
        try:
            bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid * 100 if mid > 0 else 99
        except Exception:
            spread = 99
        if spread > 0.10:
            continue
        if funding_8h < 0 and chg_24h > 0:
            direction = "BUY"
            alignment = chg_24h
        elif funding_8h > 0 and chg_24h < 0:
            direction = "SELL"
            alignment = -chg_24h
        else:
            continue
        if direction == "BUY" and vel_1m < 0:
            continue
        if direction == "SELL" and vel_1m > 0:
            continue
        score = abs(funding_8h) * 2.0 + alignment * 0.3 + abs(vel_1m) * 1.0
        candidates.append({"sym": sym, "score": score, "fund": funding_8h, "chg": chg_24h,
                          "vel": vel_1m, "spread": spread, "vol": vol, "dir": direction, "mark": mid})
    except Exception:
        continue

candidates.sort(key=lambda x: x["score"], reverse=True)
print(f"{'rank':<5}{'symbol':<14}{'dir':<5}{'fund%':>10}{'24h%':>10}{'1m%':>8}{'spread%':>10}{'volM$':>10}{'score':>8}")
print("-" * 95)
for i, cand in enumerate(candidates[:10]):
    line = (f"{i+1:<5}{cand['sym']:<14}{cand['dir']:<5}{cand['fund']:>+10.3f}"
            f"{cand['chg']:>+10.2f}{cand['vel']:>+8.2f}{cand['spread']:>+10.4f}"
            f"{cand['vol']/1e6:>9.1f}M{cand['score']:>+8.2f}")
    print(line.encode("ascii", "replace").decode("ascii"))

if candidates:
    top = candidates[0]
    print(f"\n=== TOP PROD CANDIDATE: {top['sym']} ===")
    print(f"  Direction: {top['dir']}")
    print(f"  Funding:   {top['fund']:+.3f}%/8h")
    print(f"  24h:       {top['chg']:+.2f}%")
    print(f"  1m:        {top['vel']:+.2f}%")
    print(f"  Mark:      ${top['mark']:.6f}")
    print(f"  Volume:    ${top['vol']/1e6:.1f}M")
