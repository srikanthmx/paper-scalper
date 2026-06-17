"""Pure in-memory paper order simulator.

This module deliberately imports nothing network-related. It cannot place real
orders because it has no transport — fills are computed from the live quote
stream plus configured slippage and fees. tests/test_no_real_orders.py enforces
the no-network property.

Two execution modes per position (set by the entry signal):
- "simple":      full exit at SL / TP / breakeven / max-hold
- "scale_trail": the 1:2 ladder — at +2R close scale_out_frac, SL jumps to entry
                 (breakeven), new TP set 2R further (+4R); each later TP closes
                 another fraction, SL trails to the previous TP. The stop always
                 sits two rungs behind the target.

Marking convention (learned from live losses): exit LEVELS are anchored to the
quote MID at entry and TRIGGERED by the current mid — otherwise the spread rigs
the geometry (a long marked at bid starts most of the way to its stop while its
target needs the bid to travel 2R+spread; observed live as a ~3% win rate).
FILLS stay honest: bid/ask plus slippage, so costs still land in PnL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.strategy import Side, Signal


def _parse_lots(spec: str) -> tuple[int, ...]:
    return tuple(int(x) for x in spec.split(",") if x.strip())


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
    tp_hits: int = 0             # ladder rungs filled (scale_trail mode)
    breakeven_after_r: float = 0.6
    breakeven_armed: bool = False
    # lot model — every position is a whole number of equal lots
    position_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    lots_total: int = 1
    lots_remaining: float = 1.0
    lot_qty: float = 0.0         # qty per lot
    scale_lots: tuple[int, ...] = ()  # lots to cut at TP1, TP2, …; rest trails

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
    # lifecycle: ties partial fills of one position together and shows the trail
    position_id: str = ""
    lots: float = 0.0            # lots closed in THIS fill
    lots_left: float = 0.0       # lots still open after this fill
    sl_after: float = 0.0        # where the trailing stop sits after this event
    event: str = ""              # human label, e.g. "TP1: cut 2 lots, SL→entry"

    @property
    def net_pnl_pct(self) -> float:
        notional = self.entry_price * self.qty
        return self.net_pnl / notional * 100 if notional else 0.0


class PaperBroker:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.position: Position | None = None
        # lot sizing is per-broker so each lane can be tuned independently from the
        # dashboard; defaults come from cfg, overridden live via set_lots().
        self.use_lots = cfg.use_lots
        self.lots_per_entry = cfg.lots_per_entry
        self.lot_size_usd = cfg.lot_size_usd
        self.scale_lots = cfg.scale_lots

    def set_lots(self, *, use_lots: bool | None = None, lots_per_entry: int | None = None,
                 lot_size_usd: float | None = None, scale_lots: str | None = None) -> None:
        """Override this lane's lot sizing (from the dashboard). Ignores None fields."""
        if use_lots is not None:
            self.use_lots = bool(use_lots)
        if lots_per_entry is not None:
            self.lots_per_entry = max(1, int(lots_per_entry))
        if lot_size_usd is not None:
            self.lot_size_usd = max(1.0, float(lot_size_usd))
        if scale_lots is not None:
            self.scale_lots = scale_lots

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
        # lot mode: fixed lots of fixed notional; ignore the continuous qty.
        if self.use_lots:
            lots_total = self.lots_per_entry
            lot_qty = self.lot_size_usd / fill
            qty = lots_total * lot_qty
            scale_lots = _parse_lots(self.scale_lots)
        else:
            lots_total, lot_qty, scale_lots = 1, qty, ()
        self.position = Position(
            side=signal.side, qty=qty, entry_ts=quote.ts, entry_price=fill,
            sl_price=mid * (1 - sign * signal.sl_pct / 100),
            tp_price=mid * (1 + sign * signal.tp_pct / 100),
            entry_fee=self._fee(fill, qty), reason_entry=signal.reason,
            mode=signal.mode, scale_out_frac=signal.scale_out_frac,
            max_hold_seconds=signal.max_hold_seconds or self.cfg.max_hold_seconds,
            mid_ref=mid, r_dist=mid * signal.sl_pct / 100,
            breakeven_after_r=(signal.breakeven_after_r if signal.breakeven_after_r
                               is not None else self.cfg.breakeven_after_r),
            lots_total=lots_total, lots_remaining=float(lots_total), lot_qty=lot_qty,
            scale_lots=scale_lots,
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
            if sign * (mark - pos.mid_ref) >= pos.breakeven_after_r * pos.r_dist:
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
        if sign * (mark - pos.tp_price) >= 0:
            step = (sign * (pos.tp_price - pos.mid_ref) if pos.tp_hits == 0
                    else sign * (pos.tp_price - pos.sl_price) / 2)
            rung = pos.tp_hits
            pos.tp_hits += 1
            # SL trails: entry after rung 1 ("same price as bought"), then previous TP
            pos.sl_price = pos.mid_ref + sign * (pos.tp_hits - 1) * step
            pos.tp_price = pos.mid_ref + sign * (pos.tp_hits + 1) * step
            sl_label = "entry" if pos.tp_hits == 1 else f"+{pos.tp_hits - 1}R rung"
            if not self.use_lots:
                # continuous mode (backtests): bank scale_out_frac of the remainder
                return self._close_fraction(quote, pos.scale_out_frac, "partial_tp",
                                            f"TP{pos.tp_hits}, SL→{sl_label}")
            # lot mode: cut whole lots per schedule; remainder just trails
            cut = pos.scale_lots[rung] if rung < len(pos.scale_lots) else 0
            cut = min(cut, pos.lots_remaining)
            if cut > 0:
                event = (f"TP{pos.tp_hits}: cut {cut:g} lot{'s' if cut != 1 else ''}, "
                         f"SL→{sl_label}")
                trade = self._close_lots(quote, cut, "partial_tp", event)
                if pos.lots_remaining <= 1e-9:  # schedule emptied the position
                    self.position = None
                return trade
            return None  # schedule exhausted: the runner just trails, no close
        if sign * (mark - pos.sl_price) <= 0:
            reason = "trailing_stop" if pos.tp_hits > 0 else "stop_loss"
            return self._close(quote, reason)
        if quote.ts - pos.entry_ts >= pos.max_hold_seconds:
            return self._close(quote, "max_hold")
        return None

    def _close_fraction(self, quote: Quote, frac: float, reason: str,
                        event: str = "") -> ClosedTrade:
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
            position_id=pos.position_id, sl_after=pos.sl_price, event=event,
        )

    def _close_lots(self, quote: Quote, lots: float, reason: str,
                    event: str) -> ClosedTrade:
        pos = self.position
        assert pos is not None
        raw = quote.bid if pos.side == "long" else quote.ask
        fill = self._slip(raw, pos.side, entering=False)
        close_qty = lots * pos.lot_qty
        frac = close_qty / pos.qty if pos.qty else 0.0
        entry_fee_part = pos.entry_fee * frac
        exit_fee = self._fee(fill, close_qty)
        sign = 1.0 if pos.side == "long" else -1.0
        gross = sign * (fill - pos.entry_price) * close_qty
        pos.qty -= close_qty
        pos.entry_fee -= entry_fee_part
        pos.lots_remaining -= lots
        return ClosedTrade(
            side=pos.side, qty=close_qty, entry_ts=pos.entry_ts,
            entry_price=pos.entry_price, exit_ts=quote.ts, exit_price=fill,
            fees=entry_fee_part + exit_fee, gross_pnl=gross,
            net_pnl=gross - entry_fee_part - exit_fee,
            reason_entry=pos.reason_entry, reason_exit=reason,
            position_id=pos.position_id, lots=lots, lots_left=pos.lots_remaining,
            sl_after=pos.sl_price, event=event,
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
        lots = pos.lots_remaining
        label = {"trailing_stop": "trail stop", "stop_loss": "stopped",
                 "max_hold": "time exit", "take_profit": "target"}.get(reason, reason)
        return ClosedTrade(
            side=pos.side, qty=pos.qty, entry_ts=pos.entry_ts, entry_price=pos.entry_price,
            exit_ts=quote.ts, exit_price=fill, fees=fees, gross_pnl=gross,
            net_pnl=gross - fees, reason_entry=pos.reason_entry, reason_exit=reason,
            position_id=pos.position_id, lots=lots, lots_left=0.0,
            sl_after=pos.sl_price, event=f"{label}: {lots:g} lot{'s' if lots != 1 else ''} out",
        )
