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
  kt00001  /api/dostk/acnt      예수금상세 (ord_alow_amt = 주문가능금액)
  kt00018  /api/dostk/acnt      계좌평가잔고 (보유종목)
  ka10075  /api/dostk/acnt      미체결
  ka10031  /api/dostk/rkinfo    전일거래량·거래대금 상위 100 (매집 스캔 후보)
  ka10030  /api/dostk/rkinfo    당일거래량·거래대금 상위 (18시 매집 스캔 후보)
  ka10081  /api/dostk/chart     주식일봉차트 (매집 ① 판정)
  ka90009  /api/dostk/rkinfo    외국인·기관 매매 상위 (매집 ② 판정)

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
    ):
        self.app_key = (app_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.mode = TradingMode(mode)
        self.config = config or StrategyConfig()
        self.exchange = exchange
        self.base_url = base_url or BASE_URLS[self.mode]
        self.session = session or requests.Session()
        self.min_interval = min_interval

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
    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

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
            self._throttle()
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

    # ================================================================ 매집 스캔용 시세
    def get_prev_day_top(self, qry_tp: str = "1", market: str = "000", top: int = 100) -> List[Dict[str, str]]:
        """ka10031 전일 상위 100 (qry_tp 1=거래량, 2=거래대금 / market 000=전체, 001=코스피, 101=코스닥)"""
        d, _ = self._post(self.RANK_PATH, "ka10031", {
            "mrkt_tp": market, "qry_tp": qry_tp, "rank_strt": "0", "rank_end": str(top), "stex_tp": "1",
        })
        out = []
        for it in d.get("pred_trde_qty_upper") or []:
            code = _code(it.get("stk_cd", ""))
            if code:
                out.append({"symbol": code, "name": str(it.get("stk_nm", code)).strip()})
        return out

    def get_today_top(self, sort_tp: str = "1", market: str = "000", top: int = 100) -> List[Dict[str, str]]:
        """ka10030 당일 거래 상위 (sort_tp 1=거래량, 3=거래대금) — 장 마감 후(18시) 매집 스캔 후보"""
        d, _ = self._post(self.RANK_PATH, "ka10030", {
            "mrkt_tp": market, "sort_tp": sort_tp, "mang_stk_incls": "0", "crd_tp": "0", "trde_qty_tp": "0",
            "pric_tp": "0", "trde_prica_tp": "0", "mrkt_open_tp": "0", "stex_tp": "1",
        })
        out = []
        for it in (d.get("tdy_trde_qty_upper") or [])[:top]:
            code = _code(it.get("stk_cd", ""))
            if code:
                out.append({"symbol": code, "name": str(it.get("stk_nm", code)).strip()})
        return out

    def get_stock_basic(self, symbol: str) -> Dict[str, Any]:
        """ka10001 → 현재가(장 전에는 전일 종가)·시가총액(원)"""
        d, _ = self._post(self.STKINFO_PATH, "ka10001", {"stk_cd": symbol})
        mac = abs(_num(d.get("mac")))
        # 'mac' 단위 판별: 상장사 시총은 최소 수십억 원 → 1e9 미만이면 '억원' 단위 값
        mac_won = mac * 100_000_000 if 0 < mac < 1e9 else mac
        return {"symbol": symbol, "name": str(d.get("stk_nm", symbol)).strip(),
                "price": _price(d.get("cur_prc")), "market_cap": mac_won}

    def get_daily_bars(self, symbol: str, base_dt: str) -> List[Dict[str, Any]]:
        """ka10081 일봉 → 오래된 날짜부터 정렬된 [{dt, open, high, low, close, volume}] (첫 페이지만)"""
        d, _ = self._post(self.CHART_PATH, "ka10081", {
            "stk_cd": symbol, "base_dt": base_dt, "upd_stkpc_tp": "1",
        })
        bars = []
        for r in d.get("stk_dt_pole_chart_qry") or []:
            dt = str(r.get("dt", "")).strip()
            if len(dt) != 8:
                continue
            bars.append({
                "dt": dt,
                "open": _price(r.get("open_pric")),
                "high": _price(r.get("high_pric")),
                "low": _price(r.get("low_pric")),
                "close": _price(r.get("cur_prc")),
                "volume": int(abs(_num(r.get("trde_qty")))),
            })
        bars.sort(key=lambda b: b["dt"])
        return bars

    def get_foreign_inst_top(self, date: str, market: str = "000") -> Dict[str, List[Dict[str, Any]]]:
        """ka90009 외국인·기관 매매 상위 (금액 기준, 단위: 천만원) → {"foreign": [...], "inst": [...]} 순매수 순위순"""
        rows: List[Dict[str, Any]] = []
        cont = ""
        for _ in range(3):
            d, h = self._post(self.RANK_PATH, "ka90009", {
                "mrkt_tp": market, "amt_qty_tp": "1", "qry_dt_tp": "1", "date": date, "stex_tp": "1",
            }, cont_key=cont)
            rows.extend(d.get("frgnr_orgn_trde_upper") or [])
            cont = h.get("next-key", "") if h.get("cont-yn") == "Y" else ""
            if not cont:
                break
        out: Dict[str, List[Dict[str, Any]]] = {"foreign": [], "inst": []}
        for r in rows:
            for key, pre in (("foreign", "for_netprps"), ("inst", "orgn_netprps")):
                code = _code(r.get(f"{pre}_stk_cd", ""))
                if code:
                    out[key].append({"symbol": code, "name": str(r.get(f"{pre}_stk_nm", code)).strip(),
                                     "amount": abs(_num(r.get(f"{pre}_amt"))) * 10_000_000})
        return out

    # ================================================================ 계좌
    def get_cash_balance(self) -> float:
        """kt00001 예수금상세 → 주문가능금액(ord_alow_amt)"""
        try:
            d, _ = self._post(self.ACCOUNT_PATH, "kt00001", {"qry_tp": "3"})
        except (KiwoomApiError, requests.RequestException) as e:
            logger.error("예수금 조회 실패: %s", e)
            return 0.0
        return _num(d.get("ord_alow_amt"))

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
