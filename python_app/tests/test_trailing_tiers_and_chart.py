"""트레일링 되돌림 구간(1R 미만/이상), 설정 이전, 스캔 결과 재사용, 일봉 차트"""
import json
import os
from datetime import datetime

import pytest

from python_app.app_config import (
    load_config, matching_rule_preset, rule_preset_config, save_config, strategy_config_from,
)
from python_app.models.types import Position, SignalType, StrategyConfig
from python_app.screener.accumulation import ScreenHit, ScreenResult, latest_cached
from python_app.strategy.exit_rules import check_exit, reached_1r, trailing_stop_price


def _pos(entry=10_000, stop=0.0):
    return Position(symbol="005930", name="삼성전자", qty=10, avg_price=entry, current_price=entry,
                    stop_loss_price=stop, take_profit_price=0.0, entry_time=datetime.now(), highest_price=entry)


# ------------------------------------------------------------------ 구간 경계 (진입 10,000 / 손절 -2% → 1R = 200)
@pytest.mark.parametrize("peak, expected", [
    (10_099, None),       # 발동 전 (+1% 미만)
    (10_100, 10_050),     # 1R 미만: 100의 50%
    (10_199, 10_099.5),   # 1R 바로 아래: 199의 50%
    (10_200, 10_140),     # 정확히 1R: 200의 30%
    (11_000, 10_700),     # 1R 이상: 1,000의 30%
])
def test_retrace_by_peak_zone(peak, expected):
    assert trailing_stop_price(10_000, peak, StrategyConfig(), 9_800) == (
        None if expected is None else pytest.approx(expected))


def test_trail_never_drops_when_crossing_1r():
    cfg = StrategyConfig()
    trails = [trailing_stop_price(10_000, p, cfg, 9_800) for p in range(10_100, 11_000)]
    assert trails == sorted(trails)


def test_zone_stays_after_price_falls_back_below_1r():
    cfg, pos = StrategyConfig(), _pos(stop=9_800)
    assert check_exit(pos, 10_300, cfg) is None      # 최고가 1R 초과 → 청산선 10,210
    assert check_exit(pos, 10_211, cfg) is None
    d = check_exit(pos, 10_210, cfg)                 # 현재가는 1R 근처지만 30% 구간 유지
    assert d.signal_type == SignalType.SELL_TRAILING_STOP
    assert "30% 되돌림 [1R 이상]" in d.reason


def test_reason_below_1r():
    cfg, pos = StrategyConfig(), _pos(stop=9_800)
    assert check_exit(pos, 10_150, cfg) is None
    d = check_exit(pos, 10_075, cfg)
    assert "50% 되돌림 [1R 미만]" in d.reason


def test_1r_uses_position_stop_not_global_rate():
    cfg = StrategyConfig()
    # SMC 등 전략 손절가 9,500 → 1R = 500. +3% 는 전역 손절(-2%) 기준이면 1R 이상이지만 여기선 미만
    assert not reached_1r(10_000, 10_300, cfg, 9_500)
    assert trailing_stop_price(10_000, 10_300, cfg, 9_500) == pytest.approx(10_150)
    assert trailing_stop_price(10_000, 10_500, cfg, 9_500) == pytest.approx(10_350)


def test_without_stop_price_uses_stop_loss_rate():
    assert trailing_stop_price(10_000, 10_200, StrategyConfig(stop_loss_rate=2.0)) == pytest.approx(10_140)
    assert trailing_stop_price(10_000, 10_200, StrategyConfig(stop_loss_rate=3.0)) == pytest.approx(10_100)


def test_zero_1r_rate_means_single_rate():
    cfg = StrategyConfig(trailing_retrace_rate=15.0, trailing_retrace_rate_1r=0.0)
    assert trailing_stop_price(10_000, 11_000, cfg, 9_800) == pytest.approx(10_850)


def test_zero_base_rate_still_disables_trailing():
    assert trailing_stop_price(10_000, 11_000, StrategyConfig(trailing_retrace_rate=0.0), 9_800) is None


# ------------------------------------------------------------------ 설정 이전
def test_v2_config_moves_to_new_trailing_rates(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 2, "trailing_retrace_rate": 52.0}), encoding="utf-8")
    sc = strategy_config_from(load_config(str(path)))
    assert (sc.trailing_retrace_rate, sc.trailing_retrace_rate_1r) == (50.0, 30.0)


def test_v3_config_drops_per_strategy_trailing_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 3, "dsh_stop_rate": 2.0, "dsh_trail_retrace_rate": 50.0,
                                "dsh_trail_activation_rate": 1.0, "smc_use_trailing": True}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg["config_version"] == 5
    assert not {"dsh_trail_retrace_rate", "dsh_trail_activation_rate", "smc_use_trailing"} & set(cfg)
    strategy_config_from(cfg)


def test_preset_saved_with_old_trailing_keys_still_works():
    cfg = {"rule_presets": {"옛날": {"stop_loss_rate": 1.5, "dsh_trail_retrace_rate": 40.0, "smc_use_trailing": True}}}
    assert rule_preset_config(cfg, "옛날").stop_loss_rate == 1.5
    assert matching_rule_preset(cfg, StrategyConfig(stop_loss_rate=1.5)) == "옛날"


def test_user_values_after_migration_are_kept(tmp_path):
    path = str(tmp_path / "config.json")
    save_config({"config_version": 2}, path)
    cfg = load_config(path)
    cfg["trailing_retrace_rate"], cfg["trailing_retrace_rate_1r"] = 40.0, 20.0
    save_config(cfg, path)
    sc = strategy_config_from(load_config(path))
    assert (sc.trailing_retrace_rate, sc.trailing_retrace_rate_1r) == (40.0, 20.0)


# ------------------------------------------------------------------ 저장된 스캔 결과
def _write_screen(folder, day, prev):
    res = ScreenResult(trade_date=day, prev_date=prev, created_at="20:00:00",
                       hits=[ScreenHit("005930", "삼성전자", ["①"], detail="거래량 3.0배")])
    (folder / f"screen_{day}.json").write_text(json.dumps(res.to_json()), encoding="utf-8")


def test_latest_cached_picks_newest_readable_file(tmp_path):
    assert latest_cached(str(tmp_path)) is None
    _write_screen(tmp_path, "20261104", "20261103")
    _write_screen(tmp_path, "20261105", "20261104")
    (tmp_path / "screen_20261106.json").write_text("{broken", encoding="utf-8")
    assert latest_cached(str(tmp_path)).trade_date == "20261105"


# ------------------------------------------------------------------ 차트
def _bars(n):
    return [{"dt": f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}", "open": 100 + i, "high": 110 + i, "low": 95 + i,
             "close": 105 + i, "volume": 1000 * (i + 1)} for i in range(n)]


def test_chart_data_average_matches_screener_definition():
    pytest.importorskip("PySide6.QtCharts")
    from python_app.chart_dialog import chart_data
    bars = _bars(90)
    data = chart_data(bars)
    assert len(data.bars) == 60 and data.bars[-1] == bars[-1]
    assert data.avg_volume[-1] == sum(b["volume"] for b in bars[-21:-1]) / 20    # 당일 제외 직전 20일


def test_chart_data_short_history():
    pytest.importorskip("PySide6.QtCharts")
    from python_app.chart_dialog import chart_data
    data = chart_data(_bars(21))
    assert data.avg_volume[:20] == [None] * 20 and data.avg_volume[20] is not None


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from python_app import main as app_main

    app = QApplication.instance() or QApplication([])
    path = str(tmp_path / "config.json")
    save_config({"kiwoom": {"mock": {"app_key": "k", "secret_key": "s"}}}, path)
    _write_screen(tmp_path, "20261105", "20261104")
    monkeypatch.setattr(app_main, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(app_main, "load_config", lambda: load_config(path))
    monkeypatch.setattr(app_main, "save_config", lambda cfg: save_config(cfg, path))
    w = app_main.MainWindow()
    w.qt_app = app
    w.main_module = app_main
    yield w
    w.ui_timer.stop()
    if w.chart:
        w.chart.close()
    w.deleteLater()


def _wait_for_chart(window):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    window.chart.loaded.connect(lambda *_: QTimer.singleShot(0, loop.quit))
    QTimer.singleShot(3000, loop.quit)
    loop.exec()


class _Broker:
    def __init__(self, bars=None, error=None):
        self.bars, self.error, self.calls = bars, error, []

    def get_daily_bars(self, symbol, base_dt, pages=1):
        self.calls.append((symbol, base_dt, pages))
        if self.error:
            raise RuntimeError(self.error)
        return self.bars

    def get_chart_minute_bars(self, symbol, tic):
        self.calls.append((symbol, tic))
        return [{"dt": f"20261104{9 + i // 20:02d}{i % 20 * 3:02d}00", "open": 100, "high": 110, "low": 95,
                 "close": 105, "volume": 500} for i in range(150)]


def test_watch_table_lists_latest_scan_before_trading_starts(window):
    assert window.table_watch.rowCount() == 1
    assert window.table_watch.item(0, 0).text() == "005930"
    assert window.table_watch.item(0, 2).toolTip() == "거래량 3.0배"
    assert "기준일 20261104" in window.wl_box.title()


def test_double_click_opens_chart_with_judgement_day(window, monkeypatch):
    broker = _Broker(_bars(70))
    monkeypatch.setattr(window, "_temp_broker", lambda: broker)
    window.open_chart(0)
    _wait_for_chart(window)
    assert broker.calls[0][0] == "005930"
    assert window.chart.isVisible() and window.chart.lbl_title.text() == "삼성전자 (005930)"
    assert window.chart._judge_dt == "20261104"
    assert len(window.chart.view.chart().series()) == 3


def test_chart_button_opens_selected_row(window, monkeypatch):
    broker = _Broker(_bars(70))
    monkeypatch.setattr(window, "_temp_broker", lambda: broker)
    window.table_watch.selectRow(0)
    window.btn_chart.click()
    _wait_for_chart(window)
    assert broker.calls[0][0] == "005930" and window.chart.isVisible()


def test_chart_button_without_selection_asks_to_select(window, monkeypatch):
    shown = []
    monkeypatch.setattr(window.main_module.QMessageBox, "information", lambda *a: shown.append(a[2]))
    window.table_watch.setCurrentCell(-1, -1)
    window.btn_chart.click()
    assert "선택" in shown[0] and window.chart is None


def test_chart_failure_is_shown_not_raised(window, monkeypatch):
    monkeypatch.setattr(window, "_temp_broker", lambda: _Broker(error="접속 실패"))
    window.open_chart(0)
    _wait_for_chart(window)
    assert "일봉 조회 실패: 접속 실패" in window.chart.view.chart().title()


def _wk(dt, o, h, l, c, v):
    return {"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_aggregate_week_and_month():
    from python_app.chart_dialog import aggregate
    days = [_wk("20261029", 10, 12, 9, 11, 100), _wk("20261030", 11, 15, 10, 14, 200),   # 44주 (10월)
            _wk("20261102", 14, 16, 13, 15, 300), _wk("20261103", 15, 18, 8, 17, 400)]   # 45주 (11월)
    assert aggregate(days, "W") == aggregate(days, "M") == [
        {"dt": "20261030", "first_dt": "20261029", "open": 10, "close": 14, "high": 15, "low": 9, "volume": 300},
        {"dt": "20261103", "first_dt": "20261102", "open": 14, "close": 17, "high": 18, "low": 8, "volume": 700},
    ]


def test_week_spanning_two_months_splits_by_period():
    from python_app.chart_dialog import aggregate
    days = [_wk("20260930", 1, 2, 1, 2, 1), _wk("20261001", 2, 3, 2, 3, 1)]              # 같은 주, 다른 달
    assert len(aggregate(days, "W")) == 1 and len(aggregate(days, "M")) == 2


def test_year_boundary_week_is_one_bar():
    from python_app.chart_dialog import aggregate
    days = [_wk("20251231", 1, 2, 1, 2, 1), _wk("20260102", 2, 3, 2, 3, 1)]              # ISO 2026-W01
    assert len(aggregate(days, "W")) == 1


def test_judge_bar_marks_period_containing_the_day():
    from python_app.chart_dialog import is_judge_bar
    week = {"dt": "20261106", "first_dt": "20261102"}
    assert is_judge_bar(week, "20261104") and not is_judge_bar(week, "20261030")
    assert is_judge_bar({"dt": "20261104"}, "20261104")
    assert not is_judge_bar({"dt": "20261104090300"}, "20261104")
    assert not is_judge_bar({"dt": "20261103153000"}, "20261104")
    assert not is_judge_bar({"dt": "20261104"}, "")


@pytest.mark.parametrize("period, count", [("D", 60), ("W", 60), ("M", 60), ("3", 130), ("15", 130)])
def test_axis_labels_are_unique_and_keep_latest_bar(period, count):
    from python_app.chart_dialog import axis_labels, bar_label
    if period == "M":
        dts = [f"{y}{m:02d}28" for y in range(2020, 2027) for m in range(1, 13)][-count:]
    elif period in ("D", "W"):
        dts = [f"2026{m:02d}{d:02d}" for m in range(1, 13) for d in range(1, 11)][-count:]
    else:
        dts = [f"20261104{h:02d}{mi:02d}00" for h in range(9, 16) for mi in range(0, 60, 3)][-count:]
    bars = [{"dt": dt} for dt in dts]
    labels = axis_labels(bars, period)
    assert len(labels) == len(set(labels)) == len(bars) + 1
    assert labels[-2] == bar_label(dts[-1], period)


def test_weekly_labels_do_not_collide_across_years():
    from python_app.chart_dialog import bar_label
    assert bar_label("20251104", "W") != bar_label("20261104", "W")


def test_period_buttons_reload_with_right_source(window, monkeypatch):
    broker = _Broker(_bars(300))
    monkeypatch.setattr(window, "_temp_broker", lambda: broker)
    window.open_chart(0)
    _wait_for_chart(window)
    for period, call, title in (("W", ("005930", "20261105", 3), "주봉"), ("3", ("005930", 3), "3분봉"),
                                ("15", ("005930", 15), "15분봉"), ("M", ("005930", "20261105", 3), "월봉")):
        broker.calls.clear()
        window.chart.period_buttons[period].click()
        _wait_for_chart(window)
        assert broker.calls[0][0] == call[0] and broker.calls[0][-1] == call[-1], period
        assert window.chart.windowTitle().endswith(title)
        assert len(window.chart.view.chart().series()) == 3


def test_period_stays_selected_for_next_symbol(window, monkeypatch):
    broker = _Broker(_bars(70))
    monkeypatch.setattr(window, "_temp_broker", lambda: broker)
    window.open_chart(0)
    _wait_for_chart(window)
    window.chart.period_buttons["5"].click()
    _wait_for_chart(window)
    broker.calls.clear()
    window.open_chart(0)
    _wait_for_chart(window)
    assert broker.calls == [("005930", 5)]


class _Paged:
    """ka10081/ka10080 2페이지 응답을 흉내 낸 _post"""
    def __init__(self, key, field):
        self.key, self.field, self.sent = key, field, []

    def __call__(self, path, api, body, cont_key=""):
        self.sent.append(cont_key)
        page = [{self.field: "20261104" if self.key == "d" else "20261104090000", "open_pric": "100", "high_pric": "110",
                 "low_pric": "90", "cur_prc": "105", "trde_qty": "10"}] if not cont_key else \
               [{self.field: "20261103" if self.key == "d" else "20261103153000", "open_pric": "-100", "high_pric": "110",
                 "low_pric": "90", "cur_prc": "-105", "trde_qty": "-20"}]
        name = "stk_dt_pole_chart_qry" if self.key == "d" else "stk_min_pole_chart_qry"
        return {name: page}, ({"cont-yn": "Y", "next-key": "n1"} if not cont_key else {})


def _broker():
    from python_app.broker.kiwoom_rest import KiwoomRestBroker
    from python_app.models.types import TradingMode
    return KiwoomRestBroker("k", "s", mode=TradingMode.MOCK, config=StrategyConfig())


def test_daily_bars_one_page_by_default_and_more_on_request():
    b = _broker()
    b._post = _Paged("d", "dt")
    assert [x["dt"] for x in b.get_daily_bars("005930", "20261105")] == ["20261104"]
    b._post = _Paged("d", "dt")
    bars = b.get_daily_bars("005930", "20261105", pages=3)
    assert [x["dt"] for x in bars] == ["20261103", "20261104"]
    assert b._post.sent == ["", "n1"]
    assert (bars[0]["open"], bars[0]["close"], bars[0]["volume"]) == (100, 105, 20)


def test_chart_minute_bars_span_previous_day_sorted():
    b = _broker()
    b._post = _Paged("m", "cntr_tm")
    bars = b.get_chart_minute_bars("005930", 5)
    assert [x["dt"] for x in bars] == ["20261103153000", "20261104090000"]


def test_chart_minute_bars_stop_once_enough():
    b = _broker()
    b._post = _Paged("m", "cntr_tm")
    assert len(b.get_chart_minute_bars("005930", 5, want=1)) == 1 and b._post.sent == [""]


def test_no_keys_does_not_open_chart(window, monkeypatch):
    monkeypatch.setattr(window, "_temp_broker", lambda: None)
    window.open_chart(0)
    assert window.chart is None


def test_stale_response_is_ignored(window):
    from python_app.chart_dialog import ChartDialog
    window.chart = ChartDialog(window)
    window.chart._request = 5
    window.chart._on_loaded(4, _bars(30), "")
    assert window.chart.view.chart() is None or not window.chart.view.chart().series()


def test_strategy_box_has_no_trailing_inputs(window):
    for name in ("spn_dsh_trail", "spn_dsh_act", "chk_smc_trail"):
        assert not hasattr(window, name)


def test_1r_rate_is_saved_from_settings_tab(window):
    window.spn_trail_1r.setValue(25.0)
    assert window.config.trailing_retrace_rate_1r == 25.0
    assert window.cfg["trailing_retrace_rate_1r"] == 25.0


# ---------- 보유 종목 가격선

def test_start_index_finds_entry_bar():
    from python_app.chart_dialog import start_index
    days = [{"dt": "20261103"}, {"dt": "20261104"}, {"dt": "20261105"}]
    assert start_index(days, "20261104093000") == 1
    assert start_index(days, "20260101093000") == 0      # 화면보다 오래된 매수 → 첫 봉
    assert start_index(days, "20261106093000") == 2      # 아직 봉이 없으면 마지막 봉
    assert start_index(days, "") == 0
    week = [{"dt": "20261030", "first_dt": "20261026"}, {"dt": "20261106", "first_dt": "20261102"}]
    assert start_index(week, "20261103100000") == 1
    minutes = [{"dt": "20261104090000"}, {"dt": "20261104090300"}]
    assert start_index(minutes, "20261104090130") == 1


def test_label_heights_keep_gap_only_when_crowded():
    from python_app.chart_dialog import label_heights
    assert label_heights([100, 101, 200], 5) == [100, 105, 200]


def test_axis_labels_pad_stays_unique():
    from python_app.chart_dialog import axis_labels
    labels = axis_labels([{"dt": f"202611{d:02d}"} for d in range(1, 11)], "D", pad=6)
    assert len(labels) == len(set(labels)) == 16


class _Engine:
    def __init__(self, position, managed=None):
        self.broker = _Broker(_bars(70))
        self.snap = {"running": True, "positions": {"005930": position},
                     "managed": {"005930": managed} if managed else {}}

    def snapshot(self):
        return self.snap


def _position():
    return {"avg_price": 100.0, "highest_price": 100.0, "current_price": 100.0}


def _managed(**kw):
    return {"status": "HOLDING", "entry_price": 100.0, "highest_price": 100.0, "stop_price": 0.0, "target_price": 0.0,
            "risk_stop": 0.0, "use_trailing": True, "buy_filled_at": "2026-11-04T09:30:00+09:00", "order_time": "", **kw}


def _level_map(window):
    return {lv.label: lv.price for lv in window._price_levels("005930")}


def test_levels_use_strategy_target(window):
    window.engine = _Engine(_position(), _managed(stop_price=95.0, target_price=120.0))
    levels = window._price_levels("005930")
    assert {lv.label: lv.price for lv in levels} == {"매수": 100.0, "손절": 95.0, "익절": 120.0}
    assert {lv.since for lv in levels} == {"20261104093000"}


def test_levels_show_trailing_as_profit_line_once_active(window):
    window.config.stop_loss_rate = 2.0
    window.config.take_profit_rate = 0.0
    window.config.trailing_activation_rate = 5.0
    window.config.trailing_retrace_rate = 50.0
    window.config.trailing_retrace_rate_1r = 0.0
    window.engine = _Engine(_position(), _managed(highest_price=102.0))
    assert "익절(트레일링)" not in _level_map(window) and len(_level_map(window)) == 2     # 발동 전
    window.engine = _Engine(_position(), _managed(highest_price=110.0))
    assert _level_map(window)["익절(트레일링)"] == 105.0


def test_unmanaged_holding_shows_buy_line_only(window):
    window.engine = _Engine(_position())
    assert _level_map(window) == {"매수": 100.0}


def test_not_held_symbol_has_no_levels(window):
    assert window._price_levels("005930") == []          # 엔진 없음
    window.engine = _Engine(_position())
    assert window._price_levels("000660") == []


def test_holding_table_opens_chart_with_right_extended_lines(window):
    from python_app.chart_dialog import right_pad
    window.engine = _Engine(_position(), _managed(stop_price=95.0, target_price=120.0))
    window.engine.broker = broker = _Broker(_bars(70))
    window._temp_broker = lambda: broker
    window.table_pos.setRowCount(1)
    window.table_pos.setItem(0, 0, window.main_module._item("삼성전자(005930)"))
    window.open_holding_chart(0)
    _wait_for_chart(window)
    series = window.chart.view.chart().series()
    assert len(series) == 3 + 3 + 3                      # 기본 3 + 선 3 + 라벨 3
    right = 60 + right_pad(60) - 0.5
    lines = [s for s in series[3:6]]
    assert sorted(s.at(0).y() for s in lines) == [95.0, 100.0, 120.0]
    assert all(s.at(1).x() == right and s.at(0).y() == s.at(1).y() for s in lines)


# ---------- 손절선 드래그

def _held_engine(tmp_path, **managed):
    tmp_path.mkdir(exist_ok=True)
    from python_app.engine.live_engine import LiveTradingEngine, ManagedTrade
    from python_app.models.types import Position
    from python_app.tests.fake_kiwoom import Clock, FakeKiwoom
    from python_app.tests.test_kiwoom_live import make_broker
    config = StrategyConfig()
    eng = LiveTradingEngine(make_broker(FakeKiwoom(), config=config), config, [("005930", "삼성전자")],
                            state_dir=str(tmp_path), clock=Clock(datetime(2026, 10, 20, 10, 0)))
    eng.running = True
    managed.setdefault("use_trailing", False)
    eng.managed["005930"] = ManagedTrade(symbol="005930", name="삼성전자", order_id="1", status="HOLDING",
                                         entry_price=10000.0, qty=10, **managed)
    eng.positions["005930"] = Position(symbol="005930", name="삼성전자", qty=10, avg_price=10000.0,
                                       current_price=10100.0, highest_price=10100.0, stop_loss_price=9800.0,
                                       take_profit_price=0.0, entry_time=datetime(2026, 10, 20, 9, 30))
    eng.last_quotes["005930"] = 10100.0
    return eng


def test_manual_stop_is_saved_and_survives_restart_file(tmp_path):
    eng = _held_engine(tmp_path)
    assert eng.set_manual_stop("005930", 9900.0) == ""
    m = eng.managed["005930"]
    assert m.stop_price == 9900.0 and m.stop_manual
    with open(eng.state_path, encoding="utf-8") as f:
        saved = json.load(f)["managed"]["005930"]
    assert saved["stop_price"] == 9900.0 and saved["stop_manual"] is True


@pytest.mark.parametrize("price", [10100.0, 10500.0, 0.0])
def test_manual_stop_at_or_above_current_price_is_refused(tmp_path, price):
    eng = _held_engine(tmp_path)
    assert eng.set_manual_stop("005930", price)
    assert eng.managed["005930"].stop_price == 0.0 and not eng.managed["005930"].stop_manual


def test_manual_stop_refused_when_not_holding_or_stopped(tmp_path):
    eng = _held_engine(tmp_path)
    eng.managed["005930"].status = "SELLING"
    assert eng.set_manual_stop("005930", 9900.0)
    eng.managed["005930"].status = "HOLDING"
    eng.running = False
    assert eng.set_manual_stop("005930", 9900.0)
    assert eng.managed["005930"].stop_price == 0.0


def test_added_fill_keeps_manual_stop(tmp_path):
    eng = _held_engine(tmp_path, stop_rate=2.0)
    eng.set_manual_stop("005930", 9900.0)
    m = eng.managed["005930"]
    m.entry_price = 10050.0
    eng._apply_stop_rate(m)
    assert m.stop_price == 9900.0


def _drag_setup(window, monkeypatch, tmp_path, answer):
    from PySide6.QtWidgets import QMessageBox
    from python_app import chart_dialog
    eng = _held_engine(tmp_path, stop_price=9800.0, target_price=10500.0,
                       buy_filled_at="2026-10-20T09:30:00+09:00")
    broker = _Broker([{**b, "open": 10000 + i, "high": 10200 + i, "low": 9700 + i, "close": 10050 + i}
                      for i, b in enumerate(_bars(70))])
    window.engine = eng
    window._temp_broker = lambda: broker
    asked = []
    monkeypatch.setattr(chart_dialog.QMessageBox, "question", lambda *a: asked.append(a[2]) or answer)
    warned = []
    monkeypatch.setattr(chart_dialog.QMessageBox, "warning", lambda *a: warned.append(a[2]))
    window._show_chart("005930", "삼성전자", "", "")
    _wait_for_chart(window)
    window.chart.resize(1100, 680)
    window.qt_app.processEvents()
    return eng, asked, warned, QMessageBox


def _drag(view, line, to_price):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtTest import QTest
    chart = view.chart()

    def point(y):
        x = chart.plotArea().right() - 20
        return QPoint(int(x), int(chart.mapToPosition(QPointF(0, y), line).y()))

    QTest.mousePress(view.viewport(), Qt.LeftButton, pos=point(line.at(0).y()))
    QTest.mouseMove(view.viewport(), point(to_price))
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=point(to_price))


def _line(window, label):
    return next(line for level, line, _ in window.chart._movable if level.label == label)


def test_dragging_stop_line_moves_stop_after_confirm(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    eng, asked, warned, _ = _drag_setup(window, monkeypatch, tmp_path, QMessageBox.Yes)
    _drag(window.chart.view, _line(window, "손절"), 9900)
    m = eng.managed["005930"]
    assert asked and not warned and m.stop_manual
    assert 9880 <= m.stop_price <= 9920 and m.stop_price % 10 == 0     # 1만원대 호가 10원
    assert _line(window, "손절").at(0).y() == m.stop_price              # 다시 그린 선도 새 값


def test_cancelled_drag_restores_line(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    eng, asked, _, _ = _drag_setup(window, monkeypatch, tmp_path, QMessageBox.No)
    _drag(window.chart.view, _line(window, "손절"), 9900)
    assert asked and eng.managed["005930"].stop_price == 9800.0
    assert _line(window, "손절").at(0).y() == 9800.0


def test_drag_above_current_price_shows_reason(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    eng, _, warned, _ = _drag_setup(window, monkeypatch, tmp_path, QMessageBox.Yes)
    _drag(window.chart.view, _line(window, "손절"), 10150)
    assert "현재가" in warned[0] and eng.managed["005930"].stop_price == 9800.0


def _movable_labels(window):
    return {lv.label for lv in window._price_levels("005930") if lv.move}


def test_unmanaged_or_stopped_holding_has_no_movable_line(window):
    window.engine = _Engine(_position())
    assert not _movable_labels(window)
    window.engine = _Engine(_position(), _managed(use_trailing=False, target_price=120.0))
    window.engine.snap["running"] = False
    assert not _movable_labels(window)


def test_only_no_trailing_holding_has_movable_stop_and_target(window):
    window.config.trailing_retrace_rate = 50.0
    window.engine = _Engine(_position(), _managed(use_trailing=True, target_price=120.0))
    assert not _movable_labels(window)
    window.engine = _Engine(_position(), _managed(use_trailing=False, target_price=120.0))
    assert _movable_labels(window) == {"손절", "익절"}


def test_global_trailing_off_counts_as_no_trailing(window):
    window.config.trailing_retrace_rate = 0.0
    window.engine = _Engine(_position(), _managed(use_trailing=True, target_price=120.0))
    assert _movable_labels(window) == {"손절", "익절"}


def test_engine_refuses_moves_on_trailing_holding(tmp_path):
    eng = _held_engine(tmp_path, stop_price=9800.0, target_price=10600.0, use_trailing=True)
    assert "트레일링" in eng.set_manual_stop("005930", 9900.0)
    assert "트레일링" in eng.set_manual_target("005930", 10500.0)
    m = eng.managed["005930"]
    assert (m.stop_price, m.target_price, m.stop_manual) == (9800.0, 10600.0, False)


def test_manual_target_is_saved(tmp_path):
    eng = _held_engine(tmp_path, stop_price=9800.0, target_price=10600.0)
    assert eng.set_manual_target("005930", 10800.0) == ""
    with open(eng.state_path, encoding="utf-8") as f:
        assert json.load(f)["managed"]["005930"]["target_price"] == 10800.0


@pytest.mark.parametrize("price", [10100.0, 10000.0])
def test_manual_target_at_or_below_current_price_is_refused(tmp_path, price):
    eng = _held_engine(tmp_path, target_price=10600.0)
    assert "현재가" in eng.set_manual_target("005930", price)
    assert eng.managed["005930"].target_price == 10600.0


def test_dragging_target_line_moves_target_after_confirm(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    eng, asked, warned, _ = _drag_setup(window, monkeypatch, tmp_path, QMessageBox.Yes)
    _drag(window.chart.view, _line(window, "익절"), 10400)
    m = eng.managed["005930"]
    assert asked and "익절가" in asked[0] and not warned
    assert 10380 <= m.target_price <= 10420 and m.stop_price == 9800.0
    assert _line(window, "익절").at(0).y() == m.target_price


def test_drag_target_below_current_price_shows_reason(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    eng, _, warned, _ = _drag_setup(window, monkeypatch, tmp_path, QMessageBox.Yes)
    _drag(window.chart.view, _line(window, "익절"), 10050)
    assert "현재가" in warned[0] and eng.managed["005930"].target_price == 10500.0


# ---------- 손절선을 옮겨도 1R 은 처음 세팅 그대로

def _trail_position(stop, peak):
    return Position(symbol="005930", name="삼성전자", qty=10, avg_price=10000.0, current_price=peak,
                    stop_loss_price=stop, take_profit_price=0.0, entry_time=datetime(2026, 10, 20, 9, 30),
                    highest_price=peak)


def test_moved_stop_does_not_change_1r_zone():
    """처음 손절 9,800(1R=200), 최고 10,300 → 1R 이상. 손절을 9,500 으로 내려도(그대로면 1R=500 → 미만) 1R 이상 유지"""
    config = StrategyConfig(trailing_activation_rate=1.0, trailing_retrace_rate=50.0, trailing_retrace_rate_1r=30.0)
    pos = _trail_position(9500.0, 10300.0)
    decision = check_exit(pos, 10200.0, config, risk_stop=9800.0)     # 30% 되돌림 = 10,210
    assert decision and decision.trigger_price == 10210.0 and "1R 이상" in decision.reason
    assert check_exit(_trail_position(9500.0, 10300.0), 10200.0, config) is None     # 옮긴 값 기준이면 달라짐


def test_stop_above_entry_keeps_1r_zone():
    config = StrategyConfig(trailing_activation_rate=1.0, trailing_retrace_rate=50.0, trailing_retrace_rate_1r=30.0)
    decision = check_exit(_trail_position(10050.0, 10300.0), 10200.0, config, risk_stop=9800.0)
    assert decision and "1R 이상" in decision.reason


def test_first_stop_is_kept_as_1r_basis_across_moves(tmp_path):
    eng = _held_engine(tmp_path, stop_price=9700.0)
    eng.set_manual_stop("005930", 9900.0)
    eng.set_manual_stop("005930", 9950.0)
    m = eng.managed["005930"]
    assert m.stop_price == 9950.0 and m.risk_stop == 9700.0


def test_rate_based_stop_keeps_basis_and_follows_added_fill(tmp_path):
    eng = _held_engine(tmp_path)                     # 시장가 전략: stop_price 0, 매입가 -2%
    eng.set_manual_stop("005930", 9900.0)
    m = eng.managed["005930"]
    assert m.risk_stop == 9800.0
    m.entry_price = 10100.0
    eng._apply_stop_rate(m)
    assert m.stop_price == 9900.0 and m.risk_stop == round(10100 * 0.98)


def test_v4_config_drops_per_strategy_exit_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 4, "stop_loss_rate": 1.5, "dsh_stop_rate": 0.5, "smc_rr": 3.0,
                                "rule_presets": {"ST1": {"dsh_stop_rate": 2.0, "smc_rr": 4.0, "stop_loss_rate": 2.0}}}),
                    encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg["config_version"] == 5 and cfg["stop_loss_rate"] == 1.5
    assert not {"dsh_stop_rate", "smc_rr"} & set(cfg)
    assert cfg["rule_presets"]["ST1"] == {"stop_loss_rate": 2.0}
    strategy_config_from(cfg)


def test_moving_stop_keeps_target(tmp_path):
    eng = _held_engine(tmp_path, stop_price=9800.0, target_price=10600.0)
    eng.set_manual_stop("005930", 9900.0)
    assert eng.managed["005930"].target_price == 10600.0


def test_table_trailing_uses_first_stop(window):
    window.config.trailing_activation_rate = 1.0
    window.config.trailing_retrace_rate = 50.0
    window.config.trailing_retrace_rate_1r = 30.0
    m = _managed(entry_price=10000.0, highest_price=10300.0, stop_price=9500.0, risk_stop=9800.0)
    assert window._holding_prices({"avg_price": 10000.0, "highest_price": 10300.0}, m)[4] == 10210.0


class _Quote:
    def __init__(self, price):
        self.current_price = price


def _exit_reason(eng, price):
    """엔진의 실제 감시 경로(_check_exits)를 돌려 매도 사유를 돌려준다. 매도 없으면 None"""
    sold = []
    eng.broker.get_quote = lambda sym: _Quote(price)
    eng._sell = lambda m, pos, px, reason, now: sold.append(reason) or True
    eng._check_exits(eng.clock())
    return sold[0] if sold else None


@pytest.mark.parametrize("moved_to", [9500.0, 9900.0, 10050.0])
@pytest.mark.parametrize("peak, price, sells", [
    (10300.0, 10200.0, True),       # 1R 이상 구간 되돌림 → 매도
    (10300.0, 10250.0, False),      # 1R 이상 구간, 아직 위
    (10150.0, 10070.0, True),       # 1R 미만 구간 되돌림 → 매도
    (10150.0, 10100.0, False),      # 1R 미만 구간, 아직 위
])
def test_moving_stop_leaves_trailing_exit_unchanged(tmp_path, moved_to, peak, price, sells):
    original = _held_engine(tmp_path / "a", stop_price=9800.0, highest_price=peak, use_trailing=True)
    moved = _held_engine(tmp_path / "b", stop_price=9800.0, highest_price=peak, use_trailing=True)
    retrace = moved.config.trailing_retrace_rate
    moved.config.trailing_retrace_rate = 0.0             # 트레일링을 꺼 둔 사이 옮긴 뒤 다시 켠 경우
    assert moved.set_manual_stop("005930", moved_to) == ""
    moved.config.trailing_retrace_rate = retrace
    reason = _exit_reason(original, price)
    assert _exit_reason(moved, price) == reason
    assert bool(reason) == sells and (not sells or "트레일링" in reason)


def test_moved_stop_still_sells_at_the_new_stop(tmp_path):
    eng = _held_engine(tmp_path, stop_price=9800.0, highest_price=10010.0)
    eng.set_manual_stop("005930", 9950.0)
    assert "수동 손절가" in _exit_reason(eng, 9950.0)


def test_smc_holding_follows_common_trailing_and_keeps_target(tmp_path):
    """F 도 공통 트레일링을 따른다: 되돌림이면 트레일링 매도, 목표가에 먼저 닿으면 목표가 매도"""
    eng = _held_engine(tmp_path / "a", stop_price=9800.0, target_price=10600.0, highest_price=10300.0, use_trailing=True)
    assert "트레일링" in _exit_reason(eng, 10200.0)
    eng = _held_engine(tmp_path / "b", stop_price=9800.0, target_price=10600.0, highest_price=10590.0, use_trailing=True)
    assert "목표가" in _exit_reason(eng, 10600.0)


# ---------- 실거래 시작 버튼

def _real_start_setup(window, monkeypatch, answer):
    from unittest.mock import MagicMock
    from python_app.models.types import TradingMode
    module = window.main_module
    window.mode = TradingMode.REAL
    for ed in window.key_inputs[TradingMode.REAL]:
        ed.setText("k")
    asked = []
    monkeypatch.setattr(module.QMessageBox, "warning", lambda *a, **k: asked.append(a[1]) or answer)
    worker = MagicMock()
    for name, stub in (("KiwoomRestBroker", MagicMock()), ("LiveTradingEngine", MagicMock()), ("EngineWorker", worker)):
        monkeypatch.setattr(module, name, stub)
    monkeypatch.setattr(window, "_make_screener", lambda broker: None)
    return asked, worker


def test_start_button_asks_and_passes_live_confirmation(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    asked, worker = _real_start_setup(window, monkeypatch, QMessageBox.Yes)
    window.btn_start.setEnabled(True)
    window.btn_start.click()
    assert asked == ["실거래 자동매매 시작"]
    assert worker.call_args[0][1] is True
    window.engine = window.worker = None


def test_start_button_declined_does_not_start(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    asked, worker = _real_start_setup(window, monkeypatch, QMessageBox.No)
    window.btn_start.setEnabled(True)
    window.btn_start.click()
    assert asked and not worker.called


def test_start_button_never_uses_autostart_consent(window, monkeypatch):
    """사전 동의를 켜 둬도 버튼으로 시작할 때는 확인창을 거친다"""
    from PySide6.QtWidgets import QMessageBox
    window.cfg["autostart"]["allow_real_autostart"] = True
    asked, worker = _real_start_setup(window, monkeypatch, QMessageBox.No)
    window.btn_start.setEnabled(True)
    window.btn_start.click()
    assert asked and not worker.called
