"""Trend-follow scalper with strict 1:2 and scale-out/trail exits.

Entry: simple trend follow — EMA9 above/below EMA21 with separation, price on
the trend side of EMA9, candle closing with the trend.

Exit (handled by the broker's scale_trail mode):
- initial SL at 1R (1x ATR, clamped)
- at +2R: close half, jump SL to +1R (a stop-out from here is still a winner)
- trail the remaining half by 1R off the best price
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA
from paper_scalper.engine.strategy import Signal, Snapshot


class TrendScalpStrategy:
    name = "trend"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.ema_fast = EMA(cfg.ema_fast)
        self.ema_slow = EMA(cfg.ema_slow)
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        cfg = self.cfg
        ema_f = self.ema_fast.update(candle.close)
        ema_s = self.ema_slow.update(candle.close)
        atr = self.atr.update(candle)

        snap = Snapshot(close=candle.close, ema_fast=ema_f, ema_slow=ema_s, atr=atr)
        self.snapshot = snap

        if None in (ema_f, ema_s, atr):
            snap.rejects.append("warming_up")
            return None
        assert ema_f is not None and ema_s is not None and atr is not None

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > cfg.max_spread_bps:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {cfg.max_spread_bps}")
            return None
        if not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None

        sep_bps = (ema_f - ema_s) / px * 10_000
        side = None
        if sep_bps >= cfg.ema_sep_min_bps and px > ema_f and px > candle.open:
            side = "long"
        elif -sep_bps >= cfg.ema_sep_min_bps and px < ema_f and px < candle.open:
            side = "short"
        if side is None:
            snap.rejects.append(f"no trend candle (ema sep {sep_bps:+.1f}bps)")
            return None

        sl_pct = min(max(cfg.trend_sl_atr_mult * atr_pct, cfg.trend_sl_min_pct),
                     cfg.trend_sl_max_pct)
        return Signal(
            side=side, ts=candle.ts_open + cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=cfg.trend_rr * sl_pct,
            mode="scale_trail", scale_out_frac=cfg.trend_scale_out_frac,
            max_hold_seconds=cfg.trend_max_hold_seconds,
            reason=(f"{side} trend: ema sep {sep_bps:+.1f}bps, px {px:.2f} vs ema9 "
                    f"{ema_f:.2f}, 1R={sl_pct:.3f}% TP1=+{cfg.trend_rr:.0f}R "
                    f"(half off, SL->+1R, trail 1R)"),
        )
