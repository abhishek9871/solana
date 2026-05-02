import unittest

from trading_bot.leveraged_session import LeverageConfig, LeveragedState, TradeSignal, close_position, open_position


class LeveragedSessionTests(unittest.TestCase):
    def test_long_position_profit_updates_cash(self):
        config = LeverageConfig(symbols=["BTCUSDT"], starting_quote=100000, leverage=10, max_margin_quote=1000)
        state = LeveragedState.fresh(100000)
        signal = TradeSignal("OPEN", "LONG", "BTCUSDT", 100.0, 1.0, "test", {})
        open_position(state, signal, config)
        close_signal = TradeSignal("CLOSE", "LONG", "BTCUSDT", 101.0, 1.0, "test", {})
        row = close_position(state, close_signal, config)
        self.assertGreater(row["realized_pnl"], 0)
        self.assertFalse(state.is_open)


if __name__ == "__main__":
    unittest.main()
