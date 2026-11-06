"""
Korea3MinBreakoutTrader - 매도(청산) 규칙

[매도 조건]
1. 손절 (-2%)
   current_price <= entry_price * (1 - stop_loss_rate/100)
2. 트레일링 스탑 (진입 후 +1% 이상 상승한 뒤부터, 최고가 구간별 되돌림)
   peak      = 진입 후 최고가
   gain      = peak - entry_price
   발동 조건 : gain / entry_price ≥ trailing_activation_rate (기본 1%)
               → 최고가가 한 번이라도 +1%에 닿으면 이후 계속 적용 (최고가는 내려가지 않음)
               → +1% 미만에서는 트레일링 없음, 손절 -2%만 적용
   1R        = entry_price - 손절가 (종목별, 전략 손절가가 있으면 그 가격 기준.
               차트에서 손절선을 옮겨도 1R 은 처음 세팅한 손절가 기준 그대로)
   되돌림    = gain < 1R 이면 trailing_retrace_rate (기본 50%)
               gain ≥ 1R 이면 trailing_retrace_rate_1r (기본 30%, 0 = 구간 구분 없음)
               → 현재가가 아닌 최고가로 판정하므로 한 번 1R에 닿으면 30%가 유지되고 청산선은 내려가지 않음
   trail_px  = peak - gain * 되돌림/100
   current_price <= trail_px  → 청산
   예) 진입 10,000 / 손절 9,800(1R=200) / 최고 10,150 → 1R 미만 → 150의 50% → 10,075 이하에서 청산
   예) 진입 10,000 / 손절 9,800(1R=200) / 최고 11,000 → 1R 이상 → 1,000의 30% → 10,700 이하에서 청산
   예) 진입 10,000 / 최고 10,080(+0.8%) → 미발동 → 10,000으로 내려와도 보유 유지
3. 목표가 익절 — 차트에서 정한 목표가가 있으면 그 가격,
   없으면 take_profit_rate > 0 일 때만 고정 % 익절. 기본 0 = 미사용.

트레일링 값(발동·되돌림)은 전략과 무관하게 공통 설정 하나만 쓴다 (A~F 전부).

우선순위: 손절 → 트레일링 → 고정 익절
"""

from dataclasses import dataclass
from typing import Optional

from python_app.models.types import Position, SignalType, StrategyConfig


@dataclass
class ExitDecision:
    signal_type: SignalType
    trigger_price: float
    reason: str


def stop_loss_price(entry_price: float, config: StrategyConfig) -> float:
    return round(entry_price * (1.0 - config.stop_loss_rate / 100.0))


def reached_1r(entry_price: float, peak_price: float, config: StrategyConfig, stop_price: float = 0.0) -> bool:
    """최고가가 진입가 + 1R 이상인가. stop_price 0 = 설정의 손절 %로 계산"""
    risk = entry_price - (stop_price if stop_price > 0 else stop_loss_price(entry_price, config))
    return risk > 0 and peak_price - entry_price >= risk


def trailing_stop_price(entry_price: float, peak_price: float, config: StrategyConfig,
                        stop_price: float = 0.0) -> Optional[float]:
    """트레일링 청산가. 아직 발동 조건 미충족이면 None."""
    if config.trailing_retrace_rate <= 0:
        return None
    gain = peak_price - entry_price
    if gain <= 0:
        return None
    if config.trailing_activation_rate > 0 and (gain / entry_price) * 100.0 < config.trailing_activation_rate:
        return None
    retrace = config.trailing_retrace_rate
    if config.trailing_retrace_rate_1r > 0 and reached_1r(entry_price, peak_price, config, stop_price):
        retrace = config.trailing_retrace_rate_1r
    return peak_price - gain * (retrace / 100.0)


def update_peak(position: Position, current_price: float) -> None:
    if current_price > position.highest_price:
        position.highest_price = current_price


def check_exit(position: Position, current_price: float, config: StrategyConfig,
               take_profit_price: float = 0.0, use_trailing: bool = True,
               stop_label: str = "", risk_stop: float = 0.0) -> Optional[ExitDecision]:
    """
    현재가 기준 청산 여부 판정. 호출 전에 update_peak()로 최고가를 갱신해 둘 것
    (이 함수도 안전하게 최고가를 갱신한다).
    종목별 값: position.stop_loss_price = 전략 손절가(SMC), take_profit_price = 차트에서 정한 목표가,
    use_trailing=False 면 트레일링 미적용, stop_label = 손절 사유 문구.
    risk_stop = 1R 계산에 쓸 처음 손절가 (0 = 지금 손절가, 손절가를 수동으로 옮긴 경우에만 준다).
    """
    update_peak(position, current_price)
    entry = position.avg_price

    # 1. 손절
    sl = position.stop_loss_price if position.stop_loss_price > 0 else stop_loss_price(entry, config)
    if current_price <= sl:
        label = stop_label or f"-{config.stop_loss_rate:g}%"
        return ExitDecision(
            SignalType.SELL_STOP_LOSS, sl,
            f"손절 {label} (현재가 {current_price:,.0f} <= 손절가 {sl:,.0f})",
        )

    # 2. 트레일링 스탑
    risk = risk_stop or sl
    trail = trailing_stop_price(entry, position.highest_price, config, risk) if use_trailing else None
    if trail is not None and current_price <= trail:
        gain = position.highest_price - entry
        retrace = (position.highest_price - trail) / gain * 100.0
        zone = "1R 이상" if reached_1r(entry, position.highest_price, config, risk) else "1R 미만"
        return ExitDecision(
            SignalType.SELL_TRAILING_STOP, trail,
            f"트레일링 스탑: 최고가 {position.highest_price:,.0f} 대비 상승분 {gain:,.0f}의 "
            f"{retrace:.3g}% 되돌림 [{zone}] (현재가 {current_price:,.0f} <= {trail:,.0f})",
        )

    # 3. 목표가 익절 — 차트에서 정한 목표가 우선, 없으면 고정 익절 % (옵션)
    if take_profit_price > 0:
        if current_price >= take_profit_price:
            return ExitDecision(
                SignalType.SELL_TAKE_PROFIT, take_profit_price,
                f"목표가 익절 (현재가 {current_price:,.0f} >= 목표가 {take_profit_price:,.0f})",
            )
    elif config.take_profit_rate > 0:
        tp = entry * (1.0 + config.take_profit_rate / 100.0)
        if current_price >= tp:
            return ExitDecision(
                SignalType.SELL_TAKE_PROFIT, tp,
                f"목표가 익절 +{config.take_profit_rate:g}% (현재가 {current_price:,.0f} >= {tp:,.0f})",
            )

    return None
