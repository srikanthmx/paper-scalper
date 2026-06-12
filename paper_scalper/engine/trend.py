"""Trend-follow scalper with strict 1:2 and scale-out/trail exits. AGGRESSIVE.

Entry: any candle closing with the trend while EMA9 is beyond EMA21 — tuned to
take the smallest opportunity (tiny separation floor, tiny ATR floor, small 1R).

Exit (broker scale_trail mode):
- initial SL at 1R (1x ATR, clamped small)
- at +2R: close half, jump SL to +1R (a stop-out from here is still a winner)
- trail the remaining half by 1R off the best price

Tunable params (self.p) are hot-reloadable from the dashboard.
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams


class TrendScalpStrategy(TunableParams):
    name = "trend"

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.ema_fast = EMA(cfg.ema_fast)
        self.ema_slow = EMA(cfg.ema_slow)
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()
        self.p = {
            "ema_sep_min_bps": 0.5,          # aggressive: any real separation counts
            "min_atr_pct": 0.005,            # aggressive: trade the quietest tape
            "max_atr_pct": cfg.max_atr_pct,
            "max_spread_bps": cfg.max_spread_bps,
            "sl_atr_mult": cfg.trend_sl_atr_mult,
            "sl_min_pct": 0.08,              # aggressive: small 1R, smallest scalps
            "sl_max_pct": cfg.trend_sl_max_pct,
            "rr": cfg.trend_rr,              # TP1 at +rr R (strict 1:2 by default)
            "scale_out_frac": cfg.trend_scale_out_frac,
            "max_hold_seconds": cfg.trend_max_hold_seconds,
            "require_trend_close": 1,        # 1: candle must close with trend; 0: any candle
            "max_candle_atr_mult": 3.0,      # chase guard: skip entries after blow-off candles
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        ema_f = self.ema_fast.update(candle.close)
        ema_s = self.ema_slow.update(candle.close)
        atr = self.atr.update(candle)

        snap = Snapshot(close=candle.close, ema_fast=ema_f, ema_slow=ema_s, atr=atr)
        self.snapshot = snap

        if None in (ema_f, ema_s, atr):
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
        if candle.high - candle.low > p["max_candle_atr_mult"] * atr:
            snap.rejects.append("chase guard: blow-off candle, waiting for digestion")
            return None

        sep_bps = (ema_f - ema_s) / px * 10_000
        with_trend_ok = not p["require_trend_close"]
        side = None
        if sep_bps >= p["ema_sep_min_bps"] and px > ema_f and (with_trend_ok or px > candle.open):
            side = "long"
        elif -sep_bps >= p["ema_sep_min_bps"] and px < ema_f and (with_trend_ok or px < candle.open):
            side = "short"
        if side is None:
            snap.rejects.append(f"no trend candle (ema sep {sep_bps:+.1f}bps)")
            return None

        sl_pct = min(max(p["sl_atr_mult"] * atr_pct, p["sl_min_pct"]), p["sl_max_pct"])
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=p["rr"] * sl_pct,
            mode="scale_trail", scale_out_frac=p["scale_out_frac"],
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side} trend: ema sep {sep_bps:+.1f}bps, px {px:.2f} vs ema9 "
                    f"{ema_f:.2f}, 1R={sl_pct:.3f}% TP1=+{p['rr']:.0f}R "
                    f"(half off, SL->+1R, trail 1R)"),
        )
