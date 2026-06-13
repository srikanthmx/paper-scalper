from __future__ import annotations

import math

from paper_scalper.backtest import Backtester


def _synthetic_mean_reverting(n: int = 1500) -> list[dict]:
    """An Ornstein-Uhlenbeck-ish series: mean-reversion should beat trend here."""
    candles = []
    ts = 1_700_000_000.0
    for i in range(n):
        # wide oscillation around 100 so RSI reaches the 25/75 extremes the rule needs
        px = 100.0 + 6.0 * math.sin(i * 0.45) + 1.5 * math.sin(i * 0.13)
        hi, lo = px + 0.3, px - 0.3
        candles.append({"ts_open": ts + i * 60, "open": px, "high": hi, "low": lo,
                        "close": px, "volume": 1.0})
    return candles


def test_backtester_runs_and_ranks() -> None:
    bt = Backtester(_synthetic_mean_reverting())
    results = bt.grid(["random", "trend"])  # random always fires -> non-empty grid
    assert results, "grid returned no results"
    totals = [r.total_r for r in results]
    assert totals == sorted(totals, reverse=True)  # ranked by total R descending


def test_costs_make_a_difference() -> None:
    """Cost is charged in R: a tighter stop carries more cost-in-R, same series."""
    bt = Backtester(_synthetic_mean_reverting())
    tight = bt.run("random", sl_atr=0.75, rr=1.0, hold=40)
    wide = bt.run("random", sl_atr=3.0, rr=1.0, hold=40)
    assert tight is not None and wide is not None
    assert wide.expectancy_r > tight.expectancy_r  # less cost drag on the wider stop


def test_result_total_r_is_expectancy_times_trades() -> None:
    bt = Backtester(_synthetic_mean_reverting())
    r = bt.run("random", sl_atr=2.0, rr=1.0, hold=40)
    assert r is not None
    assert math.isclose(r.total_r, r.expectancy_r * r.trades, rel_tol=1e-9)


def test_meanrev_signal_fires_on_real_extreme() -> None:
    """A sharp V-bottom must push RSI<=25 and trigger a meanrev long."""
    ts = 1_700_000_000.0
    closes = [100.0] * 60 + [100 - i * 1.5 for i in range(1, 16)]  # warm-up then steep drop
    candles = [{"ts_open": ts + i * 60, "open": p, "high": p + 0.2, "low": p - 0.2,
                "close": p, "volume": 1.0} for i, p in enumerate(closes)]
    bt = Backtester(candles)
    assert any(bt._signal("meanrev", i) == 1 for i in range(60, len(closes)))
