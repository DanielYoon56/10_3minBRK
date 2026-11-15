"""전략 공통 인터페이스: 종목별 3분봉 세션, 전략 뼈대"""
from datetime import datetime, time as dtime

import pytest

from python_app.models.types import Bar, Position, SignalType, StrategyConfig
from python_app.strategies.base_strategy import BUY, BaseStrategy, StrategySettings, SymbolSession

DAY = "2026-10-20"


def bar(hhmm, o=10000, h=10100, l=9900, c=10050, v=1000, day=DAY, done=True):
    return Bar(symbol="005930", timestamp=datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"),
               open=o, high=h, low=l, close=c, volume=v, is_completed=done)


def session():
    return SymbolSession("005930", "삼성전자", DAY)


# ------------------------------------------------------------------ 봉 무결성
def test_completed_bars_accumulate_in_order_with_time_based_index():
    s = session()
    assert s.add(bar("09:03")) and s.add(bar("09:06")) and s.add(bar("09:12"))
    assert [b.bar_index for b in s.bars] == [0, 1, 3]
    assert s.has_opening_bar and s.first.timestamp.time() == dtime(9, 3)


def test_incomplete_bar_is_never_used():
    s = session()
    assert not s.add(bar("09:03", done=False))
    assert not s.bars and s.rejected == 0         # 진행 중인 봉은 비정상이 아니다


def test_duplicate_and_out_of_order_bars_are_ignored():
    s = session()
    s.add(bar("09:03")); s.add(bar("09:06"))
    assert not s.add(bar("09:06", c=10090))      # 같은 봉을 다시 받아도 값이 바뀌지 않는다
    assert not s.add(bar("09:03"))
    assert len(s.bars) == 2 and s.last.close == 10050
    assert s.rejected == 1                        # 중복은 정상 상황이라 세지 않고, 역순만 센다


def test_other_day_and_bad_prices_are_rejected():
    s = session()
    assert not s.add(bar("09:03", day="2026-10-19"))
    assert not s.add(bar("09:03", l=10200))       # 저가가 시가·종가보다 높음
    assert not s.add(bar("09:03", h=9950))        # 고가가 시가·종가보다 낮음
    assert not s.add(bar("09:03", c=0))
    assert not s.add(bar("09:03", v=-1))
    assert not s.add(bar("09:04"))                # 3분 격자에서 벗어난 시각
    assert not s.bars


def test_session_boundaries_keep_closing_auction_and_drop_after_hours():
    s = session()
    assert not s.add(bar("09:00"))                # 장 시작 전 표기
    assert s.add(bar("15:21"))                    # 마지막 정규 봉
    assert s.add(bar("15:33"))                    # 15:30 종가 단일가 봉
    assert not s.add(bar("15:36"))                # 시간외 단일가
    assert not s.add(bar("19:57"))                # 애프터마켓
    assert [b.timestamp.strftime("%H:%M") for b in s.bars] == ["15:21", "15:33"]
    assert s.rejected == 0                        # 시간외 봉은 버리지만 비정상으로 세지 않는다


def test_missing_bars_are_counted_as_gaps():
    s = session()
    s.add(bar("09:09"))                           # 09:03, 09:06 봉 없음 (체결이 없던 구간)
    assert s.gaps == 2 and not s.has_opening_bar
    s.add(bar("09:12")); s.add(bar("09:21"))
    assert s.gaps == 4
    s2 = session()
    s2.add(bar("15:21")); gaps = s2.gaps
    s2.add(bar("15:33"))                          # 동시호가 공백은 빠진 봉이 아니다
    assert s2.gaps == gaps


# ------------------------------------------------------------------ 누적값
def test_vwap_uses_typical_price_weighted_by_volume():
    s = session()
    s.add(bar("09:03", o=100, h=110, l=90, c=100, v=10))      # 대표가 100
    s.add(bar("09:06", o=100, h=130, l=100, c=130, v=30))     # 대표가 120
    assert s.vwaps[0] == 100
    assert s.vwap == pytest.approx((100 * 10 + 120 * 30) / 40)
    assert s.cum_volume == 40 and s.day_high == 130 and s.day_low == 90


def test_gap_needs_previous_close_and_opening_bar():
    s = session()
    s.add(bar("09:03", o=10300, h=10400, l=10250, c=10350))
    assert s.gap_pct is None                      # 일봉을 아직 모름 → 지어내지 않는다
    s.daily = [{"dt": "20261019", "open": 9900, "high": 10100, "low": 9800, "close": 10000, "volume": 1}]
    assert s.gap_pct == pytest.approx(3.0)
    late = session()
    late.daily = s.daily
    late.add(bar("09:06"))
    assert late.gap_pct is None                   # 첫 봉이 없으면 시가를 알 수 없다


def test_scratch_space_is_separate_per_strategy():
    s = session()
    s.scratch("open")["x"] = 1
    assert s.scratch("intraday") == {} and s.scratch("open") == {"x": 1}


# ------------------------------------------------------------------ 전략 뼈대
class _Always(BaseStrategy):
    name = "always"
    label = "항상"

    def on_bar(self, session, ctx=None):
        return self.make_signal(session, "p", "테스트", stop_price=session.last.low)


def test_entry_window_and_enable_switch():
    st = _Always(StrategySettings(start_time="09:30", end_time="10:00"))
    assert not st.is_active(dtime(9, 27)) and st.is_active(dtime(9, 30)) and st.is_active(dtime(10, 0))
    assert not st.is_active(dtime(10, 3))
    st.update_settings(StrategySettings(enabled=False, start_time="09:30", end_time="10:00"))
    assert not st.is_active(dtime(9, 45))


def test_signal_carries_strategy_pattern_and_completed_bar_time():
    s = session()
    s.add(bar("09:03"))
    sig = _Always().on_bar(s)
    assert (sig.strategy, sig.pattern, sig.key, sig.action) == ("always", "p", "always.p", BUY)
    assert sig.entry_price == 10050 and sig.stop_price == 9900
    assert sig.timestamp == s.last.timestamp and not sig.hold_overnight
    assert sig.to_dict()["timestamp"] == "2026-10-20T09:03:00"


def test_settings_ignore_unknown_keys():
    st = StrategySettings.from_dict({"enabled": False, "end_time": "11:00", "no_such_key": 1})
    assert (st.enabled, st.start_time, st.end_time) == (False, "09:03", "11:00")


def _position(entry=10000.0, stop=0.0):
    return Position(symbol="005930", name="삼성전자", qty=10, avg_price=entry, current_price=entry,
                    stop_loss_price=stop, take_profit_price=0.0, entry_time=datetime(2026, 10, 20, 9, 30))


def test_strategy_exit_overrides_apply_only_to_its_own_positions():
    common = StrategyConfig()                                     # 손절 2%
    tight = _Always(StrategySettings(exit={"stop_loss_rate": 1.0, "unknown": 5}))
    plain = _Always()
    now = datetime(2026, 10, 20, 10, 0)
    assert tight.exit_config(common).stop_loss_rate == 1.0 and common.stop_loss_rate == 2.0
    assert plain.exit_config(common) is common
    assert tight.check_exit(_position(), 9900, common, now).signal_type == SignalType.SELL_STOP_LOSS
    assert plain.check_exit(_position(), 9900, common, now) is None
