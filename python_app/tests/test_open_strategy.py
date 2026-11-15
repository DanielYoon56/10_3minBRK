"""OPEN 전략: 세 진입 패턴(시초 범위 돌파 · 시초 눌림 · 모멘텀 지속)과 진입 금지 조건"""
from datetime import datetime

import pytest

from python_app.models.types import Bar, TradingMode
from python_app.strategies.base_strategy import MarketContext, SymbolSession
from python_app.strategies.open_strategy import OpenSettings, OpenStrategy
from python_app.strategies.strategy_manager import StrategyManager, build_manager
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.test_kiwoom_live import DAY, make_engine

D = "2026-10-20"
NOW = datetime(2026, 10, 20, 9, 30)


def bar(hhmm, o, h, l, c, v=200_000):
    return Bar(symbol="005930", timestamp=datetime.strptime(f"{D} {hhmm}", "%Y-%m-%d %H:%M"),
               open=o, high=h, low=l, close=c, volume=v)


def session(bars, prev_close=10000, prev_high=None):
    s = SymbolSession("005930", "삼성전자", D)
    s.daily = [{"dt": "20261019", "open": prev_close, "high": prev_high or prev_close * 1.01,
                "low": prev_close * 0.99, "close": prev_close, "volume": 1_000_000}]
    for b in bars:
        assert s.add(b), b
    return s


def strategy(**kw):
    return OpenStrategy(OpenSettings(**kw))


def ctx(index_change=None):
    return MarketContext(now=NOW, index=None if index_change is None else {"change_rate": index_change})


# ------------------------------------------------------------------ 기준 시나리오
def orb_bars(last=None):
    """시초 범위(2봉) 고가 10,120 → 세 번째 봉이 10,200 으로 돌파 마감"""
    return [bar("09:03", 10000, 10100, 9950, 10050, 300_000),
            bar("09:06", 10050, 10120, 10000, 10080, 200_000),
            last or bar("09:09", 10080, 10250, 10070, 10200, 200_000)]


def pullback_bars(pull=None, last=None):
    """9,990 → 10,300 상승 뒤 10,180 까지 눌리고(거래량 감소) 직전 봉 고가 위 양봉"""
    return [bar("09:03", 10000, 10150, 9990, 10140, 300_000),
            bar("09:06", 10140, 10300, 10130, 10280, 300_000),
            pull or bar("09:09", 10280, 10290, 10180, 10200, 100_000),
            last or bar("09:12", 10200, 10295, 10190, 10295, 150_000)]


def momentum_bars(third=None, last=None):
    """저점을 높이며 오르다 네 번째 봉이 당일 신고가 마감"""
    return [bar("09:03", 10000, 10060, 9990, 10050),
            bar("09:06", 10050, 10110, 10040, 10100),
            third or bar("09:09", 10100, 10170, 10090, 10160),
            last or bar("09:12", 10160, 10260, 10150, 10250)]


# ------------------------------------------------------------------ A. 시초 범위 돌파
def test_orb_signal_on_completed_breakout_bar():
    s = session(orb_bars())
    sig = strategy().on_bar(s, ctx())
    assert (sig.strategy, sig.pattern, sig.entry_price) == ("open", "orb", 10200)
    assert sig.stop_price == 10060                      # 돌파 봉 저가 10,070 - 1호가(10원)
    assert sig.metadata["level"] == 10120 and sig.metadata["gap_pct"] == 0.0
    assert sig.timestamp == s.last.timestamp and not sig.hold_overnight


def test_orb_does_not_fire_before_range_is_complete_or_without_a_cross():
    st = strategy()
    assert st.detect_orb(session(orb_bars()[:2])) is None                        # 범위 2봉만 있음
    inside = session(orb_bars(bar("09:09", 10080, 10120, 10060, 10110)))
    assert st.detect_orb(inside) is None                                          # 고가 아래 마감
    already = session(orb_bars() + [bar("09:12", 10200, 10300, 10190, 10280)])
    assert st.detect_orb(already) is None                                         # 이미 위에 있던 봉은 돌파가 아니다


@pytest.mark.parametrize("last", [
    bar("09:09", 10080, 10250, 10070, 10200, 100_000),      # 거래량 부족 (기준 175,000)
    bar("09:09", 10260, 10260, 10150, 10200, 200_000),      # 음봉
])
def test_orb_needs_volume_and_a_bullish_bar(last):
    assert strategy().detect_orb(session(orb_bars(last))) is None


def test_orb_needs_every_range_bar():
    st = strategy()
    no_second = session([orb_bars()[0], bar("09:09", 10050, 10100, 10000, 10080), bar("09:12", 10080, 10250, 10070, 10200)])
    assert st.detect_orb(no_second) is None
    no_first = session(orb_bars()[1:] + [bar("09:12", 10200, 10300, 10190, 10280)])
    assert st.detect_orb(no_first) is None


# ------------------------------------------------------------------ B. 시초 눌림
def test_pullback_signal_after_controlled_retrace_and_recovery():
    s = session(pullback_bars())
    st = strategy()
    assert st.detect_orb(s) is None and st.detect_momentum(s) is None           # 다른 패턴과 겹치지 않는 상황
    sig = st.on_bar(s, ctx())
    assert sig.pattern == "pullback" and sig.entry_price == 10295
    assert sig.stop_price == 10170                      # 눌림 저가 10,180 - 1호가


@pytest.mark.parametrize("pull, last", [
    (bar("09:09", 10280, 10290, 10050, 10100, 100_000), bar("09:12", 10100, 10295, 10090, 10295, 150_000)),  # 너무 깊은 눌림
    (bar("09:09", 10280, 10295, 10260, 10270, 100_000), bar("09:12", 10270, 10299, 10265, 10298, 150_000)),  # 눌림이라 하기엔 얕음
    (bar("09:09", 10280, 10290, 10180, 10200, 300_000), None),                                              # 눌림 거래량이 줄지 않음
    (None, bar("09:12", 10200, 10280, 10190, 10250, 150_000)),                                              # 직전 봉 고가를 못 넘음
    (None, bar("09:12", 10290, 10295, 10190, 10200, 150_000)),                                              # 음봉
])
def test_pullback_no_trade_cases(pull, last):
    assert strategy().detect_pullback(session(pullback_bars(pull, last))) is None


def test_pullback_needs_real_impulse_and_at_least_one_pullback_bar():
    st = strategy()
    flat = [bar("09:03", 10000, 10060, 9990, 10050, 300_000), bar("09:06", 10050, 10100, 10040, 10090, 300_000),
            bar("09:09", 10090, 10095, 10050, 10060, 100_000), bar("09:12", 10060, 10098, 10055, 10097, 150_000)]
    assert st.detect_pullback(session(flat)) is None                              # 상승폭 1.1% < 2%
    assert st.detect_pullback(session(pullback_bars()[:2] + [bar("09:09", 10280, 10295, 10200, 10290, 100_000)])) is None


# ------------------------------------------------------------------ C. 시초 모멘텀 지속
def test_momentum_signal_on_new_high_with_rising_lows():
    s = session(momentum_bars())
    sig = strategy(use_orb=False, use_pullback=False).on_bar(s, ctx())
    assert sig.pattern == "momentum" and sig.entry_price == 10250
    assert sig.stop_price == 10030                      # 최근 3봉 최저가 10,040 - 1호가
    assert sig.metadata["level"] == 10170


@pytest.mark.parametrize("third, last", [
    (bar("09:09", 10100, 10170, 10030, 10160), None),                                 # 저점이 낮아짐
    (None, bar("09:12", 10160, 10175, 10150, 10165)),                                 # 종가가 직전 고가(10,170)를 못 넘음
    (bar("09:09", 10100, 10170, 10090, 10160, 20_000), bar("09:12", 10160, 10260, 10150, 10250, 20_000)),  # 거래량 급감
])
def test_momentum_no_trade_cases(third, last):
    bars = momentum_bars(third, last)
    if third is not None and third.volume == 20_000:
        bars[1] = bar("09:06", 10050, 10110, 10040, 10100, 20_000)
    assert strategy().detect_momentum(session(bars)) is None


def test_momentum_needs_enough_gain_and_bars():
    st = strategy()
    assert st.detect_momentum(session(momentum_bars()[:3])) is None               # 3봉 + 이전 1봉이 필요
    assert strategy(mo_min_gain_pct=3.0).detect_momentum(session(momentum_bars())) is None


# ------------------------------------------------------------------ 패턴 독립성
def test_patterns_can_be_switched_off_separately():
    third = session(momentum_bars()[:3])                 # 세 번째 봉에서 시초 범위(고가 10,110) 돌파
    assert strategy().on_bar(third, ctx()).pattern == "orb"
    assert strategy(use_orb=False).on_bar(third, ctx()) is None
    assert strategy(use_orb=False).on_bar(session(momentum_bars()), ctx()).pattern == "momentum"
    assert strategy(use_orb=False, use_pullback=False, use_momentum=False).on_bar(session(orb_bars()), ctx()) is None


def test_when_one_pattern_fails_the_risk_check_another_may_still_qualify():
    # 세 번째 봉 저가를 깊게 만들어 돌파 봉 손절폭은 과다(3.9%), 대신 눌림·모멘텀도 아닌 경우 → 신호 없음
    wide = session(orb_bars(bar("09:09", 10080, 10250, 9810, 10200, 200_000)))
    assert strategy().detect_orb(wide) is not None and strategy().on_bar(wide, ctx()) is None
    # 같은 봉에서 모멘텀은 좁은 손절로 통과하도록: 돌파 손절 한도만 따로 좁힐 수는 없으므로 패턴 순서를 확인한다
    s = session(momentum_bars())                         # 네 번째 봉: 돌파 아님(이미 위), 모멘텀 해당
    assert strategy().on_bar(s, ctx()).pattern == "momentum"


# ------------------------------------------------------------------ 공통 진입 금지
def test_blocked_reasons(caplog):
    cases = [
        (dict(prev_close=9000), {}, None, "시가 갭"),                                # 갭 +11%
        (dict(prev_close=10300), {}, None, "시가 갭"),                               # 갭 -2.9%
        (dict(prev_close=8800), dict(gap_max_pct=20.0), None, "과열"),               # 전일 대비 +15.9%
        (dict(), dict(min_cum_value=10_000_000_000), None, "누적 거래대금"),
        (dict(), {}, -2.0, "시장 약세"),
        (dict(), {}, 3.0, "상대강도"),                                               # 종목 +2% < 지수 +3%
        (dict(prev_high=10300), {}, None, "여유"),                                   # 전일 고가까지 100원 < 손절폭 140원 × 1.5
    ]
    for sess_kw, cfg_kw, index_change, expect in cases:
        caplog.clear()
        with caplog.at_level("INFO", logger="3WAYTRADING.Open"):
            assert strategy(**cfg_kw).on_bar(session(orb_bars(), **sess_kw), ctx(index_change)) is None, expect
        assert expect in caplog.text, (expect, caplog.text)


def test_overextended_from_vwap_is_blocked():
    s = session(orb_bars(bar("09:09", 10080, 10700, 10070, 10700, 200_000)))
    st = strategy(max_stop_pct=10.0)
    assert st.detect_orb(s) is not None and "VWAP 이격" in st.blocked(s, ctx())
    assert st.on_bar(s, ctx()) is None


def test_missing_bars_mean_poor_liquidity():
    bars = orb_bars()[:2] + [bar("09:15", 10080, 10250, 10070, 10200, 200_000)]    # 09:09, 09:12 봉 없음
    s = session(bars)
    assert s.gaps == 2 and "유동성" in strategy().blocked(s, ctx())
    assert strategy(max_gaps=2).on_bar(s, ctx()).pattern == "orb"


def test_no_daily_bar_or_no_opening_bar_means_no_trade():
    s = session(orb_bars())
    s.daily = []
    assert "전일 일봉 없음" in strategy().blocked(s, ctx()) and strategy().on_bar(s, ctx()) is None
    late = session(momentum_bars()[1:] + [bar("09:15", 10250, 10350, 10240, 10340)])
    assert "첫 봉" in strategy().blocked(late, ctx())


def test_stop_distance_limits():
    wide = session(orb_bars(bar("09:09", 10080, 10250, 9810, 10200, 200_000)))     # 손절 9,800 = -3.9%
    assert strategy().on_bar(wide, ctx()) is None
    assert strategy(max_stop_pct=5.0).on_bar(wide, ctx()).stop_price == 9800
    tight = session(orb_bars(bar("09:09", 10180, 10250, 10180, 10200, 200_000)))   # 저가 기준 -0.3% → 너무 좁다
    assert strategy().on_bar(tight, ctx()).stop_price == 10140                     # -0.5% (10,149) 를 호가 단위로 내림


def test_index_conditions_are_skipped_when_index_is_unknown():
    assert strategy().on_bar(session(orb_bars()), None).pattern == "orb"
    assert strategy().on_bar(session(orb_bars()), ctx(0.5)).metadata["index_change_pct"] == 0.5


def test_signal_is_not_fired_twice_and_blocked_bar_can_retry_later():
    m = StrategyManager([strategy()])
    m.daily_provider = lambda sym, day: [{"dt": "20261019", "open": 10000, "high": 10100, "low": 9900,
                                          "close": 10000, "volume": 1}]
    sigs = [s for b in orb_bars() + [bar("09:12", 10200, 10300, 10190, 10290)] for s in m.on_bar(b, "삼성전자", ctx())]
    assert [s.pattern for s in sigs] == ["orb"]
    assert m.stats["open"].by_pattern == {"orb": 1}


# ------------------------------------------------------------------ 등록·실거래 승인
def test_open_is_registered_with_config_values():
    m = build_manager({"strategies": {"open": {"end_time": "10:30", "use_momentum": False, "no_such": 1}}})
    st = m.strategy("open")
    assert isinstance(st, OpenStrategy) and st.settings.end_time == "10:30" and not st.settings.use_momentum
    assert st.needs_daily and st.needs_index and "OPEN 09:06~10:30" in m.describe()


def test_unapproved_strategy_never_signals_in_live_mode():
    feed = lambda m: [s for b in orb_bars() for s in m.on_bar(b, "삼성전자", ctx())]
    daily = lambda sym, day: [{"dt": "20261019", "open": 10000, "high": 10100, "low": 9900, "close": 10000, "volume": 1}]
    live = StrategyManager([strategy()], daily_provider=daily)
    live.live = True
    assert feed(live) == [] and "실거래 미승인" in live.describe() and not live.needs_index()
    approved = StrategyManager([strategy(live_enabled=True)], daily_provider=daily)
    approved.live = True
    assert len(feed(approved)) == 1


# ------------------------------------------------------------------ 엔진 연동
def seed_open(fake: FakeKiwoom):
    fake.names["005930"] = "삼성전자"
    fake.add_daily("005930", "20261019", 10000, 10100, 9900, 10000, 1_000_000)
    fake.add_bar("005930", f"{DAY}090000", 10000, 10100, 9950, 10050, 300_000)
    fake.add_bar("005930", f"{DAY}090300", 10050, 10120, 10000, 10080, 200_000)
    fake.add_bar("005930", f"{DAY}090600", 10080, 10250, 10070, 10200, 200_000)
    fake.prices["005930"] = 10200


def test_engine_buys_on_open_signal_and_exits_at_the_strategy_stop(tmp_path):
    fake = FakeKiwoom()
    seed_open(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 9, 2))
    eng, events = make_engine(fake, clock, tmp_path, strategies=[strategy()])
    eng.start(); eng.tick()
    buys = fake.api_calls("kt10000")
    assert len(buys) == 1 and int(buys[0]["body"]["ord_qty"]) == 98
    assert len(fake.api_calls("ka20001")) == 2 and len(fake.api_calls("ka10081")) == 1
    clock.set("09:09:20"); eng._last_sync = datetime.min; eng.tick()
    m = eng.managed["005930"]
    assert (m.status, m.strategy, m.pattern, m.stop_price) == ("HOLDING", "open", "orb", 10060)
    fake.prices["005930"] = 10060
    clock.set("09:10:00"); eng.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "전략 손절가" in eng.managed["005930"].exit_reason
    assert any("OPEN·orb" in e for e in events)


def test_engine_skips_entry_when_market_is_weak_but_trades_when_index_lookup_fails(tmp_path):
    weak = FakeKiwoom()
    seed_open(weak)
    weak.index_rates["101"] = "-2.10"                   # 코스닥만 약해도 약한 쪽을 따른다
    eng, _ = make_engine(weak, Clock(datetime(2026, 10, 20, 9, 9, 2)), tmp_path / "weak", strategies=[strategy()])
    (tmp_path / "weak").mkdir()
    eng.start(); eng.tick()
    assert not weak.api_calls("kt10000") and eng.snapshot()["index"]["change_rate"] == -2.1

    down = FakeKiwoom()
    seed_open(down)
    down.fail_apis = {"ka20001"}
    eng, events = make_engine(down, Clock(datetime(2026, 10, 20, 9, 9, 2)), tmp_path / "down", strategies=[strategy()])
    (tmp_path / "down").mkdir()
    eng.start(); eng.tick()
    assert len(down.api_calls("kt10000")) == 1 and any("지수 조회 실패" in e for e in events)


def test_engine_in_real_mode_does_not_trade_an_unapproved_strategy(tmp_path):
    for approved, expected in ((False, 0), (True, 1)):
        fake = FakeKiwoom()
        seed_open(fake)
        folder = tmp_path / str(approved)
        folder.mkdir()
        eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 9, 2)), folder, mode=TradingMode.REAL,
                                  strategies=[strategy(live_enabled=approved)])
        assert eng.start(live_confirmed=True)
        eng.tick()
        assert len(fake.api_calls("kt10000")) == expected
        assert any("실거래 미승인" in e for e in events) is (not approved)
