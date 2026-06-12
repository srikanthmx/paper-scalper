"""Runner: feed → candles → [strategy lanes] → journal.

Each strategy runs in an isolated lane: own paper account, own risk manager,
own broker. All lanes consume the same candle/quote stream.

Usage:
    python -m paper_scalper.main --feed alpaca     # live BTC/USD data (needs keys in .env)
    python -m paper_scalper.main --feed coinbase   # keyless live BTC-USD data
    python -m paper_scalper.main --feed synthetic  # keyless smoke test
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Protocol

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote, Trade
from paper_scalper.engine.candles import Candle, CandleBuilder
from paper_scalper.engine.meanrev import MeanReversionStrategy
from paper_scalper.engine.momentum import MomentumStrategy
from paper_scalper.engine.paper_broker import ClosedTrade, PaperBroker
from paper_scalper.engine.risk import RiskManager
from paper_scalper.engine.strategy import Signal, Snapshot, Strategy
from paper_scalper.engine.trend import TrendScalpStrategy
from paper_scalper.storage.db import Journal

log = logging.getLogger("paper_scalper")

EQUITY_SNAPSHOT_SECONDS = 30


class StrategyProtocol(Protocol):
    name: str
    snapshot: Snapshot

    def on_candle(self, candle: Candle, quote: Quote | None) -> Signal | None: ...


def build_strategies(cfg: Settings) -> list[StrategyProtocol]:
    return [Strategy(cfg), MomentumStrategy(cfg), MeanReversionStrategy(cfg),
            TrendScalpStrategy(cfg)]


class Lane:
    """One strategy with its own paper account, risk limits, and broker."""

    def __init__(self, strategy: StrategyProtocol, cfg: Settings) -> None:
        self.name = strategy.name
        self.strategy = strategy
        self.risk = RiskManager(cfg)
        self.broker = PaperBroker(cfg)


class Engine:
    def __init__(self, cfg: Settings, journal: Journal,
                 strategies: list[StrategyProtocol] | None = None) -> None:
        self.cfg = cfg
        self.journal = journal
        self.candles = CandleBuilder(cfg.candle_seconds)
        self.lanes = [Lane(s, cfg) for s in (strategies or build_strategies(cfg))]
        self.last_quote: Quote | None = None
        self._last_snapshot = 0.0
        self._last_equity_snap = 0.0

    def on_event(self, event: Trade | Quote) -> None:
        if isinstance(event, Quote):
            self.last_quote = event
            for lane in self.lanes:
                closed = lane.broker.on_quote(event)
                if closed is not None:
                    self._handle_close(lane, closed)
            completed = self.candles.on_quote(event)
        else:
            completed = self.candles.on_trade(event)
        if completed is not None:
            self._on_candle_closed(completed)
        self._publish_state(event.ts)

    def _on_candle_closed(self, candle: Candle) -> None:
        self.journal.record_candle(self.cfg.symbol, candle.ts_open, candle.open,
                                   candle.high, candle.low, candle.close, candle.volume)
        for lane in self.lanes:
            lane.risk.on_candle()
            signal = lane.strategy.on_candle(candle, self.last_quote)
            snap = lane.strategy.snapshot
            if snap.rejects and snap.rejects != ["warming_up"]:
                self.journal.record_signal(candle.ts_open, self.cfg.symbol, lane.name,
                                           "reject", "; ".join(snap.rejects))
            if signal is None or lane.broker.position is not None or self.last_quote is None:
                continue
            decision = lane.risk.can_enter(signal.ts)
            if not decision.allowed:
                log.info("[%s] signal blocked by risk: %s", lane.name, decision.reason)
                self.journal.record_signal(signal.ts, self.cfg.symbol, lane.name, "risk_block",
                                           f"{signal.reason} | blocked: {decision.reason}")
                continue
            qty = lane.risk.position_size(signal.ref_price, signal.sl_pct)
            if qty <= 0:
                continue
            pos = lane.broker.open_position(signal, self.last_quote, qty)
            self.journal.record_signal(signal.ts, self.cfg.symbol, lane.name, "signal",
                                       signal.reason)
            log.info("[%s] OPEN %s qty=%.6f @ %.2f sl=%.2f tp=%.2f | %s", lane.name,
                     pos.side, pos.qty, pos.entry_price, pos.sl_price, pos.tp_price,
                     signal.reason)

    def _handle_close(self, lane: Lane, trade: ClosedTrade) -> None:
        lane.risk.on_trade_closed(trade.net_pnl, trade.exit_ts)
        self.journal.record_trade(self.cfg.symbol, lane.name, trade)
        self.journal.record_equity(trade.exit_ts, lane.risk.equity, lane.name)
        log.info("[%s] CLOSE %s @ %.2f (%s) net=%.2f fees=%.2f equity=%.2f", lane.name,
                 trade.side, trade.exit_price, trade.reason_exit, trade.net_pnl,
                 trade.fees, lane.risk.equity)
        if lane.risk.halted_reason:
            log.warning("[%s] RISK HALT: %s", lane.name, lane.risk.halted_reason)

    def _lane_state(self, lane: Lane) -> dict:
        pos = lane.broker.position
        quote = self.last_quote
        unrealized = pos.unrealized(quote) if pos and quote else 0.0
        return {
            "equity": lane.risk.equity,
            "unrealized": unrealized,
            "halted": lane.risk.halted_reason,
            "consecutive_losses": lane.risk.consecutive_losses,
            "position": None if pos is None else {
                "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price,
                "sl": pos.sl_price, "tp": pos.tp_price, "entry_ts": pos.entry_ts,
            },
        }

    def _publish_state(self, ts: float) -> None:
        now = time.time()
        if now - self._last_snapshot < 2.0:
            return
        self._last_snapshot = now
        quote = self.last_quote
        lanes = {lane.name: self._lane_state(lane) for lane in self.lanes}
        pullback = next((l.strategy.snapshot for l in self.lanes if l.name == "pullback"),
                        Snapshot())
        self.journal.set_state("live", {
            "ts": ts,
            "symbol": self.cfg.symbol,
            "last_price": quote.mid if quote else None,
            "equity": sum(s["equity"] for s in lanes.values()),
            "unrealized": sum(s["unrealized"] for s in lanes.values()),
            "lanes": lanes,
            "indicators": {
                "vwap": pullback.vwap, "ema9": pullback.ema_fast,
                "ema21": pullback.ema_slow, "rsi": pullback.rsi, "atr": pullback.atr,
            },
        })
        if now - self._last_equity_snap >= EQUITY_SNAPSHOT_SECONDS:
            self._last_equity_snap = now
            total = 0.0
            for lane in self.lanes:
                state = lanes[lane.name]
                lane_total = state["equity"] + state["unrealized"]
                total += lane_total
                self.journal.record_equity(ts, lane_total, lane.name)
            self.journal.record_equity(ts, total, "all")


async def run(feed_name: str, cfg: Settings) -> None:
    journal = Journal(cfg.db_path)
    engine = Engine(cfg, journal)
    if feed_name == "alpaca":
        from paper_scalper.data.alpaca_crypto_feed import AlpacaCryptoFeed
        if not cfg.alpaca_api_key or not cfg.alpaca_api_secret:
            raise SystemExit("Set ALPACA_API_KEY / ALPACA_API_SECRET in .env (data-only keys)")
        feed = AlpacaCryptoFeed(cfg.alpaca_api_key, cfg.alpaca_api_secret, [cfg.symbol])
    elif feed_name == "coinbase":
        from paper_scalper.data.coinbase_feed import CoinbaseFeed
        feed = CoinbaseFeed([cfg.symbol])
    elif feed_name == "synthetic":
        from paper_scalper.data.replay_feed import SyntheticFeed
        feed = SyntheticFeed(symbol=cfg.symbol)
    else:
        raise SystemExit(f"unknown feed: {feed_name}")

    log.info("paper trading %s | lanes=%s candle=%ss fee=%.0fbps slip=%.0fbps equity=%.2f/lane",
             cfg.symbol, [l.name for l in engine.lanes], cfg.candle_seconds, cfg.fee_bps,
             cfg.slippage_bps, cfg.starting_equity)
    errors = 0
    try:
        async for event in feed.stream():
            try:
                engine.on_event(event)
            except Exception:  # noqa: BLE001 — a processing bug must not end the run
                errors += 1
                log.exception("engine error on %r (%d so far) — event skipped", event, errors)
                if errors >= 100:
                    raise
    finally:
        journal.set_state("live", {"ts": time.time(), "stopped": True})
        journal.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-only scalper (no real orders)")
    parser.add_argument("--feed", choices=["alpaca", "coinbase", "synthetic"], default="alpaca")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()
    cfg = Settings()
    if args.symbol:
        cfg.symbol = args.symbol
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args.feed, cfg))
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
