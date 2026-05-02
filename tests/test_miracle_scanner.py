import unittest

from trading_bot.miracle_scanner import best_plan


class MiracleScannerTests(unittest.TestCase):
    def test_best_plan_respects_target_move_cap(self):
        plan = best_plan(balance=39.05, target_profit=15.95, fee_bps=5, max_loss=8, target_move_cap=0.02)
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan.target_move_pct, 0.02)


if __name__ == "__main__":
    unittest.main()
