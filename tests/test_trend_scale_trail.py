from __future__ import annotations

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.paper_broker import PaperBroker
from paper_scalper.engine.strategy import Signal
from paper_scalper.engine.trend import TrendScalpStrategy
from tests.test_strategy import candle


def cfg(**overrides) -> Settings:
    base = dict(fee_bps=0.0, slippage_bps=0.0, max_hold_seconds=300,
                trend_rr=2.0, trend_scale_out_frac=0.5)
    base.update(overrides)
    return Settings(**base)


def quote(ts: float, bid: float, ask: float) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def scale_signal(side: str, sl_pct: float = 1.0) -> Signal:
    return Signal(side=side, ts=0, ref_price=100, sl_pct=sl_pct, tp_pct=2 * sl_pct,
                  reason="test", mode="scale_trail", scale_out_frac=0.5,
                  max_hold_seconds=3600)


def test_scale_trail_long_full_lifecycle() -> None:
    """Entry 100, 1R=1: half off at 102 (+2R), SL jumps to 101 (+1R), trail by 1R."""
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.9, 100.0), qty=2.0)
    pos = broker.position
    assert (pos.sl_price, pos.tp_price) == (pytest.approx(99.0), pytest.approx(102.0))

    assert broker.on_quote(quote(1, 101.5, 101.6)) is None     # below TP1
    partial = broker.on_quote(quote(2, 102.0, 102.1))          # TP1 touched
    assert partial is not None and partial.reason_exit == "partial_tp"
    assert partial.qty == pytest.approx(1.0)
    assert partial.gross_pnl == pytest.approx(2.0)
    assert broker.position is not None and broker.position.qty == pytest.approx(1.0)
    assert broker.position.sl_price == pytest.approx(101.0)    # SL at +1R, not -1R

    assert broker.on_quote(quote(3, 104.0, 104.1)) is None     # runner runs; trail -> 103
    assert broker.position.sl_price == pytest.approx(103.0)
    final = broker.on_quote(quote(4, 103.0, 103.1))            # trail touched
    assert final is not None and final.reason_exit == "trailing_stop"
    assert final.qty == pytest.approx(1.0)
    assert final.gross_pnl == pytest.approx(3.0)               # locked 3R on the runner
    assert broker.position is None


def test_scale_trail_stop_at_plus_1r_is_a_winner() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.9, 100.0), qty=2.0)
    broker.on_quote(quote(1, 102.0, 102.1))                    # partial at +2R
    final = broker.on_quote(quote(2, 101.0, 101.1))            # straight back to +1R
    assert final is not None and final.reason_exit == "trailing_stop"
    assert final.gross_pnl == pytest.approx(1.0)               # still green


def test_scale_trail_short_mirrors() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("short"), quote(0, 100.0, 100.1), qty=2.0)
    partial = broker.on_quote(quote(1, 97.9, 98.0))            # +2R for a short
    assert partial is not None and partial.reason_exit == "partial_tp"
    assert broker.position.sl_price == pytest.approx(99.0)     # +1R lock
    assert broker.on_quote(quote(2, 96.9, 97.0)) is None       # trail -> 98
    assert broker.position.sl_price == pytest.approx(98.0)
    final = broker.on_quote(quote(3, 97.95, 98.05))
    assert final is not None and final.reason_exit == "trailing_stop"


def test_scale_trail_initial_stop_loses_1r() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.9, 100.0), qty=2.0)
    closed = broker.on_quote(quote(1, 99.0, 99.1))
    assert closed is not None and closed.reason_exit == "stop_loss"
    assert closed.gross_pnl == pytest.approx(-2.0)             # full 2 qty x 1R


def test_trend_strategy_emits_scale_trail_signal() -> None:
    settings = Settings(ema_sep_min_bps=1.0, min_atr_pct=0.01, max_atr_pct=5.0,
                        trend_rr=2.0)
    strategy = TrendScalpStrategy(settings)
    ts, px = 1_700_000_000.0, 100.0
    signal = None
    for i in range(40):  # steady uptrend
        signal = strategy.on_candle(candle(ts + i * 60, px, px + 0.15), None)
        px += 0.15
    assert signal is not None and signal.side == "long"
    assert signal.mode == "scale_trail"
    assert signal.tp_pct == pytest.approx(2 * signal.sl_pct)   # strict 1:2
