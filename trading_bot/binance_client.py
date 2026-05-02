from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests


class BinanceApiError(RuntimeError):
    pass


class BinanceSpotClient:
    def __init__(self, base_url: str, api_key: str = "", secret_key: str = "", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.session = requests.Session()

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self.public_get("/api/v3/klines", params)

    def get_exchange_info(self, symbol: str) -> dict[str, Any]:
        return self.public_get("/api/v3/exchangeInfo", {"symbol": symbol})

    def get_account(self) -> dict[str, Any]:
        return self.signed_request("GET", "/api/v3/account", {})

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: str | None = None,
        quote_order_qty: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "newOrderRespType": "FULL",
        }
        if quantity is not None:
            params["quantity"] = quantity
        if quote_order_qty is not None:
            params["quoteOrderQty"] = quote_order_qty
        return self.signed_request("POST", "/api/v3/order", params)

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(self.base_url + path, params=params or {}, timeout=self.timeout)
        return self._handle_response(response)

    def signed_request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self.secret_key:
            raise BinanceApiError("Testnet API key and secret are required for signed requests.")

        signed_params = dict(params)
        signed_params["timestamp"] = int(time.time() * 1000)
        signed_params["recvWindow"] = 5000
        query = urlencode(signed_params, doseq=True)
        signature = hmac.new(self.secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        signed_params["signature"] = signature
        headers = {"X-MBX-APIKEY": self.api_key}
        response = self.session.request(
            method,
            self.base_url + path,
            params=signed_params if method.upper() == "GET" else None,
            data=signed_params if method.upper() != "GET" else None,
            headers=headers,
            timeout=self.timeout,
        )
        return self._handle_response(response)

    def _handle_response(self, response: requests.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceApiError(f"Binance returned non-JSON response: {response.text[:200]}") from exc
        if response.status_code >= 400:
            raise BinanceApiError(f"Binance API error {response.status_code}: {payload}")
        return payload


class BinanceFuturesClient(BinanceSpotClient):
    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        timeout: int = 10,
        base_url: str = "https://fapi.binance.com",
    ):
        super().__init__(base_url, api_key=api_key, secret_key=secret_key, timeout=timeout)

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self.public_get("/fapi/v1/klines", params)

    def get_exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        params = {"symbol": symbol} if symbol else {}
        return self.public_get("/fapi/v1/exchangeInfo", params)

    def get_24hr_tickers(self) -> list[dict[str, Any]]:
        return self.public_get("/fapi/v1/ticker/24hr")

    def get_book_tickers(self) -> list[dict[str, Any]]:
        return self.public_get("/fapi/v1/ticker/bookTicker")

    def get_account(self) -> dict[str, Any]:
        return self.signed_request("GET", "/fapi/v2/account", {})

    def get_balance(self) -> list[dict[str, Any]]:
        return self.signed_request("GET", "/fapi/v2/balance", {})

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/fapi/v2/positionRisk", params)

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self.signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict[str, Any]:
        return self.signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        reduce_only: bool = False,
        position_side: str = "BOTH",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT",
        }
        if position_side != "BOTH":
            params["positionSide"] = position_side
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/order", params)

    def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: str,
        quantity: str | None = None,
        close_position: bool = False,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Stop-market order via the Algo Order API (post-2025-12-09 migration)."""
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "triggerPrice": stop_price,
            "workingType": "MARK_PRICE",
            "priceProtect": "TRUE",
            "newOrderRespType": "RESULT",
        }
        if close_position:
            params["closePosition"] = "true"
        elif quantity is not None:
            params["quantity"] = quantity
            if reduce_only:
                params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/algoOrder", params)

    def place_take_profit_order(
        self,
        symbol: str,
        side: str,
        stop_price: str,
        quantity: str | None = None,
        close_position: bool = False,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Take-profit-market order via the Algo Order API (post-2025-12-09 migration)."""
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": stop_price,
            "workingType": "MARK_PRICE",
            "priceProtect": "TRUE",
            "newOrderRespType": "RESULT",
        }
        if close_position:
            params["closePosition"] = "true"
        elif quantity is not None:
            params["quantity"] = quantity
            if reduce_only:
                params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/algoOrder", params)

    def cancel_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self.signed_request("DELETE", "/fapi/v1/order", params)

    def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        return self.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def cancel_all_algo_orders(self, symbol: str) -> dict[str, Any]:
        """Cancel every open conditional/algo order for a symbol. Falls back to per-order cancel if batch path errors."""
        try:
            return self.signed_request("DELETE", "/fapi/v1/algoOrder/all", {"symbol": symbol})
        except BinanceApiError:
            results = []
            try:
                opens = self.get_open_algo_orders(symbol)
            except BinanceApiError:
                return {"results": results, "fallback": "list_failed"}
            order_list = opens if isinstance(opens, list) else opens.get("orders", [])
            for o in order_list:
                aid = o.get("algoId") or o.get("orderId")
                if not aid:
                    continue
                try:
                    results.append(self.cancel_algo_order(symbol, algo_id=int(aid)))
                except BinanceApiError as exc:
                    results.append({"error": str(exc), "algoId": aid})
            return {"results": results, "fallback": "per_order"}

    def cancel_algo_order(self, symbol: str, algo_id: int | None = None, client_algo_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = algo_id
        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id
        return self.signed_request("DELETE", "/fapi/v1/algoOrder", params)

    def get_book_ticker_one(self, symbol: str) -> dict[str, Any]:
        """Single-symbol best bid/ask. Used for limit-order pricing."""
        return self.public_get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        price: str,
        quantity: str,
        time_in_force: str = "GTX",
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Place a LIMIT order. GTX = post-only (rejected if would cross book = guaranteed maker)."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": quantity,
            "price": price,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.signed_request("POST", "/fapi/v1/order", params)

    def query_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self.signed_request("GET", "/fapi/v1/order", params)

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/fapi/v1/openOrders", params)

    def get_open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/fapi/v1/openAlgoOrders", params)

    def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        info = self.get_exchange_info(symbol)
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                return s
        raise BinanceApiError(f"Symbol {symbol} not found in exchangeInfo.")


def symbol_assets(exchange_info: dict[str, Any]) -> tuple[str, str]:
    symbol = exchange_info["symbols"][0]
    return symbol["baseAsset"], symbol["quoteAsset"]


def symbol_filters(exchange_info: dict[str, Any]) -> dict[str, dict[str, str]]:
    filters: dict[str, dict[str, str]] = {}
    for item in exchange_info["symbols"][0]["filters"]:
        filters[item["filterType"]] = item
    return filters


def min_notional(exchange_info: dict[str, Any]) -> Decimal:
    filters = symbol_filters(exchange_info)
    if "NOTIONAL" in filters:
        return Decimal(filters["NOTIONAL"].get("minNotional", "0"))
    if "MIN_NOTIONAL" in filters:
        return Decimal(filters["MIN_NOTIONAL"].get("minNotional", "0"))
    return Decimal("0")


def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
