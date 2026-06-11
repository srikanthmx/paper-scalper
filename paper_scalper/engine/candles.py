from __future__ import annotations

from dataclasses import dataclass

from paper_scalper.data.normalizer import Trade


@dataclass(slots=True)
class Candle:
    ts_open: float  # bucket start, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    notional: float  # sum(price * size), for VWAP

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


class CandleBuilder:
    """Buckets trades into fixed-interval candles. Emits a candle when a trade
    arrives in a later bucket (gaps simply produce no candles — no synthetic fills)."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self._current: Candle | None = None

    def on_trade(self, trade: Trade) -> Candle | None:
        bucket = trade.ts - (trade.ts % self.seconds)
        completed: Candle | None = None
        cur = self._current
        if cur is None or bucket > cur.ts_open:
            completed = cur
            self._current = Candle(
                ts_open=bucket, open=trade.price, high=trade.price, low=trade.price,
                close=trade.price, volume=trade.size, notional=trade.price * trade.size,
            )
        elif bucket == cur.ts_open:
            cur.high = max(cur.high, trade.price)
            cur.low = min(cur.low, trade.price)
            cur.close = trade.price
            cur.volume += trade.size
            cur.notional += trade.price * trade.size
        # trades older than the current bucket are dropped (out-of-order tick)
        return completed

    def flush(self) -> Candle | None:
        cur, self._current = self._current, None
        return cur
