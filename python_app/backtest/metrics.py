"""3WAYTRADING - 백테스트 성과 지표 (모든 금액은 수수료·세금·슬리피지 차감 후)"""

from typing import Callable, Dict, Iterable, List, Optional


def summarize(trades: Iterable) -> Dict[str, Optional[float]]:
    ts = sorted(trades, key=lambda t: (t.exit_time, t.entry_time, t.symbol))
    pnls = [t.pnl for t in ts]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    equity = peak = drawdown = 0.0
    streak = worst_streak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if p < 0 else 0
        worst_streak = max(worst_streak, streak)
    n = len(pnls)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100.0 if n else None,
        "avg_win": gross_profit / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,     # 손실이 없으면 정의되지 않는다
        "expectancy": sum(pnls) / n if n else None,                                 # 거래당 평균 순손익 (원)
        "expectancy_pct": sum(t.return_pct for t in ts) / n if n else None,         # 거래당 평균 순수익률 (%)
        "max_drawdown": drawdown,                                                   # 누적 순손익의 고점 대비 최대 낙폭 (원)
        "max_consecutive_losses": worst_streak,
        "avg_holding_min": sum(t.holding_minutes for t in ts) / n if n else None,
        "net_profit": sum(pnls),
    }


def _num(v, fmt: str) -> str:
    return "-" if v is None else format(v, fmt)


_COLUMNS = (("거래", "trades", ",d"), ("승률%", "win_rate", ".1f"), ("평균익", "avg_win", ",.0f"),
            ("평균손", "avg_loss", ",.0f"), ("최대익", "largest_win", ",.0f"), ("최대손", "largest_loss", ",.0f"),
            ("PF", "profit_factor", ".2f"), ("기대값", "expectancy", ",.0f"), ("기대%", "expectancy_pct", "+.3f"),
            ("MDD", "max_drawdown", ",.0f"), ("연속손", "max_consecutive_losses", "d"),
            ("보유분", "avg_holding_min", ".0f"), ("순손익", "net_profit", "+,.0f"))


def format_table(groups: Dict[str, Dict]) -> str:
    width = max([len(k) for k in groups] + [8])
    lines = [" " * width + " | " + " | ".join(f"{name:>9}" for name, _, _ in _COLUMNS)]
    for label, summary in groups.items():
        lines.append(f"{label:<{width}} | " + " | ".join(f"{_num(summary[key], fmt):>9}" for _, key, fmt in _COLUMNS))
    return "\n".join(lines)


def grouped(trades: List, key: Callable) -> Dict[str, Dict]:
    buckets: Dict[str, List] = {}
    for t in trades:
        buckets.setdefault(key(t), []).append(t)
    return {k: summarize(buckets[k]) for k in sorted(buckets)}


def exit_kind(trade) -> str:
    r = trade.exit_reason
    return ("손절" if r.startswith("손절") else "트레일링" if r.startswith("트레일링") else
            "목표가" if r.startswith("목표가") else "장 마감 청산" if "장 마감" in r else
            "익일 시각 청산" if r.startswith("익일") else "기타")


def report(trades: List, title: str = "") -> str:
    """전체 · 전략별 · 패턴별 · 청산 사유별 · 월별"""
    parts = [f"== {title} ==" if title else ""]
    if not trades:
        return "\n".join(parts + ["거래 없음"])
    parts.append(format_table({"전체": summarize(trades), **{f"[전략] {k}": v for k, v in grouped(trades, lambda t: t.strategy).items()}}))
    parts.append(format_table({f"[패턴] {k}": v for k, v in grouped(trades, lambda t: f"{t.strategy}.{t.pattern}").items()}))
    parts.append(format_table({f"[청산] {k}": v for k, v in grouped(trades, exit_kind).items()}))
    parts.append(format_table({f"[월] {k}": v for k, v in grouped(trades, lambda t: t.entry_time.strftime("%Y-%m")).items()}))
    return "\n\n".join(p for p in parts if p)
