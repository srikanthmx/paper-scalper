"""DAILY RESEARCH LANE — strategy of the day, replaced via the Research page.

Contract for the rewrite (the research agent regenerates this module):
- class name stays ``DailyStrategy`` with ``name = "daily"``
- constructor takes ``Settings``; expose tunables in ``self.p`` (TunableParams)
- ``on_candle(candle, quote) -> Signal | None``; set ``self.snapshot`` every call
- data-only: no network imports (enforced by tests/test_no_real_orders.py)
- bump the journal version after deploy with note "algo: <name>"

Current algorithm: **Opening Range Breakout (US-open adapted)** — deployed from
the Research page 2026-06-12, replacing VWAP Pullback (in version history).
Source: tradethatswing.com strict-rule ORB (published 74.6% win rate, PF 2.51 —
on equities at the open; this BTC adaptation is our own).

Rules: BTC has no official open, but it reliably reacts to the US equity open.
Mark the high/low of the first 15 minutes after 13:30 UTC. The moment the range
completes, ARM STOP ORDERS at both edges (plus buffer) — the engine fills them
on the tick that crosses, OCO (first fill cancels the other side), valid until
the session cutoff. SL at the opposite range edge (clamped), TP at 2R.
"""

from __future__ import annotations

from datetime import datetime, timezone

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams

ALGO = "Opening Range Breakout (US-open)"


class DailyStrategy(TunableParams):
    name = "daily"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()
        self._day: int | None = None
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._armed = False
        self.p = {
            "or_start_hour": 13,       # US equity open, UTC
            "or_start_min": 30,
            "or_window_min": 15,       # opening-range length
            "cutoff_hour": 20,         # pending stops expire at this UTC hour
            "buffer_bps": 2.0,         # stop sits this far beyond the range edge
            "rr": 2.0,                 # strict 1:2
            "sl_min_pct": 0.10,        # SL = opposite range edge, clamped
            "sl_max_pct": 0.80,
            "max_spread_bps": cfg.max_spread_bps,
            "max_hold_seconds": 3600,
        }

    def _roll_day(self, ts: float) -> None:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).toordinal()
        if day != self._day:
            self._day = day
            self._or_high = self._or_low = None
            self._armed = False

    def _stop_signal(self, side: str, ts: float, valid: int) -> Signal:
        p = self.p
        buffer_mult = p["buffer_bps"] / 10_000
        if side == "long":
            entry = self._or_high * (1 + buffer_mult)
            edge = self._or_low
        else:
            entry = self._or_low * (1 - buffer_mult)
            edge = self._or_high
        sl_pct = min(max(abs(entry - edge) / entry * 100, p["sl_min_pct"]), p["sl_max_pct"])
        return Signal(
            side=side, ts=ts, ref_price=entry, sl_pct=sl_pct, tp_pct=p["rr"] * sl_pct,
            entry_type="stop", entry_stop=entry, valid_seconds=valid,
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} {ALGO}: stop armed at {entry:.2f} "
                    f"({'above' if side == 'long' else 'below'} range "
                    f"{self._or_low:.2f}-{self._or_high:.2f}), SL opposite edge "
                    f"({sl_pct:.3f}%), TP {p['rr']:.0f}R, OCO until "
                    f"{int(p['cutoff_hour'])}:00 UTC"),
        )

    def on_candle(self, candle: Candle, quote: Quote | None) -> list[Signal] | None:
        p = self.p
        self._roll_day(candle.ts_open)
        atr = self.atr.update(candle)
        snap = Snapshot(close=candle.close, atr=atr)
        self.snapshot = snap

        dt = datetime.fromtimestamp(candle.ts_open, tz=timezone.utc)
        minute = dt.hour * 60 + dt.minute
        or_start = int(p["or_start_hour"]) * 60 + int(p["or_start_min"])
        or_end = or_start + int(p["or_window_min"])

        if minute < or_start:
            snap.rejects.append(f"waiting for opening range ({int(p['or_start_hour'])}:"
                                f"{int(p['or_start_min']):02d} UTC)")
            return None
        if minute < or_end:
            self._or_high = candle.high if self._or_high is None else max(self._or_high, candle.high)
            self._or_low = candle.low if self._or_low is None else min(self._or_low, candle.low)
            snap.rejects.append(f"building opening range ({self._or_low:.2f}-{self._or_high:.2f})")
            return None
        if self._armed or self._or_high is None or self._or_low is None:
            if not self._armed:
                snap.rejects.append("no opening range today (started mid-session)")
            return None
        if minute >= int(p["cutoff_hour"]) * 60:
            snap.rejects.append("session over (past cutoff)")
            return None

        # range complete: arm OCO stop orders at both edges, valid until cutoff
        self._armed = True
        candle_end = candle.ts_open + self.cfg.candle_seconds
        cutoff_ts = datetime(dt.year, dt.month, dt.day, int(p["cutoff_hour"]),
                             tzinfo=timezone.utc).timestamp()
        valid = max(int(cutoff_ts - candle_end), 60)
        return [self._stop_signal("long", candle_end, valid),
                self._stop_signal("short", candle_end, valid)]
