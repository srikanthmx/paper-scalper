"""Lorentzian Classification scalper — adapted from jdehorty's famous TradingView
"Machine Learning: Lorentzian Classification" (the most-boosted script on TV).

Core idea: a k-nearest-neighbors classifier over a feature vector of oscillators,
using **Lorentzian distance** sum(log(1 + |a-b|)) instead of Euclidean — it
dampens outlier features (price shocks warp the feature space the way mass warps
spacetime, per the original's framing), which empirically picks better neighbors.

This adaptation:
- features: RSI(14), RSI(9), WaveTrend(10,21), CCI(20) — normalized to ~[0,1]
  (the original's 5th feature is ADX(20); omitted to keep the port lean)
- training labels: direction of close 4 candles ahead (as in the original)
- prediction: sum of the k=8 nearest neighbors' labels → [-8..+8]
- entry when |prediction| ≥ threshold; exits via ATR stop / 2R target / max-hold
- needs ~min_samples labeled candles before it predicts (≈100 minutes on 1-min
  candles) — it literally has to watch the market before it can classify it

Tunables (self.p) are hot-reloadable from the dashboard Tune tab.
"""

from __future__ import annotations

import math
from collections import deque

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.indicators import ATR, CCI, RSI, WaveTrend
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams

LOOKAHEAD = 4          # label = direction of close 4 bars later (original behavior)
K_NEIGHBORS = 8
MAX_HISTORY = 2000     # capped training memory (~33 hours of 1-min candles)

Features = tuple[float, float, float, float]


def lorentzian_distance(a: Features, b: Features) -> float:
    return sum(math.log1p(abs(x - y)) for x, y in zip(a, b))


class LorentzianStrategy(TunableParams):
    name = "lorentz"
    timeframe_seconds = 300  # 5m: best faithful-backtest TF (kNN noise-dominated below)

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.rsi14 = RSI(14)
        self.rsi9 = RSI(9)
        self.wt = WaveTrend(10, 21)
        self.cci = CCI(20)
        self.atr = ATR(cfg.atr_period)
        self.snapshot = Snapshot()
        self._history: deque[tuple[Features, int]] = deque(maxlen=MAX_HISTORY)
        self._pending: deque[tuple[Features, float, int]] = deque()  # (feat, close, left)
        # ORIGINAL Lorentzian exit: hold a fixed number of bars, then exit. No tight
        # ATR stop (that strangled it in the first, wrong implementation). A wide
        # safety stop only guards against a disaster move.
        self.p = {
            "pred_threshold": 5,       # |sum of 8 neighbor labels| required (max 8)
            "min_samples": 100,        # labeled candles needed before predicting
            "min_atr_pct": 0.005,
            "max_atr_pct": 1.50,
            "max_spread_bps": cfg.max_spread_bps,
            "hold_bars": 8,            # exit after this many bars (faithful exit)
            "safety_stop_pct": 1.50,   # wide disaster stop only
        }

    def _features(self) -> Features | None:
        if None in (self.rsi14.value, self.rsi9.value, self.wt.value, self.cci.value):
            return None
        return (
            self.rsi14.value / 100.0,
            self.rsi9.value / 100.0,
            math.tanh(self.wt.value / 60.0) * 0.5 + 0.5,
            math.tanh(self.cci.value / 200.0) * 0.5 + 0.5,
        )

    def _predict(self, query: Features) -> int:
        dists = sorted(
            (lorentzian_distance(query, feat), label) for feat, label in self._history
        )[:K_NEIGHBORS]
        return sum(label for _, label in dists)

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        self.rsi14.update(candle.close)
        self.rsi9.update(candle.close)
        self.wt.update(candle)
        self.cci.update(candle)
        atr = self.atr.update(candle)

        snap = Snapshot(close=candle.close, rsi=self.rsi14.value, atr=atr)
        self.snapshot = snap

        # resolve pending labels: a sample matures LOOKAHEAD candles after capture
        matured: list[tuple[Features, float, int]] = []
        for feat, ref_close, left in self._pending:
            left -= 1
            matured.append((feat, ref_close, left))
        self._pending = deque(x for x in matured if x[2] > 0)
        for feat, ref_close, left in matured:
            if left <= 0 and candle.close != ref_close:
                self._history.append((feat, 1 if candle.close > ref_close else -1))

        features = self._features()
        if features is None or atr is None:
            snap.rejects.append("warming_up")
            return None
        self._pending.append((features, candle.close, LOOKAHEAD))

        if len(self._history) < p["min_samples"]:
            snap.rejects.append(f"learning ({len(self._history)}/{p['min_samples']:.0f} samples)")
            return None

        px = candle.close
        atr_pct = atr / px * 100
        if quote is not None and quote.spread_bps > p["max_spread_bps"]:
            snap.rejects.append(f"spread {quote.spread_bps:.1f}bps > {p['max_spread_bps']}")
            return None
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            snap.rejects.append(f"atr {atr_pct:.3f}% outside band")
            return None

        prediction = self._predict(features)
        if abs(prediction) < p["pred_threshold"]:
            snap.rejects.append(f"weak prediction ({prediction:+d}/{K_NEIGHBORS})")
            return None

        side = "long" if prediction > 0 else "short"
        # faithful exit: hold hold_bars then time-out; wide safety stop / far target
        # so the trade is decided by the holding period, not a tight stop
        hold = int(p["hold_bars"]) * self.timeframe_seconds
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=p["safety_stop_pct"], tp_pct=p["safety_stop_pct"] * 3,
            max_hold_seconds=hold,
            reason=(f"{side} lorentzian kNN: prediction {prediction:+d}/{K_NEIGHBORS} "
                    f"({len(self._history)} samples), hold {int(p['hold_bars'])} bars, "
                    f"rsi {self.rsi14.value:.1f}, atr {atr_pct:.3f}%"),
        )
