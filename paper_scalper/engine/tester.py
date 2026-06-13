"""Execution test harness — NOT a real strategy.

Fires every candle while flat: enters in the candle's direction, fixed-dollar
target and stop (default +$50 / -$25 on BTC). Used to eyeball that the trade
machinery opens, hits TP/SL, and journals correctly. Bypasses the spread guard
on purpose (its $25 stop is intentionally tight).
"""

from __future__ import annotations

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.candles import Candle
from paper_scalper.engine.strategy import Signal, Snapshot
from paper_scalper.engine.tunable import TunableParams


class TesterStrategy(TunableParams):
    name = "tester"
    timeframe_seconds = 60

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.snapshot = Snapshot()
        self.p = {
            "target_usd": 20.0,
            "stop_usd": 10.0,
            "max_hold_seconds": 300,
            "follow_candle": 1,   # 1: trade candle direction; 0: always long
        }

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None:
        p = self.p
        self.snapshot = Snapshot(close=candle.close)
        px = candle.close
        if px <= 0:
            return None
        side = "long"
        if p["follow_candle"]:
            side = "long" if candle.close >= candle.open else "short"
        return Signal(
            side=side, ts=candle.ts_open + self.cfg.candle_seconds, ref_price=px,
            sl_pct=p["stop_usd"] / px * 100, tp_pct=p["target_usd"] / px * 100,
            max_hold_seconds=int(p["max_hold_seconds"]), allow_tight_stop=True,
            breakeven_after_r=999.0,  # disabled: clean TP/SL/max-hold test, no breakeven exits
            reason=(f"{side} TEST: entry ~{px:.0f}, target {px + (1 if side=='long' else -1)*p['target_usd']:.0f}, "
                    f"stop {px - (1 if side=='long' else -1)*p['stop_usd']:.0f} "
                    f"(+${p['target_usd']:.0f}/-${p['stop_usd']:.0f})"),
        )
