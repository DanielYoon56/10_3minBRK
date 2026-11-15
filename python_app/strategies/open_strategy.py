"""
3WAYTRADING - WAY 1: OPEN 전략 (장 초반, 3분봉)

갭이 크다는 이유만으로 사지 않는다. 완성된 3분봉에서 가격과 거래량이 확인된 뒤에만 신호를 낸다.
세 진입 패턴은 서로 독립이며 각각 켜고 끌 수 있다 (같은 봉에 여럿이 맞으면 A → B → C 순).

[A. 시초 범위 돌파 — orb]
   - 시초 범위 = 첫 or_bars 개 3분봉의 고가~저가 (봉이 빠짐없이 있어야 한다)
   - 직전 봉 종가 ≤ 범위 고가 < 현재 봉 종가  (완성 봉 종가 기준 돌파)
   - 돌파 봉이 양봉이고 VWAP 위에서 마감
   - 돌파 봉 거래량 ≥ 범위 봉 평균 거래량 × orb_vol_ratio
   - 손절 기준 = 돌파 봉 저가

[B. 시초 눌림 — pullback]
   - 상승 구간: 당일 고점(현재 봉 이전)까지의 상승폭이 구간 저점 대비 pb_min_impulse_pct% 이상
   - 눌림: 고점 뒤 1봉 이상 되돌림, 되돌림 비율(고점-눌림 저가)/(상승폭)이 pb_min_retrace ~ pb_max_retrace
   - 매도 압력 감소: 눌림 봉 평균 거래량 ≤ 상승 구간 봉 평균 거래량 × pb_vol_dry_ratio
   - 지지: 눌림 저가 ≥ VWAP × (1 - pb_support_tol_pct%)
   - 회복: 현재 봉이 양봉이고 종가 > 직전 봉 고가
   - 손절 기준 = 눌림 저가

[C. 시초 모멘텀 지속 — momentum]
   - 시가 대비 상승률 ≥ mo_min_gain_pct%
   - 최근 mo_bars 봉: 저가가 내려가지 않고(저점 상승), 모두 그 시점 VWAP 위에서 마감
   - 최근 mo_bars 봉 평균 거래량 ≥ 그 이전 봉 평균 거래량 × mo_vol_ratio  (거래량 유지)
   - 현재 봉 종가가 당일 신고가 (이전 모든 봉의 고가 위)
   - 손절 기준 = 최근 mo_bars 봉의 최저가

[공통 진입 금지 — 패턴이 맞아도 아래 중 하나면 진입하지 않는다]
   첫 봉(09:00~09:03) 없음 · 전일 일봉 없음 · 갭이 gap_min_pct~gap_max_pct 밖 · 빠진 봉이 max_gaps 초과 ·
   누적 거래대금 < min_cum_value · 지수 등락률 < min_index_change_pct · 지수 대비 상대강도 < min_rel_strength_pct ·
   전일 종가 대비 상승률 > max_day_change_pct · VWAP 이격 > max_vwap_ext_pct ·
   손절폭 > max_stop_pct · 가까운 저항(전일 고가, 상한가)까지 여유 < 손절폭 × min_rr

손절가 = 손절 기준 - 1호가. 손절폭이 min_stop_pct% 보다 좁으면 min_stop_pct% 로 넓힌다 (호가 잡음에 걸리지 않게).
지수 정보가 없으면 지수 관련 조건은 판정하지 않는다 (없는 값을 지어내지 않는다).
모든 기본값은 검증 전 임시값이다.
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple, Optional

from python_app.strategies.base_strategy import BaseStrategy, MarketContext, StrategySettings, StrategySignal, SymbolSession
from python_app.strategies.common_rules import (confirmation_ratio, index_block, index_change, overheat_block,
                                                stop_and_room)
from python_app.strategies.indicators import avg_volume as _avg_volume

logger = logging.getLogger("3WAYTRADING.Open")


@dataclass
class OpenSettings(StrategySettings):
    start_time: str = "09:06"
    end_time: str = "09:45"
    use_orb: bool = True
    use_pullback: bool = True
    use_momentum: bool = True
    # 공통 진입 금지 조건
    gap_min_pct: float = -2.0
    gap_max_pct: float = 8.0
    max_gaps: int = 1
    min_cum_value: int = 3_000_000_000
    min_index_change_pct: float = -1.5
    min_rel_strength_pct: float = 0.0
    max_day_change_pct: float = 15.0
    max_vwap_ext_pct: float = 4.0
    min_stop_pct: float = 0.5
    max_stop_pct: float = 3.0
    min_rr: float = 1.5
    upper_limit_pct: float = 30.0         # 상한가 = 전일 종가 × (1 + 30%)
    # A. 시초 범위 돌파
    or_bars: int = 2
    orb_vol_ratio: float = 0.7
    # B. 시초 눌림
    pb_min_impulse_pct: float = 2.0
    pb_min_retrace: float = 0.2
    pb_max_retrace: float = 0.6
    pb_vol_dry_ratio: float = 0.8
    pb_support_tol_pct: float = 0.3
    # C. 시초 모멘텀 지속
    mo_bars: int = 3
    mo_min_gain_pct: float = 2.0
    mo_vol_ratio: float = 0.5


class Setup(NamedTuple):
    pattern: str
    stop_ref: float      # 손절 기준 가격 (이 가격 - 1호가 = 손절가)
    level: float         # 패턴의 기준선 (범위 고가, 눌림 저가, 직전 고가)
    reason: str


class OpenStrategy(BaseStrategy):
    name = "open"
    label = "OPEN"
    patterns = ("orb", "pullback", "momentum")
    pattern_labels = {"orb": "A. 시초 범위 돌파", "pullback": "B. 시초 눌림", "momentum": "C. 모멘텀 지속"}
    settings_class = OpenSettings
    needs_daily = True
    needs_index = True

    # ================================================================ 패턴 (서로 독립)
    def detect_orb(self, s: SymbolSession) -> Optional[Setup]:
        n = max(1, int(self.settings.or_bars))
        if len(s.bars) <= n:
            return None
        rng = s.bars[:n]
        if rng[0].bar_index != 0 or rng[-1].bar_index != n - 1:       # 범위 봉이 빠짐없이 있어야 한다
            return None
        or_high = max(b.high for b in rng)
        prev, bar = s.bars[-2], s.last
        if not (prev.close <= or_high < bar.close):
            return None
        if bar.close <= bar.open or bar.close <= s.vwap:
            return None
        need = _avg_volume(rng) * self.settings.orb_vol_ratio
        if bar.volume < need:
            return None
        return Setup("orb", bar.low, or_high,
                     f"종가 {bar.close:,.0f}원이 시초 범위({n}봉) 고가 {or_high:,.0f}원 돌파, "
                     f"거래량 {bar.volume:,} ≥ 기준 {need:,.0f}")

    def detect_pullback(self, s: SymbolSession) -> Optional[Setup]:
        cfg = self.settings
        if len(s.bars) < 3:
            return None
        before, bar = s.bars[:-1], s.last
        h = max(range(len(before)), key=lambda i: before[i].high)     # 현재 봉 이전의 당일 고점
        pull = s.bars[h + 1:-1]                                         # 고점 뒤 ~ 직전 봉 = 눌림 구간
        if not pull:
            return None
        lo = min(range(h + 1), key=lambda i: before[i].low)           # 고점까지의 구간 저점
        peak, leg_low = before[h].high, before[lo].low
        impulse = peak - leg_low
        if leg_low <= 0 or impulse / leg_low * 100.0 < cfg.pb_min_impulse_pct:
            return None
        pb_low = min(b.low for b in s.bars[h + 1:])
        retrace = (peak - pb_low) / impulse
        if not (cfg.pb_min_retrace <= retrace <= cfg.pb_max_retrace):
            return None
        if _avg_volume(pull) > _avg_volume(before[lo:h + 1]) * cfg.pb_vol_dry_ratio:
            return None
        if pb_low < s.vwap * (1.0 - cfg.pb_support_tol_pct / 100.0):
            return None
        if bar.close <= bar.open or bar.close <= s.bars[-2].high:
            return None
        return Setup("pullback", pb_low, pb_low,
                     f"고점 {peak:,.0f}원 뒤 {retrace * 100:.0f}% 눌림(저가 {pb_low:,.0f}원, VWAP {s.vwap:,.0f}원 지지) 후 "
                     f"직전 봉 고가 {s.bars[-2].high:,.0f}원 위 양봉 마감 {bar.close:,.0f}원")

    def detect_momentum(self, s: SymbolSession) -> Optional[Setup]:
        cfg = self.settings
        k = max(2, int(cfg.mo_bars))
        if len(s.bars) < k + 1:
            return None
        recent, earlier, bar = s.bars[-k:], s.bars[:-k], s.last
        gain = (bar.close / s.first.open - 1.0) * 100.0
        if gain < cfg.mo_min_gain_pct:
            return None
        prior_high = max(b.high for b in s.bars[:-1])
        if bar.close <= prior_high:
            return None
        if any(recent[i].low < recent[i - 1].low for i in range(1, k)):
            return None
        if any(b.close <= v for b, v in zip(recent, s.vwaps[-k:])):
            return None
        if _avg_volume(recent) < _avg_volume(earlier) * cfg.mo_vol_ratio:
            return None
        return Setup("momentum", min(b.low for b in recent), prior_high,
                     f"시가 대비 +{gain:.1f}%, 최근 {k}봉 저점 상승·VWAP 위·거래량 유지, "
                     f"종가 {bar.close:,.0f}원 당일 신고가(직전 고가 {prior_high:,.0f}원)")

    # ================================================================ 공통 진입 금지
    def blocked(self, s: SymbolSession, ctx: Optional[MarketContext]) -> str:
        """진입하면 안 되는 사유 (빈 문자열 = 통과)"""
        cfg = self.settings
        prev = s.prev_day
        if not s.has_opening_bar:
            return "첫 봉(09:00~09:03) 없음"
        if not prev:
            return "전일 일봉 없음" + (f" ({s.daily_error})" if s.daily_error else "")
        gap = s.gap_pct
        if not (cfg.gap_min_pct <= gap <= cfg.gap_max_pct):
            return f"시가 갭 {gap:+.1f}% 가 허용 범위({cfg.gap_min_pct:g}~{cfg.gap_max_pct:g}%) 밖"
        if s.gaps > cfg.max_gaps:
            return f"거래 없는 3분 구간 {s.gaps}개 (유동성 부족)"
        if s.cum_value < cfg.min_cum_value:
            return f"누적 거래대금 {s.cum_value / 1e8:,.0f}억 < {cfg.min_cum_value / 1e8:,.0f}억"
        day_change = (s.last.close / prev["close"] - 1.0) * 100.0
        return (index_block(day_change, ctx, cfg.min_index_change_pct, cfg.min_rel_strength_pct)
                or overheat_block(s, day_change, cfg.max_day_change_pct, cfg.max_vwap_ext_pct))

    def _risk(self, s: SymbolSession, setup: Setup):
        """(손절가, 사유). 사유가 있으면 진입 금지"""
        cfg = self.settings
        return stop_and_room(s.last.close, setup.stop_ref, s.prev_day, cfg.min_stop_pct, cfg.max_stop_pct,
                             cfg.min_rr, cfg.upper_limit_pct)

    # ================================================================ 판정
    def on_bar(self, session: SymbolSession, ctx: Optional[MarketContext] = None) -> Optional[StrategySignal]:
        cfg = self.settings
        detectors = ((cfg.use_orb, self.detect_orb), (cfg.use_pullback, self.detect_pullback),
                     (cfg.use_momentum, self.detect_momentum))
        setup, stop, common = None, None, None
        for on, detect in detectors:
            found = detect(session) if on else None
            if found is None:
                continue
            if common is None:
                common = self.blocked(session, ctx)        # 공통 금지 조건은 패턴과 무관 → 한 번만 판정
            stop, reason = (None, common) if common else self._risk(session, found)
            if not reason:
                setup = found
                break
            logger.info("[%s %s] %s OPEN·%s 조건은 맞았으나 진입 안 함: %s", session.symbol, session.name,
                        session.last.time_str, found.pattern, reason)
        if setup is None:
            return None
        bar, prev = session.last, session.prev_day
        return self.make_signal(
            session, setup.pattern,
            f"{setup.reason} | 손절 {stop:,.0f}원(-{(bar.close - stop) / bar.close * 100:.1f}%)",
            stop_price=float(stop),
            confidence=confirmation_ratio(session, ctx, (session.gap_pct >= 0,)),
            metadata={"level": setup.level, "gap_pct": round(session.gap_pct, 2), "vwap": round(session.vwap, 1),
                      "day_change_pct": round((bar.close / prev["close"] - 1.0) * 100.0, 2),
                      "cum_value": round(session.cum_value), "index_change_pct": index_change(ctx)})
