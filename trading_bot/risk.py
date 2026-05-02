from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_bot.config import BotConfig
from trading_bot.models import BotState, Signal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    action: str
    reason: str
    quote_amount: float = 0.0


class RiskManager:
    def __init__(self, config: BotConfig):
        self.config = config

    def refresh_daily_limits(self, state: BotState, price: float) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        equity = state.equity(price)
        if state.daily_start_date != today:
            state.daily_start_date = today
            state.daily_start_equity = equity
            state.kill_switch = False

        drawdown = state.daily_start_equity - equity
        if drawdown >= self.config.max_daily_loss_quote:
            state.kill_switch = True

    def evaluate(self, signal: Signal, state: BotState) -> RiskDecision:
        if signal.action == "HOLD":
            return RiskDecision(False, "HOLD", "Strategy chose HOLD.")

        if state.kill_switch:
            return RiskDecision(False, "HOLD", "Daily max-loss kill switch is active.")

        if signal.action == "SELL":
            if not state.is_open:
                return RiskDecision(False, "HOLD", "No open position to sell.")
            return RiskDecision(True, "SELL", "Exit allowed.")

        if signal.action != "BUY":
            return RiskDecision(False, "HOLD", f"Unknown signal action: {signal.action}")

        if state.is_open:
            return RiskDecision(False, "HOLD", "Already in a position.")

        equity = state.equity(signal.price)
        quote_amount = min(
            state.quote_qty,
            equity * self.config.position_fraction,
            self.config.max_trade_quote,
        )
        if quote_amount < self.config.min_trade_quote:
            return RiskDecision(False, "HOLD", "Calculated trade size is below MIN_TRADE_QUOTE.")

        projected_drawdown = state.daily_start_equity - equity
        if projected_drawdown >= self.config.max_daily_loss_quote:
            state.kill_switch = True
            return RiskDecision(False, "HOLD", "Daily max-loss limit reached before entry.")

        return RiskDecision(True, "BUY", "Entry allowed by risk limits.", quote_amount)
