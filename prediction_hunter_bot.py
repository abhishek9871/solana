"""Prediction-market hunter bot.

Primary goal:
  Use small capital to pursue Polymarket reward/rebate opportunities without
  taking blind directional bets.

Default mode is DRY RUN. Live mode requires:
  POLYMARKET_DRY_RUN=0
  POLYMARKET_PRIVATE_KEY=<wallet private key>
  POLYMARKET_FUNDER_ADDRESS=<wallet/safe address>

Live orders are post-only GTC maker orders. The bot does not market-buy.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "prediction_hunter_state.json"
EVENT_LOG = LOG_DIR / "prediction_hunter_events.jsonl"

POLY_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
BINANCE_SPOT = "https://api.binance.com"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{utc_now()}] {safe}", flush=True)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def round_tick(value: float, tick: float, mode: str = "nearest") -> float:
    if tick <= 0:
        return value
    if mode == "down":
        q = math.floor((value + 1e-12) / tick) * tick
    elif mode == "up":
        q = math.ceil((value - 1e-12) / tick) * tick
    else:
        q = round(value / tick) * tick
    decimals = max(0, min(6, int(round(-math.log10(tick))) if tick < 1 else 0))
    return round(q, decimals)


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 12) -> Any:
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


@dataclass
class Book:
    token_id: str
    outcome: str
    best_bid: float | None
    best_ask: float | None
    bid_size_top5: float
    ask_size_top5: float
    tick_size: float
    min_order_size: float
    raw_hash: str = ""

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    end_date: str
    tags: list[str]
    min_order_size: float
    tick_size: float
    neg_risk: bool
    reward_daily: float
    reward_min_size: float
    reward_max_spread_cents: float
    competitiveness: float
    tokens: list[dict[str, Any]]
    books: list[Book] = field(default_factory=list)

    @property
    def is_binary(self) -> bool:
        return len(self.tokens) == 2


@dataclass
class PlannedOrder:
    strategy: str
    market_slug: str
    condition_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size: float
    reserve_usd: float
    reason: str

    @property
    def key(self) -> str:
        return f"{self.strategy}:{self.condition_id}:{self.token_id}:{self.side}"


@dataclass
class CandidatePlan:
    strategy: str
    market: Market
    orders: list[PlannedOrder]
    score: float
    max_reserved: float
    complete_set_edge: float
    risk_note: str

    def short(self) -> str:
        orders = " ".join(f"{o.outcome}:{o.side}@{o.price:.4f}x{o.size:g}" for o in self.orders)
        return (
            f"{self.strategy:<14} score={self.score:8.3f} reward=${self.market.reward_daily:g}/d "
            f"reserve=${self.max_reserved:.2f} set_edge=${self.complete_set_edge:+.3f} "
            f"{self.market.question[:72]} | {orders}"
        )


@dataclass
class ManagedOrder:
    order_id: str
    plan_key: str
    strategy: str
    condition_id: str
    token_id: str
    outcome: str
    price: float
    size: float
    ts: float
    dry_run: bool


class PolymarketPublicClient:
    def __init__(self, host: str = POLY_HOST, gamma_host: str = GAMMA_HOST):
        self.host = host.rstrip("/")
        self.gamma_host = gamma_host.rstrip("/")
        self.session = requests.Session()

    def sampling_markets(self) -> list[dict[str, Any]]:
        return self._get(f"{self.host}/sampling-markets").get("data", [])

    def markets_page(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"next_cursor": cursor} if cursor else {}
        return self._get(f"{self.host}/markets", params=params)

    def gamma_markets(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._get(
            f"{self.gamma_host}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
        )

    def reward_market(self, condition_id: str) -> dict[str, Any]:
        return self._get(f"{self.host}/rewards/markets/{condition_id}", params={"sponsored": "true"})

    def order_book(self, token_id: str) -> dict[str, Any]:
        return self._get(f"{self.host}/book", params={"token_id": token_id})

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = self.session.get(url, params=params or {}, timeout=12)
        r.raise_for_status()
        return r.json()


def parse_market(row: dict[str, Any]) -> Market | None:
    tokens = row.get("tokens") or []
    if len(tokens) < 2:
        return None
    rewards = row.get("rewards") or {}
    rates = rewards.get("rates") or []
    daily = sum(safe_float(x.get("rewards_daily_rate")) for x in rates)
    tags = []
    for tag in row.get("tags") or []:
        if isinstance(tag, str):
            tags.append(tag)
        elif isinstance(tag, dict):
            tags.append(str(tag.get("label") or tag.get("name") or tag.get("slug") or ""))
    condition_id = str(row.get("condition_id") or row.get("conditionId") or "")
    if not condition_id:
        return None
    return Market(
        condition_id=condition_id,
        slug=str(row.get("market_slug") or row.get("slug") or ""),
        question=str(row.get("question") or ""),
        end_date=str(row.get("end_date_iso") or row.get("endDate") or ""),
        tags=[t for t in tags if t],
        min_order_size=safe_float(row.get("minimum_order_size"), 5.0),
        tick_size=safe_float(row.get("minimum_tick_size"), 0.01),
        neg_risk=bool(row.get("neg_risk")),
        reward_daily=daily,
        reward_min_size=safe_float(rewards.get("min_size"), 0.0),
        reward_max_spread_cents=safe_float(rewards.get("max_spread"), 0.0),
        competitiveness=safe_float(row.get("market_competitiveness"), 0.0),
        tokens=tokens,
    )


def parse_book(token: dict[str, Any], raw: dict[str, Any], fallback_tick: float, fallback_min_size: float) -> Book:
    bids = []
    asks = []
    for item in raw.get("bids") or []:
        p = safe_float(item.get("price"))
        s = safe_float(item.get("size"))
        if p > 0 and s > 0:
            bids.append((p, s))
    for item in raw.get("asks") or []:
        p = safe_float(item.get("price"))
        s = safe_float(item.get("size"))
        if p > 0 and s > 0:
            asks.append((p, s))
    bids.sort(reverse=True)
    asks.sort()
    tick = safe_float(raw.get("tick_size"), fallback_tick) or fallback_tick
    min_size = safe_float(raw.get("min_order_size"), fallback_min_size) or fallback_min_size
    return Book(
        token_id=str(token.get("token_id") or token.get("tokenId") or token.get("id") or raw.get("asset_id")),
        outcome=str(token.get("outcome") or ""),
        best_bid=bids[0][0] if bids else None,
        best_ask=asks[0][0] if asks else None,
        bid_size_top5=sum(s for _, s in bids[:5]),
        ask_size_top5=sum(s for _, s in asks[:5]),
        tick_size=tick,
        min_order_size=min_size,
        raw_hash=str(raw.get("hash") or ""),
    )


class RewardMarketPlanner:
    def __init__(
        self,
        public: PolymarketPublicClient,
        max_capital: float,
        max_markets: int,
        min_reward_day: float,
        max_min_size: float,
        max_pair_cost: float,
        quote_offset_ticks: int,
        avoid_keywords: list[str],
    ):
        self.public = public
        self.max_capital = max_capital
        self.max_markets = max_markets
        self.min_reward_day = min_reward_day
        self.max_min_size = max_min_size
        self.max_pair_cost = max_pair_cost
        self.quote_offset_ticks = quote_offset_ticks
        self.avoid_keywords = [k.strip().lower() for k in avoid_keywords if k.strip()]

    def plans(self) -> list[CandidatePlan]:
        rows = self.public.sampling_markets()
        markets = []
        for row in rows:
            if not (row.get("active") and not row.get("closed") and row.get("accepting_orders") and row.get("enable_order_book")):
                continue
            market = parse_market(row)
            if not market or not market.is_binary:
                continue
            if market.reward_daily < self.min_reward_day:
                continue
            if not (0 < market.reward_min_size <= self.max_min_size):
                continue
            text = f"{market.question} {market.slug}".lower()
            if any(k in text for k in self.avoid_keywords):
                continue
            markets.append(market)

        markets.sort(key=lambda m: (-m.reward_daily, m.reward_min_size, m.competitiveness))
        out: list[CandidatePlan] = []
        for market in markets[: max(self.max_markets, self.max_markets * 4)]:
            try:
                for token in market.tokens:
                    raw = self.public.order_book(str(token.get("token_id")))
                    market.books.append(parse_book(token, raw, market.tick_size, market.min_order_size))
                plan = self._plan_for_market(market)
                if plan:
                    out.append(plan)
            except Exception as exc:
                append_jsonl(EVENT_LOG, {"ts": utc_now(), "event": "reward_plan_error", "market": market.slug, "error": str(exc)})
        out.sort(key=lambda p: p.score, reverse=True)
        return out[: self.max_markets]

    def _plan_for_market(self, market: Market) -> CandidatePlan | None:
        if len(market.books) != 2:
            return None
        size = max(market.reward_min_size, market.min_order_size)
        if size <= 0 or size > self.max_min_size:
            return None
        orders: list[PlannedOrder] = []
        bid_sum = 0.0
        max_spread = max(0.0, market.reward_max_spread_cents / 100.0)
        for book in market.books:
            if book.best_ask is None:
                return None
            tick = book.tick_size or market.tick_size or 0.01
            if book.best_bid is None:
                mid = book.best_ask - tick
                proposed = max(0.01, mid - max_spread / 2.0)
            else:
                proposed = book.best_bid + (tick * self.quote_offset_ticks)
            # Never cross. Live mode also uses post_only, but price discipline matters.
            proposed = min(proposed, book.best_ask - tick)
            if book.midpoint is not None and max_spread > 0:
                # Keep quote inside reward spread where possible.
                proposed = max(proposed, book.midpoint - max_spread + tick)
            proposed = clamp(round_tick(proposed, tick, "down"), tick, 1.0 - tick)
            if book.best_ask is not None and proposed >= book.best_ask:
                proposed = round_tick(book.best_ask - tick, tick, "down")
            if proposed <= 0:
                return None
            bid_sum += proposed
            orders.append(
                PlannedOrder(
                    strategy="reward",
                    market_slug=market.slug,
                    condition_id=market.condition_id,
                    token_id=book.token_id,
                    outcome=book.outcome or "OUTCOME",
                    side="BUY",
                    price=proposed,
                    size=size,
                    reserve_usd=proposed * size,
                    reason="reward qualifying maker bid",
                )
            )
        if len(orders) != 2:
            return None
        if bid_sum > self.max_pair_cost:
            # Back both bids down evenly so both-fill complete set stays positive.
            excess = bid_sum - self.max_pair_cost
            for o in orders:
                tick = next((b.tick_size for b in market.books if b.token_id == o.token_id), market.tick_size)
                o.price = clamp(round_tick(o.price - excess / 2.0 - tick, tick, "down"), tick, 1.0 - tick)
                o.reserve_usd = o.price * o.size
            bid_sum = sum(o.price for o in orders)
        reserve = sum(o.reserve_usd for o in orders)
        if reserve > self.max_capital:
            return None
        complete_edge = size * (1.0 - bid_sum)
        tightness = 0.0
        for book in market.books:
            if book.spread is not None:
                tightness += max(0.0, 0.10 - book.spread)
        comp_penalty = math.log1p(max(0.0, market.competitiveness)) * 0.5
        score = market.reward_daily * 2.0 + complete_edge * 10.0 + tightness * 20.0 - comp_penalty - reserve * 0.02
        return CandidatePlan(
            strategy="reward",
            market=market,
            orders=orders,
            score=score,
            max_reserved=reserve,
            complete_set_edge=complete_edge,
            risk_note="maker reward farming; one-sided fills create temporary directional exposure",
        )


class CryptoBinaryPlanner:
    def __init__(
        self,
        public: PolymarketPublicClient,
        max_capital: float,
        max_markets: int,
        quote_size: float,
        max_pair_cost: float,
    ):
        self.public = public
        self.max_capital = max_capital
        self.max_markets = max_markets
        self.quote_size = quote_size
        self.max_pair_cost = max_pair_cost

    def plans(self) -> list[CandidatePlan]:
        rows = self.public.sampling_markets()
        markets: list[Market] = []
        for row in rows:
            if not (row.get("active") and not row.get("closed") and row.get("accepting_orders") and row.get("enable_order_book")):
                continue
            market = parse_market(row)
            if not market or not market.is_binary:
                continue
            text = f"{market.question} {market.slug} {' '.join(market.tags)}".lower()
            asset_hit = bool(re.search(r"\b(bitcoin|btc|ethereum|eth|solana|sol)\b", text))
            binary_hit = bool(re.search(r"\b(up|down|above|below|higher|lower)\b", text))
            if not (asset_hit and binary_hit):
                continue
            markets.append(market)
        plans: list[CandidatePlan] = []
        for market in markets[: max(10, self.max_markets * 6)]:
            try:
                for token in market.tokens:
                    raw = self.public.order_book(str(token.get("token_id")))
                    market.books.append(parse_book(token, raw, market.tick_size, market.min_order_size))
                plan = self._plan(market)
                if plan:
                    plans.append(plan)
            except Exception as exc:
                append_jsonl(EVENT_LOG, {"ts": utc_now(), "event": "crypto_plan_error", "market": market.slug, "error": str(exc)})
        plans.sort(key=lambda p: p.score, reverse=True)
        return plans[: self.max_markets]

    def _plan(self, market: Market) -> CandidatePlan | None:
        if len(market.books) != 2:
            return None
        size = max(self.quote_size, market.min_order_size)
        orders: list[PlannedOrder] = []
        bid_sum = 0.0
        for book in market.books:
            if book.best_bid is None or book.best_ask is None:
                return None
            tick = book.tick_size or 0.01
            spread = book.spread or 0.0
            if spread < tick * 2:
                price = book.best_bid
            else:
                price = min(book.best_bid + tick, book.best_ask - tick)
            price = clamp(round_tick(price, tick, "down"), tick, 1.0 - tick)
            bid_sum += price
            orders.append(
                PlannedOrder(
                    strategy="crypto_maker",
                    market_slug=market.slug,
                    condition_id=market.condition_id,
                    token_id=book.token_id,
                    outcome=book.outcome,
                    side="BUY",
                    price=price,
                    size=size,
                    reserve_usd=price * size,
                    reason="crypto binary maker quote",
                )
            )
        if bid_sum > self.max_pair_cost:
            return None
        reserve = sum(o.reserve_usd for o in orders)
        if reserve > self.max_capital:
            return None
        # Prefer markets with tight books and high volume proxy.
        depth = sum(b.bid_size_top5 + b.ask_size_top5 for b in market.books)
        complete_edge = size * (1.0 - bid_sum)
        score = complete_edge * 20.0 + math.log1p(depth) - reserve * 0.05
        return CandidatePlan(
            strategy="crypto_maker",
            market=market,
            orders=orders,
            score=score,
            max_reserved=reserve,
            complete_set_edge=complete_edge,
            risk_note="high-turnover crypto binary; live mode disabled until trigger/feed logic is added",
        )


class EventLagPlanner:
    """Read-only watchlist for markets that could become resolution-lag trades."""

    KEYWORDS = [
        "fed",
        "rate",
        "cpi",
        "unemployment",
        "hurricane",
        "earthquake",
        "election",
        "wins",
        "released",
        "sentenced",
        "charged",
        "ipo",
    ]

    def __init__(self, public: PolymarketPublicClient, max_items: int):
        self.public = public
        self.max_items = max_items

    def plans(self) -> list[CandidatePlan]:
        rows = self.public.sampling_markets()
        candidates: list[tuple[float, Market]] = []
        for row in rows:
            if not (row.get("active") and not row.get("closed") and row.get("accepting_orders") and row.get("enable_order_book")):
                continue
            market = parse_market(row)
            if not market or not market.is_binary:
                continue
            text = f"{market.question} {market.slug}".lower()
            if not any(k in text for k in self.KEYWORDS):
                continue
            rough_score = market.reward_daily + (10.0 if market.reward_min_size and market.reward_min_size <= 28 else 0.0)
            candidates.append((rough_score, market))
        candidates.sort(key=lambda x: x[0], reverse=True)

        out: list[CandidatePlan] = []
        for _rough, market in candidates[: max(self.max_items, self.max_items * 3)]:
            try:
                for token in market.tokens:
                    raw = self.public.order_book(str(token.get("token_id")))
                    market.books.append(parse_book(token, raw, market.tick_size, market.min_order_size))
            except Exception:
                continue
            spread_sum = sum((b.spread or 0.0) for b in market.books)
            reward = market.reward_daily
            score = reward + spread_sum * 10.0
            out.append(
                CandidatePlan(
                    strategy="event_watch",
                    market=market,
                    orders=[],
                    score=score,
                    max_reserved=0.0,
                    complete_set_edge=0.0,
                    risk_note="watchlist only; needs external truth feed before trading",
                )
            )
            if len(out) >= self.max_items:
                break
        out.sort(key=lambda p: p.score, reverse=True)
        return out[: self.max_items]


class BinanceTruthFeed:
    def __init__(self, base_url: str = BINANCE_SPOT):
        self.base_url = base_url.rstrip("/")

    def prices(self) -> dict[str, float]:
        out = {}
        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            try:
                row = get_json(f"{self.base_url}/api/v3/ticker/price", {"symbol": sym}, timeout=5)
                out[sym] = safe_float(row.get("price"))
            except Exception:
                pass
        return out


class Broker:
    def sync_plan(self, plans: list[CandidatePlan]) -> list[ManagedOrder]:
        raise NotImplementedError

    def cancel_all(self) -> None:
        raise NotImplementedError


class DryRunBroker(Broker):
    def __init__(self):
        self.orders: dict[str, ManagedOrder] = {}

    def sync_plan(self, plans: list[CandidatePlan]) -> list[ManagedOrder]:
        desired: dict[str, PlannedOrder] = {}
        for plan in plans:
            for order in plan.orders:
                desired[order.key] = order
        for key in list(self.orders):
            if key not in desired:
                del self.orders[key]
        for key, order in desired.items():
            existing = self.orders.get(key)
            if existing and abs(existing.price - order.price) < 1e-9 and abs(existing.size - order.size) < 1e-9:
                continue
            self.orders[key] = ManagedOrder(
                order_id="dry-" + str(abs(hash((key, order.price, order.size))) % 10_000_000),
                plan_key=key,
                strategy=order.strategy,
                condition_id=order.condition_id,
                token_id=order.token_id,
                outcome=order.outcome,
                price=order.price,
                size=order.size,
                ts=time.time(),
                dry_run=True,
            )
        return list(self.orders.values())

    def cancel_all(self) -> None:
        self.orders.clear()


class LivePolymarketBroker(Broker):
    def __init__(self, host: str, chain_id: int, private_key: str, signature_type: int, funder: str):
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY
        except Exception as exc:
            raise RuntimeError("Install live dependency: py -3 -m pip install py-clob-client-v2") from exc

        api_key = os.getenv("POLYMARKET_API_KEY", "")
        api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        creds = None
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        temp = ClobClient(host=host, chain_id=chain_id, key=private_key, signature_type=signature_type, funder=funder)
        if creds is None:
            creds = temp.create_or_derive_api_key()
        self.client = ClobClient(host=host, chain_id=chain_id, key=private_key, creds=creds, signature_type=signature_type, funder=funder)
        self.OrderArgs = OrderArgs
        self.OrderType = OrderType
        self.PartialCreateOrderOptions = PartialCreateOrderOptions
        self.BUY = BUY
        self.orders: dict[str, ManagedOrder] = {}

    def sync_plan(self, plans: list[CandidatePlan]) -> list[ManagedOrder]:
        desired: dict[str, tuple[PlannedOrder, Market]] = {}
        for plan in plans:
            for order in plan.orders:
                desired[order.key] = (order, plan.market)

        for key, existing in list(self.orders.items()):
            desired_item = desired.get(key)
            if not desired_item:
                self._cancel(existing.order_id)
                del self.orders[key]
                continue
            order, _market = desired_item
            if abs(existing.price - order.price) > 1e-9 or abs(existing.size - order.size) > 1e-9:
                self._cancel(existing.order_id)
                del self.orders[key]

        for key, (order, market) in desired.items():
            if key in self.orders:
                continue
            if order.side != "BUY":
                raise RuntimeError("Live broker currently allows BUY maker orders only")
            opts = self.PartialCreateOrderOptions(tick_size=str(market.tick_size), neg_risk=market.neg_risk)
            resp = self.client.create_and_post_order(
                order_args=self.OrderArgs(
                    token_id=order.token_id,
                    price=order.price,
                    size=order.size,
                    side=self.BUY,
                ),
                options=opts,
                order_type=self.OrderType.GTC,
                post_only=True,
            )
            order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
            if not order_id:
                raise RuntimeError(f"order post returned no id: {resp}")
            self.orders[key] = ManagedOrder(
                order_id=order_id,
                plan_key=key,
                strategy=order.strategy,
                condition_id=order.condition_id,
                token_id=order.token_id,
                outcome=order.outcome,
                price=order.price,
                size=order.size,
                ts=time.time(),
                dry_run=False,
            )
        return list(self.orders.values())

    def _cancel(self, order_id: str) -> None:
        from py_clob_client_v2 import OrderPayload

        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as exc:
            append_jsonl(EVENT_LOG, {"ts": utc_now(), "event": "cancel_failed", "order_id": order_id, "error": str(exc)})

    def cancel_all(self) -> None:
        for order in list(self.orders.values()):
            self._cancel(order.order_id)
        self.orders.clear()


class PredictionHunter:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.public = PolymarketPublicClient(args.polymarket_host, args.gamma_host)
        self.truth = BinanceTruthFeed()
        self.broker = self._build_broker()
        self.cycle = 0

    def _build_broker(self) -> Broker:
        if self.args.dry_run:
            return DryRunBroker()
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
        if not private_key or not funder:
            raise RuntimeError("Live mode requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS in .env")
        return LivePolymarketBroker(
            host=self.args.polymarket_host,
            chain_id=self.args.chain_id,
            private_key=private_key,
            signature_type=self.args.signature_type,
            funder=funder,
        )

    def run_once(self) -> dict[str, Any]:
        self.cycle += 1
        reward_plans = RewardMarketPlanner(
            public=self.public,
            max_capital=self.args.max_capital_usd,
            max_markets=self.args.reward_markets,
            min_reward_day=self.args.min_reward_day,
            max_min_size=self.args.max_min_size,
            max_pair_cost=self.args.max_pair_cost,
            quote_offset_ticks=self.args.quote_offset_ticks,
            avoid_keywords=self.args.avoid_keywords.split(",") if self.args.avoid_keywords else [],
        ).plans()
        crypto_plans = CryptoBinaryPlanner(
            public=self.public,
            max_capital=self.args.crypto_capital_usd,
            max_markets=self.args.crypto_markets,
            quote_size=self.args.crypto_quote_size,
            max_pair_cost=self.args.crypto_max_pair_cost,
        ).plans()
        watch_plans = EventLagPlanner(self.public, max_items=self.args.event_watch_items).plans()

        executable_plans: list[CandidatePlan] = []
        if self.args.enable_reward:
            executable_plans.extend(reward_plans)
        if self.args.enable_crypto_live:
            executable_plans.extend(crypto_plans)

        managed = self.broker.sync_plan(executable_plans)
        truth_prices = self.truth.prices()
        payload = {
            "ts": utc_now(),
            "cycle": self.cycle,
            "dry_run": self.args.dry_run,
            "truth_prices": truth_prices,
            "reward_plans": [self._plan_json(p) for p in reward_plans],
            "crypto_plans": [self._plan_json(p) for p in crypto_plans],
            "event_watch": [self._plan_json(p) for p in watch_plans],
            "managed_orders": [asdict(o) for o in managed],
        }
        append_jsonl(EVENT_LOG, payload)
        save_json_atomic(STATE_FILE, payload)
        self._print_cycle(payload, reward_plans, crypto_plans, watch_plans, managed)
        return payload

    def _plan_json(self, plan: CandidatePlan) -> dict[str, Any]:
        return {
            "strategy": plan.strategy,
            "score": plan.score,
            "max_reserved": plan.max_reserved,
            "complete_set_edge": plan.complete_set_edge,
            "risk_note": plan.risk_note,
            "market": {
                "condition_id": plan.market.condition_id,
                "slug": plan.market.slug,
                "question": plan.market.question,
                "end_date": plan.market.end_date,
                "reward_daily": plan.market.reward_daily,
                "reward_min_size": plan.market.reward_min_size,
                "reward_max_spread_cents": plan.market.reward_max_spread_cents,
                "competitiveness": plan.market.competitiveness,
                "books": [asdict(b) for b in plan.market.books],
            },
            "orders": [asdict(o) for o in plan.orders],
        }

    def _print_cycle(
        self,
        payload: dict[str, Any],
        reward_plans: list[CandidatePlan],
        crypto_plans: list[CandidatePlan],
        watch_plans: list[CandidatePlan],
        managed: list[ManagedOrder],
    ) -> None:
        log(
            f"cycle={self.cycle} dry_run={self.args.dry_run} "
            f"reward={len(reward_plans)} crypto={len(crypto_plans)} watch={len(watch_plans)} managed={len(managed)}"
        )
        if payload.get("truth_prices"):
            px = " ".join(f"{k}={v:.2f}" for k, v in payload["truth_prices"].items() if v)
            log(f"truth {px}")
        for title, plans in [("REWARD", reward_plans[:5]), ("CRYPTO", crypto_plans[:3]), ("WATCH", watch_plans[:3])]:
            for plan in plans:
                log(f"{title} {plan.short()}")
        for order in managed[:10]:
            log(
                f"ORDER {order.strategy:<12} {'DRY' if order.dry_run else 'LIVE'} "
                f"{order.outcome:<8} @{order.price:.4f}x{order.size:g} {order.order_id}"
            )

    def run(self) -> None:
        log(
            f"prediction hunter start dry_run={self.args.dry_run} "
            f"max_capital=${self.args.max_capital_usd:.2f} reward_markets={self.args.reward_markets}"
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log("stopping; cancelling managed orders")
                self.broker.cancel_all()
                raise
            except Exception as exc:
                log(f"cycle error: {type(exc).__name__}: {exc}")
                append_jsonl(EVENT_LOG, {"ts": utc_now(), "event": "cycle_error", "error": str(exc)})
            if self.args.once:
                return
            time.sleep(max(3.0, self.args.poll_seconds))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket prediction-market reward/rebate hunter.")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=float, default=env_float("PREDICTION_HUNTER_POLL_SECONDS", 20.0))
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=env_bool("POLYMARKET_DRY_RUN", True))
    p.add_argument("--polymarket-host", default=os.getenv("POLYMARKET_HOST", POLY_HOST))
    p.add_argument("--gamma-host", default=os.getenv("POLYMARKET_GAMMA_HOST", GAMMA_HOST))
    p.add_argument("--chain-id", type=int, default=env_int("POLYMARKET_CHAIN_ID", 137))
    p.add_argument("--signature-type", type=int, default=env_int("POLYMARKET_SIGNATURE_TYPE", 0))

    p.add_argument("--enable-reward", action=argparse.BooleanOptionalAction, default=env_bool("PREDICTION_ENABLE_REWARD", True))
    p.add_argument("--reward-markets", type=int, default=env_int("PREDICTION_REWARD_MARKETS", 1))
    p.add_argument("--max-capital-usd", type=float, default=env_float("POLYMARKET_MAX_CAPITAL_USD", 20.0))
    p.add_argument("--min-reward-day", type=float, default=env_float("PREDICTION_MIN_REWARD_DAY", 3.0))
    p.add_argument("--max-min-size", type=float, default=env_float("PREDICTION_MAX_MIN_SIZE", 28.0))
    p.add_argument("--max-pair-cost", type=float, default=env_float("PREDICTION_MAX_PAIR_COST", 0.995))
    p.add_argument("--quote-offset-ticks", type=int, default=env_int("PREDICTION_QUOTE_OFFSET_TICKS", 0))
    p.add_argument(
        "--avoid-keywords",
        default=os.getenv(
            "PREDICTION_AVOID_KEYWORDS",
            "war,death,assassination,terror,shooting,invasion,missile",
        ),
    )

    p.add_argument("--enable-crypto-live", action=argparse.BooleanOptionalAction, default=env_bool("PREDICTION_ENABLE_CRYPTO_LIVE", False))
    p.add_argument("--crypto-markets", type=int, default=env_int("PREDICTION_CRYPTO_MARKETS", 2))
    p.add_argument("--crypto-capital-usd", type=float, default=env_float("PREDICTION_CRYPTO_CAPITAL_USD", 8.0))
    p.add_argument("--crypto-quote-size", type=float, default=env_float("PREDICTION_CRYPTO_QUOTE_SIZE", 5.0))
    p.add_argument("--crypto-max-pair-cost", type=float, default=env_float("PREDICTION_CRYPTO_MAX_PAIR_COST", 0.99))

    p.add_argument("--event-watch-items", type=int, default=env_int("PREDICTION_EVENT_WATCH_ITEMS", 5))
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    hunter = PredictionHunter(args)
    hunter.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
