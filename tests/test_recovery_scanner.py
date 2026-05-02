import unittest

from trading_bot.recovery_scanner import logged_loser_symbols, risk_plans


class RecoveryScannerTests(unittest.TestCase):
    def test_risk_plans_respect_max_loss(self):
        plans = risk_plans(balance=39.05, target_profit=15.95, fee_bps=5, max_loss=8)
        self.assertTrue(plans)
        self.assertTrue(all(plan.stop_loss_cash <= 8 for plan in plans))

    def test_logged_loser_symbols_returns_set(self):
        self.assertIsInstance(logged_loser_symbols(), set)


if __name__ == "__main__":
    unittest.main()
