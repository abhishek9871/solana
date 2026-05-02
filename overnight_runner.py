"""
Overnight wrapper for perfect_harmony.

Runs perfect_harmony.py in a loop. After each exit (kill switch / loss limit /
crash), waits 10 minutes then restarts. Safety floors:
  - Stops if wallet drops below MIN_BALANCE (preserves remaining capital)
  - Stops after MAX_RESTARTS attempts
  - Honors STOP_AUTO.flag at any time
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

MIN_BALANCE = 12.0          # halt if wallet < this (preserves ~$5)
RESTART_DELAY_SEC = 600     # 10 min between restarts
MAX_RESTARTS = 8            # ~80 min worst-case downtime, plenty for overnight
STOP_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP_AUTO.flag")


def log(msg):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WRAPPER] {safe}", flush=True)


def get_usdt():
    try:
        bals = client.get_balance()
        usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
        return float(usdt.get("availableBalance", 0)) if usdt else 0.0
    except Exception as e:
        log(f"balance check err: {e}")
        return -1


def sleep_with_stop_check(seconds):
    """Sleep but check STOP_FLAG every 30 sec."""
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(STOP_FLAG):
            log("STOP flag detected during cooldown.")
            return False
        time.sleep(min(30, end - time.time()))
    return True


def main():
    start_balance = get_usdt()
    log(f"=== OVERNIGHT RUNNER START ===")
    log(f"Starting wallet: ${start_balance:.4f}")
    log(f"Will run perfect_harmony up to {MAX_RESTARTS + 1} sessions, "
        f"{RESTART_DELAY_SEC}s between restarts.")
    log(f"Halt if wallet < ${MIN_BALANCE:.2f} or STOP_AUTO.flag created.")

    for attempt in range(MAX_RESTARTS + 1):
        if os.path.exists(STOP_FLAG):
            log("STOP flag present, exiting.")
            break

        bal = get_usdt()
        if bal < 0:
            log(f"Could not check balance, skipping run.")
            time.sleep(60)
            continue
        if bal < MIN_BALANCE:
            log(f"Wallet ${bal:.4f} < ${MIN_BALANCE:.2f}, halting wrapper.")
            break

        log(f"--- Session #{attempt + 1}/{MAX_RESTARTS + 1} | Wallet ${bal:.4f} ---")
        try:
            proc = subprocess.run(
                ["py", "perfect_harmony.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            log(f"perfect_harmony exited code={proc.returncode}")
        except Exception as e:
            log(f"subprocess err: {e}")

        if attempt < MAX_RESTARTS:
            log(f"Cooldown {RESTART_DELAY_SEC}s before next session...")
            if not sleep_with_stop_check(RESTART_DELAY_SEC):
                break

    final = get_usdt()
    log(f"=== OVERNIGHT RUNNER DONE ===")
    log(f"Final wallet: ${final:.4f}  (start ${start_balance:.4f}, "
        f"net {final - start_balance:+.4f})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user.")
