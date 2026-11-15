"""
3WAYTRADING 정밀 최적화: 손익비 개선 실험
- 손절폭 상한(max_stop_pct): 1.2%, 1.5%, 1.8%, 2.0%
- 목표가(take_profit_rate): 0 (미사용), 2.5%, 3.0%, 3.5%
- 트레일링 되돌림(trailing_retrace_rate_1r): 20%, 30%, 40%
- 최소 누적대금(min_cum_value): 30억, 50억, 100억
- 후보 순위 필터: 상위 5위, 10위, 20위
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import load_config, strategy_config_from
from python_app.backtest.bar_store import BarStore
from python_app.backtest.download import HISTORY_DB
from python_app.backtest.metrics import summarize
from python_app.backtest.replay import BacktestConfig, Replay
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.open_strategy import OpenSettings, OpenStrategy


def run_test(name, store, dates, symbols, base_cfg, open_kw, common_kw=None, scanner_kw=None):
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
        return [OpenStrategy(OpenSettings.from_dict({**open_kw, "enabled": True}))]

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
    avg_w = stats['avg_win'] or 0.0
    avg_l = stats['avg_loss'] or 0.0
    mdd = stats['max_drawdown'] or 0.0
    print(f"[{name:35s}] 거래: {n:3d} | 승률: {wr:4.1f}% | 평균익: {avg_w:+6,.0f} | 평균손: {avg_l:+6,.0f} | PF: {pf:4.2f} | 손익: {pnl:+10,.0f} | MDD: {mdd:8,.0f}")
    return stats


def main():
    store = BarStore(HISTORY_DB)
    # 최근 30거래일
    dates = [d for d in store.dates() if d >= "2026-10-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    print(f"=== 손익비 구조 개선 그리드 실험 ({dates[0]} ~ {dates[-1]}, {len(dates)}일) ===")

    # 1. Baseline
    run_test("0. 기본값 (max_stop 3.0%)", store, dates, symbols, base_cfg, {})

    # 2. 손절폭 축소 실험 (1.5%, 1.8%, 2.0%)
    run_test("1. 손절폭 상한 1.5%", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5})
    run_test("2. 손절폭 상한 1.8%", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.8})
    run_test("3. 손절폭 상한 2.0%", store, dates, symbols, base_cfg,
             {"max_stop_pct": 2.0})

    # 3. 손절폭 1.5% + 거래대금 상향 (50억, 100억)
    run_test("4. 손절 1.5% + 대금 50억", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "min_cum_value": 5_000_000_000})
    run_test("5. 손절 1.5% + 대금 100억", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "min_cum_value": 10_000_000_000})

    # 4. 손절 1.5% + 목표가 익절 (2.5%, 3.0%)
    run_test("6. 손절 1.5% + TP 2.5%", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5}, common_kw={"take_profit_rate": 2.5})
    run_test("7. 손절 1.5% + TP 3.0%", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5}, common_kw={"take_profit_rate": 3.0})

    # 5. 패턴별 단독 테스트 (손절 1.5%)
    run_test("8. orb 단독 (손절 1.5%)", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "use_orb": True, "use_pullback": False, "use_momentum": False})
    run_test("9. pullback 단독 (손절 1.5%)", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "use_orb": False, "use_pullback": True, "use_momentum": False})
    run_test("10. momentum 단독 (손절 1.5%)", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "use_orb": False, "use_pullback": False, "use_momentum": True})

    # 6. 후보 스캐너 순위 상위 10위만 허용
    run_test("11. 손절 1.5% + 스캐너 Top10", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5}, scanner_kw={"max_candidates": 10})
    run_test("12. 손절 1.5% + 대금50억 + Top10", store, dates, symbols, base_cfg,
             {"max_stop_pct": 1.5, "min_cum_value": 5_000_000_000}, scanner_kw={"max_candidates": 10})


if __name__ == "__main__":
    main()

