import unittest

from trading_bot.backtest import run_backtest
from trading_bot.config import BotConfig
from trading_bot.models import Candle


class BacktestTests(unittest.TestCase):
    def test_backtest_runs_with_synthetic_candles(self):
        candles = [
            Candle(
                open_time=i * 60_000,
                open=100 + i,
                high=101 + i,
                low=99 + i,
                close=100 + i,
                volume=1,
                close_time=((i + 1) * 60_000) - 1,
            )
            for i in range(80)
        ]
        config = BotConfig(starting_quote=1000, max_trade_quote=50, min_trade_quote=10)
        result = run_backtest(candles, config)
        self.assertEqual(result["candles"], 80)
        self.assertIn("ending_equity", result)


if __name__ == "__main__":
    unittest.main()
