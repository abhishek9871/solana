"""Local-only setup script: prompts for Binance API keys and writes .env.

Keys typed at the terminal stay local — they NEVER go to chat or any external service.
After saving, this script runs the readiness check to verify the keys work.

Usage:
  py setup_keys.py
  py setup_keys.py --testnet
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--testnet", action="store_true", help="store Binance Futures testnet keys")
    args = parser.parse_args()
    key_name = "BINANCE_TESTNET_API_KEY" if args.testnet else "BINANCE_API_KEY"
    secret_name = "BINANCE_TESTNET_SECRET_KEY" if args.testnet else "BINANCE_SECRET_KEY"

    print("=" * 60)
    print("LOCAL .env SETUP for Binance Futures" + (" TESTNET" if args.testnet else ""))
    print("=" * 60)
    print()
    print("Type your NEW API keys.")
    print("These stay on your computer only — nothing is sent anywhere.")
    print()

    api_key = input(f"Paste your new {key_name} (then press Enter):\n> ").strip()
    if not api_key or len(api_key) < 30:
        print("That doesn't look like a valid Binance API key. Aborting.")
        return 1

    secret = getpass.getpass(f"Paste your new {secret_name} (input hidden, press Enter):\n> ").strip()
    if not secret or len(secret) < 30:
        print("That doesn't look like a valid Binance secret. Aborting.")
        return 1

    project_root = Path(__file__).resolve().parent
    env_path = project_root / ".env"

    existing_lines: list[str] = []
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith(f"{key_name}=") or line.startswith(f"{secret_name}="):
                    continue
                existing_lines.append(line)

    new_lines = list(existing_lines)
    if new_lines and new_lines[-1] != "":
        new_lines.append("")
    new_lines.append(f"{key_name}={api_key}")
    new_lines.append(f"{secret_name}={secret}")

    with env_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass

    print()
    print(f"Saved to {env_path}")
    print()
    print("Now running the readiness check (this is read-only — no trades placed)...")
    print("=" * 60)

    sys.path.insert(0, str(project_root))
    if args.testnet:
        from trading_bot.binance_client import BinanceFuturesClient

        client = BinanceFuturesClient(
            api_key=api_key,
            secret_key=secret,
            timeout=5,
            base_url="https://testnet.binancefuture.com",
        )
        balance = client.get_balance()
        usdt = next((b for b in balance if b.get("asset") == "USDT"), None)
        if usdt:
            print(f"Testnet futures key works. USDT available: {usdt.get('availableBalance', '0')}")
            return 0
        print("Testnet key connected, but no USDT balance row was returned.")
        return 1
    from trading_bot import live_check
    return live_check.main()


if __name__ == "__main__":
    raise SystemExit(main())
