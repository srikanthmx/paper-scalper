from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Data credentials (market data only — no trading scope is ever used)
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""

    symbol: str = "BTC/USD"
    candle_seconds: int = 60

    # Paper account / costs
    starting_equity: float = 10_000.0
    fee_bps: float = 25.0       # taker fee per side (Alpaca crypto tier 1)
    slippage_bps: float = 3.0   # applied per side on top of bid/ask

    # Risk — AGGRESSIVE TEST TUNING (paper observation; tighten before judging go/no-go)
    risk_per_trade_pct: float = 0.5      # % of equity risked at SL per trade
    max_notional_fraction: float = 0.5   # cap position notional vs equity
    daily_stop_pct: float = 5.0          # halt for the UTC day (production: 2.0)
    max_consecutive_losses: int = 5      # production: 3
    max_hold_seconds: int = 300
    cooldown_candles: int = 1            # production: 3

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
    max_spread_bps: float = 5.0
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

    # Exits (ATR-adaptive, clamped to the spec's bounds)
    sl_atr_mult: float = 1.2
    tp_atr_mult: float = 2.0
    sl_min_pct: float = 0.25
    sl_max_pct: float = 0.45
    tp_min_pct: float = 0.45
    tp_max_pct: float = 0.90
    breakeven_after_r: float = 0.6   # move SL to entry once unrealized gain >= 0.6R

    db_path: str = "paper_scalper.db"
