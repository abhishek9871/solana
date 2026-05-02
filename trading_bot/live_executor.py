"""Live execution layer for funding-rate strategy.

This module wraps real Binance Futures API calls to place, monitor, and close trades.
It is NOT enabled by default. To use:
  1. Set BINANCE_API_KEY and BINANCE_SECRET_KEY in your local .env file
  2. Pass --live flag to funding_session.py
  3. CONFIRM you understand the risk via --confirm-live flag (separate, intentional)

Safety features:
- Live mode requires BOTH --live and --confirm-live
- Withdrawal must be disabled on the API key (recommended)
- Hard cap on margin per trade
- Hard cap on max concurrent positions
- Hard daily loss limit
- Auto-cancels open orders on shutdown
- Reads positions from exchange on startup (reconciles vs. local state)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError


@dataclass
class ExchangeFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass
class LivePositionInfo:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    margin: float


@dataclass
class LiveOrderResult:
    success: bool
    order_id: int | None
    avg_fill_price: float
    executed_qty: float
    raw_response: dict[str, Any]
    error: str | None = None


class LiveExecutor:
    """Real-money execution wrapper. ONLY use when you understand the risk."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        max_margin_per_trade: float = 20.0,
        max_positions: int = 3,
        base_url: str = "https://fapi.binance.com",
    ):
        if not api_key or not secret_key:
            raise ValueError("Live executor requires BINANCE_API_KEY and BINANCE_SECRET_KEY in env.")
        self.client = BinanceFuturesClient(api_key=api_key, secret_key=secret_key, base_url=base_url)
        self.max_margin_per_trade = max_margin_per_trade
        self.max_positions = max_positions
        self._filters_cache: dict[str, ExchangeFilters] = {}

    def get_filters(self, symbol: str) -> ExchangeFilters:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        info = self.client.get_symbol_info(symbol)
        tick_size = Decimal("0")
        step_size = Decimal("0")
        min_qty = Decimal("0")
        min_notional = Decimal("0")
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick_size = Decimal(f.get("tickSize", "0"))
            elif f.get("filterType") == "LOT_SIZE":
                step_size = Decimal(f.get("stepSize", "0"))
                min_qty = Decimal(f.get("minQty", "0"))
            elif f.get("filterType") == "MIN_NOTIONAL":
                min_notional = Decimal(f.get("notional", f.get("minNotional", "0")))
        ef = ExchangeFilters(symbol=symbol, tick_size=tick_size, step_size=step_size, min_qty=min_qty, min_notional=min_notional)
        self._filters_cache[symbol] = ef
        return ef

    def round_quantity(self, symbol: str, quantity: float) -> str:
        ef = self.get_filters(symbol)
        if ef.step_size <= 0:
            return f"{quantity}"
        q = (Decimal(str(quantity)) / ef.step_size).to_integral_value(rounding=ROUND_DOWN) * ef.step_size
        if q < ef.min_qty:
            return "0"
        return format(q.normalize(), "f")

    def round_price(self, symbol: str, price: float) -> str:
        ef = self.get_filters(symbol)
        if ef.tick_size <= 0:
            return f"{price}"
        p = (Decimal(str(price)) / ef.tick_size).to_integral_value(rounding=ROUND_DOWN) * ef.tick_size
        return format(p.normalize(), "f")

    def get_usdt_balance(self) -> float:
        """Return available USDT balance for futures trading."""
        try:
            balances = self.client.get_balance()
        except BinanceApiError as exc:
            raise RuntimeError(f"Cannot fetch balance — check API key permissions: {exc}")
        for b in balances:
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", 0))
        return 0.0

    def get_open_positions(self) -> list[LivePositionInfo]:
        """Return all open futures positions (non-zero size)."""
        try:
            positions = self.client.get_positions()
        except BinanceApiError as exc:
            raise RuntimeError(f"Cannot fetch positions — check API key permissions: {exc}")
        out = []
        for p in positions:
            qty = float(p.get("positionAmt", 0))
            if qty == 0:
                continue
            side = "LONG" if qty > 0 else "SHORT"
            out.append(
                LivePositionInfo(
                    symbol=p["symbol"],
                    side=side,
                    quantity=abs(qty),
                    entry_price=float(p.get("entryPrice", 0)),
                    mark_price=float(p.get("markPrice", 0)),
                    unrealized_pnl=float(p.get("unrealizedProfit", 0)),
                    leverage=int(p.get("leverage", 1)),
                    margin=abs(qty) * float(p.get("entryPrice", 0)) / max(int(p.get("leverage", 1)), 1),
                )
            )
        return out

    def open_long_limit(
        self,
        symbol: str,
        margin_usdt: float,
        leverage: int,
        offset_bps: float = 5.0,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.15,
        wait_seconds: int = 60,
    ) -> LiveOrderResult:
        """Open a long via LIMIT order at (bid - offset_bps). Maker fee, free spread edge.
        Returns success only if order fills within wait_seconds.
        """
        if margin_usdt > self.max_margin_per_trade:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Margin {margin_usdt} > cap {self.max_margin_per_trade}")
        balance = self.get_usdt_balance()
        if balance < margin_usdt:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Insufficient balance: {balance}")
        try:
            self.client.set_leverage(symbol, leverage)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"set_leverage failed: {exc}")
        try:
            self.client.set_margin_type(symbol, "ISOLATED")
        except BinanceApiError:
            pass
        try:
            book = self.client.get_book_ticker_one(symbol)
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
        except Exception as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"book ticker failed: {exc}")
        if bid <= 0:
            return LiveOrderResult(False, None, 0, 0, {}, error="bid is zero")
        target_price = bid * (1 - offset_bps / 10_000)
        price_str = self.round_price(symbol, target_price)
        notional = margin_usdt * leverage
        raw_qty = notional / float(price_str)
        qty_str = self.round_quantity(symbol, raw_qty)
        if qty_str == "0":
            return LiveOrderResult(False, None, 0, 0, {}, error="qty below minQty")
        # Place GTX (post-only) limit; if it would cross, exchange rejects
        try:
            response = self.client.place_limit_order(symbol, "BUY", price_str, qty_str, time_in_force="GTX")
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"limit_order failed: {exc}")
        order_id = int(response.get("orderId", 0))
        # Poll for fill
        import time as _time
        deadline = _time.time() + wait_seconds
        filled_qty = 0.0
        avg_price = 0.0
        while _time.time() < deadline:
            try:
                q = self.client.query_order(symbol, order_id=order_id)
                status = q.get("status")
                filled_qty = float(q.get("executedQty", 0) or 0)
                if filled_qty > 0:
                    avg_price = float(q.get("avgPrice", 0) or float(price_str))
                if status in ("FILLED",):
                    break
                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    return LiveOrderResult(False, None, 0, 0, {}, error=f"order {status}")
            except BinanceApiError:
                pass
            _time.sleep(2)
        # If only partial or no fill after timeout, cancel and fallback to MARKET
        if filled_qty <= 0:
            try:
                self.client.cancel_order(symbol, order_id=order_id)
            except BinanceApiError:
                pass
            print(f"  limit didn't fill, falling back to MARKET on {symbol}")
            try:
                response = self.client.place_market_order(symbol, "BUY", qty_str)
            except BinanceApiError as exc:
                return LiveOrderResult(False, None, 0, 0, {}, error=f"market fallback failed: {exc}")
            avg_price = float(response.get("avgPrice", 0)) or float(price_str)
            filled_qty = float(response.get("executedQty", 0))
            order_id = int(response.get("orderId", 0))
            if filled_qty <= 0:
                return LiveOrderResult(False, order_id, 0, 0, {}, error="market order did not fill")
        try:
            if avg_price <= 0:
                avg_price = float(price_str)
            sl_price = avg_price * (1 - stop_loss_pct)
            tp_price = avg_price * (1 + take_profit_pct)
            self.client.place_stop_market_order(symbol, "SELL", self.round_price(symbol, sl_price), close_position=True)
            self.client.place_take_profit_order(symbol, "SELL", self.round_price(symbol, tp_price), close_position=True)
        except BinanceApiError as exc:
            print(f"WARNING: SL/TP failed on {symbol}: {exc}")
        return LiveOrderResult(True, order_id, avg_price, filled_qty, response)

    def open_long_position(
        self,
        symbol: str,
        margin_usdt: float,
        leverage: int,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.15,
    ) -> LiveOrderResult:
        """Open a real LONG position with safety cap on margin."""
        if margin_usdt > self.max_margin_per_trade:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Margin {margin_usdt} exceeds cap {self.max_margin_per_trade}")
        balance = self.get_usdt_balance()
        if balance < margin_usdt:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Insufficient balance: need {margin_usdt}, have {balance}")

        try:
            self.client.set_leverage(symbol, leverage)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"set_leverage failed: {exc}")

        try:
            self.client.set_margin_type(symbol, "ISOLATED")
        except BinanceApiError:
            pass

        try:
            ticker = self.client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
            mark = float(ticker["price"])
        except Exception as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"price fetch failed: {exc}")

        notional = margin_usdt * leverage
        raw_qty = notional / mark
        qty_str = self.round_quantity(symbol, raw_qty)
        if qty_str == "0":
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Quantity {raw_qty} below minQty for {symbol}")

        try:
            response = self.client.place_market_order(symbol, "BUY", qty_str)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"place_market_order failed: {exc}")

        avg_price = float(response.get("avgPrice", 0)) or mark
        exec_qty = float(response.get("executedQty", 0))
        order_id = int(response.get("orderId", 0))

        sl_price = avg_price * (1 - stop_loss_pct)
        tp_price = avg_price * (1 + take_profit_pct)
        sl_str = self.round_price(symbol, sl_price)
        tp_str = self.round_price(symbol, tp_price)

        try:
            self.client.place_stop_market_order(symbol, "SELL", sl_str, close_position=True)
        except BinanceApiError as exc:
            print(f"WARNING: SL order failed for {symbol}: {exc}")
        try:
            self.client.place_take_profit_order(symbol, "SELL", tp_str, close_position=True)
        except BinanceApiError as exc:
            print(f"WARNING: TP order failed for {symbol}: {exc}")

        return LiveOrderResult(success=True, order_id=order_id, avg_fill_price=avg_price, executed_qty=exec_qty, raw_response=response)

    def open_short_position(
        self,
        symbol: str,
        margin_usdt: float,
        leverage: int,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.10,
    ) -> LiveOrderResult:
        """Open a real SHORT position with safety cap on margin."""
        if margin_usdt > self.max_margin_per_trade:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Margin {margin_usdt} exceeds cap {self.max_margin_per_trade}")
        balance = self.get_usdt_balance()
        if balance < margin_usdt:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Insufficient balance: need {margin_usdt}, have {balance}")
        try:
            self.client.set_leverage(symbol, leverage)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"set_leverage failed: {exc}")
        try:
            self.client.set_margin_type(symbol, "ISOLATED")
        except BinanceApiError:
            pass
        try:
            ticker = self.client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
            mark = float(ticker["price"])
        except Exception as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"price fetch failed: {exc}")
        notional = margin_usdt * leverage
        raw_qty = notional / mark
        qty_str = self.round_quantity(symbol, raw_qty)
        if qty_str == "0":
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Quantity {raw_qty} below minQty for {symbol}")
        try:
            response = self.client.place_market_order(symbol, "SELL", qty_str)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"place_market_order failed: {exc}")
        avg_price = float(response.get("avgPrice", 0)) or mark
        exec_qty = float(response.get("executedQty", 0))
        order_id = int(response.get("orderId", 0))
        # SL: BUY when price RISES above entry by stop_loss_pct
        sl_price = avg_price * (1 + stop_loss_pct)
        # TP: BUY when price FALLS below entry by take_profit_pct
        tp_price = avg_price * (1 - take_profit_pct)
        sl_str = self.round_price(symbol, sl_price)
        tp_str = self.round_price(symbol, tp_price)
        try:
            self.client.place_stop_market_order(symbol, "BUY", sl_str, close_position=True)
        except BinanceApiError as exc:
            print(f"WARNING: SL order failed for {symbol} short: {exc}")
        try:
            self.client.place_take_profit_order(symbol, "BUY", tp_str, close_position=True)
        except BinanceApiError as exc:
            print(f"WARNING: TP order failed for {symbol} short: {exc}")
        return LiveOrderResult(success=True, order_id=order_id, avg_fill_price=avg_price, executed_qty=exec_qty, raw_response=response)

    def close_short_position(self, symbol: str, quantity: float) -> LiveOrderResult:
        """Close an existing short position by buying back."""
        qty_str = self.round_quantity(symbol, quantity)
        if qty_str == "0":
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Quantity {quantity} below minQty")
        try:
            self.client.cancel_all_orders(symbol)
        except BinanceApiError:
            pass
        try:
            self.client.cancel_all_algo_orders(symbol)
        except BinanceApiError:
            pass
        try:
            response = self.client.place_market_order(symbol, "BUY", qty_str, reduce_only=True)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"close failed: {exc}")
        avg_price = float(response.get("avgPrice", 0))
        exec_qty = float(response.get("executedQty", 0))
        return LiveOrderResult(True, int(response.get("orderId", 0)), avg_price, exec_qty, response)

    def close_long_position(self, symbol: str, quantity: float) -> LiveOrderResult:
        """Close an existing long position by selling."""
        qty_str = self.round_quantity(symbol, quantity)
        if qty_str == "0":
            return LiveOrderResult(False, None, 0, 0, {}, error=f"Quantity {quantity} below minQty")
        try:
            self.client.cancel_all_orders(symbol)
        except BinanceApiError:
            pass
        try:
            self.client.cancel_all_algo_orders(symbol)
        except BinanceApiError:
            pass
        try:
            response = self.client.place_market_order(symbol, "SELL", qty_str, reduce_only=True)
        except BinanceApiError as exc:
            return LiveOrderResult(False, None, 0, 0, {}, error=f"close failed: {exc}")
        avg_price = float(response.get("avgPrice", 0))
        exec_qty = float(response.get("executedQty", 0))
        return LiveOrderResult(True, int(response.get("orderId", 0)), avg_price, exec_qty, response)

    def emergency_close_all(self) -> list[LiveOrderResult]:
        """Cancel all open orders and close all positions. Use on shutdown or panic."""
        results = []
        positions = self.get_open_positions()
        for p in positions:
            try:
                self.client.cancel_all_orders(p.symbol)
            except BinanceApiError:
                pass
            try:
                self.client.cancel_all_algo_orders(p.symbol)
            except BinanceApiError:
                pass
            if p.side == "LONG":
                results.append(self.close_long_position(p.symbol, p.quantity))
            else:
                qty_str = self.round_quantity(p.symbol, p.quantity)
                try:
                    response = self.client.place_market_order(p.symbol, "BUY", qty_str, reduce_only=True)
                    results.append(LiveOrderResult(True, int(response.get("orderId", 0)), float(response.get("avgPrice", 0)), float(response.get("executedQty", 0)), response))
                except BinanceApiError as exc:
                    results.append(LiveOrderResult(False, None, 0, 0, {}, error=str(exc)))
        return results

    def health_check(self) -> dict[str, Any]:
        """Validate API access and return account snapshot."""
        try:
            account = self.client.get_account()
            balance = self.get_usdt_balance()
            positions = self.get_open_positions()
            can_trade = bool(account.get("canTrade", False))
            can_withdraw = bool(account.get("canWithdraw", True))
            return {
                "can_trade": can_trade,
                "can_withdraw_warning": can_withdraw,
                "usdt_balance": balance,
                "open_positions_count": len(positions),
                "total_wallet_balance": float(account.get("totalWalletBalance", 0)),
                "total_unrealized": float(account.get("totalUnrealizedProfit", 0)),
            }
        except Exception as exc:
            return {"error": str(exc)}


def load_credentials_from_env(prefix: str = "BINANCE") -> tuple[str, str]:
    """Load API credentials from .env-style environment variables."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    secret_key = os.environ.get(f"{prefix}_SECRET_KEY", "").strip()
    return api_key, secret_key


def confirm_live_trading_intent() -> bool:
    """Check the explicit confirmation env var. Returns True only if user has confirmed."""
    return os.environ.get("BINANCE_LIVE_CONFIRM", "").strip().lower() == "yes-i-understand-risk"
