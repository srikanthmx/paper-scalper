from __future__ import annotations

import asyncio

import pytest

from paper_scalper.config import Settings
from paper_scalper.data.replay_feed import SyntheticFeed
from paper_scalper.engine.daily import DailyStrategy
from paper_scalper.engine.trend import TrendScalpStrategy
from paper_scalper.main import Engine
from paper_scalper.storage.db import Journal
from tests.test_strategy import candle


def test_apply_params_coerces_and_ignores_unknown() -> None:
    strategy = TrendScalpStrategy(Settings())
    strategy.apply_params({"rr": "3", "max_hold_seconds": 600.0, "nonsense": 1})
    assert strategy.p["rr"] == 3.0
    assert strategy.p["max_hold_seconds"] == 600
    assert isinstance(strategy.p["max_hold_seconds"], int)
    assert "nonsense" not in strategy.p


def test_save_param_version_increments_and_activates(tmp_path) -> None:
    journal = Journal(str(tmp_path / "j.db"))
    v1 = journal.save_param_version("trend", {"rr": 2.0}, note="baseline")
    v2 = journal.save_param_version("trend", {"rr": 3.0}, note="wider target")
    assert (v1, v2) == (1, 2)
    state = journal.get_state("params:trend")
    assert state["version"] == 2 and state["params"]["rr"] == 3.0
    history = journal.param_versions("trend")
    assert [h["version"] for h in history] == [2, 1]
    journal.close()


def test_engine_bootstraps_and_hot_reloads_params(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "e.db"))
    journal = Journal(settings.db_path)
    engine = Engine(settings, journal)
    trend_lane = next(l for l in engine.lanes if l.name == "trend")
    assert trend_lane.version == 1  # bootstrap saved v1 = defaults

    journal.save_param_version("trend", {**trend_lane.strategy.p, "rr": 4.0},
                               note="test reload")
    feed = SyntheticFeed(seed=3, max_ticks=2_000, realtime=False)

    async def run() -> None:
        async for event in feed.stream():
            engine.on_event(event)

    asyncio.run(run())
    assert trend_lane.version == 2
    assert trend_lane.strategy.p["rr"] == 4.0
    journal.close()


def test_version_stats_groups_trades(tmp_path) -> None:
    from paper_scalper.engine.paper_broker import ClosedTrade

    journal = Journal(str(tmp_path / "v.db"))
    trade = ClosedTrade(side="long", qty=1.0, entry_ts=0, entry_price=100, exit_ts=60,
                        exit_price=101, fees=0, gross_pnl=1.0, net_pnl=1.0,
                        reason_entry="t", reason_exit="take_profit")
    journal.record_trade("BTC/USD", "trend", trade, version=1)
    journal.record_trade("BTC/USD", "trend", trade, version=2)
    journal.record_trade("BTC/USD", "trend", trade, version=2)
    stats = {(s["strategy"], s["version"]): s["trades"] for s in journal.version_stats()}
    assert stats[("trend", 1)] == 1 and stats[("trend", 2)] == 2
    journal.close()


def _utc(hour: int, minute: int) -> float:
    from datetime import datetime, timezone
    return datetime(2026, 6, 12, hour, minute, tzinfo=timezone.utc).timestamp()


def test_daily_orb_arms_oco_stops_when_range_completes() -> None:
    strategy = DailyStrategy(Settings())
    px = 100.0
    ts = _utc(13, 30)
    for i in range(15):  # opening range 99.7–100.3
        strategy.on_candle(candle(ts + i * 60, px, px, low=99.7, high=100.3), None)
    assert any("building opening range" in r for r in strategy.snapshot.rejects)
    signals = strategy.on_candle(candle(_utc(13, 45), px, px + 0.01), None)
    assert isinstance(signals, list) and len(signals) == 2
    by_side = {s.side: s for s in signals}
    assert by_side["long"].entry_type == "stop"
    assert by_side["long"].entry_stop == pytest.approx(100.3 * 1.0002)   # edge + buffer
    assert by_side["short"].entry_stop == pytest.approx(99.7 * 0.9998)
    for s in signals:
        assert s.tp_pct == pytest.approx(2 * s.sl_pct)  # strict 1:2
        assert s.valid_seconds > 3600                   # alive until the 20:00 cutoff
    # arms exactly once per session
    assert strategy.on_candle(candle(_utc(13, 46), px, px + 0.01), None) is None


def test_daily_orb_quiet_before_open_and_without_range() -> None:
    strategy = DailyStrategy(Settings())
    px = 100.0
    strategy.on_candle(candle(_utc(9, 0), px, px + 0.1), None)
    assert any("waiting for opening range" in r for r in strategy.snapshot.rejects)
    strategy.on_candle(candle(_utc(21, 0), px, px + 0.1), None)
    assert any("no opening range" in r for r in strategy.snapshot.rejects)
