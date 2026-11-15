"""
Korea3MinBreakoutTrader - 키움증권 REST API 브로커

서버
  - 모의거래(MOCK): https://mockapi.kiwoom.com   (모의투자용 앱키 필요)
  - 실거래 (REAL): https://api.kiwoom.com        (실거래용 앱키 필요)
  ※ 키움은 모의/실거래 앱키가 따로 발급됩니다. 섞어 쓰면 인증 오류가 납니다.

사용 TR (모두 POST, 기능은 'api-id' 헤더로 구분)
  au10001  /oauth2/token        접근토큰 발급
  ka10001  /api/dostk/stkinfo   주식기본정보 (현재가·시가총액 mac)
  ka10080  /api/dostk/chart     주식분봉차트 (tic_scope=3 → 3분봉)
  kt10000  /api/dostk/ordr      현금 매수
  kt10001  /api/dostk/ordr      현금 매도
  kt10003  /api/dostk/ordr      취소
  kt00001  /api/dostk/acnt      예수금상세 (ord_alow_amt = 주문가능금액, entr = 예수금)
  kt00018  /api/dostk/acnt      계좌평가잔고 (보유종목)
  ka10075  /api/dostk/acnt      미체결
  ka10030  /api/dostk/rkinfo    당일 거래량·거래대금 상위 (실시간 후보 탐색)
  ka10027  /api/dostk/rkinfo    전일대비 등락률 상위 (실시간 후보 탐색)
  ka10081  /api/dostk/chart     주식일봉차트
  ka20001  /api/dostk/sect      업종현재가 (코스피 001 · 코스닥 101 지수 등락률)

응답 규칙
  - HTTP 200이어도 body.return_code != 0 이면 업무 오류
  - 숫자는 문자열 + 부호(+/-) + 0패딩 → _num() 으로 통일 변환
  - 잔고 종목코드는 'A005930' 형식 → 주문은 6자리 '005930'
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from python_app.broker.base import BrokerBase
from python_app.models.types import (
    Bar, Order, OrderSide, OrderStatus, OrderType, Position, Quote, StrategyConfig, TradingMode,
)
from python_app.strategy.exit_rules import stop_loss_price

logger = logging.getLogger("Korea3MinBreakoutTrader.KiwoomRest")

BASE_URLS = {
    TradingMode.MOCK: "https://mockapi.kiwoom.com",
    TradingMode.REAL: "https://api.kiwoom.com",
}


class KiwoomApiError(Exception):
    def __init__(self, api_id: str, code: Any, msg: str, http_status: int = 200):
        super().__init__(f"[{api_id}] return_code={code} {msg} (HTTP {http_status})")
        self.api_id = api_id
        self.code = code
        self.msg = msg
        self.http_status = http_status


def _num(value: Any) -> float:
    """키움 숫자 문자열 → float. '+72000', '-000000071200', '1,234', '' 모두 처리 (부호 제거 = 절대값 아님에 주의)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _price(value: Any) -> float:
    """가격 필드: 부호는 '전일 대비 방향'이므로 절대값 사용"""
    return abs(_num(value))


def _code(stk_cd: str) -> str:
    """'A005930' → '005930', '005930_NX' → '005930'"""
    s = (stk_cd or "").strip()
    if len(s) == 7 and s[0].isalpha():
        s = s[1:]
    return s.split("_")[0]


class KiwoomRestBroker(BrokerBase):
    """키움증권 REST API 브로커 (모의거래 / 실거래 공용)"""

    TOKEN_PATH = "/oauth2/token"
    ORDER_PATH = "/api/dostk/ordr"
    ACCOUNT_PATH = "/api/dostk/acnt"
    STKINFO_PATH = "/api/dostk/stkinfo"
    CHART_PATH = "/api/dostk/chart"
    RANK_PATH = "/api/dostk/rkinfo"

    def __init__(
        self,
        app_key: str,
        secret_key: str,
        mode: TradingMode = TradingMode.MOCK,
        config: Optional[StrategyConfig] = None,
        exchange: str = "KRX",
        session: Optional[requests.Session] = None,
        min_interval: float = 0.25,
        base_url: str = "",
        account_min_interval: float = 1.05,
    ):
        self.app_key = (app_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.mode = TradingMode(mode)
        self.config = config or StrategyConfig()
        self.exchange = exchange
        self.base_url = base_url or BASE_URLS[self.mode]
        self.session = session or requests.Session()
        self.min_interval = min_interval
        # 계좌 TR(/api/dostk/acnt: kt00001·kt00018·ka10075) 유량제어 초당 1회 (2026-11-12 18시부터, 기존 초당 5회)
        # 대상 TR 묶음 기준인지 TR별 기준인지 알 수 없어 계좌 TR 전체를 합쳐 1초에 1회로 제한한다
        self.account_min_interval = account_min_interval
        self._last_account_call = 0.0

        self.access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self.positions: Dict[str, Position] = {}
        self.orders: Dict[str, Order] = {}
        self.names: Dict[str, str] = {}
        self._last_call = 0.0
        self._lock = threading.Lock()

    @property
    def is_real(self) -> bool:
        return self.mode == TradingMode.REAL

    # ================================================================ 통신 공통
    def _throttle(self, path: str = ""):
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call)
        if path == self.ACCOUNT_PATH:
            wait = max(wait, self.account_min_interval - (now - self._last_account_call))
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        if path == self.ACCOUNT_PATH:
            self._last_account_call = self._last_call

    def _post(
        self, path: str, api_id: str, body: Dict[str, Any],
        cont_key: str = "", auth: bool = True, _retry: int = 0,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """POST 호출 → (body, 응답헤더). 업무 오류는 KiwoomApiError."""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
        }
        if auth:
            if not self._ensure_token():
                raise KiwoomApiError(api_id, "TOKEN", "접근토큰 발급 실패")
            headers["authorization"] = f"Bearer {self.access_token}"
        if cont_key:
            headers["cont-yn"] = "Y"
            headers["next-key"] = cont_key

        with self._lock:
            self._throttle(path)
            resp = self.session.post(self.base_url + path, headers=headers, json=body, timeout=7)

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"return_code": "PARSE", "return_msg": resp.text[:200]}

        code = data.get("return_code", 0)
        rate_limited = resp.status_code == 429 or str(code) == "5"
        if rate_limited and _retry < 3:
            time.sleep(0.6 * (_retry + 1))
            return self._post(path, api_id, body, cont_key, auth, _retry + 1)

        # 토큰 만료/무효 → 1회 재발급 후 재시도
        if auth and resp.status_code in (401, 403) and _retry == 0:
            self.access_token = None
            return self._post(path, api_id, body, cont_key, auth, _retry + 1)

        if resp.status_code != 200 or str(code) not in ("0", ""):
            raise KiwoomApiError(api_id, code, str(data.get("return_msg", resp.text[:200])), resp.status_code)

        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        return data, resp_headers

    # ================================================================ 인증
    def connect(self) -> bool:
        """au10001 접근토큰 발급"""
        if not self.app_key or not self.secret_key:
            logger.warning("키움 %s 앱키/시크릿키가 비어 있습니다.", self.mode_label)
            return False
        try:
            data, _ = self._post(self.TOKEN_PATH, "au10001", {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.secret_key,
            }, auth=False)
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("키움 %s 토큰 발급 실패: %s", self.mode_label, e)
            return False

        self.access_token = data.get("token")
        self.token_expiry = self._parse_expiry(data.get("expires_dt"))
        if not self.access_token:
            logger.error("키움 토큰 응답에 token 필드가 없습니다: %s", data)
            return False
        logger.info("키움 %s 접속 성공 (토큰 만료: %s)", self.mode_label, self.token_expiry)
        return True

    @staticmethod
    def _parse_expiry(value: Any) -> datetime:
        s = str(value or "").strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt) - timedelta(minutes=10)
            except ValueError:
                continue
        return datetime.now() + timedelta(hours=12)

    def _ensure_token(self) -> bool:
        if self.access_token and self.token_expiry and datetime.now() < self.token_expiry:
            return True
        return self.connect()

    @property
    def mode_label(self) -> str:
        return "실거래" if self.is_real else "모의거래"

    # ================================================================ 시세
    def get_quote(self, symbol: str) -> Optional[Quote]:
        """ka10001 주식기본정보 → 현재가"""
        try:
            d, _ = self._post(self.STKINFO_PATH, "ka10001", {"stk_cd": symbol})
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("[%s] 현재가 조회 실패: %s", symbol, e)
            return None
        name = d.get("stk_nm", symbol)
        self.names[symbol] = name
        return Quote(
            symbol=symbol,
            name=name,
            current_price=_price(d.get("cur_prc")),
            open_price=_price(d.get("open_pric")),
            high_price=_price(d.get("high_pric")),
            low_price=_price(d.get("low_pric")),
            prev_close=_price(d.get("base_pric")),
            volume=int(abs(_num(d.get("trde_qty")))),
            change_rate=_num(d.get("flu_rt")),
            timestamp=datetime.now(),
        )

    def get_minute_bars(self, symbol: str, trade_date: str, now: datetime, tic: int = 3) -> List[Bar]:
        """
        ka10080 분봉 → 해당 거래일(YYYYMMDD) 봉만 시간순으로 반환.
        Bar.timestamp 는 '봉 마감 시각'(09:00~09:03 봉 → 09:03)으로 정규화하고,
        마감 시각이 지나지 않은 진행중 봉은 is_completed=False.
        """
        rows: List[Dict[str, Any]] = []
        cont = ""
        for _ in range(4):  # 3분봉 하루치 ≈ 130개, 넉넉히 4페이지
            d, h = self._post(self.CHART_PATH, "ka10080", {
                "stk_cd": symbol, "tic_scope": str(tic), "upd_stkpc_tp": "1",
            }, cont_key=cont)
            chunk = d.get("stk_min_pole_chart_qry") or []
            rows.extend(chunk)
            oldest = min((str(r.get("cntr_tm", "")) for r in chunk), default="")
            cont = h.get("next-key", "") if h.get("cont-yn") == "Y" else ""
            if not chunk or not cont or oldest[:8] < trade_date:
                break

        today = []
        for r in rows:
            t = str(r.get("cntr_tm", ""))
            if t[:8] != trade_date:
                continue
            try:
                ts = datetime.strptime(t[:14], "%Y%m%d%H%M%S")
            except ValueError:
                continue
            today.append((ts, r))
        today.sort(key=lambda x: x[0])
        if not today:
            return []

        # 봉 시각 표기 방식 판별: 첫 봉이 09:00 이면 '시작 시각' 표기 → +tic 분
        first_ts = today[0][0]
        start_labeled = first_ts.hour == 9 and first_ts.minute == 0
        shift = timedelta(minutes=tic) if start_labeled else timedelta(0)

        bars: List[Bar] = []
        seen = set()
        for ts, r in today:
            end_ts = ts + shift
            if end_ts in seen:
                continue
            seen.add(end_ts)
            bars.append(Bar(
                symbol=symbol,
                timestamp=end_ts,
                open=_price(r.get("open_pric")),
                high=_price(r.get("high_pric")),
                low=_price(r.get("low_pric")),
                close=_price(r.get("cur_prc")),
                volume=int(abs(_num(r.get("trde_qty")))),
                is_completed=end_ts <= now,
            ))
        return bars

    # ================================================================ 순위 (실시간 후보 탐색)
    @staticmethod
    def _rank_row(it: Dict[str, Any], volume_key: str) -> Optional[Dict[str, Any]]:
        code = _code(it.get("stk_cd", ""))
        if not code:
            return None
        price = _price(it.get("cur_prc"))
        volume = int(abs(_num(it.get(volume_key))))
        amount = abs(_num(it.get("trde_amt"))) * 1_000_000      # 백만원 단위. 없는 TR 은 현재가 × 거래량으로 근사
        return {"symbol": code, "name": str(it.get("stk_nm", code)).strip(), "price": price,
                "change_rate": _num(it.get("flu_rt")), "volume": volume,
                "trade_value": amount or price * volume}

    def get_today_rank(self, sort_tp: str = "3", market: str = "000") -> List[Dict[str, Any]]:
        """ka10030 당일 거래 상위 100 (sort_tp 1=거래량, 3=거래대금 / market 000=전체, 001=코스피, 101=코스닥)
        → [{symbol, name, price, change_rate(%), volume, trade_value(원)}]"""
        d, _ = self._post(self.RANK_PATH, "ka10030", {
            "mrkt_tp": market, "sort_tp": sort_tp, "mang_stk_incls": "0", "crd_tp": "0", "trde_qty_tp": "0",
            "pric_tp": "0", "trde_prica_tp": "0", "mrkt_open_tp": "0", "stex_tp": "1",
        })
        rows = (self._rank_row(it, "trde_qty") for it in d.get("tdy_trde_qty_upper") or [])
        return [r for r in rows if r]

    def get_change_rank(self, market: str = "000") -> List[Dict[str, Any]]:
        """ka10027 전일대비 등락률 상위 (상승률순) → get_today_rank 와 같은 형식 (거래대금은 현재가 × 거래량 근사)"""
        d, _ = self._post(self.RANK_PATH, "ka10027", {
            "mrkt_tp": market, "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "0", "crd_cnd": "0",
            "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "1",
        })
        rows = (self._rank_row(it, "now_trde_qty") for it in d.get("pred_pre_flu_rt_upper") or [])
        return [r for r in rows if r]

    def get_index(self, market: str = "0", code: str = "001") -> Dict[str, float]:
        """ka20001 업종현재가 → {price, change_rate(%)}. 코스피 = ("0", "001"), 코스닥 = ("1", "101")"""
        d, _ = self._post("/api/dostk/sect", "ka20001", {"mrkt_tp": market, "inds_cd": code})
        return {"price": _price(d.get("cur_prc")), "change_rate": _num(d.get("flu_rt"))}

    def get_stock_basic(self, symbol: str) -> Dict[str, Any]:
        """ka10001 → 현재가(장 전에는 전일 종가)·시가총액(원)"""
        d, _ = self._post(self.STKINFO_PATH, "ka10001", {"stk_cd": symbol})
        mac = abs(_num(d.get("mac")))
        # 'mac' 단위 판별: 상장사 시총은 최소 수십억 원 → 1e9 미만이면 '억원' 단위 값
        mac_won = mac * 100_000_000 if 0 < mac < 1e9 else mac
        return {"symbol": symbol, "name": str(d.get("stk_nm", symbol)).strip(),
                "price": _price(d.get("cur_prc")), "market_cap": mac_won}

    def get_daily_bars(self, symbol: str, base_dt: str, pages: int = 1) -> List[Dict[str, Any]]:
        """ka10081 일봉 → 오래된 날짜부터 정렬된 [{dt, open, high, low, close, volume}] (기본 첫 페이지만)"""
        bars: Dict[str, Dict[str, Any]] = {}
        cont = ""
        for _ in range(pages):
            d, h = self._post(self.CHART_PATH, "ka10081", {
                "stk_cd": symbol, "base_dt": base_dt, "upd_stkpc_tp": "1",
            }, cont_key=cont)
            for r in d.get("stk_dt_pole_chart_qry") or []:
                dt = str(r.get("dt", "")).strip()
                if len(dt) == 8:
                    bars[dt] = self._chart_row(dt, r)
            cont = h.get("next-key", "") if h.get("cont-yn") == "Y" else ""
            if not cont:
                break
        return [bars[dt] for dt in sorted(bars)]

    def get_chart_minute_bars(self, symbol: str, tic: int, want: int = 200) -> List[Dict[str, Any]]:
        """ka10080 분봉 → 차트 표시용 (날짜 구분 없이 최근 want 개 이상, dt = YYYYMMDDHHMMSS, 오래된 순).
        매매 판단용 get_minute_bars 는 당일 봉만 돌려줘 장 전에는 비므로 별도로 둔다."""
        bars: Dict[str, Dict[str, Any]] = {}
        cont = ""
        for _ in range(4):
            d, h = self._post(self.CHART_PATH, "ka10080", {
                "stk_cd": symbol, "tic_scope": str(tic), "upd_stkpc_tp": "1",
            }, cont_key=cont)
            for r in d.get("stk_min_pole_chart_qry") or []:
                dt = str(r.get("cntr_tm", "")).strip()[:14]
                if len(dt) == 14:
                    bars[dt] = self._chart_row(dt, r)
            cont = h.get("next-key", "") if h.get("cont-yn") == "Y" else ""
            if not cont or len(bars) >= want:
                break
        return [bars[dt] for dt in sorted(bars)]

    @staticmethod
    def _chart_row(dt: str, r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dt": dt,
            "open": _price(r.get("open_pric")),
            "high": _price(r.get("high_pric")),
            "low": _price(r.get("low_pric")),
            "close": _price(r.get("cur_prc")),
            "volume": int(abs(_num(r.get("trde_qty")))),
        }

    # ================================================================ 계좌
    def get_cash_balance(self) -> float:
        """kt00001 예수금상세 → 주문가능금액(ord_alow_amt)과 예수금(entr) 중 작은 값.
        주문가능금액은 미수·증거금 설정에 따라 예수금보다 클 수 있어 예수금을 넘겨 사지 않게 한다.
        entr 이 없으면 0 = 매수 안 함."""
        try:
            d, _ = self._post(self.ACCOUNT_PATH, "kt00001", {"qry_tp": "3"})
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("예수금 조회 실패: %s", e)
            return 0.0
        return min(_num(d.get("ord_alow_amt")), _num(d.get("entr")))

    def get_positions(self) -> Dict[str, Position]:
        """kt00018 계좌평가잔고 → 보유종목 (기존 최고가는 유지)"""
        try:
            d, _ = self._post(self.ACCOUNT_PATH, "kt00018", {"qry_tp": "1", "dmst_stex_tp": self.exchange})
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("잔고 조회 실패: %s", e)
            return self.positions

        fresh: Dict[str, Position] = {}
        for it in d.get("acnt_evlt_remn_indv_tot") or []:
            qty = int(_num(it.get("rmnd_qty")))
            if qty <= 0:
                continue
            sym = _code(it.get("stk_cd", ""))
            avg = _price(it.get("pur_pric"))
            cur = _price(it.get("cur_prc"))
            prev = self.positions.get(sym)
            self.names[sym] = it.get("stk_nm", sym)
            fresh[sym] = Position(
                symbol=sym,
                name=self.names[sym],
                qty=qty,
                avg_price=avg,
                current_price=cur,
                stop_loss_price=stop_loss_price(avg, self.config),
                take_profit_price=(
                    avg * (1.0 + self.config.take_profit_rate / 100.0)
                    if self.config.take_profit_rate > 0 else 0.0
                ),
                entry_time=prev.entry_time if prev else datetime.now(),
                highest_price=max(prev.highest_price if prev else 0.0, avg, cur),
                sellable_qty=int(_num(it.get("trde_able_qty"))),
            )
        self.positions = fresh
        return self.positions

    def get_unfilled_orders(self) -> List[Dict[str, Any]]:
        """ka10075 미체결 목록"""
        try:
            d, _ = self._post(self.ACCOUNT_PATH, "ka10075", {
                "all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0", "stk_cd": "",
            })
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("미체결 조회 실패: %s", e)
            return []
        out = []
        for it in d.get("oso") or []:
            out.append({
                "ord_no": str(it.get("ord_no", "")).strip(),
                "symbol": _code(it.get("stk_cd", "")),
                "side": it.get("io_tp_nm", ""),
                "ord_qty": int(_num(it.get("ord_qty"))),
                "oso_qty": int(_num(it.get("oso_qty"))),
                "cntr_qty": int(_num(it.get("cntr_qty"))),
            })
        return out

    # ================================================================ 주문
    def submit_order(
        self,
        symbol: str,
        name: str,
        side: OrderSide,
        qty: int,
        price: float = 0.0,
        order_type: OrderType = OrderType.MARKET,
        reason: str = "",
    ) -> Optional[Order]:
        """kt10000(매수) / kt10001(매도). 시장가: trde_tp=3, ord_uv 빈값 / 지정가: trde_tp=0"""
        if qty <= 0:
            logger.warning("[%s] 주문 수량 0 이하 → 주문 안 함", symbol)
            return None
        api_id = "kt10000" if side == OrderSide.BUY else "kt10001"
        is_market = order_type == OrderType.MARKET
        body = {
            "dmst_stex_tp": self.exchange,
            "stk_cd": symbol,
            "ord_qty": str(int(qty)),
            "ord_uv": "" if is_market else str(int(price)),
            "trde_tp": "3" if is_market else "0",
            "cond_uv": "",
        }
        now = datetime.now()
        try:
            d, _ = self._post(self.ORDER_PATH, api_id, body)
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("★ [키움 %s 주문 거절] %s %s %d주: %s", self.mode_label, side.value, symbol, qty, e)
            return None

        ord_no = str(d.get("ord_no", "")).strip() or f"KIW_{int(now.timestamp())}"
        order = Order(
            order_id=ord_no, symbol=symbol, name=name, side=side, order_type=order_type,
            price=price, qty=qty, status=OrderStatus.SUBMITTED, created_at=now, reason=reason,
        )
        self.orders[ord_no] = order
        logger.info("★ [키움 %s 주문 접수] #%s %s %s %d주 (%s)", self.mode_label, ord_no, side.value, symbol, qty, reason)
        return order

    def cancel_order(self, order_id: str) -> bool:
        """kt10003 취소 (cncl_qty='0' → 잔량 전부)"""
        order = self.orders.get(order_id)
        if not order:
            logger.warning("취소할 주문 정보가 없습니다: %s", order_id)
            return False
        try:
            self._post(self.ORDER_PATH, "kt10003", {
                "dmst_stex_tp": self.exchange,
                "orig_ord_no": order_id,
                "stk_cd": order.symbol,
                "cncl_qty": "0",
            })
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("주문 취소 실패 #%s: %s", order_id, e)
            return False
        order.status = OrderStatus.CANCELLED
        logger.info("주문 취소 완료 #%s", order_id)
        return True
