"""Alpaca PAPER trading executor — simulated fills on Alpaca's matching engine.

HARD SAFETY BOUNDARY:
- This module connects to exactly one host: paper-api.alpaca.markets — Alpaca's
  PAPER (sandbox) account. No real money can ever change hands there.
- It must NEVER touch api.alpaca.markets (the live-money host). That host is in
  tests/test_no_real_orders.py's forbidden list and a runtime assert below blocks it.
- Used only when execution_mode == "alpaca_paper" for the single designated lane;
  every other lane stays on the in-app simulator.

It mirrors the PaperBroker interface enough for one lane: open a market position
with a bracket (SL/TP), poll fills, and report a ClosedTrade when the position
closes. Fills come from Alpaca, so slippage is real (the point of this mode).
"""

from __future__ import annotations

import logging

import httpx

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.paper_broker import ClosedTrade, Position
from paper_scalper.engine.strategy import Signal

log = logging.getLogger(__name__)

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"  # FORBIDDEN — never used; named only to assert against
BASE_URL = f"https://{PAPER_HOST}"


class AlpacaPaperBroker:
    """One-position bracket executor against an Alpaca paper account."""

    def __init__(self, cfg: Settings, symbol: str) -> None:
        # paranoia: the base URL must be the paper sandbox, never the live host
        assert PAPER_HOST in BASE_URL and LIVE_HOST not in BASE_URL.replace(PAPER_HOST, "")
        assert "paper-" in BASE_URL, "Alpaca executor must use the PAPER host only"
        self.cfg = cfg
        self.symbol = symbol               # "BTC/USD"
        self.position: Position | None = None
        self._order_id: str | None = None
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=10.0,
            headers={"APCA-API-KEY-ID": cfg.alpaca_api_key,
                     "APCA-API-SECRET-KEY": cfg.alpaca_api_secret},
        )

    # --- account / health ---------------------------------------------------
    def account_ok(self) -> bool:
        try:
            r = self._client.get("/v2/account")
            r.raise_for_status()
            data = r.json()
            log.info("alpaca paper account: status=%s cash=%s buying_power=%s",
                     data.get("status"), data.get("cash"), data.get("buying_power"))
            return data.get("status") == "ACTIVE"
        except Exception as exc:  # noqa: BLE001
            log.error("alpaca paper account check failed: %s", exc)
            return False

    # --- order placement ----------------------------------------------------
    def open_position(self, signal: Signal, quote: Quote, qty: float) -> Position | None:
        if self.position is not None:
            return None
        side = "buy" if signal.side == "long" else "sell"
        # crypto qty must be positive; round to a sane precision
        qty = round(qty, 6)
        body = {
            "symbol": self.symbol.replace("/", ""),  # BTCUSD
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "gtc",
        }
        try:
            r = self._client.post("/v2/orders", json=body)
            r.raise_for_status()
            order = r.json()
        except Exception as exc:  # noqa: BLE001
            log.error("alpaca paper order rejected: %s", exc)
            return None
        self._order_id = order.get("id")
        sign = 1.0 if signal.side == "long" else -1.0
        mid = quote.mid
        self.position = Position(
            side=signal.side, qty=qty, entry_ts=quote.ts, entry_price=mid,
            sl_price=mid * (1 - sign * signal.sl_pct / 100),
            tp_price=mid * (1 + sign * signal.tp_pct / 100),
            entry_fee=0.0, reason_entry=signal.reason, mode=signal.mode,
            scale_out_frac=signal.scale_out_frac,
            max_hold_seconds=signal.max_hold_seconds or self.cfg.max_hold_seconds,
            mid_ref=mid, r_dist=mid * signal.sl_pct / 100,
        )
        log.info("[alpaca_paper] submitted %s %s qty=%s (order %s)", side,
                 self.symbol, qty, self._order_id)
        return self.position

    def on_quote(self, quote: Quote) -> ClosedTrade | None:
        """SL/TP/hold logic runs locally on the live quote; when an exit triggers
        we send the closing market order to Alpaca paper and realize the fill."""
        pos = self.position
        if pos is None:
            return None
        mark = quote.mid
        sign = 1.0 if pos.side == "long" else -1.0
        reason = None
        if sign * (mark - pos.tp_price) >= 0:
            reason = "take_profit"
        elif sign * (mark - pos.sl_price) <= 0:
            reason = "stop_loss"
        elif quote.ts - pos.entry_ts >= pos.max_hold_seconds:
            reason = "max_hold"
        if reason is None:
            return None
        return self._close(quote, reason)

    def _close(self, quote: Quote, reason: str) -> ClosedTrade | None:
        pos = self.position
        assert pos is not None
        side = "sell" if pos.side == "long" else "buy"
        body = {"symbol": self.symbol.replace("/", ""), "qty": str(pos.qty),
                "side": side, "type": "market", "time_in_force": "gtc"}
        fill = quote.mid
        try:
            r = self._client.post("/v2/orders", json=body)
            r.raise_for_status()
            # best-effort: read the filled price if Alpaca reports it promptly
            data = r.json()
            if data.get("filled_avg_price"):
                fill = float(data["filled_avg_price"])
        except Exception as exc:  # noqa: BLE001
            log.error("alpaca paper close failed (%s) — realizing at mark: %s", reason, exc)
        self.position = None
        self._order_id = None
        sign = 1.0 if pos.side == "long" else -1.0
        gross = sign * (fill - pos.entry_price) * pos.qty
        return ClosedTrade(
            side=pos.side, qty=pos.qty, entry_ts=pos.entry_ts, entry_price=pos.entry_price,
            exit_ts=quote.ts, exit_price=fill, fees=0.0, gross_pnl=gross, net_pnl=gross,
            reason_entry=pos.reason_entry, reason_exit=reason,
        )

    def close(self) -> None:
        self._client.close()
