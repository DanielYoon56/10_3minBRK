"""3WAYTRADING - 전략들이 함께 쓰는 순수 계산 함수 (상태 없음)"""

from datetime import datetime
from typing import List, Sequence, Tuple


def avg_volume(bars: Sequence) -> float:
    return sum(b.volume for b in bars) / len(bars) if bars else 0.0


def swing_points(bars: Sequence, n: int) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """확정된 스윙 고점·저점 → ([(봉 위치, 고가)], [(봉 위치, 저가)]) (시간순).
    스윙 고점 = 왼쪽 n봉보다 고가가 높고 오른쪽 n봉보다 낮지 않은 봉. 오른쪽 n봉이 완성돼야 확정된다
    (마지막 n봉은 아직 스윙이 될 수 없다 → 미래 봉을 보지 않는다)."""
    highs, lows = [], []
    for i in range(n, len(bars) - n):
        left, right = bars[i - n:i], bars[i + 1:i + n + 1]
        if bars[i].high > max(b.high for b in left) and bars[i].high >= max(b.high for b in right):
            highs.append((i, bars[i].high))
        if bars[i].low < min(b.low for b in left) and bars[i].low <= min(b.low for b in right):
            lows.append((i, bars[i].low))
    return highs, lows


def sma(values: Sequence[float], n: int) -> float:
    """마지막 n 개의 단순 평균 (n 개가 안 되면 있는 만큼이 아니라 오류 — 호출 전에 길이를 확인할 것)"""
    if n <= 0 or len(values) < n:
        raise ValueError(f"sma: {n}개가 필요한데 {len(values)}개뿐")
    return sum(values[-n:]) / n


def weekly_bars(daily: Sequence[dict], today: str) -> List[dict]:
    """일봉(오래된 순, dt = YYYYMMDD) → 완성된 주봉 (ISO 주 단위).
    today 가 속한 주는 아직 끝나지 않았으므로 넣지 않는다 (미완성 봉을 완성 봉처럼 쓰지 않는다)."""
    def week(dt: str):
        return datetime.strptime(dt, "%Y%m%d").isocalendar()[:2]

    current = week(today)
    out: List[dict] = []
    for b in daily:
        if b["dt"] >= today or week(b["dt"]) == current:
            continue
        key = week(b["dt"])
        if out and out[-1]["week"] == key:
            w = out[-1]
            w["dt"], w["close"] = b["dt"], b["close"]
            w["high"], w["low"] = max(w["high"], b["high"]), min(w["low"], b["low"])
            w["volume"] += b["volume"]
        else:
            out.append({"week": key, "first_dt": b["dt"], "dt": b["dt"], "open": b["open"], "high": b["high"],
                        "low": b["low"], "close": b["close"], "volume": b["volume"]})
    return out


def tick_size(price: float) -> int:
    """KRX 호가 단위 (2023-01-25 개편, 코스피·코스닥 공통)"""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def floor_to_tick(price: float) -> int:
    t = tick_size(price)
    return int(price // t) * t
