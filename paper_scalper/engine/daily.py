"""DAILY RESEARCH LANE — strategy of the day, replaced by the daily research job.

This module is REGENERATED each day by an automated research session that picks a
trending TradingView strategy and reimplements it here. Contract for the rewrite:

- class name stays ``DailyStrategy`` with ``name = "daily"``
- constructor takes ``Settings``; expose tunables in ``self.p`` (TunableParams)
- ``on_candle(candle, quote) -> Signal | None``; set ``self.snapshot`` every call
- data-only: no network imports (enforced by tests/test_no_real_orders.py)
- bump the journal version after deploy:
  ``curl -X POST localhost:8765/api/params -d '{"strategy":"daily","params":{},"note":"algo: <name>"}'``

Current algorithm (seed): **Supertrend (10, 2)** — the perennial TradingView
favorite. Long when price closes above the upper band (trend flips up), short on
the mirror. Simple SL/TP exits at 1R / 2R.
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams

ALGO = "Supertrend(10,2)"


class DailyStrategy(TunableParams):
    name = "daily"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.atr = ATR(10)
        self.snapshot = Snapshot()
        self._upper: float | None = None  # final upper band
        self._lower: float | None = None  # final lower band
        self._dir = 0                     # +1 up-trend, -1 down-trend, 0 unknown
        self._prev_close: float | None = None
        self.p = {
            "st_mult": 2.0,               # band width in ATRs
            "min_atr_pct": 0.005,
            "max_atr_pct": 0.60,
            "max_spread_bps": cfg.max_spread_bps,
            "sl_atr_mult": 1.0,
            "sl_min_pct": 0.10,
            "sl_max_pct": 0.60,
            "rr": 2.0,
            "max_hold_seconds": 1800,
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        atr = self.atr.update(candle)
        snap = Snapshot(close=candle.close, atr=atr)
        self.snapshot = snap

        if atr is None:
            snap.rejects.append("warming_up")
            self._prev_close = candle.close
            return None

        mid = (candle.high + candle.low) / 2
        basic_upper = mid + p["st_mult"] * atr
        basic_lower = mid - p["st_mult"] * atr
        prev_close = self._prev_close
        self._prev_close = candle.close

        # final bands ratchet with the trend (standard Supertrend recursion)
        if self._upper is None or prev_close is None:
            self._upper, self._lower = basic_upper, basic_lower
            snap.rejects.append("warming_up")
            return None
        self._upper = basic_upper if basic_upper < self._upper or prev_close > self._upper \
            else self._upper
        self._lower = basic_lower if basic_lower > self._lower or prev_close < self._lower \
            else self._lower

        prev_dir = self._dir
        if candle.close > self._upper:
            self._dir = 1
        elif candle.close < self._lower:
            self._dir = -1

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > p["max_spread_bps"]:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {p['max_spread_bps']}")
            return None
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None
        if self._dir == prev_dir or self._dir == 0:
            snap.rejects.append(f"no flip (dir {self._dir:+d})")
            return None

        side = "long" if self._dir > 0 else "short"
        sl_pct = min(max(p["sl_atr_mult"] * atr_pct, p["sl_min_pct"]), p["sl_max_pct"])
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=p["rr"] * sl_pct,
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} {ALGO} flip: close {px:.2f} crossed "
                    f"{'upper' if side == 'long' else 'lower'} band, atr {atr_pct:.3f}%"),
        )
