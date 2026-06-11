from __future__ import annotations

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.paper_broker import PaperBroker
from paper_scalper.engine.strategy import Signal


def cfg(**overrides) -> Settings:
    base = dict(fee_bps=25.0, slippage_bps=0.0, max_hold_seconds=300, breakeven_after_r=100.0)
    base.update(overrides)
    return Settings(**base)


def quote(ts: float, bid: float, ask: float) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def signal(side: str, px: float, sl_pct: float = 1.0, tp_pct: float = 2.0) -> Signal:
    return Signal(side=side, ts=0, ref_price=px, sl_pct=sl_pct, tp_pct=tp_pct, reason="test")


def test_long_fills_at_ask_plus_slippage_with_fee() -> None:
    broker = PaperBroker(cfg(slippage_bps=10.0))
    pos = broker.open_position(signal("long", 100), quote(0, 99.0, 100.0), qty=1.0)
    assert pos.entry_price == pytest.approx(100.0 * 1.001)
    assert pos.entry_fee == pytest.approx(pos.entry_price * 0.0025)


def test_take_profit_long() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("long", 100, sl_pct=1, tp_pct=2), quote(0, 99.9, 100.0), qty=1.0)
    assert broker.on_quote(quote(1, 101.0, 101.1)) is None  # below TP of 102
    closed = broker.on_quote(quote(2, 102.0, 102.1))
    assert closed is not None and closed.reason_exit == "take_profit"
    assert closed.gross_pnl == pytest.approx(2.0)
    assert closed.net_pnl == pytest.approx(2.0 - (100 * 0.0025 + 102 * 0.0025))


def test_stop_loss_short() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("short", 100, sl_pct=1, tp_pct=2), quote(0, 100.0, 100.1), qty=1.0)
    closed = broker.on_quote(quote(1, 100.9, 101.0))  # ask >= SL 101
    assert closed is not None and closed.reason_exit == "stop_loss"
    assert closed.gross_pnl == pytest.approx(-1.0)


def test_max_hold_exit() -> None:
    broker = PaperBroker(cfg(max_hold_seconds=300))
    broker.open_position(signal("long", 100), quote(0, 99.9, 100.0), qty=1.0)
    assert broker.on_quote(quote(299, 100.0, 100.1)) is None
    closed = broker.on_quote(quote(301, 100.0, 100.1))
    assert closed is not None and closed.reason_exit == "max_hold"


def test_breakeven_stop_arms_and_protects() -> None:
    broker = PaperBroker(cfg(breakeven_after_r=0.5))
    # entry 100, SL 99 (risk = 1.0); breakeven arms at +0.5
    broker.open_position(signal("long", 100, sl_pct=1, tp_pct=5), quote(0, 99.9, 100.0), qty=1.0)
    assert broker.on_quote(quote(1, 100.6, 100.7)) is None  # arms breakeven
    closed = broker.on_quote(quote(2, 100.0, 100.1))        # bid back at entry → flat exit
    assert closed is not None and closed.reason_exit == "breakeven_stop"
    assert closed.gross_pnl == pytest.approx(0.0, abs=1e-9)  # filled at bid == entry


def test_cannot_double_open() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("long", 100), quote(0, 99.9, 100.0), qty=1.0)
    with pytest.raises(RuntimeError):
        broker.open_position(signal("long", 100), quote(1, 99.9, 100.0), qty=1.0)
