"""
거래 내역 상세 분석
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import load_config, strategy_config_from
from python_app.backtest.bar_store import BarStore
from python_app.backtest.download import HISTORY_DB
from python_app.backtest.replay import BacktestConfig, Replay
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.open_strategy import OpenSettings, OpenStrategy


def main():
    store = BarStore(HISTORY_DB)
    dates = [d for d in store.dates() if d >= "2026-11-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    cfg = load_config()
    common = strategy_config_from(cfg)

    replay = Replay(lambda: [OpenStrategy(OpenSettings(enabled=True))], common,
                    ScannerConfig.from_dict(cfg.get("scanner")),
                    BacktestConfig(slippage_ticks=1, use_scanner=True), symbols,
                    risk=RiskConfig.from_dict(cfg.get("risk")))

    results = replay.run(store, dates)
    trades = [t for r in results for t in r.trades]

    print(f"최근 {len(dates)}일간 거래 건수: {len(trades)}")
    for t in trades[:25]:
        ret = (t.exit_price / t.entry_price - 1.0) * 100.0 if t.entry_price else 0.0
        print(f"[{t.entry_time.strftime('%m-%d %H:%M')}] {t.symbol} {t.name:<8s} ({t.pattern}) "
              f"진입: {t.entry_price:>7,.0f} | 청산: {t.exit_price:>7,.0f} ({ret:+5.2f}%) | "
              f"손익: {t.pnl:+8,.0f}원 | 사유: {t.exit_reason}")


if __name__ == "__main__":
    main()

