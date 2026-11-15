"""
3WAYTRADING - 중앙 자금 보호 (CapitalGuard)

전략이 낸 BUY 신호는 주문이 아니다. 모든 신호는 여기서 승인과 수량을 받아야 주문이 된다.
실거래 엔진과 백테스트 재생이 같은 이 코드를 쓴다.

[진입 차단 — 하나라도 해당하면 주문하지 않는다]
   비상 정지 · 장 마감 임박(15:20 이후) · 장 마감 청산 시각 이후(오버나이트 신호 제외) ·
   데이터 지연(후보 갱신 실패) · API 연속 실패 · 체결 여부가 불확실한 주문 존재 ·
   일 손실한도 · 주간 손실한도 · 당일 연속 손실 한도 · 전략별 당일 손실 한도 ·
   이미 보유/주문 중(중복 주문 방지) · 당일 재진입 금지 · 지정가 유효시각 경과 ·
   최대 동시 보유 · 전략별 최대 보유 · 오버나이트 최대 보유

[수량 — 아래 중 가장 작은 값]
   위험 기준   = 1회 허용 손실 ÷ 1주당 예상 손실
                 1주당 예상 손실 = (진입가 - 손절가) + 매수 수수료 + 매도 수수료·거래세 + 슬리피지(매수·매도 각 N호가)
   종목 한도   = 1회 매수 금액 × 신호의 비중(position_size_ratio, 최대 1)
   현금        = 주문가능금액
   노출 한도   = 총 매입금액 상한 - 현재 매입금액  (오버나이트 신호는 오버나이트 상한도 함께)

손실 한도에 걸리면 '신규 매수'만 막는다. 보유 종목의 손절·청산은 계속된다 (청산은 이 모듈이 막지 않는다).
물타기·손실 후 증액은 없다: 수량은 직전 손익과 무관하게 위 식으로만 정한다.
"""

from dataclasses import dataclass, field, fields
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Set

from python_app.models.types import StrategyConfig
from python_app.strategies.indicators import tick_size

LAST_ORDER_TIME = dtime(15, 20)
FEE_RATE = 0.00015
TAX_RATE = 0.0020


@dataclass
class RiskConfig:
    """0 으로 둔 한도는 공통 설정에서 계산한다. 기본값은 검증 전 임시값이다."""
    max_loss_per_trade: int = 0           # 1회 허용 손실(원). 0 = 1회 매수 금액 × 손절 %
    max_exposure: int = 0                 # 앱 매수 종목 총 매입금액 상한. 0 = 1회 매수 금액 × 최대 동시 보유
    max_weekly_loss: int = 0              # 주간(월~금) 누적 실현손실 한도. 0 = 일 손실한도 × 3
    max_consecutive_losses: int = 5       # 당일 연속 손실 N회 → 그날 신규 매수 중단 (0 = 사용 안 함)
    strategy_max_positions: Dict[str, int] = field(default_factory=dict)     # 전략별 최대 동시 보유 (예: {"open": 2})
    strategy_max_daily_loss: Dict[str, int] = field(default_factory=dict)    # 전략별 당일 실현손실 한도(원)
    overnight_max_positions: int = 1      # 오버나이트 보유(CLOSE) 최대 종목 수
    overnight_max_exposure: int = 0       # 오버나이트 총 매입금액 상한. 0 = 1회 매수 금액
    api_fail_limit: int = 5               # 시세 조회 연속 실패 N회 → 신규 매수 중단
    slippage_ticks: int = 1               # 수량 계산에 넣는 편도 슬리피지 (호가 수)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RiskConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class AccountState:
    """승인 판단에 필요한 계좌·주문 상태 (엔진/재생이 채워서 넘긴다)"""
    cash: float = 0.0
    active: Dict[str, str] = field(default_factory=dict)        # 앱이 주문·보유 중인 종목 → 소유 전략
    overnight: Set[str] = field(default_factory=set)            # 그중 오버나이트 보유 종목
    exposure: float = 0.0                                       # 앱 매수 종목 총 매입금액 (주문 중 포함)
    overnight_exposure: float = 0.0
    account_symbols: Set[str] = field(default_factory=set)      # 계좌에 있는 모든 종목 (앱이 사지 않은 것 포함)
    closed_today: Set[str] = field(default_factory=set)
    unrealized: float = 0.0                                     # 앱 매수 종목 평가손익 합
    data_problem: str = ""                                      # 데이터 지연 등 (빈 문자열 = 정상)
    api_fail_streak: int = 0
    uncertain_orders: List[str] = field(default_factory=list)   # 체결 여부를 확인하지 못한 주문의 종목명


@dataclass
class Decision:
    approved: bool
    qty: int = 0
    reason: str = ""          # 거부 사유 또는 수량을 정한 한도
    stop_price: float = 0.0   # 수량 계산에 쓴 손절가
    risk_amount: float = 0.0  # 이 수량의 예상 최대 손실(비용 포함, 원)


class CapitalGuard:
    def __init__(self, common: StrategyConfig, risk: Optional[RiskConfig] = None,
                 fee_rate: float = FEE_RATE, tax_rate: float = TAX_RATE):
        self.common = common
        self.risk = risk or RiskConfig()
        self.fee_rate, self.tax_rate = fee_rate, tax_rate
        self.emergency = ""                      # 비상 정지 사유 (빈 문자열 = 정상)
        self.day = ""                            # YYYYMMDD
        self.week = ""                           # ISO 주 (YYYY-Www)
        self.realized_today = 0.0
        self.realized_week = 0.0
        self.realized_by_strategy: Dict[str, float] = {}
        self.loss_streak = 0
        self.halted_reason = ""                  # 오늘 신규 매수를 멈춘 사유 (손실 한도) — 하루 동안 유지

    # ================================================================ 한도 (0 = 공통 설정에서 계산)
    @property
    def loss_per_trade(self) -> float:
        return float(self.risk.max_loss_per_trade or
                     self.common.order_amount_per_trade * self.common.stop_loss_rate / 100.0)

    @property
    def exposure_limit(self) -> float:
        return float(self.risk.max_exposure or
                     self.common.order_amount_per_trade * self.common.max_concurrent_positions)

    @property
    def weekly_loss_limit(self) -> float:
        return float(self.risk.max_weekly_loss or abs(self.common.max_daily_loss) * 3)

    @property
    def overnight_exposure_limit(self) -> float:
        return float(self.risk.overnight_max_exposure or self.common.order_amount_per_trade)

    # ================================================================ 날짜·손익 기록
    @staticmethod
    def _week_of(now: datetime) -> str:
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"

    def roll(self, now: datetime):
        """날짜·주가 바뀌면 당일/주간 누계를 새로 시작한다"""
        day, week = now.strftime("%Y%m%d"), self._week_of(now)
        if week != self.week:
            self.week, self.realized_week = week, 0.0
        if day != self.day:
            self.day, self.realized_today, self.realized_by_strategy = day, 0.0, {}
            self.loss_streak, self.halted_reason = 0, ""

    def record_close(self, strategy: str, pnl: float, now: datetime):
        """청산 결과 반영 (실현손익 누계, 연속 손실)"""
        self.roll(now)
        key = (strategy or "").split(".")[0]
        self.realized_today += pnl
        self.realized_week += pnl
        self.realized_by_strategy[key] = self.realized_by_strategy.get(key, 0.0) + pnl
        if pnl < 0:
            self.loss_streak += 1
        elif pnl > 0:
            self.loss_streak = 0

    def stop_new_entries(self, reason: str):
        """비상 정지 — 신규 매수만 막는다 (보유 종목 청산과는 별개)"""
        self.emergency = reason or "비상 정지"

    def resume(self):
        self.emergency = ""

    # ================================================================ 상태 저장 (재시작해도 누계 유지)
    def to_dict(self) -> Dict[str, Any]:
        return {"day": self.day, "week": self.week, "realized_today": self.realized_today,
                "realized_week": self.realized_week, "realized_by_strategy": dict(self.realized_by_strategy),
                "loss_streak": self.loss_streak, "halted_reason": self.halted_reason, "emergency": self.emergency}

    def load_dict(self, d: Optional[dict]):
        d = d or {}
        self.day, self.week = str(d.get("day", "")), str(d.get("week", ""))
        self.realized_today = float(d.get("realized_today", 0.0))
        self.realized_week = float(d.get("realized_week", 0.0))
        self.realized_by_strategy = {k: float(v) for k, v in (d.get("realized_by_strategy") or {}).items()}
        self.loss_streak = int(d.get("loss_streak", 0))
        self.halted_reason = str(d.get("halted_reason", ""))
        self.emergency = str(d.get("emergency", ""))

    # ================================================================ 손실 한도
    def _loss_halt(self, strategy: str, unrealized: float) -> str:
        """오늘 신규 매수를 멈춰야 하는 사유. 한 번 걸리면 그날은 유지된다"""
        if self.halted_reason:
            return self.halted_reason
        reason = ""
        day_pnl = self.realized_today + unrealized
        if day_pnl <= -abs(self.common.max_daily_loss):
            reason = f"일 손실한도 도달 ({day_pnl:+,.0f}원 ≤ -{abs(self.common.max_daily_loss):,}원) → 신규 매수 중단"
        elif self.realized_week + unrealized <= -self.weekly_loss_limit:
            reason = (f"주간 손실한도 도달 ({self.realized_week + unrealized:+,.0f}원 ≤ "
                      f"-{self.weekly_loss_limit:,.0f}원) → 신규 매수 중단")
        elif self.risk.max_consecutive_losses > 0 and self.loss_streak >= self.risk.max_consecutive_losses:
            reason = f"당일 연속 손실 {self.loss_streak}회 → 신규 매수 중단"
        if reason:
            self.halted_reason = reason
        return reason

    def _strategy_loss_halt(self, strategy: str) -> str:
        limit = self.risk.strategy_max_daily_loss.get(strategy, 0)
        lost = self.realized_by_strategy.get(strategy, 0.0)
        if limit and lost <= -abs(limit):
            return f"전략 {strategy} 당일 손실한도 도달 ({lost:+,.0f}원 ≤ -{abs(limit):,}원)"
        return ""

    # ================================================================ 수량
    def stop_for(self, sig) -> float:
        return float(sig.stop_price) if sig.stop_price else round(
            sig.entry_price * (1.0 - self.common.stop_loss_rate / 100.0))

    def risk_per_share(self, entry: float, stop: float) -> float:
        """1주를 진입가에 사서 손절가에 팔 때의 예상 손실 (수수료·세금·슬리피지 포함)"""
        slip = self.risk.slippage_ticks * (tick_size(entry) + tick_size(stop))
        return (entry - stop) + entry * self.fee_rate + stop * (self.fee_rate + self.tax_rate) + slip

    def size(self, sig, state: AccountState) -> Decision:
        entry, stop = float(sig.entry_price), self.stop_for(sig)
        per_share = self.risk_per_share(entry, stop)
        cost = entry * (1.0 + self.fee_rate)
        ratio = min(sig.position_size_ratio, 1.0) if sig.position_size_ratio and sig.position_size_ratio > 0 else 1.0
        room = self.exposure_limit - state.exposure
        if sig.hold_overnight:
            room = min(room, self.overnight_exposure_limit - state.overnight_exposure)
        limits = {
            f"1회 허용 손실 {self.loss_per_trade:,.0f}원": int(self.loss_per_trade // per_share) if per_share > 0 else 0,
            f"종목 한도 {self.common.order_amount_per_trade * ratio:,.0f}원": int(self.common.order_amount_per_trade * ratio // cost),
            f"주문가능금액 {state.cash:,.0f}원": int(state.cash // cost),
            f"노출 한도 여유 {max(room, 0):,.0f}원": int(max(room, 0) // cost),
        }
        binding = min(limits, key=limits.get)
        qty = limits[binding]
        if qty < 1:
            reason = "주문가능금액 부족" if binding.startswith("주문가능금액") else f"수량 0 ({binding})"
            return Decision(False, 0, f"{reason} ({state.cash:,.0f}원)" if reason == "주문가능금액 부족" else reason, stop)
        return Decision(True, qty, binding, stop, qty * per_share)

    # ================================================================ 승인
    def evaluate(self, sig, state: AccountState, now: datetime) -> Decision:
        self.roll(now)
        sym, strategy = sig.symbol, (sig.strategy or "").split(".")[0]
        refuse = lambda reason: Decision(False, 0, reason)
        if self.emergency:
            return refuse(f"비상 정지 ({self.emergency})")
        if now.time() > LAST_ORDER_TIME:
            return refuse("장 마감 임박")
        eod = datetime.strptime(self.common.eod_liquidation_time, "%H:%M").time()
        if self.common.eod_liquidation and now.time() >= eod and not sig.hold_overnight:
            return refuse(f"장 마감 청산 시각({self.common.eod_liquidation_time}) 이후")
        if state.data_problem:
            return refuse(f"데이터 문제 ({state.data_problem})")
        if self.risk.api_fail_limit > 0 and state.api_fail_streak >= self.risk.api_fail_limit:
            return refuse(f"시세 조회 연속 실패 {state.api_fail_streak}회")
        if state.uncertain_orders:
            return refuse(f"체결 확인이 안 된 주문 있음 ({', '.join(state.uncertain_orders)})")
        halt = self._loss_halt(strategy, state.unrealized) or self._strategy_loss_halt(strategy)
        if halt:
            return refuse(halt)
        if sym in state.active:
            return refuse("이미 진행 중인 주문/보유")
        if sym in state.account_symbols:
            return refuse("이미 계좌에 보유 중인 종목")
        if sym in state.closed_today and not self.common.allow_reentry_same_day:
            return refuse("당일 이미 매매한 종목 (재진입 금지)")
        if sig.expire_at and now >= sig.expire_at:
            return refuse("지정가 유효시각 경과")
        if len(state.active) >= self.common.max_concurrent_positions:
            return refuse(f"최대 동시 보유 {self.common.max_concurrent_positions}종목 도달")
        cap = self.risk.strategy_max_positions.get(strategy, 0)
        if cap and sum(1 for s in state.active.values() if (s or "").split(".")[0] == strategy) >= cap:
            return refuse(f"전략 {strategy} 최대 보유 {cap}종목 도달")
        if sig.hold_overnight and len(state.overnight) >= self.risk.overnight_max_positions:
            return refuse(f"오버나이트 최대 보유 {self.risk.overnight_max_positions}종목 도달")
        return self.size(sig, state)
