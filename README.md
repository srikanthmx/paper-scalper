# paper-scalper

Paper-only scalping simulator. **This codebase contains no path to a real order** — enforced by
`tests/test_no_real_orders.py` (no trading hosts/endpoints anywhere; engine modules may not even
import network libraries).

```
Alpaca BTC/USD websocket (data only)      [later: Upstox Market Data Feed V3, data only]
        ↓
normalizer → candle builder → indicators (VWAP / EMA 9-21 / RSI / ATR)
        ↓
signal engine → risk manager → paper order simulator (slippage + fees)
        ↓
SQLite journal → dashboard + review report
```

## Setup

```bash
cd paper-scalper
uv sync
cp .env.example .env   # add ALPACA_API_KEY / ALPACA_API_SECRET (free data keys)
```

## Run

```bash
# live BTC/USD market data, paper fills
uv run python -m paper_scalper.main --feed alpaca

# no keys? keyless smoke test with synthetic ticks
uv run python -m paper_scalper.main --feed synthetic

# dashboard → http://127.0.0.1:8765
# live candlestick chart with entry/exit markers + SL/TP lines, equity curve,
# open position, signal activity feed, full trade journal, daily reports
uv run python -m paper_scalper.dashboard.app

# journal review (markdown report — paste into Claude for analysis)
uv run python -m paper_scalper.review

# tests, including the no-real-orders kill switch
uv run pytest
```

## Safety invariants

1. Only host the data feed may reach: `stream.data.alpaca.markets`. Upstox (when added) will be
   market-data websocket only — never `api-hft.upstox.com` or any order endpoint.
2. `engine/` and `storage/` modules import no network libraries (AST-checked in tests).
3. The paper broker fills from live bid/ask plus configured slippage, charges fees per side, and
   tracks SL / TP / breakeven / max-hold exits. All trades land in SQLite.
4. Risk manager: per-trade risk sizing, −2% daily stop, halt after 3 consecutive losses,
   cooldown candles between trades. Halts reset at UTC midnight.

## Strategy

See [STRATEGY.md](STRATEGY.md) for the v1 evaluation and the v1.1 changes (fee math, ATR-adaptive
exits, spread/chop filters).

## Roadmap

- [x] Milestone 1: BTC/USD via Alpaca, full paper loop, dashboard, journal
- [ ] Observe a few days; run `python -m paper_scalper.review` daily
- [ ] Milestone 2: Upstox Market Data Feed V3 adapter (data only) + 1-min Indian equity scalps
      (needs exchange-session VWAP anchor + market-hours gating, both already seamed)
- [ ] Only after net-positive paper results: discuss live — via Alpaca paper API first, never Upstox orders
