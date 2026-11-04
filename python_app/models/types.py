"""
Korea3MinBreakoutTrader - Data Models
Pydantic / Dataclass models for Korean Stock 3-Minute Breakout Trading System.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any


class SignalType(str, Enum):
    BUY_BREAKOUT = "BUY_BREAKOUT"            # A. 첫 3분봉 시가 돌파 (기본 전략)
    BUY_ORB_HIGH = "BUY_ORB_HIGH"            # B. 첫 3분봉 고가 돌파
    BUY_VWAP_PULLBACK = "BUY_VWAP_PULLBACK"  # C. VWAP 눌림 반등
    BUY_PREV_HIGH = "BUY_PREV_HIGH"          # D. 전일 고가 돌파
    BUY_DAY_SWING_HIGH = "BUY_DAY_SWING_HIGH"  # E. 당일 전고점(스윙 고점) 돌파
    BUY_SMC_FVG = "BUY_SMC_FVG"              # F. SMC: CHoCH 후 첫 FVG 중간값 지정가 진입
    SELL_STOP_LOSS = "SELL_STOP_LOSS"
    SELL_TAKE_PROFIT = "SELL_TAKE_PROFIT"
    SELL_TRAILING_STOP = "SELL_TRAILING_STOP"
    SELL_TIME_CUT = "SELL_TIME_CUT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TradingMode(str, Enum):
    MOCK = "MOCK"        # 모의거래 — 키움 모의투자 서버 (mockapi.kiwoom.com)
    REAL = "REAL"        # 실거래   — 키움 실서버 (api.kiwoom.com)


@dataclass
class Bar:
    """3분봉 캔들 데이터 모델"""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    is_completed: bool = True
    bar_index: int = 0  # 당일 몇 번째 3분봉인지 (0: 09:00~09:03)

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M")

    @property
    def date_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d")


@dataclass
class Quote:
    """실시간 주식 시세 데이터 모델"""
    symbol: str
    name: str
    current_price: float
    open_price: float
    high_price: float
    low_price: float
    prev_close: float
    volume: int
    change_rate: float
    timestamp: datetime
    bid_price_1: float = 0.0
    ask_price_1: float = 0.0


@dataclass
class Signal:
    """전략 매매 신호 모델"""
    symbol: str
    name: str
    signal_type: SignalType
    price: float
    timestamp: datetime
    first_bar_close: float
    first_bar_volume: int
    current_volume: int
    volume_ratio: float
    reason: str
    bar_index: int = 0
    breakout_level: float = 0.0     # 돌파 기준가 (기본: 첫 3분봉 시가)
    stop_loss_price: float = 0.0    # 진입가 기준 손절가 (기본 -2%)
    strategy: str = "open_breakout"  # 신호를 낸 전략 키 (AND 조합이면 'a+b')
    order_type: Optional[OrderType] = None  # None → MARKET. SMC 는 LIMIT (FVG 중간값 지정가)
    take_profit_price: float = 0.0   # 전략 고정 목표가 (SMC: 진입가 + R × 배수), 0 = 없음
    use_trailing: bool = True        # False → 트레일링 스탑 미적용 (손절/목표가로만 청산)
    expire_at: Optional[datetime] = None  # 지정가 주문 유효 시각 (지나면 미체결 취소)
    stop_rate: float = 0.0           # 전략 손절 % — 실제 체결가 기준으로 손절가 계산 (0 = 전역 stop_loss_rate)
    trail_retrace_rate: float = 0.0  # 전략 트레일링 되돌림 % (0 = 전역 설정)
    trail_activation_rate: float = 0.0  # 전략 트레일링 발동 % (trail_retrace_rate > 0 일 때만 사용)

    def __post_init__(self):
        if self.order_type is None:
            self.order_type = OrderType.MARKET

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "signal_type": self.signal_type.value,
            "price": self.price,
            "timestamp": self.timestamp.isoformat(),
            "first_bar_close": self.first_bar_close,
            "first_bar_volume": self.first_bar_volume,
            "current_volume": self.current_volume,
            "volume_ratio": round(self.volume_ratio, 2),
            "reason": self.reason,
            "bar_index": self.bar_index,
            "breakout_level": self.breakout_level,
            "stop_loss_price": self.stop_loss_price,
            "strategy": self.strategy,
            "order_type": self.order_type.value,
            "take_profit_price": self.take_profit_price,
            "use_trailing": self.use_trailing,
            "expire_at": self.expire_at.isoformat() if self.expire_at else None,
            "stop_rate": self.stop_rate,
            "trail_retrace_rate": self.trail_retrace_rate,
            "trail_activation_rate": self.trail_activation_rate,
        }


@dataclass
class Order:
    """주문 데이터 모델"""
    order_id: str
    symbol: str
    name: str
    side: OrderSide
    order_type: OrderType
    price: float
    qty: int
    status: OrderStatus
    created_at: datetime
    filled_at: Optional[datetime] = None
    filled_price: float = 0.0
    filled_qty: int = 0
    commission: float = 0.0
    tax: float = 0.0
    reason: str = ""


@dataclass
class Position:
    """보유 포지션 모델"""
    symbol: str
    name: str
    qty: int
    avg_price: float
    current_price: float
    stop_loss_price: float
    take_profit_price: float
    entry_time: datetime
    highest_price: float = 0.0   # 진입 후 최고가 (트레일링 스탑 기준)
    sellable_qty: int = 0        # 매도가능수량 (키움 trde_able_qty, 0이면 qty 사용)

    def __post_init__(self):
        if self.highest_price <= 0:
            self.highest_price = max(self.avg_price, self.current_price)

    @property
    def eval_amount(self) -> float:
        return self.qty * self.current_price

    @property
    def buy_amount(self) -> float:
        return self.qty * self.avg_price

    @property
    def unrealized_pnl(self) -> float:
        # 평가손익 = 평가금액 - 매수금액 - 매도시 예상 수수료/세금
        gross = (self.current_price - self.avg_price) * self.qty
        tax_and_fees = self.eval_amount * (0.00015 + 0.0020) # 0.015% 수수료 + 0.20% 거래세
        return gross - tax_and_fees

    @property
    def pnl_rate(self) -> float:
        if self.buy_amount <= 0:
            return 0.0
        return (self.unrealized_pnl / self.buy_amount) * 100.0


@dataclass
class TradeRecord:
    """체결 및 매매 완료 기록"""
    trade_id: str
    order_id: str
    symbol: str
    name: str
    side: OrderSide
    price: float
    qty: int
    commission: float
    tax: float
    realized_pnl: float
    executed_at: datetime
    reason: str = ""


@dataclass
class StrategyConfig:
    """3분봉 돌파 전략 파라미터 설정"""
    # 진입 허용 시간대 (HH:MM)
    trade_start_time: str = "09:03"
    trade_end_time: str = "10:00"
    
    # 돌파 기준가: "open" = 첫 3분봉 시가 돌파(기본), "close" = 첫 3분봉 종가 돌파(구버전)
    breakout_reference: str = "open"

    # 손익 관리
    stop_loss_rate: float = 2.0       # 손절 기준 (%) — 진입가 대비 -2%
    trailing_retrace_rate: float = 15.0  # 트레일링 스탑: 상승분(최고가-진입가)의 N% 되돌림 시 청산 (0 = 사용 안 함)
    trailing_activation_rate: float = 1.0  # 트레일링 발동 조건: 진입 후 최고가가 +1% 이상 올라야 15% 룰 적용
    take_profit_rate: float = 0.0     # 고정 익절 (%) — 0 = 사용 안 함 (트레일링 스탑으로 대체)
    use_first_bar_low_stop: bool = False # 첫 봉 저가 이탈 즉시 손절 (기본 OFF: -2% 고정 손절만 사용)

    # 장 마감 일괄 청산
    eod_liquidation: bool = True            # 지정 시각에 앱 매수 종목 전량 시장가 청산
    eod_liquidation_time: str = "15:15"     # 이 시각부터 청산 + 신규 매수 금지 (15:20 동시호가 전)
    carryover_sell_at_open: bool = True     # 앱이 꺼져 청산 못 하고 넘어온 종목 → 다음 날 09:00 시장가 청산
    
    # 자금 관리
    order_amount_per_trade: int = 1_000_000 # 1회 매수 금액 (원)
    max_concurrent_positions: int = 3       # 최대 동시 보유 종목 수
    max_daily_loss: int = 300_000           # 당일 최대 누적 손실 허용한도 (원)
    
    # 중복 신호 제어
    allow_reentry_same_day: bool = False    # 당일 동일 종목 재진입 허용 여부
    min_volume_ratio: float = 0.7           # 돌파 봉 거래량 > 첫 3분봉 거래량 × 0.7 (70% 초과)

    # 매수 전략 조합 (strategy/combiner.py)
    #   open_breakout = A. 첫 봉 시가 돌파 / orb_high = B. 첫 봉 고가 돌파 / vwap_pullback = C. VWAP 눌림 반등
    #   prev_high = D. 전일 고가 돌파 / day_swing_high = E. 당일 전고점 돌파
    #   smc_fvg = F. SMC CHoCH → 첫 FVG 중간값 지정가
    entry_strategies: List[str] = field(default_factory=lambda: ["open_breakout"])
    entry_logic: str = "OR"                 # OR = 하나라도 신호 → 진입 / AND = 선택 전략이 당일 모두 신호를 낸 시점에 진입

    # C. VWAP 눌림 반등 (진입 시작은 trade_start_time)
    vwap_end_time: str = "11:00"            # 신규 진입 마감
    vwap_trend_pct: float = 1.0             # 눌림 전에 고가가 VWAP보다 N% 이상 위에 있었어야 함 (추세 확인)
    vwap_touch_pct: float = 0.3             # 저가가 VWAP ±N% 안까지 내려오면 '눌림'

    # E. 당일 전고점 돌파: 자체 손절·트레일링 (전역 stop_loss_rate / trailing_* 와 별개)
    dsh_start_time: str = "09:30"
    dsh_end_time: str = "14:00"
    dsh_swing_len: int = 2                  # 전고점 판정: 좌우 N봉보다 고가가 높아야 함
    dsh_stop_rate: float = 0.5              # 손절: 체결가 대비 -N%
    dsh_trail_retrace_rate: float = 50.0    # 상승분(최고가-체결가)의 N% 되돌림 시 청산
    dsh_trail_activation_rate: float = 0.5  # 최고가가 +N% 이상 가야 트레일링 적용 (그 전에는 손절만)

    # F. SMC: CHoCH → 첫 FVG 중간값 지정가 진입, 목표 = 진입가 + R × smc_rr
    smc_start_time: str = "09:30"
    smc_end_time: str = "14:00"
    smc_swing_len: int = 2                  # 스윙 고/저점 판정: 좌우 N봉보다 높/낮아야 함
    smc_rr: float = 3.0                     # 목표 R 배수 (3R / 4R)
    smc_stop_mode: str = "swing"            # swing = CHoCH 전 스윙 저점 아래 / fvg = FVG 하단 아래
    smc_min_risk_pct: float = 0.3           # 손절폭(진입가 대비 %)이 이보다 작으면 생략 (수수료·호가 대비 의미 없음)
    smc_max_risk_pct: float = 3.0           # 손절폭이 이보다 크면 생략
    smc_fvg_expiry_bars: int = 10           # 지정가 대기 최대 봉 수 (10봉 = 30분)
    smc_use_trailing: bool = False          # False = 손절/목표가(R)로만 청산
