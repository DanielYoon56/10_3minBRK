"""
3WAYTRADING - WAY 3: CLOSE 전략 (장 마감 전 매수 → 다음 거래일 청산)

분석 순서는 항상 주봉 → 일봉 → 3분봉이다. 앞 단계가 통과하지 못하면 뒤 단계를 보지 않는다.
오늘 강했다는 이유만으로 사지 않는다. 장 마감 후 갭 위험을 지고 갈 만한 구조일 때만 산다.

[1. 주봉 — analyze_weekly]   완성된 주만 사용 (이번 주는 미완성이라 제외)
   BULLISH   종가 > 중기 이평, 단기 > 중기 > 장기 이평(정배열), 중기 이평 상승
   BEARISH   종가 < 중기 이평, 단기 < 중기 이평, 중기 이평 하락
   NEUTRAL   그 외
   UNKNOWN   주봉이 부족해 판단 불가 → 진입 금지 (없는 값을 지어내지 않는다)
   참고값: 최근 4주가 그 앞 4주보다 고점·저점이 모두 높은가, 지난주 거래량 / 직전 10주 평균, 26주 고가까지 거리

[2. 일봉 — analyze_daily]   완성된 일봉(어제까지) + 현재가. 당일 일봉은 미완성이라 쓰지 않는다
   DOWNTREND       현재가 < 20일선 이고 20일선 하락
   OVEREXTENDED    20일선 이격 > d_max_ext_pct 또는 최근 d_run_days 일 상승률 > d_max_run_pct 또는 당일 상승률 > d_max_day_change_pct
   BREAKOUT_SETUP  현재가 ≥ 최근 d_high_days 일 고가 × (1 - d_near_high_pct%), 20일선 위, 20일선이 내려가지 않음
   PULLBACK        20일선 상승, 60일선 위, 최근 d_pb_days 일 안에 저가가 20일선 근처(+d_pb_touch_pct% 이내)까지 눌림,
                   현재가가 5일선과 전일 종가 위로 회복
   UPTREND         현재가 > 20일선, 5일선 > 20일선, 20일선 상승
   CONSOLIDATION   그 외
   UNKNOWN         일봉 부족
   진입 가능: UPTREND · PULLBACK · BREAKOUT_SETUP (CONSOLIDATION 은 allow_consolidation 을 켰을 때만)

[3. 3분봉 — analyze_3min]   장 막판 m_bars 봉(기본 10봉 = 30분)
   VWAP 위 · 종가가 당일 범위 상단부(m_min_range_pos 이상) · 당일 고가에서 m_max_from_high_pct% 이내 ·
   막판 급락 없음(최근 고가 대비 -m_max_late_drop_pct% 이내) · 막판 후반부 저점이 전반부 저점보다 낮지 않음 ·
   양봉 거래량 ≥ 음봉 거래량 × m_min_updown_ratio (매수 우위) · 막판 평균 거래량 ≥ 당일 평균 × m_vol_ratio

[위험 — 오버나이트]
   - 손절 기준 = 최근 stop_bars 봉 최저가. 손절폭 min_stop_pct ~ max_stop_pct, 저항(최근 res_days 일 고가, 상한가)까지 여유 확인
   - 비중 = 기본 매수 금액 × size_ratio (자금 보호의 오버나이트 한도가 따로 적용된다)
   - 다음 거래일 09:00 부터 손절·트레일링을 그대로 감시하고, exit_time 까지 남아 있으면 시장가 청산.
     그날 장 마감 일괄 청산 대상이기도 하다 (최대 1일 보유).
   - 손절 주문은 장 마감 후 갭을 막지 못한다. 다음 날 시가가 손절가 아래로 열리면 그 가격에 팔린다.
   - 실적 발표·공시 일정은 조회 수단이 없어 판정하지 않는다.
   - 키움 일봉은 애프터마켓 거래까지 포함해 20시에 확정된다 → 전일 종가·거래량은 그 값이다.

모든 기본값은 검증 전 임시값이다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, NamedTuple, Optional

from python_app.models.types import Position, SignalType, StrategyConfig
from python_app.strategies.base_strategy import (BaseStrategy, MarketContext, StrategySettings, StrategySignal,
                                                 SymbolSession, parse_hhmm)
from python_app.strategies.common_rules import confirmation_ratio, index_block, index_change, stop_and_room
from python_app.strategies.indicators import avg_volume, sma, weekly_bars
from python_app.strategy.exit_rules import ExitDecision

logger = logging.getLogger("3WAYTRADING.Close")

BULLISH, NEUTRAL, BEARISH, UNKNOWN = "BULLISH", "NEUTRAL", "BEARISH", "UNKNOWN"
UPTREND, PULLBACK, BREAKOUT_SETUP = "UPTREND", "PULLBACK", "BREAKOUT_SETUP"
CONSOLIDATION, OVEREXTENDED, DOWNTREND = "CONSOLIDATION", "OVEREXTENDED", "DOWNTREND"


@dataclass
class CloseSettings(StrategySettings):
    start_time: str = "14:48"
    end_time: str = "15:15"
    exit_time: str = "10:30"              # 다음 거래일 이 시각까지 남아 있으면 시장가 청산
    size_ratio: float = 0.5               # 기본 매수 금액 대비 비중 (오버나이트 갭 위험 때문에 작게)
    allow_neutral_weekly: bool = False
    allow_consolidation: bool = False
    # 주봉
    w_ma_short: int = 5
    w_ma_mid: int = 10
    w_ma_long: int = 20
    w_slope_weeks: int = 3
    # 일봉
    d_ma_short: int = 5
    d_ma_mid: int = 20
    d_ma_long: int = 60
    d_slope_days: int = 5
    d_max_ext_pct: float = 15.0
    d_run_days: int = 5
    d_max_run_pct: float = 25.0
    d_max_day_change_pct: float = 12.0
    d_high_days: int = 20
    d_near_high_pct: float = 2.0
    d_pb_days: int = 5
    d_pb_touch_pct: float = 2.0
    # 3분봉
    m_bars: int = 10
    m_min_range_pos: float = 0.7
    m_max_from_high_pct: float = 2.0
    m_max_late_drop_pct: float = 1.0
    m_min_updown_ratio: float = 1.0
    m_vol_ratio: float = 0.5
    # 공통·위험
    min_cum_value: int = 10_000_000_000
    max_gaps: int = 5
    min_index_change_pct: float = -1.0
    stop_bars: int = 20
    min_stop_pct: float = 1.0
    max_stop_pct: float = 4.0
    min_rr: float = 1.5
    upper_limit_pct: float = 30.0
    res_days: int = 60


class View(NamedTuple):
    label: str            # 분류 (3분봉은 "VALID" / "INVALID")
    reason: str           # 판단 근거 또는 탈락 사유
    detail: dict


class CloseStrategy(BaseStrategy):
    name = "close"
    label = "CLOSE"
    patterns = ("swing",)
    settings_class = CloseSettings
    needs_daily = True
    needs_index = True

    def _apply_settings(self):
        super()._apply_settings()
        self.exit_time = parse_hhmm(self.settings.exit_time)

    # ================================================================ 1. 주봉
    def analyze_weekly(self, daily: List[dict], today: str) -> View:
        cfg = self.settings
        weeks = weekly_bars(daily, today)
        if len(weeks) < cfg.w_ma_long + cfg.w_slope_weeks:
            return View(UNKNOWN, f"완성된 주봉 {len(weeks)}개 (필요 {cfg.w_ma_long + cfg.w_slope_weeks}개)", {})
        closes = [w["close"] for w in weeks]
        close = closes[-1]
        ma_s, ma_m, ma_l = sma(closes, cfg.w_ma_short), sma(closes, cfg.w_ma_mid), sma(closes, cfg.w_ma_long)
        slope = ma_m - sma(closes[:-cfg.w_slope_weeks], cfg.w_ma_mid)
        recent, before = weeks[-4:], weeks[-8:-4]
        detail = {
            "close": close, "ma_short": ma_s, "ma_mid": ma_m, "ma_long": ma_l, "mid_slope": slope,
            "higher_highs_lows": (max(w["high"] for w in recent) > max(w["high"] for w in before)
                                  and min(w["low"] for w in recent) > min(w["low"] for w in before)),
            "volume_ratio": weeks[-1]["volume"] / max(avg_volume_dicts(weeks[-11:-1]), 1.0),
            "from_26w_high_pct": (close / max(w["high"] for w in weeks[-26:]) - 1.0) * 100.0,
        }
        if close > ma_m and ma_s > ma_m > ma_l and slope > 0:
            return View(BULLISH, "주봉 정배열 + 중기 이평 상승", detail)
        if close < ma_m and ma_s < ma_m and slope < 0:
            return View(BEARISH, "주봉 종가·단기 이평이 중기 이평 아래 + 중기 이평 하락", detail)
        return View(NEUTRAL, "주봉 방향 불분명", detail)

    # ================================================================ 2. 일봉
    def analyze_daily(self, daily: List[dict], price: float) -> View:
        cfg = self.settings
        need = cfg.d_ma_long + cfg.d_slope_days
        if len(daily) < need:
            return View(UNKNOWN, f"완성된 일봉 {len(daily)}개 (필요 {need}개)", {})
        closes = [d["close"] for d in daily]
        ma_s, ma_m, ma_l = sma(closes, cfg.d_ma_short), sma(closes, cfg.d_ma_mid), sma(closes, cfg.d_ma_long)
        slope = ma_m - sma(closes[:-cfg.d_slope_days], cfg.d_ma_mid)
        prev_close = closes[-1]
        ext = (price / ma_m - 1.0) * 100.0
        run = (price / closes[-cfg.d_run_days] - 1.0) * 100.0
        day_change = (price / prev_close - 1.0) * 100.0
        recent_high = max(d["high"] for d in daily[-cfg.d_high_days:])
        pb_low = min(d["low"] for d in daily[-cfg.d_pb_days:])
        detail = {"ma_short": ma_s, "ma_mid": ma_m, "ma_long": ma_l, "mid_slope": slope, "ext_pct": ext,
                  "run_pct": run, "day_change_pct": day_change, "recent_high": recent_high}
        if price < ma_m and slope < 0:
            return View(DOWNTREND, "20일선 아래 + 20일선 하락", detail)
        if ext > cfg.d_max_ext_pct or run > cfg.d_max_run_pct or day_change > cfg.d_max_day_change_pct:
            return View(OVEREXTENDED, f"과열 (20일선 이격 {ext:+.1f}%, {cfg.d_run_days}일 {run:+.1f}%, 당일 {day_change:+.1f}%)",
                        detail)
        if price >= recent_high * (1.0 - cfg.d_near_high_pct / 100.0) and price > ma_m and slope > 0:
            return View(BREAKOUT_SETUP, f"{cfg.d_high_days}일 고가 {recent_high:,.0f}원 근처", detail)
        if (slope > 0 and price > ma_l and pb_low <= ma_m * (1.0 + cfg.d_pb_touch_pct / 100.0)
                and price > ma_s and price > prev_close):
            return View(PULLBACK, "20일선 눌림 후 5일선·전일 종가 위로 회복", detail)
        if price > ma_m and ma_s > ma_m and slope > 0:
            return View(UPTREND, "20일선 위 + 5일선 > 20일선 + 20일선 상승", detail)
        return View(CONSOLIDATION, "뚜렷한 추세 없음", detail)

    # ================================================================ 3. 3분봉
    def analyze_3min(self, s: SymbolSession) -> View:
        cfg, bar = self.settings, s.last
        k = max(4, int(cfg.m_bars))
        if len(s.bars) < k * 2:
            return View("INVALID", f"3분봉 {len(s.bars)}개 (필요 {k * 2}개)", {})
        recent = s.bars[-k:]
        first, second = recent[:k // 2], recent[k // 2:]
        high, low = s.day_high, s.day_low
        range_pos = (bar.close - low) / (high - low) if high > low else 0.0
        from_high = (high - bar.close) / high * 100.0
        up = sum(b.volume for b in recent if b.close > b.open)
        down = sum(b.volume for b in recent if b.close < b.open)
        detail = {"vwap": s.vwap, "range_pos": range_pos, "from_high_pct": from_high,
                  "up_down_volume": up / down if down else None,
                  "late_volume_ratio": avg_volume(recent) / max(avg_volume(s.bars), 1.0)}
        fail = ""
        if bar.close <= s.vwap:
            fail = "VWAP 아래"
        elif range_pos < cfg.m_min_range_pos:
            fail = f"당일 범위의 {range_pos * 100:.0f}% 위치 (상단부 아님)"
        elif from_high > cfg.m_max_from_high_pct:
            fail = f"당일 고가에서 -{from_high:.1f}%"
        elif bar.close < max(b.high for b in recent) * (1.0 - cfg.m_max_late_drop_pct / 100.0):
            fail = "막판 하락"
        elif min(b.low for b in second) < min(b.low for b in first):
            fail = "막판 저점 하락"
        elif up < down * cfg.m_min_updown_ratio:
            fail = "막판 매도 우위"
        elif detail["late_volume_ratio"] < cfg.m_vol_ratio:
            fail = "막판 거래량 급감"
        return View("INVALID" if fail else "VALID", fail or "막판 가격·거래량 안정", detail)

    # ================================================================ 판정: 주봉 → 일봉 → 3분봉 → 위험
    def on_bar(self, session: SymbolSession, ctx: Optional[MarketContext] = None) -> Optional[StrategySignal]:
        cfg, bar = self.settings, session.last
        daily = session.daily or []
        if not daily:
            return None
        today = session.date_str.replace("-", "")
        memo = session.scratch(self.name)
        if "weekly" not in memo:                              # 주봉은 장중에 바뀌지 않는다 → 종목당 하루 한 번
            memo["weekly"] = self.analyze_weekly(daily, today)
        weekly: View = memo["weekly"]
        reason = ""
        if weekly.label in (UNKNOWN, BEARISH) or (weekly.label == NEUTRAL and not cfg.allow_neutral_weekly):
            reason = f"주봉 {weekly.label} ({weekly.reason})"
        dview = None
        if not reason:
            dview = self.analyze_daily(daily, bar.close)
            ok = (UPTREND, PULLBACK, BREAKOUT_SETUP) + ((CONSOLIDATION,) if cfg.allow_consolidation else ())
            if dview.label not in ok:
                reason = f"일봉 {dview.label} ({dview.reason})"
        mview = None
        if not reason:
            mview = self.analyze_3min(session)
            if mview.label != "VALID":
                reason = f"3분봉 {mview.reason}"
        stop = None
        if not reason:
            if session.gaps > cfg.max_gaps:
                reason = f"거래 없는 3분 구간 {session.gaps}개 (유동성 부족)"
            elif session.cum_value < cfg.min_cum_value:
                reason = f"누적 거래대금 {session.cum_value / 1e8:,.0f}억 < {cfg.min_cum_value / 1e8:,.0f}억"
            else:
                reason = index_block(dview.detail["day_change_pct"], ctx, cfg.min_index_change_pct, -1e9)
        if not reason:
            stop_ref = min(b.low for b in session.bars[-max(1, int(cfg.stop_bars)):])
            resistance = {"close": daily[-1]["close"], "high": max(d["high"] for d in daily[-cfg.res_days:])}
            stop, reason = stop_and_room(bar.close, stop_ref, resistance, cfg.min_stop_pct, cfg.max_stop_pct,
                                         cfg.min_rr, cfg.upper_limit_pct)
        if reason:
            if memo.get("logged") != reason.split(" ")[0]:    # 같은 단계의 탈락은 종목당 한 번만 남긴다
                memo["logged"] = reason.split(" ")[0]
                logger.info("[%s %s] %s CLOSE 진입 안 함: %s", session.symbol, session.name, bar.time_str, reason)
            return None
        return self.make_signal(
            session, "swing",
            f"주봉 {weekly.label} · 일봉 {dview.label}({dview.reason}) · 3분봉 {mview.reason} | "
            f"손절 {stop:,.0f}원(-{(bar.close - stop) / bar.close * 100:.1f}%), 익일 {cfg.exit_time} 까지 보유",
            stop_price=float(stop), hold_overnight=True, position_size_ratio=cfg.size_ratio,
            confidence=confirmation_ratio(session, ctx, (weekly.detail["higher_highs_lows"],
                                                         dview.label in (PULLBACK, BREAKOUT_SETUP))),
            metadata={"weekly": weekly.label, "daily": dview.label, "ext_pct": round(dview.detail["ext_pct"], 2),
                      "range_pos": round(mview.detail["range_pos"], 2), "index_change_pct": index_change(ctx)})

    # ================================================================ 청산: 공통 규칙 + 익일 지정 시각
    def check_exit(self, position: Position, price: float, common: StrategyConfig, now: datetime,
                   trade: Any = None, **kw) -> Optional[ExitDecision]:
        decision = super().check_exit(position, price, common, now, trade=trade, **kw)
        if decision is None and getattr(trade, "carried_over", False) and now.time() >= self.exit_time:
            return ExitDecision(SignalType.SELL_TIME_CUT, price,
                                f"익일 지정 시각 청산 ({self.settings.exit_time}, 현재가 {price:,.0f})")
        return decision


def avg_volume_dicts(rows: List[dict]) -> float:
    return sum(r["volume"] for r in rows) / len(rows) if rows else 0.0
