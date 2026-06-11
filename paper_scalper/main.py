"""Runner: feed → candles → strategy → risk → paper broker → journal.

Usage:
    python -m paper_scalper.main --feed alpaca     # live BTC/USD data (needs keys in .env)
    python -m paper_scalper.main --feed synthetic  # keyless smoke test
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from paper_scalper.config import Settings
from paper_scalper.data.normalizer import Quote, Trade
from paper_scalper.engine.candles import CandleBuilder
from paper_scalper.engine.paper_broker import ClosedTrade, PaperBroker
from paper_scalper.engine.risk import RiskManager
from paper_scalper.engine.strategy import Strategy
from paper_scalper.storage.db import Journal

log = logging.getLogger("paper_scalper")

EQUITY_SNAPSHOT_SECONDS = 30


class Engine:
    def __init__(self, cfg: Settings, journal: Journal) -> None:
        self.cfg = cfg
        self.journal = journal
        self.candles = CandleBuilder(cfg.candle_seconds)
        self.strategy = Strategy(cfg)
        self.risk = RiskManager(cfg)
        self.broker = PaperBroker(cfg)
        self.last_quote: Quote | None = None
        self._last_snapshot = 0.0

    def on_event(self, event: Trade | Quote) -> None:
        if isinstance(event, Quote):
            self.last_quote = event
            closed = self.broker.on_quote(event)
            if closed is not None:
                self._handle_close(closed)
        else:
            completed = self.candles.on_trade(event)
            if completed is not None:
                self._on_candle_closed(completed)
        self._publish_state(event.ts)

    def _on_candle_closed(self, candle) -> None:
        self.journal.record_candle(self.cfg.symbol, candle.ts_open, candle.open,
                                   candle.high, candle.low, candle.close, candle.volume)
        self.risk.on_candle()
        signal = self.strategy.on_candle(candle, self.last_quote)
        snap = self.strategy.snapshot
        if snap.rejects and snap.rejects != ["warming_up"]:
            self.journal.record_signal(candle.ts_open, self.cfg.symbol, "reject",
                                       "; ".join(snap.rejects))
        if signal is None or self.broker.position is not None or self.last_quote is None:
            return
        decision = self.risk.can_enter(signal.ts)
        if not decision.allowed:
            log.info("signal blocked by risk: %s", decision.reason)
            self.journal.record_signal(signal.ts, self.cfg.symbol, "risk_block",
                                       f"{signal.reason} | blocked: {decision.reason}")
            return
        qty = self.risk.position_size(signal.ref_price, signal.sl_pct)
        if qty <= 0:
            return
        pos = self.broker.open_position(signal, self.last_quote, qty)
        self.journal.record_signal(signal.ts, self.cfg.symbol, "signal", signal.reason)
        log.info("OPEN %s qty=%.6f @ %.2f sl=%.2f tp=%.2f | %s",
                 pos.side, pos.qty, pos.entry_price, pos.sl_price, pos.tp_price, signal.reason)

    def _handle_close(self, trade: ClosedTrade) -> None:
        self.risk.on_trade_closed(trade.net_pnl, trade.exit_ts)
        self.journal.record_trade(self.cfg.symbol, trade)
        self.journal.record_equity(trade.exit_ts, self.risk.equity)
        log.info("CLOSE %s @ %.2f (%s) net=%.2f fees=%.2f equity=%.2f",
                 trade.side, trade.exit_price, trade.reason_exit, trade.net_pnl,
                 trade.fees, self.risk.equity)
        if self.risk.halted_reason:
            log.warning("RISK HALT: %s", self.risk.halted_reason)

    def _publish_state(self, ts: float) -> None:
        now = time.time()
        if now - self._last_snapshot < 2.0:
            return
        self._last_snapshot = now
        pos = self.broker.position
        quote = self.last_quote
        unrealized = pos.unrealized(quote) if pos and quote else 0.0
        self.journal.set_state("live", {
            "ts": ts,
            "symbol": self.cfg.symbol,
            "last_price": quote.mid if quote else None,
            "equity": self.risk.equity,
            "unrealized": unrealized,
            "halted": self.risk.halted_reason,
            "consecutive_losses": self.risk.consecutive_losses,
            "position": None if pos is None else {
                "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price,
                "sl": pos.sl_price, "tp": pos.tp_price, "entry_ts": pos.entry_ts,
            },
            "indicators": {
                "vwap": self.strategy.snapshot.vwap,
                "ema9": self.strategy.snapshot.ema_fast,
                "ema21": self.strategy.snapshot.ema_slow,
                "rsi": self.strategy.snapshot.rsi,
                "atr": self.strategy.snapshot.atr,
            },
        })
        if now - getattr(self, "_last_equity_snap", 0.0) >= EQUITY_SNAPSHOT_SECONDS:
            self._last_equity_snap = now
            self.journal.record_equity(ts, self.risk.equity + unrealized)


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

    log.info("paper trading %s | candle=%ss fee=%.0fbps slip=%.0fbps equity=%.2f",
             cfg.symbol, cfg.candle_seconds, cfg.fee_bps, cfg.slippage_bps, cfg.starting_equity)
    try:
        async for event in feed.stream():
            engine.on_event(event)
    finally:
        journal.set_state("live", {"ts": time.time(), "stopped": True,
                                   "equity": engine.risk.equity})
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
