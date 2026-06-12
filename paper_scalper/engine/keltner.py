"""Keltner Channel + RSI mean-reversion scalper.

Implemented from the Research page candidate (source: FXOpen "four popular
1-minute scalping strategies 2026"). Fade closes outside the Keltner Channel
(EMA20 mid ± mult×ATR10) when RSI confirms the extreme; target the midline.

Tunables (self.p) are hot-reloadable from the dashboard Tune tab.
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA, RSI
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams


class KeltnerReversionStrategy(TunableParams):
    name = "keltner"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.mid = EMA(20)
        self.kc_atr = ATR(10)
        self.rsi = RSI(cfg.rsi_period)
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()
        self.p = {
            "kc_mult": 2.0,            # band width in ATR(10)s
            "rsi_low": 30.0,           # long fades need RSI at/below
            "rsi_high": 70.0,          # short fades need RSI at/above
            "min_atr_pct": 0.005,
            "max_atr_pct": 0.60,
            "max_spread_bps": cfg.max_spread_bps,
            "sl_atr_mult": 1.0,        # stop beyond the band, in ATR(14)s
            "sl_min_pct": 0.12,
            "sl_max_pct": 0.60,
            "tp_min_pct": 0.10,        # midline target, clamped
            "tp_max_pct": 0.90,
            "max_hold_seconds": 900,
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        mid = self.mid.update(candle.close)
        kc_atr = self.kc_atr.update(candle)
        rsi = self.rsi.update(candle.close)
        atr = self.atr.update(candle)

        snap = Snapshot(close=candle.close, ema_slow=mid, rsi=rsi, atr=atr)
        self.snapshot = snap

        if None in (mid, kc_atr, rsi, atr):
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

        upper = mid + p["kc_mult"] * kc_atr
        lower = mid - p["kc_mult"] * kc_atr
        side = None
        if px < lower and rsi <= p["rsi_low"]:
            side = "long"
        elif px > upper and rsi >= p["rsi_high"]:
            side = "short"
        if side is None:
            band_pos = (px - mid) / (p["kc_mult"] * kc_atr) if kc_atr > 0 else 0.0
            snap.rejects.append(f"inside channel (pos {band_pos:+.2f}, rsi {rsi:.1f})")
            return None

        sl_pct = min(max(p["sl_atr_mult"] * atr_pct, p["sl_min_pct"]), p["sl_max_pct"])
        midline_dist_pct = abs(mid - px) / px * 100
        tp_pct = min(max(midline_dist_pct, p["tp_min_pct"]), p["tp_max_pct"])
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=tp_pct,
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} keltner fade: px {px:.2f} outside "
                    f"{'lower' if side == 'long' else 'upper'} band "
                    f"({lower:.2f}/{upper:.2f}), rsi {rsi:.1f}, "
                    f"target midline {mid:.2f} ({tp_pct:.3f}%)"),
        )
