"""
Korea3MinBreakoutTrader - 추가 매수 전략 (3분봉, 장중)

[A. 첫 3분봉 고가 돌파 — orb_high]
   - 기준선 = 첫 3분봉(09:00~09:03) 고가
   - 직전 봉 종가 ≤ 기준선 < 현재 봉 종가  (상향 돌파)
   - 돌파 봉 거래량 > 첫 봉 거래량 × min_volume_ratio
   - 진입 시간대 trade_start_time ~ trade_end_time
   → 갭상승 후 시가 아래로 내려오지 않고 계속 오르는 종목을 잡는다 (시가 돌파 전략이 놓치는 경우)

[B. VWAP 눌림 반등 — vwap_pullback]
   - VWAP = Σ(대표가 × 거래량) / Σ거래량,  대표가 = (고가 + 저가 + 종가) / 3   (당일 3분봉 누적)
   - 추세 확인: 이전 봉 중 고가가 그 시점 VWAP × (1 + vwap_trend_pct%) 이상인 봉이 있었음
   - 눌림: 현재 봉 저가가 VWAP × (1 ± vwap_touch_pct%) 범위 안까지 내려옴
   - 반등: 현재 봉 양봉(종가 > 시가) + 종가 > VWAP
   - 진입 시간대 trade_start_time ~ vwap_end_time

[C. 전일 고가 돌파 — prev_high]
   - 기준선 = 전일 일봉 고가 (엔진이 키움 일봉 ka10081 로 조회해서 넘겨줌)
   - 직전 봉 종가 ≤ 기준선 < 현재 봉 종가 + 거래량 조건(A와 동일)
   - 진입 시간대 trade_start_time ~ trade_end_time

[D. 당일 전고점 돌파 — day_swing_high]
   - 전고점 = 가장 최근 확정된 당일 스윙 고점 (좌우 dsh_swing_len 봉보다 고가가 높은 봉, 오른쪽 N봉 완성 후 확정)
   - 종가가 전고점 위로 마감 + 거래량 조건(A와 동일). 돌파된 전고점은 소진 (거래량 미달이어도 다음 스윙을 기다림)
   - 진입 시간대 dsh_start_time ~ dsh_end_time
   - 청산: 체결가 대비 -dsh_stop_rate% 손절, 최고가 +dsh_trail_activation_rate% 이후 상승분의
     dsh_trail_retrace_rate% 되돌림 시 트레일링 청산 (전역 손절·트레일링 설정과 별개)

공통: 시장가 진입, 손절 = 진입가 × (1 - stop_loss_rate%) (D는 위 값), 종목당 당일 1회 신호 (allow_reentry_same_day 제외)
"""

import logging
from datetime import datetime, time as dtime
from typing import Callable, Dict, List, Optional

from python_app.models.types import Bar, Signal, SignalType, StrategyConfig

logger = logging.getLogger("Korea3MinBreakoutTrader.Strategy")


def parse_hhmm(hhmm: str) -> dtime:
    return datetime.strptime(hhmm, "%H:%M").time()


class DayState:
    """종목별 당일 3분봉 누적 상태 (전략마다 하나씩)"""

    def __init__(self, symbol: str, name: str, date_str: str):
        self.symbol = symbol
        self.name = name
        self.date_str = date_str
        self.bars: List[Bar] = []
        self.vwaps: List[float] = []      # 각 봉 마감 시점 VWAP
        self._pv = 0.0
        self._v = 0
        self.signal_fired = False
        self.extra: Dict = {}

    @property
    def first(self) -> Bar:
        return self.bars[0]

    def add(self, bar: Bar):
        bar.bar_index = len(self.bars)
        self.bars.append(bar)
        self._pv += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        self._v += bar.volume
        self.vwaps.append(self._pv / self._v if self._v > 0 else bar.close)


class BarStrategy:
    """완성된 3분봉을 하나씩 받아 매수 신호를 내는 전략의 공통 뼈대"""
    key = ""
    label = ""
    signal_type = SignalType.BUY_BREAKOUT

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.states: Dict[str, DayState] = {}
        self._apply_config()

    def _apply_config(self):
        self.start_time = parse_hhmm(self.config.trade_start_time)
        self.end_time = parse_hhmm(self.config.trade_end_time)

    def update_config(self, config: StrategyConfig):
        self.config = config
        self._apply_config()

    def process_bar(self, bar: Bar, name: str = "") -> Optional[Signal]:
        if not bar.is_completed:
            return None
        st = self.states.get(bar.symbol)
        if st is None or st.date_str != bar.date_str:
            st = self.states[bar.symbol] = DayState(bar.symbol, name, bar.date_str)
        elif name and not st.name:
            st.name = name
        if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0 or bar.low > bar.high or bar.volume < 0:
            logger.warning(f"[{bar.symbol}] {self.label}: 비정상 봉 무시 O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
            return None
        if st.bars and bar.timestamp <= st.bars[-1].timestamp:
            return None
        st.add(bar)
        return self._evaluate(st, bar)

    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        raise NotImplementedError

    def _can_enter(self, st: DayState, bar: Bar) -> bool:
        if not (self.start_time <= bar.timestamp.time() <= self.end_time):
            return False
        return not (st.signal_fired and not self.config.allow_reentry_same_day)

    def _volume_ok(self, st: DayState, bar: Bar, what: str) -> bool:
        threshold = st.first.volume * self.config.min_volume_ratio
        if bar.volume > threshold:
            return True
        logger.info(f"[{bar.symbol} {st.name}] {bar.time_str} {what} 했으나 거래량 미달 "
                    f"({bar.volume:,} <= {threshold:,.0f}) → 진입 보류")
        return False

    @property
    def stop_rate(self) -> float:
        return self.config.stop_loss_rate

    def _signal(self, st: DayState, bar: Bar, level: float, reason: str, **kw) -> Signal:
        entry = bar.close
        stop = round(entry * (1.0 - self.stop_rate / 100.0))
        first = st.first
        sig = Signal(
            symbol=bar.symbol, name=st.name, signal_type=self.signal_type, price=entry,
            timestamp=bar.timestamp, first_bar_close=first.close, first_bar_volume=first.volume,
            current_volume=bar.volume,
            volume_ratio=(bar.volume / first.volume) if first.volume > 0 else 0.0,
            reason=f"[{self.label}] {reason} | 손절 {stop:,.0f}원(-{self.stop_rate:g}%)",
            bar_index=bar.bar_index, breakout_level=level, stop_loss_price=stop, strategy=self.key, **kw,
        )
        st.signal_fired = True
        logger.info(f"★ [{bar.symbol} {st.name}] 매수 신호 {sig.reason}")
        return sig


def _cross_up(prev: Bar, bar: Bar, level: float) -> bool:
    return prev.close <= level < bar.close


class OrbHighBreakout(BarStrategy):
    """A. 첫 3분봉 고가 돌파"""
    key = "orb_high"
    label = "A.첫봉 고가 돌파"
    signal_type = SignalType.BUY_ORB_HIGH

    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        if len(st.bars) < 2 or not self._can_enter(st, bar):
            return None
        level = st.first.high
        if not _cross_up(st.bars[-2], bar, level) or not self._volume_ok(st, bar, "첫 봉 고가 돌파"):
            return None
        vr = bar.volume / st.first.volume if st.first.volume else 0.0
        return self._signal(st, bar, level, f"3분봉 종가 {bar.close:,.0f}원이 첫 봉 고가 {level:,.0f}원 상향 돌파 "
                                            f"+ 거래량 {vr:.1f}배")


class VwapPullback(BarStrategy):
    """B. VWAP 눌림 반등"""
    key = "vwap_pullback"
    label = "B.VWAP 눌림 반등"
    signal_type = SignalType.BUY_VWAP_PULLBACK

    def _apply_config(self):
        super()._apply_config()
        self.end_time = parse_hhmm(self.config.vwap_end_time)

    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        vwap = st.vwaps[-1]
        trend_before = st.extra.get("trend", False)
        if bar.high >= vwap * (1.0 + self.config.vwap_trend_pct / 100.0):
            st.extra["trend"] = True          # 이번 봉부터 추세 인정 (다음 봉부터 눌림 판정)
        if len(st.bars) < 2 or not trend_before or not self._can_enter(st, bar):
            return None
        band = self.config.vwap_touch_pct / 100.0
        touched = vwap * (1.0 - band) <= bar.low <= vwap * (1.0 + band)
        if not (touched and bar.close > bar.open and bar.close > vwap):
            return None
        return self._signal(st, bar, round(vwap),
                            f"VWAP {vwap:,.0f}원 눌림(저가 {bar.low:,.0f}) 후 양봉 반등 마감 {bar.close:,.0f}원")


class PrevHighBreakout(BarStrategy):
    """C. 전일 고가 돌파 — 전일 고가는 prev_high_provider(symbol, 'YYYYMMDD') 로 받는다"""
    key = "prev_high"
    label = "C.전일 고가 돌파"
    signal_type = SignalType.BUY_PREV_HIGH

    def __init__(self, config: StrategyConfig,
                 prev_high_provider: Optional[Callable[[str, str], Optional[float]]] = None):
        super().__init__(config)
        self.prev_high_provider = prev_high_provider

    def _prev_high(self, st: DayState) -> Optional[float]:
        if "prev_high" not in st.extra:
            value = None
            if self.prev_high_provider:
                try:
                    value = self.prev_high_provider(st.symbol, st.date_str.replace("-", ""))
                except Exception as e:
                    logger.warning(f"[{st.symbol}] 전일 고가 조회 실패: {e}")
            st.extra["prev_high"] = value if value and value > 0 else None
            if st.extra["prev_high"] is None:
                logger.warning(f"[{st.symbol} {st.name}] 전일 고가를 알 수 없어 오늘 '{self.label}' 판정 안 함")
        return st.extra["prev_high"]

    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        if len(st.bars) < 2 or not self._can_enter(st, bar):
            return None
        level = self._prev_high(st)
        if level is None or not _cross_up(st.bars[-2], bar, level):
            return None
        if not self._volume_ok(st, bar, "전일 고가 돌파"):
            return None
        return self._signal(st, bar, level, f"3분봉 종가 {bar.close:,.0f}원이 전일 고가 {level:,.0f}원 상향 돌파")



class DaySwingHighBreakout(BarStrategy):
    """D. 당일 전고점(스윙 고점) 돌파 — 자체 손절·트레일링"""
    key = "day_swing_high"
    label = "D.당일 전고점 돌파"
    signal_type = SignalType.BUY_DAY_SWING_HIGH

    def _apply_config(self):
        self.start_time = parse_hhmm(self.config.dsh_start_time)
        self.end_time = parse_hhmm(self.config.dsh_end_time)

    @property
    def stop_rate(self) -> float:
        return self.config.dsh_stop_rate

    def _update_swing(self, st: DayState):
        L = max(1, int(self.config.dsh_swing_len))
        k = len(st.bars) - 1 - L
        if k - L < 0:
            return
        high = st.bars[k].high
        if high > max(b.high for b in st.bars[k - L:k]) and high >= max(b.high for b in st.bars[k + 1:]):
            st.extra["swing_high"] = high

    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        self._update_swing(st)
        level = st.extra.get("swing_high")
        if not level or bar.close <= level:
            return None
        st.extra["swing_high"] = None
        if not self._can_enter(st, bar) or not self._volume_ok(st, bar, "당일 전고점 돌파"):
            return None
        c = self.config
        return self._signal(st, bar, level, f"3분봉 종가 {bar.close:,.0f}원이 당일 전고점 {level:,.0f}원 상향 돌파",
                            stop_rate=c.dsh_stop_rate, trail_retrace_rate=c.dsh_trail_retrace_rate,
                            trail_activation_rate=c.dsh_trail_activation_rate)
