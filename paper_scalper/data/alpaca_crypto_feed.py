"""Alpaca crypto market-data websocket adapter.

DATA ONLY. This module may connect to exactly one host: stream.data.alpaca.markets.
It has no knowledge of any trading/order endpoint, and tests/test_no_real_orders.py
enforces that for the whole codebase.
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

ALLOWED_HOST = "stream.data.alpaca.markets"
WS_URL = f"wss://{ALLOWED_HOST}/v1beta3/crypto/us"


def _parse_ts(raw: str) -> float:
    # Alpaca timestamps: RFC-3339 with nanoseconds, e.g. 2026-06-10T12:00:00.123456789Z
    head, _, frac = raw.rstrip("Z").partition(".")
    base = datetime.fromisoformat(head + "+00:00").timestamp()
    if frac:
        base += float("0." + frac[:6])
    return base


class AlpacaCryptoFeed:
    def __init__(self, api_key: str, api_secret: str, symbols: list[str]) -> None:
        assert ALLOWED_HOST in WS_URL  # paranoia: never rewire this adapter to a trading host
        self._key = api_key
        self._secret = api_secret
        self._symbols = symbols

    async def stream(self) -> AsyncIterator[MarketEvent]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    await self._handshake(ws)
                    backoff = 1.0
                    async for raw in ws:
                        for event in self._parse(raw):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
                log.warning("feed disconnected (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handshake(self, ws: websockets.ClientConnection) -> None:
        await ws.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        await ws.send(
            json.dumps({"action": "subscribe", "trades": self._symbols, "quotes": self._symbols})
        )
        log.info("subscribed to %s on %s", self._symbols, ALLOWED_HOST)

    def _parse(self, raw: str | bytes) -> list[MarketEvent]:
        events: list[MarketEvent] = []
        for msg in json.loads(raw):
            kind = msg.get("T")
            if kind == "t":
                events.append(
                    Trade(ts=_parse_ts(msg["t"]), symbol=msg["S"], price=float(msg["p"]),
                          size=float(msg["s"]))
                )
            elif kind == "q":
                events.append(
                    Quote(ts=_parse_ts(msg["t"]), symbol=msg["S"], bid=float(msg["bp"]),
                          ask=float(msg["ap"]), bid_size=float(msg["bs"]),
                          ask_size=float(msg["as"]))
                )
            elif kind == "error":
                log.error("alpaca stream error: %s", msg)
            elif kind in ("success", "subscription"):
                log.info("alpaca: %s", msg)
        return events
