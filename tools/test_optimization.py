"""
3WAYTRADING 최적화 탐색 스크립트
다양한 파라미터 조합으로 백테스트를 실행하고 주요 성과 지표(승률, 손익비, 순손익, MDD)를 비교합니다.
"""
import sys
import os
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import load_config, strategy_config_from
from python_app.backtest.bar_store import BarStore
from python_app.backtest.download import HISTORY_DB
from python_app.backtest.metrics import summarize
from python_app.backtest.replay import BacktestConfig, Replay
from python_app.models.types import StrategyConfig
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.open_strategy import OpenSettings, OpenStrategy
from python_app.strategies.intraday_strategy import IntradaySettings, IntradayStrategy
from python_app.strategies.close_strategy import CloseSettings, CloseStrategy


def run_experiment(exp_name, store, dates, symbols, base_cfg, open_kw=None, intraday_kw=None, close_kw=None, common_kw=None, scanner_kw=None):
    cfg = dict(base_cfg)
    common = strategy_config_from(cfg)
    if common_kw:
        for k, v in common_kw.items():
            setattr(common, k, v)

    sc_dict = dict(cfg.get("scanner") or {})
    if scanner_kw:
        sc_dict.update(scanner_kw)
    scanner_cfg = ScannerConfig.from_dict(sc_dict)

    def factory():
        res = []
        if open_kw is not None:
            res.append(OpenStrategy(OpenSettings.from_dict(open_kw)))
        if intraday_kw is not None:
            res.append(IntradayStrategy(IntradaySettings.from_dict(intraday_kw)))
        if close_kw is not None:
            res.append(CloseStrategy(CloseSettings.from_dict(close_kw)))
        return res

    replay = Replay(factory, common, scanner_cfg,
                    BacktestConfig(slippage_ticks=1, use_scanner=True), symbols,
                    risk=RiskConfig.from_dict(cfg.get("risk")))

    results = replay.run(store, dates)
    trades = [t for r in results for t in r.trades]
    stats = summarize(trades)
    wr = stats['win_rate'] or 0.0
    pf = stats['profit_factor'] or 0.0
    pnl = stats['net_profit'] or 0.0
    n = stats['trades']
    mdd = stats['max_drawdown'] or 0.0
    print(f"[{exp_name:32s}] 거래: {n:3d} | 승률: {wr:5.1f}% | PF: {pf:5.2f} | 순손익: {pnl:+11,.0f} | MDD: {mdd:9,.0f}")
    return stats


def main():
    store = BarStore(HISTORY_DB)
    # 최근 30거래일 기준 (빠른 탐색)
    dates = [d for d in store.dates() if d >= "2026-10-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    print(f"=== 최적화 파라미터 탐색 (테스트 기간: {dates[0]} ~ {dates[-1]}, {len(dates)}일) ===")

    # 1. 기준점 (현재 기본값, open + intraday + close)
    run_experiment("0. Baseline (현재 기본값)", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True}, intraday_kw={"enabled": True}, close_kw={"enabled": True})

    # 2. OPEN 단독 (기본값)
    run_experiment("1. OPEN 단독 (기본값)", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True})

    # 3. OPEN 단독 + 트레일링 2.0%
    run_experiment("2. OPEN 단독 + 트레일링 2.0%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True}, common_kw={"trailing_activation_rate": 2.0})

    # 4. OPEN 단독 + 트레일링 2.5%
    run_experiment("3. OPEN 단독 + 트레일링 2.5%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True}, common_kw={"trailing_activation_rate": 2.5})

    # 5. OPEN 단독 + 트레일링 3.0%
    run_experiment("4. OPEN 단독 + 트레일링 3.0%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True}, common_kw={"trailing_activation_rate": 3.0})

    # 6. OPEN 단독 + 목표가 3.0% + 트레일링 2.0%
    run_experiment("5. OPEN + TP 3.0% + Trail 2.0%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True}, common_kw={"trailing_activation_rate": 2.0, "take_profit_rate": 3.0})

    # 7. OPEN 단독 + 거래대금 50억 + 트레일링 2.5%
    run_experiment("6. OPEN + 대금50억 + Trail 2.5%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True, "min_cum_value": 5_000_000_000}, common_kw={"trailing_activation_rate": 2.5})

    # 8. OPEN 단독 + 거래대금 100억 + Trail 2.5%
    run_experiment("7. OPEN + 대금100억 + Trail 2.5%", store, dates, symbols, base_cfg,
                   open_kw={"enabled": True, "min_cum_value": 10_000_000_000}, common_kw={"trailing_activation_rate": 2.5})

    # 9. INTRADAY 단독: breakout 제외 (pullback + momentum만)
    run_experiment("8. INTRADAY (breakout OFF)", store, dates, symbols, base_cfg,
                   intraday_kw={"enabled": True, "use_breakout": False})

    # 10. INTRADAY (breakout OFF) + Trail 2.0%
    run_experiment("9. INTRADAY (no bo) + Trail 2%", store, dates, symbols, base_cfg,
                   intraday_kw={"enabled": True, "use_breakout": False}, common_kw={"trailing_activation_rate": 2.0})

    # 11. CLOSE 완화 (allow_neutral_weekly)
    run_experiment("10. CLOSE (neutral weekly)", store, dates, symbols, base_cfg,
                   close_kw={"enabled": True, "allow_neutral_weekly": True})


if __name__ == "__main__":
    main()
