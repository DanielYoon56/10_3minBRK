"""
3WAYTRADING - 백테스트용 과거 데이터 내려받기 (history.db)

  python python_app/backtest/download.py daily              전 종목 일봉 (종목 선정·전일 값용)
  python python_app/backtest/download.py minute --top 150   3분봉: 후보가 될 만했던 날이 많은 상위 N 종목 (종목당 약 1년)
  python python_app/backtest/download.py index              코스피·코스닥 지수 3분봉

조회 전용이다 (주문 없음). 중단해도 다시 실행하면 받은 종목은 건너뛴다.

[종목 선정의 한계 — 결과를 읽을 때 반드시 고려]
  - 과거 어느 날의 순위표는 다시 받을 수 없다. 대신 일봉으로 '그날 거래대금 상위 100 또는 상승률 상위 100
    (거래대금 하한 이상)'에 들었던 날 수를 종목별로 세어, 그 수가 많은 종목부터 3분봉을 받는다.
  - 현재 상장 종목만 대상이다 (상장폐지 종목 없음 → 생존 편향).
  - 받지 못한 종목이 후보였던 날은 재생에서 빠진다. 포함 비율(coverage)을 출력한다.
"""

import argparse
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from python_app.app_config import APP_DIR, kiwoom_keys, load_config
from python_app.backtest.bar_store import BarStore
from python_app.broker.kiwoom_rest import KiwoomRestBroker, _num, _price
from python_app.models.types import Bar, TradingMode
from python_app.strategies.candidate_scanner import is_excluded_name

HISTORY_DB = os.path.join(APP_DIR, "history.db")
INDEX_SYMBOLS = {"IDX001": "001", "IDX101": "101"}       # 코스피 · 코스닥
logger = logging.getLogger("3WAYTRADING.Download")


def make_broker() -> KiwoomRestBroker:
    cfg = load_config()
    mode = TradingMode.MOCK if all(kiwoom_keys(cfg, TradingMode.MOCK)) else TradingMode.REAL
    broker = KiwoomRestBroker(*kiwoom_keys(cfg, mode), mode=mode)
    if not broker.connect():
        raise SystemExit("키움 접속 실패 — 앱키를 확인하세요")
    return broker


def list_symbols(broker) -> List[tuple]:
    """ka10099 코스피(0)·코스닥(10) 상장 종목 → [(코드, 이름, 시장)] (ETF·스팩·우선주 등 제외)"""
    out = []
    for mrkt, label in (("0", "KOSPI"), ("10", "KOSDAQ")):
        d, _ = broker._post(broker.STKINFO_PATH, "ka10099", {"mrkt_tp": mrkt})
        for it in d.get("list") or []:
            code, name = str(it.get("code", "")).strip(), str(it.get("name", "")).strip()
            if len(code) == 6 and code.isdigit() and not is_excluded_name(name):
                out.append((code, name, label))
    return out


def regular_bars(symbol: str, rows: List[dict], scale: float = 1.0) -> List[Bar]:
    """ka10080 / ka20005 행(시작 시각 표기) → 마감 시각 표기 Bar. 정규장(09:03~15:21) + 종가 단일가(15:33)만"""
    seen, bars = set(), []
    for r in rows:
        t = str(r.get("cntr_tm", ""))[:14]
        try:
            end = datetime.strptime(t, "%Y%m%d%H%M%S") + timedelta(minutes=3)
        except ValueError:
            continue
        hhmm = end.strftime("%H:%M")
        if not ("09:03" <= hhmm <= "15:21" or hhmm == "15:33") or end in seen:
            continue
        seen.add(end)
        o, h, l, c = (_price(r.get(k)) / scale for k in ("open_pric", "high_pric", "low_pric", "cur_prc"))
        if min(o, h, l, c) <= 0:
            continue
        bars.append(Bar(symbol=symbol, timestamp=end, open=o, high=h, low=l, close=c,
                        volume=int(abs(_num(r.get("trde_qty"))))))
    return bars


def fetch_minutes(broker, api_id: str, body: dict, list_key: str, max_pages: int = 60) -> List[dict]:
    rows, cont = [], ""
    for _ in range(max_pages):
        d, h = broker._post(broker.CHART_PATH, api_id, body, cont_key=cont)
        chunk = d.get(list_key) or []
        rows.extend(chunk)
        cont = h.get("next-key", "") if h.get("cont-yn") == "Y" else ""
        if not chunk or not cont:
            break
    return rows


# ---------------------------------------------------------------------- 명령
def cmd_daily(store: BarStore, broker, limit: int = 0):
    symbols = list_symbols(broker)
    store.save_symbols(symbols)
    done = store.done_keys("daily:")
    todo = [s for s in symbols if f"daily:{s[0]}" not in done][:limit or None]
    today = datetime.now().strftime("%Y%m%d")
    print(f"일봉: 전체 {len(symbols)}종목, 남은 {len(todo)}종목", flush=True)
    started = time.monotonic()
    for i, (code, name, _) in enumerate(todo, 1):
        try:
            store.save_daily(code, broker.get_daily_bars(code, today))
            store.mark_done(f"daily:{code}")
        except Exception as e:
            print(f"  ! {code} {name}: {e}", flush=True)
        if i % 200 == 0:
            left = (time.monotonic() - started) / i * (len(todo) - i)
            print(f"  {i}/{len(todo)} (약 {left / 60:.0f}분 남음)", flush=True)
    print("일봉 완료", flush=True)


def candidate_day_counts(daily: Dict[str, List[dict]], days: int, min_value: float, top: int = 100) -> Counter:
    """종목별 '후보가 될 만했던 날' 수 — 그날 거래대금 상위 top 또는 상승률 상위 top (거래대금 ≥ min_value)"""
    by_day: Dict[str, List[tuple]] = {}
    for sym, rows in daily.items():
        for prev, cur in zip(rows, rows[1:]):
            value = cur["close"] * cur["volume"]
            if value >= min_value and prev["close"] > 0:
                by_day.setdefault(cur["dt"], []).append((sym, value, cur["close"] / prev["close"] - 1.0))
    counts: Counter = Counter()
    for dt in sorted(by_day)[-days:]:
        rows = by_day[dt]
        picked = {s for s, _, _ in sorted(rows, key=lambda r: -r[1])[:top]}
        picked |= {s for s, _, _ in sorted(rows, key=lambda r: -r[2])[:top]}
        counts.update(picked)
    return counts


def cmd_minute(store: BarStore, broker, top: int, days: int, min_value: float):
    counts = candidate_day_counts(store.load_all_daily(), days, min_value)
    if not counts:
        raise SystemExit("일봉이 없습니다 — 먼저 'daily' 를 실행하세요")
    chosen = [s for s, _ in counts.most_common(top)]
    coverage = sum(counts[s] for s in chosen) / sum(counts.values())
    names = store.load_symbols()
    done = store.done_keys("minute:")
    todo = [s for s in chosen if f"minute:{s}" not in done]
    print(f"3분봉: 후보일 기준 상위 {len(chosen)}종목 (전체 후보일의 {coverage * 100:.0f}% 포함), 남은 {len(todo)}종목",
          flush=True)
    started = time.monotonic()
    for i, sym in enumerate(todo, 1):
        try:
            rows = fetch_minutes(broker, "ka10080", {"stk_cd": sym, "tic_scope": "3", "upd_stkpc_tp": "1"},
                                 "stk_min_pole_chart_qry")
            n = store.save_bars(regular_bars(sym, rows))
            store.mark_done(f"minute:{sym}")
        except Exception as e:
            print(f"  ! {sym}: {e}", flush=True)
            continue
        left = (time.monotonic() - started) / i * (len(todo) - i)
        print(f"  {i}/{len(todo)} {sym} {names.get(sym, ('',))[0]} {n:,}봉 (약 {left / 60:.0f}분 남음)", flush=True)
    print("3분봉 완료", flush=True)


def cmd_index(store: BarStore, broker):
    for sym, code in INDEX_SYMBOLS.items():
        rows = fetch_minutes(broker, "ka20005", {"inds_cd": code, "tic_scope": "3"}, "inds_min_pole_qry")
        n = store.save_bars(regular_bars(sym, rows, scale=100.0))       # 지수는 100배 정수로 온다
        print(f"지수 {sym}: {n:,}봉", flush=True)


def main():
    p = argparse.ArgumentParser(description="3WAYTRADING 백테스트 데이터 내려받기")
    p.add_argument("command", choices=["daily", "minute", "index", "all"])
    p.add_argument("--top", type=int, default=150)
    p.add_argument("--days", type=int, default=250)
    p.add_argument("--min-value", type=float, default=5_000_000_000)
    p.add_argument("--limit", type=int, default=0, help="daily: 처음 N 종목만 (시험용)")
    p.add_argument("--db", default=HISTORY_DB)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    store, broker = BarStore(args.db), make_broker()
    if args.command in ("daily", "all"):
        cmd_daily(store, broker, args.limit)
    if args.command in ("index", "all"):
        cmd_index(store, broker)
    if args.command in ("minute", "all"):
        cmd_minute(store, broker, args.top, args.days, args.min_value)


if __name__ == "__main__":
    main()
