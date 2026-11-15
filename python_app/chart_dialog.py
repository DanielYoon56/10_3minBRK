"""
Korea3MinBreakoutTrader - 매집 종목 차트 창 (감시 종목 표에서 [차트 보기] 또는 더블클릭)

일·주·월봉과 3·5·15분봉을 버튼으로 바꿔 본다. 주·월봉은 일봉을 합쳐서 만든다.
한 차트에 캔들(위쪽)과 거래량(아래쪽)을 함께 그린다. 거래량 축 상한을 크게 잡아 아래쪽에만 깔리게 한다.
노란 막대 = 매집 판단일(이 포함된 봉), 선 = 직전 20봉 평균 거래량 (일봉이면 매집 판정 evaluate_candle 과 같은 계산).
보유 종목이면 매수가(흰색)·손절가(적색)·익절가(녹색)를 매수 봉에서 차트 오른쪽 끝까지 뻗는 수평선으로 그린다.
트레일링을 끈 앱 관리 종목은 자동매매 중에 손절선·익절선을 마우스로 끌어 옮길 수 있다 (놓을 때 확인).
"""

import math
import threading
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCharts import (
    QBarCategoryAxis, QBarSet, QCandlestickSeries, QCandlestickSet, QChart, QChartView,
    QLineSeries, QStackedBarSeries, QValueAxis,
)
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from python_app.strategies.indicators import floor_to_tick

UP_COLOR, DOWN_COLOR, JUDGE_COLOR = "#ff7b72", "#79c0ff", "#e3b341"
BG_COLOR, GRID_COLOR, TEXT_COLOR = "#0d1117", "#21262d", "#8b949e"
BUY_COLOR, STOP_COLOR, PROFIT_COLOR = "#ffffff", "#ff3b30", "#3fb950"

PERIODS = {"D": "일봉", "W": "주봉", "M": "월봉", "3": "3분봉", "5": "5분봉", "15": "15분봉"}
DAILY_COUNT, MINUTE_COUNT = 60, 130     # 화면에 그릴 봉 수 (3분봉 130개 ≈ 하루)
MAX_LABELS = 65                         # 이보다 봉이 많으면 라벨을 건너뛰며 표시
BLANK = "​"                        # 축 카테고리는 중복을 허용하지 않아 빈 라벨은 길이가 다른 폭 0 문자로 만든다


@dataclass
class PriceLevel:
    """보유 종목의 가격선. since = 매수 시각 YYYYMMDDHHMMSS (빈 값이면 첫 봉부터 긋는다)"""
    label: str
    price: float
    color: str
    since: str = ""
    move: Optional[Callable[[float], str]] = None    # 끌어 옮길 수 있는 선이면 적용 함수 (거부 사유 반환)


@dataclass
class ChartData:
    bars: List[Dict]                     # 화면에 그릴 봉 (오래된 것부터)
    avg_volume: List[Optional[float]]    # 봉별 직전 avg_bars 개 평균 거래량 (자료 부족 시 None)


def chart_data(bars: List[Dict], count: int = DAILY_COUNT, avg_bars: int = 20) -> ChartData:
    """평균은 전체 봉으로 계산한 뒤 최근 count 개만 남긴다 (첫 봉들도 평균이 끊기지 않게)"""
    avg = [sum(b["volume"] for b in bars[i - avg_bars: i]) / avg_bars if i >= avg_bars else None
           for i in range(len(bars))]
    return ChartData(bars[-count:], avg[-count:])


def aggregate(bars: List[Dict], period: str) -> List[Dict]:
    """일봉 → 주봉("W", ISO 주) / 월봉("M"). dt = 마지막 거래일, first_dt = 첫 거래일"""
    def key(b):
        return datetime.strptime(b["dt"], "%Y%m%d").isocalendar()[:2] if period == "W" else b["dt"][:6]

    out = []
    for _, group in groupby(bars, key=key):
        g = list(group)
        out.append({"dt": g[-1]["dt"], "first_dt": g[0]["dt"], "open": g[0]["open"], "close": g[-1]["close"],
                    "high": max(b["high"] for b in g), "low": min(b["low"] for b in g),
                    "volume": sum(b["volume"] for b in g)})
    return out


def load_bars(broker, symbol: str, today: str, period: str) -> List[Dict]:
    if period == "D":
        return broker.get_daily_bars(symbol, today)
    if period in ("W", "M"):
        return aggregate(broker.get_daily_bars(symbol, today, pages=3), period)
    return broker.get_chart_minute_bars(symbol, int(period))


def is_judge_bar(bar: Dict, judge_dt: str) -> bool:
    """매집 판단일이 이 봉 기간에 드는가 (분봉은 dt 가 14자리라 항상 False)"""
    return bool(judge_dt) and bar.get("first_dt", bar["dt"]) <= judge_dt <= bar["dt"]


def bar_label(dt: str, period: str) -> str:
    if period == "D":
        return f"{dt[4:6]}/{dt[6:8]}"
    if period == "W":
        return f"{dt[2:4]}/{dt[4:6]}/{dt[6:8]}"     # 1년 넘게 보이므로 연도가 있어야 라벨이 겹치지 않는다
    if period == "M":
        return f"{dt[2:4]}/{dt[4:6]}"
    return f"{dt[4:6]}/{dt[6:8]} {dt[8:10]}:{dt[10:12]}"


def axis_labels(bars: List[Dict], period: str, pad: int = 1) -> List[str]:
    """최신 봉부터 step 칸마다 라벨. 맨 끝 라벨은 축을 넘치면 Qt 가 지우므로 뒤에 빈 칸을 pad 개 덧붙인다"""
    n = len(bars)
    step = math.ceil(n / MAX_LABELS)
    labels = [bar_label(b["dt"], period) if (n - 1 - i) % step == 0 else BLANK * (i + 1) for i, b in enumerate(bars)]
    return labels + [BLANK * (n + 1 + k) for k in range(pad)]


def right_pad(n: int) -> int:
    """가격선이 마지막 봉 오른쪽으로 뻗을 빈 칸 수 (라벨이 들어갈 폭)"""
    return max(6, n // 8)


def start_index(bars: List[Dict], since: str) -> int:
    """since 가 든 봉의 번호. 화면보다 오래된 매수면 첫 봉, 마지막 봉보다 뒤면 마지막 봉"""
    if not since:
        return 0
    return next((i for i, b in enumerate(bars) if since[:len(b["dt"])] <= b["dt"]), len(bars) - 1)


def label_heights(prices: Sequence[float], gap: float) -> List[float]:
    """오름차순 가격의 라벨 높이. 가까운 가격끼리 글자가 겹치지 않게 위쪽 라벨을 gap 만큼 띄운다"""
    heights: List[float] = []
    for price in prices:
        heights.append(max(price, heights[-1] + gap) if heights else price)
    return heights


class LevelChartView(QChartView):
    """가격선을 위아래로 끌 수 있는 차트 뷰. 끄는 동안 moved, 놓으면 dropped 로 선 번호와 호가 단위 가격을 알린다"""
    moved = Signal(int, float)
    dropped = Signal(int, float)
    GRAB_PX = 6

    def __init__(self):
        super().__init__()
        self.viewport().setMouseTracking(True)
        self._lines: List[QLineSeries] = []
        self._grabbed: Optional[int] = None

    def set_lines(self, lines: List[QLineSeries]):
        self._lines = lines
        self._grabbed = None
        self.unsetCursor()

    def _chart_pos(self, event) -> QPointF:
        return self.chart().mapFromScene(self.mapToScene(event.position().toPoint()))

    def _line_at(self, pos: QPointF) -> Optional[int]:
        if not self.chart().plotArea().contains(pos):
            return None
        for i, line in enumerate(self._lines):
            start = self.chart().mapToPosition(line.at(0), line)
            if abs(pos.y() - start.y()) <= self.GRAB_PX and pos.x() >= start.x():
                return i
        return None

    def _price_at(self, pos: QPointF) -> float:
        area = self.chart().plotArea()
        y = min(max(pos.y(), area.top()), area.bottom())
        return float(floor_to_tick(self.chart().mapToValue(QPointF(pos.x(), y), self._lines[self._grabbed]).y()))

    def mousePressEvent(self, event):
        grabbed = self._line_at(self._chart_pos(event)) if event.button() == Qt.LeftButton else None
        if grabbed is not None:
            self._grabbed = grabbed
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = self._chart_pos(event)
        if self._grabbed is not None:
            self.moved.emit(self._grabbed, self._price_at(pos))
            return
        if self._line_at(pos) is not None:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._grabbed is not None and event.button() == Qt.LeftButton:
            grabbed, price = self._grabbed, self._price_at(self._chart_pos(event))
            self._grabbed = None
            self.dropped.emit(grabbed, price)
            return
        super().mouseReleaseEvent(event)


class ChartDialog(QDialog):
    loaded = Signal(int, object, str)    # 요청 번호, 봉 목록, 오류

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resize(1100, 680)
        self._request = 0
        self._period = "D"
        self._judge_dt = ""
        self._target = None              # (broker, symbol, name, today)
        self._levels: Optional[Callable[[], List[PriceLevel]]] = None
        self._data: Optional[ChartData] = None
        self._movable: List[tuple] = []  # 끌 수 있는 선의 (가격선, 선 시리즈, 라벨 점)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setFont(QFont("Malgun Gothic", 12, QFont.Bold))
        top.addWidget(self.lbl_title)
        top.addStretch()
        self.period_buttons = {}
        group = QButtonGroup(self)
        for period, label in PERIODS.items():
            btn = QPushButton(label.replace("봉", ""))
            btn.setCheckable(True)
            btn.setChecked(period == self._period)
            btn.setStyleSheet("QPushButton:checked { background-color: #1f6feb; color: #ffffff; }")
            btn.clicked.connect(lambda _=False, p=period: self.set_period(p))
            group.addButton(btn)
            top.addWidget(btn)
            self.period_buttons[period] = btn
        self.lbl_detail = QLabel()
        self.lbl_detail.setWordWrap(True)
        self.view = LevelChartView()
        self.view.moved.connect(self._on_level_moved)
        self.view.dropped.connect(self._on_level_dropped)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setStyleSheet(f"background: {BG_COLOR}; border: none;")
        note = QLabel("빨강 양봉 · 파랑 음봉 · 노란 막대 = 매집 판단일(일·주·월봉) · 노란 선 = 직전 20봉 평균 거래량"
                      " · 보유 종목: 흰선 매수가, 적선 손절가, 녹선 익절가 (트레일링을 끄면 끌어서 변경)")
        note.setStyleSheet(f"color: {TEXT_COLOR};")
        lay.addLayout(top)
        lay.addWidget(self.lbl_detail)
        lay.addWidget(self.view, 1)
        lay.addWidget(note)
        self.loaded.connect(self._on_loaded)

    def show_symbol(self, symbol: str, name: str, detail: str, judge_dt: str, broker, today: str,
                    levels: Optional[Callable[[], List[PriceLevel]]] = None):
        """levels 는 그릴 때마다 부른다 (주기를 바꾸면 그사이 오른 트레일링가·청산 여부가 반영된다)"""
        self._target = (broker, symbol, name, today)
        self._judge_dt = judge_dt
        self._levels = levels
        self.lbl_title.setText(f"{name} ({symbol})")
        self.lbl_detail.setText(detail or "")
        self.lbl_detail.setVisible(bool(detail))
        self._reload()
        self.show()
        self.raise_()
        self.activateWindow()

    def set_period(self, period: str):
        self._period = period
        self.period_buttons[period].setChecked(True)
        if self._target:
            self._reload()

    def _reload(self):
        """조회는 네트워크라 별도 스레드에서 한다. 그사이 종목·주기를 바꾸면 이전 응답은 버린다"""
        broker, symbol, name, today = self._target
        self._request += 1
        self.setWindowTitle(f"{name}({symbol}) {PERIODS[self._period]}")
        self._set_blank("불러오는 중…")
        threading.Thread(target=self._load, args=(self._request, broker, symbol, today, self._period),
                         daemon=True).start()

    def _load(self, request: int, broker, symbol: str, today: str, period: str):
        try:
            self.loaded.emit(request, load_bars(broker, symbol, today, period), "")
        except Exception as e:
            self.loaded.emit(request, None, str(e))

    def _on_loaded(self, request: int, bars, err: str):
        if request != self._request:
            return
        label = PERIODS[self._period]
        if err:
            self._set_blank(f"{label} 조회 실패: {err}")
        elif not bars:
            self._set_blank(f"{label} 데이터가 없습니다")
        else:
            count = DAILY_COUNT if self._period in ("D", "W", "M") else MINUTE_COUNT
            self._data = chart_data(bars, count)
            self._redraw()

    def _set_blank(self, title: str):
        self._data = None
        self.view.set_lines([])
        self.view.setChart(self._blank_chart(title))

    def _redraw(self):
        self.view.set_lines([])
        self.view.setChart(self._build_chart(self._data, self._levels() if self._levels else []))
        self.view.set_lines([line for _, line, _ in self._movable])

    def _on_level_moved(self, index: int, price: float):
        """끄는 동안은 축을 그대로 두고 선과 라벨만 옮긴다"""
        level, line, tag = self._movable[index]
        for i in (0, 1):
            line.replace(i, line.at(i).x(), price)
        tag.replace(0, tag.at(0).x(), price)
        tag.setPointLabelsFormat(f"{level.label} {price:,.0f}")

    def _on_level_dropped(self, index: int, price: float):
        level = self._movable[index][0]
        if price != level.price and QMessageBox.question(
                self, f"{level.label}가 변경",
                f"{self.lbl_title.text()}\n{level.label}가를 {level.price:,.0f}원 → {price:,.0f}원으로 바꿀까요?\n"
                "바로 자동매매 청산 기준에 적용됩니다.") == QMessageBox.Yes:
            reason = level.move(price)
            if reason:
                QMessageBox.warning(self, f"{level.label}가 변경 실패", reason)
        self._redraw()

    def _blank_chart(self, title: str) -> QChart:
        chart = QChart()
        chart.setBackgroundBrush(QBrush(QColor(BG_COLOR)))
        chart.setTitleBrush(QBrush(QColor(TEXT_COLOR)))
        chart.setTitle(title)
        chart.legend().hide()
        return chart

    def _level_series(self, levels: List[PriceLevel], bars: List[Dict], pad: int, gap: float):
        """(가격선, 라벨 점) 시리즈. 라벨은 마지막 봉 오른쪽 빈 칸 가운데에 찍는다"""
        n = len(bars)
        lines, tags = [], []
        for level in levels:
            line = QLineSeries()
            line.setPen(QPen(QColor(level.color), 1.5))
            line.append(start_index(bars, level.since), level.price)
            line.append(n + pad - 0.5, level.price)
            lines.append(line)
        ordered = sorted(levels, key=lambda lv: lv.price)
        for level, y in zip(ordered, label_heights([lv.price for lv in ordered], gap)):
            tag = QLineSeries()
            tag.append(n - 0.5 + pad / 2, y)
            tag.setPointLabelsVisible(True)
            tag.setPointLabelsClipping(False)
            tag.setPointLabelsFormat(f"{level.label} {level.price:,.0f}")
            tag.setPointLabelsColor(QColor(level.color))
            tag.setPointLabelsFont(QFont("Malgun Gothic", 8, QFont.Bold))
            tags.append(tag)
            if level.move:
                self._movable.append((level, lines[levels.index(level)], tag))
        return lines, tags

    def _build_chart(self, data: ChartData, levels: Sequence[PriceLevel] = ()) -> QChart:
        chart = self._blank_chart("")
        bars = data.bars
        levels = list(levels)
        self._movable = []
        pad = right_pad(len(bars)) if levels else 1
        prices = [lv.price for lv in levels]
        low, high = min([b["low"] for b in bars] + prices), max([b["high"] for b in bars] + prices)
        span = (high - low) or high * 0.01 or 1
        gap = span * 0.05
        lines, tags = self._level_series(levels, bars, pad, gap)

        candles = QCandlestickSeries()
        candles.setIncreasingColor(QColor(UP_COLOR))
        candles.setDecreasingColor(QColor(DOWN_COLOR))
        candles.setBodyOutlineVisible(False)
        candles.setPen(QPen(QColor(TEXT_COLOR), 1))
        for i, b in enumerate(bars):
            candles.append(QCandlestickSet(b["open"], b["high"], b["low"], b["close"], float(i)))

        peak = max(max(b["volume"] for b in bars), 1)
        unit, unit_label = (1000, "천주") if peak >= 100_000 else (1, "주")
        volume = QStackedBarSeries()
        volume.setBarWidth(0.7)
        sets = {color: QBarSet("") for color in (UP_COLOR, DOWN_COLOR, JUDGE_COLOR)}
        for color, bar_set in sets.items():
            bar_set.setColor(QColor(color))
            bar_set.setBorderColor(QColor(color))
        for b in bars:
            color = (JUDGE_COLOR if is_judge_bar(b, self._judge_dt)
                     else UP_COLOR if b["close"] >= b["open"] else DOWN_COLOR)
            for key, bar_set in sets.items():
                bar_set.append(b["volume"] / unit if key == color else 0)
        volume.append(list(sets.values()))

        average = QLineSeries()
        average.setPen(QPen(QColor(JUDGE_COLOR), 1.5))
        for i, v in enumerate(data.avg_volume):
            if v is not None:
                average.append(i, v / unit)

        for series in (volume, candles, average, *lines, *tags):
            chart.addSeries(series)

        x_axis = QBarCategoryAxis()
        x_axis.append(axis_labels(bars, self._period, pad))
        x_axis.setLabelsAngle(-90)
        x_axis.setTruncateLabels(False)
        x_axis.setLabelsFont(QFont("Malgun Gothic", 7))
        price_axis, volume_axis = QValueAxis(), QValueAxis()
        top = max([high] + [tag.at(0).y() for tag in tags]) + gap
        price_axis.setRange(low - span * 0.45, top)                    # 아래쪽 30% 는 거래량 자리
        price_axis.applyNiceNumbers()
        price_axis.setLabelFormat("%.0f")
        price_axis.setTitleText("가격 (원)")
        volume_axis.setRange(0, peak / unit * 3.5)
        volume_axis.setLabelFormat("%.0f")
        volume_axis.setTitleText(f"거래량 ({unit_label})")
        volume_axis.setGridLineVisible(False)

        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(price_axis, Qt.AlignLeft)
        chart.addAxis(volume_axis, Qt.AlignRight)
        for axis in (x_axis, price_axis, volume_axis):
            axis.setLabelsBrush(QBrush(QColor(TEXT_COLOR)))
            axis.setTitleBrush(QBrush(QColor(TEXT_COLOR)))
            axis.setGridLineColor(QColor(GRID_COLOR))
            axis.setLinePen(QPen(QColor(GRID_COLOR)))
        for series, y_axis in ((candles, price_axis), (volume, volume_axis), (average, volume_axis),
                               *((line, price_axis) for line in lines + tags)):
            series.attachAxis(x_axis)
            series.attachAxis(y_axis)
        return chart
