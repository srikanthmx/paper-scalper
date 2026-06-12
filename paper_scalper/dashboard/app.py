"""Read-only dashboard over the SQLite journal.

    uvicorn paper_scalper.dashboard.app:app --port 8765
or: python -m paper_scalper.dashboard.app
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from paper_scalper.config import Settings
from paper_scalper.storage.db import Journal

app = FastAPI(title="paper-scalper dashboard")
_cfg = Settings()
_STATIC = Path(__file__).parent / "static"


def _journal() -> Journal:
    return Journal(_cfg.db_path)


STRATEGIES = ["pullback", "momo", "meanrev", "trend", "daily", "lorentz"]


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


class ParamSave(BaseModel):
    strategy: str
    params: dict[str, float]
    note: str = ""


@app.get("/api/params")
def get_params() -> dict:
    """Active tunables per strategy (what the runner is currently using)."""
    j = _journal()
    try:
        out = {}
        for name in STRATEGIES:
            state = j.get_state(f"params:{name}") or {}
            out[name] = {
                "version": state.get("version", 0),
                "params": state.get("params", {}),
                "history": j.param_versions(name)[:10],
            }
        return out
    finally:
        j.close()


@app.post("/api/params")
def save_params(body: ParamSave) -> dict:
    """Save a new param version. Empty/partial params merge onto the active set.
    The runner applies it on the next candle and tags trades with the version."""
    if body.strategy not in STRATEGIES:
        return {"error": f"unknown strategy {body.strategy}"}
    j = _journal()
    try:
        state = j.get_state(f"params:{body.strategy}") or {}
        merged = {**state.get("params", {}), **body.params}
        version = j.save_param_version(body.strategy, merged, note=body.note)
        return {"strategy": body.strategy, "version": version, "params": merged}
    finally:
        j.close()


@app.get("/api/versions")
def versions() -> list[dict]:
    """Per (strategy, version) performance — compare what each tweak did."""
    j = _journal()
    try:
        return j.version_stats()
    finally:
        j.close()


REQUESTS_LOG = Path(__file__).resolve().parents[2] / "research_requests.log"


class DeployBody(BaseModel):
    id: str


def _request_agent(action: str, payload: dict) -> None:
    """Signal the local research agent (a Claude session watching this file)."""
    line = json.dumps({"action": action, "ts": time.time(), **payload})
    with REQUESTS_LOG.open("a") as fh:
        fh.write(line + "\n")


@app.get("/api/research")
def research() -> dict:
    j = _journal()
    try:
        return {
            "status": j.get_state("research:status") or {"state": "idle"},
            "candidates": j.get_state("research:candidates") or [],
            "current": j.get_state("research:current") or {},
            "history": j.param_versions("daily")[:10],
            "agent_seen": j.get_state("research:agent_heartbeat"),
        }
    finally:
        j.close()


@app.post("/api/research/run")
def research_run() -> dict:
    j = _journal()
    try:
        j.set_state("research:status",
                     {"state": "requested", "ts": time.time(),
                      "detail": "research requested — agent will pick it up shortly"})
        _request_agent("research", {})
        return {"ok": True}
    finally:
        j.close()


@app.post("/api/research/deploy")
def research_deploy(body: DeployBody) -> dict:
    j = _journal()
    try:
        candidates = j.get_state("research:candidates") or []
        chosen = next((c for c in candidates if c.get("id") == body.id), None)
        if chosen is None:
            return {"error": f"unknown candidate {body.id}"}
        j.set_state("research:status",
                     {"state": "deploy_requested", "ts": time.time(),
                      "detail": f"deploying {chosen['name']} — agent will implement, "
                                "test and restart the runner"})
        _request_agent("deploy", {"id": body.id, "name": chosen["name"]})
        return {"ok": True, "chosen": chosen["name"]}
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
