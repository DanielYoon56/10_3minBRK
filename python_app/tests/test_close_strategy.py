"""CLOSE 전략: 주봉 → 일봉 → 3분봉 순서, 각 분류, 오버나이트 위험, 익일 청산, 엔진·재생 연동"""
from datetime import datetime, timedelta

import pytest

from python_app.backtest.replay import BacktestConfig, Replay
from python_app.models.types import Bar, Position, SignalType, StrategyConfig
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.base_strategy import MarketContext, SymbolSession
from python_app.strategies.candidate_scanner import ScannerConfig
from python_app.strategies.close_strategy import (BEARISH, BREAKOUT_SETUP, BULLISH, CONSOLIDATION, DOWNTREND, NEUTRAL,
                                                  OVEREXTENDED, PULLBACK, UNKNOWN, UPTREND, CloseSettings,
                                                  CloseStrategy)
from python_app.strategies.indicators import weekly_bars
from python_app.strategies.strategy_manager import STRATEGY_CLASSES, build_manager
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.test_kiwoom_live import make_engine

TODAY = "20261020"          # 화요일
D = "2026-10-20"
NOW = datetime(2026, 10, 20, 14, 48)


def weekdays_before(today: str, n: int):
    day, out = datetime.strptime(today, "%Y%m%d"), []
    while len(out) < n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
    return out[::-1]


def daily_from(closes, volume=1_000_000):
    """종가 목록 → 어제까지의 일봉 (고가 = 종가+20, 저가 = 종가-20)"""
    return [{"dt": dt, "open": c, "high": c + 20, "low": c - 20, "close": c, "volume": volume}
            for dt, c in zip(weekdays_before(TODAY, len(closes)), closes)]


UP = daily_from([5000 + 30 * i for i in range(140)])                    # 꾸준한 상승, 마지막 종가 9,170
DOWN = daily_from([9170 - 30 * i for i in range(140)])
FLAT = daily_from([8000 + (10 if i % 2 else -10) for i in range(140)])
DIP = daily_from([5000 + 30 * i for i in range(135)] + [8900, 8780, 8700, 8750, 8820])   # 상승 뒤 20일선까지 눌림


def strategy(**kw):
    return CloseStrategy(CloseSettings(**kw))


def ctx(index_change=None):
    return MarketContext(now=NOW, index=None if index_change is None else {"change_rate": index_change})


def session(daily=UP, base=9200, step=1.0, n=116, tail=None, volume=100_000):
    """09:03 부터 n 봉. 기본은 봉마다 step 씩 오르는 양봉. tail = 마지막 봉들을 바꿀 (시가, 고가, 저가, 종가, 거래량) 목록"""
    s = SymbolSession("005930", "삼성전자", D)
    s.daily = daily
    t = datetime(2026, 10, 20, 9, 3)
    rows = []
    for i in range(n):
        o = base + i * step
        c = o + step
        rows.append((o, max(o, c) + 2, min(o, c) - 2, c, volume))
    if tail:
        rows[-len(tail):] = tail
    for i, (o, h, l, c, v) in enumerate(rows):
        assert s.add(Bar("005930", t + timedelta(minutes=3 * i), o, h, l, c, v))
    return s


# ------------------------------------------------------------------ 1. 주봉
def test_weekly_bars_use_completed_weeks_only():
    weeks = weekly_bars(UP, TODAY)
    assert weeks[-1]["dt"] == "20261016"                      # 이번 주 월요일(10/19) 봉은 미완성 주라 빠진다
    assert weeks[-1]["first_dt"] == "20261012" and weeks[-1]["close"] == UP[-2]["close"]
    assert weeks[-1]["high"] == max(d["high"] for d in UP[-6:-1]) and weeks[-1]["volume"] == 5_000_000
    assert len(weekly_bars(UP, "20261019")) == len(weeks)     # 월요일 장중에도 지난주까지만


@pytest.mark.parametrize("daily, expected", [(UP, BULLISH), (DOWN, BEARISH), (FLAT, NEUTRAL), (UP[-60:], UNKNOWN)])
def test_weekly_classification(daily, expected):
    view = strategy().analyze_weekly(daily, TODAY)
    assert view.label == expected
    if expected == BULLISH:
        assert view.detail["higher_highs_lows"] and view.detail["mid_slope"] > 0


# ------------------------------------------------------------------ 2. 일봉
@pytest.mark.parametrize("daily, price, expected", [
    (UP, 9262, BREAKOUT_SETUP),          # 20일 고가 9,190 근처
    (UP, 8950, UPTREND),                 # 20일선(8,885) 위, 고가에서는 떨어짐
    (DIP, 8850, PULLBACK),               # 20일선까지 눌린 뒤 5일선·전일 종가 위로 회복
    (UP, 10400, OVEREXTENDED),           # 당일 +13.4%
    (DOWN, 4950, DOWNTREND),
    (FLAT, 8000, CONSOLIDATION),
    (UP[-40:], 9262, UNKNOWN),           # 일봉 40개 → 60일선을 계산할 수 없다
])
def test_daily_classification(daily, price, expected):
    assert strategy().analyze_daily(daily, price).label == expected


def test_daily_overextension_by_distance_and_by_recent_run():
    spike = daily_from([5000 + 30 * i for i in range(135)] + [9300, 9700, 10200, 10800, 11400])
    assert strategy().analyze_daily(spike, 11500).label == OVEREXTENDED           # 20일선 이격·5일 상승률 과다


# ------------------------------------------------------------------ 3. 3분봉
def test_3min_valid_on_a_steady_close_near_the_high():
    view = strategy().analyze_3min(session())
    assert view.label == "VALID" and view.detail["range_pos"] > 0.95 and view.detail["up_down_volume"] is None


def _gap_up_bearish_tail(start=9305.0):
    """저점은 높아지지만 봉마다 음봉 (시가를 높게 열고 조금 밀려 마감)"""
    rows, prev = [], start
    for _ in range(10):
        o = prev + 3
        rows.append((o, o + 1, o - 2, o - 1, 100_000))
        prev = o - 1
    return rows


@pytest.mark.parametrize("sess_kw, cfg_kw, expect", [
    (dict(step=-1.0), {}, "VWAP 아래"),
    ({}, dict(m_min_range_pos=1.01), "상단부 아님"),
    ({}, dict(m_max_from_high_pct=-1.0), "당일 고가에서"),
    ({}, dict(m_max_late_drop_pct=-1.0), "막판 하락"),
    (dict(tail=[(9315, 9318, 9250, 9316, 100_000)]), {}, "막판 저점 하락"),
    (dict(tail=_gap_up_bearish_tail()), {}, "막판 매도 우위"),
    (dict(tail=[(9306 + i, 9309 + i, 9304 + i, 9307 + i, 5_000) for i in range(10)]), {}, "막판 거래량 급감"),
    (dict(n=15), {}, "필요 20개"),
])
def test_3min_invalid_cases(sess_kw, cfg_kw, expect):
    view = strategy(**cfg_kw).analyze_3min(session(**sess_kw))
    assert view.label == "INVALID" and expect in view.reason


# ------------------------------------------------------------------ 최종 판정: 주봉 → 일봉 → 3분봉 → 위험
def test_signal_when_all_three_timeframes_agree():
    sig = strategy().on_bar(session(), ctx())
    assert (sig.strategy, sig.pattern, sig.entry_price) == ("close", "swing", 9316)
    assert sig.hold_overnight and sig.position_size_ratio == 0.5
    assert sig.stop_price == 9220                         # 최근 20봉 저가 기준은 -0.3% → 최소 -1% 로 넓혀 호가 내림
    assert sig.metadata["weekly"] == BULLISH and sig.metadata["daily"] == BREAKOUT_SETUP
    assert "익일 10:30" in sig.reason


@pytest.mark.parametrize("daily, cfg_kw, index_change, expect", [
    (DOWN, {}, None, "주봉 BEARISH"),
    (FLAT, {}, None, "주봉 NEUTRAL"),
    (UP[-60:], {}, None, "주봉 UNKNOWN"),
    (FLAT, dict(allow_neutral_weekly=True), None, "일봉 CONSOLIDATION"),     # 주봉을 통과해야 일봉을 본다
    (UP, dict(d_max_day_change_pct=1.0), None, "일봉 OVEREXTENDED"),
    (UP, dict(m_min_range_pos=1.01), None, "3분봉"),
    (UP, dict(min_cum_value=10 ** 12), None, "누적 거래대금"),
    (UP, {}, -1.5, "시장 약세"),
    (UP, dict(min_stop_pct=5.0, max_stop_pct=4.0, stop_bars=116), None, "손절폭"),
    (UP, dict(min_rr=50.0), None, "여유"),
])
def test_first_failing_stage_blocks_the_entry(daily, cfg_kw, index_change, expect, caplog):
    with caplog.at_level("INFO", logger="3WAYTRADING.Close"):
        base = 9200 if daily is UP else daily[-1]["close"] + 30
        assert strategy(**cfg_kw).on_bar(session(daily=daily, base=base), ctx(index_change)) is None
    assert expect in caplog.text


def test_later_stages_are_not_evaluated_after_an_earlier_failure(monkeypatch):
    st = strategy()
    called = []
    monkeypatch.setattr(st, "analyze_daily", lambda *a: called.append("daily"))
    monkeypatch.setattr(st, "analyze_3min", lambda *a: called.append("3min"))
    assert st.on_bar(session(daily=DOWN, base=5000), ctx()) is None and called == []


def test_consolidation_can_be_allowed_and_missing_daily_means_no_trade():
    s = session(daily=FLAT, base=8010)
    assert strategy(allow_neutral_weekly=True, allow_consolidation=True).on_bar(s, ctx()).metadata["daily"] == CONSOLIDATION
    empty = session()
    empty.daily = []
    assert strategy().on_bar(empty, ctx()) is None


# ------------------------------------------------------------------ 청산: 공통 규칙 + 익일 지정 시각
class _Trade:
    def __init__(self, carried):
        self.carried_over = carried


def _pos(entry=9316.0, stop=9220.0):
    return Position("005930", "삼성전자", 50, entry, entry, stop, 0.0, datetime(2026, 10, 20, 14, 48))


def test_next_day_time_exit_only_for_carried_positions():
    st, common = strategy(), StrategyConfig()
    at = lambda h, m: datetime(2026, 10, 21, h, m)
    assert st.check_exit(_pos(), 9350, common, at(10, 29), trade=_Trade(True)) is None
    d = st.check_exit(_pos(), 9350, common, at(10, 30), trade=_Trade(True))
    assert d.signal_type == SignalType.SELL_TIME_CUT and "익일 지정 시각 청산" in d.reason
    assert st.check_exit(_pos(), 9350, common, datetime(2026, 10, 20, 15, 0), trade=_Trade(False)) is None   # 매수 당일
    stop = st.check_exit(_pos(), 9200, common, at(9, 1), trade=_Trade(True))
    assert stop.signal_type == SignalType.SELL_STOP_LOSS                                                      # 손절이 먼저


def test_close_is_registered_last_with_overnight_defaults():
    assert list(STRATEGY_CLASSES) == ["open", "close"]
    st = build_manager({"strategies": {"close": {"exit_time": "09:30", "size_ratio": 0.3}}}).strategy("close")
    assert isinstance(st, CloseStrategy) and st.exit_time.strftime("%H:%M") == "09:30" and st.settings.size_ratio == 0.3


# ------------------------------------------------------------------ 엔진 연동: 매수 → 당일 보유 → 익일 시각 청산
def seed_close_day(fake: FakeKiwoom):
    fake.names["005930"] = "삼성전자"
    for d in UP:
        fake.add_daily("005930", d["dt"], d["open"], d["high"], d["low"], d["close"], d["volume"])
    t = datetime(2026, 10, 20, 9, 0)
    for i in range(116):                                       # 09:00 ~ 14:45 시작 봉 → 마지막 봉 마감 14:48
        o = 9200 + i
        fake.add_bar("005930", (t + timedelta(minutes=3 * i)).strftime("%Y%m%d%H%M%S"), o, o + 3, o - 2, o + 1, 100_000)
    fake.prices["005930"] = 9316


def test_engine_buys_before_close_holds_overnight_and_exits_next_morning(tmp_path):
    fake = FakeKiwoom()
    seed_close_day(fake)
    clock = Clock(datetime(2026, 10, 20, 14, 48, 5))
    eng, events = make_engine(fake, clock, tmp_path, strategies=[strategy()])
    eng.start(); eng.tick()
    buys = fake.api_calls("kt10000")
    assert len(buys) == 1 and int(buys[0]["body"]["ord_qty"]) == 53       # 100만원 × 0.5 ÷ 9,317.4원
    clock.set("14:48:30"); eng._last_sync = datetime.min; eng.tick()
    m = eng.managed["005930"]
    assert (m.status, m.strategy, m.hold_overnight, m.stop_price) == ("HOLDING", "close", True, 9220)
    clock.set("15:15:00"); eng.tick()
    clock.set("15:19:00"); eng.tick()
    assert not fake.api_calls("kt10001")                                   # 장 마감 일괄 청산 대상이 아니다
    eng.stop()

    clock.now = datetime(2026, 10, 21, 8, 55, 0)
    eng2, _ = make_engine(fake, clock, tmp_path, strategies=[strategy()])
    eng2.start()
    fake.prices["005930"] = 9400
    clock.set("09:00:01"); eng2.tick()
    clock.set("10:29:58"); eng2.tick()
    assert not fake.api_calls("kt10001")
    clock.set("10:30:00"); eng2.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "익일 지정 시각 청산" in eng2.managed["005930"].exit_reason


def test_engine_sells_overnight_position_at_the_open_when_it_gaps_below_the_stop(tmp_path):
    fake = FakeKiwoom()
    seed_close_day(fake)
    clock = Clock(datetime(2026, 10, 20, 14, 48, 5))
    eng, _ = make_engine(fake, clock, tmp_path, strategies=[strategy()])
    eng.start(); eng.tick()
    clock.set("14:48:30"); eng._last_sync = datetime.min; eng.tick()
    eng.stop()
    clock.now = datetime(2026, 10, 21, 8, 55, 0)
    eng2, _ = make_engine(fake, clock, tmp_path, strategies=[strategy()])
    eng2.start()
    fake.prices["005930"] = 8900                                           # 손절가 9,220 아래로 갭 하락 → 그 가격에 매도
    clock.set("09:00:01"); eng2.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "손절" in eng2.managed["005930"].exit_reason
    clock.set("09:00:30"); eng2._last_sync = datetime.min; eng2.tick()
    assert eng2.managed["005930"].realized_pnl < (9220 - 9316) * 53       # 손절가보다 훨씬 큰 손실 (갭은 손절로 막지 못한다)


# ------------------------------------------------------------------ 재생: 오버나이트 이월
class _CloseAt(CloseStrategy):
    """정해진 시각에 오버나이트 신호 — 재생 엔진의 이월·익일 청산 확인용"""
    def on_bar(self, s, ctx=None):
        if s.last.time_str != "14:48":
            return None
        return self.make_signal(s, "swing", "테스트", stop_price=9000.0, hold_overnight=True, position_size_ratio=0.5)


def _replay():
    return Replay(lambda: [_CloseAt()], StrategyConfig(), ScannerConfig(),
                  BacktestConfig(slippage_ticks=0, use_scanner=False), risk=RiskConfig(max_loss_per_trade=10 ** 9))


def _bars(day, rows):
    return [Bar("A", datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"), o, h, l, c, 100_000) for hhmm, o, h, l, c in rows]


DAY1 = [("14:45", 10000, 10010, 9990, 10000), ("14:48", 10000, 10010, 9990, 10000), ("14:51", 10010, 10020, 10000, 10010),
        ("15:15", 10010, 10030, 10000, 10020), ("15:18", 10020, 10030, 10010, 10020)]
PREV = {"A": [{"dt": "20261019", "open": 10000, "high": 10000, "low": 10000, "close": 10000, "volume": 1}]}


def test_replay_carries_overnight_position_and_exits_at_the_next_day_time():
    r = _replay()
    day1 = r.run_day(D, {"A": _bars(D, DAY1)}, PREV)
    assert day1.trades == [] and list(r.carried) == ["A"]                 # 당일 일괄 청산에서 제외, 다음 날로
    day2 = r.run_day("2026-10-21", {"A": _bars("2026-10-21", [
        ("09:03", 10050, 10080, 10040, 10070), ("10:27", 10100, 10120, 10090, 10110),
        ("10:30", 10110, 10130, 10100, 10120), ("10:33", 10120, 10140, 10110, 10130)])}, PREV)
    t = day2.trades[0]
    assert t.carried_over and t.qty == 49 and t.entry_price == 10010      # 신호 다음 봉(14:51) 시가, 비중 0.5
    assert "익일 지정 시각 청산" in t.exit_reason and (t.exit_time.strftime("%H:%M"), t.exit_price) == ("10:30", 10110)
    assert r.carried == {} and t.holding_minutes > 60 * 19


def test_replay_overnight_gap_below_stop_fills_at_the_open():
    r = _replay()
    r.run_day(D, {"A": _bars(D, DAY1)}, PREV)
    day2 = r.run_day("2026-10-21", {"A": _bars("2026-10-21", [("09:03", 8800, 8850, 8700, 8750)])}, PREV)
    t = day2.trades[0]
    assert t.exit_price == 8800 and t.exit_reason.startswith("손절") and t.pnl < (9000 - 10010) * 49


def test_replay_closes_a_carried_position_by_next_days_end_and_at_end_of_data():
    r = _replay()
    r.run_day(D, {"A": _bars(D, DAY1)}, PREV)
    quiet = r.run_day("2026-10-21", {"A": _bars("2026-10-21", [("09:03", 10050, 10060, 10040, 10050)])}, PREV)
    assert "장 마감 일괄 청산" in quiet.trades[0].exit_reason               # 익일 10:30 봉이 없어도 그날 끝에는 정리 (최대 1일)

    class Store:
        def load_all_daily(self):
            return PREV

        def load_day(self, d):
            return {"A": _bars(D, DAY1)}

    r2 = _replay()
    results = r2.run(Store(), [D])
    assert "재생 종료" in results[-1].trades[0].exit_reason and r2.carried == {}
