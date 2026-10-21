"""
Korea3MinBreakoutTrader - SQLite Storage Module
로컬 데이터베이스 영구 저장소
- 주문 내역, 체결 기록, 매매 신호 이력 저장
- 당일 및 누적 손익 통계 조회
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import Dict, List, Optional
from python_app.models.types import Order, OrderSide, OrderStatus, OrderType, Signal, SignalType, TradeRecord

logger = logging.getLogger("Korea3MinBreakoutTrader.Database")


class DatabaseManager:
    def __init__(self, db_path: str = "korea_breakout_trader.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. 신호 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    signal_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    first_bar_close REAL,
                    first_bar_volume INTEGER,
                    current_volume INTEGER,
                    volume_ratio REAL,
                    reason TEXT,
                    bar_index INTEGER,
                    created_at TEXT NOT NULL
                )
            """)

            # 2. 주문 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    filled_price REAL DEFAULT 0,
                    filled_qty INTEGER DEFAULT 0,
                    commission REAL DEFAULT 0,
                    tax REAL DEFAULT 0,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    filled_at TEXT
                )
            """)

            # 3. 체결 및 완료 거래 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    commission REAL NOT NULL,
                    tax REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    reason TEXT,
                    executed_at TEXT NOT NULL
                )
            """)

            # 4. 설정 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
            logger.info("SQLite 데이터베이스 초기화 완료 (%s)", self.db_path)

    def save_signal(self, sig: Signal):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO signals (
                    symbol, name, signal_type, price, first_bar_close,
                    first_bar_volume, current_volume, volume_ratio, reason, bar_index, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sig.symbol, sig.name, sig.signal_type.value, sig.price,
                sig.first_bar_close, sig.first_bar_volume, sig.current_volume,
                sig.volume_ratio, sig.reason, sig.bar_index, sig.timestamp.isoformat()
            ))

    def save_order(self, order: Order):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders (
                    order_id, symbol, name, side, order_type, price, qty,
                    status, filled_price, filled_qty, commission, tax, reason, created_at, filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.order_id, order.symbol, order.name, order.side.value,
                order.order_type.value, order.price, order.qty, order.status.value,
                order.filled_price, order.filled_qty, order.commission, order.tax,
                order.reason, order.created_at.isoformat(),
                order.filled_at.isoformat() if order.filled_at else None
            ))

    def save_trade(self, trade: TradeRecord):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO trades (
                    trade_id, order_id, symbol, name, side, price, qty,
                    commission, tax, realized_pnl, reason, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.trade_id, trade.order_id, trade.symbol, trade.name,
                trade.side.value, trade.price, trade.qty, trade.commission,
                trade.tax, trade.realized_pnl, trade.reason, trade.executed_at.isoformat()
            ))

    def get_recent_signals(self, limit: int = 50) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def get_recent_trades(self, limit: int = 50) -> List[Dict]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]
