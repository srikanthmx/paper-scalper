"""SQLite trade journal, multi-strategy. The runner writes; the dashboard reads."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from paper_scalper.engine.paper_broker import ClosedTrade

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'pullback',
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_ts REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_ts REAL NOT NULL,
    exit_price REAL NOT NULL,
    fees REAL NOT NULL,
    gross_pnl REAL NOT NULL,
    net_pnl REAL NOT NULL,
    net_pnl_pct REAL NOT NULL,
    reason_entry TEXT NOT NULL,
    reason_exit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts REAL NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'all',
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_log (
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'pullback',
    kind TEXT NOT NULL,      -- 'signal' | 'reject' | 'risk_block'
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS param_versions (
    strategy TEXT NOT NULL,
    version INTEGER NOT NULL,
    params TEXT NOT NULL,        -- JSON of tunables at this version
    note TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    PRIMARY KEY (strategy, version)
);
CREATE TABLE IF NOT EXISTS candles (
    ts_open REAL NOT NULL,
    symbol TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, ts_open)
);
"""

# Columns added after the initial release; applied to pre-existing databases.
MIGRATIONS = [
    ("trades", "strategy", "TEXT NOT NULL DEFAULT 'pullback'"),
    ("trades", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("equity_snapshots", "strategy", "TEXT NOT NULL DEFAULT 'all'"),
    ("signal_log", "strategy", "TEXT NOT NULL DEFAULT 'pullback'"),
]


class Journal:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, column, decl in MIGRATIONS:
                cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self._conn.commit()

    def record_trade(self, symbol: str, strategy: str, trade: ClosedTrade,
                     version: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades (symbol, strategy, version, side, qty, entry_ts,"
                " entry_price, exit_ts, exit_price, fees, gross_pnl, net_pnl, net_pnl_pct,"
                " reason_entry, reason_exit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, strategy, version, trade.side, trade.qty, trade.entry_ts,
                 trade.entry_price, trade.exit_ts, trade.exit_price, trade.fees,
                 trade.gross_pnl, trade.net_pnl, trade.net_pnl_pct, trade.reason_entry,
                 trade.reason_exit),
            )
            self._conn.commit()

    def save_param_version(self, strategy: str, params: dict[str, Any],
                           note: str = "") -> int:
        """Insert a new param version and activate it. Returns the version number."""
        import time as _time
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM param_versions WHERE strategy=?",
                (strategy,),
            ).fetchone()
            version = row["v"] + 1
            self._conn.execute(
                "INSERT INTO param_versions (strategy, version, params, note, created_ts)"
                " VALUES (?,?,?,?,?)",
                (strategy, version, json.dumps(params), note, _time.time()),
            )
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"params:{strategy}", json.dumps({"version": version, "params": params})),
            )
            self._conn.commit()
        return version

    def param_versions(self, strategy: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, params, note, created_ts FROM param_versions"
                " WHERE strategy=? ORDER BY version DESC", (strategy,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"])
            out.append(d)
        return out

    def version_stats(self) -> list[dict[str, Any]]:
        """Per (strategy, version) performance — the learning ledger."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy, version, COUNT(*) AS trades,"
                " SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
                " SUM(net_pnl) AS net_pnl, SUM(fees) AS fees,"
                " AVG(net_pnl) AS expectancy"
                " FROM trades GROUP BY strategy, version"
                " ORDER BY strategy, version DESC",
            ).fetchall()
        return [dict(r) for r in rows]

    def record_equity(self, ts: float, equity: float, strategy: str = "all") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity_snapshots (ts, strategy, equity) VALUES (?,?,?)",
                (ts, strategy, equity),
            )
            self._conn.commit()

    def record_signal(self, ts: float, symbol: str, strategy: str, kind: str,
                      detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO signal_log (ts, symbol, strategy, kind, detail)"
                " VALUES (?,?,?,?,?)",
                (ts, symbol, strategy, kind, detail),
            )
            self._conn.commit()

    def record_candle(self, symbol: str, ts_open: float, open_: float, high: float,
                      low: float, close: float, volume: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO candles (symbol, ts_open, open, high, low, close, volume)"
                " VALUES (?,?,?,?,?,?,?)",
                (symbol, ts_open, open_, high, low, close, volume),
            )
            self._conn.commit()

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def get_state(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    @staticmethod
    def _strat_clause(strategy: str | None) -> tuple[str, list[Any]]:
        if strategy is None or strategy == "all":
            return "", []
        return " WHERE strategy=?", [strategy]

    def trades(self, limit: int = 200, strategy: str | None = None) -> list[dict[str, Any]]:
        where, params = self._strat_clause(strategy)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM trades{where} ORDER BY exit_ts DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def candles(self, limit: int = 600) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts_open, open, high, low, close, volume FROM candles"
                " ORDER BY ts_open DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def signals(self, limit: int = 30, strategy: str | None = None) -> list[dict[str, Any]]:
        where, params = self._strat_clause(strategy)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, symbol, strategy, kind, detail FROM signal_log{where}"
                " ORDER BY ts DESC, rowid DESC LIMIT ?", [*params, limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self, limit: int = 2000, strategy: str = "all") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, equity FROM equity_snapshots WHERE strategy=?"
                " ORDER BY ts DESC LIMIT ?", (strategy, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def daily_summary(self, strategy: str | None = None) -> list[dict[str, Any]]:
        where, params = self._strat_clause(strategy)
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(exit_ts, 'unixepoch') AS day, COUNT(*) AS trades,"
                " SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
                " SUM(net_pnl) AS net_pnl, SUM(gross_pnl) AS gross_pnl, SUM(fees) AS fees"
                f" FROM trades{where} GROUP BY day ORDER BY day DESC", params,
            ).fetchall()
        return [dict(r) for r in rows]

    def hourly_summary(self, strategy: str | None = None) -> list[dict[str, Any]]:
        """Net PnL by UTC hour — time-of-day edge/bleed becomes visible."""
        where, params = self._strat_clause(strategy)
        with self._lock:
            rows = self._conn.execute(
                "SELECT CAST(strftime('%H', exit_ts, 'unixepoch') AS INTEGER) AS hour,"
                " COUNT(*) AS trades,"
                " SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
                " SUM(net_pnl) AS net_pnl"
                f" FROM trades{where} GROUP BY hour ORDER BY hour", params,
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, strategy: str | None = None) -> dict[str, Any]:
        trades = self.trades(limit=100_000, strategy=strategy)
        equity = self.equity_curve(limit=100_000, strategy=strategy or "all")
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = -sum(t["net_pnl"] for t in losses)
        peak, max_dd = 0.0, 0.0
        for point in equity:
            peak = max(peak, point["equity"])
            if peak > 0:
                max_dd = max(max_dd, (peak - point["equity"]) / peak * 100)
        return {
            "strategy": strategy or "all",
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
            "net_pnl": sum(t["net_pnl"] for t in trades),
            "gross_pnl": sum(t["gross_pnl"] for t in trades),
            "total_fees": sum(t["fees"] for t in trades),
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "max_drawdown_pct": max_dd,
            "exit_reasons": _count_by(trades, "reason_exit"),
            "by_side": {
                side: {
                    "trades": len(sub),
                    "wins": sum(1 for t in sub if t["net_pnl"] > 0),
                    "net_pnl": sum(t["net_pnl"] for t in sub),
                }
                for side in ("long", "short")
                if (sub := [t for t in trades if t["side"] == side])
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out
