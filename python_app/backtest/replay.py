"""
3WAYTRADING - 백테스트 재생 엔진

저장된 완성 3분봉을 시간순으로 다시 흘려, 실거래와 '같은' 코드로 판정한다.
  같은 코드  : SymbolSession · StrategyManager · 각 전략(on_bar / check_exit) · 후보 순위(rank_candidates) ·
               청산 규칙 · 자금 보호(CapitalGuard: 진입 승인·수량)
  재생 전용  : 체결 가정뿐

[미래 정보 차단]
  - 시각 t 의 판정에는 마감 시각 ≤ t 인 봉만 쓴다. 후보 순위도 그 시각까지의 누적값으로 계산한다.
  - 신호가 난 봉에서는 체결하지 않는다. 그 종목의 '다음 봉 시가'에 체결 (실거래도 봉 마감 몇 초 뒤 시장가).
  - 전일 값은 그 날짜 이전 일봉만 쓴다.

[체결 가정]
  - 매수 = 다음 봉 시가 + slippage_ticks 호가, 매도 = 체결 기준가 - slippage_ticks 호가
  - 봉 안의 가격 순서는 알 수 없다 → 불리한 쪽으로: 시가 → 저가 → 고가 → 종가 순으로 청산 규칙을 적용한다
    (한 봉에 손절과 목표가가 모두 닿으면 손절). 시가가 손절가 아래로 열리면 시가에 체결.
  - 수수료(매수·매도)와 거래세(매도)를 뺀 손익만 보고한다. 부분 체결·호가 잔량은 반영하지 못한다.
  - 오버나이트 보유(CLOSE)는 다음 거래일로 넘긴다. 다음 날 첫 봉 시가가 손절가 아래면 시가에 체결 (갭 위험 반영).
  - 예수금은 무제한으로 본다 (수량은 손실·종목·노출 한도로만 정해진다).

[실거래와 다른 점 — 결과를 읽을 때 고려]
  - 실거래 청산은 2초마다 현재가로 보지만 재생은 3분봉 시·저·고·종가로 본다.
  - 실거래 후보는 시장 전체 순위표에서 나오지만 재생은 history.db 에 받아 둔 종목 안에서만 고른다.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from python_app.models.types import Bar, Position, SignalType, StrategyConfig
from python_app.risk.capital_guard import AccountState, CapitalGuard, RiskConfig
from python_app.strategies.base_strategy import (BaseStrategy, MarketContext, StrategySignal, SymbolSession,
                                                 parse_hhmm)
from python_app.strategies.candidate_scanner import Candidate, ScannerConfig, passes_filter, rank_candidates
from python_app.strategies.indicators import tick_size
from python_app.strategies.strategy_manager import StrategyManager
from python_app.strategy.exit_rules import check_exit, stop_loss_price

logger = logging.getLogger("3WAYTRADING.Replay")

UNLIMITED_CASH = 1e15
INDEX_SYMBOLS = ("IDX001", "IDX101")


@dataclass
class BacktestConfig:
    slippage_ticks: int = 1
    fee_rate: float = 0.00015
    tax_rate: float = 0.0020
    use_scanner: bool = True          # False → 받아 둔 모든 종목을 감시 (순위 없음)


@dataclass
class Trade:
    strategy: str
    pattern: str
    symbol: str
    name: str
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    qty: int
    stop_price: float
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0                  # 수수료·세금·슬리피지 차감 후
    carried_over: bool = False        # 전 거래일에서 넘어온 보유 (오버나이트)

    @property
    def holding_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60.0 if self.exit_time else 0.0

    @property
    def return_pct(self) -> float:
        cost = self.entry_price * self.qty
        return self.pnl / cost * 100.0 if cost else 0.0


@dataclass
class _Open:
    trade: Trade
    position: Position
    target: float
    hold_overnight: bool
    entered_on_bar: datetime


@dataclass
class DayResult:
    date: str
    trades: List[Trade] = field(default_factory=list)
    signals: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)


class Replay:
    def __init__(self, strategies_factory: Callable[[], List[BaseStrategy]], common: Optional[StrategyConfig] = None,
                 scanner_cfg: Optional[ScannerConfig] = None, bt: Optional[BacktestConfig] = None,
                 names: Optional[Dict[str, str]] = None, risk: Optional[RiskConfig] = None):
        self.strategies_factory = strategies_factory
        self.common = common or StrategyConfig()
        self.scanner_cfg = scanner_cfg or ScannerConfig()
        self.bt = bt or BacktestConfig()
        self.names = names or {}
        self._index_prev_close: Dict[str, float] = {}
        self.guard = CapitalGuard(self.common, risk, self.bt.fee_rate, self.bt.tax_rate)   # 실거래와 같은 승인·수량
        self.carried: Dict[str, _Open] = {}          # 다음 거래일로 넘긴 오버나이트 보유

    # ================================================================ 체결 가정
    def _buy_fill(self, price: float) -> float:
        return price + self.bt.slippage_ticks * tick_size(price)

    def _sell_fill(self, price: float) -> float:
        return max(price - self.bt.slippage_ticks * tick_size(price), 1.0)

    def _close(self, o: _Open, when: datetime, price: float, reason: str) -> Trade:
        t = o.trade
        t.exit_time, t.exit_price, t.exit_reason = when, self._sell_fill(price), reason
        gross = (t.exit_price - t.entry_price) * t.qty
        fees = (t.exit_price + t.entry_price) * t.qty * self.bt.fee_rate + t.exit_price * t.qty * self.bt.tax_rate
        t.pnl = gross - fees
        return t

    def _exit_on_bar(self, o: _Open, bar: Bar, owner: Optional[BaseStrategy]) -> Optional[Trade]:
        """봉 하나 동안의 청산 판정 — 실거래와 같은 check_exit 를 시가 → 저가 → 고가 → 종가 순으로 호출"""
        points = [("open", bar.open), ("low", bar.low), ("high", bar.high), ("close", bar.close)]
        if bar.timestamp == o.entered_on_bar:
            points = points[1:]                    # 진입한 봉은 시가(진입가) 이후만
        pos = o.position
        exit_cfg = owner.exit_config(self.common) if owner else self.common
        if o.trade.stop_price <= 0:
            pos.stop_loss_price = stop_loss_price(pos.avg_price, exit_cfg)
        kw = dict(take_profit_price=o.target, stop_label="(전략 손절가)" if o.trade.stop_price > 0 else "")
        for label, price in points:
            pos.current_price = price
            decision = (owner.check_exit(pos, price, self.common, bar.timestamp, trade=o.trade, **kw) if owner
                        else check_exit(pos, price, self.common, **kw))
            if decision is None:
                continue
            if decision.signal_type == SignalType.SELL_TAKE_PROFIT:
                fill = max(price if label == "open" else decision.trigger_price, decision.trigger_price)
            else:                                  # 손절·트레일링: 봉 안에서 닿았으면 기준가, 갭이면 그 가격
                fill = decision.trigger_price if label == "low" else price
            return self._close(o, bar.timestamp, fill, decision.reason)
        return None

    # ================================================================ 하루 재생
    def run_day(self, date_str: str, day_bars: Dict[str, List[Bar]], daily: Dict[str, List[dict]]) -> DayResult:
        """date_str = YYYY-MM-DD. day_bars = 종목 → 그날 봉(시간순), daily = 종목 → 전체 일봉(오래된 순)"""
        result = DayResult(date_str)
        ymd = date_str.replace("-", "")
        before = lambda sym, _d=None: [r for r in daily.get(sym, []) if r["dt"] < ymd]
        manager = StrategyManager(self.strategies_factory(), daily_provider=before)
        last_entry = manager.last_entry_time()
        eod = parse_hhmm(self.common.eod_liquidation_time)

        index_bars = {s: day_bars.get(s, []) for s in INDEX_SYMBOLS}
        stocks = {s: b for s, b in day_bars.items() if s not in INDEX_SYMBOLS and b}
        prev_close = {s: rows[-1]["close"] for s in stocks for rows in [before(s)] if rows}
        market = {s: SymbolSession(s, self.names.get(s, s), date_str) for s in stocks}
        by_time: Dict[datetime, List[Bar]] = {}
        for bars in stocks.values():
            for b in bars:
                by_time.setdefault(b.timestamp, []).append(b)
        index_at: Dict[str, Dict[datetime, float]] = {s: {b.timestamp: b.close for b in bars}
                                                      for s, bars in index_bars.items()}
        index_last: Dict[str, float] = {}

        fed: Dict[str, int] = {}                       # 종목 → 관리자에 넣은 봉 수
        open_pos: Dict[str, _Open] = self.carried      # 전 거래일에서 넘어온 오버나이트 보유부터
        self.carried = {}
        for o in open_pos.values():
            o.trade.carried_over = True
        pending: Dict[str, tuple] = {}                 # 종목 → (신호, 승인된 수량)
        closed_today: set = set()

        def skip(reason: str):
            result.skipped[reason] = result.skipped.get(reason, 0) + 1

        def finish(trade: Optional[Trade], sym: str):
            if trade is None:
                return
            result.trades.append(trade)
            manager.record_trade(trade.strategy, trade.pnl)
            self.guard.record_close(trade.strategy, trade.pnl, trade.exit_time)
            closed_today.add(sym)
            del open_pos[sym]

        for t in sorted(by_time):
            # ---- 1. 이 시각에 마감된 봉: 대기 주문 체결(시가) → 보유분 청산 판정
            for bar in by_time[t]:
                sym = bar.symbol
                sig, qty = pending.pop(sym, (None, 0))
                if sig is not None:                    # 신호 다음 봉의 시가에 체결 (수량은 신호 시점에 승인된 값)
                    price = self._buy_fill(bar.open)
                    stop = float(sig.stop_price or 0.0)
                    if stop and price <= stop:
                        skip("체결가가 손절가 이하 (갭 하락)")
                    else:
                        trade = Trade(sig.strategy, sig.pattern, sym, sig.name, sig.timestamp, t, price, qty, stop)
                        pos = Position(symbol=sym, name=sig.name, qty=qty, avg_price=price, current_price=price,
                                       stop_loss_price=stop, take_profit_price=0.0, entry_time=t,
                                       highest_price=price)
                        open_pos[sym] = _Open(trade, pos, float(sig.target_price or 0.0), sig.hold_overnight, t)
                o = open_pos.get(sym)
                if o is not None:
                    finish(self._exit_on_bar(o, bar, manager.strategy(o.trade.strategy)), sym)
                market[sym].add(bar)

            # ---- 2. 장 마감 일괄 청산 (오버나이트 보유 제외)
            if self.common.eod_liquidation and t.time() >= eod:
                for sym, o in list(open_pos.items()):
                    last = market[sym].last if sym in market else None
                    today_only = not (o.hold_overnight and not o.trade.carried_over)     # 오버나이트는 매수 당일만 제외
                    if today_only and last is not None and last.timestamp == t:
                        finish(self._close(o, t, last.close, f"장 마감 일괄 청산 ({self.common.eod_liquidation_time})"), sym)

            if last_entry is None or t.time() > last_entry:
                continue

            # ---- 3. 후보 선정 (이 시각까지의 누적값만 사용) + 지수
            for s in INDEX_SYMBOLS:
                if t in index_at[s]:
                    index_last[s] = index_at[s][t]
            changes = [(index_last[s] / self._index_prev_close[s] - 1.0) * 100.0
                       for s in INDEX_SYMBOLS if s in index_last and self._index_prev_close.get(s)]
            index = {"change_rate": min(changes)} if len(changes) == len(INDEX_SYMBOLS) else None

            pool = []
            for sym, s in market.items():
                if not s.bars or sym not in prev_close:
                    continue
                c = Candidate(sym, s.name, price=s.last.close, volume=s.cum_volume, trade_value=s.cum_value,
                              change_rate=(s.last.close / prev_close[sym] - 1.0) * 100.0)
                if not self.bt.use_scanner or passes_filter(c, self.scanner_cfg):
                    pool.append(c)
            if self.bt.use_scanner:
                ranked = rank_candidates(pool, self.scanner_cfg, market)[:max(0, int(self.scanner_cfg.max_candidates))]
            else:
                ranked = sorted(pool, key=lambda c: c.symbol)
                for i, c in enumerate(ranked, 1):
                    c.rank = i
            watch = {c.symbol: c for c in ranked}

            # ---- 4. 감시 종목의 새 봉을 전략에 투입 (새로 들어온 후보는 지난 봉까지 한꺼번에 — 실거래와 같다)
            signals: List[StrategySignal] = []
            for sym, cand in watch.items():
                bars = market[sym].bars
                for bar in bars[fed.get(sym, 0):]:
                    for sig in manager.on_bar(bar, self.names.get(sym, sym), MarketContext(now=t, candidate=cand, index=index)):
                        result.signals += 1
                        if sig.timestamp == t:
                            signals.append(sig)
                        else:
                            skip("지나간 봉의 신호 (늦게 후보가 됨)")
                fed[sym] = len(bars)

            # ---- 5. 자금 보호 승인 (실거래와 같은 CapitalGuard) → 다음 봉 시가 체결 대기
            for sig in sorted(signals, key=lambda s: watch[s.symbol].rank):
                amount = {s: o.trade.entry_price * o.trade.qty for s, o in open_pos.items()}
                amount.update({s: p.entry_price * q for s, (p, q) in pending.items()})
                overnight = {s for s, o in open_pos.items() if o.hold_overnight} | \
                            {s for s, (p, _) in pending.items() if p.hold_overnight}
                state = AccountState(
                    cash=UNLIMITED_CASH,
                    active={**{s: o.trade.strategy for s, o in open_pos.items()},
                            **{s: p.strategy for s, (p, _) in pending.items()}},
                    overnight=overnight, exposure=sum(amount.values()),
                    overnight_exposure=sum(amount[s] for s in overnight), closed_today=set(closed_today),
                    unrealized=sum((market[s].last.close - o.trade.entry_price) * o.trade.qty
                                   for s, o in open_pos.items() if s in market and market[s].last))
                decision = self.guard.evaluate(sig, state, t)
                if decision.approved:
                    pending[sig.symbol] = (sig, decision.qty)
                else:
                    skip(re.sub(r" \(.*", "", decision.reason))        # 금액 등 세부는 떼고 사유만 센다

        # ---- 6. 그날 봉이 끝났는데 남은 것: 미체결 주문은 버리고, 오버나이트는 다음 날로, 나머지는 마지막 봉 종가로 정리
        for _ in pending:
            skip("다음 봉 없음 (미체결)")
        for sym, o in list(open_pos.items()):
            last = market[sym].last if sym in market else None
            if last is not None:
                o.position.current_price = last.close
            if (o.hold_overnight and not o.trade.carried_over) or last is None:
                self.carried[sym] = o                  # 오늘 산 오버나이트 보유, 또는 오늘 봉이 없던 이월 보유
                del open_pos[sym]
            else:
                finish(self._close(o, last.timestamp, last.close, "장 마감 일괄 청산 (마지막 봉)"), sym)
        for s, bars in index_bars.items():
            if bars:
                self._index_prev_close[s] = bars[-1].close
        return result

    # ================================================================ 여러 날
    def run(self, store, dates: List[str], progress: Optional[Callable[[int, int], None]] = None) -> List[DayResult]:
        daily = store.load_all_daily()
        out = []
        for i, d in enumerate(dates, 1):
            out.append(self.run_day(d, store.load_day(d), daily))
            if progress:
                progress(i, len(dates))
        if out and self.carried:                       # 재생이 끝났는데 남은 오버나이트 보유 → 마지막으로 본 가격에 정리
            for sym, o in list(self.carried.items()):
                when = o.trade.entry_time
                out[-1].trades.append(self._close(o, when, o.position.current_price, "재생 종료 (다음 거래일 데이터 없음)"))
            self.carried = {}
        return out
