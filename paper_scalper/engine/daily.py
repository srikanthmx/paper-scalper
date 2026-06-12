"""DAILY RESEARCH LANE — strategy of the day, replaced via the Research page.

Contract for the rewrite (the research agent regenerates this module):
- class name stays ``DailyStrategy`` with ``name = "daily"``
- constructor takes ``Settings``; expose tunables in ``self.p`` (TunableParams)
- ``on_candle(candle, quote) -> Signal | None``; set ``self.snapshot`` every call
- data-only: no network imports (enforced by tests/test_no_real_orders.py)
- bump the journal version after deploy with note "algo: <name>"

Current algorithm: **VWAP Pullback (session-filtered)** — deployed from the
Research page 2026-06-11. Source: tradelikemaster.com BTC scalping writeup
(published PF 1.32 with the session filter, ~48% win rate).

Rules: price above session VWAP = long bias; enter long when a candle pulls
back to touch VWAP (within tolerance) and closes back above it. Mirror for
shorts below VWAP. Fixed TP +0.30% / SL −0.15% (2:1). Optionally only trade
the London/NY overlap (13:00–16:00 UTC) — the source's edge concentrated there.
"""

from __future__ import annotations

from datetime import datetime, timezone

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, SessionVWAP
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams

ALGO = "VWAP Pullback (session-filtered)"


class DailyStrategy(TunableParams):
    name = "daily"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.vwap = SessionVWAP()
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()
        self.p = {
            "touch_tol_pct": 0.04,     # how close to VWAP counts as a touch
            "sl_pct": 0.15,            # fixed stop, per source
            "tp_pct": 0.30,            # fixed target, per source (2:1)
            "session_only": 1,         # 1 = trade only session_start..end UTC
            "session_start_hour": 13,  # London/NY overlap (UTC)
            "session_end_hour": 16,
            "min_atr_pct": 0.005,
            "max_atr_pct": 0.60,
            "max_spread_bps": cfg.max_spread_bps,
            "max_hold_seconds": 900,
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        vwap = self.vwap.update(candle)
        atr = self.atr.update(candle)
        snap = Snapshot(close=candle.close, vwap=vwap, atr=atr)
        self.snapshot = snap

        if None in (vwap, atr):
            snap.rejects.append("warming_up")
            return None

        px = candle.close
        atr_pct = atr / px * 100
        if p["session_only"]:
            hour = datetime.fromtimestamp(candle.ts_open, tz=timezone.utc).hour
            if not (p["session_start_hour"] <= hour < p["session_end_hour"]):
                snap.rejects.append(f"outside session (utc {hour}h)")
                return None
        if quote is not None and quote.spread_bps > p["max_spread_bps"]:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {p['max_spread_bps']}")
            return None
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None

        tol = px * p["touch_tol_pct"] / 100
        side = None
        # pullback to VWAP from above, close back above → long; mirror for short
        if candle.open > vwap and candle.low <= vwap + tol and px > vwap:
            side = "long"
        elif candle.open < vwap and candle.high >= vwap - tol and px < vwap:
            side = "short"
        if side is None:
            snap.rejects.append(f"no vwap touch (px {(px - vwap) / px * 100:+.3f}% from vwap)")
            return None

        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=p["sl_pct"], tp_pct=p["tp_pct"],
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} {ALGO}: touched vwap {vwap:.2f} and closed "
                    f"{'above' if side == 'long' else 'below'} (px {px:.2f}), "
                    f"atr {atr_pct:.3f}%"),
        )
