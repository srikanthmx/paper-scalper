"""Lot model: fixed lots in, scaled out in whole lots with a traceable lifecycle."""

from __future__ import annotations

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.paper_broker import PaperBroker
from paper_scalper.engine.strategy import Signal


def cfg(**o) -> Settings:
    base = dict(fee_bps=0.0, slippage_bps=0.0, use_lots=True, lots_per_entry=4,
                lot_size_usd=250.0, scale_lots="2,1")
    base.update(o)
    return Settings(**base)


def q(ts: float, bid: float, ask: float) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def ladder_signal(side: str = "long") -> Signal:
    return Signal(side=side, ts=0, ref_price=100, sl_pct=1.0, tp_pct=2.0, reason="t",
                  mode="scale_trail", scale_out_frac=0.5, breakeven_after_r=999.0)


def test_entry_is_fixed_lots_not_random_qty() -> None:
    broker = PaperBroker(cfg())
    pos = broker.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=12345)  # qty ignored
    assert pos.lots_total == 4
    assert pos.lots_remaining == 4
    assert pos.lot_qty == pytest.approx(250.0 / pos.entry_price)
    assert pos.qty == pytest.approx(4 * pos.lot_qty)
    assert len(pos.position_id) == 8


def test_scale_out_in_whole_lots_with_lifecycle() -> None:
    broker = PaperBroker(cfg())
    pos = broker.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=0)
    pid = pos.position_id

    # TP1 at +2R (mid 102): cut 2 lots, SL -> entry
    f1 = broker.on_quote(q(2, 102.0, 102.1))
    assert f1.position_id == pid and f1.lots == 2 and f1.lots_left == 2
    assert "TP1" in f1.event and "cut 2 lots" in f1.event
    assert f1.sl_after == pytest.approx(100.0)            # SL moved to entry
    assert f1.net_pnl > 0

    # TP2 at +4R (mid 104): cut 1 lot, SL -> +1R rung
    f2 = broker.on_quote(q(3, 104.0, 104.1))
    assert f2.lots == 1 and f2.lots_left == 1
    assert "TP2" in f2.event
    assert f2.sl_after == pytest.approx(102.0)

    # last lot trails; a drop to the trail (mid 102) closes the final lot
    assert broker.position is not None and broker.position.lots_remaining == 1
    f3 = broker.on_quote(q(4, 101.95, 102.05))
    assert f3 is not None and f3.lots == 1 and f3.lots_left == 0
    assert f3.reason_exit == "trailing_stop" and f3.position_id == pid
    assert broker.position is None

    # all three fills share one position_id and cut 4 lots total
    assert f1.lots + f2.lots + f3.lots == 4


def test_full_stop_closes_all_lots_at_once() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=0)
    closed = broker.on_quote(q(1, 98.95, 99.05))         # mid 99 hits initial SL
    assert closed.reason_exit == "stop_loss"
    assert closed.lots == 4 and closed.lots_left == 0     # whole position out
    assert broker.position is None


def test_schedule_exhausted_runner_trails_without_cutting() -> None:
    broker = PaperBroker(cfg(scale_lots="2"))            # cut 2 at TP1, then 2 lots trail
    broker.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=0)
    f1 = broker.on_quote(q(1, 102.0, 102.1))             # TP1: cut 2
    assert f1.lots == 2 and broker.position.lots_remaining == 2
    # next TP rung: schedule exhausted -> no cut, just trail (returns None)
    assert broker.on_quote(q(2, 104.0, 104.1)) is None
    assert broker.position is not None and broker.position.lots_remaining == 2
