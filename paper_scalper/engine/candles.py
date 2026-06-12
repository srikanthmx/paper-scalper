from __future__ import annotations

from dataclasses import dataclass

from paper_scalper.data.normalizer import Quote, Trade


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
    """Buckets price events into fixed-interval candles.

    Both trades and quote mids advance OHLC and close buckets — on thin venues
    (Alpaca BTC/USD) trades can be minutes apart, and indicators need a steady
    candle clock. Volume comes from trades only; quotes contribute zero volume.
    """

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self._current: Candle | None = None

    def on_trade(self, trade: Trade) -> Candle | None:
        return self._on_price(trade.ts, trade.price, trade.size)

    def on_quote(self, quote: Quote) -> Candle | None:
        return self._on_price(quote.ts, quote.mid, 0.0)

    def _on_price(self, ts: float, price: float, size: float) -> Candle | None:
        bucket = ts - (ts % self.seconds)
        completed: Candle | None = None
        cur = self._current
        if cur is None or bucket > cur.ts_open:
            completed = cur
            self._current = Candle(
                ts_open=bucket, open=price, high=price, low=price,
                close=price, volume=size, notional=price * size,
            )
        elif bucket == cur.ts_open:
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.volume += size
            cur.notional += price * size
        # events older than the current bucket are dropped (out-of-order tick)
        return completed

    @property
    def current(self) -> Candle | None:
        """The forming (not yet closed) candle, if any."""
        return self._current

    def flush(self) -> Candle | None:
        cur, self._current = self._current, None
        return cur
