"""
수익 공식 탐색 (양의 기대값 PF > 1.0 달성 실험)
1. 지수 필터 강화 (min_index_change_pct: -0.5%, 상대강도 min_rel_strength_pct: 1.0%)
2. 과열 필터 강화 (max_day_change_pct: 8.0%, 10.0%)
3. 갭 범위 압축 (gap_min_pct: -0.5%, gap_max_pct: 5.0%)
4. 진입 마감 시각 (09:30 단축)
5. 후보 순위 5위~10위 압축
6. 손절 1.2% ~ 1.5%
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

    default_open = {
        "enabled": True,
        "use_orb": False,
        "use_pullback": True,
        "use_momentum": False,
        "pb_min_impulse_pct": 3.0,
        "max_stop_pct": 1.5,
    }
    default_open.update(open_kw)

    def factory():
        return [OpenStrategy(OpenSettings.from_dict(default_open))]

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
    print(f"[{name:38s}] 거래: {n:3d} | 승률: {wr:4.1f}% | 평균익: {avg_w:+6,.0f} | 평균손: {avg_l:+6,.0f} | PF: {pf:4.2f} | 손익: {pnl:+10,.0f} | MDD: {mdd:8,.0f}")
    return stats, trades


def main():
    store = BarStore(HISTORY_DB)
    # 최근 30거래일
    dates = [d for d in store.dates() if d >= "2026-10-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    print(f"=== 수익 공식 탐색: 30거래일 ({dates[0]} ~ {dates[-1]}) ===")

    # Baseline: PB impulse 3.0%, stop 1.5%
    run_test("0. PB 기준선 (impulse 3%, stop 1.5%)", store, dates, symbols, base_cfg, {})

    # A. 갭 제한 (0% ~ 5%)
    run_test("A1. 갭 0% ~ 5%", store, dates, symbols, base_cfg,
             {"gap_min_pct": 0.0, "gap_max_pct": 5.0})
    run_test("A2. 갭 -1% ~ 4%", store, dates, symbols, base_cfg,
             {"gap_min_pct": -1.0, "gap_max_pct": 4.0})

    # B. 과열 상한 (max_day_change_pct)
    run_test("B1. 당일 등락률 상한 8%", store, dates, symbols, base_cfg,
             {"max_day_change_pct": 8.0})
    run_test("B2. 당일 등락률 상한 10%", store, dates, symbols, base_cfg,
             {"max_day_change_pct": 10.0})

    # C. 지수 필터 강화 (지수 -0.5% 이상, 상대강도 +2%p)
    run_test("C1. 지수 -0.5% 이상만 진입", store, dates, symbols, base_cfg,
             {"min_index_change_pct": -0.5})
    run_test("C2. 지수 대비 상대강도 +2%p", store, dates, symbols, base_cfg,
             {"min_rel_strength_pct": 2.0})

    # D. 진입 시간 단축 (09:06 ~ 09:30)
    run_test("D1. 진입 마감 09:30", store, dates, symbols, base_cfg,
             {"end_time": "09:30"})

    # E. 스캐너 후보 상위 5위 / 10위 집중
    run_test("E1. 스캐너 Top 5위", store, dates, symbols, base_cfg,
             {}, scanner_kw={"max_candidates": 5})
    run_test("E2. 스캐너 Top 10위", store, dates, symbols, base_cfg,
             {}, scanner_kw={"max_candidates": 10})

    # F. 복합 최적화 조합
    run_test("F1. 복합: 갭 0~5% + 등락 10% + 09:30", store, dates, symbols, base_cfg,
             {"gap_min_pct": 0.0, "gap_max_pct": 5.0, "max_day_change_pct": 10.0, "end_time": "09:30"})
    run_test("F2. 복합: F1 + 지수 -0.5% + Top 10", store, dates, symbols, base_cfg,
             {"gap_min_pct": 0.0, "gap_max_pct": 5.0, "max_day_change_pct": 10.0, "end_time": "09:30",
              "min_index_change_pct": -0.5}, scanner_kw={"max_candidates": 10})
    run_test("F3. 복합: F2 + 대금 50억", store, dates, symbols, base_cfg,
             {"gap_min_pct": 0.0, "gap_max_pct": 5.0, "max_day_change_pct": 10.0, "end_time": "09:30",
              "min_index_change_pct": -0.5, "min_cum_value": 5_000_000_000}, scanner_kw={"max_candidates": 10})


if __name__ == "__main__":
    main()

