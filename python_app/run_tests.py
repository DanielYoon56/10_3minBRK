"""
Korea3MinBreakoutTrader - Standard Library Test Runner
"""
import sys
import unittest
from datetime import datetime

# Add root directory to sys.path
sys.path.insert(0, '.')

from python_app.models.types import Position, SignalType, StrategyConfig, OrderSide, OrderType, Quote
from python_app.strategy.exit_rules import check_exit
from python_app.broker.paper import PaperBroker


class TestExitRules(unittest.TestCase):
    def pos(self, entry):
        return Position(symbol="005930", name="삼성전자", qty=10, avg_price=entry, current_price=entry,
                        stop_loss_price=0.0, take_profit_price=0.0, entry_time=datetime.now())

    def test_stop_loss_2pct(self):
        cfg, p = StrategyConfig(), self.pos(10000)
        self.assertIsNone(check_exit(p, 9810, cfg))
        self.assertEqual(check_exit(p, 9800, cfg).signal_type, SignalType.SELL_STOP_LOSS)

    def test_trailing_30pct_of_gain_above_1r(self):
        cfg, p = StrategyConfig(), self.pos(10000)
        self.assertIsNone(check_exit(p, 11000, cfg))
        self.assertIsNone(check_exit(p, 10710, cfg))
        self.assertEqual(check_exit(p, 10700, cfg).signal_type, SignalType.SELL_TRAILING_STOP)


class TestPaperBroker(unittest.TestCase):
    def test_order_execution_and_fees(self):
        broker = PaperBroker(initial_cash=10000000.0, commission_rate=0.00015, tax_rate=0.0020, slippage_rate=0.0005)
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

        # 1. Buy 10 shares
        order = broker.submit_order("005930", "삼성전자", OrderSide.BUY, 10, price=70000, order_type=OrderType.MARKET)
        self.assertIsNotNone(order)
        self.assertIn("005930", broker.positions)
        self.assertEqual(broker.positions["005930"].qty, 10)
        self.assertLess(broker.cash, 10000000.0)

        # 2. Update quote to 72,000 and sell
        quote.current_price = 72000
        broker.update_quote(quote)
        sell_order = broker.submit_order("005930", "삼성전자", OrderSide.SELL, 10, price=72000, order_type=OrderType.MARKET)
        self.assertIsNotNone(sell_order)
        self.assertNotIn("005930", broker.positions)
        self.assertGreater(broker.trades[-1].realized_pnl, 0)


if __name__ == "__main__":
    unittest.main()
