from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from trading_bot.binance_client import (
    BinanceSpotClient,
    format_decimal,
    min_notional,
    round_step,
    symbol_filters,
)
from trading_bot.config import BotConfig
from trading_bot.models import BotState
from trading_bot.storage import append_trade, save_state


class TestnetBroker:
    def __init__(self, config: BotConfig, client: BinanceSpotClient, state: BotState):
        if not config.testnet_api_key or not config.testnet_secret_key:
            raise ValueError("Testnet mode requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_SECRET_KEY in .env.")
        self.config = config
        self.client = client
        self.state = state
        self.exchange_info = client.get_exchange_info(config.symbol)
        self.filters = symbol_filters(self.exchange_info)
        lot_size = self.filters.get("LOT_SIZE", {})
        self.step_size = Decimal(lot_size.get("stepSize", "0.00000001"))
        self.min_notional = min_notional(self.exchange_info)

    def mark_price(self, price: float) -> None:
        if self.state.is_open:
            self.state.high_watermark = max(self.state.high_watermark, price)
        else:
            self.state.high_watermark = price

    def buy(self, quote_amount: float, price: float, reason: str) -> dict[str, Any]:
        quote_decimal = Decimal(str(quote_amount))
        if quote_decimal < self.min_notional:
            raise ValueError(f"Quote amount {quote_decimal} is below exchange minimum notional {self.min_notional}.")

        response = self.client.place_market_order(
            self.config.symbol,
            "BUY",
            quote_order_qty=format_decimal(quote_decimal),
        )
        executed_base = Decimal(response.get("executedQty", "0"))
        spent_quote = Decimal(response.get("cummulativeQuoteQty", "0"))

        self.state.quote_qty = max(self.state.quote_qty - float(spent_quote), 0.0)
        self.state.base_qty = float(executed_base)
        self.state.position_symbol = self.config.symbol
        self.state.entry_price = float(spent_quote / executed_base) if executed_base > 0 else price
        self.state.entry_quote_spent = float(spent_quote)
        self.state.high_watermark = price
        self.state.last_order_id = str(response.get("orderId", ""))
        save_state(self.config.state_path, self.state)

        row = self._trade_row("BUY", self.state.entry_price, float(executed_base), float(spent_quote), 0.0, 0.0, reason, self.state.last_order_id)
        append_trade(self.config.trades_path, row)
        return row

    def sell_all(self, price: float, reason: str) -> dict[str, Any]:
        quantity = round_step(Decimal(str(self.state.base_qty)), self.step_size)
        if quantity <= 0:
            raise ValueError("No rounded testnet quantity available to sell.")

        response = self.client.place_market_order(
            self.config.symbol,
            "SELL",
            quantity=format_decimal(quantity),
        )
        sold_base = Decimal(response.get("executedQty", "0"))
        received_quote = Decimal(response.get("cummulativeQuoteQty", "0"))
        realized_pnl = float(received_quote) - self.state.entry_quote_spent

        self.state.quote_qty += float(received_quote)
        self.state.base_qty = 0.0
        self.state.position_symbol = ""
        self.state.entry_price = 0.0
        self.state.entry_quote_spent = 0.0
        self.state.high_watermark = price
        self.state.realized_pnl += realized_pnl
        self.state.last_order_id = str(response.get("orderId", ""))
        save_state(self.config.state_path, self.state)

        row = self._trade_row("SELL", price, float(sold_base), float(received_quote), 0.0, realized_pnl, reason, self.state.last_order_id)
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
