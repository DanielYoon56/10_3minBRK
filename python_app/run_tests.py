"""
Korea3MinBreakoutTrader - Standard Library Test Runner
"""
import sys
import unittest
from datetime import datetime

# Add root directory to sys.path
sys.path.insert(0, '.')

from python_app.models.types import Bar, Position, SignalType, StrategyConfig, OrderSide, OrderType, Quote
from python_app.strategy.exit_rules import check_exit
from python_app.strategy.breakout import First3MinuteCandleBreakout
from python_app.broker.paper import PaperBroker


class TestStrategy(unittest.TestCase):
    def make_bar(self, date_str, time_str, o, h, l, c, v, is_comp=True):
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return Bar(
            symbol="005930",
            timestamp=dt,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            is_completed=is_comp
        )

    def first_bar(self):
        # 시가 70,000 → 종가 69,900, 거래량 100,000
        return self.make_bar("2026-10-14", "09:03", 70000, 70500, 69600, 69900, 100000)

    def engine(self, **kw):
        return First3MinuteCandleBreakout(StrategyConfig(trade_start_time="09:03", trade_end_time="10:00", **kw))

    def test_first_bar_baseline_is_open(self):
        engine = self.engine()
        self.assertIsNone(engine.process_bar(self.first_bar(), "삼성전자"))
        state = engine.get_or_create_state("005930")
        self.assertEqual(state.breakout_level, 70000)
        self.assertEqual(state.first_bar_volume, 100000)

    def test_open_breakout_with_volume(self):
        engine = self.engine()
        engine.process_bar(self.first_bar(), "삼성전자")
        sig = engine.process_bar(self.make_bar("2026-10-14", "09:06", 69900, 70400, 69900, 70300, 120000), "삼성전자")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, SignalType.BUY_BREAKOUT)
        self.assertEqual(sig.breakout_level, 70000)
        self.assertEqual(sig.stop_loss_price, round(70300 * 0.98))

    def test_insufficient_volume_rejection(self):
        engine = self.engine()
        engine.process_bar(self.first_bar(), "삼성전자")
        sig = engine.process_bar(self.make_bar("2026-10-14", "09:06", 69900, 70400, 69900, 70300, 70000), "삼성전자")  # 정확히 70% → 미달
        self.assertIsNone(sig)

    def test_volume_above_70pct(self):
        engine = self.engine()
        engine.process_bar(self.first_bar(), "삼성전자")
        sig = engine.process_bar(self.make_bar("2026-10-14", "09:06", 69900, 70400, 69900, 70300, 75000), "삼성전자")  # 75%
        self.assertIsNotNone(sig)

    def test_time_window_restriction(self):
        engine = self.engine()
        engine.process_bar(self.first_bar(), "삼성전자")
        sig = engine.process_bar(self.make_bar("2026-10-14", "10:06", 69900, 71500, 69900, 71200, 200000), "삼성전자")
        self.assertIsNone(sig)

    def test_duplicate_prevention(self):
        engine = self.engine(allow_reentry_same_day=False)
        engine.process_bar(self.first_bar(), "삼성전자")
        self.assertIsNotNone(engine.process_bar(self.make_bar("2026-10-14", "09:06", 69900, 70400, 69900, 70300, 120000), "삼성전자"))
        engine.process_bar(self.make_bar("2026-10-14", "09:09", 70300, 70300, 69700, 69800, 50000), "삼성전자")
        self.assertIsNone(engine.process_bar(self.make_bar("2026-10-14", "09:12", 69800, 70500, 69800, 70400, 150000), "삼성전자"))


class TestExitRules(unittest.TestCase):
    def pos(self, entry):
        return Position(symbol="005930", name="삼성전자", qty=10, avg_price=entry, current_price=entry,
                        stop_loss_price=0.0, take_profit_price=0.0, entry_time=datetime.now())

    def test_stop_loss_2pct(self):
        cfg, p = StrategyConfig(), self.pos(10000)
        self.assertIsNone(check_exit(p, 9810, cfg))
        self.assertEqual(check_exit(p, 9800, cfg).signal_type, SignalType.SELL_STOP_LOSS)

    def test_trailing_15pct_of_gain(self):
        cfg, p = StrategyConfig(), self.pos(10000)
        self.assertIsNone(check_exit(p, 11000, cfg))
        self.assertIsNone(check_exit(p, 10860, cfg))
        self.assertEqual(check_exit(p, 10850, cfg).signal_type, SignalType.SELL_TRAILING_STOP)


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
