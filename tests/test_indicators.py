from __future__ import annotations

import pytest

from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA, RSI, RollingMean, SessionVWAP


def make_candle(ts: float, o: float, h: float, low: float, c: float, v: float = 1.0) -> Candle:
    typical = (h + low + c) / 3
    return Candle(ts_open=ts, open=o, high=h, low=low, close=c, volume=v, notional=typical * v)


def test_ema_seeds_with_sma_then_smooths() -> None:
    ema = EMA(3)
    assert ema.update(1) is None
    assert ema.update(2) is None
    assert ema.update(3) == pytest.approx(2.0)  # SMA seed
    # alpha = 0.5 → 0.5*4 + 0.5*2 = 3.0
    assert ema.update(4) == pytest.approx(3.0)


def test_rsi_all_gains_is_100() -> None:
    rsi = RSI(5)
    value = None
    for px in range(1, 10):
        value = rsi.update(float(px))
    assert value == pytest.approx(100.0)


def test_rsi_known_sequence_is_bounded_and_directional() -> None:
    rsi = RSI(14)
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28]
    value = None
    for c in closes:
        value = rsi.update(c)
    assert value is not None
    assert 60 < value < 80  # classic Wilder example lands ≈ 70


def test_atr_simple() -> None:
    atr = ATR(2)
    atr.update(make_candle(0, 10, 11, 9, 10))   # TR = 2 (no prev close)
    v = atr.update(make_candle(60, 10, 12, 10, 11))  # TR = max(2, 2, 0) = 2
    assert v == pytest.approx(2.0)


def test_session_vwap_resets_on_new_utc_day() -> None:
    vwap = SessionVWAP()
    day1 = 1_700_006_400  # within some UTC day
    vwap.update(make_candle(day1, 100, 100, 100, 100, v=2.0))
    assert vwap.value == pytest.approx(100.0)
    vwap.update(make_candle(day1 + 60, 200, 200, 200, 200, v=2.0))
    assert vwap.value == pytest.approx(150.0)
    vwap.update(make_candle(day1 + 86_400, 50, 50, 50, 50, v=1.0))
    assert vwap.value == pytest.approx(50.0)  # reset


def test_rolling_mean() -> None:
    m = RollingMean(3)
    assert m.update(1) is None
    assert m.update(2) is None
    assert m.update(3) == pytest.approx(2.0)
    assert m.update(6) == pytest.approx(11 / 3)
