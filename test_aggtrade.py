"""Test the NEW routed endpoints (post 2026-04-23 migration)."""
import asyncio
import json
import websockets

ENDPOINTS = [
    ("MARKET /market/ws/ aggTrade",
     "wss://fstream.binance.com/market/ws/btcusdt@aggTrade"),
    ("MARKET /market/stream/ aggTrade combined",
     "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade"),
    ("MARKET /market/ws/!forceOrder@arr",
     "wss://fstream.binance.com/market/ws/!forceOrder@arr"),
    ("PUBLIC /public/stream/ bookTicker (sanity check)",
     "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker"),
    ("MARKET /market/stream/ aggTrade + forceOrder combined",
     "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade/!forceOrder@arr"),
]

async def test(name, url, duration=10):
    bt = 0
    agg = 0
    liq = 0
    other = 0
    sample = None
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=2) as ws:
            end_time = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < end_time:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                stream = msg.get("stream", "")
                data = msg.get("data") if "data" in msg else msg
                e = data.get("e") if isinstance(data, dict) else None
                if "@bookTicker" in stream or e == "bookTicker":
                    bt += 1
                elif "@aggTrade" in stream or e == "aggTrade":
                    agg += 1
                    if sample is None:
                        sample = data
                elif "forceOrder" in stream or e == "forceOrder":
                    liq += 1
                    if sample is None:
                        sample = data
                else:
                    other += 1
        status = "OK" if (agg + liq + bt) > 0 else "DEAD"
        print(f"[{status:4s}] {name}")
        print(f"       agg={agg}, liq={liq}, bt={bt}, other={other}")
        if sample:
            print(f"       sample: {json.dumps(sample)[:160]}")
    except Exception as exc:
        print(f"[FAIL] {name}: {str(exc)[:100]}")

async def main():
    for name, url in ENDPOINTS:
        await test(name, url)

if __name__ == "__main__":
    asyncio.run(main())
