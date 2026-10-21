"""텔레그램 알림 테스트 (실제 전송 없음)"""

from datetime import datetime

from python_app.engine.live_engine import EngineSettings, LiveTradingEngine
from python_app.models.types import StrategyConfig, TradingMode
from python_app.notify.telegram import TelegramNotifier
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.test_kiwoom_live import make_broker, seed_breakout_day


class FakeTgResp:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._b


class FakeTgSession:
    def __init__(self, status=200, body=None):
        self.sent = []
        self.status = status
        self.body = body or {"ok": True}

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))
        return FakeTgResp(self.status, self.body)

    def get(self, url, timeout=None):
        return FakeTgResp(200, {"ok": True, "result": [
            {"message": {"chat": {"id": 987654321, "first_name": "Daniel"}, "text": "hi"}}]})


class CaptureNotifier:
    def __init__(self):
        self.msgs = []

    def notify(self, text):
        self.msgs.append(text)


def test_send_now_payload_and_prefix():
    sess = FakeTgSession()
    n = TelegramNotifier("TOKEN:ABC", "12345", prefix="[모의]", session=sess)
    ok, _ = n.send_now("hello")
    assert ok
    url, body = sess.sent[0]
    assert url == "https://api.telegram.org/botTOKEN:ABC/sendMessage"
    assert body["chat_id"] == "12345" and body["text"] == "hello"


def test_bad_token_returns_error_without_endless_retry():
    sess = FakeTgSession(status=401, body={"ok": False, "description": "Unauthorized"})
    ok, msg = TelegramNotifier("BAD", "1", session=sess).send_now("x")
    assert not ok and "Unauthorized" in msg and len(sess.sent) == 1


def test_find_chat_ids():
    n = TelegramNotifier("TOKEN", "", session=FakeTgSession())
    assert n.find_chat_ids() == [("987654321", "Daniel")]


def test_inactive_notifier_does_nothing():
    sess = FakeTgSession()
    n = TelegramNotifier("", "", session=sess)
    n.notify("x")
    assert not n.active and not sess.sent


def test_async_notify_delivers():
    sess = FakeTgSession()
    n = TelegramNotifier("T", "1", prefix="[실거래]", session=sess)
    n.notify("매수")
    n.flush(3)
    import time; time.sleep(0.2)
    assert sess.sent and sess.sent[0][1]["text"] == "[실거래] 매수"


def _engine(tmp_path, fake, clock, **cfg):
    config = StrategyConfig(**cfg)
    cap = CaptureNotifier()
    eng = LiveTradingEngine(make_broker(fake, TradingMode.MOCK, config), config, [("005930", "삼성전자")],
                            state_dir=str(tmp_path), clock=clock, settings=EngineSettings(bar_poll_delay_sec=3),
                            notifier=cap)
    return eng, cap


def test_engine_pushes_trade_lifecycle(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, cap = _engine(tmp_path, fake, clock)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    fake.prices["005930"] = 9898
    clock.set("09:07:00"); eng.tick()
    clock.set("09:07:30"); eng._last_sync = datetime.min; eng.tick()
    text = "\n".join(cap.msgs)
    assert "자동매매 시작" in text
    assert "매수 주문 삼성전자" in text
    assert "매수 체결 삼성전자" in text
    assert "🛑 매도 주문" in text and "손절" in text
    assert "매도 체결" in text
    # 3분봉 신호 대기 등 일반 로그는 휴대폰으로 보내지 않음
    assert not any("과거 신호" in m for m in cap.msgs)


def test_repeated_error_push_deduplicated(tmp_path):
    fake = FakeKiwoom()
    clock = Clock(datetime(2026, 10, 20, 9, 0, 0))
    eng, cap = _engine(tmp_path, fake, clock)
    for _ in range(5):
        eng._emit("error", "매도 주문 실패 삼성전자 — 다음 주기 재시도")
    assert sum("매도 주문 실패" in m for m in cap.msgs) == 1


def test_daily_summary_once(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, cap = _engine(tmp_path, fake, clock)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    clock.set("15:15:00"); eng.tick()                         # 일괄 청산
    clock.set("15:15:30"); eng._last_sync = datetime.min; eng.tick()
    clock.set("15:31:00"); eng.tick()
    clock.set("15:32:00"); eng.tick()
    summaries = [m for m in cap.msgs if "당일 마감 요약" in m]
    assert len(summaries) == 1
    assert "삼성전자" in summaries[0] and "합계 추정손익" in summaries[0]
