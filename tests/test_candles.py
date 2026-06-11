from __future__ import annotations

import pytest

from paper_scalper.data.normalizer import Trade
from paper_scalper.engine.candles import CandleBuilder


def trade(ts: float, price: float, size: float = 1.0) -> Trade:
    return Trade(ts=ts, symbol="BTC/USD", price=price, size=size)


def test_buckets_and_ohlcv() -> None:
    builder = CandleBuilder(60)
    assert builder.on_trade(trade(0, 100)) is None
    assert builder.on_trade(trade(10, 105, 2)) is None
    assert builder.on_trade(trade(59, 95)) is None
    completed = builder.on_trade(trade(60, 99))  # next bucket closes the first
    assert completed is not None
    assert (completed.open, completed.high, completed.low, completed.close) == (100, 105, 95, 95)
    assert completed.volume == 4.0
    assert completed.notional == pytest.approx(100 + 105 * 2 + 95)


def test_gap_emits_only_real_candles() -> None:
    builder = CandleBuilder(60)
    builder.on_trade(trade(0, 100))
    completed = builder.on_trade(trade(300, 110))  # 4-minute gap
    assert completed is not None and completed.ts_open == 0
    assert builder.flush().ts_open == 300


def test_quotes_advance_and_close_buckets_without_volume() -> None:
    from paper_scalper.data.normalizer import Quote

    def quote(ts: float, mid: float) -> Quote:
        return Quote(ts=ts, symbol="BTC/USD", bid=mid - 0.5, ask=mid + 0.5,
                     bid_size=1, ask_size=1)

    builder = CandleBuilder(60)
    builder.on_trade(trade(0, 100, 2.0))
    assert builder.on_quote(quote(30, 106)) is None   # extends high, no volume
    completed = builder.on_quote(quote(61, 99))       # next bucket closes on a quote
    assert completed is not None
    assert (completed.high, completed.close, completed.volume) == (106, 106, 2.0)
    assert builder.flush().volume == 0.0              # quote-only candle has zero volume


def test_out_of_order_tick_dropped() -> None:
    builder = CandleBuilder(60)
    builder.on_trade(trade(120, 100))
    assert builder.on_trade(trade(50, 999)) is None
    assert builder.flush().high == 100
