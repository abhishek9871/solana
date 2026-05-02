from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.models import BotState, Signal


def load_state(path: Path, starting_quote: float, price: float) -> BotState:
    if not path.exists():
        return BotState.fresh(starting_quote, price)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    allowed = {field.name for field in fields(BotState)}
    defaults = asdict(BotState.fresh(starting_quote, price))
    defaults.update({key: value for key, value in payload.items() if key in allowed})
    return BotState(**defaults)


def save_state(path: Path, state: BotState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(state), handle, indent=2, sort_keys=True)


def append_decision(path: Path, mode: str, symbol: str, signal: Signal, equity: float, risk_reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "symbol": symbol,
        "action": signal.action,
        "reason": signal.reason,
        "price": signal.price,
        "equity": equity,
        "risk_reason": risk_reason,
        "indicators": signal.indicators,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def append_trade(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ts",
        "mode",
        "symbol",
        "side",
        "price",
        "base_qty",
        "quote_qty",
        "fee_quote",
        "realized_pnl",
        "reason",
        "order_id",
    ]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fieldnames})
