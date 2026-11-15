"""
테스트용 가짜 키움 REST 서버 (requests.Session 대체)
api-id 헤더로 TR을 분기하고, 실제 키움과 같은 형식(문자열·부호·A접두 코드)으로 응답한다.
"""

import json
from datetime import datetime
from typing import Dict, List, Optional


class FakeResp:
    def __init__(self, status: int, body: dict, headers: Optional[dict] = None):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeKiwoom:
    def __init__(self, cash: int = 10_000_000):
        self.calls: List[dict] = []
        self.prices: Dict[str, int] = {}
        self.names: Dict[str, str] = {}
        self.bars: Dict[str, List[dict]] = {}       # 최신순 (키움과 동일)
        self.holdings: Dict[str, dict] = {}         # sym -> {qty, avg}
        self.cash = cash
        self.orders: List[dict] = []
        self.auto_fill = True
        self.unfilled: Dict[str, dict] = {}
        self.fail_token = False
        self._ord_seq = 1000
        # 후보 탐색용 순위
        self.today_top: Dict[str, List[dict]] = {"1": [], "3": []}  # ka10030 sort_tp별 (1=거래량, 3=거래대금)
        self.change_top: List[dict] = []                           # ka10027 등락률 상위
        self.index_rates: Dict[str, str] = {"001": "+0.00", "101": "+0.00"}   # ka20001 지수 등락률
        self.daily: Dict[str, List[dict]] = {}                     # ka10081 sym → 최신순 일봉
        self.mac: Dict[str, str] = {}                              # ka10001 시가총액 (문자열 그대로)
        self.fail_apis = set()

    # ---------------------------------------------------------------- 시나리오 조작
    def add_bar(self, sym: str, ts: str, o: int, h: int, l: int, c: int, v: int):
        sign = lambda x: f"+{x}" if x >= 0 else str(x)
        self.bars.setdefault(sym, []).insert(0, {
            "cntr_tm": ts, "open_pric": sign(o), "high_pric": sign(h), "low_pric": sign(-l),
            "cur_prc": sign(c), "trde_qty": f"{v:015d}",
        })

    # ---------------------------------------------------------------- Session.post
    def post(self, url, headers=None, json=None, timeout=None):
        api = headers.get("api-id")
        self.calls.append({"url": url, "api": api, "headers": dict(headers), "body": dict(json or {})})
        body = json or {}
        if api == "au10001":
            if self.fail_token:
                return FakeResp(200, {"return_code": 3, "return_msg": "인증 실패"})
            return FakeResp(200, {"token": "TOKEN123", "expires_dt": "20991231235959", "token_type": "bearer",
                                  "return_code": 0, "return_msg": "정상"})
        if headers.get("authorization") != "Bearer TOKEN123":
            return FakeResp(401, {"return_code": 3, "return_msg": "토큰 없음"})

        if api in self.fail_apis:
            return FakeResp(200, {"return_code": 1, "return_msg": f"{api} 강제 실패"})
        if api == "ka10001":
            sym = body["stk_cd"]
            p = self.prices.get(sym, 0)
            return FakeResp(200, {"stk_nm": self.names.get(sym, sym), "cur_prc": f"-{p}", "open_pric": f"+{p}",
                                  "high_pric": f"+{p}", "low_pric": f"-{p}", "base_pric": str(p),
                                  "flu_rt": "-0.50", "trde_qty": "0000000012345",
                                  "mac": self.mac.get(sym, "0"), "return_code": 0})
        if api == "ka20001":
            return FakeResp(200, {"cur_prc": "+2500.00", "flu_rt": self.index_rates[body["inds_cd"]], "return_code": 0})
        if api == "ka10027":
            return FakeResp(200, {"pred_pre_flu_rt_upper": self.change_top, "return_code": 0}, {"cont-yn": "N"})
        if api == "ka10030":
            return FakeResp(200, {"tdy_trde_qty_upper": self.today_top.get(body["sort_tp"], []), "return_code": 0},
                            {"cont-yn": "N"})
        if api == "ka10081":
            return FakeResp(200, {"stk_cd": body["stk_cd"], "stk_dt_pole_chart_qry": self.daily.get(body["stk_cd"], []),
                                  "return_code": 0}, {"cont-yn": "N"})
        if api == "ka10080":
            return FakeResp(200, {"stk_cd": body["stk_cd"], "stk_min_pole_chart_qry": self.bars.get(body["stk_cd"], []),
                                  "return_code": 0}, {"cont-yn": "N", "next-key": ""})
        if api == "kt00001":
            return FakeResp(200, {"entr": f"{self.cash:015d}", "ord_alow_amt": f"{self.cash:015d}", "return_code": 0})
        if api == "kt00018":
            items = []
            for sym, h in self.holdings.items():
                items.append({"stk_cd": "A" + sym, "stk_nm": self.names.get(sym, sym),
                              "rmnd_qty": f"{h['qty']:015d}", "trde_able_qty": f"{h['qty']:015d}",
                              "pur_pric": f"{h['avg']:015d}", "cur_prc": f"{self.prices.get(sym, h['avg']):015d}"})
            return FakeResp(200, {"acnt_evlt_remn_indv_tot": items, "return_code": 0})
        if api == "ka10075":
            return FakeResp(200, {"oso": [{"ord_no": k, "stk_cd": v["sym"], "ord_qty": str(v["qty"]),
                                           "oso_qty": str(v["qty"]), "cntr_qty": "0", "io_tp_nm": v["side"]}
                                          for k, v in self.unfilled.items()], "return_code": 0})
        if api in ("kt10000", "kt10001"):
            sym, qty = body["stk_cd"], int(body["ord_qty"])
            self._ord_seq += 1
            no = f"{self._ord_seq:07d}"
            side = "BUY" if api == "kt10000" else "SELL"
            self.orders.append({"ord_no": no, "side": side, "sym": sym, "qty": qty, "body": dict(body)})
            if self.auto_fill:
                self._fill(sym, side, qty)
            else:
                self.unfilled[no] = {"sym": sym, "qty": qty, "side": "+매수" if side == "BUY" else "-매도"}
            return FakeResp(200, {"ord_no": no, "dmst_stex_tp": "KRX", "return_code": 0, "return_msg": "정상"})
        if api == "kt10003":
            self.unfilled.pop(body["orig_ord_no"], None)
            return FakeResp(200, {"ord_no": "9999999", "base_orig_ord_no": body["orig_ord_no"], "return_code": 0})
        return FakeResp(200, {"return_code": 1, "return_msg": f"unknown api {api}"})

    def _fill(self, sym: str, side: str, qty: int):
        px = self.prices[sym]
        if side == "BUY":
            h = self.holdings.get(sym, {"qty": 0, "avg": px})
            new_qty = h["qty"] + qty
            h["avg"] = int(round((h["avg"] * h["qty"] + px * qty) / new_qty))
            h["qty"] = new_qty
            self.holdings[sym] = h
            self.cash -= px * qty
        else:
            h = self.holdings[sym]
            h["qty"] -= qty
            self.cash += px * qty
            if h["qty"] <= 0:
                del self.holdings[sym]

    def add_daily(self, sym: str, dt: str, o: int, h: int, l: int, c: int, v: int):
        self.daily.setdefault(sym, []).insert(0, {
            "dt": dt, "open_pric": f"+{o}", "high_pric": f"+{h}", "low_pric": f"+{l}",
            "cur_prc": f"+{c}", "trde_qty": str(v)})

    def add_rank(self, sym: str, name: str, price: int, rate: float, qty: int, sources=("3", "1", "change")):
        """순위 조회 응답에 종목 추가 (거래대금은 백만원 단위 — 키움과 같은 형식)"""
        self.names[sym] = name
        row = {"stk_cd": sym, "stk_nm": name, "cur_prc": f"+{price}", "flu_rt": f"{rate:+.2f}",
               "trde_qty": str(qty), "now_trde_qty": str(qty), "trde_amt": str(price * qty // 1_000_000)}
        for src in sources:
            (self.change_top if src == "change" else self.today_top[src]).append(row)

    def api_calls(self, api: str) -> List[dict]:
        return [c for c in self.calls if c["api"] == api]


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now

    def set(self, hhmmss: str):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        self.now = self.now.replace(hour=h, minute=m, second=s)
