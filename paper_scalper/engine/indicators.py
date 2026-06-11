"""Incremental indicators. Each consumes completed candles; values are None until warm."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from paper_scalper.engine.candles import Candle


class EMA:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._seed.append(price)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
            return self.value
        self.value = self.alpha * price + (1 - self.alpha) * self.value
        return self.value


class RSI:
    """Wilder's RSI."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._count = 0
        self._gain_sum = 0.0
        self._loss_sum = 0.0

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if self._avg_gain is None:
            self._count += 1
            self._gain_sum += gain
            self._loss_sum += loss
            if self._count == self.period:
                self._avg_gain = self._gain_sum / self.period
                self._avg_loss = self._loss_sum / self.period
            else:
                return None
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's ATR on candles."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] = []

    def update(self, candle: Candle) -> float | None:
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._prev_close = candle.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class SessionVWAP:
    """VWAP anchored to UTC midnight (crypto session). Resets when the UTC day changes."""

    def __init__(self) -> None:
        self.value: float | None = None
        self._pv = 0.0
        self._vol = 0.0
        self._day: int | None = None

    def update(self, candle: Candle) -> float | None:
        day = datetime.fromtimestamp(candle.ts_open, tz=timezone.utc).toordinal()
        if day != self._day:
            self._day = day
            self._pv = 0.0
            self._vol = 0.0
        self._pv += candle.notional if candle.notional > 0 else candle.typical * candle.volume
        self._vol += candle.volume
        if self._vol > 0:
            self.value = self._pv / self._vol
        return self.value


class RollingMean:
    def __init__(self, period: int) -> None:
        self.period = period
        self._window: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        self._window.append(x)
        if len(self._window) == self.period:
            self.value = sum(self._window) / self.period
        return self.value
