"""
Korea3MinBreakoutTrader - First 3-Minute Candle Breakout Strategy Engine
전략명: First3MinuteCandleBreakout

[매수 진입 조건 — 3분봉 시가돌파 + 거래량 충족 + 손절 2%]
1. 당일 첫 3분봉(09:00~09:03)이 완성되면 다음 값을 고정한다.
   - breakout_level   = 첫 3분봉 시가 (= 당일 시가)          ← 돌파 기준선
   - first_bar_volume = 첫 3분봉 거래량                        ← 거래량 기준
2. 이후 완성된 3분봉에서 아래 두 조건을 '동시에' 만족하면 매수 신호.
   - cross_up         = (previous_close <= breakout_level) and (current_close > breakout_level)
   - volume_confirmed = current_bar_volume > first_bar_volume * min_volume_ratio   (기본 0.7 = 70% 초과)
3. 매수 신호에는 진입가 기준 손절가가 함께 실린다.
   - stop_loss_price  = entry_price * (1 - stop_loss_rate / 100)   (기본 2.0% → 진입가 × 0.98)
4. 공통 필터: 첫 봉 자체에서는 신호 없음 / 진입 허용 시간대 / 종목당 당일 1회 진입.

[HTS 수식 표현]
   crossup(C, valuewhen(1, date!=date(1), O)) && V > valuewhen(1, date!=date(1), V)
   (첫 봉 '자신'의 O, V를 참조. 구버전처럼 C(1)/V(1)을 쓰면 전일 마지막 봉을 참조하는 함정이 있음)

※ 구버전(첫 3분봉 '종가' 돌파)은 StrategyConfig.breakout_reference = "close" 로 되돌릴 수 있다.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from python_app.models.types import Bar, Signal, SignalType, StrategyConfig

logger = logging.getLogger("Korea3MinBreakoutTrader.Strategy")

_REFERENCE_LABEL = {"open": "시가", "close": "종가"}


class SymbolDailyState:
    """종목별 당일 상태 관리 객체"""
    def __init__(self, symbol: str, name: str = ""):
        self.symbol = symbol
        self.name = name
        self.current_date_str: Optional[str] = None

        # 첫 3분봉 기준 데이터
        self.first_bar: Optional[Bar] = None
        self.first_bar_open: Optional[float] = None
        self.first_bar_close: Optional[float] = None
        self.first_bar_volume: Optional[int] = None
        self.first_bar_high: Optional[float] = None
        self.first_bar_low: Optional[float] = None
        self.breakout_level: Optional[float] = None  # 실제 돌파 기준가 (기본: 첫 봉 시가)

        # 전일 마지막 봉 데이터 (HTS 원본 수식 비교용)
        self.prev_day_last_close: Optional[float] = None
        self.prev_day_last_volume: Optional[int] = None

        # 신호 및 진행 상태
        self.signal_fired_today: bool = False
        self.last_completed_bar: Optional[Bar] = None
        self.today_bars: List[Bar] = []
        self.today_signals: List[Signal] = []

    def reset_for_new_day(self, date_str: str):
        """거래일 변경 시 당일 기준값 초기화"""
        if self.today_bars:
            self.prev_day_last_close = self.today_bars[-1].close
            self.prev_day_last_volume = self.today_bars[-1].volume

        self.current_date_str = date_str
        self.first_bar = None
        self.first_bar_open = None
        self.first_bar_close = None
        self.first_bar_volume = None
        self.first_bar_high = None
        self.first_bar_low = None
        self.breakout_level = None
        self.signal_fired_today = False
        self.last_completed_bar = None
        self.today_bars = []
        self.today_signals = []
        logger.info(f"[{self.symbol}] 새 거래일({date_str}) 상태 초기화 완료")


class First3MinuteCandleBreakout:
    """
    국내 주식 3분봉 시가돌파 자동매매 전략 엔진
    - 첫 3분봉(09:00~09:03) 확정 후 시가(돌파 기준) / 거래량(거래량 기준) 저장
    - 이후 완성된 3분봉에서 시가 상향 돌파 + 거래량 충족 시 매수 신호
    - 신호에 진입가 대비 손절가(기본 -2%) 포함
    - 지정 시간대 내에서만 신규 매수, 종목당 당일 1회 진입
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.symbol_states: Dict[str, SymbolDailyState] = {}
        self._apply_config()

    def _apply_config(self):
        self.start_time = datetime.strptime(self.config.trade_start_time, "%H:%M").time()
        self.end_time = datetime.strptime(self.config.trade_end_time, "%H:%M").time()
        if self.config.breakout_reference not in _REFERENCE_LABEL:
            raise ValueError(
                f"breakout_reference는 'open' 또는 'close'여야 합니다: {self.config.breakout_reference!r}"
            )

    def update_config(self, config: StrategyConfig):
        """GUI 설정 변경 반영"""
        self.config = config
        self._apply_config()

    def get_or_create_state(self, symbol: str, name: str = "") -> SymbolDailyState:
        if symbol not in self.symbol_states:
            self.symbol_states[symbol] = SymbolDailyState(symbol, name)
        elif name and not self.symbol_states[symbol].name:
            self.symbol_states[symbol].name = name
        return self.symbol_states[symbol]

    # ------------------------------------------------------------------ 손절
    def calc_stop_loss_price(self, entry_price: float, state: Optional[SymbolDailyState] = None) -> float:
        """
        진입가 기준 손절가 계산 (기본 -2%).
        use_first_bar_low_stop=True 이면 '첫 봉 저가'와 '-2%' 중 더 높은(=먼저 닿는) 가격을 손절가로 사용.
        """
        stop = entry_price * (1.0 - self.config.stop_loss_rate / 100.0)
        if self.config.use_first_bar_low_stop and state is not None and state.first_bar_low:
            stop = max(stop, state.first_bar_low)
        return round(stop)

    @staticmethod
    def is_stop_loss_hit(current_price: float, stop_loss_price: float) -> bool:
        return stop_loss_price > 0 and current_price <= stop_loss_price

    # ------------------------------------------------------------------ 검증
    def validate_bar(self, bar: Bar, state: SymbolDailyState) -> Tuple[bool, str]:
        """시세 데이터 무결성 검증 (누락, 비정상 가격, 역순 시계열)"""
        if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
            return False, f"비정상 가격 감지: O={bar.open}, H={bar.high}, L={bar.low}, C={bar.close}"
        if bar.low > bar.high:
            return False, f"저가가 고가보다 큼: L={bar.low} > H={bar.high}"
        if bar.volume < 0:
            return False, f"음수 거래량 감지: V={bar.volume}"

        if state.today_bars:
            last_bar = state.today_bars[-1]
            if bar.timestamp <= last_bar.timestamp:
                return False, f"역순/중복 타임스탬프 감지: 현재={bar.timestamp} <= 이전={last_bar.timestamp}"

        return True, "OK"

    # ------------------------------------------------------------------ 신호
    def process_bar(self, bar: Bar, name: str = "") -> Optional[Signal]:
        """
        완성된 3분봉 수신 시 전략 신호 판정
        반환: 매수 신호가 발생하면 Signal 객체, 없으면 None
        """
        state = self.get_or_create_state(bar.symbol, name)
        bar_date = bar.date_str

        # 1. 날짜 변경 감지 및 당일 기준값 갱신
        if state.current_date_str != bar_date:
            state.reset_for_new_day(bar_date)

        # 2. 데이터 무결성 검증
        is_valid, reason = self.validate_bar(bar, state)
        if not is_valid:
            logger.warning(f"[{bar.symbol}] 봉 검증 실패: {reason}")
            return None

        # 미완성 봉은 평가하지 않음
        if not bar.is_completed:
            return None

        bar_time = bar.timestamp.time()
        bar_index = len(state.today_bars)
        bar.bar_index = bar_index
        state.today_bars.append(bar)

        # 3. 당일 첫 번째 3분봉 → 기준값 등록 (신호 없음)
        if bar_index == 0 or state.breakout_level is None:
            state.first_bar = bar
            state.first_bar_open = bar.open
            state.first_bar_close = bar.close
            state.first_bar_volume = bar.volume
            state.first_bar_high = bar.high
            state.first_bar_low = bar.low
            state.breakout_level = bar.open if self.config.breakout_reference == "open" else bar.close
            state.last_completed_bar = bar

            ref_label = _REFERENCE_LABEL[self.config.breakout_reference]
            logger.info(
                f"[{bar.symbol} {state.name}] 첫 3분봉({bar.time_str}) 확정 -> "
                f"돌파 기준({ref_label}): {state.breakout_level:,.0f}원, "
                f"기준 거래량: {bar.volume:,}주"
            )
            return None

        # 4. 이전 봉 확인
        previous_bar = state.last_completed_bar
        state.last_completed_bar = bar
        if previous_bar is None:
            return None

        # 5. 진입 허용 시간대
        if not (self.start_time <= bar_time <= self.end_time):
            logger.debug(
                f"[{bar.symbol}] 거래 허용 시간 외 ({bar_time.strftime('%H:%M')} "
                f"not in {self.config.trade_start_time}~{self.config.trade_end_time})"
            )
            return None

        # 6. 당일 중복 진입 방지
        if state.signal_fired_today and not self.config.allow_reentry_same_day:
            return None

        # 7. 돌파 + 거래량 판정
        level = state.breakout_level
        cross_up = (previous_bar.close <= level) and (bar.close > level)

        volume_threshold = state.first_bar_volume * self.config.min_volume_ratio
        volume_confirmed = bar.volume > volume_threshold
        volume_ratio = (bar.volume / state.first_bar_volume) if state.first_bar_volume > 0 else 0.0

        if cross_up and not volume_confirmed:  # 시가 돌파했지만 거래량 70% 이하
            logger.info(
                f"[{bar.symbol} {state.name}] {bar.time_str} 시가 돌파했으나 거래량 미달 "
                f"({bar.volume:,} <= {volume_threshold:,.0f}) → 진입 보류"
            )
            return None

        if not cross_up:
            return None

        # 8. 매수 신호 생성 (손절가 포함)
        entry_price = bar.close
        stop_loss_price = self.calc_stop_loss_price(entry_price, state)
        ref_label = _REFERENCE_LABEL[self.config.breakout_reference]

        signal = Signal(
            symbol=bar.symbol,
            name=state.name,
            signal_type=SignalType.BUY_BREAKOUT,
            price=entry_price,
            timestamp=bar.timestamp,
            first_bar_close=state.first_bar_close,
            first_bar_volume=state.first_bar_volume,
            current_volume=bar.volume,
            volume_ratio=volume_ratio,
            reason=(
                f"3분봉 {ref_label}({level:,.0f}원) 상향 돌파 + "
                f"거래량({bar.volume:,} > 기준 {state.first_bar_volume:,}, {volume_ratio:.1f}배) | "
                f"손절 {stop_loss_price:,.0f}원(-{self.config.stop_loss_rate:g}%)"
            ),
            bar_index=bar_index,
            breakout_level=level,
            stop_loss_price=stop_loss_price,
        )
        state.signal_fired_today = True
        state.today_signals.append(signal)
        logger.info(f"★ [{bar.symbol} {state.name}] 매수 돌파 신호 포착! {signal.reason}")
        return signal

    def get_formula_comparison_report(self) -> Dict[str, str]:
        """HTS 수식과 본 시스템 구현 간 비교 리포트"""
        return {
            "hts_original_formula": "crossup(C, valuewhen(1, date!=date(1), C(1))) && V > valuewhen(1, date!=date(1), V(1))",
            "hts_open_breakout_formula": "crossup(C, valuewhen(1, date!=date(1), O)) && V > valuewhen(1, date!=date(1), V)",
            "system_explicit_formula": (
                "breakout_level = first_bar_open\n"
                "cross_up = (previous_close <= breakout_level) and (current_close > breakout_level)\n"
                "volume_confirmed = current_bar_volume > first_bar_volume * min_volume_ratio\n"
                "bullish_signal = cross_up and volume_confirmed (단, bar_index >= 1, 시간대 필터, 중복 방지)\n"
                "stop_loss_price = entry_price * (1 - stop_loss_rate/100)  # 기본 2%"
            ),
            "key_differences": (
                "1. [돌파 기준 변경]: 첫 3분봉 종가 → 첫 3분봉 시가(당일 시가) 상향 돌파.\n"
                "2. [거래량 조건부]: 돌파 봉 거래량이 첫 3분봉 거래량의 70%(min_volume_ratio)를 초과할 때만 진입.\n"
                "3. [손절 2%]: 진입가 대비 -2% 고정 손절가를 신호에 포함 (첫 봉 저가 손절은 옵션, 기본 OFF).\n"
                "4. [인덱싱 함정]: HTS에서 C(1)/V(1)을 쓰면 전일 마지막 봉을 참조하므로, 첫 봉 자신의 O/V를 써야 함.\n"
                "5. [시간대/1회 진입]: 09:03~10:00(기본) 내 종목당 1회만 진입."
            ),
            "recommendation": "첫 3분봉 시가 돌파 + 거래량 충족 + -2% 손절 조합을 기본 전략으로 사용합니다.",
        }
