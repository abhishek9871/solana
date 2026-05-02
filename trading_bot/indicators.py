from __future__ import annotations

from typing import Iterable

from trading_bot.models import Candle


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("EMA period must be positive.")
    if not values:
        return []

    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def rsi(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("RSI period must be positive.")
    if len(values) < period + 1:
        return [None] * len(values)

    result: list[float | None] = [None] * len(values)
    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def atr(candles: list[Candle], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("ATR period must be positive.")
    if len(candles) < period + 1:
        return [None] * len(candles)

    true_ranges = [0.0]
    for index in range(1, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )

    result: list[float | None] = [None] * len(candles)
    current_atr = sum(true_ranges[1 : period + 1]) / period
    result[period] = current_atr

    for index in range(period + 1, len(candles)):
        current_atr = ((current_atr * (period - 1)) + true_ranges[index]) / period
        result[index] = current_atr

    return result


def closes(candles: Iterable[Candle]) -> list[float]:
    return [candle.close for candle in candles]


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))
