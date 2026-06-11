"""Coinbase Exchange public market-data websocket adapter (keyless).

DATA ONLY. This module may connect to exactly one host: ws-feed.exchange.coinbase.com —
the public feed, which carries no order capability. tests/test_no_real_orders.py
enforces that no trading host appears anywhere in the codebase.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime

import websockets

from paper_scalper.data.normalizer import MarketEvent, Quote, Trade

log = logging.getLogger(__name__)

ALLOWED_HOST = "ws-feed.exchange.coinbase.com"
WS_URL = f"wss://{ALLOWED_HOST}"


def _parse_ts(raw: str) -> float:
    head, _, frac = raw.rstrip("Z").partition(".")
    base = datetime.fromisoformat(head + "+00:00").timestamp()
    if frac:
        base += float("0." + frac[:6])
    return base


def _product(symbol: str) -> str:
    return symbol.replace("/", "-")  # BTC/USD → BTC-USD


class CoinbaseFeed:
    def __init__(self, symbols: list[str]) -> None:
        assert ALLOWED_HOST in WS_URL  # paranoia: never rewire this adapter
        self._products = {_product(s): s for s in symbols}

    async def stream(self) -> AsyncIterator[MarketEvent]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": list(self._products),
                        "channels": ["matches", "ticker"],
                    }))
                    log.info("subscribed to %s on %s", list(self._products), ALLOWED_HOST)
                    backoff = 1.0
                    async for raw in ws:
                        event = self._parse(raw)
                        if event is not None:
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
                log.warning("feed disconnected (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _parse(self, raw: str | bytes) -> MarketEvent | None:
        msg = json.loads(raw)
        kind = msg.get("type")
        symbol = self._products.get(msg.get("product_id", ""))
        if symbol is None:
            if kind == "error":
                log.error("coinbase stream error: %s", msg)
            elif kind == "subscriptions":
                log.info("coinbase: subscription confirmed")
            return None
        if kind in ("match", "last_match"):
            return Trade(ts=_parse_ts(msg["time"]), symbol=symbol,
                         price=float(msg["price"]), size=float(msg["size"]))
        if kind == "ticker" and msg.get("best_bid") and msg.get("best_ask"):
            return Quote(ts=_parse_ts(msg["time"]), symbol=symbol,
                         bid=float(msg["best_bid"]), ask=float(msg["best_ask"]),
                         bid_size=float(msg.get("best_bid_size", 0) or 0),
                         ask_size=float(msg.get("best_ask_size", 0) or 0))
        return None
