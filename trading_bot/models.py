from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @classmethod
    def from_binance(cls, row: list[Any]) -> "Candle":
        return cls(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    price: float
    indicators: dict[str, float | None] = field(default_factory=dict)


@dataclass
class BotState:
    quote_qty: float
    base_qty: float = 0.0
    position_symbol: str = ""
    entry_price: float = 0.0
    entry_quote_spent: float = 0.0
    high_watermark: float = 0.0
    realized_pnl: float = 0.0
    daily_start_date: str = ""
    daily_start_equity: float = 0.0
    kill_switch: bool = False
    last_order_id: str = ""

    @property
    def is_open(self) -> bool:
        return self.base_qty > 0

    @classmethod
    def fresh(cls, starting_quote: float, price: float) -> "BotState":
        today = datetime.now(timezone.utc).date().isoformat()
        return cls(
            quote_qty=starting_quote,
            daily_start_date=today,
            daily_start_equity=starting_quote,
            high_watermark=price,
        )

    def equity(self, price: float) -> float:
        return self.quote_qty + (self.base_qty * price)
