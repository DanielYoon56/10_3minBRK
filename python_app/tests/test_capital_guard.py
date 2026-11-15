"""중앙 자금 보호: 진입 차단 사유, 수량 계산(가장 엄격한 한도), 손실 한도, 상태 유지, 엔진 연동"""
from datetime import datetime

import pytest

from python_app.models.types import StrategyConfig
from python_app.risk.capital_guard import AccountState, CapitalGuard, RiskConfig
from python_app.strategies.base_strategy import StrategySignal
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.stubs import OpenCrossStub
from python_app.tests.test_kiwoom_live import make_engine, seed_breakout_day

NOW = datetime(2026, 10, 20, 9, 30)          # 화요일


def sig(symbol="005930", entry=10000.0, stop=9800.0, strategy="open", **kw):
    return StrategySignal(strategy=strategy, pattern="p", symbol=symbol, name=symbol, action="BUY",
                          entry_price=entry, timestamp=NOW, reason="테스트", stop_price=stop, **kw)


def guard(risk=None, **common):
    return CapitalGuard(StrategyConfig(**common), RiskConfig(**(risk or {})))


def state(**kw):
    kw.setdefault("cash", 100_000_000)
    return AccountState(**kw)


# ------------------------------------------------------------------ 수량: 가장 엄격한 한도
def test_quantity_from_allowed_loss_includes_costs_and_slippage():
    g = guard()                                           # 1회 허용 손실 = 100만원 × 2% = 20,000원
    per_share = g.risk_per_share(10000, 9800)
    assert per_share == pytest.approx(200 + 10000 * 0.00015 + 9800 * 0.00215 + 20)    # 가격차 + 수수료·세금 + 2호가
    d = g.evaluate(sig(), state(), NOW)
    assert d.approved and d.qty == int(20_000 // per_share) == 82 and "허용 손실" in d.reason
    assert d.risk_amount <= 20_000 and d.stop_price == 9800


def test_tight_stop_is_capped_by_position_size_and_wide_stop_by_risk():
    g = guard()
    tight = g.evaluate(sig(stop=9950.0), state(), NOW)    # 손절폭 0.5% → 위험 기준으로는 200주 이상
    assert tight.qty == 99 and "종목 한도" in tight.reason  # 100만원 ÷ 10,001.5원
    wide = g.evaluate(sig(stop=9700.0), state(), NOW)
    assert wide.qty < 82 and "허용 손실" in wide.reason


def test_signal_without_stop_uses_the_common_stop_rate():
    g = guard()
    d = g.evaluate(sig(stop=None), state(), NOW)
    assert d.stop_price == 9800 and d.qty == 82


def test_cash_and_exposure_limits():
    g = guard(risk=dict(max_loss_per_trade=10 ** 9))
    assert g.evaluate(sig(), state(cash=300_000), NOW).qty == 29                  # 현금이 가장 엄격
    none = g.evaluate(sig(), state(cash=5_000), NOW)
    assert not none.approved and "주문가능금액 부족" in none.reason
    crowded = state(exposure=2_600_000)                                            # 노출 한도 300만원 중 260만원 사용
    d = g.evaluate(sig(), crowded, NOW)
    assert d.qty == 39 and "노출 한도" in d.reason
    full = g.evaluate(sig(), state(exposure=3_000_000), NOW)
    assert not full.approved and "수량 0" in full.reason


def test_position_size_ratio_reduces_but_never_increases_size():
    g = guard(risk=dict(max_loss_per_trade=10 ** 9))
    assert g.evaluate(sig(position_size_ratio=0.5), state(), NOW).qty == 49
    assert g.evaluate(sig(position_size_ratio=3.0), state(), NOW).qty == 99


def test_size_does_not_grow_after_losses():
    g = guard()
    before = g.evaluate(sig(), state(), NOW).qty
    g.record_close("open", -15_000, NOW)
    g.record_close("open", -15_000, NOW)
    assert g.evaluate(sig(symbol="000660"), state(), NOW).qty == before          # 손실 뒤 증액(물타기·마틴게일) 없음


# ------------------------------------------------------------------ 진입 차단
@pytest.mark.parametrize("kw, when, expect", [
    (dict(active={"005930": "open"}), NOW, "이미 진행 중인 주문/보유"),
    (dict(account_symbols={"005930"}), NOW, "이미 계좌에 보유 중인 종목"),
    (dict(closed_today={"005930"}), NOW, "재진입 금지"),
    (dict(active={"A": "open", "B": "open", "C": "intraday"}), NOW, "최대 동시 보유 3종목"),
    (dict(data_problem="후보 갱신 지연"), NOW, "데이터 문제"),
    (dict(api_fail_streak=5), NOW, "시세 조회 연속 실패"),
    (dict(uncertain_orders=["삼성전자"]), NOW, "체결 확인이 안 된 주문"),
    (dict(), NOW.replace(hour=15, minute=21), "장 마감 임박"),
    (dict(), NOW.replace(hour=15, minute=15), "장 마감 청산 시각(15:15) 이후"),
])
def test_refusal_reasons(kw, when, expect):
    d = guard().evaluate(sig(), state(**kw), when)
    assert not d.approved and d.qty == 0 and expect in d.reason


def test_expired_limit_signal_is_refused():
    d = guard().evaluate(sig(expire_at=NOW), state(), NOW)
    assert not d.approved and "유효시각" in d.reason


def test_reentry_can_be_allowed():
    g = guard(allow_reentry_same_day=True)
    assert g.evaluate(sig(), state(closed_today={"005930"}), NOW).approved


def test_strategy_specific_position_limit():
    g = guard(risk=dict(strategy_max_positions={"open": 1}))
    busy = state(active={"A": "open.orb"})
    assert "전략 open 최대 보유 1종목" in g.evaluate(sig(), busy, NOW).reason
    assert g.evaluate(sig(strategy="intraday"), busy, NOW).approved              # 다른 전략은 영향 없음


# ------------------------------------------------------------------ 오버나이트(CLOSE) 별도 한도
def test_overnight_signals_have_their_own_limits():
    g = guard(risk=dict(max_loss_per_trade=10 ** 9))
    night = sig(strategy="close", hold_overnight=True)
    late = NOW.replace(hour=15, minute=15)
    assert g.evaluate(night, state(), late).approved                              # 청산 시각 이후에도 오버나이트 신호는 가능
    held = state(active={"A": "close"}, overnight={"A"}, exposure=500_000, overnight_exposure=500_000)
    refused = g.evaluate(night, held, late)
    assert not refused.approved and "오버나이트 최대 보유 1종목" in refused.reason
    g2 = guard(risk=dict(max_loss_per_trade=10 ** 9, overnight_max_positions=2))
    d = g2.evaluate(night, held, late)                                             # 오버나이트 노출 한도 100만원 중 50만원 남음
    assert d.approved and d.qty == 49 and "노출 한도" in d.reason
    assert g2.evaluate(sig(symbol="000660"), held, NOW).qty == 99                 # 당일 청산 신호는 오버나이트 한도와 무관


# ------------------------------------------------------------------ 손실 한도
def test_daily_loss_limit_counts_unrealized_and_stays_for_the_day():
    g = guard(max_daily_loss=100_000)
    g.record_close("open", -60_000, NOW)
    assert g.evaluate(sig(), state(), NOW).approved
    d = g.evaluate(sig(), state(unrealized=-50_000), NOW)
    assert not d.approved and "일 손실한도 도달" in d.reason
    assert not g.evaluate(sig(), state(unrealized=+50_000), NOW).approved         # 평가손익이 회복돼도 그날은 유지
    next_day = NOW.replace(day=21)
    assert g.evaluate(sig(), state(), next_day).approved and g.halted_reason == ""


def test_weekly_loss_limit_spans_days_and_resets_next_week():
    g = guard(max_daily_loss=100_000)                     # 주간 한도 = 일 한도 × 3 = 30만원
    for day in (19, 20, 21, 22):                          # 월~목, 매일 -8만원 (일 한도에는 안 걸림)
        g.record_close("open", -80_000, NOW.replace(day=day))
    d = g.evaluate(sig(), state(), NOW.replace(day=23))   # 금요일: 주간 -32만원
    assert not d.approved and "주간 손실한도 도달" in d.reason
    assert g.evaluate(sig(), state(), NOW.replace(day=26)).approved               # 다음 주 월요일
    assert guard(max_daily_loss=100_000, risk=dict(max_weekly_loss=1_000_000)).weekly_loss_limit == 1_000_000


def test_consecutive_losses_halt_the_day_and_a_win_resets_the_streak():
    g = guard(risk=dict(max_consecutive_losses=3), max_daily_loss=10 ** 9)
    for _ in range(2):
        g.record_close("open", -1_000, NOW)
    g.record_close("open", +500, NOW)                      # 이익 → 연속 손실 초기화
    for _ in range(2):
        g.record_close("intraday", -1_000, NOW)
    assert g.evaluate(sig(), state(), NOW).approved
    g.record_close("open", -1_000, NOW)
    assert "연속 손실 3회" in g.evaluate(sig(), state(), NOW).reason
    assert g.evaluate(sig(), state(), NOW.replace(day=21)).approved


def test_strategy_daily_loss_limit_blocks_only_that_strategy():
    g = guard(risk=dict(strategy_max_daily_loss={"intraday": 30_000}), max_daily_loss=10 ** 9)
    g.record_close("intraday.breakout", -40_000, NOW)
    assert "전략 intraday 당일 손실한도" in g.evaluate(sig(strategy="intraday"), state(), NOW).reason
    assert g.evaluate(sig(strategy="open"), state(), NOW).approved


# ------------------------------------------------------------------ 비상 정지·상태 유지
def test_emergency_stop_blocks_entries_until_resumed():
    g = guard()
    g.stop_new_entries("점검")
    d = g.evaluate(sig(), state(), NOW)
    assert not d.approved and "비상 정지 (점검)" in d.reason
    g.resume()
    assert g.evaluate(sig(), state(), NOW).approved


def test_state_survives_a_restart():
    g = guard(max_daily_loss=100_000)
    g.record_close("open", -120_000, NOW)
    g.evaluate(sig(), state(), NOW)
    g.stop_new_entries("점검")
    g2 = guard(max_daily_loss=100_000)
    g2.load_dict(g.to_dict())
    assert (g2.realized_week, g2.loss_streak, g2.emergency) == (-120_000, 1, "점검")
    g2.resume()
    assert "일 손실한도 도달" in g2.evaluate(sig(), state(), NOW).reason
    g2.load_dict(None)                                    # 예전 상태 파일(자금 보호 항목 없음)
    assert g2.realized_week == 0 and g2.emergency == ""


def test_risk_config_ignores_unknown_keys():
    r = RiskConfig.from_dict({"max_loss_per_trade": 5000, "nope": 1})
    assert r.max_loss_per_trade == 5000 and r.overnight_max_positions == 1


# ------------------------------------------------------------------ 엔진 연동
def test_engine_sizes_orders_with_the_guard(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path, risk=RiskConfig(),
                              strategies=[OpenCrossStub(stop_pct=3.0)])       # 손절폭 3% → 위험 기준이 종목 한도보다 엄격
    eng.start(); eng.tick()
    qty = int(fake.api_calls("kt10000")[0]["body"]["ord_qty"])
    assert qty == int(20_000 // eng.guard.risk_per_share(10100, 9797)) and qty < 98


def test_engine_emergency_stop_blocks_buys_but_keeps_guarding_positions(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, events = make_engine(fake, clock, tmp_path)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    eng.stop_new_entries("점검")
    assert "비상 정지" in eng.halted_reason and "비상 정지" in eng.snapshot()["halted"]
    fake.prices["005930"] = 9898                              # 보유 종목 손절은 그대로 작동
    clock.set("09:07:00"); eng.tick()
    assert len(fake.api_calls("kt10001")) == 1
    eng.stop()

    fake2 = FakeKiwoom()
    seed_breakout_day(fake2)
    eng2, events2 = make_engine(fake2, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path)   # 같은 상태 파일 → 비상 정지 유지
    eng2.start(); eng2.tick()
    assert not fake2.api_calls("kt10000") and any("비상 정지" in e for e in events2)
    eng2.resume_entries()
    assert eng2.halted_reason == ""


def test_engine_blocks_entries_after_repeated_quote_failures(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path)
    eng.start()
    eng._api_fail_streak = 5
    state_before = eng._account_state(eng.clock())
    assert state_before.api_fail_streak == 5
    fake.fail_apis = {"ka10080"}
    eng.tick()
    assert eng._api_fail_streak == 6 and not fake.api_calls("kt10000")


def test_engine_weekly_loss_is_remembered_across_days(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path, max_daily_loss=10_000)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    fake.prices["005930"] = 9800
    clock.set("09:07:00"); eng.tick()
    clock.set("09:07:30"); eng._last_sync = datetime.min; eng.tick()
    lost = eng.realized_pnl
    assert lost < -30_000 and eng.guard.realized_week == pytest.approx(lost)
    eng.stop()

    clock.now = datetime(2026, 10, 21, 9, 6, 2)                # 다음 날 재시작: 당일 손익은 0, 주간 누계는 유지
    eng2, events = make_engine(fake, clock, tmp_path, max_daily_loss=10_000)
    eng2.start()
    assert eng2.realized_pnl == 0 and eng2.guard.realized_week == pytest.approx(lost)
    d = eng2.guard.evaluate(sig(symbol="000660"), eng2._account_state(clock()), clock())
    assert not d.approved and "주간 손실한도 도달" in d.reason   # 주간 한도 3만원 초과
