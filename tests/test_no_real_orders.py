"""Kill switch: the codebase must contain no path to a REAL-MONEY broker order.

Paper-sandbox trading (Alpaca paper) is allowed, isolated to one file, so we can
validate fills on a platform's matching engine. Live-money trading is forbidden
everywhere, permanently.

Layers:
1. No live-money trading host or generic order verb appears in ANY file.
2. The paper sandbox host / order path may appear ONLY in broker/alpaca_paper.py.
3. Engine/storage modules import no network library (they physically can't trade).
4. The paper broker is pinned to the paper host and can never reach the live host.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "paper_scalper"

# These must appear in NO source file — live money, ever.
FORBIDDEN_EVERYWHERE = [
    "://api.alpaca.markets",        # Alpaca LIVE host (paper is paper-api.alpaca.markets)
    "api.exchange.coinbase.com",    # Coinbase brokerage/trading
    "api.coinbase.com",
    "api-hft.upstox.com",           # Upstox is market-data ONLY
    "/order/place",
    "/order/modify",
    "/order/cancel",
    "order_api",
    "place_order",
    "submit_order",
    "create_order",
]

# Paper-sandbox order references — allowed, but ONLY inside the paper broker.
PAPER_ONLY = ["paper-api.alpaca.markets", "/v2/orders"]
PAPER_BROKER_FILE = "alpaca_paper.py"

NETWORK_MODULES = {
    "websockets", "aiohttp", "httpx", "requests", "urllib", "urllib3",
    "socket", "http", "ssl",
}
# Feed adapters, the dashboard, and the paper-sandbox broker may touch the network.
NETWORK_ALLOWED_FILES = {"alpaca_crypto_feed.py", "coinbase_feed.py", "app.py",
                         PAPER_BROKER_FILE}


def _py_files() -> list[Path]:
    files = list(PACKAGE.rglob("*.py"))
    assert files, "package sources not found"
    return files


def test_no_live_money_trading_anywhere() -> None:
    for path in _py_files():
        source = path.read_text().lower()
        for needle in FORBIDDEN_EVERYWHERE:
            assert needle.lower() not in source, f"forbidden live-money ref '{needle}' in {path}"


def test_paper_order_refs_only_in_paper_broker() -> None:
    for path in _py_files():
        source = path.read_text().lower()
        for needle in PAPER_ONLY:
            if needle.lower() in source:
                assert path.name == PAPER_BROKER_FILE, (
                    f"paper-order reference '{needle}' must live only in {PAPER_BROKER_FILE}, "
                    f"found in {path}")


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


def test_paper_broker_pinned_to_paper_host_never_live() -> None:
    from paper_scalper.broker import alpaca_paper

    assert alpaca_paper.PAPER_HOST == "paper-api.alpaca.markets"
    assert alpaca_paper.BASE_URL == "https://paper-api.alpaca.markets"
    # the live-money host (with protocol) must never be the base URL
    assert "://api.alpaca.markets" not in alpaca_paper.BASE_URL
    assert alpaca_paper.BASE_URL.startswith("https://paper-")
