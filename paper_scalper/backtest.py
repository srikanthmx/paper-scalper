"""Backtest / parameter optimizer over the stored candle history.

Walks the journal's 1-min candles, applies an entry rule, and sweeps the exit
geometry (stop in ATRs, reward:risk, hold) to find positive-expectancy configs.
Costs are charged in R terms, so the ranking already accounts for spread/slippage.

    python -m paper_scalper.backtest            # full grid, ranked
    python -m paper_scalper.backtest --rule meanrev

This is the tool that answers "is this geometry winnable" before tuning a lane.
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass

from paper_scalper.storage.db import Journal

COST_PCT = 0.04  # round-trip cost: Coinbase ~1-2bp spread + slippage


def _ema(period: int, src: list[float]) -> list[float | None]:
    a = 2 / (period + 1)
    out: list[float | None] = [None] * len(src)
    seed: list[float] = []
    for i, x in enumerate(src):
        if i and out[i - 1] is not None:
            out[i] = a * x + (1 - a) * out[i - 1]
        else:
            seed.append(x)
            out[i] = sum(seed) / len(seed) if len(seed) >= period else None
    return out


def _rsi(period: int, src: list[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(src)
    avg_g = avg_l = None
    prev = None
    for i, x in enumerate(src):
        if prev is None:
            prev = x
            continue
        ch = x - prev
        prev = x
        g, l = max(ch, 0.0), max(-ch, 0.0)
        avg_g = g if avg_g is None else (avg_g * (period - 1) + g) / period
        avg_l = l if avg_l is None else (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def _atr(period: int, high, low, close) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    val = None
    pc = None
    seed: list[float] = []
    for i in range(len(close)):
        tr = (high[i] - low[i] if pc is None
              else max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc)))
        pc = close[i]
        if val is None:
            seed.append(tr)
            val = sum(seed) / len(seed) if len(seed) >= period else None
        else:
            val = (val * (period - 1) + tr) / period
        out[i] = val
    return out


@dataclass
class Result:
    rule: str
    sl_atr: float
    rr: float
    hold: int
    expectancy_r: float
    win_pct: float
    trades: int

    @property
    def total_r(self) -> float:
        return self.expectancy_r * self.trades


class Backtester:
    def __init__(self, candles: list[dict]) -> None:
        self.c = candles
        self.n = len(candles)
        self.close = [k["close"] for k in candles]
        self.high = [k["high"] for k in candles]
        self.low = [k["low"] for k in candles]
        self.e9 = _ema(9, self.close)
        self.e21 = _ema(21, self.close)
        self.e50 = _ema(50, self.close)
        self.rsi = _rsi(14, self.close)
        self.atr = _atr(14, self.high, self.low, self.close)

    def _signal(self, rule: str, i: int) -> int:
        c, e9, e21, e50, r, a = (self.close, self.e9, self.e21, self.e50,
                                 self.rsi, self.atr)
        if None in (e9[i], e21[i], e50[i], r[i], a[i]):
            return 0
        sep = (e9[i] - e21[i]) / c[i] * 100
        if rule == "random":
            return 1 if i % 2 else -1
        if rule == "trend":
            return 1 if sep > 0.02 and c[i] > e9[i] else -1 if sep < -0.02 and c[i] < e9[i] else 0
        if rule == "breakout":
            hh = max(self.high[i - 12:i]); ll = min(self.low[i - 12:i])
            return 1 if c[i] > hh else -1 if c[i] < ll else 0
        if rule == "meanrev":  # the profitable family: deep RSI extreme, fade to mean
            return 1 if r[i] <= 25 else -1 if r[i] >= 75 else 0
        return 0

    def run(self, rule: str, sl_atr: float, rr: float, hold: int) -> Result | None:
        rs: list[float] = []
        for i in range(55, self.n - 1):
            s = self._signal(rule, i)
            if not s:
                continue
            atr_pct = self.atr[i] / self.close[i] * 100
            sl = max(sl_atr * atr_pct, 0.05)
            ent = self.close[i]
            slp = ent * (1 - s * sl / 100)
            tpp = ent * (1 + s * rr * sl / 100)
            res = None
            for k in range(i + 1, min(i + 1 + hold, self.n)):
                hit_sl = self.low[k] <= slp if s > 0 else self.high[k] >= slp
                hit_tp = self.high[k] >= tpp if s > 0 else self.low[k] <= tpp
                if hit_sl:  # conservative: stop wins ties
                    res = -1.0
                    break
                if hit_tp:
                    res = rr
                    break
            if res is None:  # timed out: mark to close, in R
                res = s * (self.close[min(i + hold, self.n - 1)] - ent) / ent * 100 / sl
            rs.append(res - COST_PCT / sl)
        if len(rs) < 15:
            return None
        wins = sum(1 for x in rs if x > 0)
        return Result(rule, sl_atr, rr, hold, statistics.mean(rs),
                      wins / len(rs) * 100, len(rs))

    def grid(self, rules: list[str]) -> list[Result]:
        out: list[Result] = []
        for rule in rules:
            for sl_atr in (0.75, 1.0, 1.5, 2.0, 2.5, 3.0):
                for rr in (0.8, 1.0, 1.2, 1.5, 2.0, 3.0):
                    for hold in (10, 20, 40, 90, 180):
                        r = self.run(rule, sl_atr, rr, hold)
                        if r:
                            out.append(r)
        out.sort(key=lambda r: -r.total_r)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest/optimize exit geometry on stored candles")
    ap.add_argument("--rule", default=None,
                    help="entry rule (meanrev/trend/breakout/random); default: all")
    ap.add_argument("--db", default=None)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    from paper_scalper.config import Settings
    journal = Journal(args.db or Settings().db_path)
    candles = journal.candles(limit=20000)
    journal.close()
    if len(candles) < 200:
        raise SystemExit(f"only {len(candles)} candles — need more history to backtest")

    bt = Backtester(candles)
    rules = [args.rule] if args.rule else ["meanrev", "trend", "breakout", "random"]
    results = bt.grid(rules)
    print(f"candles: {len(candles)} | cost {COST_PCT}% round-trip | ranked by total R\n")
    print(f"{'entry':9s} {'SL×ATR':>6s} {'RR':>6s} {'hold':>5s} "
          f"{'exp(R)':>8s} {'win%':>6s} {'trades':>6s} {'totalR':>8s}")
    for r in results[:args.top]:
        print(f"{r.rule:9s} {r.sl_atr:6.2f} {'1:' + format(r.rr, '.1f'):>6s} {r.hold:4d}m "
              f"{r.expectancy_r:+8.3f} {r.win_pct:6.1f} {r.trades:6d} {r.total_r:+8.1f}")
    pos = [r for r in results if r.expectancy_r > 0]
    print(f"\nprofitable configs: {len(pos)} / {len(results)}")
    if pos:
        b = pos[0]
        print(f"best: {b.rule} SL {b.sl_atr}×ATR, 1:{b.rr}, {b.hold}m hold "
              f"-> {b.expectancy_r:+.3f}R/trade, {b.win_pct:.0f}% win")


if __name__ == "__main__":
    main()
