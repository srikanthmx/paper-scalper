"""Scalping signal engine (v1.1 — improved over the original spec).

Improvements vs spec v1 (rationale in STRATEGY.md):
- EMA separation floor (avoid bare-crossover chop entries)
- spread filter (skip when bid/ask spread makes scalping uneconomic)
- ATR band (skip dead chop and news spikes)
- ATR-adaptive SL/TP, clamped to the spec's percentage bounds
- explicit pullback-to-EMA9 entry with reclaim, both sides
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, EMA, RSI, RollingMean, SessionVWAP

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


class Strategy:
    """Pullback-to-EMA9 trend scalper (v1.1)."""

    name = "pullback"

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

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        cfg = self.cfg
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

        if quote is not None and quote.spread_bps > cfg.max_spread_bps:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {cfg.max_spread_bps}")
            return None
        if not (cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None
        if vol_avg <= 0 or candle.volume < cfg.vol_spike_mult * vol_avg:
            snap.rejects.append("no volume spike")
            return None

        sep_bps = (ema_f - ema_s) / px * 10_000
        tol = px * cfg.pullback_tolerance_pct / 100

        side: Side | None = None
        if (
            px > vwap
            and sep_bps >= cfg.ema_sep_min_bps
            and cfg.rsi_long_min <= rsi <= cfg.rsi_long_max
            and min(prev.low, candle.low) <= ema_f + tol  # pulled back to EMA9...
            and px > ema_f                                # ...and reclaimed it
            and px > candle.open                          # closing in trend direction
        ):
            side = "long"
        elif (
            px < vwap
            and -sep_bps >= cfg.ema_sep_min_bps
            and cfg.rsi_short_min <= rsi <= cfg.rsi_short_max
            and max(prev.high, candle.high) >= ema_f - tol  # rallied into EMA9...
            and px < ema_f                                  # ...and got rejected
            and px < candle.open
        ):
            side = "short"

        if side is None:
            snap.rejects.append("no setup")
            return None

        sl_pct = min(max(cfg.sl_atr_mult * atr_pct, cfg.sl_min_pct), cfg.sl_max_pct)
        tp_pct = min(max(cfg.tp_atr_mult * atr_pct, cfg.tp_min_pct), cfg.tp_max_pct)
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=sl_pct, tp_pct=tp_pct,
            reason=(f"{side}: px {px:.2f} vs vwap {vwap:.2f}, ema sep {sep_bps:.1f}bps, "
                    f"rsi {rsi:.1f}, vol {candle.volume:.3f} vs avg {vol_avg:.3f}, "
                    f"atr {atr_pct:.3f}%"),
        )
