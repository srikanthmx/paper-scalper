"""Scalping signal engine — pullback-to-EMA9 trend scalper (v1.1).

Shared Signal/Snapshot types for all strategies live here.
Tunable params (self.p) are hot-reloadable from the dashboard; see tunable.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA, RSI, RollingMean, SessionVWAP
from paper_scalper.engine.tunable import TunableParams

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class Signal:
    side: Side
    ts: float
    ref_price: float
    sl_pct: float  # stop distance as % of entry
    tp_pct: float  # target distance as % of entry
    reason: str
    # execution mode: "simple" = full exit at SL/TP/hold;
    # "scale_trail" = close scale_out_frac at TP, SL jumps to +1R, rest trails by 1R
    mode: str = "simple"
    scale_out_frac: float = 0.5
    max_hold_seconds: int | None = None  # per-signal override of cfg.max_hold_seconds
    # entry execution: "market" fills on the next quote after the signal;
    # "stop" arms a pending order the engine fills on the TICK that crosses
    # entry_stop (true tick-level entries; opposite-side pendings are OCO)
    entry_type: str = "market"
    entry_stop: float | None = None
    valid_seconds: int = 60              # pending order lifetime
    allow_tight_stop: bool = False       # bypass the stop-inside-spread guard (test lane)
    breakeven_after_r: float | None = None  # override cfg; set huge to disable breakeven


@dataclass(slots=True)
class Snapshot:
    """Indicator state after the last completed candle (for journaling/dashboard)."""

    close: float = 0.0
    vwap: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    atr: float | None = None
    vol_avg: float | None = None
    rejects: list[str] = field(default_factory=list)


class Strategy(TunableParams):
    """Pullback-to-EMA9 trend scalper (v1.1)."""

    name = "pullback"
    timeframe_seconds = 60  # each strategy declares the candle TF it was designed for

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.vwap = SessionVWAP()
        self.ema_fast = EMA(cfg.ema_fast)
        self.ema_slow = EMA(cfg.ema_slow)
        self.rsi = RSI(cfg.rsi_period)
        self.atr = ATR(cfg.atr_period)
        self.vol_avg = RollingMean(cfg.vol_sma_period)
        self._prev_candle: Candle | None = None
        self.snapshot = Snapshot()
        self.p = {
            "vol_spike_mult": cfg.vol_spike_mult,
            "rsi_long_min": cfg.rsi_long_min,
            "rsi_long_max": cfg.rsi_long_max,
            "rsi_short_min": cfg.rsi_short_min,
            "rsi_short_max": cfg.rsi_short_max,
            "ema_sep_min_bps": cfg.ema_sep_min_bps,
            "pullback_tolerance_pct": cfg.pullback_tolerance_pct,
            "min_atr_pct": cfg.min_atr_pct,
            "max_atr_pct": cfg.max_atr_pct,
            "max_spread_bps": cfg.max_spread_bps,
            "sl_atr_mult": cfg.sl_atr_mult,
            "tp_atr_mult": cfg.tp_atr_mult,
            "sl_min_pct": cfg.sl_min_pct,
            "sl_max_pct": cfg.sl_max_pct,
            "tp_min_pct": cfg.tp_min_pct,
            "tp_max_pct": cfg.tp_max_pct,
            "rr": 2.0,                # TP1 at +2R, then trail (scale-trail ladder)
            "scale_out_frac": 0.5,
            "max_hold_seconds": cfg.max_hold_seconds,
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        vwap = self.vwap.update(candle)
        ema_f = self.ema_fast.update(candle.close)
        ema_s = self.ema_slow.update(candle.close)
        rsi = self.rsi.update(candle.close)
        # volume average must lag the current candle, else a spike inflates its own baseline
        vol_avg = self.vol_avg.value
        self.vol_avg.update(candle.volume)
        atr = self.atr.update(candle)
        prev = self._prev_candle
        self._prev_candle = candle

        snap = Snapshot(close=candle.close, vwap=vwap, ema_fast=ema_f, ema_slow=ema_s,
                        rsi=rsi, atr=atr, vol_avg=vol_avg)
        self.snapshot = snap

        # NB: values like vol_avg can legitimately be 0.0 — only None means not warm
        if None in (vwap, ema_f, ema_s, rsi, atr, vol_avg) or prev is None:
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
        if vol_avg <= 0 or candle.volume < p["vol_spike_mult"] * vol_avg:
            snap.rejects.append("no volume spike")
            return None

        sep_bps = (ema_f - ema_s) / px * 10_000
        tol = px * p["pullback_tolerance_pct"] / 100

        side: Side | None = None
        if (
            px > vwap
            and sep_bps >= p["ema_sep_min_bps"]
            and p["rsi_long_min"] <= rsi <= p["rsi_long_max"]
            and min(prev.low, candle.low) <= ema_f + tol  # pulled back to EMA9...
            and px > ema_f                                # ...and reclaimed it
            and px > candle.open                          # closing in trend direction
        ):
            side = "long"
        elif (
            px < vwap
            and -sep_bps >= p["ema_sep_min_bps"]
            and p["rsi_short_min"] <= rsi <= p["rsi_short_max"]
            and max(prev.high, candle.high) >= ema_f - tol  # rallied into EMA9...
            and px < ema_f                                  # ...and got rejected
            and px < candle.open
        ):
            side = "short"

        if side is None:
            snap.rejects.append("no setup")
            return None

        # scale-trail ladder — the trend lane proved this structure wins (+$133 live)
        sl_pct = min(max(p["sl_atr_mult"] * atr_pct, p["sl_min_pct"]), p["sl_max_pct"])
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=p["rr"] * sl_pct,
            mode="scale_trail", scale_out_frac=p["scale_out_frac"],
            max_hold_seconds=int(p["max_hold_seconds"]),
            reason=(f"{side}: px {px:.2f} vs vwap {vwap:.2f}, ema sep {sep_bps:.1f}bps, "
                    f"rsi {rsi:.1f}, vol {candle.volume:.3f} vs avg {vol_avg:.3f}, "
                    f"atr {atr_pct:.3f}% (1:{p['rr']} ladder)"),
        )
