"""Engine-level: tick-filled stop entries (OCO, expiry) and per-lane timeframes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote, Trade
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.main import Engine
from paper_scalper.storage.db import Journal


def quote(ts: float, mid: float, spread: float = 0.02) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=mid - spread / 2, ask=mid + spread / 2,
                 bid_size=1, ask_size=1)


def trade(ts: float, px: float) -> Trade:
    return Trade(ts=ts, symbol="BTC/USD", price=px, size=1.0)


class ArmingStub:
    """Arms one OCO stop pair on the first completed candle, then stays quiet."""

    name = "stub"
    timeframe_seconds = 60

    def __init__(self, cfg: Settings) -> None:
        self.snapshot = Snapshot()
        self.p: dict = {}
        self._armed = False

    def apply_params(self, new: dict) -> None:
        pass

    def on_candle(self, candle: Candle, q: Quote | None):
        self.snapshot = Snapshot(close=candle.close)
        if self._armed:
            return None
        self._armed = True
        mk = lambda side, stop: Signal(  # noqa: E731
            side=side, ts=candle.ts_open + 60, ref_price=stop, sl_pct=1.0, tp_pct=2.0,
            reason=f"stub {side}", entry_type="stop", entry_stop=stop, valid_seconds=120)
        return [mk("long", 101.0), mk("short", 99.0)]


class TFCounter:
    """Counts candles it receives; used to verify per-lane timeframe dispatch."""

    timeframe_seconds = 180

    def __init__(self, cfg: Settings) -> None:
        self.name = "tfcounter"
        self.snapshot = Snapshot()
        self.p: dict = {}
        self.candles_seen: list[float] = []

    def apply_params(self, new: dict) -> None:
        pass

    def on_candle(self, candle: Candle, q: Quote | None):
        self.snapshot = Snapshot(close=candle.close)
        self.candles_seen.append(candle.ts_open)
        return None


def make_engine(tmp_path, strategies) -> Engine:
    settings = Settings(db_path=str(tmp_path / "t.db"), slippage_bps=0.0)
    return Engine(settings, Journal(settings.db_path), strategies=strategies)


def feed_candle_window(engine: Engine, t0: float, px: float) -> None:
    """One quote per second for 61s so the 60s candle closes."""
    for i in range(61):
        engine.on_event(quote(t0 + i, px))


def test_stop_entry_fills_on_the_crossing_tick_with_oco(tmp_path) -> None:
    engine = make_engine(tmp_path, [ArmingStub(Settings())])
    lane = engine.lanes[0]
    feed_candle_window(engine, 1_700_000_000.0, 100.0)   # candle closes -> stops armed
    assert len(lane.pending) == 2
    engine.on_event(quote(1_700_000_062.0, 100.5))        # between the stops: no fill
    assert lane.broker.position is None and len(lane.pending) == 2
    engine.on_event(quote(1_700_000_063.0, 101.02))       # tick crosses the long stop
    pos = lane.broker.position
    assert pos is not None and pos.side == "long"
    assert pos.entry_price == pytest.approx(101.03)       # filled at that tick's ask
    assert lane.pending == []                             # OCO cancelled the short


def test_stop_entry_expires_unfilled(tmp_path) -> None:
    engine = make_engine(tmp_path, [ArmingStub(Settings())])
    lane = engine.lanes[0]
    feed_candle_window(engine, 1_700_000_000.0, 100.0)
    assert len(lane.pending) == 2
    engine.on_event(quote(1_700_000_060.0 + 200, 100.5))  # past valid_seconds=120
    assert lane.pending == [] and lane.broker.position is None


def test_lanes_receive_their_own_timeframe(tmp_path) -> None:
    counter = TFCounter(Settings())
    engine = make_engine(tmp_path, [counter])
    assert set(engine.builders) == {60, 180}              # chart TF + lane TF
    t0 = 1_700_000_000.0 - 1_700_000_000.0 % 180          # align to a 3m boundary
    for i in range(0, 200):                               # >9min of trades, 3s apart
        engine.on_event(trade(t0 + i * 3, 100.0))
    assert len(counter.candles_seen) == 3                 # 3 completed 3m candles
    assert all(ts % 180 == 0 for ts in counter.candles_seen)


def test_orb_end_to_end_fills_on_tick_not_candle_close(tmp_path) -> None:
    from paper_scalper.engine.daily import DailyStrategy

    settings = Settings(db_path=str(tmp_path / "orb.db"), slippage_bps=0.0)
    engine = Engine(settings, Journal(settings.db_path),
                    strategies=[DailyStrategy(settings)])
    lane = engine.lanes[0]
    t0 = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc).timestamp()
    # 13:30 -> 13:47: range builds 13:30-13:44; the post-window candle closes
    # at 13:46 and arms the OCO stops
    for i in range(17 * 60 + 1):
        engine.on_event(quote(t0 + i, 100.0))
    assert len(lane.pending) == 2                         # OCO armed after the window
    fill_ts = t0 + 18 * 60
    engine.on_event(quote(fill_ts, 100.2))                # tick through high+buffer
    pos = lane.broker.position
    assert pos is not None and pos.side == "long"
    assert pos.entry_ts == fill_ts                        # filled on the tick itself
