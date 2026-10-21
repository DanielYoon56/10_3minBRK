"""
Korea3MinBreakoutTrader - 매도(청산) 규칙

[매도 조건]
1. 손절 (-2%)
   current_price <= entry_price * (1 - stop_loss_rate/100)
2. 트레일링 스탑 (진입 후 +1% 이상 상승한 뒤부터, 상승분의 15% 되돌림)
   peak      = 진입 후 최고가
   gain      = peak - entry_price
   발동 조건 : gain / entry_price ≥ trailing_activation_rate (기본 1%)
               → 최고가가 한 번이라도 +1%에 닿으면 이후 계속 적용 (최고가는 내려가지 않음)
               → +1% 미만에서는 트레일링 없음, 손절 -2%만 적용
   trail_px  = peak - gain * trailing_retrace_rate/100
   current_price <= trail_px  → 청산
   예) 진입 10,000 / 최고 11,000 → 상승분 1,000 → 15% = 150 → 10,850 이하에서 청산
   예) 진입 10,000 / 최고 10,080(+0.8%) → 미발동 → 10,000으로 내려와도 보유 유지
3. (옵션) 고정 익절 — take_profit_rate > 0 일 때만. 기본 0 = 미사용.

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


def trailing_stop_price(entry_price: float, peak_price: float, config: StrategyConfig) -> Optional[float]:
    """트레일링 청산가. 아직 발동 조건 미충족이면 None."""
    if config.trailing_retrace_rate <= 0:
        return None
    gain = peak_price - entry_price
    if gain <= 0:
        return None
    if config.trailing_activation_rate > 0 and (gain / entry_price) * 100.0 < config.trailing_activation_rate:
        return None
    return peak_price - gain * (config.trailing_retrace_rate / 100.0)


def update_peak(position: Position, current_price: float) -> None:
    if current_price > position.highest_price:
        position.highest_price = current_price


def check_exit(position: Position, current_price: float, config: StrategyConfig) -> Optional[ExitDecision]:
    """
    현재가 기준 청산 여부 판정. 호출 전에 update_peak()로 최고가를 갱신해 둘 것
    (이 함수도 안전하게 최고가를 갱신한다).
    """
    update_peak(position, current_price)
    entry = position.avg_price

    # 1. 손절
    sl = position.stop_loss_price if position.stop_loss_price > 0 else stop_loss_price(entry, config)
    if current_price <= sl:
        return ExitDecision(
            SignalType.SELL_STOP_LOSS, sl,
            f"손절 -{config.stop_loss_rate:g}% (현재가 {current_price:,.0f} <= 손절가 {sl:,.0f})",
        )

    # 2. 트레일링 스탑
    trail = trailing_stop_price(entry, position.highest_price, config)
    if trail is not None and current_price <= trail:
        gain = position.highest_price - entry
        return ExitDecision(
            SignalType.SELL_TRAILING_STOP, trail,
            f"트레일링 스탑: 최고가 {position.highest_price:,.0f} 대비 상승분 {gain:,.0f}의 "
            f"{config.trailing_retrace_rate:g}% 되돌림 (현재가 {current_price:,.0f} <= {trail:,.0f})",
        )

    # 3. 고정 익절 (옵션)
    if config.take_profit_rate > 0:
        tp = entry * (1.0 + config.take_profit_rate / 100.0)
        if current_price >= tp:
            return ExitDecision(
                SignalType.SELL_TAKE_PROFIT, tp,
                f"목표가 익절 +{config.take_profit_rate:g}% (현재가 {current_price:,.0f} >= {tp:,.0f})",
            )

    return None
