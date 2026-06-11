"""Momentum breakout scalper: close beyond the N-candle high/low with volume.

Aggressive by design (test tuning) — enters immediately on the breakout candle.
"""

from __future__ import annotations

from collections import deque

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, RollingMean
from paper_scalper.engine.strategy import Signal, Snapshot


class MomentumStrategy:
    name = "momo"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.atr = ATR(cfg.atr_period)
        self.vol_avg = RollingMean(cfg.vol_sma_period)
        self._highs: deque[float] = deque(maxlen=cfg.momo_lookback)
        self._lows: deque[float] = deque(maxlen=cfg.momo_lookback)
        self.snapshot = Snapshot()

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        cfg = self.cfg
        vol_avg = self.vol_avg.value  # lagged baseline, see strategy.py
        self.vol_avg.update(candle.volume)
        atr = self.atr.update(candle)
        prior_high = max(self._highs) if len(self._highs) == cfg.momo_lookback else None
        prior_low = min(self._lows) if len(self._lows) == cfg.momo_lookback else None
        self._highs.append(candle.high)
        self._lows.append(candle.low)

        snap = Snapshot(close=candle.close, atr=atr, vol_avg=vol_avg)
        self.snapshot = snap

        if None in (atr, vol_avg, prior_high, prior_low):
            snap.rejects.append("warming_up")
            return None
        assert atr and vol_avg and prior_high and prior_low

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > cfg.max_spread_bps:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {cfg.max_spread_bps}")
            return None
        if not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None
        if candle.volume < cfg.momo_vol_mult * vol_avg:
            snap.rejects.append("no volume confirmation")
            return None

        side = "long" if px > prior_high else "short" if px < prior_low else None
        if side is None:
            snap.rejects.append("no breakout")
            return None

        sl_pct = min(max(1.0 * atr_pct, 0.20), 0.50)
        tp_pct = min(max(1.8 * atr_pct, 0.35), 1.00)
        ref = prior_high if side == "long" else prior_low
        return Signal(
            side=side, ts=candle.ts_open + cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=tp_pct,
            reason=(f"{side} breakout: close {px:.2f} vs {cfg.momo_lookback}-bar "
                    f"{'high' if side == 'long' else 'low'} {ref:.2f}, "
                    f"vol {candle.volume:.3f} vs avg {vol_avg:.3f}, atr {atr_pct:.3f}%"),
        )
