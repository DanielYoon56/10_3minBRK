"""
Korea3MinBreakoutTrader - 전일 종가 기준 매집 종목 스캐너

매일 장 전(또는 앱 시작 시) '전일 매집' 종목을 골라 당일 감시종목으로 쓴다.

[키움 조건식 사용 시 — 권장]   감시종목 = 조건식 결과  AND  (① OR ②)
   - 후보 = 영웅문에서 만든 조건식(주가·시총 등 쌤의 필터)의 현재 결과
   - 주가·시총 필터는 조건식에 맡기고 앱에서는 적용하지 않음
   - 조건식 조회 실패 시 그날은 신규 매수 없음 (조건 없이 매수하지 않음)
[조건식 미사용 시]   감시종목 = 전 시장 (① OR ②) + 주가·시총 필터

① 거래량·캔들 매집 (ka10031 후보 → ka10081 일봉으로 판정)
   - 전일 거래량 ≥ 직전 20일 평균 거래량 × vol_surge_ratio (기본 2배)
   - 전일 양봉 (종가 > 시가)
   - 종가가 고가 근처: (고가 - 종가) / (고가 - 저가) ≤ upper_wick_max (기본 0.25 = 당일 범위 상위 25% 안에서 마감)
   - 전일 거래대금(종가×거래량) ≥ min_trade_value
   ※ 후보군 = 전일 거래량 상위 100 + 거래대금 상위 100 (전 시장)

② 수급 매집 (ka90009)
   - 전일 외국인 순매수 상위 net_buy_top 종목 이면서(AND) 기관 순매수 상위 net_buy_top 종목
   - 외국인·기관 순매수 금액 각각 ≥ min_net_buy_amount

공통 필터 (①·② 모두 적용, ka10001)
   - 주가 ≥ 1,000원 (min_price)
   - 시가총액 ≥ 3,000억원 (min_market_cap)
   - 조회 실패 종목은 보수적으로 제외

정리
   - ETF·ETN·스팩·우선주는 기본 제외 (3분봉 돌파 전략 대상 아님)
   - 둘 다 해당 → ① 거래량 배수 → ② 순매수 금액 순으로 정렬, 상위 max_watch 개만 감시
   - 결과는 screen_YYYYMMDD.json 에 저장 → 당일 재시작 시 다시 스캔하지 않음
   - after_close=True (20시 스캔): 오늘 봉을 매집 판단일로, 후보는 당일 상위(ka10030),
     결과는 다음 거래일 파일(screen_다음평일.json)로 저장 → 다음 날 아침 앱이 그대로 재사용
"""

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("Korea3MinBreakoutTrader.Screener")

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
class ScreenerConfig:
    enabled: bool = True
    vol_surge_ratio: float = 2.0         # ① 전일 거래량 ≥ 20일 평균 × N
    avg_days: int = 20
    upper_wick_max: float = 0.25         # ① (고가-종가)/(고가-저가) ≤ 0.25
    min_price: int = 1_000               # 공통: 주가 하한 (원)
    min_market_cap: int = 300_000_000_000  # 공통: 시가총액 하한 (원, 3,000억)
    min_trade_value: int = 3_000_000_000  # ① 전일 거래대금 하한 (원, 30억)
    net_buy_top: int = 20                # ② 외국인 AND 기관 순매수 상위 N
    min_net_buy_amount: int = 1_000_000_000  # ② 외국인·기관 각각의 순매수 금액 하한 (원, 10억)
    max_watch: int = 30                  # 최종 감시 종목 수 상한
    exclude_etf_spac_pref: bool = True
    condition_name: str = ""             # 키움 조건식 이름 ("" = 사용 안 함)
    max_candidates: int = 400            # 조건식 결과가 많을 때 일봉 조회 상한

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ScreenerConfig":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ScreenHit:
    symbol: str
    name: str
    reasons: List[str] = field(default_factory=list)   # ["①", "②외국인", "②기관"]
    vol_ratio: float = 0.0
    net_buy_amount: float = 0.0
    detail: str = ""


@dataclass
class ScreenResult:
    trade_date: str
    prev_date: str
    created_at: str
    hits: List[ScreenHit]
    candidates_checked: int = 0
    errors: List[str] = field(default_factory=list)

    def watchlist(self) -> List[tuple]:
        return [(h.symbol, h.name) for h in self.hits]

    def to_json(self) -> dict:
        return {**asdict(self), "hits": [asdict(h) for h in self.hits]}

    @classmethod
    def from_json(cls, d: dict) -> "ScreenResult":
        return cls(trade_date=d["trade_date"], prev_date=d.get("prev_date", ""), created_at=d.get("created_at", ""),
                   hits=[ScreenHit(**h) for h in d.get("hits", [])],
                   candidates_checked=d.get("candidates_checked", 0), errors=d.get("errors", []))


def next_weekday(day: datetime) -> str:
    d = day + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def _prev_weekday(day: datetime) -> str:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def evaluate_candle(bars: List[dict], prev_date: str, cfg: ScreenerConfig) -> Optional[ScreenHit]:
    """① 판정. bars: 날짜 오름차순 일봉. prev_date 봉 = 매집 판단일."""
    idx = next((i for i, b in enumerate(bars) if b["dt"] == prev_date), None)
    if idx is None or idx < cfg.avg_days:
        return None
    b = bars[idx]
    past = bars[idx - cfg.avg_days: idx]
    avg_vol = sum(x["volume"] for x in past) / len(past)
    if avg_vol <= 0 or b["close"] <= 0:
        return None
    ratio = b["volume"] / avg_vol
    rng = b["high"] - b["low"]
    wick = (b["high"] - b["close"]) / rng if rng > 0 else 0.0
    value = b["close"] * b["volume"]
    ok = (ratio >= cfg.vol_surge_ratio and b["close"] > b["open"] and wick <= cfg.upper_wick_max
          and value >= cfg.min_trade_value)
    if not ok:
        return None
    return ScreenHit(symbol="", name="", reasons=["①"], vol_ratio=round(ratio, 2),
                     detail=f"거래량 {ratio:.1f}배·양봉·고가대비 {wick * 100:.0f}% 아래 마감·대금 {value / 1e8:,.0f}억")


class AccumulationScreener:
    def __init__(self, broker, cfg: ScreenerConfig, state_dir: str = ".",
                 log: Optional[Callable[[str, str], None]] = None, condition_client_factory=None):
        self.broker = broker
        self.cfg = cfg
        self.condition_client_factory = condition_client_factory   # () -> KiwoomConditionClient (with 문 지원)
        self.state_dir = state_dir
        self.log = log or (lambda lv, m: getattr(logger, lv, logger.info)(m))

    def cache_path(self, trade_date: str) -> str:
        return os.path.join(self.state_dir, f"screen_{trade_date}.json")

    def load_cached(self, trade_date: str) -> Optional[ScreenResult]:
        p = self.cache_path(trade_date)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return ScreenResult.from_json(json.load(f))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save(self, res: ScreenResult):
        p = self.cache_path(res.trade_date)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res.to_json(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    def run(self, now: datetime, use_cache: bool = True, after_close: bool = False) -> ScreenResult:
        today = now.strftime("%Y%m%d")
        # after_close: 오늘이 매집 판단일, 결과는 다음 거래일 감시종목
        trade_date = next_weekday(now) if after_close else today
        if use_cache:
            cached = self.load_cached(trade_date)
            if cached and cached.hits:
                self.log("info", f"매집 스캔 결과 재사용 ({len(cached.hits)}종목, {cached.created_at})")
                return cached

        cfg = self.cfg
        errors: List[str] = []
        hits: Dict[str, ScreenHit] = {}
        names: Dict[str, str] = {}

        use_condition = bool(cfg.condition_name.strip())
        cands: List[dict] = []
        if use_condition:
            # --- 후보: 키움 조건식 결과
            if not self.condition_client_factory:
                errors.append("조건식 클라이언트가 없습니다")
            else:
                try:
                    with self.condition_client_factory() as cc:
                        cands = cc.search(cfg.condition_name.strip())
                    self.log("info", f"조건식 '{cfg.condition_name}' 결과 {len(cands)}종목")
                except Exception as e:
                    errors.append(f"조건식 '{cfg.condition_name}' 조회 실패: {e}")
            if not cands:
                res = ScreenResult(trade_date=trade_date, prev_date="", created_at=now.strftime("%H:%M:%S"),
                                   hits=[], candidates_checked=0, errors=errors)
                for e in errors[:3]:
                    self.log("warning", f"매집 스캔 경고: {e}")
                if not errors:
                    self.log("info", f"조건식 '{cfg.condition_name}' 해당 종목 없음")
                return res
            if len(cands) > cfg.max_candidates:
                self.log("warning", f"조건식 결과 {len(cands)}종목 → 상위 {cfg.max_candidates}개만 검사")
                cands = cands[: cfg.max_candidates]
        else:
            # --- 후보: 전일(after_close 면 당일) 거래량 상위 100 + 거래대금 상위 100
            for qry, label in (("1", "거래량"), ("2", "거래대금")):
                try:
                    if after_close:
                        cands.extend(self.broker.get_today_top(sort_tp="1" if qry == "1" else "3"))
                    else:
                        cands.extend(self.broker.get_prev_day_top(qry_tp=qry))
                except Exception as e:
                    errors.append(f"{'당일' if after_close else '전일'} {label} 상위 조회 실패: {e}")
        seen = set()
        uniq = []
        for c in cands:
            if c["symbol"] in seen:
                continue
            seen.add(c["symbol"])
            names[c["symbol"]] = c["name"]
            if cfg.exclude_etf_spac_pref and is_excluded_name(c["name"]):
                continue
            uniq.append(c)

        # --- 전일(매집 판단일) 결정: 장 시작 후 실행이면 오늘 봉이 있으므로 '오늘 이전 마지막 거래일'
        prev_date = ""
        hit_dates: Dict[str, str] = {}
        # --- ① 일봉 판정
        for c in uniq:
            try:
                bars = self.broker.get_daily_bars(c["symbol"], today)
            except Exception as e:
                errors.append(f"{c['name']} 일봉 조회 실패: {e}")
                continue
            past = [b for b in bars if b["dt"] < trade_date]
            if not past:
                continue
            if not prev_date or past[-1]["dt"] > prev_date:
                prev_date = past[-1]["dt"]
            hit = evaluate_candle(past, past[-1]["dt"], cfg)
            if hit:
                hit.symbol, hit.name = c["symbol"], c["name"]
                hits[c["symbol"]] = hit
                hit_dates[c["symbol"]] = past[-1]["dt"]
        prev_date = prev_date or (today if after_close else _prev_weekday(now))
        # 거래정지 등으로 마지막 봉이 전일이 아닌 종목은 제외
        for sym in [s_ for s_, d in hit_dates.items() if d != prev_date]:
            hits.pop(sym, None)

        # --- ② 외국인·기관 순매수 상위
        try:
            fi = self.broker.get_foreign_inst_top(prev_date)
        except Exception as e:
            fi = {"foreign": [], "inst": []}
            errors.append(f"외국인·기관 매매 상위 조회 실패: {e}")
        cand_set = {c["symbol"] for c in uniq}
        qualified: Dict[str, Dict[str, dict]] = {}
        for key in ("foreign", "inst"):
            qualified[key] = {}
            for row in fi.get(key, [])[: cfg.net_buy_top]:
                if row["amount"] < cfg.min_net_buy_amount:
                    continue
                if use_condition and row["symbol"] not in cand_set:
                    continue          # 조건식 AND: 조건식 결과에 없는 종목은 제외
                if cfg.exclude_etf_spac_pref and is_excluded_name(row["name"]):
                    continue
                qualified[key][row["symbol"]] = row
        # 외국인 AND 기관: 두 순위에 모두 든 종목만
        for sym in [s_ for s_ in qualified["foreign"] if s_ in qualified["inst"]]:
            h = hits.get(sym)
            if not h:
                h = ScreenHit(symbol=sym, name=qualified["foreign"][sym]["name"])
                hits[sym] = h
            for key, label in (("foreign", "외국인"), ("inst", "기관")):
                amt = qualified[key][sym]["amount"]
                h.reasons.append(f"②{label}")
                h.net_buy_amount += amt
                h.detail = (h.detail + " · " if h.detail else "") + f"{label} 순매수 {amt / 1e8:,.0f}억"

        # --- 공통 필터: 주가 ≥ min_price, 시가총액 ≥ min_market_cap (조건식 사용 시 생략 — 조건식에서 처리)
        dropped = 0
        common = not use_condition and (cfg.min_price > 0 or cfg.min_market_cap > 0)
        for sym in (list(hits) if common else []):
            try:
                info = self.broker.get_stock_basic(sym)
            except Exception as e:
                errors.append(f"{hits[sym].name} 기본정보 조회 실패 → 제외: {e}")
                hits.pop(sym)
                continue
            h = hits[sym]
            if info["price"] < cfg.min_price or info["market_cap"] < cfg.min_market_cap:
                hits.pop(sym)
                dropped += 1
                continue
            h.detail += f" · {info['price']:,.0f}원 · 시총 {info['market_cap'] / 1e8:,.0f}억"
        if dropped:
            self.log("info", f"주가·시가총액 조건 미달 {dropped}종목 제외 "
                             f"(주가 ≥ {cfg.min_price:,}원, 시총 ≥ {cfg.min_market_cap / 1e8:,.0f}억)")

        # 조건식 결과에 종목명이 없으면 이름만 보충
        for h in hits.values():
            if not h.name or h.name == h.symbol:
                try:
                    h.name = self.broker.get_stock_basic(h.symbol)["name"] or h.symbol
                except Exception:
                    pass

        ordered = sorted(hits.values(),
                         key=lambda h: (-(("①" in h.reasons) + any(r.startswith("②") for r in h.reasons)),
                                        -h.vol_ratio, -h.net_buy_amount))[: cfg.max_watch]
        res = ScreenResult(trade_date=trade_date, prev_date=prev_date, created_at=now.strftime("%H:%M:%S"),
                           hits=ordered, candidates_checked=len(uniq), errors=errors)
        n1 = sum("①" in h.reasons for h in ordered)
        n2 = sum(any(r.startswith("②") for r in h.reasons) for h in ordered)
        src = f"조건식 '{cfg.condition_name}'" if use_condition else "전 시장"
        self.log("info", f"매집 스캔 완료 (기준일 {prev_date}, {src}): 후보 {len(uniq)} → 선정 {len(ordered)}종목 "
                         f"[①거래량·캔들 {n1} / ②수급 {n2}]")
        for e in errors[:5]:
            self.log("warning", f"매집 스캔 경고: {e}")
        if ordered:
            self._save(res)
        return res
