"""
추가 매수 전략 (B 첫 봉 고가 돌파 / C VWAP 눌림 / D 전일 고가 돌파 / E 당일 전고점 돌파 / F SMC CHoCH→FVG) + AND/OR 조합 테스트
"""

import json
from datetime import datetime, timedelta

from python_app.engine.live_engine import EngineSettings, LiveTradingEngine
from python_app.models.types import Bar, OrderType, Position, Signal, SignalType, StrategyConfig
from python_app.strategy.combiner import EntryCombiner
from python_app.strategy.exit_rules import check_exit, trailing_stop_price
from python_app.strategy.intraday import DaySwingHighBreakout, OrbHighBreakout, PrevHighBreakout, VwapPullback
from python_app.strategy.smc import SmcChochFvg, floor_to_tick, tick_size
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.test_kiwoom_live import DAY, make_broker

SYM = "005930"
START = datetime(2026, 10, 20, 9, 3)


def bars_from(rows, start=START):
    """rows: (open, high, low, close, volume) — 09:03 마감 봉부터 3분 간격"""
    return [Bar(SYM, start + timedelta(minutes=3 * i), o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]


def feed(strategy, rows):
    return [strategy.process_bar(b, "삼성전자") for b in bars_from(rows)]


# ============================================================ B. 첫 봉 고가 돌파
def test_orb_high_catches_gap_up_that_open_breakout_misses():
    rows = [(10000, 10100, 9990, 10080, 100_000),     # 첫 봉: 시가 아래로 안 내려감
            (10080, 10200, 10070, 10180, 90_000)]     # 첫 봉 고가 10,100 돌파
    combo = EntryCombiner(StrategyConfig(entry_strategies=["open_breakout"]))
    assert feed(combo, rows)[-1] is None              # 시가 돌파는 '아래→위' 교차가 없어 신호 없음
    sig = feed(OrbHighBreakout(StrategyConfig()), rows)[-1]
    assert sig and sig.signal_type == SignalType.BUY_ORB_HIGH
    assert sig.breakout_level == 10100 and sig.order_type == OrderType.MARKET
    assert sig.stop_loss_price == round(10180 * 0.98)


def test_orb_high_needs_volume():
    rows = [(10000, 10100, 9990, 10080, 100_000), (10080, 10200, 10070, 10180, 70_000)]
    assert feed(OrbHighBreakout(StrategyConfig()), rows)[-1] is None


# ============================================================ C. VWAP 눌림 반등
VWAP_ROWS = [(10000, 10050, 9980, 10040, 100_000),
             (10040, 10200, 10040, 10180, 100_000),   # 고가가 VWAP +1% 이상 → 추세 확인
             (10090, 10120, 10085, 10110, 50_000)]    # VWAP(≈10,086) 근처 눌림 후 양봉 마감


def test_vwap_pullback_signal():
    sig = feed(VwapPullback(StrategyConfig()), VWAP_ROWS)[-1]
    assert sig and sig.signal_type == SignalType.BUY_VWAP_PULLBACK
    assert 10080 <= sig.breakout_level <= 10090


def test_vwap_pullback_requires_prior_trend():
    rows = [VWAP_ROWS[0], (10040, 10080, 10030, 10060, 100_000), VWAP_ROWS[2]]
    assert feed(VwapPullback(StrategyConfig()), rows)[-1] is None


def test_vwap_pullback_entry_end_fixed_at_1400():
    strat = VwapPullback(StrategyConfig(trade_end_time="09:06"))
    assert strat.end_time.strftime("%H:%M") == "14:00"
    assert feed(strat, VWAP_ROWS)[-1] is not None       # 공통 마감(09:06)과 무관


# ============================================================ D. 전일 고가 돌파
PREV_ROWS = [(10000, 10050, 9950, 10000, 100_000),
             (10000, 10090, 9990, 10080, 60_000),
             (10080, 10180, 10070, 10150, 80_000)]


def test_prev_high_breakout_uses_provider_once():
    calls = []
    strat = PrevHighBreakout(StrategyConfig(), lambda s, d: calls.append((s, d)) or 10100)
    sig = feed(strat, PREV_ROWS)[-1]
    assert sig and sig.signal_type == SignalType.BUY_PREV_HIGH and sig.breakout_level == 10100
    assert calls == [(SYM, "20261020")]


def test_prev_high_without_data_never_signals():
    assert feed(PrevHighBreakout(StrategyConfig(), None), PREV_ROWS)[-1] is None
    assert feed(PrevHighBreakout(StrategyConfig(), lambda s, d: None), PREV_ROWS)[-1] is None


# ============================================================ F. SMC CHoCH → 첫 FVG
# 스윙 저점 9,850(2) → 9,830 마감으로 하락 구조(7) → 최저 9,740(9) → 스윙 고점 9,880(10)
# → 9,895 마감으로 상승 CHoCH(13) → 12번 고가 9,830 < 14번 저가 9,890 : 첫 FVG (중간값 9,860)
SMC_ROWS = [
    (10000, 10020, 9940, 9960, 50_000),
    (9960, 9970, 9900, 9920, 50_000),
    (9920, 9930, 9850, 9880, 50_000),
    (9880, 9960, 9870, 9950, 50_000),
    (9950, 10000, 9940, 9990, 50_000),
    (9990, 10050, 9970, 10000, 50_000),
    (10000, 10010, 9900, 9910, 50_000),
    (9910, 9920, 9820, 9830, 50_000),
    (9830, 9840, 9750, 9770, 50_000),
    (9770, 9800, 9740, 9790, 50_000),
    (9790, 9880, 9780, 9870, 50_000),
    (9870, 9870, 9800, 9810, 50_000),
    (9810, 9830, 9790, 9820, 50_000),
    (9820, 9900, 9815, 9895, 80_000),
    (9895, 9990, 9890, 9980, 90_000),
]


def test_smc_choch_then_first_fvg_midpoint_limit():
    sigs = feed(SmcChochFvg(StrategyConfig(trade_end_time="14:00")), SMC_ROWS)
    assert all(s is None for s in sigs[:-1])
    sig = sigs[-1]
    assert sig and sig.signal_type == SignalType.BUY_SMC_FVG and sig.order_type == OrderType.LIMIT
    assert sig.price == 9860                          # (9,830 + 9,890) / 2
    assert sig.stop_loss_price == 9740 - 10           # 보호 저점 - 1호가
    assert sig.expire_at == sig.timestamp + timedelta(minutes=30)


def test_smc_entry_end_fixed_at_1400():
    strat = SmcChochFvg(StrategyConfig(trade_end_time="10:00"))
    assert strat.end_time.strftime("%H:%M") == "14:00"
    sig = feed(strat, SMC_ROWS)[-1]
    assert sig.expire_at == sig.timestamp + timedelta(minutes=30)     # 공통 마감(10:00)에 잘리지 않음


def test_smc_fvg_stop_mode():
    sig = feed(SmcChochFvg(StrategyConfig(smc_stop_mode="fvg")), SMC_ROWS)[-1]
    assert sig.stop_loss_price == 9830 - 10           # FVG 하단 - 1호가


def test_smc_skips_when_risk_out_of_range():
    assert feed(SmcChochFvg(StrategyConfig(smc_max_risk_pct=1.0)), SMC_ROWS)[-1] is None
    # 첫 FVG 를 건너뛰면 같은 CHoCH 의 다음 FVG 는 쓰지 않는다
    strat = SmcChochFvg(StrategyConfig(smc_max_risk_pct=1.0))
    rows = SMC_ROWS + [(9980, 10100, 9995, 10090, 90_000)]
    assert all(s is None for s in feed(strat, rows))


def test_smc_bullish_break_without_prior_bear_is_bos_not_choch():
    rows = [(10000 + i * 30, 10040 + i * 30, 9990 + i * 30, 10030 + i * 30, 50_000) for i in range(15)]
    assert all(s is None for s in feed(SmcChochFvg(StrategyConfig()), rows))


def test_smc_window():
    assert feed(SmcChochFvg(StrategyConfig(trade_start_time="10:00", trade_end_time="14:00")), SMC_ROWS)[-1] is None


def test_tick_size():
    assert tick_size(1_999) == 1 and tick_size(4_995) == 5 and tick_size(19_990) == 10
    assert tick_size(49_950) == 50 and tick_size(199_900) == 100 and tick_size(499_500) == 500
    assert tick_size(500_000) == 1_000
    assert floor_to_tick(9_867) == 9_860 and floor_to_tick(71_234) == 71_200


# ============================================================ AND / OR 조합
COMBO_ROWS = [(10000, 10100, 9900, 9950, 100_000),
              (9950, 10080, 9950, 10050, 80_000),     # 시가 돌파
              (10050, 10200, 10040, 10150, 90_000)]   # 첫 봉 고가 돌파


def test_or_returns_first_strategy_signal():
    sigs = feed(EntryCombiner(StrategyConfig(entry_strategies=["open_breakout", "orb_high"])), COMBO_ROWS)
    assert sigs[1].strategy == "open_breakout" and sigs[2].strategy == "orb_high"


def test_or_counts_strategies_fired_today():
    sigs = feed(EntryCombiner(StrategyConfig(entry_strategies=["open_breakout", "orb_high"])), COMBO_ROWS)
    assert sigs[1].matched_strategies == 1
    assert sigs[2].matched_strategies == 2 and "OR 2개 충족" in sigs[2].reason
    assert sigs[2].strategy == "orb_high"


def _signal(symbol, matched):
    return Signal(symbol, symbol, SignalType.BUY_BREAKOUT, 10000, START, 10000, 1000, 1000, 1.0, "테스트",
                  matched_strategies=matched)


def _poll_order(tmp_path, matched, screen_info=None):
    """종목별 전략 충족 수(matched)로 한 번 조회했을 때 신호가 처리되는 종목 순서"""
    config = StrategyConfig()
    fake, clock = FakeKiwoom(), Clock(datetime(2026, 10, 20, 9, 6, 10))
    eng = LiveTradingEngine(make_broker(fake, config=config), config, [(s, s) for s in matched],
                            state_dir=str(tmp_path), clock=clock)
    eng.trade_date = DAY
    eng.screen_info = screen_info or {}
    eng.broker.get_minute_bars = lambda sym, *a, **k: [Bar(sym, START, 10000, 10000, 10000, 10000, 1000)]
    eng.strategy.process_bar = lambda bar, name: _signal(bar.symbol, matched[bar.symbol])
    order = []
    eng._on_signal = lambda sig, now: order.append(sig.symbol)
    eng._poll_bars_if_due(clock())
    return order


def test_engine_buys_most_matched_strategies_first(tmp_path):
    assert _poll_order(tmp_path, {"000001": 1, "000002": 3, "000003": 2}) == ["000002", "000003", "000001"]


def test_engine_counts_accumulation_conditions_and_keeps_watch_order_on_tie(tmp_path):
    info = {"000002": {"reasons": ["①", "②외국인", "②기관"]}, "000003": {"reasons": ["①"]}}
    assert _poll_order(tmp_path, {"000001": 1, "000002": 1, "000003": 1, "000004": 1}, info) ==         ["000002", "000003", "000001", "000004"]


def test_and_waits_until_all_strategies_fired():
    combo = EntryCombiner(StrategyConfig(entry_strategies=["orb_high", "open_breakout"], entry_logic="AND"))
    sigs = feed(combo, COMBO_ROWS)
    assert sigs[1] is None
    assert sigs[2].strategy == "open_breakout+orb_high"
    assert sigs[2].signal_type == SignalType.BUY_ORB_HIGH and "AND 충족" in sigs[2].reason


def test_combiner_rejects_unknown_strategy_and_reports_last_entry_time():
    import pytest
    with pytest.raises(ValueError):
        EntryCombiner(StrategyConfig(entry_strategies=["nope"]))
    with pytest.raises(ValueError):
        EntryCombiner(StrategyConfig(entry_logic="XOR"))
    combo = EntryCombiner(StrategyConfig(entry_strategies=["open_breakout", "smc_fvg"], trade_end_time="11:30"))
    assert combo.last_entry_time().strftime("%H:%M") == "14:00"      # F 마감 14:00 고정이 가장 늦음
    combo = EntryCombiner(StrategyConfig(entry_strategies=["open_breakout", "day_swing_high"], trade_end_time="11:30"))
    assert combo.last_entry_time().strftime("%H:%M") == "11:30"


# ============================================================ 청산 (전략 목표가 / 트레일링 끄기)
def _pos(stop):
    return Position(SYM, "삼성전자", 10, 9860, 9860, stop, 0.0, datetime.now())


def test_exit_strategy_target_and_no_trailing():
    cfg = StrategyConfig()
    p = _pos(9730)
    assert check_exit(p, 10100, cfg, take_profit_price=10250, use_trailing=False) is None
    # 최고가 10,100(+2.4%) 후 10,000 으로 밀려도 트레일링 미적용 → 유지
    assert check_exit(p, 10000, cfg, take_profit_price=10250, use_trailing=False) is None
    d = check_exit(p, 10250, cfg, take_profit_price=10250, use_trailing=False)
    assert d.signal_type == SignalType.SELL_TAKE_PROFIT
    d = check_exit(_pos(9730), 9730, cfg, take_profit_price=10250, use_trailing=False, stop_label="(전략 손절가)")
    assert d.signal_type == SignalType.SELL_STOP_LOSS and "전략 손절가" in d.reason


# ============================================================ 엔진: SMC 지정가 흐름
def _smc_engine(tmp_path, **cfg):
    fake = FakeKiwoom()
    fake.names[SYM] = "삼성전자"
    fake.auto_fill = False
    t0 = datetime(2026, 10, 20, 9, 0)
    for i, (o, h, l, c, v) in enumerate(SMC_ROWS):
        fake.add_bar(SYM, (t0 + timedelta(minutes=3 * i)).strftime("%Y%m%d%H%M%S"), o, h, l, c, v)
    fake.prices[SYM] = 9980
    clock = Clock(datetime(2026, 10, 20, 9, 45, 5))
    config = StrategyConfig(entry_strategies=["smc_fvg"], **cfg)
    events = []
    eng = LiveTradingEngine(make_broker(fake, config=config), config, [(SYM, "삼성전자")], state_dir=str(tmp_path),
                            clock=clock, settings=EngineSettings(bar_poll_delay_sec=3),
                            on_event=lambda lv, m: events.append(m))
    assert eng.start()
    eng.tick()
    return fake, clock, eng, events


def test_engine_places_limit_order_and_cancels_on_expiry(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path, trade_end_time="14:00")
    buys = [o for o in fake.orders if o["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["body"]["trde_tp"] == "0" and buys[0]["body"]["ord_uv"] == "9860"
    m = eng.managed[SYM]
    assert m.status == "BUYING" and m.stop_price == 9730 and m.target_price == 0

    clock.set("10:10:00")                     # 아직 유효 (10:15 까지)
    eng.tick()
    assert eng.managed[SYM].status == "BUYING"
    clock.set("10:15:01")
    eng.tick()
    assert eng.managed[SYM].status == "FAILED"
    assert not fake.unfilled
    assert any("지정가 매수 미체결" in e for e in events)


def test_engine_cancels_limit_when_price_breaks_stop_before_fill(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path)
    fake.prices[SYM] = 9720
    clock.set("09:50:00")
    eng.tick()
    assert eng.managed[SYM].status == "FAILED" and not fake.unfilled
    assert any("셋업 무효" in e for e in events)


def test_engine_smc_follows_common_take_profit_rate(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path, trailing_retrace_rate=0.0, take_profit_rate=4.0)
    no, o = next(iter(fake.unfilled.items()))
    fake.unfilled.pop(no)
    fake.prices[SYM] = 9860
    fake._fill(SYM, "BUY", o["qty"])
    clock.set("09:48:00")
    eng.tick()
    m = eng.managed[SYM]
    assert m.status == "HOLDING" and m.entry_price == 9860

    fake.auto_fill = True
    fake.prices[SYM] = 10100                  # +2.4% — 공통 트레일링을 끄면 유지
    clock.set("09:49:00")
    eng.tick()
    fake.prices[SYM] = 10000
    clock.set("09:49:03")
    eng.tick()
    assert eng.managed[SYM].status == "HOLDING"
    fake.prices[SYM] = 10260                  # 9,860 × 1.04 = 10,254
    clock.set("09:49:06")
    eng.tick()
    assert any("목표가 익절" in e for e in events)
    assert [o["side"] for o in fake.orders] == ["BUY", "SELL"]


def test_engine_smc_holding_sells_on_common_trailing(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path)
    no, o = next(iter(fake.unfilled.items()))
    fake.unfilled.pop(no)
    fake.prices[SYM] = 9860
    fake._fill(SYM, "BUY", o["qty"])
    clock.set("09:48:00")
    eng.tick()
    fake.auto_fill = True
    fake.prices[SYM] = 10100
    clock.set("09:49:00")
    eng.tick()
    fake.prices[SYM] = 10000
    clock.set("09:49:03")
    eng.tick()
    assert any("트레일링 스탑" in e for e in events)
    assert [o["side"] for o in fake.orders] == ["BUY", "SELL"]


def test_engine_no_reentry_after_closed_trade(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path)
    eng.managed[SYM].status = "CLOSED"
    sig = eng.signals[-1]
    eng._try_buy(sig, sig.timestamp)
    assert any("재진입 금지" in e for e in events)


# ============================================================ E. 당일 전고점 돌파
DSH_ROWS = [(10000, 10050, 9980, 10040, 100_000),
            (10040, 10100, 10030, 10090, 90_000),
            (10090, 10200, 10080, 10150, 90_000),    # 스윙 고점 10,200
            (10150, 10160, 10060, 10080, 60_000),
            (10080, 10120, 10050, 10110, 60_000),    # 여기서 10,200 확정
            (10110, 10300, 10100, 10280, 90_000)]    # 종가로 전고점 돌파


def _dsh(**kw):
    return DaySwingHighBreakout(StrategyConfig(**kw))


def test_day_swing_high_signal_uses_global_stop():
    sigs = feed(_dsh(), DSH_ROWS)
    assert sigs[:-1] == [None] * 5
    sig = sigs[-1]
    assert sig.signal_type == SignalType.BUY_DAY_SWING_HIGH and sig.order_type == OrderType.MARKET
    assert sig.breakout_level == 10200 and sig.stop_loss_price == round(10280 * 0.98)
    assert "-2%" in sig.reason


def test_day_swing_high_wick_only_is_not_a_breakout():
    rows = DSH_ROWS[:-1] + [(10110, 10300, 10100, 10190, 90_000)]
    assert feed(_dsh(), rows)[-1] is None


def test_day_swing_high_needs_volume_and_consumes_level():
    rows = DSH_ROWS[:-1] + [(10110, 10300, 10100, 10280, 50_000), (10280, 10350, 10270, 10340, 90_000)]
    assert feed(_dsh(), rows)[-2:] == [None, None]


def test_day_swing_high_respects_window():
    # 공통 진입 시간대(trade_start_time) 밖이면 신호 없음 — 돌파는 09:18 봉
    assert feed(DaySwingHighBreakout(StrategyConfig(trade_start_time="09:30")), DSH_ROWS)[-1] is None


def _dsh_engine(tmp_path, **cfg):
    fake = FakeKiwoom()
    fake.names[SYM] = "삼성전자"
    t0 = datetime(2026, 10, 20, 9, 0)
    for i, (o, h, l, c, v) in enumerate(DSH_ROWS):
        fake.add_bar(SYM, (t0 + timedelta(minutes=3 * i)).strftime("%Y%m%d%H%M%S"), o, h, l, c, v)
    fake.prices[SYM] = 10000
    clock = Clock(datetime(2026, 10, 20, 9, 18, 5))
    config = StrategyConfig(entry_strategies=["day_swing_high"], **cfg)
    eng = LiveTradingEngine(make_broker(fake, config=config), config, [(SYM, "삼성전자")], state_dir=str(tmp_path),
                            clock=clock, settings=EngineSettings(bar_poll_delay_sec=3))
    assert eng.start()
    eng.tick()
    clock.set("09:18:20"); eng._last_sync = datetime.min; eng.tick()
    m = eng.managed[SYM]
    assert m.status == "HOLDING" and m.entry_price == 10000
    return fake, clock, eng, m


def test_engine_e_uses_global_stop_rate(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path)
    assert m.stop_price == 0                   # A~D 와 같이 체결가 기준 전역 -2%
    fake.prices[SYM] = 9810
    clock.set("09:18:50"); eng.tick()
    assert m.status == "HOLDING"
    fake.prices[SYM] = 9800
    clock.set("09:19:00"); eng.tick()
    assert m.status == "SELLING" and "손절" in m.exit_reason


def test_engine_trails_30pct_above_1r(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path)    # 손절 -2% → 1R = 200원
    fake.prices[SYM] = 10200
    clock.set("09:19:00"); eng.tick()
    fake.prices[SYM] = 10150                   # 상승분 200의 25% 되돌림 → 유지
    clock.set("09:19:10"); eng.tick()
    assert m.status == "HOLDING"
    fake.prices[SYM] = 10140                   # 30% 되돌림
    clock.set("09:19:20"); eng.tick()
    assert m.status == "SELLING" and "트레일링" in m.exit_reason and "30%" in m.exit_reason


def test_engine_e_below_activation_uses_common_rate(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path, trailing_activation_rate=3.0)
    fake.prices[SYM] = 10200                   # +2%: 공통 발동(+3%) 전 → 트레일링 없음
    clock.set("09:19:00"); eng.tick()
    fake.prices[SYM] = 10060
    clock.set("09:19:10"); eng.tick()
    assert m.status == "HOLDING"


def test_old_state_file_with_removed_fields_loads(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path)
    with open(eng.state_path, encoding="utf-8") as f:
        data = json.load(f)
    data["managed"][SYM].update(trail_retrace_rate=50.0, trail_activation_rate=0.5)
    with open(eng.state_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    eng._load_state(data["trade_date"])
    assert eng.managed[SYM].symbol == SYM and not hasattr(eng.managed[SYM], "trail_retrace_rate")


def test_market_partial_fill_updates_avg_price_and_stop(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path)
    fake.holdings[SYM] = {"qty": m.qty + 10, "avg": 10200}
    eng._sync_account()
    assert m.entry_price == 10200 and m.stop_price == 0
    assert eng.positions[SYM].stop_loss_price == round(10200 * 0.98)      # 전역 %가 새 평균단가를 따른다


def test_fill_below_signal_price_is_not_a_gain(tmp_path):
    fake, clock, eng, m = _dsh_engine(tmp_path)   # 신호가 10,280 / 체결 10,000
    assert m.highest_price == 10000
    clock.set("09:19:00"); eng.tick()
    assert m.status == "HOLDING"


# ============================================================ 같은 날 재진입 — 끝난 거래 보존
class _DailyDb:
    def __init__(self):
        self.saved = None

    def save_daily_pnl(self, mode, date, pnl, invested, trades, wins, losses):
        self.saved = (pnl, invested, trades, wins, losses)


def _reentered(tmp_path):
    fake, clock, eng, events = _smc_engine(tmp_path, allow_reentry_same_day=True)
    m = eng.managed[SYM]
    m.status, m.entry_price, m.qty, m.realized_pnl = "CLOSED", 10000.0, 10, 3000.0
    eng.realized_pnl = 3000.0
    sig = eng.signals[-1]
    eng._try_buy(sig, sig.timestamp)
    return eng, m


def test_reentry_keeps_closed_trade_in_todays_list(tmp_path):
    eng, first = _reentered(tmp_path)
    assert eng.closed_today == [first] and eng.managed[SYM].status == "BUYING"
    assert [t["status"] for t in eng.snapshot()["trades"]] == ["CLOSED", "BUYING"]


def test_daily_pnl_counts_both_trades_of_a_reentered_symbol(tmp_path):
    eng, _ = _reentered(tmp_path)
    second = eng.managed[SYM]
    second.status, second.entry_price, second.qty, second.realized_pnl = "CLOSED", 20000.0, 10, -1000.0
    eng.realized_pnl = 2000.0
    eng.db = _DailyDb()
    eng._save_daily_pnl()
    assert eng.db.saved == (2000.0, 300000.0, 2, 1, 1)


def test_closed_today_survives_restart_same_day_only(tmp_path):
    eng, first = _reentered(tmp_path)
    again = LiveTradingEngine(eng.broker, eng.config, [(SYM, "삼성전자")], state_dir=str(tmp_path), clock=eng.clock)
    again._load_state(eng.trade_date)
    assert [m.realized_pnl for m in again.closed_today] == [3000.0]
    other = LiveTradingEngine(eng.broker, eng.config, [(SYM, "삼성전자")], state_dir=str(tmp_path), clock=eng.clock)
    other._load_state("20991231")
    assert other.closed_today == []


def test_new_day_clears_closed_today(tmp_path):
    eng, _ = _reentered(tmp_path)
    eng._new_day("20261021")
    assert eng.closed_today == []
