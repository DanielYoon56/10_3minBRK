"""저장된 매매 규칙(프리셋): 저장·조회 로직 + 전략 설정 탭 동작 (Qt offscreen)"""
import json
import os

import pytest

from python_app.app_config import (
    copy_strategy_fields, load_config, matching_rule_preset, rule_preset_config,
    save_config, save_rule_preset, strategy_config_from,
)
from python_app.models.types import StrategyConfig, TradingMode


def _cfg(**fields):
    return {"rule_presets": {}, **fields}


def test_load_config_keeps_old_files_working(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"stop_loss_rate": 3.0}), encoding="utf-8")
    assert load_config(str(path))["rule_presets"] == {}


def test_preset_survives_save_and_load(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = _cfg()
    save_rule_preset(cfg, "공격형", StrategyConfig(stop_loss_rate=3.5, max_daily_loss=123_000))
    save_config(cfg, path)
    loaded = rule_preset_config(load_config(path), "공격형")
    assert loaded.stop_loss_rate == 3.5
    assert loaded.max_daily_loss == 123_000


def test_preset_is_a_snapshot_not_a_live_reference():
    cfg, sc = _cfg(), StrategyConfig()
    save_rule_preset(cfg, "a", sc)
    sc.stop_loss_rate = 9.0
    assert cfg["rule_presets"]["a"]["stop_loss_rate"] == 2.0


def test_field_missing_from_old_preset_keeps_current_value():
    cfg = _cfg(take_profit_rate=0.7)
    cfg["rule_presets"]["옛날"] = {"stop_loss_rate": 1.5}
    target = rule_preset_config(cfg, "옛날")
    assert (target.stop_loss_rate, target.take_profit_rate) == (1.5, 0.7)


def test_v6_config_drops_discarded_strategies_and_accumulation_scan(tmp_path):
    """3WAYTRADING 전환: 기존 매수 전략(A~F)·매집 스캔 설정은 지우고 공통 설정은 그대로 둔다"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "config_version": 6, "stop_loss_rate": 1.5, "entry_strategies": ["open_breakout", "smc_fvg"],
        "entry_logic": "AND", "trade_end_time": "10:00", "min_volume_ratio": 0.7, "smc_swing_len": 3,
        "screener": {"enabled": True, "condition_name": "조건"},
        "rule_presets": {"ST1": {"stop_loss_rate": 2.5, "entry_strategies": ["orb_high"], "vwap_touch_pct": 0.3}},
    }), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg["config_version"] == 7 and cfg["stop_loss_rate"] == 1.5
    assert not {"entry_strategies", "entry_logic", "trade_end_time", "min_volume_ratio", "smc_swing_len",
                "screener"} & set(cfg)
    assert cfg["rule_presets"]["ST1"] == {"stop_loss_rate": 2.5}
    assert cfg["scanner"]["enabled"] is True
    assert cfg["strategies"]["open"]["enabled"] is True and cfg["strategies"]["open"]["live_enabled"] is False
    assert strategy_config_from(cfg).stop_loss_rate == 1.5


def test_matching_preset():
    cfg = _cfg()
    save_rule_preset(cfg, "a", StrategyConfig(stop_loss_rate=1.0))
    save_rule_preset(cfg, "b", StrategyConfig(stop_loss_rate=2.5))
    assert matching_rule_preset(cfg, StrategyConfig(stop_loss_rate=2.5)) == "b"
    assert matching_rule_preset(cfg, StrategyConfig(stop_loss_rate=7.0)) is None


def test_copy_strategy_fields_overwrites_target():
    live = StrategyConfig()
    copy_strategy_fields(StrategyConfig(max_daily_loss=1, take_profit_rate=2.5), live)
    assert (live.max_daily_loss, live.take_profit_rate) == (1, 2.5)


# ------------------------------------------------------------------ GUI
@pytest.fixture
def window(tmp_path, monkeypatch):
    """실제 config.json·DB 를 건드리지 않도록 임시 폴더로 돌린 MainWindow"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from python_app import main as app_main

    app = QApplication.instance() or QApplication([])
    path = str(tmp_path / "config.json")
    save_config({"stop_loss_rate": 2.0}, path)
    monkeypatch.setattr(app_main, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(app_main, "load_config", lambda: load_config(path))
    monkeypatch.setattr(app_main, "save_config", lambda cfg: save_config(cfg, path))
    w = app_main.MainWindow()
    w.saved_path = path
    w.main_module = app_main
    w.qt_app = app
    yield w
    w.ui_timer.stop()
    w.deleteLater()


def _answer(window, monkeypatch, button):
    """확인창을 띄우지 않고 button 으로 답한다. 표시된 본문을 목록으로 돌려준다"""
    calls = []
    box = window.main_module.QMessageBox

    def fake(*args, **kw):
        calls.append(args[2])
        return button

    monkeypatch.setattr(box, "warning", fake)
    monkeypatch.setattr(box, "question", fake)
    return calls


def _save_as(window, monkeypatch, name):
    monkeypatch.setattr(window.main_module.QInputDialog, "getText", lambda *a, **k: (name, True))
    window.save_preset()


def _yes(window):
    return window.main_module.QMessageBox.Yes


def _no(window):
    return window.main_module.QMessageBox.No


def test_apply_restores_every_field_in_place(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    live = window.config
    window.spn_sl.setValue(3.3)
    window.chk_eod.setChecked(False)
    live.take_profit_rate = 2.5              # 화면에 없는 세부값
    _save_as(window, monkeypatch, "공격형")
    assert window.lbl_preset.text() == "현재 값 = '공격형'"

    window.spn_sl.setValue(1.1)
    window.chk_eod.setChecked(True)
    live.take_profit_rate = 0.0
    window.on_settings_changed()
    assert window.lbl_preset.text() == "현재 값: 저장 안 됨"

    window.apply_preset()
    assert window.config is live
    assert live.stop_loss_rate == 3.3 and window.spn_sl.value() == 3.3
    assert live.eod_liquidation is False and not window.chk_eod.isChecked()
    assert live.take_profit_rate == 2.5
    assert window.lbl_preset.text() == "현재 값 = '공격형'"
    assert strategy_config_from(load_config(window.saved_path)) == live


def test_selecting_in_combo_alone_changes_nothing(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_sl.setValue(3.3)
    _save_as(window, monkeypatch, "a")
    window.spn_sl.setValue(1.1)
    _save_as(window, monkeypatch, "b")
    window.cmb_preset.setCurrentText("a")
    assert window.config.stop_loss_rate == 1.1


def test_unsaved_values_need_confirmation(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_sl.setValue(3.3)
    _save_as(window, monkeypatch, "a")
    window.spn_sl.setValue(1.1)
    calls = _answer(window, monkeypatch, _no(window))
    window.apply_preset()
    assert "사라집니다" in calls[0]
    assert window.config.stop_loss_rate == 1.1


def test_saved_preset_applies_without_prompt(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_sl.setValue(3.3)
    _save_as(window, monkeypatch, "a")
    window.spn_sl.setValue(1.1)
    _save_as(window, monkeypatch, "b")
    window.cmb_preset.setCurrentText("a")
    calls = _answer(window, monkeypatch, _no(window))
    window.apply_preset()
    assert calls == [] and window.config.stop_loss_rate == 3.3


class _Running:
    def isRunning(self):
        return True

    def stop(self):
        pass


def test_real_running_confirmation_shows_target_rules(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_amount.setValue(5_000_000)
    _save_as(window, monkeypatch, "큰금액")
    window.spn_amount.setValue(1_000_000)
    _save_as(window, monkeypatch, "기본")
    window.cmb_preset.setCurrentText("큰금액")
    window.worker, window.mode = _Running(), TradingMode.REAL
    calls = _answer(window, monkeypatch, _no(window))
    window.apply_preset()
    window.worker, window.mode = None, TradingMode.MOCK
    assert "5,000,000원" in calls[0]
    assert window.config.order_amount_per_trade == 1_000_000


def test_overwrite_declined_and_delete(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_sl.setValue(3.3)
    _save_as(window, monkeypatch, "a")
    window.spn_sl.setValue(1.1)
    _answer(window, monkeypatch, _no(window))
    _save_as(window, monkeypatch, "a")
    assert window.cfg["rule_presets"]["a"]["stop_loss_rate"] == 3.3

    _answer(window, monkeypatch, _yes(window))
    window.delete_preset()
    assert load_config(window.saved_path)["rule_presets"] == {}
    assert not window.btn_preset_apply.isEnabled()


def test_scanner_settings_are_saved_from_settings_tab(window):
    window.scan_max.setValue(25)
    window.scan_value.setValue(80)          # 억
    saved = load_config(window.saved_path)["scanner"]
    assert saved["max_candidates"] == 25 and saved["min_trade_value"] == 8_000_000_000


def test_rule_summary_lists_strategies_and_flags_unapproved_ones_in_real_mode(window):
    assert "OPEN 09:06~09:45" in window._rule_summary(window.config)
    window.mode = TradingMode.REAL
    summary = window._rule_summary(window.config)
    window.mode = TradingMode.MOCK
    assert "활성 전략 없음" in summary and "실거래 미승인" in summary


def test_strategy_switches_are_saved_and_pushed_to_running_engine(window):
    from python_app.strategies.strategy_manager import build_manager
    chk, te_start, te_end, patterns = window.strategy_inputs["open"]
    manager = build_manager(window.cfg)
    window.engine = type("Engine", (), {"manager": manager, "scanner": None})()
    patterns["momentum"].setChecked(False)
    te_end.setTime(window.main_module.QTime(10, 30))
    chk.setChecked(False)
    window.engine = None
    saved = load_config(window.saved_path)["strategies"]["open"]
    assert (saved["enabled"], saved["end_time"], saved["use_momentum"], saved["use_orb"]) == (False, "10:30", False, True)
    live = manager.strategy("open")
    assert not live.enabled and live.settings.end_time == "10:30" and not live.settings.use_momentum
