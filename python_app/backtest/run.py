"""
3WAYTRADING - 백테스트 실행 (history.db 의 과거 3분봉을 재생)

  python python_app/backtest/run.py                          전 기간, config.json 의 설정 그대로
  python python_app/backtest/run.py --split 2026-07-01       개발 구간 / 검증 구간(그 날짜부터)을 나눠서 보고
  python python_app/backtest/run.py --strategy open --from 2026-01-02 --to 2026-06-30

설정(전략·후보 탐색·공통 청산)은 실거래와 같은 config.json 을 읽는다. 데이터는 backtest/download.py 로 받는다.
검증 구간의 결과를 보고 값을 고치면 그 구간은 더 이상 검증 구간이 아니다.
"""

import argparse
import logging
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from python_app.app_config import load_config, strategy_config_from
from python_app.backtest.bar_store import BarStore
from python_app.backtest.download import HISTORY_DB
from python_app.backtest.metrics import report
from python_app.backtest.replay import INDEX_SYMBOLS, BacktestConfig, Replay
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.strategy_manager import STRATEGY_CLASSES


def main():
    p = argparse.ArgumentParser(description="3WAYTRADING 백테스트")
    p.add_argument("--from", dest="start", default="")
    p.add_argument("--to", dest="end", default="")
    p.add_argument("--split", default="", help="이 날짜(YYYY-MM-DD)부터를 검증 구간으로 따로 보고")
    p.add_argument("--strategy", nargs="*", default=[], help="재생할 전략 (기본: 등록된 전략 전부, 사용 여부와 무관)")
    p.add_argument("--no-scanner", action="store_true", help="후보 순위 없이 받아 둔 모든 종목을 감시")
    p.add_argument("--slippage-ticks", type=int, default=1)
    p.add_argument("--db", default=HISTORY_DB)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cfg = load_config()
    common = strategy_config_from(cfg)
    names = [n for n in STRATEGY_CLASSES if not args.strategy or n in args.strategy]
    blocks = cfg.get("strategies") or {}

    def factory():
        return [STRATEGY_CLASSES[n](STRATEGY_CLASSES[n].settings_class.from_dict({**blocks.get(n, {}), "enabled": True}))
                for n in names]

    store = BarStore(args.db)
    dates = [d for d in store.dates() if (not args.start or d >= args.start) and (not args.end or d <= args.end)]
    if len(dates) < 2:
        raise SystemExit("재생할 날짜가 없습니다 — backtest/download.py 로 데이터를 먼저 받으세요")
    symbols = {s: n for s, (n, _) in store.load_symbols().items()}
    replay = Replay(factory, common, ScannerConfig.from_dict(cfg.get("scanner")),
                    BacktestConfig(slippage_ticks=args.slippage_ticks, use_scanner=not args.no_scanner), symbols,
                    risk=RiskConfig.from_dict(cfg.get("risk")))

    print(f"기간 {dates[0]} ~ {dates[-1]} ({len(dates)}거래일) · 전략 {', '.join(names)} · "
          f"1회 {common.order_amount_per_trade:,}원 · 1회 허용 손실 {replay.guard.loss_per_trade:,.0f}원 · "
          f"동시 보유 {common.max_concurrent_positions} · "
          f"슬리피지 {args.slippage_ticks}호가/편도 · 후보 순위 {'사용' if not args.no_scanner else '미사용'}", flush=True)
    results = replay.run(store, dates, progress=lambda i, n: i % 25 == 0 and print(f"  {i}/{n}일", flush=True))

    stock_days = sum(1 for d in dates for _ in [0])
    trades = [t for r in results for t in r.trades]
    skipped = Counter()
    for r in results:
        skipped.update(r.skipped)
    print(f"\n신호 {sum(r.signals for r in results):,}개 → 체결 {len(trades):,}건 · 거래 있는 날 {sum(bool(r.trades) for r in results)}/{stock_days}일")
    print("진입하지 못한 신호:", ", ".join(f"{k} {v}" for k, v in skipped.most_common()) or "없음")
    print(f"(첫날은 지수 전일 종가가 없어 지수 조건 미적용. 지수 종목: {', '.join(INDEX_SYMBOLS)})\n")
    if args.split:
        print(report([t for t in trades if t.entry_time.strftime("%Y-%m-%d") < args.split], f"개발 구간 (~ {args.split} 이전)"))
        print()
        print(report([t for t in trades if t.entry_time.strftime("%Y-%m-%d") >= args.split], f"검증 구간 ({args.split} ~)"))
    else:
        print(report(trades, "전체 구간"))


if __name__ == "__main__":
    main()
