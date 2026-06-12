"""Mean-reversion scalper: fade RSI extremes when price is stretched from VWAP.

Counter-trend by design — complements the trend (pullback) and breakout (momo)
lanes so at least one lane is active in most regimes.
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, RSI, SessionVWAP
from paper_scalper.engine.strategy import Signal, Snapshot


class MeanReversionStrategy:
    name = "meanrev"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.vwap = SessionVWAP()
        self.rsi = RSI(cfg.rsi_period)
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        cfg = self.cfg
        vwap = self.vwap.update(candle)
        rsi = self.rsi.update(candle.close)
        atr = self.atr.update(candle)

        snap = Snapshot(close=candle.close, vwap=vwap, rsi=rsi, atr=atr)
        self.snapshot = snap

        # NB: 0.0 is a legitimate value — only None means not warm
        if None in (vwap, rsi, atr):
            snap.rejects.append("warming_up")
            return None

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > cfg.max_spread_bps:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {cfg.max_spread_bps}")
            return None
        if not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None

        stretch = cfg.mr_vwap_atr_mult * atr
        side = None
        if rsi <= cfg.mr_rsi_low and px <= vwap - stretch:
            side = "long"
        elif rsi >= cfg.mr_rsi_high and px >= vwap + stretch:
            side = "short"
        if side is None:
            snap.rejects.append(f"no extreme (rsi {rsi:.1f}, vwap dist "
                                f"{(px - vwap) / atr:+.2f} atr)")
            return None

        sl_pct = min(max(1.5 * atr_pct, 0.25), 0.60)
        tp_pct = min(max(1.2 * atr_pct, 0.30), 0.80)
        return Signal(
            side=side, ts=candle.ts_open + cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=tp_pct,
            max_hold_seconds=cfg.mr_max_hold_seconds,  # fades revert slower than scalps
            reason=(f"{side} fade: rsi {rsi:.1f}, px {px:.2f} vs vwap {vwap:.2f} "
                    f"({(px - vwap) / atr:+.2f} atr), atr {atr_pct:.3f}%"),
        )
