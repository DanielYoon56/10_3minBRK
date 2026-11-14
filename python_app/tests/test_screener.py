"""
전일 종가 기준 매집 종목 스캐너 테스트 (가짜 키움 서버, 실제 조회 없음)
매집 = ①(거래량 급증·양봉·고가 근처 마감) OR ②(외국인 AND 기관 순매수 상위), 공통: 주가 ≥ 1,000원, 시총 ≥ 3,000억
"""

from datetime import datetime, timedelta

from python_app.engine.live_engine import EngineSettings, LiveTradingEngine
from python_app.models.types import StrategyConfig, TradingMode
from python_app.screener.accumulation import AccumulationScreener, ScreenerConfig, is_excluded_name
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.test_kiwoom_live import make_broker

TODAY = datetime(2026, 10, 20, 8, 50, 0)   # 화
PREV = "20261019"                           # 월


def _days(n_before: int):
    """PREV 이전 평일 n_before 개 (오래된 순)"""
    d = datetime(2026, 10, 19)
    out = []
    while len(out) < n_before:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return list(reversed(out))


def add_stock(fake, sym, name, avg_vol, prev_bar, mac, top=True):
    """prev_bar = (o, h, l, c, v) — 전일 봉. 직전 20일은 평균 거래량 avg_vol 의 평범한 봉"""
    fake.names[sym] = name
    o, h, l, c, v = prev_bar
    for dt in _days(20):
        fake.add_daily(sym, dt, c, c + 10, c - 10, c, avg_vol)
    fake.add_daily(sym, PREV, o, h, l, c, v)
    fake.prices[sym] = c
    fake.mac[sym] = mac
    if top:
        fake.prev_top["1"].append({"stk_cd": sym, "stk_nm": name})


def build_market(fake):
    # A: ① 거래량 3배·양봉·고가 근처 마감·대금 31.6억·시총 5,000억(억 단위 값) → 선정
    add_stock(fake, "111111", "에이전자", 100_000, (10000, 10600, 9900, 10550, 300_000), "5000")
    # B: ① 불통(음봉) 이지만 ② 외국인 20억 + 기관 50억 순매수 · 시총 8,000억(원 단위 값) → 선정
    add_stock(fake, "222222", "비화학", 100_000, (10600, 10700, 9900, 10000, 300_000), "800000000000")
    # C: ① 해당하지만 시총 1,000억 → 제외
    add_stock(fake, "333333", "씨소형", 100_000, (10000, 10600, 9900, 10550, 300_000), "1000")
    # D: ① 해당하지만 주가 800원 → 제외
    add_stock(fake, "444444", "디동전", 1_000_000, (700, 810, 690, 800, 5_000_000), "4000")
    # E: ETF → 제외
    add_stock(fake, "555555", "KODEX 레버리지", 100_000, (10000, 10600, 9900, 10550, 300_000), "90000")
    # G: 거래량 3배·양봉이지만 윗꼬리 김(고가 11000, 종가 10100) → 제외
    add_stock(fake, "777777", "지윗꼬리", 100_000, (10000, 11000, 9900, 10100, 300_000), "9000")
    # H: 우선주 → 제외
    add_stock(fake, "888888", "삼성전자우", 100_000, (10000, 10600, 9900, 10550, 300_000), "300000")
    # ② 외국인·기관 매매 상위 (금액 단위: 천만원)
    fake.names["666666"] = "에프소액"
    fake.prices["666666"] = 20000
    fake.mac["666666"] = "50000"
    fake.frgn_inst_rows = [
        {"for_netprps_stk_cd": "666666", "for_netprps_stk_nm": "에프소액", "for_netprps_amt": "+50",   # 5억 → 미달
         "orgn_netprps_stk_cd": "222222", "orgn_netprps_stk_nm": "비화학", "orgn_netprps_amt": "+500"},  # 50억
        {"for_netprps_stk_cd": "222222", "for_netprps_stk_nm": "비화학", "for_netprps_amt": "+200"},   # 20억
    ]


def screener(fake, tmp_path, **cfg):
    return AccumulationScreener(make_broker(fake), ScreenerConfig(**cfg), state_dir=str(tmp_path))


# ======================================================================== 스캐너
def test_or_rule_and_common_filters(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    res = screener(fake, tmp_path).run(TODAY, use_cache=False)
    assert res.prev_date == PREV
    assert [h.symbol for h in res.hits] == ["111111", "222222"]
    a, b = res.hits
    assert a.reasons == ["①"] and a.vol_ratio == 3.0
    assert b.reasons == ["②외국인", "②기관"] and b.net_buy_amount == 7_000_000_000
    assert "시총 5,000억" in a.detail and "시총 8,000억" in b.detail


def test_exclusion_reasons(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    syms = {h.symbol for h in screener(fake, tmp_path).run(TODAY, use_cache=False).hits}
    assert "333333" not in syms   # 시총 1,000억
    assert "444444" not in syms   # 주가 800원
    assert "555555" not in syms   # ETF
    assert "777777" not in syms   # 윗꼬리
    assert "888888" not in syms   # 우선주
    assert "666666" not in syms   # 순매수 5억 < 10억


def test_both_conditions_rank_first(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    fake.frgn_inst_rows.append({"for_netprps_stk_cd": "111111", "for_netprps_stk_nm": "에이전자",
                                "for_netprps_amt": "+300",
                                "orgn_netprps_stk_cd": "111111", "orgn_netprps_stk_nm": "에이전자",
                                "orgn_netprps_amt": "+150"})
    res = screener(fake, tmp_path).run(TODAY, use_cache=False)
    assert res.hits[0].symbol == "111111" and res.hits[0].reasons == ["①", "②외국인", "②기관"]


def test_net_buy_requires_both_foreign_and_inst(tmp_path):
    """외국인만 또는 기관만 순매수 상위인 종목은 ② 불통"""
    fake = FakeKiwoom()
    build_market(fake)
    fake.frgn_inst_rows = [
        {"for_netprps_stk_cd": "666666", "for_netprps_stk_nm": "에프소액", "for_netprps_amt": "+300",  # 외국인만
         "orgn_netprps_stk_cd": "222222", "orgn_netprps_stk_nm": "비화학", "orgn_netprps_amt": "+500"},  # 기관만
    ]
    syms = {h.symbol for h in screener(fake, tmp_path).run(TODAY, use_cache=False).hits}
    assert "666666" not in syms and "222222" not in syms
    # 한쪽 금액이 하한 미달이어도 불통
    fake.frgn_inst_rows.append({"for_netprps_stk_cd": "222222", "for_netprps_stk_nm": "비화학",
                                "for_netprps_amt": "+50"})   # 5억 < 10억
    syms = {h.symbol for h in screener(fake, tmp_path).run(TODAY, use_cache=False).hits}
    assert "222222" not in syms


def test_threshold_settings_respected(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    res = screener(fake, tmp_path, min_market_cap=10_000_000_000_000).run(TODAY, use_cache=False)  # 10조
    assert res.hits == []
    res = screener(fake, tmp_path, vol_surge_ratio=4.0).run(TODAY, use_cache=False)
    assert [h.symbol for h in res.hits] == ["222222"]


def test_today_bar_ignored_when_run_after_open(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    fake.add_daily("111111", "20261020", 10550, 10600, 10000, 10050, 900_000)   # 오늘 진행중 봉
    res = screener(fake, tmp_path).run(datetime(2026, 10, 20, 9, 30), use_cache=False)
    assert res.prev_date == PREV and res.hits[0].symbol == "111111"


def test_after_close_uses_today_bar_and_saves_for_next_day(tmp_path):
    """20시 스캔: 오늘(PREV=월) 봉이 판단일, 후보는 당일 상위, 결과는 다음 거래일(화) 파일로 저장"""
    fake = FakeKiwoom()
    build_market(fake)
    fake.today_top["1"], fake.prev_top["1"] = fake.prev_top["1"], []
    sc = screener(fake, tmp_path)
    res = sc.run(datetime(2026, 10, 19, 18, 0), use_cache=False, after_close=True)
    assert res.prev_date == PREV and res.trade_date == "20261020"
    assert [h.symbol for h in res.hits] == ["111111", "222222"]
    assert fake.api_calls("ka10030") and not fake.api_calls("ka10031")
    n = len(fake.api_calls("ka10081"))
    assert len(sc.run(TODAY).hits) == 2 and len(fake.api_calls("ka10081")) == n   # 다음 날 아침 재사용


def test_cache_reused_same_day(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    sc = screener(fake, tmp_path)
    sc.run(TODAY)
    n = len(fake.api_calls("ka10081"))
    res = sc.run(TODAY)
    assert len(fake.api_calls("ka10081")) == n and len(res.hits) == 2


def test_progress_reports_each_candidate(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    screener = AccumulationScreener(make_broker(fake), ScreenerConfig(), state_dir=str(tmp_path))
    seen = []
    screener.progress = lambda done, total: seen.append((done, total))
    screener.run(TODAY, use_cache=False)
    total = seen[0][1]
    assert total > 1 and seen == [(i, total) for i in range(total)]


def test_name_exclusion_helper():
    for n in ("KODEX 200", "TIGER 미국S&P500", "엔에이치스팩29호", "현대차2우B", "삼성전자우", "신한 인버스 ETN"):
        assert is_excluded_name(n), n
    for n in ("삼성전자", "현대차", "에코프로비엠", "NAVER", "LG화학"):
        assert not is_excluded_name(n), n


# ======================================================================== 엔진 연동
def _engine(fake, clock, tmp_path):
    config = StrategyConfig()
    from python_app.tests.test_notify import CaptureNotifier
    cap = CaptureNotifier()
    broker = make_broker(fake, TradingMode.MOCK, config)
    eng = LiveTradingEngine(broker, config, [("005930", "삼성전자")], state_dir=str(tmp_path), clock=clock,
                            settings=EngineSettings(bar_poll_delay_sec=3), notifier=cap,
                            screener=AccumulationScreener(broker, ScreenerConfig(), state_dir=str(tmp_path)))
    return eng, cap


def _wait_scan(eng):
    eng._screen_thread.join(10)


def test_engine_trades_only_screened_symbols(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    # 오늘 분봉: 매집종목 A(111111)와 비매집 삼성전자 모두 시가돌파
    for sym in ("111111", "005930"):
        fake.names.setdefault(sym, sym)
        fake.add_bar(sym, "20261020090000", 10000, 10050, 9900, 9950, 100_000)
        fake.add_bar(sym, "20261020090300", 9950, 10150, 9950, 10100, 80_000)
        fake.prices[sym] = 10100
    clock = Clock(TODAY)
    eng, cap = _engine(fake, clock, tmp_path)
    eng.start()
    assert eng.watchlist == []                     # 스캔 전에는 매수 대상 없음
    eng.tick(); _wait_scan(eng)
    assert [s for s, _ in eng.watchlist] == ["111111", "222222"]
    assert any("오늘 감시종목 2개" in m for m in cap.msgs)

    clock.set("09:06:02"); eng.tick()
    bought = [c["body"]["stk_cd"] for c in fake.api_calls("kt10000")]
    assert bought == ["111111"]                    # 삼성전자는 매집 종목이 아니라 매수 안 함


def test_engine_scan_failure_means_no_new_buys(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    fake.fail_apis = {"ka10031", "ka90009"}
    clock = Clock(TODAY)
    eng, cap = _engine(fake, clock, tmp_path)
    eng.start(); eng.tick(); _wait_scan(eng)
    assert eng.watchlist == []
    assert any("신규 매수 없음" in m for m in cap.msgs)


def test_engine_rescan_runs_once_per_day(tmp_path):
    fake = FakeKiwoom()
    build_market(fake)
    clock = Clock(TODAY)
    eng, _ = _engine(fake, clock, tmp_path)
    eng.start(); eng.tick(); _wait_scan(eng)
    n = len(fake.api_calls("ka10031"))
    clock.set("08:55:00"); eng.tick()
    assert eng._screen_thread is None or not eng._screen_thread.is_alive()
    assert len(fake.api_calls("ka10031")) == n
