"""
Korea3MinBreakoutTrader - SMC(Smart Money Concept) 매수 전략: CHoCH → 첫 FVG 중간값 지정가 진입

[용어 (3분봉, 당일 봉만 사용)]
  스윙 고점  : 좌우 smc_swing_len(기본 2)봉보다 고가가 높은 봉. 오른쪽 N봉이 완성돼야 확정된다.
  스윙 저점  : 좌우 N봉보다 저가가 낮은 봉.
  하락 구조  : 종가가 마지막 스윙 저점 아래로 마감 (BOS/CHoCH 하락)
  CHoCH(상승) : '하락 구조' 상태에서 종가가 마지막 스윙 고점 위로 마감 → 추세 전환
                (하락 구조가 한 번도 없었던 상승 돌파는 BOS 로 보고 진입하지 않는다)
  상승 FVG   : 연속 3봉에서 3번째 봉 저가 > 1번째 봉 고가 → 그 사이 가격 공백
                FVG 하단 = 1번째 봉 고가, 상단 = 3번째 봉 저가, 중간값 = (하단 + 상단) / 2

[진입]
  1. 상승 CHoCH 확정 (CHoCH 봉 포함 이후)
  2. 그 뒤 처음 완성되는 상승 FVG 하나만 사용 (3번째 봉이 CHoCH 봉이거나 그 이후)
     → 이 FVG 가 조건(손절폭)을 못 맞추면 이번 CHoCH 는 건너뛰고 다음 CHoCH 를 기다린다
  3. FVG 중간값(호가 단위로 내림)에 지정가 매수. smc_fvg_expiry_bars 봉 안에 체결 안 되면 취소
  4. 손절가
       swing : 마지막 하락 구조 전환 ~ CHoCH 봉 사이 최저가(보호 저점) - 1호가   (기본)
       fvg   : FVG 하단 - 1호가
  5. 손절폭이 진입가의 smc_min_risk_pct% 미만이거나 smc_max_risk_pct% 초과면 진입 안 함
  6. 신호 시각이 trade_start_time ~ 14:00(마감 고정) 안일 때만, 종목당 당일 1회

[청산] 손절가만 전략 값(위 4번)이고 트레일링·장 마감 일괄 청산 등 나머지는 공통 설정.
"""

import logging
from datetime import timedelta
from typing import Optional, Tuple

from python_app.models.types import Bar, OrderType, Signal, SignalType, StrategyConfig
from python_app.strategy.intraday import LATE_ENTRY_END_TIME, BarStrategy, DayState, parse_hhmm

logger = logging.getLogger("Korea3MinBreakoutTrader.Strategy")


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


class SmcChochFvg(BarStrategy):
    key = "smc_fvg"
    label = "F.SMC CHoCH→FVG"
    signal_type = SignalType.BUY_SMC_FVG

    def _apply_config(self):
        super()._apply_config()      # 진입 시작 = 공통 trade_start_time
        self.end_time = parse_hhmm(LATE_ENTRY_END_TIME)   # 마감 14:00 고정
        if self.config.smc_stop_mode not in ("swing", "fvg"):
            raise ValueError(f"smc_stop_mode는 'swing' 또는 'fvg'여야 합니다: {self.config.smc_stop_mode!r}")

    # ------------------------------------------------------------------ 구조 추적
    def _update_swings(self, st: DayState):
        n = len(st.bars) - 1
        L = max(1, int(self.config.smc_swing_len))
        k = n - L
        if k - L < 0:
            return
        bars = st.bars
        left, right = bars[k - L:k], bars[k + 1:n + 1]
        hk, lk = bars[k].high, bars[k].low
        if hk > max(b.high for b in left) and hk >= max(b.high for b in right):
            st.extra["swing_high"] = (k, hk)
        if lk < min(b.low for b in left) and lk <= min(b.low for b in right):
            st.extra["swing_low"] = (k, lk)

    def _update_structure(self, st: DayState, bar: Bar):
        n = len(st.bars) - 1
        x = st.extra
        sh: Optional[Tuple[int, float]] = x.get("swing_high")
        sl: Optional[Tuple[int, float]] = x.get("swing_low")
        if sh and bar.close > sh[1]:
            if x.get("trend") == "bear":
                # 보호 저점 = 마지막 하락 구조 전환 봉 ~ CHoCH 봉 사이 최저가 (CHoCH 를 만든 상승 레그의 시작)
                protect = min(st.bars[x["bear_idx"]:n + 1], key=lambda b: b.low)
                x["choch"] = {"idx": n, "level": sh[1], "protect_low": protect.low,
                              "protect_time": protect.time_str, "fvg_checked": False}
                logger.info(f"[{st.symbol} {st.name}] {bar.time_str} 상승 CHoCH: 종가 {bar.close:,.0f} > "
                            f"스윙 고점 {sh[1]:,.0f} (보호 저점 {protect.low:,.0f} @{protect.time_str})")
            x["trend"] = "bull"
            x["swing_high"] = None
        elif sl and bar.close < sl[1]:
            x["trend"] = "bear"
            x["bear_idx"] = n
            x["swing_low"] = None
            if x.get("choch") and not x["choch"]["fvg_checked"]:
                logger.info(f"[{st.symbol} {st.name}] {bar.time_str} FVG 전에 하락 구조로 전환 → CHoCH 무효")
            x["choch"] = None

    # ------------------------------------------------------------------ 판정
    def _evaluate(self, st: DayState, bar: Bar) -> Optional[Signal]:
        self._update_swings(st)
        self._update_structure(st, bar)
        ch = st.extra.get("choch")
        n = len(st.bars) - 1
        if not ch or ch["fvg_checked"] or n < 2 or n < ch["idx"]:
            return None
        b1, b3 = st.bars[n - 2], bar
        if not b3.low > b1.high:
            return None
        ch["fvg_checked"] = True              # CHoCH 이후 '첫' FVG 만 사용
        return self._make_signal(st, bar, ch, fvg_low=b1.high, fvg_high=b3.low)

    def _make_signal(self, st: DayState, bar: Bar, ch: dict, fvg_low: float, fvg_high: float) -> Optional[Signal]:
        c = self.config
        tag = f"[{st.symbol} {st.name}] {bar.time_str} FVG {fvg_low:,.0f}~{fvg_high:,.0f}"
        if not self._can_enter(st, bar):
            logger.info(f"{tag} → 진입 시간대 밖이거나 당일 이미 신호 → 생략")
            return None
        entry = floor_to_tick((fvg_low + fvg_high) / 2.0)
        base = ch["protect_low"] if c.smc_stop_mode == "swing" else fvg_low
        stop = base - tick_size(base)
        risk = entry - stop
        risk_pct = risk / entry * 100.0 if entry > 0 else 0.0
        if risk <= 0 or risk_pct < c.smc_min_risk_pct or risk_pct > c.smc_max_risk_pct:
            logger.info(f"{tag} → 손절폭 {risk_pct:.2f}% (허용 {c.smc_min_risk_pct:g}~{c.smc_max_risk_pct:g}%) 밖 → 생략")
            return None
        expire = bar.timestamp + timedelta(minutes=3 * max(1, int(c.smc_fvg_expiry_bars)))
        window_end = bar.timestamp.replace(hour=self.end_time.hour, minute=self.end_time.minute, second=0)
        expire = min(expire, max(window_end, bar.timestamp + timedelta(minutes=3)))
        stop_desc = "보호 저점" if c.smc_stop_mode == "swing" else "FVG 하단"
        first = st.first
        sig = Signal(
            symbol=bar.symbol, name=st.name, signal_type=self.signal_type, price=entry,
            timestamp=bar.timestamp, first_bar_close=first.close, first_bar_volume=first.volume,
            current_volume=bar.volume, volume_ratio=(bar.volume / first.volume) if first.volume else 0.0,
            reason=(f"[{self.label}] CHoCH(스윙 고점 {ch['level']:,.0f} 돌파) 후 첫 FVG {fvg_low:,.0f}~{fvg_high:,.0f} "
                    f"중간값 {entry:,.0f}원 지정가 | 손절 {stop:,.0f}원({stop_desc}, -{risk_pct:.2f}%) | "
                    f"{expire:%H:%M}까지 미체결 시 취소"),
            bar_index=bar.bar_index, breakout_level=ch["level"], stop_loss_price=stop, strategy=self.key,
            order_type=OrderType.LIMIT,
            expire_at=expire,
        )
        st.signal_fired = True
        logger.info(f"★ {tag} 매수 셋업 {sig.reason}")
        return sig
