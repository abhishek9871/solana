"""Live trading readiness check.

Run this after setting up your .env file to verify your API key works
WITHOUT placing any real trades. It will:
  1. Load credentials from .env
  2. Connect to Binance Futures API
  3. Verify trading permissions
  4. Show your USDT balance and open positions
  5. Run the funding-rate scanner (read-only) to show what trades it WOULD take

This is the safe step before --live trading.

Usage:
  py -m trading_bot.live_check
"""

from __future__ import annotations

import sys

from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import LiveExecutor, load_credentials_from_env
from trading_bot.funding_session import (
    FundingConfig,
    config_from_args,
    evaluate_entry_candidates,
)


def main() -> int:
    print("=" * 60)
    print("LIVE READINESS CHECK (no real trades placed)")
    print("=" * 60)

    api_key, secret_key = load_credentials_from_env()
    if not api_key or not secret_key:
        print()
        print("ERROR: BINANCE_API_KEY or BINANCE_SECRET_KEY not set.")
        print()
        print("Steps to enable:")
        print("  1. Create a Binance Futures account (must opt in to futures)")
        print("  2. Create an API key at https://www.binance.com/en/my/settings/api-management")
        print("  3. Enable 'Enable Futures' permission")
        print("  4. STRONGLY RECOMMENDED: disable 'Enable Withdrawals'")
        print("  5. Whitelist your IP if possible")
        print("  6. Add to .env in this project:")
        print("       BINANCE_API_KEY=your_key_here")
        print("       BINANCE_SECRET_KEY=your_secret_here")
        print()
        print("Then re-run this command.")
        return 1

    print(f"Credentials loaded: API_KEY={api_key[:6]}... SECRET=*****")
    print()

    try:
        executor = LiveExecutor(api_key=api_key, secret_key=secret_key)
    except Exception as exc:
        print(f"ERROR creating executor: {exc}")
        return 2

    print("Running health check...")
    health = executor.health_check()
    if "error" in health:
        print(f"HEALTH CHECK FAILED: {health['error']}")
        print()
        print("Common causes:")
        print("  - API key has no Futures permission")
        print("  - Wrong secret key")
        print("  - IP not whitelisted")
        return 3

    print()
    print("=== ACCOUNT STATUS ===")
    print(f"  Can trade:           {health['can_trade']}")
    print(f"  Withdrawals enabled: {health['can_withdraw_warning']}  {'(WARNING: disable for safety)' if health['can_withdraw_warning'] else '(GOOD: disabled)'}")
    print(f"  USDT balance:        {health['usdt_balance']:.4f}")
    print(f"  Total wallet:        {health['total_wallet_balance']:.4f}")
    print(f"  Open positions:      {health['open_positions_count']}")
    print(f"  Unrealized PnL:      {health['total_unrealized']:+.4f}")

    if health["open_positions_count"] > 0:
        print()
        print("=== EXISTING OPEN POSITIONS ===")
        for p in executor.get_open_positions():
            print(f"  {p.symbol:<14} {p.side:<5} qty={p.quantity:.6g} entry={p.entry_price:.6g} mark={p.mark_price:.6g} pnl={p.unrealized_pnl:+.2f}")

    print()
    print("=== WHAT THE FUNDING-RATE BOT WOULD DO RIGHT NOW ===")

    class _Args:
        starting_quote = 50.0
        leverage = 10.0
        margin_per_trade = 18.0
        fee_bps = 5.0
        funding_threshold = -0.003
        hold_hours = 24.0
        stop_loss_pct = 0.05
        take_profit_pct = 0.15
        max_event_age_hours = 4.0
        poll_seconds = 60
        max_daily_loss = 30.0
        cooldown_minutes = 5
        target_pnl = 50.0
        top_usdt = 80
        max_positions = 3

    config = config_from_args(_Args())
    public_client = BinanceFuturesClient()
    candidates = evaluate_entry_candidates(public_client, config, exclude=set())
    if not candidates:
        print("  No qualifying setups right now (no funding rate < -0.3% recent enough)")
    else:
        print(f"  Found {len(candidates)} candidate(s) — would open up to {config.max_positions}:")
        for c in candidates[: config.max_positions]:
            print(f"    {c['symbol']:<14} funding_rate={c['funding_rate']*100:+.3f}% mark={c['mark_price']:.6g}")

    print()
    print("=" * 60)
    print("READINESS: " + ("OK" if health["can_trade"] else "BLOCKED — fix permissions above"))
    print("=" * 60)
    print()
    print("Next step (only when you understand the risk):")
    print("  Set BINANCE_LIVE_CONFIRM=yes-i-understand-risk in your .env")
    print("  Run: py -m trading_bot.funding_session --live")
    print()
    print("Until then, paper mode runs as usual (no real money).")
    return 0 if health.get("can_trade") else 4


if __name__ == "__main__":
    raise SystemExit(main())
