"""엔진 ↔ 전략 관리자·후보 탐색기 연동: 전략 손절가, 전략별 청산, 오버나이트 보유, 후보 순위, 기록"""
from datetime import datetime

from python_app.backtest.bar_store import BarStore
from python_app.db.storage import DatabaseManager
from python_app.strategies.base_strategy import StrategySettings
from python_app.strategies.candidate_scanner import CandidateScanner, ScannerConfig
from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
from python_app.tests.stubs import OpenCrossStub
from python_app.tests.test_kiwoom_live import DAY, make_engine, seed_breakout_day


def seed(fake, sym, name):
    fake.names[sym] = name
    fake.add_bar(sym, f"{DAY}090000", 10000, 10050, 9900, 9950, 100_000)
    fake.add_bar(sym, f"{DAY}090300", 9950, 10150, 9950, 10100, 80_000)
    fake.prices[sym] = 10100


def bought(fake, tmp_path, clock=None, **kw):
    seed_breakout_day(fake)
    clock = clock or Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, events = make_engine(fake, clock, tmp_path, **kw)
    assert eng.start()
    eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["005930"].status == "HOLDING"
    return eng, events, clock


# ------------------------------------------------------------------ 전략 없음 (현재 실제 상태)
def test_without_strategies_engine_records_bars_but_never_buys(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    store = BarStore(str(tmp_path / "bars.db"))
    eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path, strategies=[], bar_store=store)
    eng.start(); eng.tick()
    assert not fake.api_calls("kt10000")
    assert len(store.load_bars("005930", "2026-10-20")) == 2
    assert any("활성 전략 없음" in e for e in events)


# ------------------------------------------------------------------ 손절가·청산 소유권
def test_market_entry_uses_the_stop_price_given_by_the_strategy(tmp_path):
    fake = FakeKiwoom()
    eng, _, clock = bought(fake, tmp_path, strategies=[OpenCrossStub(stop_pct=1.0)])
    m = eng.managed["005930"]
    assert (m.strategy, m.pattern, m.stop_price) == ("stub", "cross", 9999)
    fake.prices["005930"] = 10000                      # 전략 손절가 위 → 유지
    clock.set("09:07:00"); eng.tick()
    assert not fake.api_calls("kt10001")
    fake.prices["005930"] = 9999                       # 공통 손절(-2% = 9,898)보다 먼저 닿는다
    clock.set("09:07:03"); eng.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "전략 손절가" in eng.managed["005930"].exit_reason


def test_exit_is_judged_with_the_owning_strategys_settings(tmp_path):
    fake = FakeKiwoom()
    tight = OpenCrossStub(StrategySettings(start_time="09:03", end_time="10:00", exit={"stop_loss_rate": 1.0}))
    eng, _, clock = bought(fake, tmp_path, strategies=[tight])
    fake.prices["005930"] = 9999                       # 매입가 10,100 의 -1%
    clock.set("09:07:00"); eng.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "손절 -1%" in eng.managed["005930"].exit_reason
    assert eng.config.stop_loss_rate == 2.0            # 공통 설정은 그대로


def test_position_whose_strategy_is_gone_falls_back_to_common_rules(tmp_path):
    fake = FakeKiwoom()
    eng, _, clock = bought(fake, tmp_path)
    eng.stop()
    eng2, _ = make_engine(fake, clock, tmp_path, strategies=[])        # 전략이 빠진 채 재시작
    eng2.start()
    fake.prices["005930"] = 9898
    clock.set("09:10:00"); eng2.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "손절 -2%" in eng2.managed["005930"].exit_reason


def test_closed_trade_is_counted_for_its_strategy_only(tmp_path):
    fake = FakeKiwoom()
    eng, _, clock = bought(fake, tmp_path, strategies=[OpenCrossStub(), OpenCrossStub(name="other")])
    fake.prices["005930"] = 9898
    clock.set("09:07:00"); eng.tick()
    clock.set("09:07:30"); eng._last_sync = datetime.min; eng.tick()
    stats = eng.snapshot()["strategies"]
    assert (stats["stub"]["trades"], stats["stub"]["losses"]) == (1, 1) and stats["stub"]["pnl"] < 0
    assert stats["other"]["trades"] == 0 and stats["other"]["signals"] == 1     # 같은 봉에 신호는 냈지만 소유는 stub


def test_same_bar_signals_from_two_strategies_place_one_order(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path,
                              strategies=[OpenCrossStub(), OpenCrossStub(name="other")])
    eng.start(); eng.tick()
    assert len(fake.api_calls("kt10000")) == 1 and eng.managed["005930"].strategy == "stub"
    assert any("이미 진행 중인 주문/보유" in e for e in events)


# ------------------------------------------------------------------ 오버나이트 보유
def test_overnight_position_survives_eod_liquidation_and_is_still_guarded(tmp_path):
    fake = FakeKiwoom()
    eng, _, clock = bought(fake, tmp_path, strategies=[OpenCrossStub(hold_overnight=True)])
    assert eng.managed["005930"].hold_overnight and eng.managed["005930"].entry_date == DAY
    fake.prices["005930"] = 10150
    clock.set("15:15:00"); eng.tick()
    clock.set("15:16:00"); eng.tick()
    assert not fake.api_calls("kt10001")               # 일괄 청산 대상이 아니다
    fake.prices["005930"] = 9898                       # 청산 시각 이후에도 손절 감시는 계속
    clock.set("15:17:00"); eng.tick()
    assert len(fake.api_calls("kt10001")) == 1 and "손절" in eng.managed["005930"].exit_reason


def test_overnight_position_is_kept_at_next_open_and_closed_by_next_eod(tmp_path):
    fake = FakeKiwoom()
    eng, _, clock = bought(fake, tmp_path, strategies=[OpenCrossStub(hold_overnight=True)])
    fake.prices["005930"] = 10150
    clock.set("15:25:00"); eng.tick()
    eng.stop()

    clock.now = datetime(2026, 10, 21, 8, 55, 0)
    eng2, _ = make_engine(fake, clock, tmp_path, strategies=[OpenCrossStub(hold_overnight=True)])
    eng2.start()
    m = eng2.managed["005930"]
    assert m.carried_over and m.hold_overnight
    clock.set("09:00:01"); eng2.tick()
    clock.set("09:30:00"); eng2.tick()
    assert not fake.api_calls("kt10001")               # 익일 시초 청산 대상이 아니다
    clock.set("15:15:00"); eng2.tick()                 # 최대 1일 보유 → 다음 거래일 장 마감에는 청산
    assert len(fake.api_calls("kt10001")) == 1 and "장 마감 일괄 청산" in eng2.managed["005930"].exit_reason


def test_overnight_entry_is_allowed_after_eod_time_but_day_trade_entry_is_not(tmp_path):
    for overnight, expected in ((True, 1), (False, 0)):
        fake = FakeKiwoom()
        seed_breakout_day(fake)
        eng, _ = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path / str(overnight),
                             strategies=[OpenCrossStub(hold_overnight=overnight)], eod_liquidation_time="09:05")
        (tmp_path / str(overnight)).mkdir(exist_ok=True)
        eng.start(); eng.tick()
        assert len(fake.api_calls("kt10000")) == expected


# ------------------------------------------------------------------ 후보 탐색기 연동
def _scanner(broker):
    return CandidateScanner(broker, ScannerConfig())


def test_watchlist_follows_candidates_and_higher_rank_is_bought_first(tmp_path):
    fake = FakeKiwoom()
    seed(fake, "111111", "알파"); seed(fake, "222222", "베타")
    fake.add_rank("111111", "알파", 10100, 2.0, 1_000_000)
    fake.add_rank("222222", "베타", 10100, 6.0, 3_000_000)          # 등락률·거래대금 모두 우위
    store = BarStore(str(tmp_path / "bars.db"))
    eng, events = make_engine(fake, Clock(datetime(2026, 10, 20, 9, 6, 2)), tmp_path, scanner=_scanner,
                              bar_store=store, watchlist=(("005930", "삼성전자"),), max_concurrent_positions=1)
    eng.start(); eng.tick()
    assert [s for s, _ in eng.watchlist] == ["222222", "111111"]     # 고정 종목이 아니라 후보를 본다
    buys = fake.api_calls("kt10000")
    assert [b["body"]["stk_cd"] for b in buys] == ["222222"]
    assert any("최대 동시 보유" in e for e in events)
    assert store.symbols("2026-10-20") == ["111111", "222222"]
    snap = eng.snapshot()
    assert snap["states"]["222222"]["rank"] == 1 and snap["states"]["222222"]["signals"] == ["stub"]
    assert "후보 2종목" in snap["scan_status"]


def test_held_symbol_stays_watched_after_it_drops_out_of_candidates(tmp_path):
    fake = FakeKiwoom()
    seed(fake, "222222", "베타")
    fake.add_rank("222222", "베타", 10100, 6.0, 3_000_000)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, _ = make_engine(fake, clock, tmp_path, scanner=_scanner)
    eng.start(); eng.tick()
    clock.set("09:06:20"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["222222"].status == "HOLDING"
    fake.today_top = {"1": [], "3": []}; fake.change_top = []
    fake.add_rank("333333", "감마", 5000, 4.0, 5_000_000)
    clock.set("09:09:04"); eng.tick()
    assert [s for s, _ in eng.watchlist] == ["333333", "222222"]
    assert eng.snapshot()["states"]["222222"]["rank"] is None


def test_scan_failure_keeps_candidates_and_warns_once(tmp_path):
    fake = FakeKiwoom()
    seed(fake, "222222", "베타")
    fake.add_rank("222222", "베타", 10100, 6.0, 3_000_000)
    clock = Clock(datetime(2026, 10, 20, 9, 6, 2))
    eng, events = make_engine(fake, clock, tmp_path, scanner=_scanner, strategies=[])
    eng.start(); eng.tick()
    fake.fail_apis = {"ka10030", "ka10027"}
    clock.set("09:09:04"); eng.tick()
    clock.set("09:12:04"); eng.tick()
    assert [s for s, _ in eng.watchlist] == ["222222"]
    assert sum("후보 조회 오류" in e for e in events) == 1


# ------------------------------------------------------------------ 기록
def test_closing_auction_bar_is_recorded_without_orders(tmp_path):
    fake = FakeKiwoom()
    seed_breakout_day(fake)
    fake.add_bar("005930", f"{DAY}151800", 10100, 10120, 10080, 10100, 5_000)
    fake.add_bar("005930", f"{DAY}153000", 10100, 10110, 10100, 10110, 90_000)
    fake.add_bar("005930", f"{DAY}153300", 10110, 10110, 10110, 10110, 100)     # 시간외 (아직 진행 중)
    store = BarStore(str(tmp_path / "bars.db"))
    eng, _ = make_engine(fake, Clock(datetime(2026, 10, 20, 15, 33, 5)), tmp_path, bar_store=store)
    eng.start(); eng.tick()
    times = [b.timestamp.strftime("%H:%M") for b in store.load_bars("005930", "2026-10-20")]
    assert times == ["09:03", "09:06", "15:21", "15:33"]
    assert [b.timestamp.strftime("%H:%M") for b in eng.manager.sessions["005930"].bars] == times
    assert not fake.api_calls("kt10000") and not fake.api_calls("kt10001")


def test_trades_and_signals_are_stored_with_strategy_and_mode(tmp_path):
    fake = FakeKiwoom()
    db = DatabaseManager(str(tmp_path / "t.db"))
    eng, _, clock = bought(fake, tmp_path, db=db, strategies=[OpenCrossStub(stop_pct=1.0)])
    fake.prices["005930"] = 9999
    clock.set("09:07:00"); eng.tick()
    clock.set("09:07:30"); eng._last_sync = datetime.min; eng.tick()
    assert eng.managed["005930"].status == "CLOSED"
    sig = db.get_recent_signals()[0]
    assert (sig["strategy"], sig["pattern"], sig["signal_type"], sig["stop_price"]) == \
           ("stub", "cross", "BUY_STUB.CROSS", 9999)
    trades = {t["side"]: t for t in db.get_recent_trades()}
    assert trades["BUY"]["strategy"] == trades["SELL"]["strategy"] == "stub"
    assert trades["BUY"]["mode"] == "MOCK" and trades["BUY"]["reason"] == "stub.cross"
    stats = db.get_strategy_stats("MOCK")
    assert len(stats) == 1 and (stats[0]["strategy"], stats[0]["trades"], stats[0]["wins"], stats[0]["losses"]) == \
           ("stub", 1, 0, 1)
    assert stats[0]["pnl"] < 0 and db.get_strategy_stats("REAL") == []


def test_old_database_gets_new_columns_without_losing_rows(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE trades (trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    name TEXT, side TEXT NOT NULL, price REAL NOT NULL, qty INTEGER NOT NULL, commission REAL NOT NULL,
                    tax REAL NOT NULL, realized_pnl REAL NOT NULL, reason TEXT, executed_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO trades VALUES ('t1','o1','005930','삼성전자','SELL',10000,1,0,0,-500,'손절','2026-10-19T10:00:00')")
    conn.commit(); conn.close()
    db = DatabaseManager(path)
    DatabaseManager(path)                                  # 두 번 열어도 문제없다
    rows = db.get_recent_trades()
    assert len(rows) == 1 and rows[0]["realized_pnl"] == -500 and rows[0]["strategy"] is None
    assert db.get_strategy_stats("MOCK") == []             # 이전 전략 거래는 새 전략 통계에 섞이지 않는다
