"""
Korea3MinBreakoutTrader - 매수 전략 조합 (AND / OR)

선택 가능한 전략 (StrategyConfig.entry_strategies, 아래 순서가 우선순위)
  open_breakout  첫 3분봉 시가 돌파 (기존 기본 전략, strategy/breakout.py)
  orb_high       A. 첫 3분봉 고가 돌파 (strategy/intraday.py)
  vwap_pullback  B. VWAP 눌림 반등
  prev_high      C. 전일 고가 돌파
  day_swing_high D. 당일 전고점 돌파 (자체 손절·트레일링)
  smc_fvg        SMC CHoCH → 첫 FVG 중간값 지정가 (strategy/smc.py)

조합 (StrategyConfig.entry_logic)
  OR  : 선택한 전략 중 하나라도 신호 → 그 신호로 진입 (같은 봉에 여럿이면 위 우선순위)
  AND : 선택한 전략이 '당일' 모두 한 번 이상 신호를 낸 시점(마지막 신호가 나온 봉)에 진입.
        진입 방식·손절·목표는 마지막(이번 봉) 신호의 것을 쓴다.
        예) A + SMC: A 가 09:20 에 나고 SMC 셋업이 10:40 에 나오면 10:40 에 FVG 중간값 지정가 진입.
        전략마다 진입 시간대가 다르므로 AND 는 신호가 크게 줄어든다.

모든 전략은 매 봉을 계속 받는다 (각자 하루 1회 신호). 종목당 실제 진입 1회 제한은 엔진이 한다.
"""

import logging
from dataclasses import replace
from datetime import time as dtime
from typing import Callable, Dict, List, Optional

from python_app.models.types import Bar, Signal, StrategyConfig
from python_app.strategy.breakout import First3MinuteCandleBreakout
from python_app.strategy.intraday import DaySwingHighBreakout, OrbHighBreakout, PrevHighBreakout, VwapPullback
from python_app.strategy.smc import SmcChochFvg

logger = logging.getLogger("Korea3MinBreakoutTrader.Strategy")

STRATEGY_LABELS: Dict[str, str] = {
    "open_breakout": "첫 봉 시가 돌파",
    "orb_high": "A. 첫 봉 고가 돌파",
    "vwap_pullback": "B. VWAP 눌림 반등",
    "prev_high": "C. 전일 고가 돌파",
    "day_swing_high": "D. 당일 전고점 돌파",
    "smc_fvg": "SMC CHoCH→FVG",
}
STRATEGY_KEYS: List[str] = list(STRATEGY_LABELS)


def validate_entry_config(config: StrategyConfig):
    unknown = [k for k in config.entry_strategies if k not in STRATEGY_LABELS]
    if unknown:
        raise ValueError(f"알 수 없는 매수 전략: {unknown} (가능: {STRATEGY_KEYS})")
    if str(config.entry_logic).upper() not in ("AND", "OR"):
        raise ValueError(f"entry_logic은 'AND' 또는 'OR'이어야 합니다: {config.entry_logic!r}")


class EntryCombiner:
    def __init__(self, config: StrategyConfig,
                 prev_high_provider: Optional[Callable[[str, str], Optional[float]]] = None):
        validate_entry_config(config)
        self.config = config
        self.breakout = First3MinuteCandleBreakout(config)
        self.strategies = {
            "open_breakout": self.breakout,
            "orb_high": OrbHighBreakout(config),
            "vwap_pullback": VwapPullback(config),
            "prev_high": PrevHighBreakout(config, prev_high_provider),
            "day_swing_high": DaySwingHighBreakout(config),
            "smc_fvg": SmcChochFvg(config),
        }
        self._fired: Dict[str, Dict[str, Signal]] = {}   # symbol → 전략키 → 당일 첫 신호
        self._fired_date: Dict[str, str] = {}

    # ------------------------------------------------------------------ 설정
    @property
    def symbol_states(self):
        """GUI 스냅샷용 — 첫 봉 시가/거래량 등 (시가 돌파 전략 상태)"""
        return self.breakout.symbol_states

    @property
    def logic(self) -> str:
        return str(self.config.entry_logic).upper()

    def active_keys(self) -> List[str]:
        return [k for k in STRATEGY_KEYS if k in self.config.entry_strategies]

    def update_config(self, config: StrategyConfig):
        validate_entry_config(config)
        self.config = config
        for s in self.strategies.values():
            s.update_config(config)

    def last_entry_time(self) -> dtime:
        """선택한 전략 중 가장 늦은 진입 마감 시각 (엔진이 이 시각까지 분봉을 조회)"""
        ends = [self.strategies[k].end_time for k in self.active_keys()] or [self.breakout.end_time]
        return max(ends)

    def describe(self) -> str:
        keys = self.active_keys()
        if not keys:
            return "매수 전략 없음"
        return f" {self.logic} ".join(STRATEGY_LABELS[k] for k in keys)

    # ------------------------------------------------------------------ 판정
    def process_bar(self, bar: Bar, name: str = "") -> Optional[Signal]:
        keys = self.active_keys()
        sigs: Dict[str, Signal] = {}
        for key, strat in self.strategies.items():
            # 시가 돌파 전략은 GUI 표시용 기준값 때문에 선택 안 해도 항상 봉을 넣는다 (신호는 무시)
            if key in keys or key == "open_breakout":
                sig = strat.process_bar(bar, name)
                if sig and key in keys:
                    sigs[key] = sig
        if not keys or not sigs:
            return None

        sym = bar.symbol
        if self._fired_date.get(sym) != bar.date_str:
            self._fired_date[sym] = bar.date_str
            self._fired[sym] = {}
        fired = self._fired[sym]
        for k, s in sigs.items():
            fired.setdefault(k, s)

        trigger_key = next(k for k in keys if k in sigs)
        trigger = sigs[trigger_key]
        if self.logic == "OR" or len(keys) == 1:
            return trigger

        missing = [k for k in keys if k not in fired]
        if missing:
            logger.info(f"[{sym} {trigger.name}] AND 조합 대기: {STRATEGY_LABELS[trigger_key]} 충족, "
                        f"남은 조건 {', '.join(STRATEGY_LABELS[k] for k in missing)}")
            return None
        parts = [f"{STRATEGY_LABELS[k]}@{fired[k].timestamp:%H:%M}" for k in keys]
        return replace(trigger, strategy="+".join(keys),
                       reason=f"[AND 충족: {' · '.join(parts)}] {trigger.reason}")
