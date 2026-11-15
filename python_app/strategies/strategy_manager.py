"""
3WAYTRADING - 전략 관리자

하는 일
  - 완성된 3분봉을 종목별 세션(SymbolSession)에 한 번만 쌓는다
  - 그 시각에 활성인 전략을 각각 따로 호출해 신호를 모은다 (전략끼리 서로의 판정에 관여하지 않는다)
  - 같은 전략이 같은 종목에 하루 두 번 신호를 내지 못하게 막는다
  - 전략별 통계(신호 수·청산 건수·승패·손익)를 따로 쌓는다

하지 않는 일
  - 진입 조건 판정 (각 전략의 on_bar 가 한다)
  - 주문·수량 결정 (엔진과 자금 보호가 한다)
"""

import logging
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Callable, Dict, Iterable, List, Optional

from python_app.models.types import Bar
from python_app.strategies.base_strategy import BUY, BaseStrategy, MarketContext, StrategySignal, SymbolSession
from python_app.strategies.close_strategy import CloseStrategy
from python_app.strategies.open_strategy import OpenStrategy

logger = logging.getLogger("3WAYTRADING.Manager")

# 전략 이름 → 클래스. 등록 순서 = 같은 봉에서의 우선순위 (장중 INTRADAY 폐기: open, close).
STRATEGY_CLASSES: Dict[str, type] = {"open": OpenStrategy, "close": CloseStrategy}


@dataclass
class StrategyStats:
    signals: int = 0
    by_pattern: Dict[str, int] = field(default_factory=dict)
    errors: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0


class StrategyManager:
    def __init__(self, strategies: Iterable[BaseStrategy] = (),
                 daily_provider: Optional[Callable[[str, str], List[dict]]] = None):
        self.strategies: Dict[str, BaseStrategy] = {}
        for s in strategies:
            if not s.name or s.name in self.strategies:
                raise ValueError(f"전략 이름이 비었거나 겹칩니다: {s.name!r}")
            self.strategies[s.name] = s
        self.daily_provider = daily_provider     # (종목코드, 'YYYYMMDD') → 그 날짜 '이전' 일봉 (오래된 순)
        self.live = False                        # 실거래 여부 (엔진이 시작할 때 정한다)
        self.sessions: Dict[str, SymbolSession] = {}
        self.stats: Dict[str, StrategyStats] = {name: StrategyStats() for name in self.strategies}
        self._fired: Dict[tuple, str] = {}       # (전략, 종목) → 신호를 낸 날짜

    # ------------------------------------------------------------------ 조회
    def strategy(self, name: str) -> Optional[BaseStrategy]:
        """'open' 또는 'open.orb' 형태 모두 받는다"""
        return self.strategies.get((name or "").split(".")[0])

    def tradable(self, strat: BaseStrategy) -> bool:
        """신규 진입을 판정할 전략인가. 실거래에서는 live_enabled 를 직접 켠 전략만 (검증 전 전략의 실주문 방지)"""
        return strat.enabled and (not self.live or bool(strat.settings.live_enabled))

    def enabled(self) -> List[BaseStrategy]:
        return [s for s in self.strategies.values() if self.tradable(s)]

    def needs_index(self) -> bool:
        return any(s.needs_index for s in self.enabled())

    def describe(self) -> str:
        on = self.enabled()
        held = [s.label or s.name for s in self.strategies.values() if s.enabled and not self.tradable(s)]
        note = f" (실거래 미승인으로 제외: {', '.join(held)})" if held else ""
        if not on:
            return "활성 전략 없음 (신규 매수 없음)" + note
        return " · ".join(f"{s.label or s.name} {s.settings.start_time}~{s.settings.end_time}" for s in on) + note

    def last_entry_time(self) -> Optional[dtime]:
        ends = [s.end_time for s in self.enabled()]
        return max(ends) if ends else None

    def fired_today(self, symbol: str) -> List[str]:
        s = self.sessions.get(symbol)
        if not s:
            return []
        return [name for (name, sym), day in self._fired.items() if sym == symbol and day == s.date_str]

    # ------------------------------------------------------------------ 세션
    def session_for(self, symbol: str, name: str, date_str: str) -> SymbolSession:
        s = self.sessions.get(symbol)
        if s is None or s.date_str != date_str:
            s = self.sessions[symbol] = SymbolSession(symbol, name, date_str)
        elif name and not s.name:
            s.name = name
        return s

    def _ensure_daily(self, session: SymbolSession):
        if session.daily is not None or not self.daily_provider:
            return
        try:
            session.daily = list(self.daily_provider(session.symbol, session.date_str.replace("-", "")))
        except Exception as e:
            session.daily = []
            session.daily_error = str(e)
            logger.warning("[%s %s] 일봉 조회 실패 → 일봉이 필요한 전략은 오늘 이 종목을 판정하지 않음: %s",
                           session.symbol, session.name, e)

    # ------------------------------------------------------------------ 판정
    def on_bar(self, bar: Bar, name: str = "", ctx: Optional[MarketContext] = None) -> List[StrategySignal]:
        """완성 봉 하나를 받아 이번 봉에서 나온 BUY 신호 목록을 돌려준다 (전략 등록 순)"""
        session = self.session_for(bar.symbol, name, bar.date_str)
        if not session.add(bar):
            return []
        out: List[StrategySignal] = []
        t = bar.timestamp.time()
        for strat in self.strategies.values():
            if not self.tradable(strat) or not strat.is_active(t):
                continue
            if self._fired.get((strat.name, bar.symbol)) == session.date_str:
                continue
            if strat.needs_daily:
                self._ensure_daily(session)
            stats = self.stats[strat.name]
            try:
                sig = strat.on_bar(session, ctx)
            except Exception:
                stats.errors += 1
                logger.exception("[%s %s] 전략 '%s' 판정 오류 → 이 봉은 건너뜀", bar.symbol, session.name, strat.name)
                continue
            if sig is None or sig.action != BUY:
                continue
            problem = self._invalid(sig)
            if problem:
                stats.errors += 1
                logger.error("[%s %s] 전략 '%s' 신호 무효 (%s) → 버림", bar.symbol, session.name, strat.name, problem)
                continue
            self._fired[(strat.name, bar.symbol)] = session.date_str
            stats.signals += 1
            stats.by_pattern[sig.pattern] = stats.by_pattern.get(sig.pattern, 0) + 1
            out.append(sig)
        return out

    @staticmethod
    def _invalid(sig: StrategySignal) -> str:
        if not sig.entry_price or sig.entry_price <= 0:
            return "진입가 없음"
        if sig.stop_price is not None and not 0 < sig.stop_price < sig.entry_price:
            return f"손절가 {sig.stop_price} 가 진입가 {sig.entry_price} 아래가 아님"
        if sig.target_price is not None and sig.target_price <= sig.entry_price:
            return f"목표가 {sig.target_price} 가 진입가 {sig.entry_price} 위가 아님"
        return ""

    # ------------------------------------------------------------------ 통계
    def record_trade(self, strategy: str, pnl: float):
        """청산 결과를 소유 전략 통계에 반영"""
        strat = self.strategy(strategy)
        if not strat:
            return
        st = self.stats[strat.name]
        st.trades += 1
        st.pnl += pnl
        if pnl > 0:
            st.wins += 1
        elif pnl < 0:
            st.losses += 1

    def stats_snapshot(self) -> Dict[str, dict]:
        return {name: {"label": self.strategies[name].label or name, "enabled": self.strategies[name].enabled,
                       "signals": st.signals, "by_pattern": dict(st.by_pattern), "errors": st.errors,
                       "trades": st.trades, "wins": st.wins, "losses": st.losses, "pnl": st.pnl}
                for name, st in self.stats.items()}


def build_manager(cfg: dict, daily_provider=None) -> StrategyManager:
    """config.json 의 strategies 블록으로 등록된 전략을 만든다 (설정에 없으면 기본값)"""
    blocks = cfg.get("strategies") or {}
    strategies = [cls(cls.settings_class.from_dict(blocks.get(name))) for name, cls in STRATEGY_CLASSES.items()]
    return StrategyManager(strategies, daily_provider=daily_provider)
