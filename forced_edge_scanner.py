"""Read-only forced-edge scanner.

This is not a directional trading bot. It scans for opportunities where profit
can be calculated before execution:

1. Compound III discounted collateral sales on Base, checked against Uniswap V3.
2. Binance spot triangular paths, simulated through real order-book depth.
3. Binance spot/perp carry candidates, netted against rough entry costs.

No private keys are loaded. No orders or transactions are sent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CACHE_DIR = DATA_DIR / "forced_edge_cache"


ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(f"[{utc_now()}] {msg}", flush=True)


def clean_addr(value: str) -> str:
    v = value.lower()
    if not v.startswith("0x"):
        v = "0x" + v
    if len(v) != 42:
        raise ValueError(f"bad address: {value}")
    return v


def abi_word(value: int) -> str:
    if value < 0:
        value = (1 << 256) + value
    return f"{value:064x}"


def abi_addr(value: str) -> str:
    return clean_addr(value)[2:].rjust(64, "0")


def int_from_hex_word(data: str, index: int = 0) -> int:
    raw = data[2:] if data.startswith("0x") else data
    start = index * 64
    if len(raw) < start + 64:
        return 0
    return int(raw[start : start + 64], 16)


def bool_from_hex(data: str) -> bool:
    return int_from_hex_word(data) != 0


def topic_to_addr(topic: str) -> str | None:
    raw = topic[2:] if topic.startswith("0x") else topic
    if len(raw) != 64:
        return None
    addr = "0x" + raw[-40:]
    return None if addr == ZERO_ADDR else addr.lower()


def human(amount: int | float, decimals: int) -> float:
    return float(amount) / (10**decimals)


def base_units(amount: float, decimals: int) -> int:
    return int(math.floor(amount * (10**decimals)))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 12, headers: dict[str, str] | None = None) -> Any:
    r = requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def post_json(url: str, payload: dict[str, Any], timeout: int = 12) -> Any:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


@dataclass(order=True)
class Opportunity:
    sort_key: float = field(init=False, repr=False)
    module: str
    venue: str
    kind: str
    symbol: str
    gross_usd: float
    cost_usd: float
    net_usd: float
    executable: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sort_key = self.net_usd

    def short(self) -> str:
        flag = "EDGE" if self.executable and self.net_usd > 0 else "NO_EDGE"
        return (
            f"{flag:<7} {self.module:<10} {self.venue:<14} {self.kind:<18} "
            f"{self.symbol:<18} gross=${self.gross_usd:+.4f} "
            f"cost=${self.cost_usd:.4f} net=${self.net_usd:+.4f} {self.reason}"
        )


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True, default=str) + "\n")


class EvmRpc:
    def __init__(self, url: str, timeout: int = 12):
        urls = [u.strip() for u in url.split(",") if u.strip()]
        self.urls = urls or ["https://base-rpc.publicnode.com"]
        self.url_index = 0
        self.timeout = timeout
        self._id = 0

    def rpc(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        last_error: Exception | None = None
        for attempt in range(len(self.urls)):
            idx = (self.url_index + attempt) % len(self.urls)
            url = self.urls[idx]
            try:
                out = post_json(url, payload, timeout=self.timeout)
                if "error" in out:
                    raise RuntimeError(f"{method} failed on {url}: {out['error']}")
                self.url_index = idx
                return out.get("result")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"{method} failed on all RPCs: {last_error}")

    def block_number(self) -> int:
        return int(self.rpc("eth_blockNumber", []), 16)

    def gas_price(self) -> int:
        return int(self.rpc("eth_gasPrice", []), 16)

    def call(self, to: str, data: str, block: str = "latest") -> str:
        return self.rpc("eth_call", [{"to": clean_addr(to), "data": data}, block])

    def logs(self, address: str, from_block: int, to_block: int, topics: list[Any] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "address": clean_addr(address),
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if topics:
            params["topics"] = topics
        return self.rpc("eth_getLogs", [params])


class UniswapV3Quoter:
    # Uniswap V3 Base deployments:
    # https://developers.uniswap.org/contracts/v3/reference/deployments/base-deployments
    BASE_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    QUOTE_EXACT_INPUT_SINGLE = "c6a5026a"  # quoteExactInputSingle((address,address,uint256,uint24,uint160))

    def __init__(self, rpc: EvmRpc, quoter: str = BASE_QUOTER_V2):
        self.rpc = rpc
        self.quoter = clean_addr(quoter)

    def quote_exact_input_single(self, token_in: str, token_out: str, amount_in: int, fee: int) -> int | None:
        if amount_in <= 0:
            return None
        data = (
            "0x"
            + self.QUOTE_EXACT_INPUT_SINGLE
            + abi_addr(token_in)
            + abi_addr(token_out)
            + abi_word(amount_in)
            + abi_word(fee)
            + abi_word(0)
        )
        try:
            out = self.rpc.call(self.quoter, data)
            if not out or out == "0x":
                return None
            return int_from_hex_word(out, 0)
        except Exception:
            return None

    def best_quote(self, token_in: str, token_out: str, amount_in: int, fee_tiers: Iterable[int]) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        for fee in fee_tiers:
            out = self.quote_exact_input_single(token_in, token_out, amount_in, fee)
            if out is None:
                continue
            if best is None or out > best[0]:
                best = (out, fee)
        return best


class CompoundV3BaseScanner:
    COMPOUND_REPO = "https://raw.githubusercontent.com/compound-finance/comet/main"
    MARKET_PATH = "deployments/base/usdc"

    # Function selectors.
    GET_RESERVES = "0902f1ac"
    TARGET_RESERVES = "32176c49"
    GET_COLLATERAL_RESERVES = "9ff567f8"
    QUOTE_COLLATERAL = "7ac88ed1"
    IS_LIQUIDATABLE = "042e02cf"
    BORROW_BALANCE_OF = "374c49b4"
    COLLATERAL_BALANCE_OF = "5c2549ee"

    # Event topics from Compound III Comet.
    TOPICS = {
        "Supply": "0xd1cf3d156d5f8f0d50f6c122ed609cec09d35c9b9fb3fff6ea0959134dae424e",
        "Withdraw": "0x9b1bfa7fa9ee420a16e124f794c35ac9f90472acc99140eb2f6447c714cad8eb",
        "SupplyCollateral": "0xfa56f7b24f17183d81894d3ac2ee654e3c26388d17a28dbd9549b8114304e1f4",
        "WithdrawCollateral": "0xd6d480d5b3068db003533b170d67561494d72e3bf9fa40a266471351ebba9e16",
        "Transfer": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    }

    def __init__(
        self,
        rpc: EvmRpc,
        capital_usd: float,
        min_net_usd: float,
        safety_usd: float,
        lookback_blocks: int,
        log_chunk_blocks: int,
        gas_units: int,
        scan_accounts: bool,
        max_accounts: int,
        account_time_budget_sec: float,
    ):
        self.rpc = rpc
        self.capital_usd = capital_usd
        self.min_net_usd = min_net_usd
        self.safety_usd = safety_usd
        self.lookback_blocks = lookback_blocks
        self.log_chunk_blocks = log_chunk_blocks
        self.gas_units = gas_units
        self.scan_accounts = scan_accounts
        self.max_accounts = max_accounts
        self.account_time_budget_sec = account_time_budget_sec
        self.roots, self.config = self._load_market_config()
        self.comet = clean_addr(self.roots["comet"])
        self.base = self.config["baseToken"]
        self.base_addr = clean_addr(self.config["baseTokenAddress"])
        self.base_decimals = 6
        self.assets = self._assets()
        self.quoter = UniswapV3Quoter(rpc)

    def _cached_get(self, name: str, url: str) -> Any:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / name
        if path.exists() and time.time() - path.stat().st_mtime < 24 * 3600:
            return json.loads(path.read_text(encoding="utf-8"))
        payload = get_json(url)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def _load_market_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        root_url = f"{self.COMPOUND_REPO}/{self.MARKET_PATH}/roots.json"
        config_url = f"{self.COMPOUND_REPO}/{self.MARKET_PATH}/configuration.json"
        roots = self._cached_get("compound_base_usdc_roots.json", root_url)
        config = self._cached_get("compound_base_usdc_configuration.json", config_url)
        return roots, config

    def _assets(self) -> list[dict[str, Any]]:
        out = []
        for symbol, cfg in self.config.get("assets", {}).items():
            out.append(
                {
                    "symbol": symbol,
                    "address": clean_addr(cfg["address"]),
                    "decimals": int(str(cfg.get("decimals", "18")).replace("_", "")),
                    "liquidation_factor": safe_float(cfg.get("liquidationFactor"), 0.0),
                }
            )
        return out

    def _call_uint(self, selector: str, args: str = "") -> int:
        return int_from_hex_word(self.rpc.call(self.comet, "0x" + selector + args))

    def _get_reserves(self) -> int:
        return self._call_uint(self.GET_RESERVES)

    def _target_reserves(self) -> int:
        return self._call_uint(self.TARGET_RESERVES)

    def _collateral_reserves(self, asset: str) -> int:
        return self._call_uint(self.GET_COLLATERAL_RESERVES, abi_addr(asset))

    def _quote_collateral(self, asset: str, base_amount: int) -> int:
        return self._call_uint(self.QUOTE_COLLATERAL, abi_addr(asset) + abi_word(base_amount))

    def _is_liquidatable(self, account: str) -> bool:
        out = self.rpc.call(self.comet, "0x" + self.IS_LIQUIDATABLE + abi_addr(account))
        return bool_from_hex(out)

    def _borrow_balance(self, account: str) -> int:
        return self._call_uint(self.BORROW_BALANCE_OF, abi_addr(account))

    def _collateral_balance(self, account: str, asset: str) -> int:
        return self._call_uint(self.COLLATERAL_BALANCE_OF, abi_addr(account) + abi_addr(asset))

    def _eth_usdc(self) -> float:
        weth = "0x4200000000000000000000000000000000000006"
        quote = self.quoter.best_quote(weth, self.base_addr, 10**15, [100, 500, 3000, 10000])
        if not quote:
            return 0.0
        return human(quote[0], self.base_decimals) * 1000.0

    def _gas_cost_usd(self) -> float:
        eth_usdc = self._eth_usdc()
        if eth_usdc <= 0:
            return 0.0
        gas_eth = self.rpc.gas_price() * self.gas_units / 1e18
        return gas_eth * eth_usdc

    def _base_ticket_sizes(self) -> list[float]:
        max_ticket = max(1.0, min(self.capital_usd * 0.80, self.capital_usd - 1.0))
        candidates = [2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
        sizes = [x for x in candidates if x <= max_ticket]
        if max_ticket not in sizes:
            sizes.append(round(max_ticket, 2))
        return sorted(set(x for x in sizes if x > 0))

    def scan_collateral_sales(self) -> list[Opportunity]:
        out: list[Opportunity] = []
        reserves = self._get_reserves()
        target = self._target_reserves()
        sale_active = reserves < target
        gas_cost = self._gas_cost_usd()
        reserves_usdc = human(reserves, self.base_decimals)
        target_usdc = human(target, self.base_decimals)

        if not sale_active:
            out.append(
                Opportunity(
                    module="compound",
                    venue="base:cUSDCv3",
                    kind="discount_sale",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason=f"reserves >= target ({reserves_usdc:.2f}/{target_usdc:.2f} USDC)",
                    details={"reserves_usdc": reserves_usdc, "target_reserves_usdc": target_usdc},
                )
            )
            return out

        for asset in self.assets:
            asset_reserves = self._collateral_reserves(asset["address"])
            asset_reserves_h = human(asset_reserves, asset["decimals"])
            if asset_reserves <= 0:
                out.append(
                    Opportunity(
                        module="compound",
                        venue="base:cUSDCv3",
                        kind="discount_sale",
                        symbol=asset["symbol"],
                        gross_usd=0.0,
                        cost_usd=0.0,
                        net_usd=0.0,
                        executable=False,
                        reason="no collateral reserves available",
                        details={"asset_reserves": asset_reserves_h, "sale_active": sale_active},
                    )
                )
                continue

            best: Opportunity | None = None
            for ticket in self._base_ticket_sizes():
                base_in = base_units(ticket, self.base_decimals)
                collateral_out = self._quote_collateral(asset["address"], base_in)
                if collateral_out <= 0:
                    continue
                if collateral_out > asset_reserves:
                    continue
                dex_quote = self.quoter.best_quote(
                    asset["address"],
                    self.base_addr,
                    collateral_out,
                    [100, 500, 3000, 10000],
                )
                if not dex_quote:
                    continue
                dex_out, fee_tier = dex_quote
                exit_usdc = human(dex_out, self.base_decimals)
                gross = exit_usdc - ticket
                total_cost = gas_cost + self.safety_usd
                net = gross - total_cost
                opp = Opportunity(
                    module="compound",
                    venue="base:cUSDCv3",
                    kind="discount_sale",
                    symbol=f"{asset['symbol']}/{self.base}",
                    gross_usd=gross,
                    cost_usd=total_cost,
                    net_usd=net,
                    executable=net >= self.min_net_usd,
                    reason="buyCollateral quote vs UniswapV3 exit",
                    details={
                        "ticket_usdc": ticket,
                        "compound_collateral_out": human(collateral_out, asset["decimals"]),
                        "asset_reserves": asset_reserves_h,
                        "uniswap_fee_tier": fee_tier,
                        "exit_usdc": exit_usdc,
                        "base_reserves_usdc": reserves_usdc,
                        "target_reserves_usdc": target_usdc,
                        "gas_cost_usd": gas_cost,
                        "safety_usd": self.safety_usd,
                    },
                )
                if best is None or opp.net_usd > best.net_usd:
                    best = opp
            if best:
                out.append(best)
            else:
                out.append(
                    Opportunity(
                        module="compound",
                        venue="base:cUSDCv3",
                        kind="discount_sale",
                        symbol=f"{asset['symbol']}/{self.base}",
                        gross_usd=0.0,
                        cost_usd=gas_cost + self.safety_usd,
                        net_usd=-(gas_cost + self.safety_usd),
                        executable=False,
                        reason="collateral reserve too small or no Uniswap quote",
                        details={
                            "asset_reserves": asset_reserves_h,
                            "sale_active": sale_active,
                            "base_reserves_usdc": reserves_usdc,
                            "target_reserves_usdc": target_usdc,
                        },
                    )
                )
        return out

    def _recent_candidate_accounts(self) -> set[str]:
        latest = self.rpc.block_number()
        start = max(0, latest - self.lookback_blocks)
        topics = [[v for v in self.TOPICS.values()]]
        accounts: set[str] = set()
        chunk = max(100, self.log_chunk_blocks)
        for frm in range(start, latest + 1, chunk):
            to = min(latest, frm + chunk - 1)
            try:
                logs = self.rpc.logs(self.comet, frm, to, topics)
            except Exception as exc:
                log_line(f"compound log scan skipped block {frm}-{to}: {exc}")
                continue
            for item in logs:
                for topic in item.get("topics", [])[1:]:
                    addr = topic_to_addr(topic)
                    if addr and addr != self.comet and addr != self.base_addr:
                        accounts.add(addr)
        return accounts

    def scan_accounts_for_liquidations(self) -> list[Opportunity]:
        if not self.scan_accounts:
            return []
        out: list[Opportunity] = []
        accounts = self._recent_candidate_accounts()
        checked = 0
        liquidatable = 0
        deadline = time.time() + max(1.0, self.account_time_budget_sec)
        for account in sorted(accounts):
            if checked >= self.max_accounts or time.time() >= deadline:
                break
            checked += 1
            try:
                if not self._is_liquidatable(account):
                    continue
                liquidatable += 1
                borrow = human(self._borrow_balance(account), self.base_decimals)
                details: dict[str, Any] = {
                    "account": account,
                    "borrow_usdc": borrow,
                    "collateral": {},
                }
                gross_collateral_usdc = 0.0
                for asset in self.assets:
                    bal = self._collateral_balance(account, asset["address"])
                    if bal <= 0:
                        continue
                    quote = self.quoter.best_quote(asset["address"], self.base_addr, bal, [100, 500, 3000, 10000])
                    bal_h = human(bal, asset["decimals"])
                    val = human(quote[0], self.base_decimals) if quote else 0.0
                    gross_collateral_usdc += val
                    details["collateral"][asset["symbol"]] = {"amount": bal_h, "dex_usdc_quote": val}
                out.append(
                    Opportunity(
                        module="compound",
                        venue="base:cUSDCv3",
                        kind="account_liq",
                        symbol=self.base,
                        gross_usd=max(0.0, gross_collateral_usdc - borrow),
                        cost_usd=0.0,
                        net_usd=0.0,
                        executable=False,
                        reason="account is liquidatable; absorb reward is not immediate cash edge",
                        details=details,
                    )
                )
            except Exception as exc:
                out.append(
                    Opportunity(
                        module="compound",
                        venue="base:cUSDCv3",
                        kind="account_liq",
                        symbol=self.base,
                        gross_usd=0.0,
                        cost_usd=0.0,
                        net_usd=0.0,
                        executable=False,
                        reason=f"account check failed: {exc}",
                        details={"account": account},
                    )
                )
        out.append(
            Opportunity(
                module="compound",
                venue="base:cUSDCv3",
                kind="account_scan",
                symbol=self.base,
                gross_usd=0.0,
                cost_usd=0.0,
                net_usd=0.0,
                executable=False,
                reason=f"checked={checked}/{len(accounts)} liquidatable={liquidatable}",
                details={
                    "checked_accounts": checked,
                    "candidate_accounts": len(accounts),
                    "liquidatable_accounts": liquidatable,
                    "max_accounts": self.max_accounts,
                    "time_budget_sec": self.account_time_budget_sec,
                },
            )
        )
        return out

    def scan(self) -> list[Opportunity]:
        return self.scan_collateral_sales() + self.scan_accounts_for_liquidations()


class BinanceSpotTriangularScanner:
    def __init__(
        self,
        capital_usd: float,
        fee_rate: float,
        min_net_usd: float,
        top_candidates: int,
        base_url: str = "https://api.binance.com",
    ):
        self.capital_usd = capital_usd
        self.fee_rate = fee_rate
        self.min_net_usd = min_net_usd
        self.top_candidates = top_candidates
        self.base_url = base_url.rstrip("/")

    def _public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return get_json(self.base_url + path, params=params, timeout=15)

    def _metadata(self) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[float, float]]]:
        ex = self._public("/api/v3/exchangeInfo")
        tickers = self._public("/api/v3/ticker/bookTicker")
        meta: dict[str, tuple[str, str]] = {}
        for s in ex.get("symbols", []):
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed"):
                meta[s["symbol"]] = (s["baseAsset"], s["quoteAsset"])
        books: dict[str, tuple[float, float]] = {}
        for row in tickers:
            sym = row.get("symbol")
            bid = safe_float(row.get("bidPrice"))
            ask = safe_float(row.get("askPrice"))
            if sym in meta and bid > 0 and ask > bid:
                books[sym] = (bid, ask)
        return meta, books

    def _edges(self, meta: dict[str, tuple[str, str]], books: dict[str, tuple[float, float]]) -> dict[str, list[tuple[str, float, str, str]]]:
        graph: dict[str, list[tuple[str, float, str, str]]] = {}
        for sym, (base, quote) in meta.items():
            if sym not in books:
                continue
            bid, ask = books[sym]
            graph.setdefault(base, []).append((quote, bid * (1 - self.fee_rate), sym, "SELL"))
            graph.setdefault(quote, []).append((base, (1 / ask) * (1 - self.fee_rate), sym, "BUY"))
        return graph

    def _depth(self, symbol: str) -> dict[str, Any]:
        return self._public("/api/v3/depth", {"symbol": symbol, "limit": 100})

    def _simulate(self, start_amount: float, legs: list[tuple[str, str]]) -> tuple[float, list[dict[str, Any]], str]:
        amount = start_amount
        detail: list[dict[str, Any]] = []
        for sym, side in legs:
            depth = self._depth(sym)
            if side == "BUY":
                remaining_quote = amount
                got_base = 0.0
                spent = 0.0
                for price_s, qty_s in depth.get("asks", []):
                    price = float(price_s)
                    qty = float(qty_s)
                    take = min(qty, remaining_quote / price)
                    got_base += take * (1 - self.fee_rate)
                    spent += take * price
                    remaining_quote -= take * price
                    if remaining_quote <= 1e-10:
                        break
                if remaining_quote > max(0.01, start_amount * 0.001):
                    return amount, detail, f"insufficient ask depth on {sym}"
                detail.append({"symbol": sym, "side": side, "spent": spent, "got": got_base})
                amount = got_base
            else:
                remaining_base = amount
                got_quote = 0.0
                sold = 0.0
                for price_s, qty_s in depth.get("bids", []):
                    price = float(price_s)
                    qty = float(qty_s)
                    take = min(qty, remaining_base)
                    got_quote += take * price * (1 - self.fee_rate)
                    sold += take
                    remaining_base -= take
                    if remaining_base <= 1e-10:
                        break
                if remaining_base > max(1e-9, amount * 0.001):
                    return amount, detail, f"insufficient bid depth on {sym}"
                detail.append({"symbol": sym, "side": side, "sold": sold, "got": got_quote})
                amount = got_quote
        return amount, detail, "depth-simulated"

    def scan(self) -> list[Opportunity]:
        try:
            meta, books = self._metadata()
        except Exception as exc:
            return [
                Opportunity(
                    module="triangular",
                    venue="binance_spot",
                    kind="read",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason=f"Binance spot read failed: {exc}",
                )
            ]
        graph = self._edges(meta, books)
        starts = ["USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB", "SOL"]
        rough: list[tuple[float, str, list[tuple[str, str]]]] = []
        for start in starts:
            for a, r1, s1, d1 in graph.get(start, []):
                for b, r2, s2, d2 in graph.get(a, []):
                    if b == start:
                        continue
                    for c, r3, s3, d3 in graph.get(b, []):
                        if c != start or len({s1, s2, s3}) < 3:
                            continue
                        ret = r1 * r2 * r3 - 1
                        if ret > -0.002:
                            rough.append((ret, start, [(s1, d1), (s2, d2), (s3, d3)]))
        rough.sort(reverse=True, key=lambda x: x[0])
        out: list[Opportunity] = []
        for ret, start, legs in rough[: self.top_candidates]:
            start_amount = self.capital_usd if start in {"USDT", "USDC", "FDUSD"} else min(25.0, self.capital_usd)
            final, detail, reason = self._simulate(start_amount, legs)
            net = final - start_amount
            out.append(
                Opportunity(
                    module="triangular",
                    venue="binance_spot",
                    kind="3hop_depth",
                    symbol=start,
                    gross_usd=net,
                    cost_usd=0.0,
                    net_usd=net,
                    executable=net >= self.min_net_usd,
                    reason=reason,
                    details={
                        "start_amount": start_amount,
                        "rough_top_book_return_pct": ret * 100,
                        "legs": [{"symbol": s, "side": side} for s, side in legs],
                        "depth_steps": detail,
                    },
                )
            )
        if not out:
            out.append(
                Opportunity(
                    module="triangular",
                    venue="binance_spot",
                    kind="3hop_depth",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason="no top-book candidate survived rough filter",
                )
            )
        return out


class BinanceCarryScanner:
    def __init__(
        self,
        capital_usd: float,
        spot_taker_fee: float,
        futures_taker_fee: float,
        min_net_usd: float,
        base_url_spot: str = "https://api.binance.com",
        base_url_futures: str = "https://fapi.binance.com",
    ):
        self.capital_usd = capital_usd
        self.spot_taker_fee = spot_taker_fee
        self.futures_taker_fee = futures_taker_fee
        self.min_net_usd = min_net_usd
        self.base_url_spot = base_url_spot.rstrip("/")
        self.base_url_futures = base_url_futures.rstrip("/")

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return get_json(base + path, params=params, timeout=15)

    def scan(self) -> list[Opportunity]:
        try:
            premium = self._get(self.base_url_futures, "/fapi/v1/premiumIndex")
            fut_books_raw = self._get(self.base_url_futures, "/fapi/v1/ticker/bookTicker")
            spot_ex = self._get(self.base_url_spot, "/api/v3/exchangeInfo")
            spot_books_raw = self._get(self.base_url_spot, "/api/v3/ticker/bookTicker")
        except Exception as exc:
            return [
                Opportunity(
                    module="carry",
                    venue="binance",
                    kind="spot_perp",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason=f"Binance carry read failed: {exc}",
                )
            ]
        spot_symbols = {
            s["symbol"]
            for s in spot_ex.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
        }
        spot_books = {
            r["symbol"]: (float(r["bidPrice"]), float(r["askPrice"]))
            for r in spot_books_raw
            if r.get("symbol") in spot_symbols and safe_float(r.get("bidPrice")) > 0 and safe_float(r.get("askPrice")) > 0
        }
        fut_books = {
            r["symbol"]: (float(r["bidPrice"]), float(r["askPrice"]))
            for r in fut_books_raw
            if safe_float(r.get("bidPrice")) > 0 and safe_float(r.get("askPrice")) > 0
        }
        now_ms = int(time.time() * 1000)
        rows: list[Opportunity] = []
        for p in premium:
            sym = p.get("symbol")
            if not sym or not sym.endswith("USDT") or sym not in spot_books or sym not in fut_books:
                continue
            funding = safe_float(p.get("lastFundingRate"))
            if funding <= 0:
                continue
            spot_bid, spot_ask = spot_books[sym]
            fut_bid, fut_ask = fut_books[sym]
            if spot_bid <= 0 or spot_ask <= 0 or fut_bid <= 0 or fut_ask <= 0:
                continue
            notional = self.capital_usd
            entry_spread_cost = (spot_ask - spot_bid) / ((spot_ask + spot_bid) / 2) * notional / 2
            fut_spread_cost = (fut_ask - fut_bid) / ((fut_ask + fut_bid) / 2) * notional / 2
            fee_cost = notional * (self.spot_taker_fee + self.futures_taker_fee)
            # One funding event, short perp / long spot. This is an estimate, not atomic profit.
            gross = notional * funding
            cost = entry_spread_cost + fut_spread_cost + fee_cost
            net = gross - cost
            next_hours = max(0.0, (int(p.get("nextFundingTime", now_ms)) - now_ms) / 3_600_000)
            rows.append(
                Opportunity(
                    module="carry",
                    venue="binance",
                    kind="spot_perp",
                    symbol=sym,
                    gross_usd=gross,
                    cost_usd=cost,
                    net_usd=net,
                    executable=net >= self.min_net_usd,
                    reason="positive funding short-perp/long-spot estimate",
                    details={
                        "funding_rate_pct": funding * 100,
                        "next_funding_hours": next_hours,
                        "spot_bid": spot_bid,
                        "spot_ask": spot_ask,
                        "futures_bid": fut_bid,
                        "futures_ask": fut_ask,
                        "notional_usd": notional,
                        "warning": "not atomic; funding can change and both legs must fill",
                    },
                )
            )
        rows.sort(reverse=True, key=lambda x: x.net_usd)
        return rows[:15] or [
            Opportunity(
                module="carry",
                venue="binance",
                kind="spot_perp",
                symbol="ALL",
                gross_usd=0.0,
                cost_usd=0.0,
                net_usd=0.0,
                executable=False,
                reason="no positive-funding spot/perp overlap survived cost filter",
            )
        ]


class PolymarketCompleteSetScanner:
    def __init__(
        self,
        capital_usd: float,
        taker_fee_rate: float,
        min_net_usd: float,
        market_limit: int,
        base_url: str = "https://clob.polymarket.com",
    ):
        self.capital_usd = capital_usd
        self.taker_fee_rate = taker_fee_rate
        self.min_net_usd = min_net_usd
        self.market_limit = market_limit
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return get_json(self.base_url + path, params=params, timeout=15)

    def _best_ask(self, token_id: str) -> tuple[float, float] | None:
        book = self._get("/book", {"token_id": token_id})
        asks = []
        for row in book.get("asks", []):
            price = safe_float(row.get("price"))
            size = safe_float(row.get("size"))
            if price > 0 and size > 0:
                asks.append((price, size))
        if not asks:
            return None
        return min(asks, key=lambda x: x[0])

    def scan(self) -> list[Opportunity]:
        try:
            markets = self._get("/sampling-markets").get("data", [])
        except Exception as exc:
            return [
                Opportunity(
                    module="polymarket",
                    venue="clob",
                    kind="complete_set",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason=f"Polymarket read failed: {exc}",
                )
            ]

        out: list[Opportunity] = []
        checked = 0
        for market in markets:
            if checked >= self.market_limit:
                break
            if not market.get("active") or market.get("closed") or not market.get("accepting_orders"):
                continue
            tokens = market.get("tokens") or []
            if len(tokens) != 2:
                continue
            checked += 1
            try:
                a = self._best_ask(str(tokens[0]["token_id"]))
                b = self._best_ask(str(tokens[1]["token_id"]))
            except Exception:
                continue
            if not a or not b:
                continue
            px_a, sz_a = a
            px_b, sz_b = b
            cost_per_set = px_a + px_b
            if cost_per_set <= 0:
                continue
            shares = min(sz_a, sz_b, self.capital_usd / cost_per_set)
            min_order_size = safe_float(market.get("minimum_order_size"), 5.0)
            spend_a = shares * px_a
            spend_b = shares * px_b
            if spend_a < min_order_size or spend_b < min_order_size:
                continue
            gross = shares * (1.0 - cost_per_set)
            fee = shares * cost_per_set * self.taker_fee_rate
            net = gross - fee
            if net > -0.25:
                out.append(
                    Opportunity(
                        module="polymarket",
                        venue="clob",
                        kind="complete_set",
                        symbol="YES+NO",
                        gross_usd=gross,
                        cost_usd=fee,
                        net_usd=net,
                        executable=net >= self.min_net_usd,
                        reason="buy both binary outcomes below payout; both legs must fill",
                        details={
                            "question": market.get("question"),
                            "market_slug": market.get("market_slug"),
                            "condition_id": market.get("condition_id"),
                            "outcomes": [tokens[0].get("outcome"), tokens[1].get("outcome")],
                            "ask_prices": [px_a, px_b],
                            "ask_sizes": [sz_a, sz_b],
                            "cost_per_set": cost_per_set,
                            "shares": shares,
                            "minimum_order_size": min_order_size,
                            "taker_fee_rate": self.taker_fee_rate,
                            "warning": "not atomic; live execution needs FAK/IOC handling and jurisdiction check",
                        },
                    )
                )
        out.sort(reverse=True, key=lambda x: x.net_usd)
        return out[:15] or [
            Opportunity(
                module="polymarket",
                venue="clob",
                kind="complete_set",
                symbol="ALL",
                gross_usd=0.0,
                cost_usd=0.0,
                net_usd=0.0,
                executable=False,
                reason=f"checked={checked} no YES+NO below payout after fee",
            )
        ]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only forced-edge scanner. Sends no orders.")
    p.add_argument("--modules", default="compound,triangular,carry,polymarket", help="comma list: compound,triangular,carry,polymarket")
    p.add_argument("--capital-usd", type=float, default=float(os.getenv("FORCED_EDGE_CAPITAL_USD", "28")))
    p.add_argument("--min-net-usd", type=float, default=float(os.getenv("FORCED_EDGE_MIN_NET_USD", "0.03")))
    p.add_argument("--safety-usd", type=float, default=float(os.getenv("FORCED_EDGE_SAFETY_USD", "0.05")))
    p.add_argument(
        "--base-rpc",
        default=os.getenv(
            "BASE_RPC_URL",
            "https://base-rpc.publicnode.com,https://base.llamarpc.com,https://mainnet.base.org,https://1rpc.io/base",
        ),
        help="Base RPC URL or comma-separated fallback list",
    )
    p.add_argument("--compound-lookback-blocks", type=int, default=int(os.getenv("COMPOUND_LOOKBACK_BLOCKS", "1800")))
    p.add_argument("--compound-log-chunk-blocks", type=int, default=int(os.getenv("COMPOUND_LOG_CHUNK_BLOCKS", "600")))
    p.add_argument("--compound-scan-accounts", action="store_true", help="also scan recent Compound accounts for liquidatable state")
    p.add_argument("--compound-max-accounts", type=int, default=int(os.getenv("COMPOUND_MAX_ACCOUNTS", "60")))
    p.add_argument("--compound-account-time-budget-sec", type=float, default=float(os.getenv("COMPOUND_ACCOUNT_TIME_BUDGET_SEC", "25")))
    p.add_argument("--gas-units", type=int, default=int(os.getenv("FORCED_EDGE_BASE_GAS_UNITS", "500000")))
    p.add_argument("--binance-spot-fee", type=float, default=float(os.getenv("BINANCE_SPOT_TAKER_FEE", "0.001")))
    p.add_argument("--binance-futures-fee", type=float, default=float(os.getenv("BINANCE_FUTURES_TAKER_FEE", "0.0005")))
    p.add_argument("--tri-top-candidates", type=int, default=int(os.getenv("TRIANGULAR_TOP_CANDIDATES", "8")))
    p.add_argument("--polymarket-limit", type=int, default=int(os.getenv("POLYMARKET_MARKET_LIMIT", "40")))
    p.add_argument("--polymarket-taker-fee", type=float, default=float(os.getenv("POLYMARKET_TAKER_FEE", "0.001")))
    p.add_argument("--jsonl", default=str(LOG_DIR / "forced_edge_events.jsonl"))
    p.add_argument("--once", action="store_true", help="run one scan cycle and exit")
    p.add_argument("--poll-seconds", type=float, default=float(os.getenv("FORCED_EDGE_POLL_SECONDS", "30")))
    return p.parse_args()


def run_once(args: argparse.Namespace, writer: JsonlWriter) -> list[Opportunity]:
    modules = {m.strip().lower() for m in args.modules.split(",") if m.strip()}
    opportunities: list[Opportunity] = []

    if "compound" in modules:
        try:
            rpc = EvmRpc(args.base_rpc)
            opportunities.extend(
                CompoundV3BaseScanner(
                    rpc=rpc,
                    capital_usd=args.capital_usd,
                    min_net_usd=args.min_net_usd,
                    safety_usd=args.safety_usd,
                    lookback_blocks=args.compound_lookback_blocks,
                    log_chunk_blocks=args.compound_log_chunk_blocks,
                    gas_units=args.gas_units,
                    scan_accounts=args.compound_scan_accounts,
                    max_accounts=args.compound_max_accounts,
                    account_time_budget_sec=args.compound_account_time_budget_sec,
                ).scan()
            )
        except Exception as exc:
            opportunities.append(
                Opportunity(
                    module="compound",
                    venue="base:cUSDCv3",
                    kind="scan",
                    symbol="ALL",
                    gross_usd=0.0,
                    cost_usd=0.0,
                    net_usd=0.0,
                    executable=False,
                    reason=f"Compound scan failed: {exc}",
                )
            )

    if "triangular" in modules or "tri" in modules:
        opportunities.extend(
            BinanceSpotTriangularScanner(
                capital_usd=args.capital_usd,
                fee_rate=args.binance_spot_fee,
                min_net_usd=args.min_net_usd,
                top_candidates=args.tri_top_candidates,
            ).scan()
        )

    if "carry" in modules:
        opportunities.extend(
            BinanceCarryScanner(
                capital_usd=args.capital_usd,
                spot_taker_fee=args.binance_spot_fee,
                futures_taker_fee=args.binance_futures_fee,
                min_net_usd=args.min_net_usd,
            ).scan()
        )

    if "polymarket" in modules or "poly" in modules:
        opportunities.extend(
            PolymarketCompleteSetScanner(
                capital_usd=args.capital_usd,
                taker_fee_rate=args.polymarket_taker_fee,
                min_net_usd=args.min_net_usd,
                market_limit=args.polymarket_limit,
            ).scan()
        )

    opportunities.sort(reverse=True, key=lambda o: o.net_usd)
    cycle = {
        "ts": utc_now(),
        "capital_usd": args.capital_usd,
        "min_net_usd": args.min_net_usd,
        "opportunities": [asdict(o) for o in opportunities],
    }
    writer.write(cycle)
    return opportunities


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    writer = JsonlWriter(Path(args.jsonl))

    log_line(
        "forced-edge scanner starting "
        f"modules={args.modules} capital=${args.capital_usd:.2f} min_net=${args.min_net_usd:.2f} "
        "mode=READ_ONLY"
    )
    cycle = 0
    while True:
        cycle += 1
        try:
            opportunities = run_once(args, writer)
            edges = [o for o in opportunities if o.executable and o.net_usd >= args.min_net_usd]
            log_line(f"cycle={cycle} total={len(opportunities)} executable_edges={len(edges)}")
            for opp in opportunities[:20]:
                log_line(opp.short())
        except KeyboardInterrupt:
            log_line("stopped by user")
            return 0
        except Exception as exc:
            log_line(f"cycle failed: {type(exc).__name__}: {exc}")
        if args.once:
            return 0
        time.sleep(max(1.0, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
