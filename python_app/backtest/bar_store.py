"""
3WAYTRADING - 3분봉·후보 기록 저장소 (SQLite)

  bars       : 완성 3분봉 (종목·봉 마감 시각당 1행)
  candidates : 스캐너가 조회 시점마다 고른 후보와 순위
  daily      : 일봉 (백테스트용 — 전일 종가·고가, 종목 선정)
  symbols    : 종목 이름·시장
  progress   : 내려받기 진행 표시 (중단 후 이어받기)

실거래 기록은 bars.db, 백테스트용 과거 데이터는 history.db 에 따로 둔다 (backtest/download.py).

과거 3분봉은 키움에서 종목당 약 1년치를 다시 받을 수 있지만, 그날 스캐너가 어떤 종목을 골랐는지는
다시 만들 수 없다 → 후보 기록을 매일 남겨 백테스트의 종목 선택을 실제와 맞춘다.
"""

import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Tuple

from python_app.models.types import Bar

_TS = "%Y-%m-%d %H:%M"


class BarStore:
    def __init__(self, path: str = "bars.db"):
        self.path = path
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, ts TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (symbol, ts)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    ts TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT,
                    rank INTEGER NOT NULL, score REAL NOT NULL,
                    price REAL, change_rate REAL, volume INTEGER, trade_value REAL, sources TEXT,
                    PRIMARY KEY (ts, symbol)
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS bars_ts ON bars (ts)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily (
                    symbol TEXT NOT NULL, dt TEXT NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (symbol, dt)
                )""")
            conn.execute("CREATE TABLE IF NOT EXISTS symbols (symbol TEXT PRIMARY KEY, name TEXT, market TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY, done_at TEXT)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save_bars(self, bars: Iterable[Bar]) -> int:
        rows = [(b.symbol, b.timestamp.strftime(_TS), b.open, b.high, b.low, b.close, int(b.volume))
                for b in bars if b.is_completed]
        if rows:
            with self._conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def save_candidates(self, now: datetime, candidates: Iterable) -> int:
        ts = now.strftime(_TS)
        rows = [(ts, c.symbol, c.name, c.rank, c.score, c.price, c.change_rate, int(c.volume), c.trade_value,
                 "+".join(c.sources)) for c in candidates]
        if rows:
            with self._conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def load_bars(self, symbol: str, date_str: str) -> List[Bar]:
        """date_str = YYYY-MM-DD. 시간순"""
        with self._conn() as conn:
            cur = conn.execute("SELECT ts, open, high, low, close, volume FROM bars "
                               "WHERE symbol = ? AND ts LIKE ? ORDER BY ts", (symbol, date_str + "%"))
            return [Bar(symbol=symbol, timestamp=datetime.strptime(ts, _TS), open=o, high=h, low=l, close=c, volume=v)
                    for ts, o, h, l, c, v in cur.fetchall()]

    def symbols(self, date_str: str) -> List[str]:
        with self._conn() as conn:
            cur = conn.execute("SELECT DISTINCT symbol FROM bars WHERE ts LIKE ? ORDER BY symbol", (date_str + "%",))
            return [r[0] for r in cur.fetchall()]

    def dates(self) -> List[str]:
        with self._conn() as conn:
            cur = conn.execute("SELECT DISTINCT substr(ts, 1, 10) FROM bars ORDER BY 1")
            return [r[0] for r in cur.fetchall()]

    def load_day(self, date_str: str) -> Dict[str, List[Bar]]:
        """그날의 모든 종목 봉 (종목 → 시간순 봉)"""
        out: Dict[str, List[Bar]] = {}
        with self._conn() as conn:
            cur = conn.execute("SELECT symbol, ts, open, high, low, close, volume FROM bars "
                               "WHERE ts >= ? AND ts < ? ORDER BY symbol, ts", (date_str + " 00:00", date_str + " 99"))
            for sym, ts, o, h, l, c, v in cur.fetchall():
                out.setdefault(sym, []).append(
                    Bar(symbol=sym, timestamp=datetime.strptime(ts, _TS), open=o, high=h, low=l, close=c, volume=v))
        return out

    # ------------------------------------------------------------------ 일봉·종목·진행
    def save_daily(self, symbol: str, rows: Iterable[dict]) -> int:
        data = [(symbol, r["dt"], r["open"], r["high"], r["low"], r["close"], int(r["volume"])) for r in rows]
        if data:
            with self._conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def load_daily(self, symbol: str) -> List[dict]:
        """오래된 순 [{dt(YYYYMMDD), open, high, low, close, volume}]"""
        with self._conn() as conn:
            cur = conn.execute("SELECT dt, open, high, low, close, volume FROM daily WHERE symbol = ? ORDER BY dt",
                               (symbol,))
            return [{"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": v}
                    for dt, o, h, l, c, v in cur.fetchall()]

    def load_all_daily(self) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {}
        with self._conn() as conn:
            cur = conn.execute("SELECT symbol, dt, open, high, low, close, volume FROM daily ORDER BY symbol, dt")
            for sym, dt, o, h, l, c, v in cur.fetchall():
                out.setdefault(sym, []).append({"dt": dt, "open": o, "high": h, "low": l, "close": c, "volume": v})
        return out

    def save_symbols(self, rows: Iterable[Tuple[str, str, str]]):
        with self._conn() as conn:
            conn.executemany("INSERT OR REPLACE INTO symbols VALUES (?, ?, ?)", list(rows))

    def load_symbols(self) -> Dict[str, Tuple[str, str]]:
        with self._conn() as conn:
            return {s: (n, m) for s, n, m in conn.execute("SELECT symbol, name, market FROM symbols").fetchall()}

    def mark_done(self, key: str):
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO progress VALUES (?, ?)", (key, datetime.now().isoformat()))

    def done_keys(self, prefix: str) -> set:
        with self._conn() as conn:
            return {r[0] for r in conn.execute("SELECT key FROM progress WHERE key LIKE ?", (prefix + "%",)).fetchall()}
