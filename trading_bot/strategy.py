from __future__ import annotations

from trading_bot.config import BotConfig
from trading_bot.indicators import atr, closes, ema, rsi
from trading_bot.models import BotState, Candle, Signal


def decide(candles: list[Candle], state: BotState, config: BotConfig) -> Signal:
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
    previous_fast = fast[-2]
    previous_slow = slow[-2]
    latest_fast = fast[-1]
    latest_slow = slow[-1]
    latest_rsi = rsi_values[-1]
    latest_atr = atr_values[-1]

    crossed_up = previous_fast <= previous_slow and latest_fast > latest_slow
    crossed_down = previous_fast >= previous_slow and latest_fast < latest_slow
    indicators = {
        "fast_ema": latest_fast,
        "slow_ema": latest_slow,
        "rsi": latest_rsi,
        "atr": latest_atr,
    }

    if state.is_open:
        high_watermark = max(state.high_watermark, price)
        stop_price = state.entry_price * (1 - config.stop_loss_pct)
        take_profit_price = state.entry_price * (1 + config.take_profit_pct)
        trailing_stop_price = high_watermark * (1 - config.trailing_stop_pct)

        if price <= stop_price:
            return Signal("SELL", "Stop loss reached.", price, indicators)
        if price >= take_profit_price:
            return Signal("SELL", "Take profit reached.", price, indicators)
        if price <= trailing_stop_price and high_watermark > state.entry_price:
            return Signal("SELL", "Trailing stop reached.", price, indicators)
        if crossed_down:
            return Signal("SELL", "Fast EMA crossed below slow EMA.", price, indicators)
        return Signal("HOLD", "Position open; no exit rule triggered.", price, indicators)

    rsi_ok = latest_rsi is not None and latest_rsi <= config.rsi_buy_ceiling
    trend_ok = latest_fast > latest_slow and price > latest_slow
    if crossed_up and trend_ok and rsi_ok:
        return Signal("BUY", "Fast EMA crossed above slow EMA with acceptable RSI.", price, indicators)

    return Signal("HOLD", "No entry rule triggered.", price, indicators)
