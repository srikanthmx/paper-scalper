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
                trend_rr=2.0, trend_scale_out_frac=0.5, use_lots=False)
    base.update(overrides)
    return Settings(**base)


def quote(ts: float, bid: float, ask: float) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def scale_signal(side: str, sl_pct: float = 1.0) -> Signal:
    return Signal(side=side, ts=0, ref_price=100, sl_pct=sl_pct, tp_pct=2 * sl_pct,
                  reason="test", mode="scale_trail", scale_out_frac=0.5,
                  max_hold_seconds=3600)


def test_scale_trail_ladder_full_lifecycle() -> None:
    """mid_ref 100, 1R=1, rr=2: +2R closes half & SL->entry & TP->+4R;
    +4R closes half of the rest & SL->+2R & TP->+6R; drop to +2R exits the rest."""
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.95, 100.05), qty=2.0)
    pos = broker.position
    assert (pos.sl_price, pos.tp_price) == (pytest.approx(99.0), pytest.approx(102.0))

    assert broker.on_quote(quote(1, 101.5, 101.6)) is None     # mid 101.55 < TP1
    rung1 = broker.on_quote(quote(2, 102.0, 102.1))            # mid 102.05 >= 102
    assert rung1 is not None and rung1.reason_exit == "partial_tp"
    assert rung1.qty == pytest.approx(1.0)
    assert rung1.gross_pnl == pytest.approx(102.0 - 100.05)
    pos = broker.position
    assert pos is not None and pos.qty == pytest.approx(1.0)
    assert pos.sl_price == pytest.approx(100.0)   # SL = entry ("same price as bought")
    assert pos.tp_price == pytest.approx(104.0)   # TP moved 2R further

    rung2 = broker.on_quote(quote(3, 104.0, 104.1))            # mid 104.05 >= 104
    assert rung2 is not None and rung2.reason_exit == "partial_tp"
    assert rung2.qty == pytest.approx(0.5)
    pos = broker.position
    assert pos.qty == pytest.approx(0.5)
    assert pos.sl_price == pytest.approx(102.0)   # SL trails to previous TP
    assert pos.tp_price == pytest.approx(106.0)

    final = broker.on_quote(quote(4, 101.95, 102.05))          # mid 102 hits the trail
    assert final is not None and final.reason_exit == "trailing_stop"
    assert final.gross_pnl == pytest.approx((101.95 - 100.05) * 0.5)  # ~+2R locked
    assert broker.position is None


def test_scale_trail_breakeven_after_first_rung() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.95, 100.05), qty=2.0)
    broker.on_quote(quote(1, 102.0, 102.1))                    # rung 1: SL -> entry
    final = broker.on_quote(quote(2, 99.95, 100.05))           # mid 100 = entry
    assert final is not None and final.reason_exit == "trailing_stop"
    # banked +2R on half; remainder exits flat minus the spread — net still green
    assert final.gross_pnl == pytest.approx(99.95 - 100.05)


def test_scale_trail_short_mirrors() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("short"), quote(0, 99.95, 100.05), qty=2.0)
    partial = broker.on_quote(quote(1, 97.9, 98.0))            # mid 97.95 <= TP 98
    assert partial is not None and partial.reason_exit == "partial_tp"
    pos = broker.position
    assert pos.sl_price == pytest.approx(100.0)   # breakeven (entry mid)
    assert pos.tp_price == pytest.approx(96.0)    # 2R further down
    rung2 = broker.on_quote(quote(2, 95.9, 96.0))              # mid 95.95 <= 96
    assert rung2 is not None and rung2.reason_exit == "partial_tp"
    assert broker.position.sl_price == pytest.approx(98.0)     # trail to previous TP
    final = broker.on_quote(quote(3, 97.95, 98.05))            # mid 98 hits the trail
    assert final is not None and final.reason_exit == "trailing_stop"


def test_scale_trail_initial_stop_loses_1r() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(scale_signal("long"), quote(0, 99.95, 100.05), qty=2.0)
    closed = broker.on_quote(quote(1, 98.95, 99.05))           # mid 99 = SL
    assert closed is not None and closed.reason_exit == "stop_loss"
    assert closed.gross_pnl == pytest.approx((98.95 - 100.05) * 2)


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


def test_trend_chase_guard_skips_blowoff_candles() -> None:
    settings = Settings(ema_sep_min_bps=1.0, min_atr_pct=0.01, max_atr_pct=50.0)
    strategy = TrendScalpStrategy(settings)
    ts, px = 1_700_000_000.0, 100.0
    for i in range(40):  # steady uptrend establishes a small ATR
        strategy.on_candle(candle(ts + i * 60, px, px + 0.15), None)
        px += 0.15
    blowoff = candle(ts + 40 * 60, px, px + 3.0)  # ~20x the recent ATR
    assert strategy.on_candle(blowoff, None) is None
    assert any("chase guard" in r for r in strategy.snapshot.rejects)
