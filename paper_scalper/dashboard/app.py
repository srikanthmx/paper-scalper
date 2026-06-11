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


@app.get("/api/summary")
def summary() -> dict:
    j = _journal()
    try:
        return {"live": j.get_state("live"), **j.summary()}
    finally:
        j.close()


@app.get("/api/trades")
def trades(limit: int = 200) -> list[dict]:
    j = _journal()
    try:
        return j.trades(limit=limit)
    finally:
        j.close()


@app.get("/api/equity")
def equity(limit: int = 2000) -> list[dict]:
    j = _journal()
    try:
        return j.equity_curve(limit=limit)
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
def signals(limit: int = 30) -> list[dict]:
    j = _journal()
    try:
        return j.signals(limit=limit)
    finally:
        j.close()


@app.get("/api/daily")
def daily() -> list[dict]:
    j = _journal()
    try:
        return j.daily_summary()
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
