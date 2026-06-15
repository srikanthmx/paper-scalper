"""Permanent trade-history archive — never lose learning data again.

The live journal (paper_scalper.db) gets reset between eras, but every trade is
archived first into the never-cleared trade_archive table, and exported here to
trade_history.jsonl — a plain-text, git-committable record that survives even if
the database file is deleted.

    python -m paper_scalper.archive ingest        # pull old archive_*.db snapshots + live trades in
    python -m paper_scalper.archive export        # dump the full archive to trade_history.jsonl
    python -m paper_scalper.archive stats         # summarize what's preserved
    python -m paper_scalper.archive reset <label> # SAFELY archive-then-clear (never lose data)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from paper_scalper.config import Settings
from paper_scalper.storage.db import Journal

ROOT = Path(__file__).resolve().parents[1]
EXPORT_FILE = ROOT / "trade_history.jsonl"
TRADE_COLS = ("symbol", "strategy", "version", "source", "side", "qty", "entry_ts",
              "entry_price", "exit_ts", "exit_price", "fees", "gross_pnl", "net_pnl",
              "net_pnl_pct", "reason_entry", "reason_exit")


def _existing_keys(journal: Journal) -> set:
    """Dedupe key: a trade is uniquely (strategy, entry_ts, exit_ts, entry_price)."""
    return {(r["strategy"], r["entry_ts"], r["exit_ts"], r["entry_price"])
            for r in journal.archived_trades()}


def ingest() -> None:
    """Pull every old archive_*.db snapshot AND current live trades into the archive,
    de-duplicated, so nothing already captured on disk is ever lost."""
    journal = Journal(Settings().db_path)
    seen = _existing_keys(journal)
    added = 0

    # 1) current live trades
    rows = []
    for t in journal.trades(1_000_000):
        key = (t["strategy"], t["entry_ts"], t["exit_ts"], t["entry_price"])
        if key not in seen:
            seen.add(key)
            rows.append({"archive_ts": time.time(), "reset_label": "live", **t})
    # 2) old snapshot DBs
    for snap in sorted(ROOT.glob("archive_*.db")):
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute("SELECT * FROM trades")
        except sqlite3.OperationalError:
            conn.close()
            continue
        for r in cur.fetchall():
            d = dict(r)
            key = (d.get("strategy"), d.get("entry_ts"), d.get("exit_ts"), d.get("entry_price"))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"archive_ts": time.time(), "reset_label": f"snapshot:{snap.name}",
                         "source": d.get("source", "app"), "version": d.get("version", 0), **d})
        conn.close()

    if rows:
        journal.insert_archived(rows)
        added = len(rows)
    print(f"ingested {added} new trades into the archive "
          f"({len(journal.archived_trades())} total now)")
    journal.close()
    export()


def export() -> None:
    journal = Journal(Settings().db_path)
    rows = journal.archived_trades()
    with EXPORT_FILE.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(f"exported {len(rows)} trades to {EXPORT_FILE.relative_to(ROOT)}")
    journal.close()


def stats() -> None:
    journal = Journal(Settings().db_path)
    rows = journal.archived_trades()
    if not rows:
        print("archive empty — run `python -m paper_scalper.archive ingest`")
        journal.close()
        return
    from collections import defaultdict
    by_era = defaultdict(lambda: [0, 0.0])
    by_lane = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by_era[r["reset_label"]][0] += 1
        by_era[r["reset_label"]][1] += r["net_pnl"] or 0
        by_lane[r["strategy"]][0] += 1
        by_lane[r["strategy"]][1] += r["net_pnl"] or 0
    print(f"=== archive: {len(rows)} trades preserved ===")
    print("\nby era:")
    for era, (n, pnl) in sorted(by_era.items()):
        print(f"  {era:28s} {n:5d} trades  net {pnl:+9.2f}")
    print("\nby strategy (all-time):")
    for lane, (n, pnl) in sorted(by_lane.items(), key=lambda x: -x[1][1]):
        print(f"  {lane:10s} {n:5d} trades  net {pnl:+9.2f}  avg {pnl/n:+.2f}")
    journal.close()


def reset() -> None:
    """The ONLY sanctioned way to clear the live journal: archive first, always."""
    label = sys.argv[2] if len(sys.argv) > 2 else f"reset-{int(time.time())}"
    journal = Journal(Settings().db_path)
    n = journal.reset_trades(label)
    journal.close()
    print(f"archived {n} trades as '{label}', then cleared the live journal")
    export()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    {"ingest": ingest, "export": export, "stats": stats, "reset": reset}.get(cmd, stats)()


if __name__ == "__main__":
    main()
