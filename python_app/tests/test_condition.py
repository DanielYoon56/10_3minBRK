"""
키움 조건검색 테스트
- 실제 웹소켓 서버(로컬)로 LOGIN · PING 되돌려주기 · 목록 · 연속조회 프로토콜 검증
- 스캐너: 조건식 결과 AND (① OR ②), 앱의 주가·시총 필터 생략
"""

import asyncio
import json
import threading
from datetime import datetime

import pytest
import websocket
import websockets

from python_app.broker.kiwoom_condition import ConditionSearchError, KiwoomConditionClient, parse_items
from python_app.screener.accumulation import AccumulationScreener, ScreenerConfig
from python_app.tests.fake_kiwoom import FakeKiwoom
from python_app.tests.test_kiwoom_live import make_broker
from python_app.tests.test_screener import TODAY, build_market


class LocalKiwoomWS:
    """키움 조건검색 웹소켓 흉내 서버 (별도 스레드의 asyncio)"""

    def __init__(self, conditions=None, pages=None, login_code=0):
        self.conditions = conditions or [["0", "쌤_매집후보"], ["3", "급등주"]]
        self.pages = pages or [
            {"cont_yn": "Y", "next_key": "K1",
             "data": [{"9001": "A111111", "302": "에이전자"}, {"9001": "A222222", "302": "비화학"}]},
            {"cont_yn": "N", "next_key": "",
             "data": [{"jmcode": "444444"}, {"9001": "A005930", "302": "삼성전자"}]},
        ]
        self.login_code = login_code
        self.received = []
        self.loop = asyncio.new_event_loop()
        self.port = None
        ready = threading.Event()

        async def handler(conn):
            try:
                await serve(conn)
            except websockets.ConnectionClosed:
                pass

        async def serve(conn):
            async for raw in conn:
                msg = json.loads(raw)
                self.received.append(msg)
                t = msg.get("trnm")
                if t == "LOGIN":
                    await conn.send(json.dumps({"trnm": "LOGIN", "return_code": self.login_code, "return_msg": "x"}))
                    await conn.send(json.dumps({"trnm": "PING"}))          # 서버 PING → 클라이언트가 되돌려야 함
                elif t == "CNSRLST":
                    await conn.send(json.dumps({"trnm": "CNSRLST", "return_code": 0, "data": self.conditions}))
                elif t == "CNSRREQ":
                    idx = 1 if msg.get("cont_yn") == "Y" and msg.get("next_key") == "K1" else 0
                    page = self.pages[min(idx, len(self.pages) - 1)]
                    await conn.send(json.dumps({"trnm": "REAL", "data": []}))  # 끼어드는 실시간 메시지 무시해야 함
                    await conn.send(json.dumps({"trnm": "CNSRREQ", "seq": msg["seq"], "return_code": 0, **page}))

        async def main():
            server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
            ready.set()
            await asyncio.Future()

        threading.Thread(target=lambda: self.loop.run_until_complete(main()), daemon=True).start()
        ready.wait(5)

    def factory(self, _url):
        return websocket.create_connection(f"ws://127.0.0.1:{self.port}", timeout=5)


def _client(fake_server, tmp_path=None):
    broker = make_broker(FakeKiwoom())
    return KiwoomConditionClient(broker, raw_log_dir=str(tmp_path) if tmp_path else None,
                                 timeout=5, ws_factory=fake_server.factory)


def test_ws_url_follows_mode():
    from python_app.models.types import TradingMode
    fake = FakeKiwoom()
    assert KiwoomConditionClient(make_broker(fake, TradingMode.MOCK)).ws_url == \
        "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    assert KiwoomConditionClient(make_broker(fake, TradingMode.REAL)).ws_url == \
        "wss://api.kiwoom.com:10000/api/dostk/websocket"


def test_login_ping_list_and_paged_search(tmp_path):
    srv = LocalKiwoomWS()
    with _client(srv, tmp_path) as cc:
        assert cc.list_conditions() == [("0", "쌤_매집후보"), ("3", "급등주")]
        items = cc.search("쌤_매집후보")
    assert [i["symbol"] for i in items] == ["111111", "222222", "444444", "005930"]
    assert items[0]["name"] == "에이전자"
    sent = [m["trnm"] for m in srv.received]
    assert sent[0] == "LOGIN" and srv.received[0]["token"] == "TOKEN123"
    assert "PING" in sent                                   # PING 을 되돌려 보냄
    reqs = [m for m in srv.received if m["trnm"] == "CNSRREQ"]
    assert reqs[0]["seq"] == "0" and reqs[0]["search_type"] == "0"
    assert reqs[1]["cont_yn"] == "Y" and reqs[1]["next_key"] == "K1"
    raw_logs = list(tmp_path.glob("condition_raw_*.json"))
    assert raw_logs                                          # 실제 응답 형식 확인용 원본 저장


def test_unknown_condition_name_lists_available(tmp_path):
    srv = LocalKiwoomWS()
    with _client(srv) as cc:
        with pytest.raises(ConditionSearchError) as e:
            cc.search("없는조건")
    assert "쌤_매집후보" in str(e.value)


def test_login_failure_raises():
    srv = LocalKiwoomWS(login_code=8005)
    with pytest.raises(ConditionSearchError):
        with _client(srv):
            pass


def test_parse_items_variants():
    data = [{"9001": "A005930", "302": "삼성전자"}, {"jmcode": "000660"}, ["035420", "NAVER"], "A247540",
            {"stk_cd": "005380_NX", "stk_nm": "현대차"}, {"foo": "bar"}]
    assert [i["symbol"] for i in parse_items(data)] == ["005930", "000660", "035420", "247540", "005380"]


# ======================================================================== 스캐너 + 조건식
class FakeCondClient:
    def __init__(self, items=None, fail=None):
        self.items, self.fail = items or [], fail

    def __enter__(self):
        if self.fail:
            raise ConditionSearchError(self.fail)
        return self

    def __exit__(self, *a):
        pass

    def search(self, name):
        return list(self.items)


def _scr(fake, tmp_path, items=None, fail=None, **cfg):
    return AccumulationScreener(make_broker(fake), ScreenerConfig(condition_name="쌤_매집후보", **cfg),
                                state_dir=str(tmp_path), condition_client_factory=lambda: FakeCondClient(items, fail))


def test_condition_and_accumulation(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    items = [{"symbol": "111111", "name": "에이전자"},      # ① 해당 → 선정
             {"symbol": "444444", "name": "디동전"},        # ① 해당, 주가 800원이지만 앱 필터 생략 → 선정
             {"symbol": "777777", "name": "지윗꼬리"}]      # ① 불통, ② 아님 → 제외
    res = _scr(fake, tmp_path, items).run(TODAY, use_cache=False)
    syms = [h.symbol for h in res.hits]
    assert set(syms) == {"111111", "444444"}
    assert "222222" not in syms          # ② 기관 순매수지만 조건식 결과에 없음 → AND 로 제외
    assert not fake.api_calls("ka10031") # 조건식 사용 시 전 시장 상위 조회 안 함
    assert not any(c["api"] == "ka10001" for c in fake.calls)   # 주가·시총 조회 생략


def test_condition_with_supply_signal(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    res = _scr(fake, tmp_path, [{"symbol": "222222", "name": "비화학"}]).run(TODAY, use_cache=False)
    assert [(h.symbol, h.reasons) for h in res.hits] == [("222222", ["②기관"])]


def test_condition_failure_means_no_watchlist(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    res = _scr(fake, tmp_path, fail="조건식 로그인 실패").run(TODAY, use_cache=False)
    assert res.hits == [] and res.errors
    assert not fake.api_calls("ka10031")   # 조건 없이 전 시장으로 대체하지 않음


def test_empty_condition_result(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    res = _scr(fake, tmp_path, items=[]).run(TODAY, use_cache=False)
    assert res.hits == [] and not res.errors
