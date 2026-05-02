from __future__ import annotations

from trading_bot.config import BotConfig
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.models import BotState, Candle, Signal
from trading_bot.strategy import decide as crossover_decide


def decide(candles: list[Candle], state: BotState, config: BotConfig) -> Signal:
    if state.is_open:
        return crossover_decide(candles, state, config)

    min_required = max(config.slow_ema, config.rsi_period, config.atr_period) + 3
    if len(candles) < min_required:
        price = candles[-1].close if candles else 0.0
        return Signal("HOLD", f"Need at least {min_required} candles.", price)

    close_values = closes(candles)
    fast = ema(close_values, config.fast_ema)
    slow = ema(close_values, config.slow_ema)
    rsi_values = rsi(close_values, config.rsi_period)
    atr_values = atr(candles, config.atr_period)

    price = close_values[-1]
    latest_fast = fast[-1]
    latest_slow = slow[-1]
    latest_rsi = rsi_values[-1]
    latest_atr = atr_values[-1]
    previous_close = close_values[-2]
    atr_pct = (latest_atr / price) if latest_atr else None
    ema_gap = ((latest_fast - latest_slow) / latest_slow) if latest_slow else None
    indicators = {
        "fast_ema": latest_fast,
        "slow_ema": latest_slow,
        "rsi": latest_rsi,
        "atr": latest_atr,
        "atr_pct": atr_pct,
        "ema_gap": ema_gap,
    }

    trend_ok = latest_fast > latest_slow and price > latest_fast and price > previous_close
    rsi_ok = latest_rsi is not None and 45 <= latest_rsi <= config.rsi_buy_ceiling
    volatility_ok = atr_pct is not None and 0.0003 <= atr_pct <= 0.025

    if trend_ok and rsi_ok and volatility_ok:
        return Signal("BUY", "Momentum trend setup passed scan rules.", price, indicators)

    return Signal("HOLD", "No momentum setup passed scan rules.", price, indicators)


def score(signal: Signal) -> float:
    ema_gap = signal.indicators.get("ema_gap") or 0.0
    atr_pct = signal.indicators.get("atr_pct") or 0.0
    latest_rsi = signal.indicators.get("rsi") or 50.0
    rsi_penalty = abs(float(latest_rsi) - 56.0) / 10_000
    return float(ema_gap) + float(atr_pct) - rsi_penalty
