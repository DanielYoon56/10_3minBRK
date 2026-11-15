"""테스트용 전략 — 엔진·관리자의 동작(주문, 청산, 이월 등)을 실제 전략 없이 검증하기 위한 최소 구현"""
from typing import Optional

from python_app.strategies.base_strategy import BaseStrategy, StrategySettings


class OpenCrossStub(BaseStrategy):
    """3분봉 종가가 첫 봉 시가를 상향 돌파 + 거래량이 첫 봉의 70% 초과 → 매수 신호"""
    name = "stub"
    label = "스텁"
    patterns = ("cross",)

    def __init__(self, settings: Optional[StrategySettings] = None, stop_pct: float = 0.0,
                 hold_overnight: bool = False, name: str = ""):
        super().__init__(settings or StrategySettings(start_time="09:03", end_time="10:00"))
        self.stop_pct = stop_pct
        self.hold_overnight = hold_overnight
        if name:
            self.name = name

    def on_bar(self, session, ctx=None):
        if len(session.bars) < 2 or not session.has_opening_bar:
            return None
        first, prev, bar = session.first, session.bars[-2], session.last
        if not (prev.close <= first.open < bar.close) or bar.volume <= first.volume * 0.7:
            return None
        stop = round(bar.close * (1 - self.stop_pct / 100)) if self.stop_pct else None
        return self.make_signal(session, "cross", f"종가 {bar.close:,.0f}원이 시가 {first.open:,.0f}원 상향 돌파",
                                stop_price=stop, hold_overnight=self.hold_overnight)
