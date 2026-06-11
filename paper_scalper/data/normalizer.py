"""Normalized market events. Every feed adapter emits these and nothing else."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Trade:
    ts: float  # unix seconds
    symbol: str
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Quote:
    ts: float
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid * 10_000


MarketEvent = Trade | Quote
