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
from paper_scalper.engine.daily import DailyStrategy
from paper_scalper.engine.keltner import KeltnerReversionStrategy
from paper_scalper.engine.lorentz import LorentzianStrategy
from paper_scalper.engine.meanrev import MeanReversionStrategy
from paper_scalper.engine.momentum import MomentumStrategy
from paper_scalper.engine.paper_broker import ClosedTrade, PaperBroker
from paper_scalper.engine.risk import RiskManager
from paper_scalper.engine.strategy import Signal, Snapshot, Strategy
from paper_scalper.engine.tester import TesterStrategy
from paper_scalper.engine.trend import TrendScalpStrategy
from paper_scalper.storage.db import Journal

log = logging.getLogger("paper_scalper")

EQUITY_SNAPSHOT_SECONDS = 30
STALE_GAP_SECONDS = 30  # a feed gap longer than this means prices are unreliable


class StrategyProtocol(Protocol):
    name: str
    snapshot: Snapshot
    p: dict

    def on_candle(self, candle: Candle,
                  quote: Quote | None) -> Signal | list[Signal] | None: ...

    def apply_params(self, new: dict) -> None: ...


def stop_inside_spread(signal: Signal, quote: Quote, cfg: Settings) -> bool:
    """True when the stop distance can't survive entry costs: crossing the spread
    plus slippage puts the mark below the SL immediately (instant stop-out).
    Found live: a $120 spread during a spike insta-killed every trend entry."""
    sl_bps = signal.sl_pct * 100
    entry_cost_bps = quote.spread_bps + 2 * cfg.slippage_bps
    return sl_bps <= entry_cost_bps * 1.2


def build_strategies(cfg: Settings, only: str | None = None) -> list[StrategyProtocol]:
    lanes = [Strategy(cfg), MomentumStrategy(cfg), MeanReversionStrategy(cfg),
             TrendScalpStrategy(cfg), DailyStrategy(cfg), LorentzianStrategy(cfg),
             KeltnerReversionStrategy(cfg), TesterStrategy(cfg)]
    if only:
        wanted = {n.strip() for n in only.split(",")}
        lanes = [l for l in lanes if l.name in wanted]
        if not lanes:
            raise SystemExit(f"--only {only}: no matching strategies")
    return lanes


class Lane:
    """One strategy with its own paper account, risk limits, and broker."""

    def __init__(self, strategy: StrategyProtocol, cfg: Settings) -> None:
        self.name = strategy.name
        self.strategy = strategy
        self.timeframe = getattr(strategy, "timeframe_seconds", cfg.candle_seconds)
        self.risk = RiskManager(cfg)
        # Route the designated lane to the Alpaca paper sandbox (real fills); all
        # other lanes use the in-app simulator. source tags every trade.
        if cfg.execution_mode == "alpaca_paper" and self.name == cfg.platform_lane:
            from paper_scalper.broker.alpaca_paper import AlpacaPaperBroker
            self.broker = AlpacaPaperBroker(cfg, cfg.symbol)
            self.source = "alpaca_paper"
        else:
            self.broker = PaperBroker(cfg)
            self.source = "app"
        self.version = 0       # active param version (journal param_versions)
        self.open_version = 0  # version the current position was entered under
        self.pending: list[tuple[Signal, float]] = []  # armed stop orders (signal, expiry)


class Engine:
    def __init__(self, cfg: Settings, journal: Journal,
                 strategies: list[StrategyProtocol] | None = None,
                 only: str | None = None) -> None:
        self.cfg = cfg
        self.journal = journal
        self.lanes = [Lane(s, cfg) for s in (strategies or build_strategies(cfg, only))]
        # one candle builder per timeframe; lanes subscribe to their own TF.
        # cfg.candle_seconds is always built — it feeds the chart journal.
        self.lanes_by_tf: dict[int, list[Lane]] = {}
        for lane in self.lanes:
            self.lanes_by_tf.setdefault(lane.timeframe, []).append(lane)
        self.builders: dict[int, CandleBuilder] = {
            tf: CandleBuilder(tf)
            for tf in {cfg.candle_seconds, *self.lanes_by_tf.keys()}
        }
        self.last_quote: Quote | None = None
        self._last_snapshot = 0.0
        self._last_equity_snap = 0.0
        self._events_since_pub = 0
        self._last_price_ts: float | None = None
        self._gap_skip = False  # block new entries on the tick right after a feed gap
        for lane in self.lanes:
            self._sync_params(lane, bootstrap=True)
        self._warmup_from_history()

    def _warmup_from_history(self) -> None:
        """Pre-train strategies on stored candles so they're ready immediately
        instead of warming up live (Lorentzian otherwise needs ~hours of history
        to fill its kNN training set). Replays candles through on_candle with no
        quote, building indicator + ML state without opening any trades."""
        raw = self.journal.candles(limit=20000)
        if len(raw) < 60:
            return
        for tf, lanes in self.lanes_by_tf.items():
            bars = self._resample(raw, tf)
            for lane in lanes:
                for c in bars:
                    lane.strategy.on_candle(c, None)
        log.info("warmed up %d lanes from %d stored candles",
                 len(self.lanes), len(raw))

    @staticmethod
    def _resample(raw: list[dict], tf: int) -> list[Candle]:
        out: list[Candle] = []
        cur: Candle | None = None
        for k in raw:
            b = k["ts_open"] - (k["ts_open"] % tf)
            if cur is None or b > cur.ts_open:
                if cur is not None:
                    out.append(cur)
                cur = Candle(ts_open=b, open=k["open"], high=k["high"], low=k["low"],
                             close=k["close"], volume=k["volume"],
                             notional=(k["high"] + k["low"] + k["close"]) / 3 * k["volume"])
            else:
                cur.high = max(cur.high, k["high"])
                cur.low = min(cur.low, k["low"])
                cur.close = k["close"]
                cur.volume += k["volume"]
                cur.notional += (k["high"] + k["low"] + k["close"]) / 3 * k["volume"]
        if cur is not None:
            out.append(cur)
        return out

    def _sync_params(self, lane: Lane, bootstrap: bool = False) -> None:
        """Apply dashboard-saved params; tag the lane with the active version."""
        state = self.journal.get_state(f"params:{lane.name}")
        if state is None:
            if bootstrap:
                lane.version = self.journal.save_param_version(
                    lane.name, dict(lane.strategy.p), note="initial defaults")
            return
        if state["version"] != lane.version:
            lane.strategy.apply_params(state["params"])
            lane.version = state["version"]
            log.info("[%s] params v%d applied: %s", lane.name, lane.version,
                     state["params"])

    def on_event(self, event: Trade | Quote) -> None:
        # Detect a feed gap (e.g. websocket reconnect): price may have jumped while
        # we were blind. Block NEW entries on this tick so we never open at a stale
        # price into a gap. Open positions still get their exits — holding through an
        # outage is real risk — but we won't add fresh exposure on bad data.
        if self._last_price_ts is not None and event.ts - self._last_price_ts > STALE_GAP_SECONDS:
            self._gap_skip = True
            log.warning("feed gap %.0fs — blocking new entries this tick",
                        event.ts - self._last_price_ts)
        self._last_price_ts = event.ts
        # Exits and pending fills must be checked on EVERY price update, not just
        # quotes. Coinbase sends far more trade ticks than quotes; checking only on
        # quotes lets stops/targets trigger late (a stop fires well past its level,
        # turning a 1R loss into several R). A trade tick is marked against the last
        # known spread so fills stay realistic.
        if isinstance(event, Quote):
            self.last_quote = event
            mark_quote: Quote | None = event
        elif self.last_quote is not None:
            half = (self.last_quote.ask - self.last_quote.bid) / 2
            mark_quote = Quote(ts=event.ts, symbol=event.symbol,
                               bid=event.price - half, ask=event.price + half,
                               bid_size=0.0, ask_size=0.0)
        else:
            mark_quote = None  # no spread known yet (no quote seen) — skip exit check
        if mark_quote is not None:
            for lane in self.lanes:
                closed = lane.broker.on_quote(mark_quote)
                if closed is not None:
                    self._handle_close(lane, closed)
                self._check_pending(lane, mark_quote)
        for tf, builder in self.builders.items():
            completed = (builder.on_quote(event) if isinstance(event, Quote)
                         else builder.on_trade(event))
            if completed is None:
                continue
            if tf == self.cfg.candle_seconds:
                self.journal.record_candle(self.cfg.symbol, completed.ts_open,
                                           completed.open, completed.high, completed.low,
                                           completed.close, completed.volume)
            self._on_candle_closed(completed, self.lanes_by_tf.get(tf, []))
        self._gap_skip = False  # only the gap tick itself is blocked
        self._publish_state(event.ts)

    def _check_pending(self, lane: Lane, quote: Quote) -> None:
        """Fill armed stop orders on the tick that crosses them (OCO per lane)."""
        if not lane.pending:
            return
        if lane.broker.position is not None:
            return
        still_armed: list[tuple[Signal, float]] = []
        for sig, expiry in lane.pending:
            if quote.ts >= expiry:
                self.journal.record_signal(quote.ts, self.cfg.symbol, lane.name, "reject",
                                           f"stop entry expired unfilled @ {sig.entry_stop:.2f}")
                continue
            sign = 1.0 if sig.side == "long" else -1.0
            if sig.entry_stop is not None and sign * (quote.mid - sig.entry_stop) >= 0:
                if self._try_open(lane, sig, quote):
                    lane.pending = []  # OCO: a fill cancels every sibling order
                    return
            still_armed.append((sig, expiry))
        lane.pending = still_armed

    def _on_candle_closed(self, candle: Candle, lanes: list[Lane]) -> None:
        for lane in lanes:
            self._sync_params(lane)
            lane.risk.on_candle()
            result = lane.strategy.on_candle(candle, self.last_quote)
            snap = lane.strategy.snapshot
            if snap.rejects and snap.rejects != ["warming_up"]:
                self.journal.record_signal(candle.ts_open, self.cfg.symbol, lane.name,
                                           "reject", "; ".join(snap.rejects))
            signals = result if isinstance(result, list) else [result] if result else []
            for signal in signals:
                if signal.entry_type == "stop":
                    lane.pending.append((signal, signal.ts + signal.valid_seconds))
                    self.journal.record_signal(signal.ts, self.cfg.symbol, lane.name,
                                               "signal", f"ARMED: {signal.reason}")
                    log.info("[%s] ARMED stop %s @ %.2f", lane.name, signal.side,
                             signal.entry_stop or 0.0)
                elif lane.broker.position is None and self.last_quote is not None:
                    self._try_open(lane, signal, self.last_quote)

    def _try_open(self, lane: Lane, signal: Signal, quote: Quote) -> bool:
        if self._gap_skip:
            self.journal.record_signal(quote.ts, self.cfg.symbol, lane.name, "risk_block",
                                       f"{signal.reason} | blocked: feed gap, stale price")
            return False
        decision = lane.risk.can_enter(quote.ts)
        if not decision.allowed:
            log.info("[%s] signal blocked by risk: %s", lane.name, decision.reason)
            self.journal.record_signal(quote.ts, self.cfg.symbol, lane.name, "risk_block",
                                       f"{signal.reason} | blocked: {decision.reason}")
            return False
        if not signal.allow_tight_stop and stop_inside_spread(signal, quote, self.cfg):
            self.journal.record_signal(
                quote.ts, self.cfg.symbol, lane.name, "risk_block",
                f"{signal.reason} | blocked: stop {signal.sl_pct * 100:.1f}bps inside "
                f"spread {quote.spread_bps:.1f}bps — instant stop-out")
            return False
        qty = lane.risk.position_size(signal.ref_price, signal.sl_pct)
        if qty <= 0:
            return False
        pos = lane.broker.open_position(signal, quote, qty)
        if pos is None:  # platform rejected the order (e.g. Alpaca paper)
            self.journal.record_signal(quote.ts, self.cfg.symbol, lane.name, "risk_block",
                                       f"{signal.reason} | blocked: platform rejected order")
            return False
        lane.open_version = lane.version
        lane.pending = []
        self.journal.record_signal(quote.ts, self.cfg.symbol, lane.name, "signal",
                                   f"[{lane.source}] {signal.reason}")
        log.info("[%s] OPEN %s qty=%.6f @ %.2f sl=%.2f tp=%.2f | %s", lane.name,
                 pos.side, pos.qty, pos.entry_price, pos.sl_price, pos.tp_price,
                 signal.reason)
        return True

    def _handle_close(self, lane: Lane, trade: ClosedTrade) -> None:
        lane.risk.on_trade_closed(trade.net_pnl, trade.exit_ts)
        self.journal.record_trade(self.cfg.symbol, lane.name, trade,
                                  version=lane.open_version, source=lane.source)
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
            "version": lane.version,
            "source": lane.source,
            "tf": lane.timeframe,
            "pending": [{"side": s.side, "entry_stop": s.entry_stop, "expiry": exp}
                        for s, exp in lane.pending],
            "unrealized": unrealized,
            "halted": lane.risk.halted_reason,
            "consecutive_losses": lane.risk.consecutive_losses,
            "position": None if pos is None else {
                "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price,
                "sl": pos.sl_price, "tp": pos.tp_price, "entry_ts": pos.entry_ts,
            },
        }

    def _publish_state(self, ts: float) -> None:
        self._events_since_pub += 1
        now = time.time()
        if now - self._last_snapshot < 2.0:
            return
        elapsed = max(now - self._last_snapshot, 1e-9)
        tick_rate = self._events_since_pub / elapsed * 60 if self._last_snapshot else 0.0
        self._events_since_pub = 0
        self._last_snapshot = now
        quote = self.last_quote
        forming = self.builders[self.cfg.candle_seconds].current
        lanes = {lane.name: self._lane_state(lane) for lane in self.lanes}
        pullback = next((l.strategy.snapshot for l in self.lanes if l.name == "pullback"),
                        Snapshot())
        self.journal.set_state("live", {
            "ts": ts,
            "symbol": self.cfg.symbol,
            "execution_mode": self.cfg.execution_mode,
            "platform_lane": self.cfg.platform_lane,
            "last_price": quote.mid if quote else None,
            "bid": quote.bid if quote else None,
            "ask": quote.ask if quote else None,
            "spread_bps": quote.spread_bps if quote else None,
            "tick_ts": quote.ts if quote else None,
            "ticks_per_min": round(tick_rate, 1),
            "forming": None if forming is None else {
                "ts_open": forming.ts_open, "open": forming.open, "high": forming.high,
                "low": forming.low, "close": forming.close, "volume": forming.volume,
            },
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


async def run(feed_name: str, cfg: Settings, only: str | None = None) -> None:
    journal = Journal(cfg.db_path)
    engine = Engine(cfg, journal, only=only)
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
    parser.add_argument("--only", default=None,
                        help="run only these lanes (comma-separated), pausing all others")
    args = parser.parse_args()
    cfg = Settings()
    if args.symbol:
        cfg.symbol = args.symbol
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args.feed, cfg, only=args.only))
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
