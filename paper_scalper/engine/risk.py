from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from paper_scalper.config import Settings


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskManager:
    """Gates entries and sizes positions. Tracks daily loss, loss streaks, cooldowns."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.equity = cfg.starting_equity
        self.day_start_equity = cfg.starting_equity
        self.consecutive_losses = 0
        self.halted_reason: str | None = None
        self._day: int | None = None
        self._cooldown = 0

    def _roll_day(self, ts: float) -> None:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).toordinal()
        if day != self._day:
            self._day = day
            self.day_start_equity = self.equity
            self.consecutive_losses = 0
            self.halted_reason = None
            self._cooldown = 0

    def can_enter(self, ts: float) -> RiskDecision:
        self._roll_day(ts)
        if not self.cfg.enable_risk_halts:
            return RiskDecision(True, "ok (learning mode, halts disabled)")
        if self.halted_reason:
            return RiskDecision(False, self.halted_reason)
        if self._cooldown > 0:
            return RiskDecision(False, f"cooldown ({self._cooldown} candles left)")
        return RiskDecision(True, "ok")

    def position_size(self, price: float, sl_pct: float) -> float:
        """Qty such that hitting the SL loses risk_per_trade_pct of equity, capped by notional."""
        risk_amount = self.equity * self.cfg.risk_per_trade_pct / 100
        sl_distance = price * sl_pct / 100
        if sl_distance <= 0:
            return 0.0
        qty = risk_amount / sl_distance
        max_qty = self.equity * self.cfg.max_notional_fraction / price
        return max(min(qty, max_qty), 0.0)

    def on_candle(self) -> None:
        if self._cooldown > 0:
            self._cooldown -= 1

    def on_trade_closed(self, pnl: float, ts: float) -> None:
        self._roll_day(ts)
        self.equity += pnl
        self._cooldown = self.cfg.cooldown_candles
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if not self.cfg.enable_risk_halts:
            return
        day_pnl_pct = (self.equity - self.day_start_equity) / self.day_start_equity * 100
        if day_pnl_pct <= -self.cfg.daily_stop_pct:
            self.halted_reason = f"daily stop hit ({day_pnl_pct:.2f}%)"
        elif self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halted_reason = f"{self.consecutive_losses} consecutive losses"
