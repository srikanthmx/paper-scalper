"""Pure in-memory paper order simulator.

This module deliberately imports nothing network-related. It cannot place real
orders because it has no transport — fills are computed from the live quote
stream plus configured slippage and fees. tests/test_no_real_orders.py enforces
the no-network property.
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.strategy import Side, Signal


@dataclass(slots=True)
class Position:
    side: Side
    qty: float
    entry_ts: float
    entry_price: float  # fill incl. slippage
    sl_price: float
    tp_price: float
    entry_fee: float
    reason_entry: str
    breakeven_armed: bool = False

    def unrealized(self, quote: Quote) -> float:
        exit_px = quote.bid if self.side == "long" else quote.ask
        sign = 1.0 if self.side == "long" else -1.0
        return sign * (exit_px - self.entry_price) * self.qty - self.entry_fee


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    side: Side
    qty: float
    entry_ts: float
    entry_price: float
    exit_ts: float
    exit_price: float
    fees: float
    gross_pnl: float
    net_pnl: float
    reason_entry: str
    reason_exit: str

    @property
    def net_pnl_pct(self) -> float:
        notional = self.entry_price * self.qty
        return self.net_pnl / notional * 100 if notional else 0.0


class PaperBroker:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.position: Position | None = None

    def _fee(self, price: float, qty: float) -> float:
        return price * qty * self.cfg.fee_bps / 10_000

    def _slip(self, price: float, side: Side, entering: bool) -> float:
        slip = price * self.cfg.slippage_bps / 10_000
        # slippage always works against you
        worse_for_buy = (side == "long") == entering
        return price + slip if worse_for_buy else price - slip

    def open_position(self, signal: Signal, quote: Quote, qty: float) -> Position:
        if self.position is not None:
            raise RuntimeError("position already open")
        raw = quote.ask if signal.side == "long" else quote.bid
        fill = self._slip(raw, signal.side, entering=True)
        sign = 1.0 if signal.side == "long" else -1.0
        self.position = Position(
            side=signal.side, qty=qty, entry_ts=quote.ts, entry_price=fill,
            sl_price=fill * (1 - sign * signal.sl_pct / 100),
            tp_price=fill * (1 + sign * signal.tp_pct / 100),
            entry_fee=self._fee(fill, qty), reason_entry=signal.reason,
        )
        return self.position

    def on_quote(self, quote: Quote) -> ClosedTrade | None:
        pos = self.position
        if pos is None:
            return None
        mark = quote.bid if pos.side == "long" else quote.ask
        sign = 1.0 if pos.side == "long" else -1.0

        # breakeven: once gain >= breakeven_after_r * initial risk, SL moves to entry
        if not pos.breakeven_armed:
            risk = abs(pos.entry_price - pos.sl_price)
            if sign * (mark - pos.entry_price) >= self.cfg.breakeven_after_r * risk:
                pos.sl_price = pos.entry_price
                pos.breakeven_armed = True

        if sign * (mark - pos.tp_price) >= 0:
            return self._close(quote, "take_profit")
        if sign * (mark - pos.sl_price) <= 0:
            reason = "breakeven_stop" if pos.breakeven_armed else "stop_loss"
            return self._close(quote, reason)
        if quote.ts - pos.entry_ts >= self.cfg.max_hold_seconds:
            return self._close(quote, "max_hold")
        return None

    def _close(self, quote: Quote, reason: str) -> ClosedTrade:
        pos = self.position
        assert pos is not None
        self.position = None
        raw = quote.bid if pos.side == "long" else quote.ask
        fill = self._slip(raw, pos.side, entering=False)
        exit_fee = self._fee(fill, pos.qty)
        sign = 1.0 if pos.side == "long" else -1.0
        gross = sign * (fill - pos.entry_price) * pos.qty
        fees = pos.entry_fee + exit_fee
        return ClosedTrade(
            side=pos.side, qty=pos.qty, entry_ts=pos.entry_ts, entry_price=pos.entry_price,
            exit_ts=quote.ts, exit_price=fill, fees=fees, gross_pnl=gross,
            net_pnl=gross - fees, reason_entry=pos.reason_entry, reason_exit=reason,
        )
