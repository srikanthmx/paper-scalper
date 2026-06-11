"""Hard kill switch: the codebase must contain no path to a real broker order.

Two layers:
1. Static scan — no trading-API hosts or order endpoints anywhere in the package.
2. Import isolation — the paper broker and engine modules must not import any
   network library, so they physically cannot send an order.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "paper_scalper"

FORBIDDEN_SUBSTRINGS = [
    # Alpaca trading hosts (data hosts are stream.data./data.alpaca.markets)
    "api.alpaca.markets",
    "paper-api.alpaca.markets",
    "/v2/orders",
    # Coinbase trading/brokerage hosts (data host is ws-feed.exchange.coinbase.com)
    "api.exchange.coinbase.com",
    "api.coinbase.com",
    # Upstox order/trading endpoints — Upstox is for market data ONLY
    "api-hft.upstox.com",
    "/order/place",
    "/order/modify",
    "/order/cancel",
    "order_api",
    "place_order",
    "submit_order",
    "create_order",
]

NETWORK_MODULES = {
    "websockets", "aiohttp", "httpx", "requests", "urllib", "urllib3",
    "socket", "http", "ssl",
}

# Only feed adapters and the dashboard may touch the network.
NETWORK_ALLOWED_FILES = {"alpaca_crypto_feed.py", "coinbase_feed.py", "app.py"}


def _py_files() -> list[Path]:
    files = list(PACKAGE.rglob("*.py"))
    assert files, "package sources not found"
    return files


def test_no_trading_endpoints_anywhere() -> None:
    for path in _py_files():
        source = path.read_text().lower()
        for needle in FORBIDDEN_SUBSTRINGS:
            assert needle not in source, f"forbidden trading reference '{needle}' in {path}"


def test_engine_modules_cannot_reach_network() -> None:
    for path in _py_files():
        if path.name in NETWORK_ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            bad = set(names) & NETWORK_MODULES
            assert not bad, f"{path} imports network module(s) {bad}"


def test_data_feeds_only_connect_to_data_hosts() -> None:
    from paper_scalper.data import alpaca_crypto_feed, coinbase_feed

    assert alpaca_crypto_feed.ALLOWED_HOST == "stream.data.alpaca.markets"
    assert alpaca_crypto_feed.WS_URL.startswith("wss://stream.data.alpaca.markets")
    assert coinbase_feed.ALLOWED_HOST == "ws-feed.exchange.coinbase.com"
    assert coinbase_feed.WS_URL.startswith("wss://ws-feed.exchange.coinbase.com")
