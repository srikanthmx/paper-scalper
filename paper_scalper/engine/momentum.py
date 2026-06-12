"""Momentum breakout scalper: close beyond the N-candle high/low with volume.

Aggressive by design (test tuning) — enters immediately on the breakout candle.
Tunable params (self.p) are hot-reloadable from the dashboard.
"""

from __future__ import annotations

from collections import deque

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, RollingMean
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams


class MomentumStrategy(TunableParams):
    name = "momo"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.atr = ATR(cfg.atr_period)
        self.vol_avg = RollingMean(cfg.vol_sma_period)
        self._highs: deque[float] = deque(maxlen=cfg.momo_lookback)
        self._lows: deque[float] = deque(maxlen=cfg.momo_lookback)
        self.snapshot = Snapshot()
        self.p = {
            "momo_vol_mult": cfg.momo_vol_mult,
            "min_atr_pct": cfg.min_atr_pct,
            "max_atr_pct": cfg.max_atr_pct,
            "max_spread_bps": cfg.max_spread_bps,
            "sl_atr_mult": 1.0,
            "tp_atr_mult": 1.8,
            "sl_min_pct": 0.20,
            "sl_max_pct": 0.50,
            "tp_min_pct": 0.35,
            "tp_max_pct": 1.00,
            "max_hold_seconds": cfg.max_hold_seconds,
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        vol_avg = self.vol_avg.value  # lagged baseline, see strategy.py
        self.vol_avg.update(candle.volume)
        atr = self.atr.update(candle)
        lookback = self._highs.maxlen
        prior_high = max(self._highs) if len(self._highs) == lookback else None
        prior_low = min(self._lows) if len(self._lows) == lookback else None
        self._highs.append(candle.high)
        self._lows.append(candle.low)

        snap = Snapshot(close=candle.close, atr=atr, vol_avg=vol_avg)
        self.snapshot = snap

        # NB: 0.0 is a legitimate value (e.g. vol_avg in quiet hours) — only None means not warm
        if None in (atr, vol_avg, prior_high, prior_low):
            snap.rejects.append("warming_up")
            return None

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > p["max_spread_bps"]:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {p['max_spread_bps']}")
            return None
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None
        if vol_avg <= 0 or candle.volume < p["momo_vol_mult"] * vol_avg:
            snap.rejects.append("no volume confirmation")
            return None

        side = "long" if px > prior_high else "short" if px < prior_low else None
        if side is None:
            snap.rejects.append("no breakout")
            return None

        sl_pct = min(max(p["sl_atr_mult"] * atr_pct, p["sl_min_pct"]), p["sl_max_pct"])
        tp_pct = min(max(p["tp_atr_mult"] * atr_pct, p["tp_min_pct"]), p["tp_max_pct"])
        ref = prior_high if side == "long" else prior_low
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=tp_pct,
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} breakout: close {px:.2f} vs {lookback}-bar "
                    f"{'high' if side == 'long' else 'low'} {ref:.2f}, "
                    f"vol {candle.volume:.3f} vs avg {vol_avg:.3f}, atr {atr_pct:.3f}%"),
        )
