from __future__ import annotations

import asyncio

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.data.replay_feed import SyntheticFeed
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.strategy import Strategy


def cfg(**overrides) -> Settings:
    base = dict(
        candle_seconds=60, ema_fast=9, ema_slow=21, rsi_period=14, atr_period=14,
        vol_sma_period=20, vol_spike_mult=1.3, max_spread_bps=5.0,
        # loosened tuning bounds: these tests exercise the logic, not the calibration
        rsi_long_min=35.0, rsi_long_max=85.0, ema_sep_min_bps=1.0,
        min_atr_pct=0.01, max_atr_pct=5.0, pullback_tolerance_pct=0.10,
    )
    base.update(overrides)
    return Settings(**base)


def candle(ts: float, open_: float, close: float, low: float | None = None,
           high: float | None = None, vol: float = 1.0) -> Candle:
    low = min(open_, close) - 0.02 if low is None else low
    high = max(open_, close) + 0.02 if high is None else high
    typical = (high + low + close) / 3
    return Candle(ts_open=ts, open=open_, high=high, low=low, close=close,
                  volume=vol, notional=typical * vol)


def warm_up(strategy: Strategy, n: int = 30, base: float = 100.0) -> tuple[float, float]:
    """Feed a gentle up-trend (+0.12 / -0.08 alternating). Returns (next_ts, last_close)."""
    px = base
    ts = 1_700_000_000.0
    for i in range(n):
        step = 0.12 if i % 2 == 0 else -0.08
        strategy.on_candle(candle(ts, px, px + step), None)
        px += step
        ts += 60
    return ts, px


def trigger_long(strategy: Strategy, ts: float, px: float, vol: float = 1.6):
    """Pullback candle to EMA9, then a bullish reclaim candle with a volume spike."""
    ema9 = strategy.ema_fast.value
    assert ema9 is not None
    pull = candle(ts, px, px - 0.05, low=ema9 - 0.05)
    assert strategy.on_candle(pull, None) is None
    reclaim_close = strategy.ema_fast.value + 0.20
    reclaim = candle(ts + 60, px - 0.05, reclaim_close, vol=vol)
    return strategy.on_candle(reclaim, None)


def test_long_signal_fires_on_pullback_reclaim_with_volume() -> None:
    strategy = Strategy(cfg())
    ts, px = warm_up(strategy)
    signal = trigger_long(strategy, ts, px)
    assert signal is not None
    assert signal.side == "long"
    assert signal.sl_pct <= signal.tp_pct
    assert "vwap" in signal.reason


def test_no_signal_without_volume_spike() -> None:
    strategy = Strategy(cfg())
    ts, px = warm_up(strategy)
    assert trigger_long(strategy, ts, px, vol=1.0) is None
    assert "no volume spike" in strategy.snapshot.rejects


def test_wide_spread_blocks_entry() -> None:
    strategy = Strategy(cfg())
    ts, px = warm_up(strategy)
    ema9 = strategy.ema_fast.value
    strategy.on_candle(candle(ts, px, px - 0.05, low=ema9 - 0.05), None)
    wide = Quote(ts=ts + 60, symbol="BTC/USD", bid=px - 0.5, ask=px + 0.5,
                 bid_size=1, ask_size=1)  # ~100bps spread
    reclaim = candle(ts + 60, px - 0.05, strategy.ema_fast.value + 0.20, vol=1.6)
    assert strategy.on_candle(reclaim, wide) is None
    assert any("spread" in r for r in strategy.snapshot.rejects)


def test_pipeline_smoke_synthetic_feed(tmp_path) -> None:
    """End-to-end: synthetic ticks → engine → journal, no crashes, state persisted."""
    from paper_scalper.main import Engine
    from paper_scalper.storage.db import Journal

    settings = Settings(db_path=str(tmp_path / "t.db"), candle_seconds=60)
    journal = Journal(settings.db_path)
    engine = Engine(settings, journal)
    feed = SyntheticFeed(seed=7, max_ticks=40_000, realtime=False)

    async def run() -> None:
        async for event in feed.stream():
            engine.on_event(event)

    asyncio.run(run())
    assert len(engine.lanes) == 5  # pullback + momo + meanrev + trend + daily
    for lane in engine.lanes:
        assert lane.strategy.snapshot.close > 0  # candles flowed through every lane
    live = journal.get_state("live")
    assert live is not None
    assert set(live["lanes"]) == {"pullback", "momo", "meanrev", "trend", "daily"}
    journal.close()
