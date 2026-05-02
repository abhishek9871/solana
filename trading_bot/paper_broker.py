from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading_bot.config import BotConfig
from trading_bot.models import BotState
from trading_bot.storage import append_trade, save_state


class PaperBroker:
    def __init__(self, config: BotConfig, state: BotState):
        self.config = config
        self.state = state

    def mark_price(self, price: float) -> None:
        if self.state.is_open:
            self.state.high_watermark = max(self.state.high_watermark, price)
        else:
            self.state.high_watermark = price

    def buy(self, quote_amount: float, price: float, reason: str) -> dict[str, Any]:
        fee_quote = quote_amount * (self.config.fee_bps / 10_000)
        net_quote = quote_amount - fee_quote
        base_qty = net_quote / price

        self.state.quote_qty -= quote_amount
        self.state.base_qty = base_qty
        self.state.position_symbol = self.config.symbol
        self.state.entry_price = price
        self.state.entry_quote_spent = quote_amount
        self.state.high_watermark = price
        self.state.last_order_id = f"paper-{int(datetime.now(timezone.utc).timestamp())}"
        save_state(self.config.state_path, self.state)

        row = self._trade_row("BUY", price, base_qty, quote_amount, fee_quote, 0.0, reason, self.state.last_order_id)
        append_trade(self.config.trades_path, row)
        return row

    def sell_all(self, price: float, reason: str) -> dict[str, Any]:
        gross_quote = self.state.base_qty * price
        fee_quote = gross_quote * (self.config.fee_bps / 10_000)
        net_quote = gross_quote - fee_quote
        realized_pnl = net_quote - self.state.entry_quote_spent
        base_qty = self.state.base_qty

        self.state.quote_qty += net_quote
        self.state.base_qty = 0.0
        self.state.position_symbol = ""
        self.state.entry_price = 0.0
        self.state.entry_quote_spent = 0.0
        self.state.high_watermark = price
        self.state.realized_pnl += realized_pnl
        self.state.last_order_id = f"paper-{int(datetime.now(timezone.utc).timestamp())}"
        save_state(self.config.state_path, self.state)

        row = self._trade_row("SELL", price, base_qty, gross_quote, fee_quote, realized_pnl, reason, self.state.last_order_id)
        append_trade(self.config.trades_path, row)
        return row

    def _trade_row(
        self,
        side: str,
        price: float,
        base_qty: float,
        quote_qty: float,
        fee_quote: float,
        realized_pnl: float,
        reason: str,
        order_id: str,
    ) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.config.mode,
            "symbol": self.config.symbol,
            "side": side,
            "price": round(price, 8),
            "base_qty": round(base_qty, 10),
            "quote_qty": round(quote_qty, 8),
            "fee_quote": round(fee_quote, 8),
            "realized_pnl": round(realized_pnl, 8),
            "reason": reason,
            "order_id": order_id,
        }
