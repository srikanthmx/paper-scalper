from __future__ import annotations

import pytest

from paper_scalper.config import Settings
from paper_scalper.engine.risk import RiskManager

TS = 1_700_000_000.0


def manager(**overrides) -> RiskManager:
    base = dict(starting_equity=10_000.0, daily_stop_pct=2.0, max_consecutive_losses=3,
                cooldown_candles=2, risk_per_trade_pct=0.5, max_notional_fraction=0.5,
                enable_risk_halts=True)  # these tests exercise the halt logic
    base.update(overrides)
    return RiskManager(Settings(**base))


def test_daily_stop_halts_until_next_day() -> None:
    risk = manager()
    risk.on_trade_closed(-250.0, TS)
    assert risk.can_enter(TS + 600).allowed is False  # -2.5% > -2% stop (plus cooldown)
    assert "daily stop" in risk.halted_reason
    assert risk.can_enter(TS + 86_400).allowed is True  # next UTC day resets


def test_three_consecutive_losses_halt() -> None:
    risk = manager(cooldown_candles=0)
    for i in range(3):
        risk.on_trade_closed(-30.0, TS + i)
    assert risk.halted_reason is not None
    assert "consecutive" in risk.halted_reason


def test_win_resets_streak() -> None:
    risk = manager(cooldown_candles=0)
    risk.on_trade_closed(-30.0, TS)
    risk.on_trade_closed(-30.0, TS + 1)
    risk.on_trade_closed(50.0, TS + 2)
    assert risk.consecutive_losses == 0
    assert risk.halted_reason is None


def test_cooldown_blocks_then_clears() -> None:
    risk = manager(cooldown_candles=2)
    risk.on_trade_closed(10.0, TS)
    assert risk.can_enter(TS + 1).allowed is False
    risk.on_candle()
    risk.on_candle()
    assert risk.can_enter(TS + 1).allowed is True


def test_position_size_risks_fixed_fraction() -> None:
    risk = manager()
    # risk 0.5% of 10k = $50; SL 0.5% of price 100 = $0.5 → 100 units... capped:
    # notional cap = 0.5 * 10000 / 100 = 50 units
    qty = risk.position_size(price=100.0, sl_pct=0.5)
    assert qty == pytest.approx(50.0)
    # wider SL → smaller size, under the cap: $50 / (100*2%) = 25 units
    assert risk.position_size(price=100.0, sl_pct=2.0) == pytest.approx(25.0)
