"""
Korea3MinBreakoutTrader - Data Models
Pydantic / Dataclass models for Korean Stock 3-Minute Breakout Trading System.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any


class SignalType(str, Enum):
    # 매수 신호는 strategies.base_strategy.StrategySignal (전략·패턴 이름으로 구분)
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
    strategy: str = ""    # 이 거래를 소유한 전략 (open / intraday / close)
    mode: str = ""        # MOCK / REAL


@dataclass
class StrategyConfig:
    """공통 매매 설정 — 청산 규칙과 자금 관리. 진입 조건·시간대는 전략별 설정(config.json 의 strategies)에 있다."""
    # 손익 관리 (전략이 따로 정하지 않은 값의 기본)
    stop_loss_rate: float = 2.0       # 손절 기준 (%) — 전략이 손절가를 주지 않았을 때 진입가 대비
    trailing_retrace_rate: float = 50.0  # 트레일링 스탑: 최고가 1R 미만일 때 상승분(최고가-진입가)의 N% 되돌림 시 청산 (0 = 사용 안 함)
    trailing_retrace_rate_1r: float = 30.0  # 최고가가 1R(진입가-손절가) 이상일 때 되돌림 N% (0 = 구간 구분 없음)
    trailing_activation_rate: float = 1.0  # 트레일링 발동 조건: 진입 후 최고가가 +1% 이상 올라야 적용
    take_profit_rate: float = 0.0     # 고정 익절 (%) — 0 = 사용 안 함 (트레일링 스탑으로 대체)

    # 장 마감 일괄 청산 (오버나이트 보유 전략의 포지션은 제외)
    eod_liquidation: bool = True            # 지정 시각에 앱 매수 종목 전량 시장가 청산
    eod_liquidation_time: str = "15:15"     # 이 시각부터 청산 + 신규 매수 금지 (15:20 동시호가 전)
    carryover_sell_at_open: bool = True     # 앱이 꺼져 청산 못 하고 넘어온 종목 → 다음 날 09:00 시장가 청산

    # 자금 관리
    order_amount_per_trade: int = 1_000_000 # 1회 매수 금액 (원)
    max_concurrent_positions: int = 3       # 최대 동시 보유 종목 수
    max_daily_loss: int = 300_000           # 당일 최대 누적 손실 허용한도 (원)

    # 중복 진입 제어
    allow_reentry_same_day: bool = False    # 당일 동일 종목 재진입 허용 여부
