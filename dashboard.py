"""Real-time trading dashboard, browser-based.

Run: py dashboard.py

Auto-opens http://localhost:8765 in your default browser.
Refreshes data every 3 seconds.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.live_executor import LiveExecutor, load_credentials_from_env

PORT = 8765
SESSION_START_FILE = PROJECT_ROOT / "data" / "session_start.json"
SESSION_DEFAULT_START = 52.08  # India morning start of session
PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def load_session_start(current_equity: float) -> tuple[float, str]:
    today = datetime.now(timezone.utc).date().isoformat()
    if SESSION_START_FILE.exists():
        try:
            data = json.loads(SESSION_START_FILE.read_text())
            if data.get("date") == today:
                return float(data["start_balance"]), data.get("source", "saved")
        except Exception:
            pass
    SESSION_START_FILE.parent.mkdir(parents=True, exist_ok=True)
    start = SESSION_DEFAULT_START if current_equity > SESSION_DEFAULT_START else current_equity
    SESSION_START_FILE.write_text(json.dumps({"date": today, "start_balance": start, "source": "auto"}))
    return start, "auto"


def get_mark(symbol: str) -> tuple[float, float] | None:
    try:
        r = requests.get(PREMIUM_INDEX_URL, params={"symbol": symbol}, timeout=5).json()
        return float(r.get("markPrice", 0)), float(r.get("lastFundingRate", 0)) * 100
    except Exception:
        return None


def time_to_next_funding() -> tuple[str, float, str]:
    now = datetime.now(timezone.utc)
    h = now.hour
    next_h = ((h // 8) + 1) * 8
    if next_h >= 24:
        next_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_dt = now.replace(hour=next_h, minute=0, second=0, microsecond=0)
    delta = (next_dt - now).total_seconds()
    mins = int(delta // 60)
    secs = int(delta % 60)
    return f"{mins // 60:02d}:{mins % 60:02d}:{secs:02d}", delta / 3600, next_dt.strftime("%H:%M UTC")


_executor_lock = threading.Lock()
_executor: LiveExecutor | None = None


def get_executor() -> LiveExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            api_key, secret = load_credentials_from_env()
            if not api_key or not secret:
                raise RuntimeError("No credentials in .env")
            _executor = LiveExecutor(api_key=api_key, secret_key=secret)
        return _executor


def snapshot() -> dict:
    ex = get_executor()
    try:
        balance = ex.get_usdt_balance()
        positions = ex.get_open_positions()
    except Exception as exc:
        return {"error": str(exc)}

    pos_data = []
    total_unrealized = 0.0
    total_margin = 0.0
    for p in positions:
        mark_data = get_mark(p.symbol)
        if mark_data is None:
            continue
        mark, fund = mark_data
        chg = (mark - p.entry_price) / p.entry_price * 100
        sl = p.entry_price * 0.95
        tp = p.entry_price * 1.15
        sl_dist = (mark - sl) / mark * 100
        tp_dist = (tp - mark) / mark * 100
        funding_payment = -fund / 100 * p.quantity * mark
        try:
            algo_orders = ex.client.get_open_algo_orders(p.symbol)
            algo_count = len(algo_orders) if isinstance(algo_orders, list) else 0
        except Exception:
            algo_count = 0
        pos_data.append({
            "symbol": p.symbol,
            "side": p.side,
            "qty": p.quantity,
            "entry": p.entry_price,
            "mark": mark,
            "chg_pct": chg,
            "unrealized": p.unrealized_pnl,
            "sl": sl,
            "tp": tp,
            "sl_dist_pct": sl_dist,
            "tp_dist_pct": tp_dist,
            "funding_rate": fund,
            "funding_payment_estimate": funding_payment,
            "algo_orders_count": algo_count,
        })
        total_unrealized += p.unrealized_pnl
        total_margin += p.margin

    equity = balance + total_margin + total_unrealized
    session_start, _ = load_session_start(equity)
    day_pnl = equity - session_start
    day_pct = (day_pnl / session_start * 100) if session_start > 0 else 0.0
    countdown, hours, next_at = time_to_next_funding()

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "balance": balance,
        "margin": total_margin,
        "unrealized": total_unrealized,
        "equity": equity,
        "session_start": session_start,
        "day_pnl": day_pnl,
        "day_pct": day_pct,
        "positions": pos_data,
        "next_funding": {"countdown": countdown, "hours": hours, "at": next_at},
    }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Trading Bot Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: #0d1117;
    color: #e6edf3;
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    font-size: 14px;
  }
  .container { max-width: 980px; margin: 0 auto; padding: 24px; }
  .header {
    display: flex; justify-content: space-between; align-items: baseline;
    border-bottom: 1px solid #30363d; padding-bottom: 12px; margin-bottom: 20px;
  }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  .header .ts { color: #6e7681; font-size: 12px; }
  .pnl-banner {
    border-radius: 8px; padding: 18px; margin-bottom: 20px; text-align: center;
    transition: background 0.4s ease;
  }
  .pnl-banner.green { background: linear-gradient(90deg, #1f6f3f, #238636); }
  .pnl-banner.red   { background: linear-gradient(90deg, #71242b, #b62a37); }
  .pnl-banner.flat  { background: #21262d; }
  .pnl-banner .big { font-size: 32px; font-weight: 700; letter-spacing: 0.5px; }
  .pnl-banner .small { font-size: 13px; opacity: 0.85; margin-top: 4px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card h2 { margin: 0 0 12px 0; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e; }
  .row { display: flex; justify-content: space-between; padding: 4px 0; }
  .row .label { color: #8b949e; }
  .row .val { color: #e6edf3; font-variant-numeric: tabular-nums; }
  .pos { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .pos h3 { margin: 0 0 8px 0; font-size: 16px; }
  .pos .meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 12px; }
  .pos .meta .label { color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .pos .meta .val { font-size: 16px; font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar-track {
    position: relative; height: 28px; background: #21262d; border-radius: 14px;
    overflow: hidden; margin: 16px 0 8px;
  }
  .bar-zone-sl { position: absolute; left: 0; top: 0; bottom: 0; background: #4d2228; }
  .bar-zone-tp { position: absolute; right: 0; top: 0; bottom: 0; background: #1f4d2a; }
  .bar-marker {
    position: absolute; top: 0; bottom: 0; width: 3px; background: #58a6ff;
    box-shadow: 0 0 12px #58a6ff; transition: left 0.4s ease;
  }
  .bar-labels { display: flex; justify-content: space-between; font-size: 11px; color: #8b949e; }
  .bar-labels .sl { color: #f85149; }
  .bar-labels .tp { color: #3fb950; }
  .green { color: #3fb950 !important; }
  .red { color: #f85149 !important; }
  .yellow { color: #d29922 !important; }
  .gray { color: #6e7681 !important; }
  .footer { text-align: center; color: #6e7681; font-size: 12px; margin-top: 30px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; }
  .badge.green { background: #1f4d2a; color: #3fb950; }
  .badge.red { background: #4d2228; color: #f85149; }
  .err { background: #4d2228; color: #f85149; padding: 12px; border-radius: 8px; margin-bottom: 16px; }
  .pulse { animation: pulse 2s infinite; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.55; } 100% { opacity: 1; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>⚡ Funding-Rate Bot — Live</h1>
    <div class="ts" id="ts">Loading...</div>
  </div>
  <div id="error-area"></div>
  <div class="pnl-banner flat" id="pnl-banner">
    <div class="big" id="pnl-big">$0.00</div>
    <div class="small" id="pnl-small">Day P&L</div>
  </div>
  <div class="grid">
    <div class="card">
      <h2>Account</h2>
      <div class="row"><span class="label">Equity</span><span class="val" id="equity">—</span></div>
      <div class="row"><span class="label">Available cash</span><span class="val" id="balance">—</span></div>
      <div class="row"><span class="label">Margin in use</span><span class="val" id="margin">—</span></div>
      <div class="row"><span class="label">Unrealized P&L</span><span class="val" id="unrealized">—</span></div>
      <div class="row"><span class="label">Session started at</span><span class="val gray" id="session-start">—</span></div>
    </div>
    <div class="card">
      <h2>Next Funding Event</h2>
      <div class="row"><span class="label">Countdown</span><span class="val pulse" id="fund-cd">—</span></div>
      <div class="row"><span class="label">At</span><span class="val gray" id="fund-at">—</span></div>
      <div class="row"><span class="label">Est. payment to position</span><span class="val" id="fund-est">—</span></div>
    </div>
  </div>
  <h2 style="font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e;">Open Positions</h2>
  <div id="positions"></div>
  <div class="footer">Auto-refresh every 3 seconds. Source: live Binance Futures API.</div>
</div>
<script>
function fmt(n, d=2, signed=false) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  let s = (signed && n >= 0 ? '+' : '') + n.toFixed(d);
  return s;
}
function fmtPrice(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  if (Math.abs(n) < 0.001) return n.toFixed(8);
  if (Math.abs(n) < 1) return n.toFixed(6);
  return n.toFixed(4);
}
function colorClass(n) {
  if (n > 0.001) return 'green';
  if (n < -0.001) return 'red';
  return 'gray';
}
async function tick() {
  try {
    const r = await fetch('/data');
    const d = await r.json();
    const errArea = document.getElementById('error-area');
    if (d.error) {
      errArea.innerHTML = '<div class="err">API error: ' + d.error + '</div>';
      return;
    }
    errArea.innerHTML = '';
    document.getElementById('ts').textContent = d.ts;
    document.getElementById('balance').textContent = '$' + fmt(d.balance, 4);
    document.getElementById('margin').textContent = '$' + fmt(d.margin, 4);
    const unEl = document.getElementById('unrealized');
    unEl.textContent = '$' + fmt(d.unrealized, 4, true);
    unEl.className = 'val ' + colorClass(d.unrealized);
    document.getElementById('equity').textContent = '$' + fmt(d.equity, 4);
    document.getElementById('session-start').textContent = '$' + fmt(d.session_start, 2);
    const banner = document.getElementById('pnl-banner');
    document.getElementById('pnl-big').textContent = '$' + fmt(d.day_pnl, 4, true);
    document.getElementById('pnl-small').textContent = 'Day P&L (' + fmt(d.day_pct, 2, true) + '%)';
    if (d.day_pnl > 0.01) banner.className = 'pnl-banner green';
    else if (d.day_pnl < -0.01) banner.className = 'pnl-banner red';
    else banner.className = 'pnl-banner flat';
    document.getElementById('fund-cd').textContent = d.next_funding.countdown;
    document.getElementById('fund-at').textContent = d.next_funding.at;
    let totalEst = 0;
    for (const p of d.positions) totalEst += (p.funding_payment_estimate || 0);
    const fEst = document.getElementById('fund-est');
    fEst.textContent = d.positions.length ? '$' + fmt(totalEst, 4, true) : '—';
    fEst.className = 'val ' + colorClass(totalEst);
    const posDiv = document.getElementById('positions');
    if (!d.positions.length) {
      posDiv.innerHTML = '<div class="card" style="text-align:center; color:#6e7681;">No open positions. Bot scanning for setups.</div>';
    } else {
      let html = '';
      for (const p of d.positions) {
        const pct = Math.max(0, Math.min(100, ((p.mark - p.sl) / (p.tp - p.sl)) * 100));
        const algoBadge = p.algo_orders_count >= 2
          ? '<span class="badge green">SL+TP attached</span>'
          : '<span class="badge red">UNPROTECTED</span>';
        html += `
          <div class="pos">
            <h3>${p.symbol} <span style="font-weight:400; color:#8b949e">${p.side}</span> ${algoBadge}</h3>
            <div class="meta">
              <div><div class="label">Entry</div><div class="val">${fmtPrice(p.entry)}</div></div>
              <div><div class="label">Mark</div><div class="val">${fmtPrice(p.mark)}</div></div>
              <div><div class="label">Move</div><div class="val ${colorClass(p.chg_pct)}">${fmt(p.chg_pct, 3, true)}%</div></div>
              <div><div class="label">Unrealized</div><div class="val ${colorClass(p.unrealized)}">$${fmt(p.unrealized, 2, true)}</div></div>
            </div>
            <div class="bar-track">
              <div class="bar-zone-sl" style="width: 25%;"></div>
              <div class="bar-zone-tp" style="width: 25%;"></div>
              <div class="bar-marker" style="left: ${pct}%;"></div>
            </div>
            <div class="bar-labels">
              <span class="sl">SL ${fmtPrice(p.sl)} (${fmt(p.sl_dist_pct, 2, true)}% buffer)</span>
              <span class="tp">TP ${fmtPrice(p.tp)} (${fmt(p.tp_dist_pct, 2, true)}% needed)</span>
            </div>
            <div class="row" style="margin-top:12px;"><span class="label">Funding rate (next event)</span><span class="val ${colorClass(-p.funding_rate)}">${fmt(p.funding_rate, 3, true)}%</span></div>
            <div class="row"><span class="label">Quantity</span><span class="val">${p.qty.toLocaleString()}</span></div>
          </div>
        `;
      }
      posDiv.innerHTML = html;
    }
  } catch (e) {
    document.getElementById('error-area').innerHTML = '<div class="err">Connection error: ' + e + '</div>';
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return  # silence stdout

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/data":
            data = snapshot()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main() -> int:
    api_key, secret = load_credentials_from_env()
    if not api_key or not secret:
        print("ERROR: BINANCE_API_KEY / BINANCE_SECRET_KEY not in .env")
        return 1
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Dashboard running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
