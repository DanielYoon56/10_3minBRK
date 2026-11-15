"""
PULLBACK 전략 심층 튜닝
- pb_min_impulse_pct: 2.0%, 2.5%, 3.0%, 3.5%
- pb_vol_dry_ratio: 0.6, 0.7, 0.8
- min_cum_value: 30억, 50억, 100억
- 목표가 / 트레일링 최적화
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


def run_test(name, store, dates, symbols, base_cfg, open_kw, common_kw=None):
    cfg = dict(base_cfg)
    common = strategy_config_from(cfg)
    if common_kw:
        for k, v in common_kw.items():
            setattr(common, k, v)

    scanner_cfg = ScannerConfig.from_dict(cfg.get("scanner"))

    default_open = {
        "enabled": True,
        "use_orb": False,
        "use_pullback": True,
        "use_momentum": False,
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
    print(f"[{name:35s}] 거래: {n:3d} | 승률: {wr:4.1f}% | 평균익: {avg_w:+6,.0f} | 평균손: {avg_l:+6,.0f} | PF: {pf:4.2f} | 손익: {pnl:+10,.0f} | MDD: {mdd:8,.0f}")
    return stats, trades


def main():
    store = BarStore(HISTORY_DB)
    # 최근 30거래일
    dates = [d for d in store.dates() if d >= "2026-10-01" and d <= "2026-11-13"]
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    base_cfg = load_config()

    print(f"=== PULLBACK 전략 심층 튜닝 ({dates[0]} ~ {dates[-1]}, {len(dates)}일) ===")

    # 1. 상승 파동 최소 크기 (pb_min_impulse_pct)
    run_test("PB impulse >= 2.0%", store, dates, symbols, base_cfg, {"pb_min_impulse_pct": 2.0})
    run_test("PB impulse >= 2.5%", store, dates, symbols, base_cfg, {"pb_min_impulse_pct": 2.5})
    run_test("PB impulse >= 3.0%", store, dates, symbols, base_cfg, {"pb_min_impulse_pct": 3.0})
    run_test("PB impulse >= 3.5%", store, dates, symbols, base_cfg, {"pb_min_impulse_pct": 3.5})

    # 2. 거래대금 상향 (50억, 100억)
    run_test("PB imp>=2.5% + 대금 50억", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5, "min_cum_value": 5_000_000_000})
    run_test("PB imp>=2.5% + 대금 100억", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5, "min_cum_value": 10_000_000_000})

    # 3. 거래량 마름 비율 (0.6, 0.7)
    run_test("PB imp>=2.5% + vol_dry 0.6", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5, "pb_vol_dry_ratio": 0.6})
    run_test("PB imp>=2.5% + vol_dry 0.7", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5, "pb_vol_dry_ratio": 0.7})

    # 4. 트레일링 되돌림 비율 조정 (최고가 1R 이상 시 되돌림 20%, 25%, 30%)
    run_test("PB imp>=2.5% + Trail 1R 20%", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5}, common_kw={"trailing_retrace_rate_1r": 20.0})
    run_test("PB imp>=2.5% + Trail 1R 25%", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5}, common_kw={"trailing_retrace_rate_1r": 25.0})

    # 5. 목표가 익절 병행 (2.0%, 2.5%)
    run_test("PB imp>=2.5% + TP 2.0%", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5}, common_kw={"take_profit_rate": 2.0})
    run_test("PB imp>=2.5% + TP 2.5%", store, dates, symbols, base_cfg,
             {"pb_min_impulse_pct": 2.5}, common_kw={"take_profit_rate": 2.5})


if __name__ == "__main__":
    main()

