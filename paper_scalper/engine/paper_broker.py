"""Pure in-memory paper order simulator.

This module deliberately imports nothing network-related. It cannot place real
orders because it has no transport — fills are computed from the live quote
stream plus configured slippage and fees. tests/test_no_real_orders.py enforces
the no-network property.

Two execution modes per position (set by the entry signal):
- "simple":      full exit at SL / TP / breakeven / max-hold
- "scale_trail": at TP (e.g. +2R) close scale_out_frac of the position, jump the
                 stop to +1R, then trail the remainder by 1R off the best price

Marking convention (learned from live losses): exit LEVELS are anchored to the
quote MID at entry and TRIGGERED by the current mid — otherwise the spread rigs
the geometry (a long marked at bid starts most of the way to its stop while its
target needs the bid to travel 2R+spread; observed live as a ~3% win rate).
FILLS stay honest: bid/ask plus slippage, so costs still land in PnL.
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
    mode: str = "simple"
    scale_out_frac: float = 0.5
    max_hold_seconds: int = 300
    mid_ref: float = 0.0         # quote mid at entry — anchor for all exit levels
    r_dist: float = 0.0          # initial risk distance in mid terms
    tp1_filled: bool = False
    best_px: float = 0.0         # best mid since TP1 (trailing anchor)
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
        mid = quote.mid
        self.position = Position(
            side=signal.side, qty=qty, entry_ts=quote.ts, entry_price=fill,
            sl_price=mid * (1 - sign * signal.sl_pct / 100),
            tp_price=mid * (1 + sign * signal.tp_pct / 100),
            entry_fee=self._fee(fill, qty), reason_entry=signal.reason,
            mode=signal.mode, scale_out_frac=signal.scale_out_frac,
            max_hold_seconds=signal.max_hold_seconds or self.cfg.max_hold_seconds,
            mid_ref=mid, r_dist=mid * signal.sl_pct / 100,
        )
        return self.position

    def on_quote(self, quote: Quote) -> ClosedTrade | None:
        pos = self.position
        if pos is None:
            return None
        mark = quote.mid  # levels trigger on mid; fills execute at bid/ask
        sign = 1.0 if pos.side == "long" else -1.0

        if pos.mode == "scale_trail":
            return self._on_quote_scale_trail(quote, pos, mark, sign)

        # ── simple mode ──
        # breakeven: once gain >= breakeven_after_r * initial risk, SL moves to entry mid
        if not pos.breakeven_armed:
            if sign * (mark - pos.mid_ref) >= self.cfg.breakeven_after_r * pos.r_dist:
                pos.sl_price = pos.mid_ref
                pos.breakeven_armed = True
        if sign * (mark - pos.tp_price) >= 0:
            return self._close(quote, "take_profit")
        if sign * (mark - pos.sl_price) <= 0:
            reason = "breakeven_stop" if pos.breakeven_armed else "stop_loss"
            return self._close(quote, reason)
        if quote.ts - pos.entry_ts >= pos.max_hold_seconds:
            return self._close(quote, "max_hold")
        return None

    def _on_quote_scale_trail(self, quote: Quote, pos: Position, mark: float,
                              sign: float) -> ClosedTrade | None:
        if not pos.tp1_filled:
            if sign * (mark - pos.tp_price) >= 0:
                # take scale_out_frac off at TP1, stop jumps to +1R, start trailing
                trade = self._close_fraction(quote, pos.scale_out_frac, "partial_tp")
                pos.tp1_filled = True
                pos.best_px = mark
                pos.sl_price = pos.mid_ref + sign * pos.r_dist  # lock +1R (mid terms)
                return trade
            if sign * (mark - pos.sl_price) <= 0:
                return self._close(quote, "stop_loss")
        else:
            # trail the remainder by 1R off the best price; never loosen the stop
            pos.best_px = max(pos.best_px, mark) if sign > 0 else min(pos.best_px, mark)
            trail = pos.best_px - sign * pos.r_dist
            pos.sl_price = max(pos.sl_price, trail) if sign > 0 else min(pos.sl_price, trail)
            if sign * (mark - pos.sl_price) <= 0:
                return self._close(quote, "trailing_stop")
        if quote.ts - pos.entry_ts >= pos.max_hold_seconds:
            return self._close(quote, "max_hold")
        return None

    def _close_fraction(self, quote: Quote, frac: float, reason: str) -> ClosedTrade:
        pos = self.position
        assert pos is not None
        raw = quote.bid if pos.side == "long" else quote.ask
        fill = self._slip(raw, pos.side, entering=False)
        close_qty = pos.qty * frac
        entry_fee_part = pos.entry_fee * frac
        exit_fee = self._fee(fill, close_qty)
        sign = 1.0 if pos.side == "long" else -1.0
        gross = sign * (fill - pos.entry_price) * close_qty
        pos.qty -= close_qty
        pos.entry_fee -= entry_fee_part
        return ClosedTrade(
            side=pos.side, qty=close_qty, entry_ts=pos.entry_ts,
            entry_price=pos.entry_price, exit_ts=quote.ts, exit_price=fill,
            fees=entry_fee_part + exit_fee, gross_pnl=gross,
            net_pnl=gross - entry_fee_part - exit_fee,
            reason_entry=pos.reason_entry, reason_exit=reason,
        )

    def _close(self, quote: Quote, reason: str) -> ClosedTrade:
        pos = self.position
        assert pos is not None
        self.position = None
        trade = self._final_close(pos, quote, reason)
        return trade

    def _final_close(self, pos: Position, quote: Quote, reason: str) -> ClosedTrade:
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
