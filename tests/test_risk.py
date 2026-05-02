import unittest

from trading_bot.config import BotConfig
from trading_bot.models import BotState, Signal
from trading_bot.risk import RiskManager


class RiskManagerTests(unittest.TestCase):
    def test_buy_size_is_capped_by_max_trade_quote(self):
        config = BotConfig(max_trade_quote=25, position_fraction=0.5, min_trade_quote=10)
        state = BotState.fresh(starting_quote=1000, price=100)
        decision = RiskManager(config).evaluate(Signal("BUY", "test", 100), state)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.quote_amount, 25)

    def test_kill_switch_blocks_buy(self):
        config = BotConfig()
        state = BotState.fresh(starting_quote=1000, price=100)
        state.kill_switch = True
        decision = RiskManager(config).evaluate(Signal("BUY", "test", 100), state)
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
