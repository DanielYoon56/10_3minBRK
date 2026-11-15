"""
수익 전략 정밀 검증 및 장기 데이터 테스트
- C1(지수 -0.5% 이상) + B1(등락률 상한 8% 또는 9%)
- 최근 30일뿐 아니라 장기 구간(2025-11-03 ~ 2026-11-13, 253거래일) 전진 분석(Walk-forward)
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


def run_test(name, store, dates, symbols, base_cfg, open_kw):
    cfg = dict(base_cfg)
    common = strategy_config_from(cfg)
    scanner_cfg = ScannerConfig.from_dict(cfg.get("scanner"))

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
    print(f"[{name:42s}] 기간: {len(dates):3d}일 | 거래: {n:4d} | 승률: {wr:4.1f}% | 평균익: {avg_w:+6,.0f} | 평균손: {avg_l:+6,.0f} | PF: {pf:4.2f} | 손익: {pnl:+11,.0f} | MDD: {mdd:9,.0f}")
    return stats, trades


def main():
    store = BarStore(HISTORY_DB)
    all_dates = store.dates()
    # 최근 30일
    recent_30 = [d for d in all_dates if d >= "2026-10-01" and d <= "2026-11-13"]
    # 2026년 상반기 (개발 구간: 2026-01 ~ 2026-06)
    first_half = [d for d in all_dates if d >= "2026-01-02" and d <= "2026-06-30"]
    # 전체 1년 (2025-11-03 ~ 2026-11-13)
    full_dates = all_dates

    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    print(f"=== 1. 최근 30거래일 조합 실험 ===")
    run_test("PB 지수-0.5% + 등락률 8%", store, recent_30, symbols, base_cfg,
             {"min_index_change_pct": -0.5, "max_day_change_pct": 8.0})
    run_test("PB 지수-0.5% + 등락률 9%", store, recent_30, symbols, base_cfg,
             {"min_index_change_pct": -0.5, "max_day_change_pct": 9.0})
    run_test("PB 지수-0.3% + 등락률 8%", store, recent_30, symbols, base_cfg,
             {"min_index_change_pct": -0.3, "max_day_change_pct": 8.0})
    run_test("PB 지수-0.5% + 등락률 8% + 갭 0~5%", store, recent_30, symbols, base_cfg,
             {"min_index_change_pct": -0.5, "max_day_change_pct": 8.0, "gap_min_pct": 0.0, "gap_max_pct": 5.0})

    print(f"\n=== 2. 검증 구간 (2026년 상반기: {len(first_half)}거래일) ===")
    run_test("상반기: PB 지수-0.5% + 등락률 8%", store, first_half, symbols, base_cfg,
             {"min_index_change_pct": -0.5, "max_day_change_pct": 8.0})

    print(f"\n=== 3. 1년 전체 구간 ({len(full_dates)}거래일) ===")
    run_test("전체 1년: PB 지수-0.5% + 등락률 8%", store, full_dates, symbols, base_cfg,
             {"min_index_change_pct": -0.5, "max_day_change_pct": 8.0})


if __name__ == "__main__":
    main()

