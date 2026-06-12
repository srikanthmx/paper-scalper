from __future__ import annotations

import pytest

from paper_scalper.config import Settings
from paper_scalper.engine.keltner import KeltnerReversionStrategy
from tests.test_strategy import candle


def strategy(**param_overrides) -> KeltnerReversionStrategy:
    s = KeltnerReversionStrategy(Settings())
    s.apply_params({"min_atr_pct": 0.0001, "max_atr_pct": 50.0, **param_overrides})
    return s


def test_fades_long_below_lower_band_with_oversold_rsi() -> None:
    s = strategy()
    ts, px = 1_700_000_000.0, 100.0
    for i in range(25):  # flat tape to settle the channel
        s.on_candle(candle(ts + i * 60, px, px + (0.02 if i % 2 == 0 else -0.02)), None)
    signal = None
    for i in range(25, 40):  # waterfall: RSI craters, price exits the lower band
        signal = signal or s.on_candle(candle(ts + i * 60, px, px - 0.35), None)
        px -= 0.35
    assert signal is not None and signal.side == "long"
    assert "keltner fade" in signal.reason
    assert "midline" in signal.reason
    assert signal.tp_pct >= s.p["tp_min_pct"]


def test_quiet_inside_channel() -> None:
    s = strategy()
    ts, px = 1_700_000_000.0, 100.0
    for i in range(30):
        s.on_candle(candle(ts + i * 60, px, px + (0.02 if i % 2 == 0 else -0.02)), None)
    assert any("inside channel" in r for r in s.snapshot.rejects)


def test_rsi_gate_blocks_band_break_without_extreme() -> None:
    # price breaks the band but RSI gate is impossible to satisfy -> no signal
    s = strategy(rsi_low=1.0, rsi_high=99.0)
    ts, px = 1_700_000_000.0, 100.0
    for i in range(25):
        s.on_candle(candle(ts + i * 60, px, px + (0.02 if i % 2 == 0 else -0.02)), None)
    fired = False
    for i in range(25, 40):
        fired = fired or s.on_candle(candle(ts + i * 60, px, px - 0.35), None) is not None
        px -= 0.35
    assert fired is False
