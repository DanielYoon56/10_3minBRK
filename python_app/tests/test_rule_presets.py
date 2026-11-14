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
    save_rule_preset(cfg, "공격형", StrategyConfig(stop_loss_rate=3.5, entry_strategies=["orb_high", "smc_fvg"]))
    save_config(cfg, path)
    loaded = rule_preset_config(load_config(path), "공격형")
    assert loaded.stop_loss_rate == 3.5
    assert loaded.entry_strategies == ["orb_high", "smc_fvg"]


def test_preset_is_a_snapshot_not_a_live_reference():
    cfg, sc = _cfg(), StrategyConfig()
    save_rule_preset(cfg, "a", sc)
    sc.entry_strategies.append("orb_high")
    sc.stop_loss_rate = 9.0
    assert cfg["rule_presets"]["a"]["entry_strategies"] == ["open_breakout"]
    assert cfg["rule_presets"]["a"]["stop_loss_rate"] == 2.0


def test_field_missing_from_old_preset_keeps_current_value():
    cfg = _cfg(smc_min_risk_pct=0.7)
    cfg["rule_presets"]["옛날"] = {"stop_loss_rate": 1.5}
    target = rule_preset_config(cfg, "옛날")
    assert (target.stop_loss_rate, target.smc_min_risk_pct) == (1.5, 0.7)


def test_matching_preset():
    cfg = _cfg()
    save_rule_preset(cfg, "a", StrategyConfig(stop_loss_rate=1.0))
    save_rule_preset(cfg, "b", StrategyConfig(stop_loss_rate=2.5))
    assert matching_rule_preset(cfg, StrategyConfig(stop_loss_rate=2.5)) == "b"
    assert matching_rule_preset(cfg, StrategyConfig(stop_loss_rate=7.0)) is None


def test_copy_strategy_fields_overwrites_target():
    live = StrategyConfig()
    copy_strategy_fields(StrategyConfig(max_daily_loss=1, smc_fvg_expiry_bars=25), live)
    assert (live.max_daily_loss, live.smc_fvg_expiry_bars) == (1, 25)


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
    save_config({"stop_loss_rate": 2.0, "smc_fvg_expiry_bars": 10}, path)
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
    window.chk_strats["smc_fvg"].setChecked(True)
    window.cmb_logic.setCurrentIndex(1)
    live.smc_fvg_expiry_bars = 25            # 화면에 없는 세부값
    _save_as(window, monkeypatch, "공격형")
    assert window.lbl_preset.text() == "현재 값 = '공격형'"

    window.spn_sl.setValue(1.1)
    window.chk_strats["smc_fvg"].setChecked(False)
    window.cmb_logic.setCurrentIndex(0)
    live.smc_fvg_expiry_bars = 10
    window.on_settings_changed()
    assert window.lbl_preset.text() == "현재 값: 저장 안 됨"

    window.apply_preset()
    assert window.config is live
    assert live.stop_loss_rate == 3.3 and window.spn_sl.value() == 3.3
    assert live.entry_strategies == ["open_breakout", "smc_fvg"] and live.entry_logic == "AND"
    assert live.smc_fvg_expiry_bars == 25
    assert window.lbl_preset.text() == "현재 값 = '공격형'"
    assert strategy_config_from(load_config(window.saved_path)) == live


def test_applying_pushes_one_complete_config_to_running_engine(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.spn_sl.setValue(3.3)
    window.spn_amount.setValue(2_000_000)
    _save_as(window, monkeypatch, "a")
    window.spn_sl.setValue(1.1)
    window.spn_amount.setValue(500_000)
    _save_as(window, monkeypatch, "b")

    seen = []

    class Strategy:
        def update_config(self, c):
            seen.append((c.stop_loss_rate, c.order_amount_per_trade))

    window.engine = type("Engine", (), {"strategy": Strategy()})()
    window.cmb_preset.setCurrentText("a")
    window.apply_preset()
    window.engine = None
    assert seen == [(3.3, 2_000_000)]


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


def test_running_warns_when_preset_turns_on_a_new_strategy(window, monkeypatch):
    _answer(window, monkeypatch, _yes(window))
    window.chk_strats["orb_high"].setChecked(True)
    _save_as(window, monkeypatch, "B포함")
    window.chk_strats["orb_high"].setChecked(False)
    _save_as(window, monkeypatch, "A만")
    window.cmb_preset.setCurrentText("B포함")
    window.worker = _Running()
    calls = _answer(window, monkeypatch, _no(window))
    window.apply_preset()
    window.worker = None
    assert "새로 켜는 매수 전략" in calls[0]


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


def test_condition_list_load_gives_visible_feedback(window, monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def list_conditions(self):
            return [("0", "기존조건"), ("1", "새조건")]

    monkeypatch.setattr(window.main_module, "KiwoomConditionClient", FakeClient)
    monkeypatch.setattr(window, "_temp_broker", lambda: object())
    window.cmb_cond.setCurrentText("기존조건")
    window.show()
    window.load_condition_list()
    popup_shown = window.cmb_cond.view().isVisible()
    window.cmb_cond.hidePopup()

    assert popup_shown
    assert window.cmb_cond.currentText() == "기존조건"
    assert window.cmb_cond.count() == 2
    assert "2개" in window.statusBar.currentMessage()


def test_scan_progress_shows_count_and_time_left(window):
    window._scan_started = window.main_module.time.monotonic() - 100
    window.on_scan_progress(100, 400)
    assert window.btn_scan.text() == "스캔 중… 100/400 (약 5분 남음)"
    window.on_scan_progress(0, 400)
    assert window.btn_scan.text() == "스캔 중… 0/400"


def test_scan_warning_reaches_log_tab(window):
    window._make_screener(object()).log("warning", "조건식 결과 682종목 → 상위 400개만 검사")
    assert "상위 400개만 검사" in window.txt_log.toPlainText()
