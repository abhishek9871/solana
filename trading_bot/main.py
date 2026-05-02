from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace

from trading_bot.binance_client import BinanceApiError, BinanceSpotClient
from trading_bot.config import BotConfig, load_config
from trading_bot.models import Candle
from trading_bot.paper_broker import PaperBroker
from trading_bot.risk import RiskManager
from trading_bot.storage import append_decision, load_state, save_state
from trading_bot.strategy import decide
from trading_bot.testnet_broker import TestnetBroker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safety-first Binance paper/testnet bot.")
    parser.add_argument("--mode", choices=["paper", "testnet"], help="Override BOT_MODE from .env.")
    parser.add_argument("--symbol", help="Override SYMBOL from .env, for example BTCUSDT.")
    parser.add_argument("--once", action="store_true", help="Run exactly one decision cycle.")
    parser.add_argument("--loop", action="store_true", help="Run continuously until interrupted.")
    parser.add_argument("--poll-seconds", type=int, help="Override POLL_SECONDS from .env.")
    return parser.parse_args()


def apply_overrides(config: BotConfig, args: argparse.Namespace) -> BotConfig:
    updates = {}
    if args.mode:
        updates["mode"] = args.mode
    if args.symbol:
        updates["symbol"] = args.symbol.upper()
    if args.poll_seconds:
        updates["poll_seconds"] = args.poll_seconds
    return replace(config, **updates)


def build_market_client(config: BotConfig) -> BinanceSpotClient:
    if config.mode == "testnet":
        return BinanceSpotClient(
            config.testnet_base_url,
            api_key=config.testnet_api_key,
            secret_key=config.testnet_secret_key,
        )
    return BinanceSpotClient(config.mainnet_base_url)


def run_cycle(config: BotConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    client = build_market_client(config)
    candles = [Candle.from_binance(row) for row in client.get_klines(config.symbol, config.interval, config.candle_limit)]
    if not candles:
        raise RuntimeError("No candles returned by Binance.")

    price = candles[-1].close
    state = load_state(config.state_path, config.starting_quote, price)
    risk = RiskManager(config)
    risk.refresh_daily_limits(state, price)

    broker = TestnetBroker(config, client, state) if config.mode == "testnet" else PaperBroker(config, state)
    broker.mark_price(price)

    signal = decide(candles, state, config)
    decision = risk.evaluate(signal, state)
    equity = state.equity(price)
    append_decision(config.decisions_path, config.mode, config.symbol, signal, equity, decision.reason)

    trade_row = None
    if decision.allowed and decision.action == "BUY":
        trade_row = broker.buy(decision.quote_amount, price, signal.reason)
    elif decision.allowed and decision.action == "SELL":
        trade_row = broker.sell_all(price, signal.reason)
    else:
        save_state(config.state_path, state)

    print_status(config, signal.action, decision.reason, price, equity, state.kill_switch, trade_row)


def print_status(
    config: BotConfig,
    action: str,
    reason: str,
    price: float,
    equity: float,
    kill_switch: bool,
    trade_row: dict | None,
) -> None:
    status = "KILL_SWITCH" if kill_switch else "ACTIVE"
    print(
        f"{config.mode.upper()} {config.symbol} price={price:.8f} equity={equity:.2f} "
        f"signal={action} status={status} reason={reason}"
    )
    if trade_row:
        print(
            f"TRADE {trade_row['side']} base={trade_row['base_qty']} "
            f"quote={trade_row['quote_qty']} price={trade_row['price']} pnl={trade_row['realized_pnl']}"
        )


def main() -> int:
    args = parse_args()
    if not args.once and not args.loop:
        args.once = True

    try:
        config = apply_overrides(load_config(), args)
        while True:
            run_cycle(config)
            if args.once:
                return 0
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except (BinanceApiError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
