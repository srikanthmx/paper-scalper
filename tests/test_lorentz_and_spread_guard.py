from __future__ import annotations

import math

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.lorentz import (K_NEIGHBORS, LorentzianStrategy,
                                          lorentzian_distance)
from paper_scalper.engine.strategy import Signal
from paper_scalper.main import stop_inside_spread
from tests.test_strategy import candle


def test_lorentzian_distance_dampens_outliers() -> None:
    base = (0.5, 0.5, 0.5, 0.5)
    near = (0.6, 0.5, 0.5, 0.5)
    outlier = (5.0, 0.5, 0.5, 0.5)
    assert lorentzian_distance(base, near) == pytest.approx(math.log1p(0.1))
    # log dampening: a 45x larger gap costs far less than 45x the distance
    assert lorentzian_distance(base, outlier) < 45 * lorentzian_distance(base, near)


def test_lorentz_learns_then_predicts_long_in_uptrend() -> None:
    strategy = LorentzianStrategy(Settings())
    strategy.apply_params({"min_samples": 40, "pred_threshold": 5,
                           "min_atr_pct": 0.0001, "max_atr_pct": 50.0})
    ts, px = 1_700_000_000.0, 100.0
    signal = None
    for i in range(200):  # persistent uptrend: all labels +1, neighbors all vote long
        sig = strategy.on_candle(candle(ts + i * 60, px, px + 0.1), None)
        signal = signal or sig
        px += 0.1
    assert len(strategy._history) > 40
    assert all(label == 1 for _, label in strategy._history)
    assert signal is not None and signal.side == "long"
    assert f"+{K_NEIGHBORS}" in signal.reason  # unanimous neighbor vote


def test_lorentz_stays_quiet_while_learning() -> None:
    strategy = LorentzianStrategy(Settings())
    ts, px = 1_700_000_000.0, 100.0
    for i in range(60):  # well past indicator warm-up, below min_samples=100
        assert strategy.on_candle(candle(ts + i * 60, px, px + 0.1), None) is None
        px += 0.1
    assert any("learning" in r for r in strategy.snapshot.rejects)


def quote(bid: float, ask: float) -> Quote:
    return Quote(ts=0, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def signal(sl_pct: float) -> Signal:
    return Signal(side="long", ts=0, ref_price=100.0, sl_pct=sl_pct, tp_pct=2 * sl_pct,
                  reason="t")


def test_stop_inside_spread_blocks_doa_entries() -> None:
    """Regression: a ~19bps spread with a ~11bps stop = instant stop-out on entry."""
    cfg = Settings(slippage_bps=3.0)
    wide = quote(63540.0, 63660.0)  # ~19bps spread, as seen live during the spike
    assert stop_inside_spread(signal(sl_pct=0.11), wide, cfg) is True
    assert stop_inside_spread(signal(sl_pct=0.50), wide, cfg) is False
    tight = quote(63599.0, 63601.0)  # ~0.3bps spread
    assert stop_inside_spread(signal(sl_pct=0.11), tight, cfg) is False
