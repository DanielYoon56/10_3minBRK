"""실시간 후보 탐색기: 발굴, 제외, 순위, 교체, 조회 실패"""
from datetime import datetime

from python_app.models.types import Bar
from python_app.strategies.base_strategy import SymbolSession
from python_app.strategies.candidate_scanner import (
    Candidate, CandidateScanner, ScannerConfig, is_excluded_name, rank_candidates,
)
from python_app.tests.fake_kiwoom import FakeKiwoom
from python_app.tests.test_kiwoom_live import make_broker

NOW = datetime(2026, 10, 20, 9, 6, 4)


def scanner(fake, **cfg):
    return CandidateScanner(make_broker(fake), ScannerConfig(**cfg))


def test_rank_rows_are_parsed_with_units():
    fake = FakeKiwoom()
    fake.add_rank("005930", "삼성전자", 70000, 1.5, 1_000_000)
    b = make_broker(fake)
    row = b.get_today_rank("3")[0]
    assert row == {"symbol": "005930", "name": "삼성전자", "price": 70000, "change_rate": 1.5,
                   "volume": 1_000_000, "trade_value": 70_000_000_000}        # 백만원 → 원
    fake.change_top[0] = {k: v for k, v in fake.change_top[0].items() if k != "trde_amt"}
    assert b.get_change_rank()[0]["trade_value"] == 70_000_000_000            # 거래대금 없는 TR 은 현재가 × 거래량


def test_sources_are_merged_without_duplicates():
    fake = FakeKiwoom()
    fake.add_rank("005930", "삼성전자", 70000, 1.5, 1_000_000)                   # 세 순위 모두
    fake.add_rank("000660", "SK하이닉스", 200000, 3.0, 500_000, sources=("change",))
    sc = scanner(fake)
    added, removed = sc.refresh(NOW)
    assert sorted(c.symbol for c in added) == ["000660", "005930"] and not removed
    assert sc.get("005930").sources == ["value", "volume", "change"]
    assert sc.get("000660").sources == ["change"]
    assert len(sc.watchlist()) == 2


def test_unsuitable_stocks_are_excluded():
    fake = FakeKiwoom()
    fake.add_rank("111111", "정상종목", 10000, 5.0, 1_000_000)        # 100억
    fake.add_rank("222222", "KODEX 레버리지", 10000, 5.0, 1_000_000)
    fake.add_rank("333333", "가나다우", 10000, 5.0, 1_000_000)
    fake.add_rank("444444", "동전주", 900, 5.0, 100_000_000)
    fake.add_rank("555555", "거래부족", 10000, 5.0, 100_000)          # 10억
    fake.add_rank("666666", "상한가근처", 10000, 29.5, 1_000_000)
    fake.add_rank("777777", "하락종목", 10000, -1.0, 1_000_000)
    sc = scanner(fake)
    sc.refresh(NOW)
    assert [s for s, _ in sc.watchlist()] == ["111111"]
    assert is_excluded_name("TIGER 200") and is_excluded_name("현대차2우B") and not is_excluded_name("현대차")


def test_ranking_prefers_momentum_and_liquidity_and_penalises_overheating():
    cfg = ScannerConfig()
    strong = Candidate("A", "강함", price=10000, change_rate=8.0, volume=3_000_000, trade_value=3e10)
    weak = Candidate("B", "약함", price=10000, change_rate=1.0, volume=1_000_000, trade_value=1e10)
    hot = Candidate("C", "과열", price=10000, change_rate=24.0, volume=2_000_000, trade_value=2e10)
    ranked = rank_candidates([weak, hot, strong], cfg)
    assert [c.symbol for c in ranked] == ["A", "C", "B"] and [c.rank for c in ranked] == [1, 2, 3]
    no_penalty = rank_candidates([Candidate("C", "과열", 10000, 24.0, 2_000_000, 2e10),
                                  Candidate("A", "강함", 10000, 8.0, 3_000_000, 3e10)],
                                 ScannerConfig(w_overheat=0.0))
    assert no_penalty[0].score > 0 and hot.score < no_penalty[[c.symbol for c in no_penalty].index("C")].score


def test_price_structure_breaks_ties():
    def sess(sym, close):
        s = SymbolSession(sym, sym, "2026-10-20")
        s.add(Bar(sym, datetime(2026, 10, 20, 9, 3), 10000, 10500, 9900, 10400, 1000))
        s.add(Bar(sym, datetime(2026, 10, 20, 9, 6), 10400, 10450, min(close, 10400), close, 1000))
        return s
    a = Candidate("A", "고가근처", price=10000, change_rate=5.0, volume=1_000_000, trade_value=1e10)
    b = Candidate("B", "밀림", price=10000, change_rate=5.0, volume=1_000_000, trade_value=1e10)
    ranked = rank_candidates([b, a], ScannerConfig(), {"A": sess("A", 10450), "B": sess("B", 9950)})
    assert ranked[0].symbol == "A" and a.score > b.score


def test_candidates_are_replaced_as_the_market_changes():
    fake = FakeKiwoom()
    fake.add_rank("111111", "먼저", 10000, 5.0, 1_000_000)
    sc = scanner(fake)
    sc.refresh(NOW)
    fake.today_top = {"1": [], "3": []}; fake.change_top = []
    fake.add_rank("222222", "새로", 10000, 6.0, 2_000_000)
    added, removed = sc.refresh(NOW.replace(minute=9))
    assert [c.symbol for c in added] == ["222222"] and [c.symbol for c in removed] == ["111111"]
    assert sc.get("222222").first_seen == "09:09" and sc.get("111111") is None
    fake.add_rank("111111", "먼저", 10000, 5.0, 1_000_000)
    sc.refresh(NOW.replace(minute=12))
    assert sc.get("222222").first_seen == "09:09" and sc.get("222222").last_seen == "09:12"


def test_only_top_n_are_kept():
    fake = FakeKiwoom()
    for i in range(10):
        fake.add_rank(f"{i:06d}", f"종목{i}", 10000, 1.0 + i, 1_000_000 + i)
    sc = scanner(fake, max_candidates=3)
    sc.refresh(NOW)
    assert [s for s, _ in sc.watchlist()] == ["000009", "000008", "000007"]
    assert sc.rank_of("000009") == 1 and sc.rank_of("000001") > 1000


def test_total_failure_keeps_previous_candidates_and_goes_stale():
    fake = FakeKiwoom()
    fake.add_rank("111111", "먼저", 10000, 5.0, 1_000_000)
    sc = scanner(fake)
    assert sc.is_stale(NOW)                       # 한 번도 조회하지 못한 상태
    sc.refresh(NOW)
    fake.fail_apis = {"ka10030", "ka10027"}
    added, removed = sc.refresh(NOW.replace(minute=9))
    assert not added and not removed and sc.get("111111") is not None
    assert "ka10030" in sc.last_error and sc.last_refresh == NOW
    assert not sc.is_stale(NOW.replace(minute=15)) and sc.is_stale(NOW.replace(minute=17))


def test_partial_failure_uses_remaining_sources():
    fake = FakeKiwoom()
    fake.add_rank("111111", "먼저", 10000, 5.0, 1_000_000)
    fake.fail_apis = {"ka10027"}
    sc = scanner(fake)
    sc.refresh(NOW)
    assert sc.get("111111").sources == ["value", "volume"] and "change" in sc.last_error
