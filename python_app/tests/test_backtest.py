"""백테스트: 재생 엔진의 체결 가정·미래 정보 차단·진입 한도, 성과 지표, 내려받기 보조 함수"""
from datetime import datetime

import pytest

from python_app.backtest.download import candidate_day_counts, regular_bars
from python_app.backtest.metrics import exit_kind, report, summarize
from python_app.backtest.replay import BacktestConfig, Replay, Trade
from python_app.models.types import Bar, StrategyConfig
from python_app.risk.capital_guard import RiskConfig
from python_app.strategies.base_strategy import BaseStrategy, StrategySettings
from python_app.strategies.candidate_scanner import ScannerConfig

D = "2026-10-20"
FEE, TAX = 0.00015, 0.0020
NO_RISK_SIZING = dict(max_loss_per_trade=10 ** 9, max_consecutive_losses=0)    # 수량 = 1회 매수 금액 기준


def B(hhmm, o, h, l, c, v=100_000, sym="A", day=D):
    return Bar(symbol=sym, timestamp=datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"),
               open=o, high=h, low=l, close=c, volume=v)


def daily(*syms, close=10000):
    return {s: [{"dt": "20261019", "open": close, "high": close, "low": close, "close": close, "volume": 1}]
            for s in syms}


class At(BaseStrategy):
    """정해진 시각의 봉에서 신호 — 재생 엔진의 체결·청산을 확인하기 위한 전략"""
    name = "t"

    def __init__(self, at="09:06", stop=None, target=None, exit=None, end="15:00"):
        super().__init__(StrategySettings(start_time="09:03", end_time=end, exit=exit or {}))
        self.at, self.stop, self.target, self.seen = at, stop, target, []

    def on_bar(self, s, ctx=None):
        self.seen.append((s.symbol, s.last.time_str, ctx.index if ctx else None))
        if s.last.time_str != self.at:
            return None
        return self.make_signal(s, "p", "테스트", stop_price=self.stop, target_price=self.target)


def replay(strategy_kw=None, slippage=0, scanner=False, scanner_cfg=None, **common):
    kw = strategy_kw or {}
    made = []

    def factory():
        made.append(At(**kw))
        return [made[-1]]

    r = Replay(factory, StrategyConfig(**common), scanner_cfg or ScannerConfig(),
               BacktestConfig(slippage_ticks=slippage, use_scanner=scanner), risk=RiskConfig(**NO_RISK_SIZING))
    r.made = made
    return r


HEAD = [B("09:03", 10000, 10050, 9990, 10040), B("09:06", 10040, 10110, 10030, 10100)]   # 09:06 봉에서 신호


def run(bars, **kw):
    return replay(**kw).run_day(D, {"A": bars}, daily("A"))


# ------------------------------------------------------------------ 체결 가정
def test_entry_fills_at_next_bar_open_never_on_the_signal_bar():
    res = run(HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("15:15", 10150, 10160, 10140, 10150)])
    t = res.trades[0]
    assert (t.signal_time.strftime("%H:%M"), t.entry_time.strftime("%H:%M"), t.entry_price) == ("09:06", "09:09", 10150)
    assert t.qty == 98                                   # 100만원 ÷ (10,150 × 1.00015)


def test_signal_without_a_following_bar_is_not_filled():
    res = run(HEAD)
    assert res.trades == [] and res.signals == 1 and res.skipped == {"다음 봉 없음 (미체결)": 1}


def test_stop_fills_at_the_stop_price_inside_the_bar():
    res = run(HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10160, 9950, 9960)],
              strategy_kw=dict(stop=10000.0))
    t = res.trades[0]
    assert (t.exit_price, t.exit_time.strftime("%H:%M")) == (10000, "09:12") and exit_kind(t) == "손절"
    expected = (10000 - 10150) * 98 - (10000 + 10150) * 98 * FEE - 10000 * 98 * TAX
    assert t.pnl == pytest.approx(expected) and t.pnl < -14_700


def test_gap_below_stop_fills_at_the_open():
    res = run(HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 9900, 9920, 9880, 9900)],
              strategy_kw=dict(stop=10000.0))
    assert res.trades[0].exit_price == 9900


def test_stop_on_the_entry_bar_itself_is_honoured():
    res = run(HEAD + [B("09:09", 10150, 10160, 9990, 10000)], strategy_kw=dict(stop=10000.0))
    t = res.trades[0]
    assert t.entry_time == t.exit_time and t.exit_price == 10000


def test_entry_is_skipped_when_next_open_is_already_at_or_below_the_stop():
    res = run(HEAD + [B("09:09", 9990, 10000, 9980, 9990)], strategy_kw=dict(stop=10000.0))
    assert res.trades == [] and res.skipped == {"체결가가 손절가 이하 (갭 하락)": 1}


def test_when_stop_and_target_are_both_touched_in_one_bar_the_stop_wins():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10500, 9990, 10400)]
    assert exit_kind(run(bars, strategy_kw=dict(stop=10000.0, target=10400.0)).trades[0]) == "손절"


def test_target_fills_at_the_target_price():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10500, 10140, 10450)]
    t = run(bars, strategy_kw=dict(stop=10000.0, target=10400.0), trailing_retrace_rate=0.0).trades[0]
    assert t.exit_price == 10400 and exit_kind(t) == "목표가" and t.pnl > 0


def test_trailing_stop_uses_the_live_exit_rules():
    # 진입 10,150 · 공통 손절 -2%(9,947, 1R = 203) · 최고가 10,400(+250 ≥ 1R) → 30% 되돌림 = 10,325
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10200, 10400, 10190, 10390),
                   B("09:15", 10380, 10390, 10300, 10310)]
    t = run(bars).trades[0]
    assert t.exit_price == 10325 and exit_kind(t) == "트레일링" and t.exit_time.strftime("%H:%M") == "09:15"


def test_strategy_exit_override_is_used_for_its_own_trades():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10160, 10040, 10050)]
    assert exit_kind(run(bars + [B("15:15", 10050, 10060, 10040, 10050)]).trades[0]) == "장 마감 청산"    # 공통 -2% 미도달
    t = run(bars, strategy_kw=dict(exit={"stop_loss_rate": 1.0})).trades[0]
    assert exit_kind(t) == "손절" and t.exit_price == round(10150 * 0.99)


def test_position_is_closed_at_end_of_day():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("15:12", 10200, 10210, 10190, 10200),
                   B("15:15", 10200, 10230, 10190, 10220), B("15:18", 10220, 10300, 10210, 10300)]
    t = run(bars).trades[0]
    assert (t.exit_time.strftime("%H:%M"), t.exit_price) == ("15:15", 10220) and exit_kind(t) == "장 마감 청산"
    illiquid = run(HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("14:00", 10200, 10210, 10190, 10200)]).trades[0]
    assert illiquid.exit_time.strftime("%H:%M") == "14:00" and exit_kind(illiquid) == "장 마감 청산"


def test_slippage_and_costs_are_always_charged():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("15:15", 10150, 10150, 10150, 10150)]
    free = run(bars).trades[0]
    slipped = run(bars, slippage=1).trades[0]
    assert (slipped.entry_price, slipped.exit_price) == (10160, 10140)        # 호가 단위 10원씩 불리하게
    assert free.pnl == pytest.approx(-(10150 + 10150) * 98 * FEE - 10150 * 98 * TAX)   # 가격이 같아도 비용만큼 손실
    assert slipped.pnl < free.pnl < 0


# ------------------------------------------------------------------ 진입 한도
def two_symbols():
    tail = [B("09:09", 10150, 10160, 10140, 10150), B("15:15", 10150, 10160, 10140, 10150)]
    return {s: [Bar(s, b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in HEAD + tail] for s in ("A", "B")}


def test_position_limit_and_no_reentry():
    r = replay(max_concurrent_positions=1)
    res = r.run_day(D, two_symbols(), daily("A", "B"))
    assert [t.symbol for t in res.trades] == ["A"] and res.skipped == {"최대 동시 보유 1종목 도달": 1}
    both = replay(max_concurrent_positions=3).run_day(D, two_symbols(), daily("A", "B"))
    assert sorted(t.symbol for t in both.trades) == ["A", "B"]


def test_daily_loss_limit_blocks_later_entries():
    bars = two_symbols()
    bars["A"] = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10160, 9000, 9000)]   # 큰 손실
    bars["B"] = [B("09:03", 10000, 10050, 9990, 10040, sym="B"), B("09:06", 10040, 10110, 10030, 10100, sym="B"),
                 B("09:15", 10040, 10110, 10030, 10100, sym="B"), B("09:18", 10100, 10110, 10090, 10100, sym="B")]

    class Late(At):
        def on_bar(self, s, ctx=None):
            self.at = "09:06" if s.symbol == "A" else "09:15"
            return super().on_bar(s, ctx)

    r = Replay(lambda: [Late(stop=9000.0)], StrategyConfig(max_daily_loss=50_000), ScannerConfig(),
               BacktestConfig(slippage_ticks=0, use_scanner=False), risk=RiskConfig(**NO_RISK_SIZING))
    res = r.run_day(D, bars, daily("A", "B"))
    assert [t.symbol for t in res.trades] == ["A"] and res.trades[0].pnl < -50_000
    assert res.skipped == {"일 손실한도 도달": 1}


def test_no_entry_after_eod_time():
    bars = [B("15:12", 10000, 10050, 9990, 10040), B("15:15", 10040, 10110, 10030, 10100),
            B("15:18", 10100, 10110, 10090, 10100)]
    res = run(bars, strategy_kw=dict(at="15:15", end="15:20"))
    assert res.trades == [] and res.skipped == {"장 마감 청산 시각(15:15) 이후": 1}


# ------------------------------------------------------------------ 후보 순위·미래 정보
def test_scanner_filter_keeps_illiquid_symbols_out():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("15:15", 10150, 10160, 10140, 10150)]
    cfg = ScannerConfig(min_trade_value=10_000_000_000)                 # 누적 100억 — 이 종목은 봉당 약 10억
    r = replay(scanner=True, scanner_cfg=cfg)
    res = r.run_day(D, {"A": bars}, daily("A"))
    assert res.trades == [] and res.signals == 0 and r.made[0].seen == []


def test_symbol_that_becomes_a_candidate_late_cannot_trade_an_earlier_signal():
    bars = HEAD + [B("09:09", 10150, 10160, 10140, 10150), B("09:12", 10150, 10160, 10140, 10150),
                   B("15:15", 10150, 10160, 10140, 10150)]
    cfg = ScannerConfig(min_trade_value=3_500_000_000)                  # 네 번째 봉(09:12)에서야 누적 40억
    r = replay(scanner=True, scanner_cfg=cfg)
    res = r.run_day(D, {"A": bars}, daily("A"))
    assert res.trades == [] and res.skipped == {"지나간 봉의 신호 (늦게 후보가 됨)": 1}
    assert [hhmm for _, hhmm, _ in r.made[0].seen][:2] == ["09:03", "09:06"]     # 지난 봉은 후보가 된 뒤에야 전달됨


def test_strategy_only_ever_sees_bars_up_to_the_current_time():
    class Peek(At):
        def on_bar(self, s, ctx=None):
            assert all(b.timestamp <= s.last.timestamp for b in s.bars) and ctx.now == s.last.timestamp
            return super().on_bar(s, ctx)

    r = Replay(lambda: [Peek()], StrategyConfig(), ScannerConfig(), BacktestConfig(slippage_ticks=0, use_scanner=False))
    res = r.run_day(D, {"A": HEAD + [B("09:09", 10150, 10160, 10140, 10150)]}, daily("A"))
    assert len(res.trades) == 1


def test_symbol_without_a_previous_daily_bar_is_not_watched():
    res = replay().run_day(D, {"A": HEAD + [B("09:09", 10150, 10160, 10140, 10150)]}, {})
    assert res.trades == [] and res.signals == 0


def test_index_change_comes_from_the_previous_days_close():
    def idx(day, close):
        return {s: [Bar(s, datetime.strptime(f"{day} {h}", "%Y-%m-%d %H:%M"), close, close, close, close, 1)
                    for h in ("09:03", "09:06", "09:09")] for s in ("IDX001", "IDX101")}

    r = replay()
    day1 = {"A": HEAD + [B("09:09", 10150, 10160, 10140, 10150)], **idx(D, 1000.0)}
    r.run_day(D, day1, daily("A"))
    assert all(index is None for _, _, index in r.made[0].seen)         # 첫날은 전일 지수를 모른다
    d2 = "2026-10-21"
    day2 = {"A": [B(b.timestamp.strftime("%H:%M"), b.open, b.high, b.low, b.close, day=d2) for b in day1["A"]]}
    day2.update(idx(d2, 990.0))
    day2["IDX101"] = idx(d2, 1005.0)["IDX101"]
    r.run_day(d2, day2, daily("A"))
    assert r.made[1].seen[0][2] == {"change_rate": pytest.approx(-1.0)}  # 코스피 -1.0%, 코스닥 +0.5% 중 약한 쪽


def test_multi_day_run_reads_from_the_store(tmp_path):
    from python_app.backtest.bar_store import BarStore
    store = BarStore(str(tmp_path / "h.db"))
    store.save_daily("A", daily("A")["A"])
    for day in (D, "2026-10-21"):
        store.save_bars([B(b.timestamp.strftime("%H:%M"), b.open, b.high, b.low, b.close, day=day)
                         for b in HEAD + [B("09:09", 10150, 10160, 10140, 10150)]])
    seen = []
    results = replay().run(store, store.dates(), progress=lambda i, n: seen.append((i, n)))
    assert [len(r.trades) for r in results] == [1, 1] and seen == [(1, 2), (2, 2)]
    assert store.load_daily("A")[0]["close"] == 10000


# ------------------------------------------------------------------ 성과 지표
def _trades(pnls):
    out = []
    for i, p in enumerate(pnls):
        t = Trade("s", "p", "A", "A", datetime(2026, 10, 20, 9, 6), datetime(2026, 10, 20, 9, 9), 10000, 100, 9800,
                  exit_time=datetime(2026, 10, 20, 9, 12 + i), exit_price=10000 + p / 100, exit_reason="손절 -2%", pnl=p)
        out.append(t)
    return out


def test_summary_numbers():
    s = summarize(_trades([100, -50, -30, 200, -20, -10, -5]))
    assert (s["trades"], s["wins"], s["losses"]) == (7, 2, 5)
    assert s["win_rate"] == pytest.approx(2 / 7 * 100)
    assert (s["avg_win"], s["avg_loss"]) == (150, pytest.approx(-23))
    assert (s["largest_win"], s["largest_loss"]) == (200, -50)
    assert s["profit_factor"] == pytest.approx(300 / 115)
    assert s["expectancy"] == pytest.approx(185 / 7) and s["net_profit"] == 185
    assert s["max_drawdown"] == 80                       # 누적 100 → 20
    assert s["max_consecutive_losses"] == 3
    assert s["avg_holding_min"] == pytest.approx(6.0)    # 3, 4, …, 9분


def test_summary_of_nothing_and_of_only_wins_does_not_invent_values():
    empty = summarize([])
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["profit_factor"] is None
    wins = summarize(_trades([10, 20]))
    assert wins["profit_factor"] is None and wins["avg_loss"] is None and wins["max_drawdown"] == 0


def test_report_groups_by_strategy_pattern_exit_and_month():
    text = report(_trades([100, -50]), "검증")
    for label in ("== 검증 ==", "전체", "[전략] s", "[패턴] s.p", "[청산] 손절", "[월] 2026-10"):
        assert label in text
    assert report([], "빈 구간").endswith("거래 없음")


# ------------------------------------------------------------------ 내려받기 보조
def test_minute_rows_are_relabelled_to_bar_end_and_limited_to_the_regular_session():
    def row(hhmmss, c):
        return {"cntr_tm": f"20261020{hhmmss}", "open_pric": f"+{c}", "high_pric": f"+{c}", "low_pric": f"-{c}",
                "cur_prc": f"-{c}", "trde_qty": "100"}
    rows = [row("195700", 9), row("153300", 8), row("153000", 7), row("151800", 6), row("090000", 5),
            row("090000", 5), row("085700", 4), {"cntr_tm": "bad"}]
    bars = regular_bars("A", rows)
    assert sorted((b.timestamp.strftime("%H:%M"), b.close) for b in bars) == [("09:03", 5), ("15:21", 6), ("15:33", 7)]
    assert regular_bars("IDX001", [row("090000", 529053)], scale=100.0)[0].close == pytest.approx(5290.53)


def test_candidate_day_counts_use_same_day_value_and_change():
    def rows(*closes, volume=1_000_000):
        return [{"dt": f"2026101{i}", "open": c, "high": c, "low": c, "close": c, "volume": volume}
                for i, c in enumerate(closes)]
    data = {"BIG": rows(10000, 10000, 10000), "JUMP": rows(1000, 1000, 1300, volume=6_000_000),
            "THIN": rows(10000, 12000, 14000, volume=100)}
    counts = candidate_day_counts(data, days=10, min_value=5_000_000_000, top=1)
    # BIG 은 이틀 모두 거래대금 1위, JUMP 는 둘째 날 상승률 1위, THIN 은 급등했지만 거래대금 미달
    assert counts["BIG"] == 2 and counts["JUMP"] == 1 and "THIN" not in counts
