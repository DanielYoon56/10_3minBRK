"""전략 관리자: 전략 독립성, 중복 신호 차단, 오류 격리, 통계, 봉 저장소"""
from datetime import datetime

import pytest

from python_app.backtest.bar_store import BarStore
from python_app.models.types import Bar
from python_app.strategies.base_strategy import BaseStrategy, StrategySettings
from python_app.strategies.candidate_scanner import Candidate
from python_app.strategies.strategy_manager import StrategyManager, build_manager
from python_app.tests.stubs import OpenCrossStub

DAY = "2026-10-20"


def bar(hhmm, o, h, l, c, v, sym="005930", day=DAY):
    return Bar(symbol=sym, timestamp=datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"),
               open=o, high=h, low=l, close=c, volume=v)


def breakout_bars(sym="005930", day=DAY):
    return [bar("09:03", 10000, 10050, 9900, 9950, 100_000, sym, day),
            bar("09:06", 9950, 10150, 9950, 10100, 80_000, sym, day)]


class _Each(BaseStrategy):
    """받은 봉마다 신호 — 관리자의 중복 차단을 확인하기 위한 전략"""
    def __init__(self, name, **kw):
        self.name = name
        self.calls = 0
        self.kw = kw
        super().__init__(StrategySettings(start_time="09:03", end_time="15:00"))

    def on_bar(self, session, ctx=None):
        self.calls += 1
        return self.make_signal(session, "p", "매 봉", **self.kw)


class _Broken(BaseStrategy):
    name = "broken"

    def on_bar(self, session, ctx=None):
        raise RuntimeError("전략 내부 오류")


def feed(manager, bars):
    out = []
    for b in bars:
        out.extend(manager.on_bar(b, "삼성전자"))
    return out


def test_no_strategies_means_no_signals_but_bars_are_kept():
    m = StrategyManager()
    assert feed(m, breakout_bars()) == []
    assert len(m.sessions["005930"].bars) == 2
    assert "활성 전략 없음" in m.describe() and m.last_entry_time() is None


def test_strategies_judge_independently_on_the_same_bar():
    a, b = _Each("a"), OpenCrossStub()
    m = StrategyManager([a, b])
    sigs = feed(m, breakout_bars())
    assert [s.strategy for s in sigs] == ["a", "stub"]           # 등록 순서 = 우선순위
    assert len(m.sessions["005930"].bars) == 2                   # 세션에는 봉이 한 번만 쌓인다


def test_one_signal_per_strategy_per_symbol_per_day():
    a = _Each("a")
    m = StrategyManager([a])
    sigs = feed(m, breakout_bars() + [bar("09:09", 10100, 10200, 10050, 10150, 50_000)])
    assert len(sigs) == 1 and a.calls == 1
    assert m.fired_today("005930") == ["a"]
    other = feed(m, breakout_bars(sym="000660"))
    assert len(other) == 1                                        # 다른 종목은 별개
    next_day = feed(m, breakout_bars(day="2026-10-21"))
    assert len(next_day) == 1 and m.fired_today("005930") == ["a"]   # 날짜가 바뀌면 다시 가능


def test_disabled_or_out_of_window_strategy_is_not_called():
    off = _Each("off")
    off.update_settings(StrategySettings(enabled=False))
    late = _Each("late")
    late.update_settings(StrategySettings(start_time="10:00", end_time="11:00"))
    m = StrategyManager([off, late])
    assert feed(m, breakout_bars()) == [] and off.calls == late.calls == 0
    assert "late" in m.describe() and "off" not in m.describe()


def test_one_failing_strategy_does_not_block_the_others():
    m = StrategyManager([_Broken(), _Each("ok")])
    sigs = feed(m, breakout_bars())
    assert [s.strategy for s in sigs] == ["ok"]
    assert m.stats["broken"].errors == 2 and m.stats["ok"].errors == 0     # 봉마다 오류를 세고 계속 진행


@pytest.mark.parametrize("kw", [{"stop_price": 10100.0}, {"stop_price": 10200.0}, {"stop_price": 0.0},
                                {"target_price": 9950.0}])
def test_signal_with_impossible_prices_is_dropped(kw):
    # 진입가(첫 봉 종가) 9,950 기준: 손절가는 진입가보다 낮아야 하고 목표가는 높아야 한다
    m = StrategyManager([_Each("bad", **kw)])
    assert feed(m, breakout_bars()[:1]) == []
    assert m.stats["bad"].errors == 1 and m.fired_today("005930") == []


def test_incomplete_or_duplicate_bar_never_reaches_strategies():
    a = _Each("a")
    m = StrategyManager([a])
    b = breakout_bars()[0]
    b.is_completed = False
    assert m.on_bar(b, "삼성전자") == [] and a.calls == 0
    b.is_completed = True
    m.on_bar(b, "삼성전자")
    m.on_bar(b, "삼성전자")
    assert a.calls == 1


def test_daily_bars_are_loaded_once_only_for_strategies_that_need_them():
    calls = []

    def provider(symbol, yyyymmdd):
        calls.append((symbol, yyyymmdd))
        return [{"dt": "20261019", "open": 9900, "high": 10100, "low": 9800, "close": 10000, "volume": 1}]

    plain, needy = _Each("plain"), _Each("needy")
    m = StrategyManager([plain], daily_provider=provider)
    feed(m, breakout_bars())
    assert calls == [] and m.sessions["005930"].daily is None
    needy.needs_daily = True
    m2 = StrategyManager([needy], daily_provider=provider)
    feed(m2, breakout_bars())
    assert calls == [("005930", "20261020")] and m2.sessions["005930"].gap_pct == pytest.approx(0.0)


def test_daily_failure_is_recorded_not_raised():
    def provider(symbol, yyyymmdd):
        raise RuntimeError("조회 실패")

    needy = _Each("needy")
    needy.needs_daily = True
    m = StrategyManager([needy], daily_provider=provider)
    feed(m, breakout_bars())
    s = m.sessions["005930"]
    assert s.daily == [] and "조회 실패" in s.daily_error and s.gap_pct is None


def test_statistics_are_kept_per_strategy():
    m = StrategyManager([_Each("a"), OpenCrossStub()])
    feed(m, breakout_bars())
    m.record_trade("a.p", 12_000)
    m.record_trade("a", -5_000)
    m.record_trade("stub", 0)
    m.record_trade("gone", 99)                    # 등록되지 않은 전략은 무시
    snap = m.stats_snapshot()
    assert (snap["a"]["signals"], snap["a"]["trades"], snap["a"]["wins"], snap["a"]["losses"]) == (1, 2, 1, 1)
    assert snap["a"]["pnl"] == 7_000 and snap["a"]["by_pattern"] == {"p": 1}
    assert (snap["stub"]["trades"], snap["stub"]["wins"], snap["stub"]["losses"]) == (1, 0, 0)


def test_duplicate_strategy_names_are_refused():
    with pytest.raises(ValueError):
        StrategyManager([_Each("a"), _Each("a")])


def test_build_manager_reads_each_strategy_block():
    m = build_manager({"strategies": {"open": {"enabled": False}}})
    assert not m.strategy("open").enabled and m.strategy("open.orb") is m.strategy("open")
    assert m.strategy("nope") is None and "OPEN" not in m.describe()
    off = build_manager({"strategies": {name: {"enabled": False} for name in ("open", "close")}})
    assert "활성 전략 없음" in off.describe()


# ------------------------------------------------------------------ 봉 저장소
def test_bar_store_round_trip_and_idempotent_save(tmp_path):
    store = BarStore(str(tmp_path / "bars.db"))
    bars = breakout_bars() + breakout_bars(sym="000660") + breakout_bars(day="2026-10-21")
    unfinished = bar("09:09", 1, 2, 1, 2, 3)
    unfinished.is_completed = False
    assert store.save_bars(bars + [unfinished]) == 6
    store.save_bars(bars)                         # 같은 봉을 다시 저장해도 늘지 않는다
    loaded = store.load_bars("005930", DAY)
    assert [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in loaded] == \
           [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in breakout_bars()]
    assert store.symbols(DAY) == ["000660", "005930"] and store.dates() == [DAY, "2026-10-21"]


def test_bar_store_keeps_candidate_snapshots(tmp_path):
    import sqlite3
    store = BarStore(str(tmp_path / "bars.db"))
    c = Candidate("005930", "삼성전자", price=70000, change_rate=1.5, volume=10, trade_value=7e5,
                  sources=["value", "change"], score=1.25, rank=1)
    assert store.save_candidates(datetime(2026, 10, 20, 9, 6, 4), [c]) == 1
    rows = sqlite3.connect(store.path).execute("SELECT ts, symbol, rank, score, sources FROM candidates").fetchall()
    assert rows == [("2026-10-20 09:06", "005930", 1, 1.25, "value+change")]
