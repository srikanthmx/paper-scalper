"""Alpaca paper executor: the in-app ladder runs unchanged, every fill is a real
order. We mock the HTTP client so no network is touched — the point is to prove the
scale-out ladder (open 4, cut 2, cut 1, trail 1) is executed as Alpaca orders and
that shorts are refused (crypto is long-only on Alpaca)."""

from __future__ import annotations

from paper_scalper.broker.alpaca_paper import AlpacaPaperBroker
from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote
from paper_scalper.engine.strategy import Signal


class FakeResp:
    def __init__(self, data: dict) -> None:
        self._d = data

    def raise_for_status(self) -> None:  # noqa: D401
        pass

    def json(self) -> dict:
        return self._d


class FakeClient:
    """Records every order; reports an immediate fill so polling returns at once."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self._n = 0

    def post(self, path: str, json: dict) -> FakeResp:
        self._n += 1
        self.orders.append(json)
        return FakeResp({"id": f"o{self._n}"})

    def get(self, path: str) -> FakeResp:
        # /v2/orders/<id> — report a filled price (value irrelevant to ladder logic)
        return FakeResp({"filled_avg_price": "100.0"})

    def close(self) -> None:
        pass


def cfg() -> Settings:
    return Settings(alpaca_api_key="k", alpaca_api_secret="s", fee_bps=0.0,
                    slippage_bps=0.0, use_lots=True, lots_per_entry=4,
                    lot_size_usd=250.0, scale_lots="2,1")


def q(ts: float, bid: float, ask: float) -> Quote:
    return Quote(ts=ts, symbol="BTC/USD", bid=bid, ask=ask, bid_size=1, ask_size=1)


def ladder_signal(side: str = "long") -> Signal:
    return Signal(side=side, ts=0, ref_price=100, sl_pct=1.0, tp_pct=2.0, reason="t",
                  mode="scale_trail", scale_out_frac=0.5, breakeven_after_r=999.0)


def broker_with_fake() -> tuple[AlpacaPaperBroker, FakeClient]:
    b = AlpacaPaperBroker(cfg(), "BTC/USD")
    fake = FakeClient()
    b._client = fake  # type: ignore[assignment]
    return b, fake


def test_ladder_is_executed_as_real_alpaca_orders() -> None:
    b, fake = broker_with_fake()
    pos = b.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=0)
    assert pos is not None and pos.lots_total == 4
    entry = fake.orders[0]
    assert entry["symbol"] == "BTC/USD" and entry["side"] == "buy"
    assert entry["type"] == "limit"               # marketable limit (crypto needs it)
    assert float(entry["limit_price"]) > 100.05    # priced past the ask to fill now

    # TP1 (mid 102): cut 2 lots -> a real SELL of 2 lots
    f1 = b.on_quote(q(2, 102.0, 102.1))
    assert f1.lots == 2 and "TP1" in f1.event
    assert fake.orders[-1]["side"] == "sell"
    assert float(fake.orders[-1]["limit_price"]) < 102.0  # priced below the bid

    # TP2 (mid 104): cut 1 lot
    f2 = b.on_quote(q(3, 104.0, 104.1))
    assert f2.lots == 1 and "TP2" in f2.event

    # last lot trails out on the drop back to the breakeven/trail stop
    f3 = b.on_quote(q(4, 101.95, 102.05))
    assert f3 is not None and f3.lots == 1 and b.position is None

    # one entry + three scale/exit orders = the whole ladder went to Alpaca
    assert len(fake.orders) == 4
    assert [o["side"] for o in fake.orders] == ["buy", "sell", "sell", "sell"]
    assert f1.lots + f2.lots + f3.lots == 4


def test_short_entry_is_refused_no_order_sent() -> None:
    b, fake = broker_with_fake()
    pos = b.open_position(ladder_signal("short"), q(0, 99.95, 100.05), qty=0)
    assert pos is None and b.position is None
    assert fake.orders == []  # nothing sent to the venue


def test_fill_falls_back_to_model_when_no_fill_confirmed() -> None:
    b, fake = broker_with_fake()

    class NoFill(FakeClient):
        def get(self, path: str) -> FakeResp:
            return FakeResp({"filled_avg_price": None})

    nofill = NoFill()
    b._client = nofill  # type: ignore[assignment]
    # entry still opens (modelled fill); slippage is 0 so fill == ask
    pos = b.open_position(ladder_signal(), q(0, 99.95, 100.05), qty=0)
    assert pos is not None and pos.entry_price == 100.05
    assert nofill.orders[0]["side"] == "buy"
