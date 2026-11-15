"""
3WAYTRADING - 전략들이 함께 쓰는 진입 금지·위험 계산 (상태 없음)

각 전략의 진입 판정은 전략 파일에 있다. 여기에는 '어느 전략이든 같은 뜻으로 써야 하는' 계산만 둔다:
손절가 산정, 저항까지의 여유, 지수 조건, 확인 항목 충족 비율.
"""

from typing import List, Optional, Tuple

from python_app.strategies.base_strategy import MarketContext, SymbolSession
from python_app.strategies.indicators import floor_to_tick, tick_size


def stop_and_room(entry: float, stop_ref: float, prev_day: dict, min_stop_pct: float, max_stop_pct: float,
                  min_rr: float, upper_limit_pct: float) -> Tuple[float, str]:
    """(손절가, 진입 금지 사유). 사유가 비어 있으면 통과.
    손절가 = 손절 기준 - 1호가. 손절폭이 min_stop_pct% 보다 좁으면 그만큼 넓히고, max_stop_pct% 보다 넓으면 금지.
    가까운 저항(진입가 위의 전일 고가, 상한가)까지의 여유가 손절폭 × min_rr 보다 작으면 금지."""
    stop = stop_ref - tick_size(stop_ref)
    stop_pct = (entry - stop) / entry * 100.0
    if stop_pct > max_stop_pct:
        return stop, f"손절폭 {stop_pct:.1f}% > {max_stop_pct:g}%"
    if stop_pct < min_stop_pct:
        stop = floor_to_tick(entry * (1.0 - min_stop_pct / 100.0))
        stop_pct = (entry - stop) / entry * 100.0
        if stop_pct > max_stop_pct:
            return stop, f"손절폭 {stop_pct:.1f}% > {max_stop_pct:g}%"
    risk = entry - stop
    if risk <= 0:
        return stop, "손절가를 정할 수 없음"
    resistances: List[float] = [prev_day["close"] * (1.0 + upper_limit_pct / 100.0)]
    if prev_day["high"] > entry:
        resistances.append(prev_day["high"])
    room = min(resistances) - entry
    if room < risk * min_rr:
        return stop, f"저항 {min(resistances):,.0f}원까지 여유 {room:,.0f}원 < 손절폭 {risk:,.0f}원 × {min_rr:g}"
    return stop, ""


def index_change(ctx: Optional[MarketContext]) -> Optional[float]:
    return (ctx.index or {}).get("change_rate") if ctx else None


def index_block(day_change_pct: float, ctx: Optional[MarketContext], min_index_change_pct: float,
                min_rel_strength_pct: float) -> str:
    """지수 약세·상대강도 부족 사유. 지수 정보가 없으면 판정하지 않는다 (빈 문자열)"""
    change = index_change(ctx)
    if change is None:
        return ""
    if change < min_index_change_pct:
        return f"지수 {change:+.2f}% (시장 약세)"
    if day_change_pct - change < min_rel_strength_pct:
        return f"지수 대비 상대강도 {day_change_pct - change:+.1f}%p 부족"
    return ""


def overheat_block(s: SymbolSession, day_change_pct: float, max_day_change_pct: float, max_vwap_ext_pct: float) -> str:
    if day_change_pct > max_day_change_pct:
        return f"전일 대비 +{day_change_pct:.1f}% (과열)"
    ext = (s.last.close / s.vwap - 1.0) * 100.0 if s.vwap > 0 else 0.0
    if ext > max_vwap_ext_pct:
        return f"VWAP 이격 +{ext:.1f}% (과열)"
    return ""


def confirmation_ratio(s: SymbolSession, ctx: Optional[MarketContext], extra: Tuple[bool, ...] = ()) -> float:
    """확인 항목 충족 비율 (우선순위 참고용 — 검증된 확률이 아니다)"""
    bar = s.last
    checks = list(extra) + [bar.high > bar.low and (bar.close - bar.low) / (bar.high - bar.low) >= 0.75]
    change = index_change(ctx)
    if change is not None:
        checks.append(change >= 0)
    if ctx and ctx.candidate is not None:
        checks.append(ctx.candidate.rank <= 10)
    return sum(checks) / len(checks)
