from __future__ import annotations

from paper_scalper.engine.paper_broker import ClosedTrade
from paper_scalper.storage.db import Journal


def _trade(net: float) -> ClosedTrade:
    return ClosedTrade(side="long", qty=1.0, entry_ts=1.0, entry_price=100.0, exit_ts=2.0,
                       exit_price=100 + net, fees=0.0, gross_pnl=net, net_pnl=net,
                       reason_entry="t", reason_exit="take_profit")


def test_reset_archives_before_clearing(tmp_path) -> None:
    j = Journal(str(tmp_path / "j.db"))
    j.record_trade("BTC/USD", "trend", _trade(5.0))
    j.record_trade("BTC/USD", "meanrev", _trade(-3.0))
    assert len(j.trades()) == 2

    archived = j.reset_trades("era-1")
    assert archived == 2
    assert len(j.trades()) == 0           # live cleared
    assert len(j.archived_trades()) == 2  # but preserved forever

    # a second era accumulates in the same permanent archive
    j.record_trade("BTC/USD", "trend", _trade(7.0))
    j.reset_trades("era-2")
    arch = j.archived_trades()
    assert len(arch) == 3
    assert {r["reset_label"] for r in arch} == {"era-1", "era-2"}
    j.close()


def test_archive_survives_reopen(tmp_path) -> None:
    path = str(tmp_path / "j.db")
    j = Journal(path)
    j.record_trade("BTC/USD", "momo", _trade(2.0))
    j.reset_trades("era-1")
    j.close()
    # reopening the DB still has the archived history
    j2 = Journal(path)
    assert len(j2.archived_trades()) == 1
    j2.close()


def test_insert_archived_backfill(tmp_path) -> None:
    j = Journal(str(tmp_path / "j.db"))
    j.insert_archived([{"archive_ts": 1.0, "reset_label": "snapshot:old.db",
                        "symbol": "BTC/USD", "strategy": "pullback", "version": 1,
                        "source": "app", "side": "long", "qty": 1.0, "entry_ts": 1.0,
                        "entry_price": 100.0, "exit_ts": 2.0, "exit_price": 101.0,
                        "fees": 0.0, "gross_pnl": 1.0, "net_pnl": 1.0, "net_pnl_pct": 1.0,
                        "reason_entry": "t", "reason_exit": "take_profit"}])
    arch = j.archived_trades()
    assert len(arch) == 1 and arch[0]["strategy"] == "pullback"
    j.close()
