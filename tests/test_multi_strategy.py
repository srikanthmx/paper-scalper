from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.engine.meanrev import MeanReversionStrategy
from paper_scalper.engine.momentum import MomentumStrategy
from paper_scalper.engine.paper_broker import ClosedTrade
from paper_scalper.storage.db import Journal
from tests.test_strategy import candle


def cfg(**overrides) -> Settings:
    base = dict(candle_seconds=60, atr_period=5, vol_sma_period=5, momo_lookback=5,
                momo_vol_mult=1.1, rsi_period=5, max_spread_bps=50.0,
                min_atr_pct=0.001, max_atr_pct=10.0, mr_rsi_low=32.0, mr_rsi_high=68.0,
                mr_vwap_atr_mult=0.5)
    base.update(overrides)
    return Settings(**base)


def test_momentum_fires_on_breakout_with_volume() -> None:
    strategy = MomentumStrategy(cfg())
    ts, px = 1_700_000_000.0, 100.0
    for i in range(10):  # flat range 99.8–100.2
        strategy.on_candle(candle(ts + i * 60, px, px + (0.2 if i % 2 == 0 else -0.2)), None)
    signal = strategy.on_candle(candle(ts + 600, px, px + 1.0, vol=2.0), None)
    assert signal is not None and signal.side == "long"
    assert "breakout" in signal.reason


def test_momentum_needs_volume() -> None:
    strategy = MomentumStrategy(cfg())
    ts, px = 1_700_000_000.0, 100.0
    for i in range(10):
        strategy.on_candle(candle(ts + i * 60, px, px + (0.2 if i % 2 == 0 else -0.2)), None)
    assert strategy.on_candle(candle(ts + 600, px, px + 1.0, vol=1.0), None) is None
    assert "no volume confirmation" in strategy.snapshot.rejects


def test_meanrev_fades_oversold_below_vwap() -> None:
    strategy = MeanReversionStrategy(cfg())
    ts, px = 1_700_006_400.0, 100.0
    for i in range(8):  # steady decline → low RSI, price below VWAP
        nxt = px - 0.4
        strategy.on_candle(candle(ts + i * 60, px, nxt), None)
        px = nxt
    signal = strategy.on_candle(candle(ts + 480, px, px - 0.4), None)
    assert signal is not None and signal.side == "long"
    assert "fade" in signal.reason


def test_journal_separates_strategies(tmp_path) -> None:
    journal = Journal(str(tmp_path / "j.db"))
    trade = ClosedTrade(side="long", qty=1.0, entry_ts=0.0, entry_price=100.0, exit_ts=60.0,
                        exit_price=101.0, fees=0.5, gross_pnl=1.0, net_pnl=0.5,
                        reason_entry="t", reason_exit="take_profit")
    journal.record_trade("BTC/USD", "momo", trade)
    journal.record_trade("BTC/USD", "pullback", trade)
    journal.record_trade("BTC/USD", "pullback", trade)
    assert journal.summary("momo")["trades"] == 1
    assert journal.summary("pullback")["trades"] == 2
    assert journal.summary()["trades"] == 3
    assert {t["strategy"] for t in journal.trades()} == {"momo", "pullback"}
    journal.close()


def test_lanes_trade_independently(tmp_path) -> None:
    """One lane's halt must not block another lane."""
    from paper_scalper.engine.risk import RiskManager

    settings = Settings(max_consecutive_losses=1, cooldown_candles=0)
    risk_a, risk_b = RiskManager(settings), RiskManager(settings)
    risk_a.on_trade_closed(-10.0, 1_700_000_000.0)
    assert risk_a.halted_reason is not None
    assert risk_b.can_enter(1_700_000_000.0).allowed is True
