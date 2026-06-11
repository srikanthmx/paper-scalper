"""Keyless feeds for testing the full pipeline without Alpaca credentials.

SyntheticFeed: geometric random-walk BTC ticks with regimes (trend / chop), so the
strategy actually fires. Useful for smoke-testing the simulator end to end.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator

from paper_scalper.data.normalizer import MarketEvent, Quote, Trade


class SyntheticFeed:
    def __init__(
        self,
        symbol: str = "BTC/USD",
        start_price: float = 65_000.0,
        ticks_per_second: float = 4.0,
        seed: int | None = None,
        max_ticks: int | None = None,
        realtime: bool = True,
    ) -> None:
        self.symbol = symbol
        self.price = start_price
        self.tps = ticks_per_second
        self.rng = random.Random(seed)
        self.max_ticks = max_ticks
        self.realtime = realtime
        self._drift = 0.0
        self._regime_left = 0
        self._activity = 1.0

    def _step(self) -> None:
        if self._regime_left <= 0:
            # new regime every ~2-6 minutes of ticks: trending or chopping
            self._regime_left = self.rng.randint(int(120 * self.tps), int(360 * self.tps))
            self._drift = self.rng.choice([-1.0, -0.5, 0.0, 0.0, 0.5, 1.0]) * 2e-6
            # volume clusters with trends (real markets do this; i.i.d. sizes never
            # produce a candle-level volume spike, so the strategy would sit idle)
            self._activity = 1.0 if self._drift == 0.0 else self.rng.uniform(1.5, 3.5)
        self._regime_left -= 1
        vol = 6e-5 * (1.0 + 0.5 * (self._activity - 1.0))
        self.price *= 1.0 + self._drift + self.rng.gauss(0.0, vol)

    async def stream(self) -> AsyncIterator[MarketEvent]:
        count = 0
        ts = time.time() if self.realtime else 1_700_000_000.0
        interval = 1.0 / self.tps
        while self.max_ticks is None or count < self.max_ticks:
            self._step()
            ts = time.time() if self.realtime else ts + interval
            spread = self.price * self.rng.uniform(0.5e-4, 1.5e-4)
            yield Quote(ts=ts, symbol=self.symbol, bid=self.price - spread / 2,
                        ask=self.price + spread / 2, bid_size=1.0, ask_size=1.0)
            size = self.rng.expovariate(1 / 0.05) * self._activity
            yield Trade(ts=ts, symbol=self.symbol, price=self.price, size=size)
            count += 1
            if self.realtime:
                await asyncio.sleep(interval)
