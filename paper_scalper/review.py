"""Trade-journal review: prints a markdown report you can paste into Claude for analysis.

    python -m paper_scalper.review
"""

from __future__ import annotations

from datetime import datetime, timezone

from paper_scalper.config import Settings
from paper_scalper.storage.db import Journal


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    cfg = Settings()
    journal = Journal(cfg.db_path)
    s = journal.summary()
    trades = journal.trades(limit=100_000)

    print(f"# Paper-trading review — {cfg.symbol}")
    print(f"\nGenerated {datetime.now(tz=timezone.utc):%Y-%m-%d %H:%M UTC}. "
          f"Costs modeled: fee {cfg.fee_bps}bps/side, slippage {cfg.slippage_bps}bps/side.\n")
    print("## Summary\n")
    pf = s["profit_factor"]
    print(f"- Trades: {s['trades']} (W {s['wins']} / L {s['losses']}, "
          f"win rate {s['win_rate_pct']:.1f}%)")
    print(f"- Net PnL: ${s['net_pnl']:.2f}  |  Gross: ${s['gross_pnl']:.2f}  |  "
          f"Fees: ${s['total_fees']:.2f}")
    if s["gross_pnl"] != 0:
        print(f"- Fee drag: {s['total_fees'] / abs(s['gross_pnl']) * 100:.0f}% of gross PnL")
    print(f"- Profit factor: {pf:.2f}" if pf is not None else "- Profit factor: n/a")
    print(f"- Avg win ${s['avg_win']:.2f} / avg loss ${s['avg_loss']:.2f}")
    print(f"- Max drawdown: {s['max_drawdown_pct']:.2f}%")
    print(f"- Exits: {s['exit_reasons']}")

    if trades:
        print("\n## Last 20 trades\n")
        print("| exit (UTC) | side | entry | exit | net $ | net % | exit reason |")
        print("|---|---|---|---|---|---|---|")
        for t in trades[:20]:
            print(f"| {_ts(t['exit_ts'])} | {t['side']} | {t['entry_price']:.2f} "
                  f"| {t['exit_price']:.2f} | {t['net_pnl']:.2f} "
                  f"| {t['net_pnl_pct']:.3f} | {t['reason_exit']} |")

    print("\n## Questions for review\n")
    print("- Is net PnL positive after fees, or is gross edge being eaten by costs?")
    print("- Which exit reason dominates? (max_hold dominating = targets too far "
          "or entries late; stop_loss dominating = entries are chasing)")
    print("- Win rate vs avg win/loss: does the R:R justify the hit rate?")
    journal.close()


if __name__ == "__main__":
    main()
