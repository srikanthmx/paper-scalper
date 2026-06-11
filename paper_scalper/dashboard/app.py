"""Read-only dashboard over the SQLite journal.

    uvicorn paper_scalper.dashboard.app:app --port 8765
or: python -m paper_scalper.dashboard.app
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from paper_scalper.config import Settings
from paper_scalper.storage.db import Journal

app = FastAPI(title="paper-scalper dashboard")
_cfg = Settings()
_STATIC = Path(__file__).parent / "static"


def _journal() -> Journal:
    return Journal(_cfg.db_path)


STRATEGIES = ["pullback", "momo", "meanrev"]


@app.get("/api/summary")
def summary(strategy: str = "all") -> dict:
    j = _journal()
    try:
        return {"live": j.get_state("live"), **j.summary(strategy)}
    finally:
        j.close()


@app.get("/api/overview")
def overview() -> dict:
    """Per-strategy comparison + auto-diagnostics — the report, always on."""
    j = _journal()
    try:
        live = j.get_state("live") or {}
        strategies = {name: j.summary(name) for name in STRATEGIES}
        return {
            "live": live,
            "strategies": strategies,
            "diagnostics": {name: _diagnose(s) for name, s in strategies.items()},
        }
    finally:
        j.close()


def _diagnose(s: dict) -> list[str]:
    """Heuristics from STRATEGY.md, computed automatically per strategy."""
    notes: list[str] = []
    n = s["trades"]
    if n == 0:
        return ["No trades yet — check the signal feed for the binding filter."]
    if s["gross_pnl"] > 0 and s["net_pnl"] < 0:
        notes.append("Gross-positive but net-negative: fee drag — widen TP or trade less.")
    if s["total_fees"] > abs(s["gross_pnl"]) and n >= 5:
        notes.append("Fees exceed gross edge — targets too small for round-trip costs.")
    exits = s["exit_reasons"]
    for reason, hint in [
        ("max_hold", "max_hold dominates: targets too far or entries late."),
        ("stop_loss", "stop_loss dominates: entries are chasing — tighten filters."),
        ("breakeven_stop", "breakeven_stop frequent: good entries, greedy targets."),
    ]:
        if n >= 5 and exits.get(reason, 0) / n > 0.5:
            notes.append(hint)
    pf = s["profit_factor"]
    if pf is not None and pf >= 1.3 and n >= 30:
        notes.append("Profit factor ≥ 1.3 over a meaningful sample — candidate for go/no-go review.")
    return notes or ["No red flags at current sample size."]


@app.get("/api/trades")
def trades(limit: int = 200, strategy: str = "all") -> list[dict]:
    j = _journal()
    try:
        return j.trades(limit=limit, strategy=strategy)
    finally:
        j.close()


@app.get("/api/equity")
def equity(limit: int = 2000, strategy: str = "all") -> list[dict]:
    j = _journal()
    try:
        return j.equity_curve(limit=limit, strategy=strategy)
    finally:
        j.close()


@app.get("/api/candles")
def candles(limit: int = 600) -> list[dict]:
    j = _journal()
    try:
        return j.candles(limit=limit)
    finally:
        j.close()


@app.get("/api/signals")
def signals(limit: int = 30, strategy: str = "all") -> list[dict]:
    j = _journal()
    try:
        return j.signals(limit=limit, strategy=strategy)
    finally:
        j.close()


@app.get("/api/daily")
def daily(strategy: str = "all") -> list[dict]:
    j = _journal()
    try:
        return j.daily_summary(strategy)
    finally:
        j.close()


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
