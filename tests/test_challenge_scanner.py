import unittest

from trading_bot.challenge_scanner import required_move_pct


class ChallengeScannerTests(unittest.TestCase):
    def test_required_move_for_50_usdt_target_is_positive(self):
        args = type(
            "Args",
            (),
            {
                "starting_quote": 50,
                "margin_fraction": 0.9,
                "max_margin": 45,
                "leverage": 50,
                "target_profit": 20,
            },
        )()
        self.assertGreater(required_move_pct(args), 0.0)


if __name__ == "__main__":
    unittest.main()
