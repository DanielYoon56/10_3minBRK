"""
3WAYTRADING - 실시간 자동매매 엔진 (키움 모의거래 / 실거래 공용)

GUI와 분리된 순수 로직. 워커 스레드가 tick()을 1초마다 호출한다.
전략은 신호만 내고, 주문은 이 엔진만 낸다.

[매수 흐름]
  3분봉 마감 시각(09:03, 09:06, …)마다
  → 후보 탐색기(CandidateScanner)가 당일 순위를 다시 조회해 감시종목을 교체
  → 감시종목 분봉 조회(ka10080), 새로 완성된 봉을 저장하고 전략 관리자(StrategyManager)에 투입
  → 전략별 신호(StrategySignal)를 후보 순위순으로 진입 조건 검사 후 주문(kt10000)
  → 지정가 신호: 유효 시각까지 미체결이면 취소, 체결 전 현재가가 손절가 이하면 취소

  → 모든 신호는 자금 보호(CapitalGuard)의 승인과 수량 계산을 거친다 (손실 한도, 노출 한도, 중복 주문 방지 등)

[매도 흐름]
  앱이 매수한 종목만(관리종목) 2초마다 현재가 조회(ka10001)
  → 그 종목을 소유한 전략의 check_exit (기본: 손절 / 트레일링 / 목표가, 손절가는 전략이 준 값)
  → 시장가 매도(kt10001)
  → 오버나이트 보유(hold_overnight) 포지션은 장 마감 일괄 청산·익일 시초 청산에서 제외

[안전장치]
  - 계좌에 원래 있던 종목은 절대 매도하지 않음 (관리종목만 청산)
  - 시작이 늦어 과거 봉에서 나온 신호는 무시 (현재 봉 신호만 진입)
  - 일 손실한도 도달 시 신규 매수 중단
  - 종목당 진행 중 주문 1건 (중복 주문 방지), 미체결 30초 → 취소 후 재시도
  - 최고가·관리종목을 파일에 저장 → 재시작해도 트레일링 기준 유지
  - 실거래(REAL)는 live_confirmed=True 없이는 시작 불가
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from python_app.broker.kiwoom_rest import KiwoomRestBroker
from python_app.models.types import (Bar, OrderSide, OrderStatus, OrderType, Position, StrategyConfig,
                                     TradeRecord, TradingMode)
from python_app.risk.capital_guard import AccountState, CapitalGuard
from python_app.strategies.base_strategy import MarketContext, StrategySignal
from python_app.strategies.strategy_manager import StrategyManager
from python_app.strategy.exit_rules import check_exit, stop_loss_price

logger = logging.getLogger("Korea3MinBreakoutTrader.LiveEngine")

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:  # tzdata 미설치 Windows 등
    _KST = None


def kst_now() -> datetime:
    if _KST is not None:
        return datetime.now(_KST).replace(tzinfo=None)
    return datetime.utcnow() + timedelta(hours=9)


MARKET_OPEN = dtime(9, 0)
LAST_ORDER_TIME = dtime(15, 20)   # 15:20~15:30 종가 동시호가 → 손절/트레일링 감시 중단
CLOSING_AUCTION = dtime(15, 20)
LAST_EOD_ORDER = dtime(15, 29, 30)  # 일괄청산 주문은 동시호가 중에도 접수 (15:30 종가 체결)
LAST_BAR_POLL = dtime(15, 34, 30)   # 15:30 종가 단일가 봉(마감 15:33)까지 기록
FEE_RATE = 0.00015
TAX_RATE = 0.0020


@dataclass
class ManagedTrade:
    symbol: str
    name: str
    status: str                       # BUYING / HOLDING / SELLING / CLOSED / FAILED
    order_id: str = ""
    order_time: str = ""
    entry_price: float = 0.0
    qty: int = 0
    highest_price: float = 0.0
    exit_reason: str = ""
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    signal_price: float = 0.0
    carried_over: bool = False        # 전일에서 넘어온 보유 (장 마감 청산 누락)
    strategy: str = ""                # 이 포지션을 소유한 전략 (open / intraday / close) — 청산도 이 전략이 판정
    pattern: str = ""                 # 전략 안의 진입 패턴
    hold_overnight: bool = False      # 오버나이트 보유 (장 마감 일괄 청산·익일 시초 청산 제외)
    entry_date: str = ""              # 매수 주문일 YYYYMMDD
    order_type: str = "MARKET"        # 매수 주문 방식 (MARKET / LIMIT)
    stop_price: float = 0.0           # 전략 손절가 (0 = 매입가 × (1 - stop_loss_rate%))
    target_price: float = 0.0         # 차트에서 정한 목표가 (0 = 없음)
    use_trailing: bool = True         # 새 진입은 항상 True. False 는 예전 F 진입분(상태 파일)만 남아 있다
    expire_at: str = ""               # 지정가 매수 유효 시각 (ISO)
    order_qty: int = 0                # 매수 주문 수량
    buy_open: bool = False            # 지정가 매수 잔량이 아직 살아 있음 (부분 체결)
    buy_order_id: str = ""            # 매수 주문번호 (order_id는 매도 주문 시 덮어씀)
    buy_filled_at: str = ""           # 첫 매수 체결 확인 시각 (ISO)
    stop_rate: float = 0.0            # 손절선을 옮긴 뒤 1R 기준을 체결가로 다시 계산할 % (0 = 사용 안 함)
    stop_manual: bool = False         # 사용자가 차트에서 옮긴 손절가 (추가 체결로 다시 계산하지 않음)
    risk_stop: float = 0.0            # 손절선을 옮기기 전 손절가 — 1R 기준으로 계속 쓴다 (0 = stop_price 기준)

    def order_dt(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.order_time)
        except ValueError:
            return None

    def expire_dt(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.expire_at) if self.expire_at else None
        except ValueError:
            return None

    @property
    def is_limit_buy(self) -> bool:
        return self.order_type == OrderType.LIMIT.value


@dataclass
class EngineSettings:
    bar_poll_delay_sec: int = 4       # 3분봉 마감 후 몇 초 뒤 조회할지 (확정 대기)
    exit_poll_sec: float = 2.0        # 보유종목 시세 확인 주기
    account_sync_sec: float = 15.0    # 잔고/예수금 동기화 주기
    order_timeout_sec: float = 30.0   # 미체결 판정 → 취소
    stale_signal_sec: int = 120       # 봉 마감 후 이 시간 지난 신호는 진입 안 함
    limit_sync_sec: float = 5.0       # 지정가 매수 대기 중 잔고 동기화 주기 (체결 → 손절 감시를 빨리 시작)


class LiveTradingEngine:
    def __init__(
        self,
        broker: KiwoomRestBroker,
        config: StrategyConfig,
        watchlist: List[Tuple[str, str]],
        state_dir: str = ".",
        clock: Callable[[], datetime] = kst_now,
        settings: Optional[EngineSettings] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
        notifier=None,
        db=None,
        scanner=None,
        manager: Optional[StrategyManager] = None,
        bar_store=None,
        guard: Optional[CapitalGuard] = None,
    ):
        self.broker = broker
        self.config = config
        self.fixed_watchlist = list(watchlist)    # 후보 탐색기를 쓰지 않을 때의 고정 감시종목
        self.watchlist = list(watchlist)
        self.clock = clock
        self.settings = settings or EngineSettings()
        self.on_event = on_event
        self.notifier = notifier          # TelegramNotifier (notify(text)) 또는 None
        self._push_seen: Dict[str, datetime] = {}
        self._summary_sent_date = ""
        self.db = db
        self.scanner = scanner            # CandidateScanner — 있으면 감시종목을 장중에 계속 교체
        self.bar_store = bar_store        # BarStore — 있으면 완성 3분봉·후보를 기록 (백테스트용)
        self.manager = manager or StrategyManager()
        if self.manager.daily_provider is None:
            self.manager.daily_provider = self._daily_before
        self.manager.live = broker.is_real      # 실거래에서는 live_enabled 를 켠 전략만 신규 진입
        self.guard = guard or CapitalGuard(config)   # 모든 매수 신호의 승인·수량 계산
        self._halt_announced = ""
        self._api_fail_streak = 0
        self._scan_error = ""
        self.market_index: Optional[Dict[str, float]] = None
        self._index_error = ""
        self.state_path = os.path.join(state_dir, f"live_state_{broker.mode.value}.json")

        self.running = False
        self.trade_date = ""
        self.managed: Dict[str, ManagedTrade] = {}
        self.closed_today: List[ManagedTrade] = []   # 같은 날 재진입으로 managed 에서 밀려난 끝난 거래
        self.realized_pnl = 0.0
        self.cash = 0.0
        self.positions: Dict[str, Position] = {}
        self.last_quotes: Dict[str, float] = {}
        self.last_bar_close: Dict[str, float] = {}
        self.signals: List[StrategySignal] = []

        self._last_fed: Dict[str, datetime] = {}
        self._last_bar_poll_idx = -1
        self._last_exit_poll = datetime.min
        self._last_sync = datetime.min
        self._eod_logged = False
        self._lock = threading.RLock()

    # ================================================================ 이벤트/로그
    def _emit(self, level: str, msg: str, push: bool = False):
        getattr(logger, level if level in ("info", "warning", "error") else "info")(msg)
        if self.on_event:
            try:
                self.on_event(level, msg)
            except Exception:
                pass
        if push or level == "error":
            self._push(msg.strip(" └"))

    def _push(self, text: str):
        """휴대폰 알림 (같은 문구는 5분 내 1회만 — 재시도 반복 시 알림 폭주 방지)"""
        if not self.notifier:
            return
        now = datetime.now()
        last = self._push_seen.get(text)
        if last and (now - last).total_seconds() < 300:
            return
        self._push_seen[text] = now
        try:
            self.notifier.notify(text)
        except Exception as e:
            logger.error("알림 전송 오류: %s", e)

    # ================================================================ 상태 저장
    def _save_state(self):
        data = {
            "trade_date": self.trade_date,
            "mode": self.broker.mode.value,
            "realized_pnl": self.realized_pnl,
            "managed": {k: asdict(v) for k, v in self.managed.items()},
            "closed_today": [asdict(v) for v in self.closed_today],
            "guard": self.guard.to_dict(),       # 주간 손익·연속 손실 누계 (재시작해도 유지)
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _load_state(self, today: str):
        """같은 모드의 저장 상태 복원. 날짜가 바뀌었으면 보유(HOLDING) 종목만 이월."""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            self._emit("warning", f"상태 파일 읽기 실패 → 새로 시작: {e}")
            return
        self.guard.load_dict(data.get("guard"))
        known = ManagedTrade.__dataclass_fields__    # 예전 상태 파일의 없어진 필드는 무시
        managed = {k: ManagedTrade(**{f: x for f, x in v.items() if f in known})
                   for k, v in (data.get("managed") or {}).items()}
        for v in managed.values():   # buy_order_id 없던 시절의 상태 파일
            if not v.buy_order_id and v.status in ("BUYING", "HOLDING"):
                v.buy_order_id = v.order_id
        if data.get("trade_date") == today:
            self.managed = managed
            self.closed_today = [ManagedTrade(**{f: x for f, x in v.items() if f in known})
                                 for v in data.get("closed_today") or []]
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
        else:
            self.managed = {k: v for k, v in managed.items() if v.status in ("HOLDING", "SELLING")}
            for v in self.managed.values():
                v.status = "HOLDING"
                v.carried_over = True
        if self.managed:
            self._emit("info", f"관리종목 복원: {', '.join(f'{m.name}({m.status})' for m in self.managed.values())}")

    # ================================================================ 시작/중지
    def start(self, live_confirmed: bool = False) -> bool:
        if self.broker.is_real and not live_confirmed:
            self._emit("error", "실거래 모드는 확인 절차 없이 시작할 수 없습니다.")
            return False
        if not self.broker.connect():
            self._emit("error", f"키움 {self.broker.mode_label} 접속 실패 — 앱키/시크릿키와 모드(모의/실거래)를 확인하세요.")
            return False
        now = self.clock()
        self.trade_date = now.strftime("%Y%m%d")
        self._load_state(self.trade_date)
        self._sync_account(force=True)
        self._last_bar_poll_idx = -1
        self.halted_reason = ""
        self._update_watchlist()
        self.running = True
        held = [m.name for m in self.managed.values() if m.status == "HOLDING"]
        target = "감시종목: 실시간 후보 탐색" if self.scanner else f"고정 감시 {len(self.fixed_watchlist)}종목"
        self._emit("info", f"▶ 자동매매 시작 [{self.broker.mode_label}] {target} · "
                           f"전략: {self.manager.describe()} · "
                           f"주문가능 {self.cash:,.0f}원" + (f" · 관리중 {', '.join(held)}" if held else ""), push=True)
        return True

    def stop(self):
        self.running = False
        with self._lock:
            self._save_state()
        held = [m.name for m in self.managed.values() if m.status in ("HOLDING", "SELLING")]
        self._emit("info", "■ 자동매매 중지" + (f" — 보유 유지: {', '.join(held)} (감시 중단!)" if held else ""),
                   push=True)

    # ================================================================ 메인 tick
    def tick(self):
        if not self.running:
            return
        with self._lock:
            now = self.clock()
            if now.weekday() >= 5:
                return
            today = now.strftime("%Y%m%d")
            if today != self.trade_date:
                self._new_day(today)

            t = now.time()
            if t >= dtime(15, 31) and self._summary_sent_date != today:
                self._summary_sent_date = today
                self._sync_account()
                self._daily_summary()
            if t < MARKET_OPEN or t > LAST_BAR_POLL:
                return
            if t > dtime(15, 30):
                self._poll_bars_if_due(now)     # 종가 단일가 봉 기록만 (주문·청산 감시 없음)
                return

            limit_waiting = any(m.is_limit_buy and (m.status == "BUYING" or m.buy_open)
                                for m in self.managed.values())
            sync_sec = (min(self.settings.account_sync_sec, self.settings.limit_sync_sec)
                        if limit_waiting else self.settings.account_sync_sec)
            if (now - self._last_sync).total_seconds() >= sync_sec:
                self._sync_account()

            self._resolve_pending(now)

            if (now - self._last_exit_poll).total_seconds() >= self.settings.exit_poll_sec:
                if self._eod_due(now):
                    if t <= LAST_EOD_ORDER:
                        self._last_exit_poll = now
                        self._liquidate_all(now)
                        if t <= LAST_ORDER_TIME:
                            self._check_exits(now)      # 일괄 청산 대상이 아닌 오버나이트 보유분 감시
                elif t <= LAST_ORDER_TIME:
                    self._last_exit_poll = now
                    self._check_pending_limits(now)
                    self._check_exits(now)

            self._poll_bars_if_due(now)

    def _new_day(self, today: str):
        self._emit("info", f"새 거래일 {today}")
        self.trade_date = today
        self.realized_pnl = 0.0
        self.halted_reason = ""
        self.closed_today = []
        self.managed = {k: v for k, v in self.managed.items() if v.status in ("HOLDING", "SELLING")}
        for v in self.managed.values():
            v.status = "HOLDING"
            v.carried_over = True
        self._eod_logged = False
        self._last_fed.clear()
        self._last_bar_poll_idx = -1
        self.signals.clear()
        if self.scanner:
            self.scanner.candidates = {}
            self.scanner.last_refresh = None
        self._update_watchlist()
        self._save_state()

    # ================================================================ 후보 탐색 (감시종목)
    def _held_symbols(self) -> List[Tuple[str, str]]:
        return [(m.symbol, m.name) for m in self.managed.values() if m.status in ("BUYING", "HOLDING", "SELLING")]

    def _update_watchlist(self):
        """감시종목 = 후보(없으면 고정 종목) + 보유 종목. 보유 종목은 후보에서 빠져도 봉을 계속 받는다."""
        base = self.scanner.watchlist() if self.scanner else list(self.fixed_watchlist)
        seen = {s for s, _ in base}
        self.watchlist = base + [h for h in self._held_symbols() if h[0] not in seen]

    def _refresh_candidates(self, now: datetime, idx: int):
        if not self.scanner:
            self._update_watchlist()
            return
        every = max(1, int(self.scanner.cfg.refresh_every_bars))
        if self.scanner.last_refresh is None or idx % every == 0:
            added, removed = self.scanner.refresh(now, self.manager.sessions)
            err = self.scanner.last_error
            if err and err != self._scan_error:
                self._emit("warning", f"🔍 후보 조회 오류: {err} — 기존 후보 {len(self.scanner.candidates)}종목 유지")
            self._scan_error = err
            if added:
                names = ", ".join(c.name for c in added[:10]) + (f" 외 {len(added) - 10}" if len(added) > 10 else "")
                self._emit("info", f"🔍 후보 {len(self.scanner.candidates)}종목 (+{len(added)} / -{len(removed)}): {names}")
            if self.bar_store and (added or removed or self.scanner.last_refresh == now):
                try:
                    self.bar_store.save_candidates(now, self.scanner.candidates.values())
                except Exception as e:
                    logger.error("후보 기록 오류: %s", e)
        self._update_watchlist()

    # ================================================================ 계좌 동기화
    def _sync_account(self, force: bool = False):
        self._last_sync = self.clock()
        self.positions = dict(self.broker.get_positions())
        self.cash = self.broker.get_cash_balance()

        changed = False
        for sym, m in list(self.managed.items()):
            pos = self.positions.get(sym)
            if m.status == "BUYING" and pos:
                m.status = "HOLDING"
                m.entry_price = pos.avg_price
                self._apply_stop_rate(m)
                m.qty = pos.qty
                m.highest_price = max(pos.avg_price, pos.current_price)   # 신호가는 체결 전 값이라 제외
                m.buy_open = m.is_limit_buy and 0 < m.order_qty and pos.qty < m.order_qty
                stop = m.stop_price or pos.stop_loss_price
                extra = f", 목표 {m.target_price:,.0f}원" if m.target_price else ""
                part = f" (부분 체결 {m.qty}/{m.order_qty}주 — 잔량 대기)" if m.buy_open else ""
                self._emit("info", f"✅ 매수 체결 {m.name} {m.qty}주 @ {m.entry_price:,.0f}원 "
                                   f"(손절 {stop:,.0f}원{extra}){part}", push=True)
                self._record_buy_fill(m)
                changed = True
            elif m.status == "HOLDING" and pos:
                if pos.qty > m.qty or (m.buy_open and pos.qty != m.qty):   # 추가 체결 → 평균단가 갱신
                    m.entry_price = pos.avg_price
                    self._apply_stop_rate(m)
                    m.buy_open = m.buy_open and pos.qty < m.order_qty
                    changed = True
                m.qty = pos.qty
                if m.entry_price <= 0:
                    m.entry_price = pos.avg_price
                    self._apply_stop_rate(m)
            elif m.status in ("HOLDING", "SELLING") and not pos:
                self._close_trade(m, manual=(m.status == "HOLDING"))
                changed = True
        if changed:
            self._save_state()

    @staticmethod
    def _apply_stop_rate(m: ManagedTrade):
        if m.stop_rate > 0 and m.entry_price > 0:
            stop = round(m.entry_price * (1.0 - m.stop_rate / 100.0))
            if m.stop_manual:
                m.risk_stop = stop       # 옮긴 손절가는 두고 1R 기준만 새 평균단가를 따른다
            else:
                m.stop_price = stop

    def _manual_refusal(self, symbol: str) -> str:
        """손절·익절선을 손으로 옮길 수 없는 사유 (빈 문자열 = 가능). 트레일링이 돌면 옮겨도 의미가 없어 막는다"""
        m, pos = self.managed.get(symbol), self.positions.get(symbol)
        if not self.running:
            return "자동매매가 실행 중이 아닙니다."
        if m is None or pos is None or m.status != "HOLDING":
            return "보유 중인 앱 관리 종목이 아닙니다."
        if m.use_trailing and self.config.trailing_retrace_rate > 0:
            return "트레일링을 쓰는 종목은 손절·익절선을 옮길 수 없습니다."
        if not (self.last_quotes.get(symbol) or pos.current_price):
            return "현재가를 확인할 수 없습니다."
        return ""

    def set_manual_stop(self, symbol: str, price: float) -> str:
        """차트에서 옮긴 손절가 적용. 성공이면 빈 문자열, 거부면 사유.
        현재가 이상이면 다음 감시에서 곧바로 매도되므로 받지 않는다."""
        with self._lock:
            reason = self._manual_refusal(symbol)
            if reason:
                return reason
            m, pos = self.managed[symbol], self.positions[symbol]
            current = self.last_quotes.get(symbol) or pos.current_price
            if not 0 < price < current:
                return f"손절가는 현재가({current:,.0f}원)보다 낮아야 합니다."
            before = m.stop_price or pos.stop_loss_price
            if not m.stop_manual:        # 처음 옮길 때의 손절가가 1R 기준으로 남는다
                m.risk_stop = before
                if not m.stop_price:     # 매입가 기준 -N% 종목은 추가 체결 때 1R 기준도 같은 %로 갱신
                    m.stop_rate = self.config.stop_loss_rate
            m.stop_price, m.stop_manual = price, True
            self._save_state()
        self._emit("info", f"✋ 손절가 수동 변경 {m.name} {before:,.0f}원 → {price:,.0f}원")
        return ""

    def set_manual_target(self, symbol: str, price: float) -> str:
        """차트에서 옮긴 익절가 적용. 현재가 이하면 다음 감시에서 곧바로 매도되므로 받지 않는다."""
        with self._lock:
            reason = self._manual_refusal(symbol)
            if reason:
                return reason
            m, pos = self.managed[symbol], self.positions[symbol]
            current = self.last_quotes.get(symbol) or pos.current_price
            if price <= current:
                return f"익절가는 현재가({current:,.0f}원)보다 높아야 합니다."
            before = m.target_price or m.entry_price * (1.0 + self.config.take_profit_rate / 100.0)
            m.target_price = price
            self._save_state()
        self._emit("info", f"✋ 익절가 수동 변경 {m.name} {before:,.0f}원 → {price:,.0f}원")
        return ""

    def _close_trade(self, m: ManagedTrade, manual: bool = False):
        px = m.exit_price or self.last_quotes.get(m.symbol, m.entry_price)
        gross = (px - m.entry_price) * m.qty
        fees = (px + m.entry_price) * m.qty * FEE_RATE + px * m.qty * TAX_RATE
        m.realized_pnl = gross - fees
        m.status = "CLOSED"
        self.realized_pnl += m.realized_pnl
        self.manager.record_trade(m.strategy, m.realized_pnl)
        self.guard.record_close(m.strategy, m.realized_pnl, self.clock())
        how = "수동 청산 감지" if manual else "매도 체결"
        icon = "🔴" if m.realized_pnl < 0 else "🟢"
        self._emit("info", f"{icon} {how} {m.name} {m.qty}주 ≈{px:,.0f}원 → 추정손익 {m.realized_pnl:+,.0f}원 "
                           f"(당일 누적 {self.realized_pnl:+,.0f}원)", push=True)
        self._record_buy_fill(m)     # 부분 체결 후 늘어난 최종 수량 반영
        self._record_fill("" if manual else m.order_id, m, OrderSide.SELL, px, m.qty, self.clock(),
                          reason=m.exit_reason or how, pnl=m.realized_pnl)
        self._save_daily_pnl()

    def _record_buy_fill(self, m: ManagedTrade):
        if not m.buy_order_id or m.entry_price <= 0:
            return
        if not m.buy_filled_at:
            m.buy_filled_at = self.clock().isoformat()
        partial = 0 < m.qty < m.order_qty
        self._record_fill(m.buy_order_id, m, OrderSide.BUY, m.entry_price, m.qty,
                          datetime.fromisoformat(m.buy_filled_at),
                          reason=f"{m.strategy}.{m.pattern}" if m.pattern else m.strategy,
                          status=OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.FILLED)

    def _record_fill(self, order_id: str, m: ManagedTrade, side: OrderSide, price: float, qty: int, at: datetime,
                     reason: str = "", pnl: float = 0.0, status: OrderStatus = OrderStatus.FILLED):
        """체결을 orders·trades에 기록. 주문번호가 없으면(HTS 수동 청산) trades만. DB 오류로 매매가 멈추면 안 됨"""
        if not self.db:
            return
        fee = price * qty * FEE_RATE
        tax = price * qty * TAX_RATE if side == OrderSide.SELL else 0.0
        try:
            if order_id:
                self.db.update_order_fill(order_id, status.value, price, qty, fee, tax, at.isoformat())
            self.db.save_trade(TradeRecord(
                trade_id=f"{at:%Y%m%d}-{order_id or m.symbol + '-MANUAL'}", order_id=order_id, symbol=m.symbol,
                name=m.name, side=side, price=price, qty=qty, commission=fee, tax=tax, realized_pnl=pnl,
                executed_at=at, reason=reason, strategy=m.strategy, mode=self.broker.mode.value))
        except Exception as e:
            logger.error("체결 기록 오류 (%s %s): %s", m.name, side.value, e)

    def _record_cancel(self, order_id: str):
        if not self.db or not order_id:
            return
        try:
            self.db.update_order_fill(order_id, OrderStatus.CANCELLED.value)
        except Exception as e:
            logger.error("주문 취소 기록 오류 #%s: %s", order_id, e)

    def _trades_today(self) -> List[ManagedTrade]:
        """당일 모든 거래 (재진입으로 밀려난 끝난 거래 포함, 오래된 것부터)"""
        return self.closed_today + list(self.managed.values())

    def _save_daily_pnl(self):
        """당일 청산 거래 요약을 DB에 기록 → GUI '일별 누적수익' 탭"""
        if not self.db or not self.trade_date:
            return
        closed = [m for m in self._trades_today() if m.status == "CLOSED"]
        try:
            self.db.save_daily_pnl(
                self.broker.mode.value, self.trade_date, self.realized_pnl,
                invested=sum(m.entry_price * m.qty for m in closed), trades=len(closed),
                wins=sum(1 for m in closed if m.realized_pnl > 0),
                losses=sum(1 for m in closed if m.realized_pnl < 0))
        except Exception as e:
            logger.error("일별 손익 저장 오류: %s", e)

    # ================================================================ 미체결 처리
    def _is_stuck(self, m: ManagedTrade, now: datetime, in_auction: bool) -> bool:
        if m.is_limit_buy and m.status == "BUYING":
            exp = m.expire_dt()
            return exp is not None and now >= exp
        if m.status not in ("BUYING", "SELLING") or not m.order_dt():
            return False
        if in_auction and m.status == "SELLING":
            return False
        return (now - m.order_dt()).total_seconds() > self.settings.order_timeout_sec

    def _resolve_pending(self, now: datetime):
        timeout = self.settings.order_timeout_sec
        in_auction = now.time() >= CLOSING_AUCTION   # 동시호가 매도는 15:30에 체결 → 취소하지 않음
        stuck = [m for m in self.managed.values() if self._is_stuck(m, now, in_auction)]
        # 지정가 부분 체결 후 유효 시각이 지난 잔량
        leftovers = [m for m in self.managed.values()
                     if m.buy_open and m.status == "HOLDING" and m.expire_dt() and now >= m.expire_dt()]
        if not stuck and not leftovers:
            return
        self._sync_account()
        open_orders = {o["ord_no"]: o for o in self.broker.get_unfilled_orders()}
        for m in leftovers:
            if m.buy_open and m.order_id in open_orders:
                self.broker.cancel_order(m.order_id)
                self._emit("info", f"지정가 매수 잔량 유효시간 종료 → 잔량 취소 {m.name} (보유 {m.qty}주로 관리)")
            m.buy_open = False
        for m in stuck:
            if m.status not in ("BUYING", "SELLING"):
                continue  # 동기화 중 체결 확인됨
            if m.order_id in open_orders:
                self.broker.cancel_order(m.order_id)
                if m.is_limit_buy and m.status == "BUYING":
                    self._emit("info", f"⌛ 지정가 매수 미체결 → 유효시각({m.expire_dt():%H:%M}) 경과로 취소 "
                                       f"{m.name} {m.signal_price:,.0f}원", push=True)
                else:
                    self._emit("warning", f"미체결 {timeout:.0f}초 초과 → 주문 취소 {m.name} #{m.order_id}", push=True)
            self._record_cancel(m.order_id)
            if m.status == "BUYING":
                m.status = "FAILED"
            else:
                m.status = "HOLDING"   # 다음 tick에 매도 재시도
                m.order_id = ""
        self._save_state()

    def _check_pending_limits(self, now: datetime):
        """지정가 매수 대기 중 현재가가 손절가 이하로 내려가면 셋업 무효 → 취소"""
        for m in list(self.managed.values()):
            if not (m.is_limit_buy and m.status == "BUYING" and m.stop_price > 0):
                continue
            q = self.broker.get_quote(m.symbol)
            if not q or q.current_price <= 0:
                continue
            self.last_quotes[m.symbol] = q.current_price
            if q.current_price > m.stop_price:
                continue
            self._sync_account()
            if m.status != "BUYING":
                continue   # 그 사이 체결 → 보유 손절 감시가 처리
            open_ids = {o["ord_no"] for o in self.broker.get_unfilled_orders()}
            if m.order_id in open_ids:
                self.broker.cancel_order(m.order_id)
            self._sync_account()
            if m.status == "BUYING":
                m.status = "FAILED"
                self._record_cancel(m.order_id)
                self._emit("info", f"✖ 지정가 매수 취소 {m.name}: 체결 전 현재가 {q.current_price:,.0f}원이 "
                                   f"손절가 {m.stop_price:,.0f}원 이하 → 셋업 무효", push=True)
            self._save_state()

    # ================================================================ 청산
    def _check_exits(self, now: datetime):
        for sym, m in list(self.managed.items()):
            if m.status != "HOLDING":
                continue
            pos = self.positions.get(sym)
            if not pos:
                continue
            q = self.broker.get_quote(sym)
            if not q or q.current_price <= 0:
                continue
            price = q.current_price
            self.last_quotes[sym] = price
            pos.current_price = price
            pos.avg_price = m.entry_price or pos.avg_price
            pos.highest_price = max(m.highest_price, pos.highest_price, pos.avg_price)
            owner = self.manager.strategy(m.strategy)     # 청산은 이 포지션을 소유한 전략이 판정
            exit_cfg = owner.exit_config(self.config) if owner else self.config
            pos.stop_loss_price = m.stop_price if m.stop_price > 0 else stop_loss_price(pos.avg_price, exit_cfg)

            if m.carried_over and self.config.carryover_sell_at_open and not m.hold_overnight:
                self._sell(m, pos, price, "전일 이월 종목 시초 청산 (전날 장 마감 청산 누락)", now)
                continue

            kw = dict(take_profit_price=m.target_price, use_trailing=m.use_trailing,
                      stop_label=("(수동 손절가)" if m.stop_manual else "(전략 손절가)" if m.stop_price > 0 else ""),
                      risk_stop=m.risk_stop)
            decision = (owner.check_exit(pos, price, self.config, now, trade=m, **kw) if owner
                        else check_exit(pos, price, self.config, **kw))
            if pos.highest_price > m.highest_price:
                m.highest_price = pos.highest_price
                self._save_state()
            if decision:
                self._sell(m, pos, price, decision.reason, now)

    def _sell(self, m: ManagedTrade, pos: Position, price: float, reason: str, now: datetime) -> bool:
        if m.buy_open:   # 지정가 매수 잔량이 남아 있으면 먼저 취소 (매도 후 추가 체결 방지)
            if m.order_id in {o["ord_no"] for o in self.broker.get_unfilled_orders()}:
                self.broker.cancel_order(m.order_id)
            m.buy_open = False
        qty = pos.sellable_qty or pos.qty
        order = self.broker.submit_order(m.symbol, m.name, OrderSide.SELL, qty, price=price,
                                         order_type=OrderType.MARKET, reason=reason)
        if not order:
            self._emit("error", f"매도 주문 실패 {m.name} — 다음 주기 재시도")
            return False
        m.status = "SELLING"
        m.order_id = order.order_id
        m.order_time = now.isoformat()
        m.exit_reason = reason
        m.exit_price = price
        icon = "🛑" if "손절" in reason else "📉" if "트레일링" in reason else "🔔"
        self._emit("info", f"{icon} 매도 주문 {m.name} {qty}주 — {reason}", push=True)
        if self.db:
            self.db.save_order(order)
        self._save_state()
        self._last_sync = datetime.min  # 다음 tick에 바로 체결 확인
        return True

    # ================================================================ 장 마감 일괄 청산
    def _eod_time(self) -> dtime:
        return datetime.strptime(self.config.eod_liquidation_time, "%H:%M").time()

    def _eod_due(self, now: datetime) -> bool:
        return bool(self.config.eod_liquidation) and now.time() >= self._eod_time()

    def _liquidate_all(self, now: datetime):
        # 오버나이트 보유분은 매수한 날에는 청산하지 않는다 (다음 거래일부터는 일괄 청산 대상 → 최대 1일 보유)
        targets = [(s, m) for s, m in self.managed.items() if m.status == "HOLDING" and s in self.positions
                   and not (m.hold_overnight and not m.carried_over)]
        if targets and not self._eod_logged:
            self._eod_logged = True
            self._emit("warning", f"■ 장 마감 일괄 청산 시작 ({self.config.eod_liquidation_time}) — "
                                  f"{', '.join(m.name for _, m in targets)}", push=True)
        for sym, m in targets:
            pos = self.positions[sym]
            q = self.broker.get_quote(sym)
            price = q.current_price if q and q.current_price > 0 else (self.last_quotes.get(sym) or pos.current_price)
            self.last_quotes[sym] = price
            self._sell(m, pos, price, f"장 마감 일괄 청산 ({self.config.eod_liquidation_time})", now)

    # ================================================================ 진입
    def _daily_before(self, symbol: str, trade_date: str) -> List[dict]:
        """trade_date(YYYYMMDD) '이전' 일봉 (오래된 순). 당일 일봉은 장중 미완성이라 넘기지 않는다."""
        return [r for r in self.broker.get_daily_bars(symbol, trade_date) if r["dt"] < trade_date]

    def _fetch_index(self) -> Optional[Dict[str, float]]:
        """코스피·코스닥 지수 등락률. change_rate 는 둘 중 약한 쪽 (보수적). 조회 실패면 None — 전략은 지수 조건을 판정하지 않는다"""
        if not self.manager.needs_index():
            return None
        try:
            kospi = self.broker.get_index("0", "001")["change_rate"]
            kosdaq = self.broker.get_index("1", "101")["change_rate"]
        except Exception as e:
            if str(e) != self._index_error:
                self._emit("warning", f"지수 조회 실패 → 이번 주기는 지수 조건 없이 판정: {e}")
            self._index_error = str(e)
            return None
        self._index_error = ""
        return {"kospi": kospi, "kosdaq": kosdaq, "change_rate": min(kospi, kosdaq)}

    def _bar_index(self, now: datetime) -> int:
        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        return int((now - start).total_seconds() // 180)

    def _poll_bars_if_due(self, now: datetime):
        idx = self._bar_index(now)
        boundary = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=3 * idx)
        if idx <= self._last_bar_poll_idx:
            return
        if self._last_bar_poll_idx >= 0 and (now - boundary).total_seconds() < self.settings.bar_poll_delay_sec:
            return
        if idx < 1 and self._last_bar_poll_idx >= 0:
            return
        self._last_bar_poll_idx = idx

        self._refresh_candidates(now, idx)
        self.market_index = self._fetch_index()
        pending: List[StrategySignal] = []
        fresh: List[Bar] = []
        try:
            for sym, name in self.watchlist:
                try:
                    bars = self.broker.get_minute_bars(sym, self.trade_date, now, tic=3)
                except Exception as e:
                    self._api_fail_streak += 1
                    self._emit("warning", f"[{name}] 3분봉 조회 실패: {e}")
                    continue
                self._api_fail_streak = 0
                if bars:
                    self.last_bar_close[sym] = bars[-1].close
                last = self._last_fed.get(sym)
                ctx = MarketContext(now=now, candidate=self.scanner.get(sym) if self.scanner else None,
                                    index=self.market_index)
                for bar in bars:
                    if not bar.is_completed or (last and bar.timestamp <= last):
                        continue
                    self._last_fed[sym] = bar.timestamp
                    fresh.append(bar)
                    pending.extend(self.manager.on_bar(bar, name, ctx))
        finally:
            if self.bar_store and fresh:
                try:
                    self.bar_store.save_bars(fresh)
                except Exception as e:
                    logger.error("3분봉 기록 오류: %s", e)
            # 전 종목 신호를 모은 뒤 후보 순위가 높은 종목부터 매수 (순위는 우선순위에만 쓴다)
            for sig in sorted(pending, key=lambda s: self.scanner.rank_of(s.symbol) if self.scanner else 0):
                self._on_signal(sig, now)

    def _on_signal(self, sig: StrategySignal, now: datetime):
        self.signals.append(sig)
        if self.db:
            try:
                self.db.save_signal(sig)
            except Exception as e:
                logger.error("신호 기록 오류 (%s): %s", sig.name, e)
        age = (now - sig.timestamp).total_seconds()
        if age > self.settings.stale_signal_sec:
            self._emit("info", f"○ 과거 신호 기록만 함 (진입 안 함): {sig.name} {sig.timestamp:%H:%M} — {sig.reason}")
            return
        self._emit("info", f"★ 매수 신호 {sig.name} {sig.timestamp:%H:%M} — {sig.reason}")
        self._try_buy(sig, now)

    # ---------------------------------------------------------------- 자금 보호
    @property
    def halted_reason(self) -> str:
        """신규 매수가 멈춘 사유 (비상 정지 또는 손실 한도). 빈 문자열 = 정상"""
        return (f"비상 정지 ({self.guard.emergency})" if self.guard.emergency else self.guard.halted_reason)

    @halted_reason.setter
    def halted_reason(self, value: str):
        self.guard.halted_reason = value

    def stop_new_entries(self, reason: str = "사용자 요청"):
        """비상 정지 — 신규 매수만 막는다. 보유 종목의 손절·청산 감시는 계속된다."""
        with self._lock:
            self.guard.stop_new_entries(reason)
            self._save_state()
        self._emit("warning", f"⛔ 비상 정지: 신규 매수 중단 ({reason}) — 보유 종목 감시는 계속", push=True)

    def resume_entries(self):
        with self._lock:
            self.guard.resume()
            self._save_state()
        self._emit("info", "▶ 비상 정지 해제: 신규 매수 재개", push=True)

    def _account_state(self, now: datetime) -> AccountState:
        active = {s: m for s, m in self.managed.items() if m.status in ("BUYING", "HOLDING", "SELLING")}
        amount = lambda m: (m.entry_price or m.signal_price) * (m.qty or m.order_qty)
        unreal = sum((self.last_quotes[s] - m.entry_price) * m.qty for s, m in active.items()
                     if m.status == "HOLDING" and s in self.last_quotes)
        limit = self.settings.order_timeout_sec * 3
        uncertain = [m.name for m in active.values()
                     if m.status in ("BUYING", "SELLING") and not (m.is_limit_buy and m.status == "BUYING")
                     and m.order_dt() and (now - m.order_dt()).total_seconds() > limit]
        self.guard.roll(now)
        self.guard.realized_today = self.realized_pnl        # 당일 실현손익은 엔진 값이 기준 (상태 파일에서 복원됨)
        return AccountState(
            cash=self.cash,
            active={s: m.strategy for s, m in active.items()},
            overnight={s for s, m in active.items() if m.hold_overnight},
            exposure=sum(amount(m) for m in active.values()),
            overnight_exposure=sum(amount(m) for m in active.values() if m.hold_overnight),
            account_symbols=set(self.positions),
            closed_today={s for s, m in self.managed.items() if m.status == "CLOSED"},
            unrealized=unreal,
            data_problem=("후보 갱신 지연" if self.scanner and self.scanner.is_stale(now) else ""),
            api_fail_streak=self._api_fail_streak,
            uncertain_orders=uncertain,
        )

    def _try_buy(self, sig: StrategySignal, now: datetime):
        sym = sig.symbol
        decision = self.guard.evaluate(sig, self._account_state(now), now)
        if decision.approved:                      # 승인된 신호만 예수금을 새로 조회해 수량을 다시 계산
            self.cash = self.broker.get_cash_balance()
            state = self._account_state(now)
            decision = self.guard.size(sig, state)
        halt = self.guard.halted_reason
        if halt and halt != self._halt_announced:
            self._halt_announced = halt
            self._emit("warning", "⛔ " + halt, push=True)
        if not decision.approved:
            short = decision.reason.startswith("주문가능금액 부족")
            self._emit("warning" if short else "info", f"  └ 진입 생략 ({sig.name}): {decision.reason}", push=short)
            return
        qty = decision.qty

        is_limit = sig.order_type == OrderType.LIMIT
        order = self.broker.submit_order(sym, sig.name, OrderSide.BUY, qty, price=sig.entry_price,
                                         order_type=sig.order_type, reason=sig.reason)
        if not order:
            self._emit("error", f"  └ 매수 주문 실패 ({sig.name}) — 로그 확인")
            return
        previous = self.managed.get(sym)
        if previous and previous.status in ("CLOSED", "FAILED"):    # 재진입이 끝난 거래를 덮어쓰지 않게 옮겨 둔다
            self.closed_today.append(previous)
        self.managed[sym] = ManagedTrade(
            symbol=sym, name=sig.name, status="BUYING", order_id=order.order_id, buy_order_id=order.order_id,
            order_time=now.isoformat(), qty=qty, highest_price=sig.entry_price, signal_price=sig.entry_price,
            strategy=sig.strategy, pattern=sig.pattern, order_type=sig.order_type.value,
            stop_price=sig.stop_price or 0.0,       # 전략 손절가. 없으면 0 = 매입가 × (1 - stop_loss_rate%)
            target_price=sig.target_price or 0.0,
            hold_overnight=sig.hold_overnight, entry_date=self.trade_date,
            expire_at=sig.expire_at.isoformat() if sig.expire_at else "", order_qty=qty,
        )
        if self.db:
            self.db.save_order(order)
        self._save_state()
        self._last_sync = datetime.min
        how = (f"지정가 {sig.entry_price:,.0f}원, {sig.expire_at:%H:%M}까지" if is_limit and sig.expire_at
               else f"시장가, 신호가 {sig.entry_price:,.0f}원")
        self._emit("info", f"📈 매수 주문 {sig.name} {qty}주 ({how})\n{sig.reason}", push=True)

    def _daily_summary(self):
        closed = [m for m in self._trades_today() if m.status == "CLOSED"]
        holding = [m for m in self.managed.values() if m.status in ("HOLDING", "SELLING")]
        lines = [f"📊 당일 마감 요약 [{self.broker.mode_label}] {self.trade_date[:4]}-{self.trade_date[4:6]}-{self.trade_date[6:]}"]
        if closed:
            for m in closed:
                lines.append(f"· {m.name} {m.qty}주 {m.realized_pnl:+,.0f}원 — {m.exit_reason or '청산'}")
        else:
            lines.append("· 청산 거래 없음")
        if holding:
            lines.append("· 미청산 보유: " + ", ".join(f"{m.name} {m.qty}주" for m in holding))
        lines.append(f"합계 추정손익 {self.realized_pnl:+,.0f}원 · 주문가능 {self.cash:,.0f}원")
        self._emit("info", "\n".join(lines), push=True)

    # ================================================================ GUI 스냅샷
    def snapshot(self) -> Dict:
        with self._lock:
            states = {}
            for sym, _ in self.watchlist:
                s = self.manager.sessions.get(sym)
                c = self.scanner.get(sym) if self.scanner else None
                states[sym] = {
                    "rank": c.rank if c else None,
                    "change_rate": c.change_rate if c else None,
                    "trade_value": c.trade_value if c else None,
                    "sources": list(c.sources) if c else [],
                    "vwap": s.vwap if s and s.bars else None,
                    "bars": len(s.bars) if s else 0,
                    "signals": self.manager.fired_today(sym),
                }
            return {
                "watchlist": list(self.watchlist),
                "scan_status": self.scanner.status(self.clock()) if self.scanner else "",
                "strategies": self.manager.stats_snapshot(),
                "index": dict(self.market_index) if self.market_index else None,
                "mode": self.broker.mode_label,
                "running": self.running,
                "halted": self.halted_reason,
                "cash": self.cash,
                "realized_pnl": self.realized_pnl,
                "positions": {k: asdict(v) for k, v in self.positions.items()},
                "managed": {k: asdict(v) for k, v in self.managed.items()},
                "trades": [asdict(v) for v in self._trades_today()],
                "quotes": {**self.last_bar_close, **self.last_quotes},
                "states": states,
            }
