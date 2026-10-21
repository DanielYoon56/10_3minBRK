"""
Korea3MinBreakoutTrader - 설정 파일 (python_app/config.json)

- 최초 실행 시 config.example.json 을 복사해 config.json 을 만든다.
- 앱키/시크릿키는 config.json 에만 저장되며 .gitignore 로 Git 업로드가 차단된다.
- 환경변수가 있으면 파일 값보다 우선한다:
    KIWOOM_MOCK_APPKEY / KIWOOM_MOCK_SECRETKEY
    KIWOOM_REAL_APPKEY / KIWOOM_REAL_SECRETKEY
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
"""

import json
import os
import shutil
import sys
from dataclasses import fields
from typing import Any, Dict, List, Tuple

from python_app.models.types import StrategyConfig, TradingMode

if getattr(sys, "frozen", False):
    # PyInstaller EXE: 사용자 파일은 EXE 옆, 템플릿은 번들 내부
    APP_DIR = os.path.dirname(sys.executable)
    _BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE_DIR = APP_DIR
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
EXAMPLE_PATH = os.path.join(_BUNDLE_DIR, "config.example.json")

_STRATEGY_KEYS = {f.name for f in fields(StrategyConfig)}


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    if not os.path.exists(path) and os.path.exists(EXAMPLE_PATH):
        shutil.copyfile(EXAMPLE_PATH, path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("trading_mode", TradingMode.MOCK.value)
    if cfg["trading_mode"] not in (TradingMode.MOCK.value, TradingMode.REAL.value):
        cfg["trading_mode"] = TradingMode.MOCK.value   # 구버전 'PAPER' 등 → 모의거래
    # 설정 버전 이전: v2 = 트레일링 발동 기준 0% → 1% (예전에 저장된 0.0 을 새 기본값으로)
    if int(cfg.get("config_version", 1)) < 2:
        if float(cfg.get("trailing_activation_rate", 0.0) or 0.0) == 0.0:
            cfg["trailing_activation_rate"] = 1.0
        cfg["config_version"] = 2
    kw = cfg.setdefault("kiwoom", {})
    for mode in ("mock", "real"):
        kw.setdefault(mode, {"app_key": "", "secret_key": ""})
    tg = cfg.setdefault("telegram", {})
    for k, v in (("enabled", False), ("bot_token", ""), ("chat_id", "")):
        tg.setdefault(k, v)
    from dataclasses import asdict as _asdict
    from python_app.screener.accumulation import ScreenerConfig
    sc = cfg.setdefault("screener", {})
    for k, v in _asdict(ScreenerConfig()).items():
        sc.setdefault(k, v)
    au = cfg.setdefault("autostart", {})
    for k, v in (("auto_start_trading", True), ("allow_real_autostart", False),
                 ("quit_after_close", True), ("quit_time", "15:40")):
        au.setdefault(k, v)
    return cfg


def save_config(cfg: Dict[str, Any], path: str = CONFIG_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def strategy_config_from(cfg: Dict[str, Any]) -> StrategyConfig:
    return StrategyConfig(**{k: v for k, v in cfg.items() if k in _STRATEGY_KEYS})


def update_strategy_fields(cfg: Dict[str, Any], sc: StrategyConfig):
    for k in _STRATEGY_KEYS:
        cfg[k] = getattr(sc, k)


def kiwoom_keys(cfg: Dict[str, Any], mode: TradingMode) -> Tuple[str, str]:
    m = "real" if mode == TradingMode.REAL else "mock"
    env = "REAL" if mode == TradingMode.REAL else "MOCK"
    entry = cfg.get("kiwoom", {}).get(m, {})
    app_key = os.environ.get(f"KIWOOM_{env}_APPKEY") or entry.get("app_key", "")
    secret = os.environ.get(f"KIWOOM_{env}_SECRETKEY") or entry.get("secret_key", "")
    return app_key.strip(), secret.strip()


def watchlist_from(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(str(w["symbol"]).zfill(6), w.get("name", w["symbol"])) for w in cfg.get("watchlist", [])]


def telegram_settings(cfg: Dict[str, Any]) -> Tuple[bool, str, str]:
    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(tg.get("chat_id", ""))
    return bool(tg.get("enabled")), token.strip(), chat.strip()
