"""
Korea3MinBreakoutTrader - 실시간 자동매매 엔진 (키움 모의거래 / 실거래 공용)

GUI와 분리된 순수 로직. 워커 스레드가 tick()을 1초마다 호출한다.

[매수 흐름]
  3분봉 마감 시각(09:03, 09:06, …)마다 감시종목 분봉 조회(ka10080)
  → 새로 완성된 봉을 전략 조합(EntryCombiner: 선택한 전략들을 AND/OR)에 투입
  → 매수 신호 + 진입 가능 조건 충족 시 시장가 매수(kt10000)
  → SMC 신호는 FVG 중간값 지정가 매수: 유효 시각까지 미체결이면 취소, 체결 전 현재가가 손절가 이하면 취소

[매도 흐름]
  앱이 매수한 종목만(관리종목) 2초마다 현재가 조회(ka10001)
  → exit_rules.check_exit: 손절 -2% / 트레일링(+1% 이상 상승 후 상승분 15% 되돌림)
     (SMC: 전략 손절가 / 목표가(R배수), 트레일링은 기본 미적용)
  → 시장가 매도(kt10001)

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
from python_app.models.types import (OrderSide, OrderStatus, OrderType, Position, Signal, StrategyConfig,
                                     TradeRecord, TradingMode)
from python_app.strategy.combiner import EntryCombiner
from python_app.strategy.exit_rules import check_exit, trade_config

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
    strategy: str = ""                # 진입 신호를 낸 전략 키
    order_type: str = "MARKET"        # 매수 주문 방식 (LIMIT = SMC 지정가)
    stop_price: float = 0.0           # 전략 손절가 (0 = 매입가 × (1 - stop_loss_rate%))
    target_price: float = 0.0         # 전략 목표가 (0 = 없음)
    use_trailing: bool = True
    expire_at: str = ""               # 지정가 매수 유효 시각 (ISO)
    order_qty: int = 0                # 매수 주문 수량
    buy_open: bool = False            # 지정가 매수 잔량이 아직 살아 있음 (부분 체결)
    buy_order_id: str = ""            # 매수 주문번호 (order_id는 매도 주문 시 덮어씀)
    buy_filled_at: str = ""           # 첫 매수 체결 확인 시각 (ISO)
    stop_rate: float = 0.0            # 전략 손절 % — 체결가로 stop_price 계산 (0 = 사용 안 함)
    trail_retrace_rate: float = 0.0   # 전략 트레일링 되돌림 % (0 = 전역 설정)
    trail_activation_rate: float = 0.0

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
        screener=None,
    ):
        self.broker = broker
        self.config = config
        self.watchlist = list(watchlist)
        self.clock = clock
        self.settings = settings or EngineSettings()
        self.on_event = on_event
        self.notifier = notifier          # TelegramNotifier (notify(text)) 또는 None
        self._push_seen: Dict[str, datetime] = {}
        self._summary_sent_date = ""
        self.db = db
        self.screener = screener          # AccumulationScreener — 있으면 매일 매집 종목으로 감시종목 자동 교체
        self.screen_info: Dict[str, Dict] = {}
        self.screen_status = ""
        self._screen_date = ""
        self._screen_thread: Optional[threading.Thread] = None
        self.strategy = EntryCombiner(config, prev_high_provider=self._prev_day_high)
        self.state_path = os.path.join(state_dir, f"live_state_{broker.mode.value}.json")

        self.running = False
        self.halted_reason = ""
        self.trade_date = ""
        self.managed: Dict[str, ManagedTrade] = {}
        self.realized_pnl = 0.0
        self.cash = 0.0
        self.positions: Dict[str, Position] = {}
        self.last_quotes: Dict[str, float] = {}
        self.last_bar_close: Dict[str, float] = {}
        self.signals: List[Signal] = []

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
        managed = {k: ManagedTrade(**v) for k, v in (data.get("managed") or {}).items()}
        for v in managed.values():   # buy_order_id 없던 시절의 상태 파일
            if not v.buy_order_id and v.status in ("BUYING", "HOLDING"):
                v.buy_order_id = v.order_id
        if data.get("trade_date") == today:
            self.managed = managed
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
        if self.screener:
            self.watchlist = []           # 오늘 매집 스캔이 끝나기 전에는 신규 매수 대상 없음
            self.screen_status = "매집 스캔 대기"
        self.running = True
        held = [m.name for m in self.managed.values() if m.status == "HOLDING"]
        target = "감시종목: 전일 매집 종목 자동 스캔" if self.screener else f"감시 {len(self.watchlist)}종목"
        self._emit("info", f"▶ 자동매매 시작 [{self.broker.mode_label}] {target} · "
                           f"매수 전략: {self.strategy.describe()} · "
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
            self._maybe_start_screen(now)
            if t >= dtime(15, 31) and self._summary_sent_date != today:
                self._summary_sent_date = today
                self._sync_account()
                self._daily_summary()
            if t < MARKET_OPEN or t > dtime(15, 30):
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
        self.managed = {k: v for k, v in self.managed.items() if v.status in ("HOLDING", "SELLING")}
        for v in self.managed.values():
            v.status = "HOLDING"
            v.carried_over = True
        self._eod_logged = False
        self._last_fed.clear()
        self._last_bar_poll_idx = -1
        self.signals.clear()
        if self.screener:
            self.watchlist = []
            self.screen_info = {}
            self.screen_status = "매집 스캔 대기"
        self._save_state()

    # ================================================================ 매집 종목 스캔
    def _maybe_start_screen(self, now: datetime):
        if not self.screener or self._screen_date == now.strftime("%Y%m%d"):
            return
        if now.time() < dtime(8, 0) or now.time() > self.strategy.last_entry_time():
            return
        self._screen_date = now.strftime("%Y%m%d")
        self._start_screen_thread(now, use_cache=True)

    def request_rescan(self) -> bool:
        """GUI [지금 스캔] — 캐시 무시하고 다시 스캔"""
        if not self.screener or (self._screen_thread and self._screen_thread.is_alive()):
            return False
        self._screen_date = self.clock().strftime("%Y%m%d")
        self._start_screen_thread(self.clock(), use_cache=False)
        return True

    def _start_screen_thread(self, now: datetime, use_cache: bool):
        self.screen_status = "매집 스캔 중…"
        self._emit("info", "🔍 전일 매집 종목 스캔 시작 (코스피·코스닥 전체)")
        # 백그라운드 실행 → 스캔(최대 1분) 동안에도 손절·트레일링 감시는 계속
        self._screen_thread = threading.Thread(target=self._run_screen, args=(now, use_cache),
                                               name="screener", daemon=True)
        self._screen_thread.start()

    def _run_screen(self, now: datetime, use_cache: bool):
        try:
            res = self.screener.run(now, use_cache=use_cache)
        except Exception as e:
            logger.exception("매집 스캔 오류")
            with self._lock:
                self.screen_status = "매집 스캔 실패"
            self._emit("error", f"⚠ 매집 스캔 실패: {e} — 오늘 신규 매수 없음 ([지금 스캔]으로 재시도 가능)")
            return
        with self._lock:
            self.watchlist = res.watchlist()
            self.screen_info = {h.symbol: {"reasons": h.reasons, "detail": h.detail} for h in res.hits}
            self.screen_status = f"매집 {len(res.hits)}종목 (기준일 {res.prev_date})"
        if res.hits:
            names = ", ".join(f"{h.name}[{'+'.join(h.reasons)}]" for h in res.hits[:15])
            more = f" 외 {len(res.hits) - 15}" if len(res.hits) > 15 else ""
            self._emit("info", f"🔍 오늘 감시종목 {len(res.hits)}개 (전일 {res.prev_date} 매집): {names}{more}", push=True)
        elif res.errors:
            self._emit("error", f"⚠ 매집 스캔 실패 ({res.errors[0]}) — 오늘 신규 매수 없음")
        else:
            self._emit("warning", f"🔍 전일({res.prev_date}) 매집 조건을 만족한 종목이 없습니다 — 오늘 신규 매수 없음",
                       push=True)

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
            m.stop_price = round(m.entry_price * (1.0 - m.stop_rate / 100.0))

    def _close_trade(self, m: ManagedTrade, manual: bool = False):
        px = m.exit_price or self.last_quotes.get(m.symbol, m.entry_price)
        gross = (px - m.entry_price) * m.qty
        fees = (px + m.entry_price) * m.qty * FEE_RATE + px * m.qty * TAX_RATE
        m.realized_pnl = gross - fees
        m.status = "CLOSED"
        self.realized_pnl += m.realized_pnl
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
                          datetime.fromisoformat(m.buy_filled_at), reason=m.strategy,
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
                executed_at=at, reason=reason))
        except Exception as e:
            logger.error("체결 기록 오류 (%s %s): %s", m.name, side.value, e)

    def _record_cancel(self, order_id: str):
        if not self.db or not order_id:
            return
        try:
            self.db.update_order_fill(order_id, OrderStatus.CANCELLED.value)
        except Exception as e:
            logger.error("주문 취소 기록 오류 #%s: %s", order_id, e)

    def _save_daily_pnl(self):
        """당일 청산 거래 요약을 DB에 기록 → GUI '일별 누적수익' 탭"""
        if not self.db or not self.trade_date:
            return
        closed = [m for m in self.managed.values() if m.status == "CLOSED"]
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
            if m.stop_price > 0:
                pos.stop_loss_price = m.stop_price

            if m.carried_over and self.config.carryover_sell_at_open:
                self._sell(m, pos, price, "전일 이월 종목 시초 청산 (전날 장 마감 청산 누락)", now)
                continue

            config = trade_config(self.config, m.trail_retrace_rate, m.trail_activation_rate)
            decision = check_exit(pos, price, config, take_profit_price=m.target_price,
                                  use_trailing=m.use_trailing,
                                  stop_label="(전략 손절가)" if m.stop_price > 0 else "")
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
        targets = [(s, m) for s, m in self.managed.items() if m.status == "HOLDING" and s in self.positions]
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
    def _prev_day_high(self, symbol: str, trade_date: str) -> Optional[float]:
        """D. 전일 고가 돌파용 — 일봉(ka10081)에서 trade_date 직전 거래일 고가 (전략이 종목·날짜별로 캐시)"""
        rows = [r for r in self.broker.get_daily_bars(symbol, trade_date) if r["dt"] < trade_date]
        return rows[-1]["high"] if rows else None

    def _bar_index(self, now: datetime) -> int:
        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        return int((now - start).total_seconds() // 180)

    def _poll_bars_if_due(self, now: datetime):
        end_t = self.strategy.last_entry_time()
        if now.time() > (datetime.combine(now.date(), end_t) + timedelta(minutes=4)).time():
            return
        idx = self._bar_index(now)
        boundary = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(minutes=3 * idx)
        if idx <= self._last_bar_poll_idx:
            return
        if self._last_bar_poll_idx >= 0 and (now - boundary).total_seconds() < self.settings.bar_poll_delay_sec:
            return
        if idx < 1 and self._last_bar_poll_idx >= 0:
            return
        self._last_bar_poll_idx = idx

        for sym, name in self.watchlist:
            try:
                bars = self.broker.get_minute_bars(sym, self.trade_date, now, tic=3)
            except Exception as e:
                self._emit("warning", f"[{name}] 3분봉 조회 실패: {e}")
                continue
            if bars:
                self.last_bar_close[sym] = bars[-1].close
            last = self._last_fed.get(sym)
            for bar in bars:
                if not bar.is_completed or (last and bar.timestamp <= last):
                    continue
                self._last_fed[sym] = bar.timestamp
                sig = self.strategy.process_bar(bar, name)
                if sig:
                    self._on_signal(sig, now)

    def _on_signal(self, sig: Signal, now: datetime):
        self.signals.append(sig)
        if self.db:
            try:
                self.db.save_signal(sig)
            except Exception:
                pass
        age = (now - sig.timestamp).total_seconds()
        if age > self.settings.stale_signal_sec:
            self._emit("info", f"○ 과거 신호 기록만 함 (진입 안 함): {sig.name} {sig.timestamp:%H:%M} — {sig.reason}")
            return
        self._emit("info", f"★ 매수 신호 {sig.name} {sig.timestamp:%H:%M} — {sig.reason}")
        self._try_buy(sig, now)

    def _active_count(self) -> int:
        return sum(1 for m in self.managed.values() if m.status in ("BUYING", "HOLDING", "SELLING"))

    def _check_daily_loss(self) -> bool:
        unreal = 0.0
        for m in self.managed.values():
            if m.status == "HOLDING" and m.symbol in self.last_quotes:
                unreal += (self.last_quotes[m.symbol] - m.entry_price) * m.qty
        if self.realized_pnl + unreal <= -abs(self.config.max_daily_loss):
            if not self.halted_reason:
                self.halted_reason = (f"일 손실한도 도달 ({self.realized_pnl + unreal:+,.0f}원 ≤ "
                                      f"-{self.config.max_daily_loss:,}원) → 신규 매수 중단")
                self._emit("warning", "⛔ " + self.halted_reason, push=True)
            return False
        return True

    def _try_buy(self, sig: Signal, now: datetime):
        sym = sig.symbol
        skip = None
        if now.time() > LAST_ORDER_TIME:
            skip = "장 마감 임박"
        elif self._eod_due(now):
            skip = f"장 마감 청산 시각({self.config.eod_liquidation_time}) 이후"
        elif self.halted_reason or not self._check_daily_loss():
            skip = self.halted_reason
        elif sym in self.positions:
            skip = "이미 계좌에 보유 중인 종목"
        elif sym in self.managed and self.managed[sym].status in ("BUYING", "HOLDING", "SELLING"):
            skip = "이미 진행 중인 주문/보유"
        elif (sym in self.managed and self.managed[sym].status == "CLOSED"
              and not self.config.allow_reentry_same_day):
            skip = "당일 이미 매매한 종목 (재진입 금지)"
        elif sig.expire_at and now >= sig.expire_at:
            skip = "지정가 유효시각 경과"
        elif self._active_count() >= self.config.max_concurrent_positions:
            skip = f"최대 동시 보유 {self.config.max_concurrent_positions}종목 도달"
        if skip:
            self._emit("info", f"  └ 진입 생략 ({sig.name}): {skip}")
            return

        self.cash = self.broker.get_cash_balance()
        budget = min(float(self.config.order_amount_per_trade), self.cash)
        qty = int(budget // (sig.price * (1 + FEE_RATE))) if sig.price > 0 else 0
        if qty < 1:
            self._emit("warning", f"  └ 진입 생략 ({sig.name}): 주문가능금액 부족 ({self.cash:,.0f}원)", push=True)
            return

        is_limit = sig.order_type == OrderType.LIMIT
        order = self.broker.submit_order(sym, sig.name, OrderSide.BUY, qty, price=sig.price,
                                         order_type=sig.order_type, reason=sig.reason)
        if not order:
            self._emit("error", f"  └ 매수 주문 실패 ({sig.name}) — 로그 확인")
            return
        self.managed[sym] = ManagedTrade(
            symbol=sym, name=sig.name, status="BUYING", order_id=order.order_id, buy_order_id=order.order_id,
            order_time=now.isoformat(), qty=qty, highest_price=sig.price, signal_price=sig.price,
            strategy=sig.strategy, order_type=sig.order_type.value,
            stop_price=sig.stop_loss_price if is_limit else 0.0,   # 시장가 전략은 기존대로 매입가 기준 -N%
            target_price=sig.take_profit_price, use_trailing=sig.use_trailing,
            expire_at=sig.expire_at.isoformat() if sig.expire_at else "", order_qty=qty,
            stop_rate=sig.stop_rate, trail_retrace_rate=sig.trail_retrace_rate,
            trail_activation_rate=sig.trail_activation_rate,
        )
        if self.db:
            self.db.save_order(order)
        self._save_state()
        self._last_sync = datetime.min
        how = (f"지정가 {sig.price:,.0f}원, {sig.expire_at:%H:%M}까지" if is_limit and sig.expire_at
               else f"시장가, 신호가 {sig.price:,.0f}원")
        self._emit("info", f"📈 매수 주문 {sig.name} {qty}주 ({how})\n{sig.reason}", push=True)

    def _daily_summary(self):
        closed = [m for m in self.managed.values() if m.status == "CLOSED"]
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
                st = self.strategy.symbol_states.get(sym)
                states[sym] = {
                    "breakout_level": st.breakout_level if st else None,
                    "first_bar_volume": st.first_bar_volume if st else None,
                    "signal": st.signal_fired_today if st else False,
                }
            return {
                "watchlist": list(self.watchlist),
                "screen": dict(self.screen_info),
                "screen_status": self.screen_status,
                "mode": self.broker.mode_label,
                "running": self.running,
                "halted": self.halted_reason,
                "cash": self.cash,
                "realized_pnl": self.realized_pnl,
                "positions": {k: asdict(v) for k, v in self.positions.items()},
                "managed": {k: asdict(v) for k, v in self.managed.items()},
                "quotes": {**self.last_bar_close, **self.last_quotes},
                "states": states,
            }
