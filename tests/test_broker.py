"""Broker semantics: exit levels anchor to the entry quote MID and trigger on the
current mid; fills execute at bid/ask plus slippage (costs land in PnL, not in
the trigger geometry). Entry quotes here are symmetric so mid_ref is round."""

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


def test_levels_anchor_to_mid_not_fill() -> None:
    broker = PaperBroker(cfg())
    pos = broker.open_position(signal("long", 100, sl_pct=1, tp_pct=2),
                               quote(0, 99.95, 100.05), qty=1.0)
    assert pos.mid_ref == pytest.approx(100.0)
    assert pos.entry_price == pytest.approx(100.05)  # fill pays the spread
    assert pos.sl_price == pytest.approx(99.0)       # but levels are mid-anchored
    assert pos.tp_price == pytest.approx(102.0)


def test_take_profit_long_triggers_on_mid_fills_at_bid() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("long", 100, sl_pct=1, tp_pct=2),
                         quote(0, 99.95, 100.05), qty=1.0)
    assert broker.on_quote(quote(1, 101.0, 101.1)) is None    # mid 101.05 < 102
    closed = broker.on_quote(quote(2, 102.0, 102.1))          # mid 102.05 >= 102
    assert closed is not None and closed.reason_exit == "take_profit"
    assert closed.gross_pnl == pytest.approx(102.0 - 100.05)  # exit at bid
    assert closed.fees == pytest.approx((100.05 + 102.0) * 0.0025)


def test_stop_loss_short_triggers_on_mid_fills_at_ask() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("short", 100, sl_pct=1, tp_pct=2),
                         quote(0, 99.95, 100.05), qty=1.0)
    closed = broker.on_quote(quote(1, 100.95, 101.05))        # mid 101 >= SL 101
    assert closed is not None and closed.reason_exit == "stop_loss"
    assert closed.gross_pnl == pytest.approx(99.95 - 101.05)  # entry bid, exit ask


def test_max_hold_exit() -> None:
    broker = PaperBroker(cfg(max_hold_seconds=300))
    broker.open_position(signal("long", 100), quote(0, 99.95, 100.05), qty=1.0)
    assert broker.on_quote(quote(299, 100.0, 100.1)) is None
    closed = broker.on_quote(quote(301, 100.0, 100.1))
    assert closed is not None and closed.reason_exit == "max_hold"


def test_breakeven_stop_arms_and_protects() -> None:
    broker = PaperBroker(cfg(breakeven_after_r=0.5))
    # mid_ref 100, 1R = 1.0; breakeven arms at mid >= 100.5, SL moves to mid_ref
    broker.open_position(signal("long", 100, sl_pct=1, tp_pct=5),
                         quote(0, 99.95, 100.05), qty=1.0)
    assert broker.on_quote(quote(1, 100.6, 100.7)) is None    # mid 100.65 arms it
    closed = broker.on_quote(quote(2, 99.95, 100.05))         # mid 100 <= 100
    assert closed is not None and closed.reason_exit == "breakeven_stop"
    # "breakeven" still pays the round-trip spread — that's the honest cost
    assert closed.gross_pnl == pytest.approx(99.95 - 100.05)


def test_tp_profits_with_breakeven_disabled() -> None:
    """Tester-style: breakeven off, a long that reaches +20 (TP) closes green;
    one that drops to -10 (SL) closes red. Proves the raw entry/exit geometry."""
    broker = PaperBroker(cfg(fee_bps=0.0, slippage_bps=0.5))  # learning mode (live)
    sig = Signal(side="long", ts=0, ref_price=63500, sl_pct=10 / 63500 * 100,
                 tp_pct=20 / 63500 * 100, reason="test", breakeven_after_r=999.0)
    broker.open_position(sig, quote(0, 63499.5, 63500.5), qty=1.0)
    # ticks up but not to TP, then back toward entry — must NOT exit (breakeven off)
    assert broker.on_quote(quote(1, 63512, 63513)) is None    # mid 63512.5, +0.6R but no BE
    assert broker.on_quote(quote(2, 63500, 63501)) is None    # back near entry, still open
    won = broker.on_quote(quote(3, 63520, 63521))             # mid 63520.5 >= TP 63520
    assert won is not None and won.reason_exit == "take_profit"
    assert won.net_pnl > 0                                    # a TP close is PROFITABLE

    broker2 = PaperBroker(cfg(fee_bps=0.0, slippage_bps=0.5))
    broker2.open_position(sig, quote(0, 63499.5, 63500.5), qty=1.0)
    lost = broker2.on_quote(quote(1, 63489, 63490))           # mid 63489.5 <= SL 63490
    assert lost is not None and lost.reason_exit == "stop_loss"
    assert lost.net_pnl < 0


def test_cannot_double_open() -> None:
    broker = PaperBroker(cfg())
    broker.open_position(signal("long", 100), quote(0, 99.95, 100.05), qty=1.0)
    with pytest.raises(RuntimeError):
        broker.open_position(signal("long", 100), quote(1, 99.95, 100.05), qty=1.0)
