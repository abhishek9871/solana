from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class BotConfig:
    mode: str = "paper"
    symbol: str = "BTCUSDT"
    interval: str = "5m"
    candle_limit: int = 150
    poll_seconds: int = 60

    fast_ema: int = 12
    slow_ema: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    rsi_buy_ceiling: float = 68.0
    stop_loss_pct: float = 0.0075
    take_profit_pct: float = 0.012
    trailing_stop_pct: float = 0.006

    starting_quote: float = 1000.0
    position_fraction: float = 0.10
    max_trade_quote: float = 50.0
    min_trade_quote: float = 10.0
    max_daily_loss_quote: float = 15.0
    fee_bps: float = 10.0

    mainnet_base_url: str = "https://api.binance.com"
    testnet_base_url: str = "https://testnet.binance.vision"
    testnet_api_key: str = ""
    testnet_secret_key: str = ""

    data_dir: Path = ROOT_DIR / "data"
    logs_dir: Path = ROOT_DIR / "logs"

    @property
    def state_path(self) -> Path:
        return self.data_dir / f"{self.mode}_state.json"

    @property
    def decisions_path(self) -> Path:
        return self.logs_dir / "decisions.jsonl"

    @property
    def trades_path(self) -> Path:
        return self.logs_dir / "trades.csv"


def load_config() -> BotConfig:
    load_dotenv(ROOT_DIR / ".env")
    mode = os.getenv("BOT_MODE", "paper").strip().lower()
    if mode not in {"paper", "testnet"}:
        raise ValueError("BOT_MODE must be either 'paper' or 'testnet'. Live mainnet trading is not implemented.")

    return BotConfig(
        mode=mode,
        symbol=os.getenv("SYMBOL", "BTCUSDT").strip().upper(),
        interval=os.getenv("INTERVAL", "5m").strip(),
        candle_limit=_int_env("CANDLE_LIMIT", 150),
        poll_seconds=_int_env("POLL_SECONDS", 60),
        fast_ema=_int_env("FAST_EMA", 12),
        slow_ema=_int_env("SLOW_EMA", 26),
        rsi_period=_int_env("RSI_PERIOD", 14),
        atr_period=_int_env("ATR_PERIOD", 14),
        rsi_buy_ceiling=_float_env("RSI_BUY_CEILING", 68.0),
        stop_loss_pct=_float_env("STOP_LOSS_PCT", 0.0075),
        take_profit_pct=_float_env("TAKE_PROFIT_PCT", 0.012),
        trailing_stop_pct=_float_env("TRAILING_STOP_PCT", 0.006),
        starting_quote=_float_env("STARTING_QUOTE", 1000.0),
        position_fraction=_float_env("POSITION_FRACTION", 0.10),
        max_trade_quote=_float_env("MAX_TRADE_QUOTE", 50.0),
        min_trade_quote=_float_env("MIN_TRADE_QUOTE", 10.0),
        max_daily_loss_quote=_float_env("MAX_DAILY_LOSS_QUOTE", 15.0),
        fee_bps=_float_env("FEE_BPS", 10.0),
        testnet_api_key=os.getenv("BINANCE_TESTNET_API_KEY", "").strip(),
        testnet_secret_key=os.getenv("BINANCE_TESTNET_SECRET_KEY", "").strip(),
    )
