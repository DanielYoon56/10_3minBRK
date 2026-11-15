"""
3WAYTRADING - 전략 공통 인터페이스

  StrategySignal : 전략이 내는 신호. 주문이 아니다 — 자금 보호 검사를 통과해야 엔진이 주문한다.
  SymbolSession  : 종목별 당일 3분봉 누적 상태 (완성 봉만, 정규장만). 실거래·백테스트가 같은 객체를 쓴다.
  BaseStrategy   : OPEN / INTRADAY / CLOSE 전략의 뼈대. 진입(on_bar)과 청산(check_exit)을 전략마다 따로 가진다.

봉 규칙 (키움 ka10080 실측 기준)
  - Bar.timestamp 는 봉 마감 시각 (09:00~09:03 봉 → 09:03)
  - 정규장 봉은 09:03 ~ 15:21, 그 뒤 15:30 종가 단일가 봉(마감 15:33) 하나. 15:33 이후는 시간외 → 받지 않는다.
  - 키움은 체결이 없던 3분 구간의 봉을 보내지 않는다 → 빈 구간 수(gaps)를 유동성 지표로 남긴다.
"""

import logging
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from python_app.models.types import Bar, OrderType, Position, StrategyConfig
from python_app.strategy.exit_rules import ExitDecision, check_exit

logger = logging.getLogger("3WAYTRADING.Strategy")

BAR_MINUTES = 3
FIRST_BAR_END = dtime(9, 3)
LAST_REGULAR_BAR_END = dtime(15, 21)
CLOSING_AUCTION_BAR_END = dtime(15, 33)

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"
_NORMAL_SKIPS = ("duplicate", "incomplete", "outside session")


def parse_hhmm(hhmm: str) -> dtime:
    return datetime.strptime(hhmm, "%H:%M").time()


@dataclass
class StrategySignal:
    strategy: str                 # open / intraday / close
    pattern: str                  # 전략 안의 진입 패턴 (예: orb, pullback, momentum)
    symbol: str
    name: str
    action: str                   # BUY / SELL / HOLD
    entry_price: float
    timestamp: datetime           # 신호를 낸 봉의 마감 시각
    reason: str
    stop_price: Optional[float] = None     # 구조 기반 손절가 (None → 공통 손절 %)
    target_price: Optional[float] = None
    confidence: float = 0.0       # 규칙 충족 정도 0~1. 검증된 확률이 아니다
    position_size_ratio: Optional[float] = None   # 기본 매수 금액 대비 배수 (None = 1)
    order_type: OrderType = OrderType.MARKET
    expire_at: Optional[datetime] = None          # 지정가 유효 시각
    hold_overnight: bool = False  # True → 장 마감 일괄 청산·익일 시초 청산 대상에서 제외 (CLOSE)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.strategy}.{self.pattern}" if self.pattern else self.strategy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy, "pattern": self.pattern, "symbol": self.symbol, "name": self.name,
            "action": self.action, "confidence": round(self.confidence, 3), "entry_price": self.entry_price,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "position_size_ratio": self.position_size_ratio, "reason": self.reason,
            "timestamp": self.timestamp.isoformat(), "order_type": self.order_type.value,
            "hold_overnight": self.hold_overnight, "metadata": self.metadata,
        }


@dataclass
class MarketContext:
    """전략 판정에 함께 넘기는 주변 정보. 없는 값은 None — 전략은 없는 정보를 지어내지 않는다."""
    now: datetime
    candidate: Any = None                 # 스캐너의 Candidate (순위·등락률·거래대금) 또는 None
    index: Optional[Dict[str, float]] = None   # 시장 지수 정보 (조회가 붙기 전에는 None)


class SymbolSession:
    """종목별 당일 3분봉 누적 상태"""

    def __init__(self, symbol: str, name: str, date_str: str):
        self.symbol = symbol
        self.name = name
        self.date_str = date_str            # YYYY-MM-DD
        self.bars: List[Bar] = []
        self.vwaps: List[float] = []        # 각 봉 마감 시점 VWAP
        self.cum_volume = 0
        self.cum_value = 0.0                # 누적 거래대금 근사 = Σ 대표가 × 거래량 (봉에 거래대금 필드가 없다)
        self.gaps = 0                       # 정규장에서 봉이 빠진(체결 없던) 3분 구간 수
        self.rejected = 0                   # 비정상(가격·시각 오류, 역순)이라 버린 봉 수
        self.daily: Optional[List[dict]] = None   # 당일 '이전' 일봉 (오래된 순). None = 아직 조회 안 함
        self.daily_error = ""
        self._scratch: Dict[str, dict] = {}

    # ------------------------------------------------------------------ 봉 추가
    def add(self, bar: Bar) -> bool:
        """완성된 정규장 봉만 시간순으로 받는다. 받았으면 True"""
        problem = self._problem(bar)
        if problem:
            if problem not in _NORMAL_SKIPS:      # 진행 중인 봉·시간외 봉·다시 받은 봉은 정상 상황
                self.rejected += 1
                logger.warning("[%s %s] 봉 무시 (%s): %s O=%s H=%s L=%s C=%s V=%s", self.symbol, self.name, problem,
                               bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
            return False
        t = bar.timestamp.time()
        if t <= LAST_REGULAR_BAR_END:
            prev_end = self.bars[-1].timestamp if self.bars else (
                bar.timestamp.replace(hour=9, minute=0, second=0, microsecond=0))
            self.gaps += max(0, int((bar.timestamp - prev_end) / timedelta(minutes=BAR_MINUTES)) - 1)
        start = bar.timestamp.replace(hour=9, minute=0, second=0, microsecond=0)
        bar.bar_index = int((bar.timestamp - start) / timedelta(minutes=BAR_MINUTES)) - 1
        self.bars.append(bar)
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.cum_volume += bar.volume
        self.cum_value += typical * bar.volume
        self.vwaps.append(self.cum_value / self.cum_volume if self.cum_volume > 0 else bar.close)
        return True

    def _problem(self, bar: Bar) -> str:
        if not bar.is_completed:
            return "incomplete"
        if bar.date_str != self.date_str:
            return "other day"
        if self.bars and bar.timestamp <= self.bars[-1].timestamp:
            return "duplicate" if bar.timestamp == self.bars[-1].timestamp else "out of order"
        t = bar.timestamp.time()
        if t < FIRST_BAR_END or t > CLOSING_AUCTION_BAR_END:
            return "outside session"
        if bar.timestamp.second or bar.timestamp.minute % BAR_MINUTES:
            return "off 3-minute grid"
        if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
            return "non-positive price"
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            return "high/low inconsistent"
        if bar.volume < 0:
            return "negative volume"
        return ""

    # ------------------------------------------------------------------ 조회
    @property
    def last(self) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    @property
    def first(self) -> Optional[Bar]:
        return self.bars[0] if self.bars else None

    @property
    def vwap(self) -> float:
        return self.vwaps[-1] if self.vwaps else 0.0

    @property
    def day_high(self) -> float:
        return max((b.high for b in self.bars), default=0.0)

    @property
    def day_low(self) -> float:
        return min((b.low for b in self.bars), default=0.0)

    @property
    def has_opening_bar(self) -> bool:
        """09:00~09:03 봉이 있는가 (없으면 시가·시초 범위를 믿을 수 없다)"""
        return bool(self.bars) and self.bars[0].timestamp.time() == FIRST_BAR_END

    @property
    def prev_day(self) -> Optional[dict]:
        return self.daily[-1] if self.daily else None

    @property
    def gap_pct(self) -> Optional[float]:
        """시가 갭 % = (당일 시가 - 전일 종가) / 전일 종가. 알 수 없으면 None"""
        prev = self.prev_day
        if not prev or not self.has_opening_bar or prev["close"] <= 0:
            return None
        return (self.bars[0].open / prev["close"] - 1.0) * 100.0

    def scratch(self, owner: str) -> dict:
        """전략별 작업 공간 (전략끼리 상태를 섞지 않는다)"""
        return self._scratch.setdefault(owner, {})


@dataclass
class StrategySettings:
    """전략 공통 설정. 전략마다 이 클래스를 상속해 자기 파라미터를 더한다."""
    enabled: bool = True
    start_time: str = "09:03"
    end_time: str = "15:00"
    exit: Dict[str, float] = field(default_factory=dict)   # 공통 청산 설정 덮어쓰기 (예: {"stop_loss_rate": 1.5})
    live_enabled: bool = False    # 실거래에서 신규 진입 허용 — 검증 후 사용자가 직접 켠다 (모의거래는 enabled 만 본다)

    @classmethod
    def from_dict(cls, d: Optional[dict]):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


_EXIT_KEYS = ("stop_loss_rate", "trailing_retrace_rate", "trailing_retrace_rate_1r",
              "trailing_activation_rate", "take_profit_rate")


class BaseStrategy:
    name = ""                     # open / intraday / close
    label = ""
    patterns: Tuple[str, ...] = ()
    pattern_labels: Dict[str, str] = {}     # 패턴 이름 → 화면 표시 이름 (설정의 use_<패턴> 으로 켜고 끈다)
    settings_class = StrategySettings
    needs_daily = False           # True → 관리자가 종목별 전일까지의 일봉을 session.daily 에 채워 준다
    needs_index = False           # True → 엔진이 봉 주기마다 시장 지수를 조회해 ctx.index 로 넘긴다

    def __init__(self, settings: Optional[StrategySettings] = None):
        self.settings = settings or self.settings_class()
        self._apply_settings()

    def _apply_settings(self):
        self.start_time = parse_hhmm(self.settings.start_time)
        self.end_time = parse_hhmm(self.settings.end_time)

    def update_settings(self, settings: StrategySettings):
        self.settings = settings
        self._apply_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    def is_active(self, t: dtime) -> bool:
        """이 시각의 봉에서 신규 진입을 판정하는가"""
        return self.enabled and self.start_time <= t <= self.end_time

    # ------------------------------------------------------------------ 진입
    def on_bar(self, session: SymbolSession, ctx: Optional[MarketContext] = None) -> Optional[StrategySignal]:
        """session.last (방금 완성된 봉) 기준으로 진입 신호를 내거나 None. 주문은 내지 않는다."""
        raise NotImplementedError

    def make_signal(self, session: SymbolSession, pattern: str, reason: str, stop_price: Optional[float] = None,
                    **kw) -> StrategySignal:
        bar = session.last
        return StrategySignal(strategy=self.name, pattern=pattern, symbol=session.symbol, name=session.name,
                              action=BUY, entry_price=bar.close, timestamp=bar.timestamp,
                              reason=f"[{self.label or self.name}·{pattern}] {reason}", stop_price=stop_price, **kw)

    # ------------------------------------------------------------------ 청산
    def exit_config(self, common: StrategyConfig) -> StrategyConfig:
        """공통 청산 설정에 이 전략의 덮어쓰기를 얹은 사본"""
        over = {k: v for k, v in (self.settings.exit or {}).items() if k in _EXIT_KEYS}
        return replace(common, **over) if over else common

    def check_exit(self, position: Position, price: float, common: StrategyConfig, now: datetime,
                   trade: Any = None, **kw) -> Optional[ExitDecision]:
        """자기 포지션의 청산 판정. 기본은 공통 규칙(손절·트레일링·목표가)을 전략 설정으로 호출한다."""
        return check_exit(position, price, self.exit_config(common), **kw)
