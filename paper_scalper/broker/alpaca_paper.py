"""Alpaca PAPER trading executor — real fills on Alpaca's sandbox matching engine.

Architecture: this is a THIN execution adapter. The trading *brain* — lot sizing,
the scale-out/trailing ladder, mid-anchored SL/TP, max-hold — lives entirely in
PaperBroker and is INHERITED here. The only thing this class changes is how a fill
price is obtained: instead of modelling slippage locally, it places a real market
order on the Alpaca paper account and reads the actual fill. So whatever the in-app
logic does (open 4 lots, cut 2 at TP1, trail the last), the exact same orders are
sent to Alpaca and journaled with the real slippage.

HARD SAFETY BOUNDARY:
- Connects to exactly one host: paper-api.alpaca.markets (PAPER / sandbox). No real
  money can change hands there.
- Must NEVER touch api.alpaca.markets (live). That host is in
  tests/test_no_real_orders.py's forbidden list and a runtime assert blocks it.

VENUE CONSTRAINTS (handled, not worked around):
- Alpaca crypto cannot be shorted — short entries are refused (returns None, the
  engine logs a risk_block and the lane simply skips that signal on this venue).
- Alpaca PAPER crypto does NOT fill market orders (verified live: they sit in
  status=new indefinitely). We send a MARKETABLE LIMIT instead — limit priced a few
  bps past the touch — which fills immediately at the real best price (real slippage,
  the whole point) and confirms synchronously.
- We still poll the order for its fill price and fall back to a modelled fill
  (logged) if it can't be placed or doesn't confirm in time.
"""

from __future__ import annotations

import logging
import time

import httpx

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.paper_broker import PaperBroker, Position
from paper_scalper.engine.strategy import Side, Signal

log = logging.getLogger(__name__)

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"  # FORBIDDEN — never used; named only to assert against
BASE_URL = f"https://{PAPER_HOST}"

_FILL_POLL_TRIES = 8
_FILL_POLL_SLEEP = 0.25  # seconds between fill-status polls
_MARKETABLE_BPS = 50.0   # price the limit this far past the touch so it fills now


class AlpacaPaperBroker(PaperBroker):
    """PaperBroker whose fills come from an Alpaca paper account, not a slippage model."""

    def __init__(self, cfg: Settings, symbol: str) -> None:
        super().__init__(cfg)
        # paranoia: the base URL must be the paper sandbox, never the live host
        assert PAPER_HOST in BASE_URL and LIVE_HOST not in BASE_URL.replace(PAPER_HOST, "")
        assert "paper-" in BASE_URL, "Alpaca executor must use the PAPER host only"
        self.symbol = symbol               # "BTC/USD"
        self._venue_symbol = symbol        # Alpaca crypto orders use the slash form
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

    # --- entry: refuse shorts (crypto is long-only on Alpaca), else inherit ---
    def open_position(self, signal: Signal, quote: Quote, qty: float) -> Position | None:
        if signal.side == "short":
            log.info("[alpaca_paper] skip short %s — crypto cannot be shorted on Alpaca",
                     signal.reason)
            return None
        return super().open_position(signal, quote, qty)

    # --- the one override: a fill is a real Alpaca market order --------------
    def _fill_order(self, side: Side, entering: bool, raw: float, qty: float) -> float:
        """Place a real marketable-limit order for `qty` and return the actual fill.
        Falls back to the modelled fill (parent's slippage) if the order can't be
        placed or doesn't confirm in time — the in-app journal stays consistent."""
        order_side = ("buy" if side == "long" else "sell") if entering \
            else ("sell" if side == "long" else "buy")
        # raw is already the touch (ask for buys, bid for sells); cross it to fill now.
        buf = raw * _MARKETABLE_BPS / 10_000
        limit_price = raw + buf if order_side == "buy" else raw - buf
        qty_r = round(qty, 6)
        body = {"symbol": self._venue_symbol, "qty": str(qty_r), "side": order_side,
                "type": "limit", "limit_price": f"{limit_price:.2f}",
                "time_in_force": "gtc"}
        try:
            r = self._client.post("/v2/orders", json=body)
            r.raise_for_status()
            order = r.json()
        except Exception as exc:  # noqa: BLE001
            log.error("[alpaca_paper] order rejected (%s %s qty=%s) — modelling fill: %s",
                      order_side, self._venue_symbol, qty_r, exc)
            return self._slip(raw, side, entering)
        fill = self._await_fill(order.get("id"))
        if fill is None:
            log.warning("[alpaca_paper] %s qty=%s placed (order %s) but no fill price "
                        "confirmed — modelling fill", order_side, qty_r, order.get("id"))
            return self._slip(raw, side, entering)
        log.info("[alpaca_paper] %s %s qty=%s filled @ %.2f (order %s)",
                 order_side, self._venue_symbol, qty_r, fill, order.get("id"))
        return fill

    def _await_fill(self, order_id: str | None) -> float | None:
        """Poll the order a few times for its average fill price."""
        if not order_id:
            return None
        for _ in range(_FILL_POLL_TRIES):
            try:
                r = self._client.get(f"/v2/orders/{order_id}")
                r.raise_for_status()
                px = r.json().get("filled_avg_price")
                if px:
                    return float(px)
            except Exception as exc:  # noqa: BLE001
                log.error("[alpaca_paper] fill poll failed for %s: %s", order_id, exc)
                return None
            time.sleep(_FILL_POLL_SLEEP)
        return None

    def close(self) -> None:
        self._client.close()
