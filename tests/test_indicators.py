import unittest

from trading_bot.indicators import ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_returns_one_value_per_input(self):
        values = [1, 2, 3, 4, 5]
        result = ema(values, 3)
        self.assertEqual(len(result), len(values))
        self.assertGreater(result[-1], result[0])

    def test_rsi_for_rising_series_reaches_100(self):
        values = list(range(1, 20))
        result = rsi(values, 14)
        self.assertEqual(result[-1], 100.0)


if __name__ == "__main__":
    unittest.main()
