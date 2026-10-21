"""
Korea3MinBreakoutTrader - Strategy Test Suite
매수: 첫 3분봉 시가 상향 돌파 + 거래량 충족
매도: -2% 손절 / 상승분 15% 되돌림 트레일링 스탑
"""

from datetime import datetime
import pytest
from python_app.models.types import Bar, Position, SignalType, StrategyConfig
from python_app.strategy.breakout import First3MinuteCandleBreakout
from python_app.strategy.exit_rules import check_exit, trailing_stop_price

D = "2026-10-14"


def make_bar(date_str: str, time_str: str, o: float, h: float, l: float, c: float, v: int, is_comp: bool = True) -> Bar:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return Bar(symbol="005930", timestamp=dt, open=o, high=h, low=l, close=c, volume=v, is_completed=is_comp)


def first_bar():
    # 첫 3분봉: 시가 70,000 → 종가 69,900 (음봉), 거래량 100,000
    return make_bar(D, "09:03", 70000, 70500, 69600, 69900, 100_000)


def make_engine(**kw) -> First3MinuteCandleBreakout:
    return First3MinuteCandleBreakout(StrategyConfig(trade_start_time="09:03", trade_end_time="10:00", **kw))


def make_position(entry: float) -> Position:
    return Position(
        symbol="005930", name="삼성전자", qty=10, avg_price=entry, current_price=entry,
        stop_loss_price=0.0, take_profit_price=0.0, entry_time=datetime.now(),
    )


# ---------------------------------------------------------------- 매수 조건
def test_first_bar_sets_open_as_breakout_level_and_no_signal():
    engine = make_engine()
    assert engine.process_bar(first_bar(), "삼성전자") is None
    st = engine.get_or_create_state("005930")
    assert st.breakout_level == 70000          # 시가 기준
    assert st.first_bar_open == 70000
    assert st.first_bar_volume == 100_000


def test_open_breakout_with_volume_fires_signal_with_2pct_stop():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")

    bar1 = make_bar(D, "09:06", 69900, 70400, 69900, 70300, 120_000)  # 70,000 돌파, 거래량 1.2배
    sig = engine.process_bar(bar1, "삼성전자")

    assert sig is not None
    assert sig.signal_type == SignalType.BUY_BREAKOUT
    assert sig.price == 70300
    assert sig.breakout_level == 70000
    assert sig.volume_ratio == pytest.approx(1.2)
    assert sig.stop_loss_price == round(70300 * 0.98)   # 68,894


def test_open_breakout_without_volume_is_rejected():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    bar1 = make_bar(D, "09:06", 69900, 70400, 69900, 70300, 60_000)   # 60% → 미달
    assert engine.process_bar(bar1, "삼성전자") is None


def test_volume_exactly_70pct_is_not_enough():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    bar1 = make_bar(D, "09:06", 69900, 70400, 69900, 70300, 70_000)   # 정확히 70% → '초과' 아님
    assert engine.process_bar(bar1, "삼성전자") is None


def test_volume_above_70pct_fires_even_below_first_bar_volume():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    bar1 = make_bar(D, "09:06", 69900, 70400, 69900, 70300, 70_001)   # 70% 초과 (첫 봉보다 적어도 OK)
    sig = engine.process_bar(bar1, "삼성전자")
    assert sig is not None and sig.volume_ratio == pytest.approx(0.70001)


def test_volume_ratio_is_configurable():
    engine = make_engine(min_volume_ratio=1.0)
    engine.process_bar(first_bar(), "삼성전자")
    assert engine.process_bar(make_bar(D, "09:06", 69900, 70400, 69900, 70300, 90_000), "삼성전자") is None


def test_close_above_first_close_but_below_open_does_not_fire():
    """구버전(종가 기준)이면 신호였을 봉이 시가 기준에서는 무시되어야 함"""
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    bar1 = make_bar(D, "09:06", 69900, 70000, 69800, 69950, 200_000)  # 69,900 < 69,950 <= 70,000
    assert engine.process_bar(bar1, "삼성전자") is None

    legacy = make_engine(breakout_reference="close")
    legacy.process_bar(first_bar(), "삼성전자")
    assert legacy.process_bar(make_bar(D, "09:06", 69900, 70000, 69800, 69950, 200_000), "삼성전자") is not None


def test_bullish_first_bar_needs_pullback_then_reclaim():
    """첫 봉이 시가 위에서 끝나면 crossup 정의상 한 번 시가 아래로 내려왔다가 재돌파해야 함"""
    engine = make_engine()
    engine.process_bar(make_bar(D, "09:03", 70000, 70600, 69900, 70400, 100_000), "삼성전자")
    assert engine.process_bar(make_bar(D, "09:06", 70400, 70800, 70300, 70700, 150_000), "삼성전자") is None
    assert engine.process_bar(make_bar(D, "09:09", 70700, 70700, 69800, 69900, 80_000), "삼성전자") is None
    sig = engine.process_bar(make_bar(D, "09:12", 69900, 70300, 69900, 70200, 130_000), "삼성전자")
    assert sig is not None and sig.price == 70200


def test_time_window_restriction():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    assert engine.process_bar(make_bar(D, "10:06", 69900, 71500, 69900, 71200, 200_000), "삼성전자") is None


def test_duplicate_signal_prevention():
    engine = make_engine(allow_reentry_same_day=False)
    engine.process_bar(first_bar(), "삼성전자")
    assert engine.process_bar(make_bar(D, "09:06", 69900, 70400, 69900, 70300, 120_000), "삼성전자") is not None
    engine.process_bar(make_bar(D, "09:09", 70300, 70300, 69700, 69800, 50_000), "삼성전자")
    assert engine.process_bar(make_bar(D, "09:12", 69800, 70500, 69800, 70400, 150_000), "삼성전자") is None


def test_date_rollover_resets_baseline():
    engine = make_engine()
    engine.process_bar(first_bar(), "삼성전자")
    engine.process_bar(make_bar("2026-10-15", "09:03", 72000, 72800, 71900, 72500, 250_000), "삼성전자")
    st = engine.get_or_create_state("005930")
    assert st.current_date_str == "2026-10-15"
    assert st.breakout_level == 72000
    assert st.first_bar_volume == 250_000
    assert st.signal_fired_today is False


# ---------------------------------------------------------------- 매도 조건
def test_stop_loss_2pct():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    assert check_exit(pos, 9_810, cfg) is None              # -1.9%
    d = check_exit(pos, 9_800, cfg)                          # -2.0%
    assert d is not None and d.signal_type == SignalType.SELL_STOP_LOSS


def test_trailing_stop_15pct_of_gain():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    assert check_exit(pos, 11_000, cfg) is None              # 최고가 11,000, 상승분 1,000
    assert trailing_stop_price(10_000, pos.highest_price, cfg) == pytest.approx(10_850)
    assert check_exit(pos, 10_860, cfg) is None              # 14% 되돌림 → 유지
    d = check_exit(pos, 10_850, cfg)                          # 15% 되돌림 → 청산
    assert d is not None and d.signal_type == SignalType.SELL_TRAILING_STOP


def test_trailing_follows_new_highs():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    check_exit(pos, 11_000, cfg)
    check_exit(pos, 12_000, cfg)                              # 최고가 갱신 → 상승분 2,000
    assert check_exit(pos, 10_900, cfg) is not None           # 기준 11,700 이하
    pos2 = make_position(10_000)
    check_exit(pos2, 12_000, cfg)
    assert check_exit(pos2, 11_750, cfg) is None              # 11,700 위 → 유지


def test_no_trailing_before_any_gain_and_no_fixed_take_profit():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    assert check_exit(pos, 9_950, cfg) is None                # 상승 전 하락: 손절선 전까지 유지
    assert check_exit(pos, 13_000, cfg) is None              # +30%여도 고정 익절 없음 (트레일링만)


def test_trailing_activation_threshold_option():
    cfg = StrategyConfig(trailing_activation_rate=1.0)
    pos = make_position(10_000)
    check_exit(pos, 10_050, cfg)                              # +0.5% → 미발동
    assert check_exit(pos, 10_000, cfg) is None


# ---------------------------------------------------------------- 트레일링 발동 +1% (기본값)
def test_default_activation_is_1pct():
    assert StrategyConfig().trailing_activation_rate == 1.0


def test_no_trailing_below_1pct_gain():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    assert check_exit(pos, 10_080, cfg) is None          # 최고 +0.8%
    assert check_exit(pos, 10_000, cfg) is None          # 되돌림 100% 지만 미발동 → 유지
    assert check_exit(pos, 9_850, cfg) is None           # 손절선(9,800) 위 → 유지
    d = check_exit(pos, 9_800, cfg)
    assert d is not None and d.signal_type == SignalType.SELL_STOP_LOSS


def test_trailing_activates_exactly_at_1pct():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    check_exit(pos, 10_100, cfg)                         # 최고 +1.0% → 발동
    assert trailing_stop_price(10_000, pos.highest_price, cfg) == pytest.approx(10_085)
    assert check_exit(pos, 10_090, cfg) is None
    d = check_exit(pos, 10_085, cfg)                     # 상승분 100 × 15% = 15 되돌림
    assert d is not None and d.signal_type == SignalType.SELL_TRAILING_STOP


def test_activation_sticks_after_peak_even_if_price_dips_below_1pct():
    cfg = StrategyConfig()
    pos = make_position(10_000)
    check_exit(pos, 10_400, cfg)                         # 최고 +4% → 발동, 청산가 10,340
    d = check_exit(pos, 10_050, cfg)                     # +0.5%로 급락 → 트레일링으로 청산
    assert d is not None and d.signal_type == SignalType.SELL_TRAILING_STOP


def test_activation_zero_restores_immediate_trailing():
    cfg = StrategyConfig(trailing_activation_rate=0.0)
    pos = make_position(10_000)
    check_exit(pos, 10_080, cfg)
    assert check_exit(pos, 10_060, cfg) is not None      # 0%면 소폭 상승 후에도 즉시 작동
