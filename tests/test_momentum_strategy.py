import unittest

from trading_bot.config import BotConfig
from trading_bot.models import BotState, Candle
from trading_bot.momentum_strategy import decide


class MomentumStrategyTests(unittest.TestCase):
    def test_returns_signal_for_synthetic_data(self):
        candles = [
            Candle(
                open_time=i * 60_000,
                open=100 + (i * 0.1),
                high=100.2 + (i * 0.1),
                low=99.8 + (i * 0.1),
                close=100 + (i * 0.1),
                volume=1,
                close_time=((i + 1) * 60_000) - 1,
            )
            for i in range(80)
        ]
        signal = decide(candles, BotState.fresh(1000, candles[-1].close), BotConfig())
        self.assertIn(signal.action, {"BUY", "HOLD"})


if __name__ == "__main__":
    unittest.main()
