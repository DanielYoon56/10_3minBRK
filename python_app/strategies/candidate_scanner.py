"""
3WAYTRADING - 실시간 후보 탐색기

당일 시세만으로 후보를 찾는다 (전일 선정 종목에 의존하지 않는다).
  원천   : 당일 거래대금 상위 · 거래량 상위 (ka10030), 등락률 상위 (ka10027) — 주기적으로 다시 조회
  제외   : ETF·ETN·스팩·우선주, 주가·거래대금·등락률 범위 밖
  순위   : 등락률·거래대금·거래량의 후보군 내 백분위 + 가격 구조(VWAP 위, 당일 고가 근처) - 과열 감점
  교체   : 조회할 때마다 새 후보를 넣고 조건을 벗어난 종목을 뺀다

순위는 매수 우선순위에만 쓰인다. 여기서는 매수 신호를 만들지 않는다.
"""

import logging
import re
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("3WAYTRADING.Scanner")

_ETF_PATTERN = re.compile(
    r"(KODEX|TIGER|KBSTAR|RISE|ACE|ARIRANG|HANARO|KOSEF|SOL |PLUS |TIMEFOLIO|KIWOOM|1Q |WON |BNK |"
    r"ETN|ETF|스팩|리츠|인버스|레버리지|선물)", re.IGNORECASE)


def is_excluded_name(name: str) -> bool:
    n = (name or "").strip()
    if _ETF_PATTERN.search(n):
        return True
    # 우선주: '삼성전자우', '현대차2우B', '...우(전환)'
    return bool(re.search(r"\d?우[A-Z]?(\(.*\))?$", n))


@dataclass
class ScannerConfig:
    """기본값은 검증 전 임시값이다."""
    enabled: bool = True
    max_candidates: int = 40              # 3분마다 봉을 조회할 종목 수 (호출 한도 때문에 100 미만 권장)
    refresh_every_bars: int = 1           # 몇 봉마다 순위를 다시 조회할지
    min_price: int = 1_000
    max_price: int = 0                    # 0 = 상한 없음
    min_trade_value: int = 5_000_000_000  # 당일 누적 거래대금 하한 (원)
    min_change_rate: float = 0.0          # 등락률 하한 (%)
    max_change_rate: float = 25.0         # 등락률 상한 (%) — 상한가 근처 제외
    exclude_etf_spac_pref: bool = True
    use_value_rank: bool = True
    use_volume_rank: bool = True
    use_change_rank: bool = True
    w_change: float = 1.0
    w_value: float = 1.0
    w_volume: float = 0.5
    w_structure: float = 0.5              # VWAP 위 + 당일 고가 근처
    overheat_rate: float = 15.0           # 이 등락률부터 과열 감점
    w_overheat: float = 1.0
    stale_after_sec: int = 600            # 이 시간 동안 조회에 성공하지 못하면 후보 목록을 믿지 않는다

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ScannerConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Candidate:
    symbol: str
    name: str
    price: float = 0.0
    change_rate: float = 0.0
    volume: int = 0
    trade_value: float = 0.0              # 원
    sources: List[str] = field(default_factory=list)   # value / volume / change
    score: float = 0.0
    rank: int = 0                         # 1 = 최우선
    first_seen: str = ""                  # HH:MM
    last_seen: str = ""


def passes_filter(c: Candidate, cfg: ScannerConfig) -> bool:
    """후보 제외 규칙 (실거래 탐색기와 백테스트 재생이 같이 쓴다)"""
    if cfg.exclude_etf_spac_pref and is_excluded_name(c.name):
        return False
    if c.price < cfg.min_price or (cfg.max_price > 0 and c.price > cfg.max_price):
        return False
    if c.trade_value < cfg.min_trade_value:
        return False
    return cfg.min_change_rate <= c.change_rate <= cfg.max_change_rate


def _percentiles(values: List[float]) -> List[float]:
    """각 값의 후보군 내 백분위 (0~1, 같은 값은 같은 점수)"""
    n = len(values)
    if n <= 1:
        return [1.0] * n
    order = sorted(values)
    return [order.index(v) / (n - 1) for v in values]


def rank_candidates(pool: List[Candidate], cfg: ScannerConfig, sessions: Optional[dict] = None) -> List[Candidate]:
    """점수 계산 후 높은 순으로 정렬해 rank 를 매긴다 (우선순위 전용)"""
    if not pool:
        return []
    p_change = _percentiles([c.change_rate for c in pool])
    p_value = _percentiles([c.trade_value for c in pool])
    p_volume = _percentiles([float(c.volume) for c in pool])
    span = max(cfg.max_change_rate - cfg.overheat_rate, 1e-9)
    for i, c in enumerate(pool):
        score = cfg.w_change * p_change[i] + cfg.w_value * p_value[i] + cfg.w_volume * p_volume[i]
        s = (sessions or {}).get(c.symbol)
        if s is not None and s.bars:
            last, high = s.bars[-1].close, s.day_high
            above_vwap = 1.0 if last > s.vwap else 0.0
            near_high = 1.0 - min((high - last) / high * 100.0 / 5.0, 1.0) if high > 0 else 0.0   # 고가 대비 -5% 이상이면 0
            score += cfg.w_structure * (above_vwap + near_high) / 2.0
        if c.change_rate > cfg.overheat_rate:
            score -= cfg.w_overheat * min((c.change_rate - cfg.overheat_rate) / span, 1.0)
        c.score = round(score, 4)
    ranked = sorted(pool, key=lambda c: (-c.score, -c.trade_value, c.symbol))
    for i, c in enumerate(ranked, start=1):
        c.rank = i
    return ranked


class CandidateScanner:
    def __init__(self, broker, cfg: Optional[ScannerConfig] = None,
                 log: Optional[Callable[[str, str], None]] = None):
        self.broker = broker
        self.cfg = cfg or ScannerConfig()
        self.log = log
        self.candidates: Dict[str, Candidate] = {}     # 순위순
        self.last_refresh: Optional[datetime] = None   # 마지막으로 조회에 성공한 시각
        self.last_error = ""
        self.refresh_count = 0

    # ------------------------------------------------------------------ 조회
    def _fetch(self) -> Tuple[Dict[str, Candidate], List[str]]:
        sources = []
        if self.cfg.use_value_rank:
            sources.append(("value", lambda: self.broker.get_today_rank("3")))
        if self.cfg.use_volume_rank:
            sources.append(("volume", lambda: self.broker.get_today_rank("1")))
        if self.cfg.use_change_rank:
            sources.append(("change", self.broker.get_change_rank))
        merged: Dict[str, Candidate] = {}
        errors: List[str] = []
        ok = 0
        for label, fn in sources:
            try:
                rows = fn()
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            ok += 1
            for r in rows:
                c = merged.get(r["symbol"])
                if c is None:
                    c = merged[r["symbol"]] = Candidate(symbol=r["symbol"], name=r["name"])
                c.sources.append(label)
                c.price = r.get("price") or c.price
                c.change_rate = r.get("change_rate", c.change_rate)
                c.volume = max(c.volume, int(r.get("volume") or 0))
                c.trade_value = max(c.trade_value, float(r.get("trade_value") or 0.0))
        if sources and not ok:
            raise RuntimeError(" / ".join(errors) or "순위 조회 실패")
        return merged, errors

    def refresh(self, now: datetime, sessions: Optional[dict] = None) -> Tuple[List[Candidate], List[Candidate]]:
        """순위를 다시 조회해 후보를 교체한다 → (새로 들어온 후보, 빠진 후보).
        조회가 전부 실패하면 기존 목록을 그대로 두고 last_error 만 남긴다."""
        try:
            merged, errors = self._fetch()
        except Exception as e:
            self.last_error = str(e)
            logger.warning("후보 조회 실패 → 기존 후보 %d종목 유지: %s", len(self.candidates), e)
            return [], []
        self.last_error = " / ".join(errors)
        pool = [c for c in merged.values() if passes_filter(c, self.cfg)]
        ranked = rank_candidates(pool, self.cfg, sessions)[:max(0, int(self.cfg.max_candidates))]
        hhmm = now.strftime("%H:%M")
        fresh: Dict[str, Candidate] = {}
        for c in ranked:
            old = self.candidates.get(c.symbol)
            c.first_seen = old.first_seen if old else hhmm
            c.last_seen = hhmm
            fresh[c.symbol] = c
        added = [c for s, c in fresh.items() if s not in self.candidates]
        removed = [c for s, c in self.candidates.items() if s not in fresh]
        self.candidates = fresh
        self.last_refresh = now
        self.refresh_count += 1
        return added, removed

    # ------------------------------------------------------------------ 조회 결과
    def watchlist(self) -> List[Tuple[str, str]]:
        return [(c.symbol, c.name) for c in self.candidates.values()]

    def get(self, symbol: str) -> Optional[Candidate]:
        return self.candidates.get(symbol)

    def rank_of(self, symbol: str) -> int:
        c = self.candidates.get(symbol)
        return c.rank if c else 10 ** 6

    def is_stale(self, now: datetime) -> bool:
        """후보 목록을 믿을 수 없는가 (한 번도 조회 못 했거나 너무 오래됨)"""
        if self.last_refresh is None:
            return True
        return (now - self.last_refresh).total_seconds() > self.cfg.stale_after_sec

    def status(self, now: Optional[datetime] = None) -> str:
        if self.last_refresh is None:
            return "후보 탐색 대기" + (f" (조회 실패: {self.last_error})" if self.last_error else "")
        text = f"후보 {len(self.candidates)}종목 ({self.last_refresh:%H:%M} 기준)"
        if now is not None and self.is_stale(now):
            text += " · 갱신 지연"
        return text
