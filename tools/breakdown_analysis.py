"""
상반기(2026-01 ~ 2026-06) 및 하반기(2026-07 ~ 2026-11) 전 패턴 분해 분석
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import load_config, strategy_config_from
from python_app.backtest.bar_store import BarStore
from python_app.backtest.download import HISTORY_DB
from python_app.backtest.metrics import grouped, format_table
from python_app.backtest.replay import BacktestConfig, Replay
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.open_strategy import OpenSettings, OpenStrategy
from python_app.strategies.intraday_strategy import IntradaySettings, IntradayStrategy
from python_app.strategies.close_strategy import CloseSettings, CloseStrategy


def analyze_period(label, dates, store, symbols, base_cfg):
    cfg = dict(base_cfg)
    common = strategy_config_from(cfg)

    def factory():
        return [
            OpenStrategy(OpenSettings(enabled=True, max_stop_pct=1.5)),
            IntradayStrategy(IntradaySettings(enabled=True, use_breakout=False, max_stop_pct=1.5)),
            CloseStrategy(CloseSettings(enabled=True, allow_neutral_weekly=True, max_stop_pct=1.5))
        ]

    replay = Replay(factory, common, ScannerConfig.from_dict(cfg.get("scanner")),
                    BacktestConfig(slippage_ticks=1, use_scanner=True), symbols,
                    risk=RiskConfig.from_dict(cfg.get("risk")))

    results = replay.run(store, dates)
    trades = [t for r in results for t in r.trades]
    print(f"\n=================== {label} ({dates[0]} ~ {dates[-1]}, {len(dates)}일) ===================")
    print(f"체결: {len(trades)}건")
    if trades:
        print(format_table(grouped(trades, lambda t: f"{t.strategy}.{t.pattern}")))


def main():
    store = BarStore(HISTORY_DB)
    all_dates = store.dates()
    h1 = [d for d in all_dates if d >= "2026-01-02" and d <= "2026-06-30"]
    h2 = [d for d in all_dates if d >= "2026-07-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    analyze_period("2026 상반기", h1, store, symbols, base_cfg)
    analyze_period("2026 하반기", h2, store, symbols, base_cfg)


if __name__ == "__main__":
    main()

