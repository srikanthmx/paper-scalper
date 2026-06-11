# Strategy evaluation & v1.1 changes

## Evaluation of spec v1 (the plan as given)

**1. Fees are the elephant — the spec ignored them.**
Alpaca crypto taker fee is ~25 bps per side at the lowest tier. Round trip with 3 bps slippage
per side ≈ **0.56%**. The spec's TP band is 0.4–0.7%: at the low end every winner is a net
loser, and SL 0.25–0.4% means losers cost SL + 0.56%. For the strategy to have any chance,
costs must be modeled honestly (we do: fees + slippage per side) and targets must sit at the
upper end of the band. The journal records gross vs net so fee drag is visible from day one.
If observation shows gross-positive / net-negative, the fix is fewer + bigger trades, not more.

**2. Fixed % SL/TP doesn't fit a volatility-clustered asset.**
0.3% is huge in quiet hours and noise during a hot hour. v1.1 sizes exits from ATR
(SL = 1.2×ATR, TP = 2.0×ATR) but clamps them inside the spec's bounds (SL 0.25–0.45%,
TP 0.45–0.9%). TP floor 0.45% keeps targets above round-trip cost.

**3. Bare EMA crossover + RSI band fires constantly in chop.**
Added entry filters:
- **EMA separation floor** (≥2 bps): require an actual trend, not a wobble through the cross.
- **ATR band** (0.03%–0.60%): skip dead chop (no edge) and news candles (uncontrolled risk).
- **Spread filter** (≤5 bps): a wide spread is an extra invisible fee; scalps can't pay it.
- **Pullback made concrete**: prior/current candle must touch EMA9 (±0.1%), then close back
  beyond it in trend direction. The spec said "enter after small pullback" without defining it.

**4. No re-entry discipline.**
After any exit there's a cooldown (3 candles) so one trend doesn't get churned into five
fee-paying trades. Risk halts (daily −2%, 3 consecutive losses) reset at UTC midnight.

**5. Exits improved.**
Added a **breakeven stop**: once unrealized gain ≥ 0.6R, SL moves to entry. Scalps that almost
reach TP and round-trip back to a full stop are a big silent cost in this style. Max-hold 5 min
kept from spec.

**6. Position sizing was unspecified.**
v1.1 risks a fixed **0.5% of equity per trade** (qty = risk $ / SL distance), capped at 50% of
equity notional. This makes the −2% daily stop meaningful: ~4 max-loss trades end the day.

## What to watch during the observation window

Run `uv run python -m paper_scalper.review` daily and look at:

| Symptom | Likely cause | Adjustment |
|---|---|---|
| Gross > 0, net < 0 | fee drag | raise `TP_MIN_PCT`, raise `VOL_SPIKE_MULT` (trade less) |
| `max_hold` dominates exits | targets too far / entries late | lower `tp_atr_mult` or shorten hold |
| `stop_loss` dominates | chasing entries | raise `ema_sep_min_bps`, tighten RSI bands |
| `breakeven_stop` frequent | good entries, greedy TP | lower `tp_atr_mult` slightly |
| Very few signals | filters too tight | check `signal_log` rejects table for the binding filter |

The `signal_log` table records every rejected setup with the reason, so tuning is data-driven
rather than guesswork.

## Known modeling limitations (acceptable for paper)

- Fills assume full size at bid/ask ± slippage; no book depth / partial fills.
- Intra-candle SL+TP both touched → quote order decides (we check TP first only after breakeven
  logic; conservative enough at 15s–60s candles with tick-level exit checks).
- Synthetic feed is for plumbing tests only — never tune parameters on it.

## Go/no-go bar for "make it live"

Suggested bar before even discussing live: **≥ 100 trades observed, net PnL > 0 after fees,
profit factor ≥ 1.3, max drawdown < 3%, and no single day < −2%.** Anything less is noise.
