"""
Korea3MinBreakoutTrader - Broker Test Suite
Pytest tests for PaperBroker order execution, position math, and stop-loss logic.
"""

import pytest
from python_app.broker.paper import PaperBroker
from python_app.models.types import OrderSide, OrderType, Quote
from datetime import datetime


def test_paper_broker_buy_and_cash_deduction():
    broker = PaperBroker(initial_cash=10_000_000.0, commission_rate=0.00015, slippage_rate=0.0005)
    
    quote = Quote(
        symbol="005930",
        name="삼성전자",
        current_price=70000,
        open_price=70000,
        high_price=70500,
        low_price=69800,
        prev_close=69500,
        volume=500000,
        change_rate=0.72,
        timestamp=datetime.now()
    )
    broker.update_quote(quote)

    order = broker.submit_order(
        symbol="005930",
        name="삼성전자",
        side=OrderSide.BUY,
        qty=10,
        price=70000,
        order_type=OrderType.MARKET,
        reason="돌파 매수"
    )

    assert order is not None
    assert "005930" in broker.positions
    pos = broker.positions["005930"]
    assert pos.qty == 10
    assert broker.cash < 10_000_000.0


def test_paper_broker_sell_realizes_pnl():
    broker = PaperBroker(initial_cash=10_000_000.0, commission_rate=0.00015, tax_rate=0.0020, slippage_rate=0.0)
    
    # 1. Buy 10 shares @ 70,000
    broker.submit_order("005930", "삼성전자", OrderSide.BUY, 10, price=70000, order_type=OrderType.LIMIT)
    assert broker.positions["005930"].qty == 10

    # 2. Sell 10 shares @ 72,100 (+3.0%)
    quote = Quote(
        symbol="005930",
        name="삼성전자",
        current_price=72100,
        open_price=70000,
        high_price=72500,
        low_price=69800,
        prev_close=69500,
        volume=600000,
        change_rate=3.74,
        timestamp=datetime.now()
    )
    broker.update_quote(quote)
    sell_order = broker.submit_order("005930", "삼성전자", OrderSide.SELL, 10, price=72100, order_type=OrderType.LIMIT)
    assert sell_order is not None
    assert "005930" not in broker.positions  # Fully closed

    trade = broker.trades[-1]
    assert trade.realized_pnl > 0  # Profit realized


def _quote(price):
    from python_app.models.types import Quote
    return Quote(symbol="005930", name="삼성전자", current_price=price, open_price=10000, high_price=price,
                 low_price=price, prev_close=10000, volume=1, change_rate=0.0, timestamp=datetime.now())


def test_auto_exit_stop_loss_2pct():
    broker = PaperBroker(initial_cash=10_000_000.0, slippage_rate=0.0)
    broker.update_quote(_quote(10000))
    broker.submit_order("005930", "삼성전자", OrderSide.BUY, 10, price=10000)
    assert broker.positions["005930"].stop_loss_price == 9800
    assert broker.update_quote(_quote(9850)) is None
    sell = broker.update_quote(_quote(9800))
    assert sell is not None and "손절" in sell.reason
    assert "005930" not in broker.positions


def test_auto_exit_trailing_stop():
    broker = PaperBroker(initial_cash=10_000_000.0, slippage_rate=0.0)
    broker.update_quote(_quote(10000))
    broker.submit_order("005930", "삼성전자", OrderSide.BUY, 10, price=10000)
    broker.update_quote(_quote(11000))                    # 최고가 11,000
    assert broker.update_quote(_quote(10750)) is None     # 25% 되돌림 → 유지
    sell = broker.update_quote(_quote(10700))             # 1R 이상 구간 30% 되돌림 → 청산
    assert sell is not None and "트레일링" in sell.reason
    assert broker.trades[-1].realized_pnl > 0
