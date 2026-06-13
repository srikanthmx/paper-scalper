from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Data credentials (market data only — no trading scope is ever used)
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""

    symbol: str = "BTC/USD"
    candle_seconds: int = 60

    # Paper account / costs — LEARNING MODE: fees zeroed to study entry/exit logic
    # in isolation. Re-enable (25bps Alpaca taker) before judging any go/no-go.
    starting_equity: float = 10_000.0
    fee_bps: float = 0.0        # learning mode (Alpaca crypto tier 1 taker: 25.0)
    slippage_bps: float = 0.5   # Coinbase tight book (~$3 r/t on BTC). 3.0 was an
                                # Alpaca-era guess that swamped small-target scalps.

    # Risk — LEARNING MODE: halts and cooldowns disabled, trade freely
    enable_risk_halts: bool = False      # True restores daily stop + loss-streak halts
    risk_per_trade_pct: float = 0.5      # % of equity risked at SL per trade
    max_notional_fraction: float = 2.0   # paper leverage cap; at 0.5 it silently overrode
                                         # risk_per_trade sizing on every BTC trade
    daily_stop_pct: float = 5.0          # only used when enable_risk_halts
    max_consecutive_losses: int = 5      # only used when enable_risk_halts
    # Hold must scale with the target: a 1:2 trade needs ~30min for 2R to be
    # reachable. At 300s, random-entry 1:2 wins only ~8% (proven by backtest) —
    # the target is physically unreachable before the clock forces a verdict.
    max_hold_seconds: int = 1800
    cooldown_candles: int = 0            # learning mode (production: 3)

    # Strategy filters
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    vol_sma_period: int = 20
    vol_spike_mult: float = 1.15     # production: 1.3
    rsi_long_min: float = 40.0       # production: 45
    rsi_long_max: float = 75.0       # production: 65
    rsi_short_min: float = 25.0      # production: 35
    rsi_short_max: float = 60.0      # production: 55
    max_spread_bps: float = 20.0    # Alpaca BTC/USD spread is structurally 6-12bps (production: 5)
    ema_sep_min_bps: float = 1.0     # production: 2.0
    pullback_tolerance_pct: float = 0.10  # how close price must come to EMA9 to count as pullback
    min_atr_pct: float = 0.015       # production: 0.03
    max_atr_pct: float = 0.60        # skip news spikes

    # Momentum breakout lane
    momo_lookback: int = 12          # candles for breakout high/low
    momo_vol_mult: float = 1.10

    # Mean-reversion lane
    mr_rsi_low: float = 32.0
    mr_rsi_high: float = 68.0
    mr_vwap_atr_mult: float = 0.8    # required stretch from VWAP in ATRs
    mr_max_hold_seconds: int = 900   # fades need longer than momentum scalps

    # Trend-scalp lane (strict 1:2 with scale-out + trailing stop)
    trend_sl_atr_mult: float = 1.0
    trend_sl_min_pct: float = 0.15
    trend_sl_max_pct: float = 0.60
    trend_rr: float = 2.0            # TP1 at +2R
    trend_scale_out_frac: float = 0.5  # close half at TP1, SL jumps to +1R
    trend_max_hold_seconds: int = 1800

    # Exits (ATR-adaptive, clamped to the spec's bounds)
    sl_atr_mult: float = 1.2
    tp_atr_mult: float = 2.0
    sl_min_pct: float = 0.25
    sl_max_pct: float = 0.45
    tp_min_pct: float = 0.45
    tp_max_pct: float = 0.90
    breakeven_after_r: float = 0.6   # move SL to entry once unrealized gain >= 0.6R

    db_path: str = "paper_scalper.db"
