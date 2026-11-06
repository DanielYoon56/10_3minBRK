"""
키움 REST 브로커 + 실시간 자동매매 엔진 통합 테스트 (가짜 키움 서버 사용, 실제 주문 없음)
"""

from datetime import datetime

import pytest

from python_app.broker.kiwoom_rest import KiwoomRestBroker, _num, _code
from python_app.engine.live_engine import LiveTradingEngine, EngineSettings
from python_app.models.types import OrderSide, StrategyConfig, TradingMode
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom

DAY = "20261020"   # 화요일


def make_broker(fake: FakeKiwoom, mode=TradingMode.MOCK, config=None) -> KiwoomRestBroker:
    return KiwoomRestBroker("APPKEY", "SECRET", mode=mode, config=config or StrategyConfig(),
                            session=fake, min_interval=0)


def make_engine(fake, clock, tmp_path, mode=TradingMode.MOCK, **cfg):
    config = StrategyConfig(**cfg)
    broker = make_broker(fake, mode, config)
    events = []
    eng = LiveTradingEngine(broker, config, [("005930", "삼성전자")], state_dir=str(tmp_path),
                            clock=clock, settings=EngineSettings(bar_poll_delay_sec=3),
                            on_event=lambda lv, m: events.append(m))
    return eng, events


def seed_breakout_day(fake: FakeKiwoom):
    """첫 봉 시가 10,000 / 종가 9,950 / 거래량 100,000 → 두 번째 봉 10,100 마감, 거래량 80,000(80%)"""
    fake.names["005930"] = "삼성전자"
    fake.add_bar("005930", "20261019152700", 9800, 9800, 9700, 9750, 50_000)   # 전일 봉 (무시돼야 함)
    fake.add_bar("005930", f"{DAY}090000", 10000, 10050, 9900, 9950, 100_000)
    fake.add_bar("005930", f"{DAY}090300", 9950, 10150, 9950, 10100, 80_000)
    fake.prices["005930"] = 10100


# ======================================================================== 브로커 단위
def test_mode_selects_server_and_token_headers():
    fake = FakeKiwoom()
    mock = make_broker(fake, TradingMode.MOCK)
    real = make_broker(fake, TradingMode.REAL)
    assert mock.base_url == "https://mockapi.kiwoom.com"
    assert real.base_url == "https://api.kiwoom.com"
    assert mock.connect()
    tok = fake.api_calls("au10001")[0]
    assert tok["url"] == "https://mockapi.kiwoom.com/oauth2/token"
    assert tok["body"] == {"grant_type": "client_credentials", "appkey": "APPKEY", "secretkey": "SECRET"}


def test_number_and_code_parsing():
    assert _num("-000000071200") == -71200
    assert _num("+72,000") == 72000
    assert _num("") == 0
    assert _code("A005930") == "005930"
    assert _code("005930_NX") == "005930"


def test_quote_balance_cash_parsing():
    fake = FakeKiwoom(cash=5_000_000)
    fake.prices["005930"] = 71200
    fake.holdings["000660"] = {"qty": 3, "avg": 180000}
    b = make_broker(fake)
    q = b.get_quote("005930")
    assert q.current_price == 71200            # '-71200' 부호 제거
    assert b.get_cash_balance() == 5_000_000
    pos = b.get_positions()
    assert list(pos) == ["000660"] and pos["000660"].qty == 3
    assert pos["000660"].stop_loss_price == round(180000 * 0.98)


def test_order_body_market_buy_and_sell():
    fake = FakeKiwoom()
    fake.prices["005930"] = 70000
    b = make_broker(fake)
    o = b.submit_order("005930", "삼성전자", OrderSide.BUY, 10)
    assert o and o.order_id
    c = fake.api_calls("kt10000")[0]
    assert c["url"].endswith("/api/dostk/ordr")
    assert c["body"]["trde_tp"] == "3" and c["body"]["ord_uv"] == "" and c["body"]["ord_qty"] == "10"
    b.submit_order("005930", "삼성전자", OrderSide.SELL, 10)
    assert fake.api_calls("kt10001")


def test_minute_bars_today_only_end_time_and_completion():
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    fake.add_bar("005930", f"{DAY}090600", 10100, 10200, 10050, 10150, 30_000)  # 진행중 봉
    b = make_broker(fake)
    bars = b.get_minute_bars("005930", DAY, datetime(2026, 10, 20, 9, 7, 0))
    assert [x.timestamp.strftime("%H:%M") for x in bars] == ["09:03", "09:06", "09:09"]
    assert [x.is_completed for x in bars] == [True, True, False]
    assert bars[0].low == 9900 and bars[0].volume == 100_000


def test_api_error_return_code_is_not_success():
    fake = FakeKiwoom()
    fake.fail_token = True
    assert make_broker(fake).connect() is False


# ======================================================================== 엔진 시나리오
def test_full_day_buy_then_trailing_exit(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    fake.holdings["000660"] = {"qty": 5, "avg": 200000}     # 원래 갖고 있던 종목
    fake.prices["000660"] = 100000                           # -50% 폭락해도 건드리면 안 됨
    clock = Clock(datetime(2026, 10, 20, 9, 6, 1))
    eng, events = make_engine(fake, clock, tmp_path)
    assert eng.start()

    # 09:06:01 → 시작 직후 1회 조회: 09:06 봉 신호 (나이 1초) → 매수
    eng.tick()
    buys = fake.api_calls("kt10000")
    assert len(buys) == 1
    assert int(buys[0]["body"]["ord_qty"]) == 98   # 99주는 수수료 포함 1,000,050원 > 100만원
    assert eng.managed["005930"].status == "BUYING"

    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    m = eng.managed["005930"]
    assert m.status == "HOLDING" and m.entry_price == 10100

    # 상승 → 최고가 11,100 (상승분 1,000)
    fake.prices["005930"] = 11100
    clock.set("09:20:00"); eng.tick()
    assert eng.managed["005930"].highest_price == 11100
    # 20% 되돌림 → 유지
    fake.prices["005930"] = 10900
    clock.set("09:20:03"); eng.tick()
    assert not fake.api_calls("kt10001")
    # 1R 이상 구간 30% 되돌림 (11,100 - 300 = 10,800) → 매도
    fake.prices["005930"] = 10800
    clock.set("09:20:06"); eng.tick()
    sells = fake.api_calls("kt10001")
    assert len(sells) == 1 and sells[0]["body"]["stk_cd"] == "005930"
    assert "트레일링" in eng.managed["005930"].exit_reason

    clock.set("09:20:30"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["005930"].status == "CLOSED"
    assert eng.realized_pnl > 0
    # 원래 보유 종목(000660)은 한 번도 매도 주문이 나가지 않음
    assert all(c["body"]["stk_cd"] != "000660" for c in fake.api_calls("kt10001"))


def test_stop_loss_2pct_exit(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    fake.prices["005930"] = 9898                       # 10,100 × 0.98 = 9,898
    clock.set("09:07:00"); eng.tick()
    sells = fake.api_calls("kt10001")
    assert len(sells) == 1 and "손절" in eng.managed["005930"].exit_reason


def test_late_start_does_not_chase_old_signal(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 40, 0))   # 신호(09:06) 34분 뒤 시작
    eng, events = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    assert not fake.api_calls("kt10000")
    assert any("과거 신호" in e for e in events)


def test_volume_below_70pct_no_buy(tmp_path):
    fake = FakeKiwoom()
    fake.names["005930"] = "삼성전자"
    fake.add_bar("005930", f"{DAY}090000", 10000, 10050, 9900, 9950, 100_000)
    fake.add_bar("005930", f"{DAY}090300", 9950, 10150, 9950, 10100, 70_000)   # 정확히 70%
    fake.prices["005930"] = 10100
    clock = Clock(datetime(2026, 10, 20, 9, 6, 5))
    eng, _ = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    assert not fake.api_calls("kt10000")


def test_real_mode_requires_confirmation(tmp_path):
    fake = FakeKiwoom()
    clock = Clock(datetime(2026, 10, 20, 9, 0, 0))
    eng, events = make_engine(fake, clock, tmp_path, mode=TradingMode.REAL)
    assert eng.start() is False
    assert not fake.api_calls("au10001")
    assert eng.start(live_confirmed=True) is True
    assert fake.api_calls("au10001")[0]["url"].startswith("https://api.kiwoom.com")


def test_unfilled_buy_is_cancelled_after_timeout(tmp_path):
    fake = FakeKiwoom()
    fake.auto_fill = False
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    assert eng.managed["005930"].status == "BUYING"
    clock.set("09:06:40"); eng.tick()
    assert fake.api_calls("kt10003")
    assert eng.managed["005930"].status == "FAILED"


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, events = make_engine(fake, clock, tmp_path, max_daily_loss=100_000)
    eng.start()
    eng.realized_pnl = -150_000
    eng.tick()
    assert not fake.api_calls("kt10000")
    assert "일 손실한도" in eng.halted_reason


def test_state_persists_peak_across_restart(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    fake.prices["005930"] = 12000
    clock.set("09:10:00"); eng.tick()
    eng.stop()

    eng2, _ = make_engine(fake, clock, tmp_path)
    eng2.start()
    assert eng2.managed["005930"].highest_price == 12000
    # 재시작 후 12,000 기준 트레일링: 상승분 1,900 × 30% = 570 → 11,430 이하 매도
    fake.prices["005930"] = 11430
    clock.set("09:12:00"); eng2.tick()
    assert fake.api_calls("kt10001")


# ======================================================================== 장 마감 일괄 청산
def _bought(fake, clock, tmp_path, **cfg):
    seed_breakout_day(fake)
    eng, events = make_engine(fake, clock, tmp_path, **cfg)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["005930"].status == "HOLDING"
    return eng, events


def test_eod_liquidation_sells_only_app_positions(tmp_path):
    fake = FakeKiwoom()
    fake.holdings["000660"] = {"qty": 5, "avg": 200000}; fake.prices["000660"] = 200000
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, events = _bought(fake, clock, tmp_path)
    fake.prices["005930"] = 10300                       # 손절·트레일링 미해당
    clock.set("15:14:58"); eng.tick()
    assert not fake.api_calls("kt10001")
    clock.set("15:15:00"); eng.tick()
    sells = fake.api_calls("kt10001")
    assert len(sells) == 1 and sells[0]["body"]["stk_cd"] == "005930"
    assert "장 마감 일괄 청산" in eng.managed["005930"].exit_reason
    clock.set("15:15:30"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["005930"].status == "CLOSED"
    assert "000660" in fake.holdings                    # 기존 보유 종목은 그대로


def test_no_new_buy_after_eod_time(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path, trade_end_time="15:30", eod_liquidation_time="09:05")
    eng.start(); eng.tick()
    assert not fake.api_calls("kt10000")


def test_closing_auction_sell_not_cancelled(tmp_path):
    fake = FakeKiwoom()
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = _bought(fake, clock, tmp_path, eod_liquidation_time="15:22")
    fake.auto_fill = False                              # 동시호가: 15:30까지 미체결
    clock.set("15:22:00"); eng.tick()
    assert eng.managed["005930"].status == "SELLING"
    clock.set("15:23:00"); eng.tick()                   # 60초 경과해도 취소 안 함
    assert not fake.api_calls("kt10003")
    assert eng.managed["005930"].status == "SELLING"


def test_eod_disabled_keeps_position(tmp_path):
    fake = FakeKiwoom()
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = _bought(fake, clock, tmp_path, eod_liquidation=False)
    clock.set("15:16:00"); eng.tick()
    assert not fake.api_calls("kt10001")


def test_carryover_sold_at_next_open_when_app_was_off(tmp_path):
    fake = FakeKiwoom()
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = _bought(fake, clock, tmp_path)
    clock.set("11:00:00"); eng.tick()
    eng.stop()                                           # 15:15 전에 앱 종료 → 청산 누락

    clock.now = datetime(2026, 10, 21, 8, 55, 0)        # 다음 날 장 전 시작
    eng2, events = make_engine(fake, clock, tmp_path)
    eng2.start()
    assert eng2.managed["005930"].carried_over
    eng2.tick()
    assert not fake.api_calls("kt10001")                 # 09:00 전에는 주문 안 함
    clock.set("09:00:01"); eng2.tick()
    sells = fake.api_calls("kt10001")
    assert len(sells) == 1 and "이월" in eng2.managed["005930"].exit_reason


def test_old_config_migrates_activation_to_1pct(tmp_path):
    import json
    from python_app.app_config import load_config, strategy_config_from
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"trading_mode": "MOCK", "trailing_activation_rate": 0.0}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["trailing_activation_rate"] == 1.0 and cfg["config_version"] == 5
    assert strategy_config_from(cfg).trailing_activation_rate == 1.0
    # 이전 후 사용자가 다시 0으로 바꾸면 그대로 유지
    cfg["trailing_activation_rate"] = 0.0
    p.write_text(json.dumps(cfg), encoding="utf-8")
    assert load_config(str(p))["trailing_activation_rate"] == 0.0


def test_cash_balance_is_smaller_of_orderable_and_deposit():
    b = make_broker(FakeKiwoom())
    for body, expected in (({"entr": "000000001000000", "ord_alow_amt": "000000003000000"}, 1_000_000),
                           ({"entr": "000000003000000", "ord_alow_amt": "000000001000000"}, 1_000_000),
                           ({"ord_alow_amt": "000000003000000"}, 0)):
        b._post = lambda *a, body=body, **k: (body, {})
        assert b.get_cash_balance() == expected
