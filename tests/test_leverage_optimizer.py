import unittest

from trading_bot.leverage_optimizer import base_config, candidate_configs


class LeverageOptimizerTests(unittest.TestCase):
    def test_candidate_configs_are_generated(self):
        args = type(
            "Args",
            (),
            {"interval": "1m", "candle_limit": 100, "starting_quote": 50, "challenge": False},
        )()
        config = base_config(["BTCUSDT"], args)
        candidates = list(candidate_configs(config))
        self.assertGreater(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
