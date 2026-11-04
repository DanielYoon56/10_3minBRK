"""
Korea3MinBreakoutTrader - Windows 데스크톱 자동매매 (PySide6 + 키움증권 REST API)

매수: 첫 3분봉 시가 상향 돌파 + 돌파 봉 거래량 > 첫 봉 거래량의 70%
매도: 손절 -2% / 트레일링 스탑 (+1% 이상 상승 후, 상승분의 15% 되돌림)

[모의거래] 키움 모의투자 서버(mockapi.kiwoom.com) — 모의투자용 앱키
[실거래]   키움 실서버(api.kiwoom.com)            — 실거래용 앱키, 전환·시작 시 이중 확인
"""

import argparse
import os
import subprocess
import sys
import logging
from datetime import datetime, time as dtime

from PySide6.QtCore import Qt, QThread, Signal as PySignal, QTimer, QTime, QLockFile
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QLineEdit, QSpinBox,
    QDoubleSpinBox, QTimeEdit, QTabWidget, QGroupBox, QSplitter, QTextEdit,
    QStatusBar, QHeaderView, QMessageBox, QButtonGroup, QInputDialog, QCheckBox, QComboBox,
    QScrollArea, QFrame,
)

# run.bat / "python main.py" 처럼 python_app 폴더 안에서 실행해도 패키지 임포트가 되도록 상위 폴더 등록
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import (
    APP_DIR, load_config, save_config, strategy_config_from, update_strategy_fields,
    kiwoom_keys, watchlist_from, telegram_settings,
)
from python_app.notify.telegram import TelegramNotifier
from python_app.broker.kiwoom_condition import KiwoomConditionClient
from python_app.screener.accumulation import AccumulationScreener, ScreenerConfig
from python_app.broker.kiwoom_rest import KiwoomRestBroker
from python_app.db.storage import DatabaseManager
from python_app.engine.live_engine import LiveTradingEngine, kst_now
from python_app.models.types import TradingMode
from python_app.strategy.combiner import STRATEGY_KEYS, STRATEGY_LABELS
from python_app.strategy.exit_rules import trade_config, trailing_stop_price

LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(os.path.join(LOG_DIR, f"{datetime.now():%Y%m%d}.log"), encoding="utf-8")],
)
TASK_NAME = "Korea3minTrader_AutoStart"
ROOT_DIR = os.path.dirname(APP_DIR)
logger = logging.getLogger("Korea3MinBreakoutTrader.Main")

# Windows Dark Financial Theme QSS
DARK_THEME_QSS = """
QMainWindow, QWidget {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: 'Segoe UI', 'Malgun Gothic', sans-serif;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #30363d;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
    color: #58a6ff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}
QTableWidget {
    background-color: #161b22;
    border: 1px solid #30363d;
    gridline-color: #21262d;
    selection-background-color: #1f6feb;
    selection-color: #ffffff;
}
QHeaderView::section {
    background-color: #21262d;
    color: #8b949e;
    padding: 5px;
    border: 1px solid #30363d;
    font-weight: 600;
}
QPushButton {
    background-color: #21262d;
    color: #c9d1d9;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 6px 12px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #30363d;
    border-color: #8b949e;
}
QPushButton#btn_start {
    background-color: #238636;
    color: #ffffff;
    border: none;
}
QPushButton#btn_start:hover {
    background-color: #2ea043;
}
QPushButton#btn_stop {
    background-color: #da3633;
    color: #ffffff;
    border: none;
}
QPushButton#btn_stop:hover {
    background-color: #f85149;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 4px;
    color: #c9d1d9;
}
QTabWidget::pane {
    border: 1px solid #30363d;
}
QTabBar::tab {
    background-color: #161b22;
    color: #8b949e;
    padding: 8px 16px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #21262d;
    color: #58a6ff;
    font-weight: bold;
    border-bottom: 2px solid #58a6ff;
}
QTextEdit {
    background-color: #161b22;
    border: 1px solid #30363d;
    color: #8b949e;
    font-family: 'Consolas', monospace;
}
QStatusBar {
    background-color: #161b22;
    border-top: 1px solid #30363d;
    color: #8b949e;
}
"""

MODE_QSS = """
QPushButton#mode_btn { padding: 6px 16px; font-weight: 700; }
QPushButton#mode_btn:checked[mode="MOCK"] { background-color: #1f6feb; color: #ffffff; border: 1px solid #1f6feb; }
QPushButton#mode_btn:checked[mode="REAL"] { background-color: #da3633; color: #ffffff; border: 1px solid #da3633; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #8b949e; border-radius: 3px; background: #0d1117; }
QCheckBox::indicator:checked { background: #1f6feb; border-color: #58a6ff; }
QCheckBox::indicator:hover { border-color: #58a6ff; }
QPushButton:disabled { color: #6e7681; background-color: #161b22; border-color: #21262d; }
"""

LIVE_CONFIRM_WORD = "실거래"


class EngineWorker(QThread):
    """자동매매 엔진을 백그라운드에서 1초마다 tick"""
    log_message = PySignal(str, str)   # level, message
    started_ok = PySignal(bool)

    def __init__(self, engine: LiveTradingEngine, live_confirmed: bool):
        super().__init__()
        self.engine = engine
        self.live_confirmed = live_confirmed
        self._running = False
        engine.on_event = lambda lv, msg: self.log_message.emit(lv, msg)

    def run(self):
        ok = self.engine.start(live_confirmed=self.live_confirmed)
        self.started_ok.emit(ok)
        if not ok:
            return
        self._running = True
        while self._running:
            try:
                self.engine.tick()
            except Exception as e:
                logger.exception("엔진 tick 오류")
                self.log_message.emit("error", f"엔진 오류: {e}")
            self.msleep(1000)
        self.engine.stop()

    def stop(self):
        self._running = False
        self.wait(5000)


class ScanWorker(QThread):
    """자동매매 시작 전 [지금 스캔] 미리보기용"""
    done = PySignal(object, str)   # ScreenResult or None, error

    def __init__(self, screener, now):
        super().__init__()
        self.screener, self.now = screener, now

    def run(self):
        try:
            self.done.emit(self.screener.run(self.now, use_cache=False), "")
        except Exception as e:
            self.done.emit(None, str(e))


def _scrollable(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setWidget(widget)
    return area


def _item(text, align=Qt.AlignCenter, color: str = ""):
    it = QTableWidgetItem(str(text))
    it.setTextAlignment(align)
    if color:
        it.setForeground(QColor(color))
    return it


class MainWindow(QMainWindow):
    def __init__(self, autostart: bool = False):
        super().__init__()
        self.setWindowTitle("Korea3MinBreakoutTrader — 3분봉 시가돌파 자동매매 [키움증권]")
        self.autostart = autostart
        self._force_close = False
        self._start_retries = 0
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, avail.width()), min(900, avail.height() - 40))   # 작은 화면이면 화면에 맞춤

        self.cfg = load_config()
        self.config = strategy_config_from(self.cfg)
        self.watchlist = watchlist_from(self.cfg)
        self.mode = TradingMode(self.cfg["trading_mode"])
        self.db = DatabaseManager(os.path.join(APP_DIR, "korea_breakout_trader.db"))
        self.engine: LiveTradingEngine = None
        self.worker: EngineWorker = None

        self._init_ui()
        self.setStyleSheet(DARK_THEME_QSS + MODE_QSS)
        self._apply_mode_ui()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(1000)

        if self.autostart:
            self.append_log("info", "자동 실행(--autostart)으로 켜짐 — 5초 후 자동매매 시작 절차 진행")
            QTimer.singleShot(5000, self.autostart_flow)

    # ================================================================ UI 구성
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- 헤더: 제목 / 시계 / 모의·실거래 전환 / 시작·중지
        header = QHBoxLayout()
        title = QLabel("KRX 3분봉 시가돌파 자동매매")
        title.setFont(QFont("Malgun Gothic", 14, QFont.Bold))
        title.setStyleSheet("color: #58a6ff;")
        header.addWidget(title)
        header.addStretch()

        self.lbl_clock = QLabel("KST --:--:--")
        self.lbl_clock.setFont(QFont("Consolas", 12))
        self.lbl_clock.setStyleSheet("color: #8b949e; margin-right: 12px;")
        header.addWidget(self.lbl_clock)

        self.btn_mock = QPushButton("모의거래")
        self.btn_real = QPushButton("실거래")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for btn, mode in ((self.btn_mock, TradingMode.MOCK), (self.btn_real, TradingMode.REAL)):
            btn.setObjectName("mode_btn")
            btn.setProperty("mode", mode.value)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self.mode_group.addButton(btn)
            header.addWidget(btn)
        self.btn_mock.clicked.connect(lambda: self.on_mode_clicked(TradingMode.MOCK))
        self.btn_real.clicked.connect(lambda: self.on_mode_clicked(TradingMode.REAL))
        header.addSpacing(16)

        self.btn_start = QPushButton("자동매매 시작")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self.start_trading)
        header.addWidget(self.btn_start)
        self.btn_stop = QPushButton("자동매매 중지")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_trading)
        header.addWidget(self.btn_stop)
        root.addLayout(header)

        # --- 모드 배너
        self.lbl_banner = QLabel()
        self.lbl_banner.setAlignment(Qt.AlignCenter)
        self.lbl_banner.setFont(QFont("Malgun Gothic", 11, QFont.Bold))
        root.addWidget(self.lbl_banner)

        # --- 계좌 요약
        summary = QGroupBox("계좌 및 당일 현황")
        sl = QHBoxLayout(summary)
        self.lbl_cash = QLabel("주문가능금액: -")
        self.lbl_pnl = QLabel("당일 추정 실현손익: 0원")
        self.lbl_active = QLabel(f"관리 종목: 0 / {self.config.max_concurrent_positions}")
        self.lbl_status = QLabel("상태: 대기")
        for lbl in (self.lbl_cash, self.lbl_pnl, self.lbl_active, self.lbl_status):
            lbl.setFont(QFont("Malgun Gothic", 11, QFont.DemiBold))
            sl.addWidget(lbl)
        root.addWidget(summary)

        # --- 본문
        splitter = QSplitter(Qt.Horizontal)

        self.wl_box = wl_box = QGroupBox("감시 종목")
        wl = QVBoxLayout(wl_box)
        self.table_watch = QTableWidget(0, 7)
        self.table_watch.setHorizontalHeaderLabels(["종목코드", "종목명", "매집 근거", "현재가", "돌파기준", "기준거래량", "상태"])
        self.table_watch.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_watch.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table_watch.verticalHeader().setVisible(False)
        wl.addWidget(self.table_watch)
        self._fill_watch_preview()
        splitter.addWidget(wl_box)

        tabs = QTabWidget()
        self.table_pos = QTableWidget(0, 10)
        self.table_pos.setHorizontalHeaderLabels([
            "종목", "구분", "수량", "매입가", "현재가", "수익률", "최고가", "손절가", "트레일링가", "상태"])
        self.table_pos.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_pos.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table_pos.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table_pos.verticalHeader().setVisible(False)
        tabs.addTab(self.table_pos, "보유 / 관리 종목")

        self.table_trades = QTableWidget(0, 7)
        self.table_trades.setHorizontalHeaderLabels(["종목", "주문번호", "상태", "수량", "진입가", "청산 사유", "추정손익"])
        self.table_trades.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_trades.verticalHeader().setVisible(False)
        tabs.addTab(self.table_trades, "당일 매매")
        tabs.addTab(self._build_daily_tab(), "일별 누적수익")

        # 설정 탭은 스크롤 영역에 넣는다 — 작은 화면(1280×720 등)에서도 창이 화면 밖으로 잘리지 않게
        tabs.addTab(_scrollable(self._build_strategy_tab()), "전략 설정")
        tabs.addTab(_scrollable(self._build_api_tab()), "키움 API 설정")
        tabs.addTab(_scrollable(self._build_automation_tab()), "알림·자동실행")

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        tabs.addTab(self.txt_log, "실행 로그")
        self.tabs = tabs
        tabs.currentChanged.connect(lambda i: tabs.widget(i) is self.daily_tab and self.refresh_daily())

        splitter.addWidget(tabs)
        splitter.setSizes([560, 880])
        root.addWidget(splitter, 1)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

    def _build_daily_tab(self) -> QWidget:
        self.daily_tab = w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("구분"))
        self.cmb_daily_mode = QComboBox()
        for mode, label in ((TradingMode.MOCK, "모의거래"), (TradingMode.REAL, "실거래")):
            self.cmb_daily_mode.addItem(label, mode.value)
        self.cmb_daily_mode.setCurrentIndex(self.cmb_daily_mode.findData(self.mode.value))
        self.cmb_daily_mode.currentIndexChanged.connect(self.refresh_daily)
        top.addWidget(self.cmb_daily_mode)
        btn = QPushButton("새로고침")
        btn.clicked.connect(self.refresh_daily)
        top.addWidget(btn)
        top.addSpacing(16)
        self.lbl_daily_sum = QLabel()
        self.lbl_daily_sum.setFont(QFont("Malgun Gothic", 11, QFont.DemiBold))
        top.addWidget(self.lbl_daily_sum)
        top.addStretch()
        lay.addLayout(top)

        self.table_daily = QTableWidget(0, 7)
        self.table_daily.setHorizontalHeaderLabels(["날짜", "청산 거래", "승 / 패", "승률", "당일 손익", "당일 수익률", "누적 손익"])
        self.table_daily.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_daily.verticalHeader().setVisible(False)
        lay.addWidget(self.table_daily)
        note = QLabel("※ 추정 실현손익(수수료·세금 추정 차감) 기준. 수익률 = 당일 손익 ÷ 당일 청산 종목 매입금액.")
        note.setStyleSheet("color: #8b949e;")
        lay.addWidget(note)
        self._daily_pnl_seen = None
        self.refresh_daily()
        return w

    def refresh_daily(self, *_):
        rows = self.db.get_daily_pnl(self.cmb_daily_mode.currentData())

        def color(v):
            return "#ff7b72" if v > 0 else "#79c0ff" if v < 0 else ""

        self.table_daily.setRowCount(len(rows))
        for r, d in enumerate(reversed(rows)):      # 최근 날짜가 위
            day = d["trade_date"]
            pnl = d["realized_pnl"]
            decided = d["wins"] + d["losses"]
            vals = (
                (f"{day[:4]}-{day[4:6]}-{day[6:]}", ""),
                (f"{d['trades']:,}", ""),
                (f"{d['wins']} / {d['losses']}", ""),
                (f"{d['wins'] / decided * 100:.0f}%" if decided else "-", ""),
                (f"{pnl:+,.0f}원", color(pnl)),
                (f"{pnl / d['invested'] * 100:+.2f}%" if d["invested"] else "-", color(pnl)),
                (f"{d['cum_pnl']:+,.0f}원", color(d["cum_pnl"])),
            )
            for c, (v, col) in enumerate(vals):
                self.table_daily.setItem(r, c, _item(v, color=col))

        total = rows[-1]["cum_pnl"] if rows else 0.0
        trades = sum(d["trades"] for d in rows)
        wins = sum(d["wins"] for d in rows)
        decided = wins + sum(d["losses"] for d in rows)
        self.lbl_daily_sum.setText(
            f"누적 손익 {total:+,.0f}원 · {len(rows)}거래일 · 청산 {trades}건"
            + (f" · 승률 {wins / decided * 100:.0f}%" if decided else ""))
        self.lbl_daily_sum.setStyleSheet(f"color: {color(total) or '#c9d1d9'};")

    def _build_strategy_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("매매 규칙 파라미터 (변경 즉시 적용·저장)")
        g = QGridLayout(box)

        def tedit(hhmm):
            h, m = (int(x) for x in hhmm.split(":"))
            te = QTimeEdit(QTime(h, m))
            te.setDisplayFormat("HH:mm")
            return te

        def dspin(lo, hi, step, val, dec=1):
            s = QDoubleSpinBox()
            s.setRange(lo, hi); s.setSingleStep(step); s.setDecimals(dec); s.setValue(val)
            return s

        def ispin(lo, hi, step, val):
            s = QSpinBox()
            s.setRange(lo, hi); s.setSingleStep(step); s.setValue(int(val))
            s.setGroupSeparatorShown(True)
            return s

        c = self.config
        self.te_start = tedit(c.trade_start_time)
        self.te_end = tedit(c.trade_end_time)
        self.spn_vol = dspin(0.1, 10.0, 0.1, c.min_volume_ratio, 2)
        self.spn_sl = dspin(0.5, 10.0, 0.1, c.stop_loss_rate)
        self.spn_trail = dspin(0.0, 90.0, 1.0, c.trailing_retrace_rate)
        self.spn_trail_act = dspin(0.0, 20.0, 0.1, c.trailing_activation_rate)
        self.spn_amount = ispin(100_000, 100_000_000, 100_000, c.order_amount_per_trade)
        self.spn_maxpos = ispin(1, 20, 1, c.max_concurrent_positions)
        self.spn_maxloss = ispin(10_000, 100_000_000, 50_000, c.max_daily_loss)
        self.chk_eod = QCheckBox("장 마감 전 일괄 청산 (앱 매수 종목 전량 시장가)")
        self.chk_eod.setChecked(c.eod_liquidation)
        self.te_eod = tedit(c.eod_liquidation_time)
        self.chk_carry = QCheckBox("앱이 꺼져 청산 못 한 종목은 다음 날 09:00 시장가 청산")
        self.chk_carry.setChecked(c.carryover_sell_at_open)
        self.chk_no_reentry = QCheckBox("당일 재진입 금지 (청산한 종목은 그날 다시 매수 안 함)")
        self.chk_no_reentry.setChecked(not c.allow_reentry_same_day)

        rows = [
            ("신규 진입 시작", self.te_start, "신규 진입 마감", self.te_end),
            ("돌파 봉 최소 거래량 (첫 봉 대비, 0.7 = 70%)", self.spn_vol, "손절 (%)", self.spn_sl),
            ("트레일링 되돌림 (상승분의 %)", self.spn_trail, "트레일링 발동: 진입 후 최고 상승률 ≥ (%)", self.spn_trail_act),
            ("1회 매수 금액 (원)", self.spn_amount, "최대 동시 보유 종목", self.spn_maxpos),
            ("일 손실한도 (원, 도달 시 신규 매수 중단)", self.spn_maxloss, "일괄 청산 시각 (15:20 동시호가 전 권장)", self.te_eod),
        ]
        for r, (l1, w1, l2, w2) in enumerate(rows):
            g.addWidget(QLabel(l1), r, 0); g.addWidget(w1, r, 1)
            if w2 is not None:
                g.addWidget(QLabel(l2), r, 2); g.addWidget(w2, r, 3)

        for wdg in (self.spn_vol, self.spn_sl, self.spn_trail, self.spn_trail_act):
            wdg.valueChanged.connect(self.on_settings_changed)
        for wdg in (self.spn_amount, self.spn_maxpos, self.spn_maxloss):
            wdg.valueChanged.connect(self.on_settings_changed)
        g.addWidget(self.chk_eod, len(rows), 0, 1, 2)
        g.addWidget(self.chk_carry, len(rows), 2, 1, 2)
        g.addWidget(self.chk_no_reentry, len(rows) + 1, 0, 1, 2)
        for wdg in (self.te_start, self.te_end, self.te_eod):
            wdg.timeChanged.connect(self.on_settings_changed)
        for wdg in (self.chk_eod, self.chk_carry, self.chk_no_reentry):
            wdg.toggled.connect(self.on_settings_changed)
        lay.addWidget(box)

        lay.addWidget(self._build_entry_box(tedit, dspin))
        lay.addWidget(self._build_screener_box())

        info = QLabel(
            "★ 감시종목: 키움 조건식 결과 중 전일 매집(① 거래량 급증·양봉·고가 근처 마감  또는  ② 외국인·기관 동시 순매수 상위) 종목\n"
            "★ 매수: 위 '매수 전략'에서 선택한 전략을 OR(하나라도) / AND(당일 모두 충족) 로 조합 — 시장가 진입 (SMC 만 FVG 중간값 지정가)\n"
            "★ 매도: ① 매입가 대비 손절  ② +1% 이상 오른 뒤 최고가까지 상승분의 N%를 되돌리면 전량 시장가 청산  ③ 장 마감 전 일괄 청산\n"
            "   (SMC 진입분: 전략 손절가 / 목표가(R 배수)로 청산, 트레일링은 체크 시에만)\n"
            "★ 주의: 감시·매도는 이 앱이 켜져 있을 때만 작동합니다 (PC 절전·종료 시 중단)\n"
            "★ 안전: 앱이 매수한 종목만 청산 · 늦게 시작하면 지나간 신호는 매수 안 함 · 일 손실한도 도달 시 신규 매수 중단")
        info.setStyleSheet("color: #8b949e;")
        lay.addWidget(info)
        lay.addStretch()
        return w

    def _build_entry_box(self, tedit, dspin) -> QWidget:
        c = self.config
        box = QGroupBox("매수 전략 (복수 선택 + 조합, 변경 즉시 적용·저장)")
        g = QGridLayout(box)
        self.chk_strats = {}
        hints = {
            "open_breakout": "첫 3분봉 시가 상향 돌파 + 거래량",
            "orb_high": "첫 3분봉 고가 상향 돌파 + 거래량 (갭상승 종목)",
            "vwap_pullback": "VWAP 위 추세 → VWAP 근처 눌림 후 양봉 반등",
            "prev_high": "전일 일봉 고가 상향 돌파 + 거래량",
            "day_swing_high": "당일 스윙 고점 상향 돌파 + 거래량, 자체 손절·트레일링",
            "smc_fvg": "하락 구조 → CHoCH → 첫 FVG 중간값 지정가, 목표 R 배수",
        }
        for i, key in enumerate(STRATEGY_KEYS):
            chk = QCheckBox(STRATEGY_LABELS[key])
            chk.setChecked(key in c.entry_strategies)
            chk.setToolTip(hints[key])
            chk.toggled.connect(self.on_settings_changed)
            self.chk_strats[key] = chk
            g.addWidget(chk, i // 3, i % 3)
        self.cmb_logic = QComboBox()
        self.cmb_logic.addItems(["OR — 하나라도 신호 시 진입", "AND — 선택 전략 모두 당일 신호 시 진입"])
        self.cmb_logic.setCurrentIndex(1 if str(c.entry_logic).upper() == "AND" else 0)
        self.cmb_logic.currentIndexChanged.connect(self.on_settings_changed)
        g.addWidget(QLabel("조합"), 2, 0)
        g.addWidget(self.cmb_logic, 2, 1)

        self.te_vwap_end = tedit(c.vwap_end_time)
        self.te_smc_start = tedit(c.smc_start_time)
        self.te_smc_end = tedit(c.smc_end_time)
        self.spn_rr = dspin(1.0, 10.0, 0.5, c.smc_rr)
        self.cmb_stop_mode = QComboBox()
        self.cmb_stop_mode.addItems(["보호 저점(CHoCH 레그 최저가) 아래", "FVG 하단 아래"])
        self.cmb_stop_mode.setCurrentIndex(1 if c.smc_stop_mode == "fvg" else 0)
        self.chk_smc_trail = QCheckBox("SMC 진입분도 트레일링 스탑 적용")
        self.chk_smc_trail.setChecked(c.smc_use_trailing)
        self.te_dsh_start = tedit(c.dsh_start_time)
        self.te_dsh_end = tedit(c.dsh_end_time)
        self.spn_dsh_stop = dspin(0.1, 10.0, 0.1, c.dsh_stop_rate)
        self.spn_dsh_trail = dspin(1.0, 90.0, 5.0, c.dsh_trail_retrace_rate)
        self.spn_dsh_act = dspin(0.0, 20.0, 0.1, c.dsh_trail_activation_rate)
        rows = [
            ("B. VWAP 진입 마감", self.te_vwap_end, "SMC 목표 (R 배수, 3 = 3R)", self.spn_rr),
            ("SMC 진입 시작", self.te_smc_start, "SMC 진입 마감", self.te_smc_end),
            ("SMC 손절 기준", self.cmb_stop_mode, None, self.chk_smc_trail),
            ("D. 진입 시작", self.te_dsh_start, "D. 진입 마감", self.te_dsh_end),
            ("D. 손절 (-%)", self.spn_dsh_stop, "D. 상승분 되돌림 (%)", self.spn_dsh_trail),
            ("D. 트레일링 발동 (+%)", self.spn_dsh_act, None, QLabel("")),
        ]
        for r, (l1, w1, l2, w2) in enumerate(rows, start=3):
            g.addWidget(QLabel(l1), r, 0); g.addWidget(w1, r, 1)
            if l2:
                g.addWidget(QLabel(l2), r, 2)
            g.addWidget(w2, r, 3)
        for te in (self.te_vwap_end, self.te_smc_start, self.te_smc_end, self.te_dsh_start, self.te_dsh_end):
            te.timeChanged.connect(self.on_settings_changed)
        for spn in (self.spn_dsh_stop, self.spn_dsh_trail, self.spn_dsh_act):
            spn.valueChanged.connect(self.on_settings_changed)
        self.spn_rr.valueChanged.connect(self.on_settings_changed)
        self.cmb_stop_mode.currentIndexChanged.connect(self.on_settings_changed)
        self.chk_smc_trail.toggled.connect(self.on_settings_changed)
        note = QLabel("A·C 는 위 '신규 진입 시작~마감', B 는 시작~VWAP 마감, SMC·D 는 자체 시간대를 씁니다. "
                      "AND 는 신호가 크게 줄어듭니다. 세부값(눌림 폭·손절폭 범위·지정가 유효 봉 수)은 config.json")
        note.setStyleSheet("color: #8b949e;")
        note.setWordWrap(True)
        g.addWidget(note, len(rows) + 3, 0, 1, 4)
        return box

    def _build_screener_box(self) -> QWidget:
        sc = self.cfg["screener"]
        box = QGroupBox("감시종목 자동 선정 — 키움 조건식 AND 전일 매집 (① 거래량·캔들  또는  ② 외국인·기관 동시 순매수)")
        g = QGridLayout(box)

        self.chk_scr = QCheckBox("매일 장 전(08:00~) 자동 선정 사용  (끄면 설정 파일의 고정 종목 감시)")
        self.chk_scr.setChecked(bool(sc.get("enabled", True)))
        g.addWidget(self.chk_scr, 0, 0, 1, 4)

        self.cmb_cond = QComboBox()
        self.cmb_cond.setEditable(True)
        self.cmb_cond.setMinimumWidth(220)
        if sc.get("condition_name"):
            self.cmb_cond.addItem(sc["condition_name"])
        self.cmb_cond.setCurrentText(sc.get("condition_name", ""))
        self.cmb_cond.lineEdit().setPlaceholderText("영웅문에서 '서버 저장'한 조건식 이름")
        btn_list = QPushButton("조건식 목록 불러오기")
        btn_list.clicked.connect(self.load_condition_list)
        g.addWidget(QLabel("키움 조건식"), 1, 0); g.addWidget(self.cmb_cond, 1, 1, 1, 2); g.addWidget(btn_list, 1, 3)

        def dspin(lo, hi, step, val, dec=1, suffix=""):
            w = QDoubleSpinBox(); w.setRange(lo, hi); w.setSingleStep(step); w.setDecimals(dec); w.setValue(val)
            if suffix:
                w.setSuffix(suffix)
            return w

        def ispin(lo, hi, step, val, suffix=""):
            w = QSpinBox(); w.setRange(lo, hi); w.setSingleStep(step); w.setValue(int(val)); w.setGroupSeparatorShown(True)
            if suffix:
                w.setSuffix(suffix)
            return w

        self.scr_vol = dspin(1.0, 20.0, 0.5, sc["vol_surge_ratio"], 1, " 배")
        self.scr_wick = dspin(0, 100, 5, sc["upper_wick_max"] * 100, 0, " %")
        self.scr_value = ispin(0, 100_000, 10, sc["min_trade_value"] / 1e8, " 억")
        self.scr_top = ispin(1, 100, 5, sc["net_buy_top"], " 위")
        self.scr_netbuy = ispin(0, 100_000, 5, sc["min_net_buy_amount"] / 1e8, " 억")
        self.scr_max = ispin(1, 100, 5, sc["max_watch"], " 종목")
        self.scr_price = ispin(0, 1_000_000, 500, sc["min_price"], " 원")
        self.scr_mcap = ispin(0, 1_000_000, 500, sc["min_market_cap"] / 1e8, " 억")
        rows = [
            ("① 전일 거래량 ≥ 20일 평균 ×", self.scr_vol, "① 윗꼬리 ≤ (당일 범위 대비)", self.scr_wick),
            ("① 전일 거래대금 ≥", self.scr_value, "② 외국인·기관 각각 순매수 상위", self.scr_top),
            ("② 순매수 금액 ≥", self.scr_netbuy, "최대 감시 종목 수", self.scr_max),
            ("주가 ≥ (조건식 미사용 시만)", self.scr_price, "시가총액 ≥ (조건식 미사용 시만)", self.scr_mcap),
        ]
        for r, (l1, w1, l2, w2) in enumerate(rows, start=2):
            g.addWidget(QLabel(l1), r, 0); g.addWidget(w1, r, 1); g.addWidget(QLabel(l2), r, 2); g.addWidget(w2, r, 3)

        self.btn_scan = QPushButton("지금 스캔 (결과 미리보기)")
        self.btn_scan.clicked.connect(self.scan_now)
        g.addWidget(self.btn_scan, len(rows) + 2, 3)

        for w in (self.scr_vol, self.scr_wick, self.scr_value, self.scr_top, self.scr_netbuy, self.scr_max,
                  self.scr_price, self.scr_mcap):
            w.valueChanged.connect(self.save_screener)
        self.chk_scr.toggled.connect(self.save_screener)
        self.cmb_cond.currentTextChanged.connect(self.save_screener)
        self._sync_screener_enabled()
        return box

    # ================================================================ 감시종목 선정
    def _screener_cfg(self) -> ScreenerConfig:
        return ScreenerConfig.from_dict(self.cfg["screener"])

    def _sync_screener_enabled(self):
        use_cond = bool(self.cmb_cond.currentText().strip())
        for w in (self.scr_price, self.scr_mcap):
            w.setEnabled(not use_cond)

    def save_screener(self, *_):
        sc = self.cfg["screener"]
        sc.update({
            "enabled": self.chk_scr.isChecked(),
            "condition_name": self.cmb_cond.currentText().strip(),
            "vol_surge_ratio": self.scr_vol.value(),
            "upper_wick_max": round(self.scr_wick.value() / 100, 3),
            "min_trade_value": int(self.scr_value.value() * 1e8),
            "net_buy_top": self.scr_top.value(),
            "min_net_buy_amount": int(self.scr_netbuy.value() * 1e8),
            "max_watch": self.scr_max.value(),
            "min_price": self.scr_price.value(),
            "min_market_cap": int(self.scr_mcap.value() * 1e8),
        })
        save_config(self.cfg)
        self._sync_screener_enabled()
        if self.engine and self.engine.screener:
            self.engine.screener.cfg = self._screener_cfg()
        if not self.engine:
            self._fill_watch_preview()

    def _make_screener(self, broker) -> AccumulationScreener:
        return AccumulationScreener(
            broker, self._screener_cfg(), state_dir=APP_DIR,
            log=lambda lv, m: self.append_log(lv, m) if QThread.currentThread() == self.thread()
            else getattr(logger, lv if lv in ("info", "warning", "error") else "info")(m),
            condition_client_factory=lambda: KiwoomConditionClient(broker, raw_log_dir=LOG_DIR))

    def _temp_broker(self):
        self._keys_from_inputs()
        app_key, secret = kiwoom_keys(self.cfg, self.mode)
        if not app_key or not secret:
            QMessageBox.warning(self, "키 필요", "'키움 API 설정' 탭에 현재 모드의 앱키/시크릿키를 입력하세요.")
            return None
        return KiwoomRestBroker(app_key, secret, mode=self.mode, config=self.config)

    def load_condition_list(self):
        broker = self._temp_broker()
        if not broker:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with KiwoomConditionClient(broker, raw_log_dir=LOG_DIR) as cc:
                conds = cc.list_conditions()
            err = ""
        except Exception as e:
            conds, err = [], str(e)
        finally:
            QApplication.restoreOverrideCursor()
        if err:
            QMessageBox.critical(self, "조건식 목록", f"불러오기 실패:\n{err}")
            return
        if not conds:
            QMessageBox.information(self, "조건식 목록",
                                    "저장된 조건식이 없습니다.\n영웅문 [0150] 조건검색에서 조건식을 만들고 '서버 저장' 하세요.")
            return
        cur = self.cmb_cond.currentText()
        self.cmb_cond.blockSignals(True)
        self.cmb_cond.clear()
        self.cmb_cond.addItems([nm for _, nm in conds])
        self.cmb_cond.setCurrentText(cur if cur in [nm for _, nm in conds] else conds[0][1])
        self.cmb_cond.blockSignals(False)
        self.save_screener()
        self.append_log("info", f"조건식 {len(conds)}개: {', '.join(nm for _, nm in conds)}")

    def scan_now(self):
        if self.engine and self.worker and self.worker.isRunning():
            if self.engine.request_rescan():
                self.append_log("info", "감시종목 다시 스캔 요청")
            else:
                self.append_log("info", "이미 스캔 중이거나 자동 선정이 꺼져 있습니다")
            return
        broker = self._temp_broker()
        if not broker:
            return
        if not broker.connect():
            QMessageBox.critical(self, "스캔", "키움 접속 실패 — 키와 모드를 확인하세요.")
            return
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("스캔 중… (최대 1~2분)")
        self._scan_worker = ScanWorker(self._make_screener(broker), kst_now())
        self._scan_worker.done.connect(self.on_scan_done)
        self._scan_worker.start()

    def on_scan_done(self, res, err: str):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("지금 스캔 (결과 미리보기)")
        if err or res is None:
            self.append_log("error", f"스캔 실패: {err}")
            QMessageBox.critical(self, "스캔", f"스캔 실패:\n{err}")
            return
        self._preview = res
        self._fill_watch_preview()
        if res.errors:
            self.append_log("warning", " / ".join(res.errors[:3]))
        msg = (f"기준일 {res.prev_date or '-'} · 후보 {res.candidates_checked} → 선정 {len(res.hits)}종목\n\n" +
               "\n".join(f"· {h.name}({h.symbol}) [{'+'.join(h.reasons)}] {h.detail}" for h in res.hits[:20]))
        QMessageBox.information(self, "스캔 결과", msg if res.hits else msg + "\n조건을 만족한 종목이 없습니다.")

    def _fill_watch_preview(self):
        """자동매매 시작 전 감시종목 표시 (스캔 미리보기 또는 고정 종목)"""
        res = getattr(self, "_preview", None)
        sc = self.cfg.get("screener", {})
        if res is not None:
            rows = [(h.symbol, h.name, "+".join(h.reasons)) for h in res.hits]
            title = f"감시 종목 — 미리보기 {len(rows)}종목 (기준일 {res.prev_date})"
        elif sc.get("enabled", True):
            rows = []
            cond = sc.get("condition_name")
            title = ("감시 종목 — 장 전 자동 선정 예정 " +
                     (f"(조건식 '{cond}' AND 매집)" if cond else "(조건식 미지정 → 전 시장 매집)"))
        else:
            rows = [(s_, n, "고정") for s_, n in self.watchlist]
            title = f"감시 종목 — 고정 {len(rows)}종목"
        self.wl_box.setTitle(title)
        self.table_watch.setRowCount(len(rows))
        for r, (sym, name, why) in enumerate(rows):
            for c, v in enumerate((sym, name, why, "-", "-", "-", "대기")):
                self.table_watch.setItem(r, c, _item(v))

    def _build_api_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.key_inputs = {}
        for mode, title, color in ((TradingMode.MOCK, "모의거래 (모의투자 서버 · mockapi.kiwoom.com)", "#58a6ff"),
                                   (TradingMode.REAL, "실거래 (실서버 · api.kiwoom.com)", "#f85149")):
            box = QGroupBox(title)
            box.setStyleSheet(f"QGroupBox {{ color: {color}; }}")
            g = QGridLayout(box)
            key = "real" if mode == TradingMode.REAL else "mock"
            entry = self.cfg["kiwoom"][key]
            ed_app = QLineEdit(entry.get("app_key", ""))
            ed_sec = QLineEdit(entry.get("secret_key", ""))
            for ed in (ed_app, ed_sec):
                ed.setEchoMode(QLineEdit.Password)
                ed.setPlaceholderText("키움 OpenAPI 포털에서 발급")
            g.addWidget(QLabel("App Key"), 0, 0); g.addWidget(ed_app, 0, 1)
            g.addWidget(QLabel("Secret Key"), 1, 0); g.addWidget(ed_sec, 1, 1)
            btn_test = QPushButton("연결 테스트")
            btn_test.clicked.connect(lambda _=False, m=mode: self.test_connection(m))
            g.addWidget(btn_test, 0, 2, 2, 1)
            self.key_inputs[mode] = (ed_app, ed_sec)
            lay.addWidget(box)

        row = QHBoxLayout()
        btn_show = QPushButton("키 보이기/숨기기")
        btn_show.clicked.connect(self.toggle_key_visibility)
        btn_save = QPushButton("저장")
        btn_save.setObjectName("btn_start")
        btn_save.clicked.connect(self.save_keys)
        row.addWidget(btn_show); row.addStretch(); row.addWidget(btn_save)
        lay.addLayout(row)

        help_lbl = QLabel(
            "· 키움은 모의투자/실거래 앱키를 따로 발급합니다. 각 칸에 맞는 키를 넣으세요.\n"
            "· 키움 OpenAPI 포털에서 이 PC의 공인 IP를 등록해야 접속됩니다.\n"
            "· 키는 이 PC의 python_app/config.json 에만 저장되며 Git에 올라가지 않습니다.\n"
            "· 환경변수 KIWOOM_MOCK_APPKEY / KIWOOM_MOCK_SECRETKEY / KIWOOM_REAL_APPKEY / KIWOOM_REAL_SECRETKEY 가 있으면 우선 사용합니다.")
        help_lbl.setStyleSheet("color: #8b949e;")
        lay.addWidget(help_lbl)
        lay.addStretch()
        return w

    def _build_automation_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # --- 텔레그램
        tg_box = QGroupBox("텔레그램 알림 (매수·매도·손절·트레일링·일괄청산·오류·일일 요약)")
        g = QGridLayout(tg_box)
        tg = self.cfg["telegram"]
        self.chk_tg = QCheckBox("알림 사용")
        self.chk_tg.setChecked(bool(tg.get("enabled")))
        self.ed_tg_token = QLineEdit(tg.get("bot_token", ""))
        self.ed_tg_token.setEchoMode(QLineEdit.Password)
        self.ed_tg_token.setPlaceholderText("@BotFather 에서 받은 봇 토큰 (123456:ABC-...)")
        self.ed_tg_chat = QLineEdit(str(tg.get("chat_id", "")))
        self.ed_tg_chat.setPlaceholderText("봇에게 메시지를 보낸 뒤 [chat_id 자동 찾기]")
        btn_find = QPushButton("chat_id 자동 찾기")
        btn_find.clicked.connect(self.find_chat_id)
        btn_test = QPushButton("테스트 메시지")
        btn_test.clicked.connect(self.test_telegram)
        btn_save = QPushButton("저장")
        btn_save.setObjectName("btn_start")
        btn_save.clicked.connect(self.save_automation)
        g.addWidget(self.chk_tg, 0, 0)
        g.addWidget(QLabel("봇 토큰"), 1, 0); g.addWidget(self.ed_tg_token, 1, 1, 1, 2)
        g.addWidget(QLabel("chat_id"), 2, 0); g.addWidget(self.ed_tg_chat, 2, 1); g.addWidget(btn_find, 2, 2)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(btn_test); row.addWidget(btn_save)
        g.addLayout(row, 3, 0, 1, 3)
        lay.addWidget(tg_box)

        # --- 자동 실행
        au = self.cfg["autostart"]
        au_box = QGroupBox("자동 실행 (평일 08:00 Windows 작업 스케줄러)")
        ag = QGridLayout(au_box)
        self.chk_auto_trade = QCheckBox("자동 실행으로 켜지면 자동매매까지 자동 시작")
        self.chk_auto_trade.setChecked(bool(au.get("auto_start_trading", True)))
        self.chk_auto_real = QCheckBox("실거래 모드도 자동 시작 허용 (확인창 없이 실계좌 주문)")
        self.chk_auto_real.setChecked(bool(au.get("allow_real_autostart", False)))
        self.chk_auto_real.clicked.connect(self.on_auto_real_clicked)
        self.chk_auto_quit = QCheckBox("장 종료 후 자동매매 중지 + 앱 자동 종료")
        self.chk_auto_quit.setChecked(bool(au.get("quit_after_close", True)))
        h, m = (int(x) for x in au.get("quit_time", "15:40").split(":"))
        self.te_quit = QTimeEdit(QTime(h, m)); self.te_quit.setDisplayFormat("HH:mm")
        for wdg in (self.chk_auto_trade, self.chk_auto_quit):
            wdg.toggled.connect(self.save_automation_silent)
        self.te_quit.timeChanged.connect(self.save_automation_silent)
        ag.addWidget(self.chk_auto_trade, 0, 0, 1, 3)
        ag.addWidget(self.chk_auto_real, 1, 0, 1, 3)
        ag.addWidget(self.chk_auto_quit, 2, 0, 1, 2); ag.addWidget(self.te_quit, 2, 2)

        self.lbl_task = QLabel("Windows 자동 시작: 확인 중…")
        self.btn_task_on = QPushButton("Windows 자동 시작 등록")
        self.btn_task_on.setObjectName("btn_start")
        self.btn_task_on.clicked.connect(lambda: self.set_windows_task(True))
        self.btn_task_off = QPushButton("등록 해제")
        self.btn_task_off.clicked.connect(lambda: self.set_windows_task(False))
        ag.addWidget(self.lbl_task, 3, 0)
        ag.addWidget(self.btn_task_on, 3, 1); ag.addWidget(self.btn_task_off, 3, 2)
        lay.addWidget(au_box)

        help_lbl = QLabel(
            "· 등록하면 평일 08:00에 PC가 절전 중이어도 깨워서 앱을 실행합니다.\n"
            "  단, '절전 모드 해제 타이머 허용'이 '사용'이어야 합니다 (Windows 설정 앱엔 없음):\n"
            "  Win+R → control powercfg.cpl → 설정 변경 → 고급 전원 관리 옵션 설정 변경 → 절전 → 절전 모드 해제 타이머 허용\n"
            "· PC가 꺼져 있다가 나중에 켜지면 그때 실행됩니다. 이미 지나간 신호로는 매수하지 않습니다.\n"
            "· 실거래 자동 시작은 위 체크를 켠 경우에만 동작합니다. 꺼져 있으면 시작하지 않고 휴대폰으로 알려 줍니다.\n"
            "· 앱이 이미 켜져 있으면 두 번째 실행은 자동으로 취소됩니다 (중복 주문 방지).\n"
            "· 실행 기록은 python_app/logs/날짜.log 에 저장됩니다.")
        help_lbl.setStyleSheet("color: #8b949e;")
        lay.addWidget(help_lbl)
        lay.addStretch()
        QTimer.singleShot(500, self.refresh_task_status)
        return w

    # ================================================================ 알림·자동실행
    def _automation_from_inputs(self):
        self.cfg["telegram"] = {"enabled": self.chk_tg.isChecked(),
                                "bot_token": self.ed_tg_token.text().strip(),
                                "chat_id": self.ed_tg_chat.text().strip()}
        self.cfg["autostart"] = {"auto_start_trading": self.chk_auto_trade.isChecked(),
                                 "allow_real_autostart": self.chk_auto_real.isChecked(),
                                 "quit_after_close": self.chk_auto_quit.isChecked(),
                                 "quit_time": self.te_quit.time().toString("HH:mm")}

    def save_automation_silent(self, *_):
        self._automation_from_inputs()
        save_config(self.cfg)

    def save_automation(self):
        self.save_automation_silent()
        QMessageBox.information(self, "저장", "알림·자동실행 설정을 저장했습니다.")

    def on_auto_real_clicked(self, checked: bool):
        if checked:
            text, ok = QInputDialog.getText(
                self, "실거래 자동 시작 허용",
                "PC가 자동으로 켜질 때 확인창 없이 실계좌 자동매매가 시작됩니다.\n"
                f"허용하려면 '{LIVE_CONFIRM_WORD}' 를 입력하세요:")
            if not ok or text.strip() != LIVE_CONFIRM_WORD:
                self.chk_auto_real.setChecked(False)
                return
        self.save_automation_silent()
        self.append_log("info", f"실거래 자동 시작 {'허용' if self.chk_auto_real.isChecked() else '해제'}")

    def _make_notifier(self, mode: TradingMode = None) -> TelegramNotifier:
        self._automation_from_inputs()
        enabled, token, chat = telegram_settings(self.cfg)
        mode = mode or self.mode
        return TelegramNotifier(token, chat, enabled=enabled,
                                prefix="[실거래]" if mode == TradingMode.REAL else "[모의]")

    def find_chat_id(self):
        self._automation_from_inputs()
        _, token, _ = telegram_settings(self.cfg)
        if not token:
            QMessageBox.warning(self, "chat_id 찾기", "봇 토큰을 먼저 입력하세요.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            chats = TelegramNotifier(token, "").find_chat_ids()
        finally:
            QApplication.restoreOverrideCursor()
        if not chats:
            QMessageBox.information(self, "chat_id 찾기",
                                    "찾지 못했습니다.\n텔레그램에서 만든 봇을 열어 아무 메시지(예: 안녕)를 보낸 뒤 다시 누르세요.")
            return
        cid, name = chats[-1]
        self.ed_tg_chat.setText(cid)
        self.chk_tg.setChecked(True)
        self.save_automation_silent()
        QMessageBox.information(self, "chat_id 찾기", f"찾았습니다: {name} ({cid})\n[테스트 메시지]로 확인하세요.")

    def test_telegram(self):
        n = self._make_notifier()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = n.send_now(f"{n.prefix} ✅ Korea3MinBreakoutTrader 알림 테스트 ({kst_now():%H:%M:%S})")
        finally:
            QApplication.restoreOverrideCursor()
        (QMessageBox.information if ok else QMessageBox.critical)(self, "테스트 메시지", msg)

    def _schtasks_query(self) -> bool:
        if os.name != "nt":
            return False
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0

    def refresh_task_status(self):
        if os.name != "nt":
            self.lbl_task.setText("Windows 자동 시작: Windows에서만 사용 가능")
            self.btn_task_on.setEnabled(False); self.btn_task_off.setEnabled(False)
            return
        on = self._schtasks_query()
        self.lbl_task.setText(f"Windows 자동 시작: {'✅ 등록됨 (평일 08:00)' if on else '미등록'}")

    def set_windows_task(self, enable: bool):
        script = os.path.join(ROOT_DIR, "tools", "autostart.ps1")
        if os.name != "nt" or not os.path.exists(script):
            QMessageBox.warning(self, "자동 시작", f"등록 스크립트를 찾을 수 없습니다:\n{script}")
            return
        self.save_automation_silent()
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
        if not enable:
            args.append("-Remove")
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout + r.stderr).strip()
        self.append_log("info" if r.returncode == 0 else "error", f"자동 시작 {'등록' if enable else '해제'}: {out[-300:]}")
        self.refresh_task_status()
        if r.returncode != 0:
            QMessageBox.critical(self, "자동 시작", f"실패했습니다.\n{out[-500:]}")

    # ---------------------------------------------------------------- --autostart 흐름
    def _quit_time(self) -> dtime:
        return datetime.strptime(self.cfg["autostart"].get("quit_time", "15:40"), "%H:%M").time()

    def autostart_flow(self):
        au = self.cfg["autostart"]
        now = kst_now()
        notifier = self._make_notifier()
        if not au.get("auto_start_trading", True):
            self.append_log("info", "자동매매 자동 시작이 꺼져 있어 앱만 실행했습니다.")
            return
        if now.weekday() >= 5:
            self.append_log("info", "주말이라 자동매매를 시작하지 않습니다.")
            return
        if au.get("quit_after_close", True) and now.time() >= self._quit_time():
            self.append_log("info", "장 종료 이후 실행 → 자동매매 없이 종료합니다.")
            QTimer.singleShot(3000, self._auto_quit)
            return
        if self.mode == TradingMode.REAL and not au.get("allow_real_autostart", False):
            msg = "⚠ 실거래 모드지만 '실거래 자동 시작 허용'이 꺼져 있어 자동매매를 시작하지 않았습니다."
            self.append_log("warning", msg)
            notifier.notify(msg)
            return
        self.start_trading(interactive=False)

    def _auto_quit(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        n = self.engine.notifier if self.engine else None
        if n:
            n.flush(5)
        self._force_close = True
        self.close()

    # ================================================================ 모드 전환
    def _apply_mode_ui(self):
        (self.btn_real if self.mode == TradingMode.REAL else self.btn_mock).setChecked(True)
        for b in (self.btn_mock, self.btn_real):
            b.style().unpolish(b); b.style().polish(b)
        if self.mode == TradingMode.REAL:
            self.lbl_banner.setText("⚠ 실거래 모드 — 실제 계좌로 주문이 나갑니다")
            self.lbl_banner.setStyleSheet("background:#3d1214; color:#ff7b72; border:1px solid #da3633; border-radius:4px; padding:6px;")
        else:
            self.lbl_banner.setText("모의거래 모드 — 키움 모의투자 서버 (실제 돈 사용 안 함)")
            self.lbl_banner.setStyleSheet("background:#0c2d6b; color:#79c0ff; border:1px solid #1f6feb; border-radius:4px; padding:6px;")
        self.statusBar.showMessage(f"현재 모드: {'실거래' if self.mode == TradingMode.REAL else '모의거래'}")
        self.cmb_daily_mode.setCurrentIndex(self.cmb_daily_mode.findData(self.mode.value))

    def on_mode_clicked(self, mode: TradingMode):
        if mode == self.mode:
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "모드 전환", "자동매매를 중지한 뒤 모드를 바꿀 수 있습니다.")
            self._apply_mode_ui()
            return
        if mode == TradingMode.REAL:
            text, ok = QInputDialog.getText(
                self, "실거래 전환 확인",
                "실거래 모드에서는 실제 계좌로 주문이 나갑니다.\n"
                f"계속하려면 '{LIVE_CONFIRM_WORD}' 를 입력하세요:")
            if not ok or text.strip() != LIVE_CONFIRM_WORD:
                self._apply_mode_ui()
                self.append_log("info", "실거래 전환 취소됨")
                return
        self.mode = mode
        self.cfg["trading_mode"] = mode.value
        save_config(self.cfg)
        self._apply_mode_ui()
        self.append_log("info", f"모드 전환 → {'실거래' if mode == TradingMode.REAL else '모의거래'}")

    # ================================================================ 키 설정
    def _keys_from_inputs(self):
        for mode, (ed_app, ed_sec) in self.key_inputs.items():
            key = "real" if mode == TradingMode.REAL else "mock"
            self.cfg["kiwoom"][key] = {"app_key": ed_app.text().strip(), "secret_key": ed_sec.text().strip()}

    def save_keys(self):
        self._keys_from_inputs()
        save_config(self.cfg)
        QMessageBox.information(self, "저장", "키움 API 키를 저장했습니다.")

    def toggle_key_visibility(self):
        for ed_app, ed_sec in self.key_inputs.values():
            for ed in (ed_app, ed_sec):
                ed.setEchoMode(QLineEdit.Normal if ed.echoMode() == QLineEdit.Password else QLineEdit.Password)

    def test_connection(self, mode: TradingMode):
        self._keys_from_inputs()
        app_key, secret = kiwoom_keys(self.cfg, mode)
        label = "실거래" if mode == TradingMode.REAL else "모의거래"
        if not app_key or not secret:
            QMessageBox.warning(self, "연결 테스트", f"{label} 앱키/시크릿키를 입력하세요.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            b = KiwoomRestBroker(app_key, secret, mode=mode, config=self.config)
            ok = b.connect()
            cash = b.get_cash_balance() if ok else 0
            n = len(b.get_positions()) if ok else 0
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            QMessageBox.information(self, "연결 테스트", f"{label} 접속 성공\n주문가능금액: {cash:,.0f}원\n보유 종목: {n}개")
        else:
            QMessageBox.critical(self, "연결 테스트",
                                 f"{label} 접속 실패\n· 키가 {label}용인지\n· 키움 포털에 이 PC IP가 등록됐는지 확인하세요.\n(자세한 내용은 실행 로그)")

    # ================================================================ 전략 설정
    def on_settings_changed(self, *_):
        c = self.config
        c.trade_start_time = self.te_start.time().toString("HH:mm")
        c.trade_end_time = self.te_end.time().toString("HH:mm")
        c.min_volume_ratio = round(self.spn_vol.value(), 2)
        c.stop_loss_rate = self.spn_sl.value()
        c.trailing_retrace_rate = self.spn_trail.value()
        c.trailing_activation_rate = self.spn_trail_act.value()
        c.order_amount_per_trade = self.spn_amount.value()
        c.max_concurrent_positions = self.spn_maxpos.value()
        c.max_daily_loss = self.spn_maxloss.value()
        c.eod_liquidation = self.chk_eod.isChecked()
        c.eod_liquidation_time = self.te_eod.time().toString("HH:mm")
        c.carryover_sell_at_open = self.chk_carry.isChecked()
        c.allow_reentry_same_day = not self.chk_no_reentry.isChecked()
        c.entry_strategies = [k for k in STRATEGY_KEYS if self.chk_strats[k].isChecked()]
        c.entry_logic = "AND" if self.cmb_logic.currentIndex() == 1 else "OR"
        c.vwap_end_time = self.te_vwap_end.time().toString("HH:mm")
        c.smc_start_time = self.te_smc_start.time().toString("HH:mm")
        c.smc_end_time = self.te_smc_end.time().toString("HH:mm")
        c.smc_rr = round(self.spn_rr.value(), 2)
        c.smc_stop_mode = "fvg" if self.cmb_stop_mode.currentIndex() == 1 else "swing"
        c.smc_use_trailing = self.chk_smc_trail.isChecked()
        c.dsh_start_time = self.te_dsh_start.time().toString("HH:mm")
        c.dsh_end_time = self.te_dsh_end.time().toString("HH:mm")
        c.dsh_stop_rate = round(self.spn_dsh_stop.value(), 2)
        c.dsh_trail_retrace_rate = round(self.spn_dsh_trail.value(), 2)
        c.dsh_trail_activation_rate = round(self.spn_dsh_act.value(), 2)
        if not c.entry_strategies:
            self.append_log("warning", "매수 전략이 하나도 선택되지 않았습니다 — 신규 매수 없음")
        if self.engine:
            self.engine.strategy.update_config(c)
        update_strategy_fields(self.cfg, c)
        save_config(self.cfg)

    # ================================================================ 시작/중지
    def start_trading(self, interactive: bool = True):
        self._keys_from_inputs()
        app_key, secret = kiwoom_keys(self.cfg, self.mode)
        label = "실거래" if self.mode == TradingMode.REAL else "모의거래"
        if not app_key or not secret:
            msg = f"'키움 API 설정' 탭에 {label}용 앱키/시크릿키를 입력하세요."
            if interactive:
                QMessageBox.warning(self, "키 필요", msg)
                self.tabs.setCurrentIndex(3)
            else:
                self.append_log("error", msg)
                self._make_notifier().notify(f"⚠ 자동 시작 실패: {msg}")
            return

        live_confirmed = False
        if self.mode == TradingMode.REAL and not interactive:
            live_confirmed = bool(self.cfg["autostart"].get("allow_real_autostart"))   # 설정 시 사전 동의
        elif self.mode == TradingMode.REAL:
            c = self.config
            ans = QMessageBox.warning(
                self, "실거래 자동매매 시작",
                "실제 계좌로 자동 주문을 시작합니다.\n\n"
                f"· 1회 매수 금액: {c.order_amount_per_trade:,}원\n"
                f"· 최대 동시 보유: {c.max_concurrent_positions}종목\n"
                f"· 일 손실한도: {c.max_daily_loss:,}원\n"
                f"· 당일 재진입: {'금지' if not c.allow_reentry_same_day else '허용'}\n"
                f"· 손절 -{c.stop_loss_rate:g}% / 트레일링: +{c.trailing_activation_rate:g}% 이상 상승 후 상승분 {c.trailing_retrace_rate:g}% 되돌림\n"
                f"· 장 마감 일괄 청산: {c.eod_liquidation_time if c.eod_liquidation else '사용 안 함'}\n\n"
                "※ 감시·매도는 이 앱이 켜져 있는 동안만 작동합니다. PC 절전을 꺼 두세요.\n\n"
                "시작할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
            live_confirmed = True

        broker = KiwoomRestBroker(app_key, secret, mode=self.mode, config=self.config)
        screener = self._make_screener(broker) if self.cfg["screener"].get("enabled", True) else None
        if screener:   # 엔진 스레드에서 로그는 엔진 이벤트로 전달
            screener.log = lambda lv, m: self.engine._emit(lv, m) if self.engine else None
        self.engine = LiveTradingEngine(broker, self.config, self.watchlist, state_dir=APP_DIR, db=self.db,
                                        notifier=self._make_notifier(), screener=screener)
        self.worker = EngineWorker(self.engine, live_confirmed)
        self.worker.log_message.connect(lambda lv, m: self.append_log(lv, m, _from_engine=True))
        self.worker.started_ok.connect(self.on_engine_started)
        self.btn_start.setEnabled(False)
        self.btn_mock.setEnabled(False)
        self.btn_real.setEnabled(False)
        self.lbl_status.setText("상태: 접속 중…")
        self.worker.start()

    def on_engine_started(self, ok: bool):
        if ok:
            self.btn_stop.setEnabled(True)
            self.lbl_status.setText("상태: 자동매매 가동 중")
            self.statusBar.showMessage(f"[{'실거래' if self.mode == TradingMode.REAL else '모의거래'}] 자동매매 가동 중")
            self._start_retries = 0
        else:
            self._reset_buttons()
            self.lbl_status.setText("상태: 접속 실패")
            if self.autostart:
                # 절전 해제 직후 네트워크가 늦게 붙는 경우 대비: 1분 간격 최대 5회 재시도
                self._start_retries += 1
                if self._start_retries <= 5:
                    self.append_log("warning", f"접속 실패 → 60초 후 재시도 ({self._start_retries}/5)")
                    QTimer.singleShot(60_000, lambda: self.start_trading(interactive=False))
                else:
                    self._make_notifier().notify("⚠ 자동 시작 실패: 키움 접속 5회 실패 — PC와 키 설정을 확인하세요")
            else:
                QMessageBox.critical(self, "시작 실패", "키움 접속에 실패했습니다. 실행 로그와 'API 설정'을 확인하세요.")

    def stop_trading(self):
        if self.worker:
            self.worker.stop()
        self._reset_buttons()
        self.lbl_status.setText("상태: 중지됨 (보유 종목 유지)")

    def _reset_buttons(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_mock.setEnabled(True)
        self.btn_real.setEnabled(True)

    def closeEvent(self, event):
        if self._force_close:
            event.accept()
            return
        if self.worker and self.worker.isRunning():
            ans = QMessageBox.question(self, "종료", "자동매매가 가동 중입니다. 중지하고 종료할까요?\n(보유 종목은 청산되지 않습니다)")
            if ans != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.stop()
        event.accept()

    # ================================================================ 화면 갱신
    def append_log(self, level: str, msg: str, _from_engine: bool = False):
        if not _from_engine:      # 엔진 메시지는 엔진에서 이미 파일에 기록됨
            getattr(logger, level if level in ("info", "warning", "error") else "info")(msg)
        color = {"error": "#f85149", "warning": "#d29922"}.get(level, "#c9d1d9")
        self.txt_log.append(f'<span style="color:#6e7681">[{datetime.now():%H:%M:%S}]</span> '
                            f'<span style="color:{color}">{msg}</span>')

    def refresh_ui(self):
        now = kst_now()
        self.lbl_clock.setText(now.strftime("KST %H:%M:%S"))
        if (self.autostart and not self._force_close and self.cfg["autostart"].get("quit_after_close", True)
                and now.time() >= self._quit_time() and self.worker and self.worker.isRunning()):
            self.append_log("info", "장 종료 시각 → 자동매매 중지 후 앱 종료")
            self._auto_quit()
            return
        if not self.engine:
            return
        snap = self.engine.snapshot()
        self.lbl_cash.setText(f"주문가능금액: {snap['cash']:,.0f}원")
        pnl = snap["realized_pnl"]
        self.lbl_pnl.setText(f"당일 추정 실현손익: {pnl:+,.0f}원")
        if pnl != self._daily_pnl_seen:      # 청산 발생 → 일별 누적수익 탭 갱신
            self._daily_pnl_seen = pnl
            self.refresh_daily()
        self.lbl_pnl.setStyleSheet(f"color: {'#ff7b72' if pnl > 0 else '#79c0ff' if pnl < 0 else '#c9d1d9'};")
        active = sum(1 for m in snap["managed"].values() if m["status"] in ("BUYING", "HOLDING", "SELLING"))
        self.lbl_active.setText(f"관리 종목: {active} / {self.config.max_concurrent_positions}")
        if snap["halted"]:
            self.lbl_status.setText("상태: 신규 매수 중단 (일 손실한도)")

        quotes = snap["quotes"]
        watch = snap["watchlist"]
        screen = snap.get("screen", {})
        status = snap.get("screen_status") or ""
        self.wl_box.setTitle(f"감시 종목 {len(watch)}개" + (f" — {status}" if status else ""))
        if self.table_watch.rowCount() != len(watch):
            self.table_watch.setRowCount(len(watch))
        for r, (sym, name) in enumerate(watch):
            st = snap["states"].get(sym, {})
            px = quotes.get(sym)
            lvl = st.get("breakout_level")
            info = screen.get(sym, {})
            self.table_watch.setItem(r, 0, _item(sym))
            self.table_watch.setItem(r, 1, _item(name))
            why = "+".join(info.get("reasons", [])) or ("고정" if not self.engine.screener else "-")
            it = _item(why)
            if info.get("detail"):
                it.setToolTip(info["detail"])
            self.table_watch.setItem(r, 2, it)
            self.table_watch.setItem(r, 3, _item(f"{px:,.0f}" if px else "-"))
            self.table_watch.setItem(r, 4, _item(f"{lvl:,.0f}" if lvl else "-"))
            vol = st.get("first_bar_volume")
            self.table_watch.setItem(r, 5, _item(f"{vol:,}" if vol else "-"))
            m = snap["managed"].get(sym)
            if m and m["status"] in ("BUYING", "HOLDING", "SELLING"):
                text, color = {"BUYING": ("매수 주문중", "#d29922"), "HOLDING": ("보유중", "#ff7b72"),
                               "SELLING": ("매도 주문중", "#d29922")}[m["status"]]
            elif st.get("signal"):
                text, color = "당일 신호 발생", "#ff7b72"
            elif lvl and px:
                text, color = ("기준선 위", "#3fb950") if px > lvl else ("기준선 아래", "#8b949e")
            else:
                text, color = "첫 봉 대기", "#8b949e"
            self.table_watch.setItem(r, 6, _item(text, color=color))

        rows = []
        for sym, p in snap["positions"].items():
            m = snap["managed"].get(sym)
            managed = m is not None and m["status"] in ("BUYING", "HOLDING", "SELLING")
            entry = (m["entry_price"] if managed and m["entry_price"] else p["avg_price"])
            cur = quotes.get(sym, p["current_price"])
            peak = max(m["highest_price"] if managed else p["highest_price"], entry)
            trail = (trailing_stop_price(entry, peak, trade_config(self.config, m["trail_retrace_rate"],
                                                                   m["trail_activation_rate"]))
                     if managed and m["use_trailing"] else None)
            stop = (m["stop_price"] or entry * (1 - self.config.stop_loss_rate / 100)) if managed else 0
            if managed and not m["use_trailing"]:
                trail_txt = f"목표 {m['target_price']:,.0f}" if m["target_price"] else "미적용"
            else:
                trail_txt = f"{trail:,.0f}" if trail else ("상승 대기" if managed else "-")
            rows.append((
                f"{p['name']}({sym})",
                "앱 매수" if managed else "기존 보유 · 관리 안 함",
                f"{p['qty']:,}", f"{entry:,.0f}", f"{cur:,.0f}",
                f"{(cur / entry - 1) * 100:+.2f}%" if entry else "-",
                f"{peak:,.0f}" if managed else "-",
                f"{stop:,.0f}" if managed else "-",
                trail_txt,
                {"HOLDING": "보유", "SELLING": "매도 주문중", "BUYING": "매수 주문중"}.get(m["status"], "-") if managed else "-",
            ))
        self.table_pos.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.table_pos.setItem(r, c, _item(v))

        trades = list(snap["managed"].values())
        self.table_trades.setRowCount(len(trades))
        status_kr = {"BUYING": "매수 주문중", "HOLDING": "보유", "SELLING": "매도 주문중",
                     "CLOSED": "청산 완료", "FAILED": "미체결 취소"}
        for r, m in enumerate(trades):
            vals = (m["name"], m["order_id"], status_kr.get(m["status"], m["status"]), f"{m['qty']:,}",
                    f"{m['entry_price']:,.0f}" if m["entry_price"] else "-", m["exit_reason"] or "-",
                    f"{m['realized_pnl']:+,.0f}" if m["status"] == "CLOSED" else "-")
            for c, v in enumerate(vals):
                self.table_trades.setItem(r, c, _item(v))


def main():
    parser = argparse.ArgumentParser(description="Korea3MinBreakoutTrader")
    parser.add_argument("--autostart", action="store_true", help="작업 스케줄러용: 켜지면 자동매매 자동 시작")
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv)

    # 중복 실행 방지 (두 개가 동시에 돌면 같은 신호로 주문이 두 번 나감)
    lock = QLockFile(os.path.join(APP_DIR, "app.lock"))
    if not lock.tryLock(100):
        logger.warning("이미 실행 중 → 이번 실행 취소")
        if not args.autostart:
            QMessageBox.warning(None, "이미 실행 중", "Korea3MinBreakoutTrader 가 이미 실행 중입니다.")
        sys.exit(0)

    window = MainWindow(autostart=args.autostart)
    if args.autostart:
        window.showMinimized()
    else:
        window.show()
    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
