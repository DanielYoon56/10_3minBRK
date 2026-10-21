"""
Korea3MinBreakoutTrader - Paper Trading Broker (모의매매 엔진)
실제 한국 주식 시장의 체결 메커니즘, 슬리피지, 수수료, 거래세를 충실히 반영한 모의투자 브로커.
"""

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from python_app.broker.base import BrokerBase
from python_app.models.types import (
    Order, OrderSide, OrderStatus, OrderType, Position, Quote, StrategyConfig, TradeRecord
)
from python_app.strategy.exit_rules import check_exit, stop_loss_price

logger = logging.getLogger("Korea3MinBreakoutTrader.PaperBroker")


class PaperBroker(BrokerBase):
    """
    모의매매 브로커 구현체
    - 국내 주식 거래세: 0.20% (매도시)
    - 증권사 수수료: 0.015% (매수/매도 시)
    - 슬리피지 시뮬레이션: 시장가 주문시 1틱 또는 0.05% 가산/차감
    """

    def __init__(
        self,
        initial_cash: float = 10_000_000.0,
        commission_rate: float = 0.00015, # 0.015%
        tax_rate: float = 0.0020,         # 0.20%
        slippage_rate: float = 0.0005,    # 0.05%
        config: Optional[StrategyConfig] = None,
        auto_exit: bool = True,
    ):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage_rate = slippage_rate
        self.config = config or StrategyConfig()
        self.auto_exit = auto_exit  # 시세 수신 시 손절/트레일링 자동 청산

        self.positions: Dict[str, Position] = {}
        self.orders: Dict[str, Order] = {}
        self.trades: List[TradeRecord] = []
        self.latest_quotes: Dict[str, Quote] = {}
        self.is_connected = True

    def connect(self) -> bool:
        self.is_connected = True
        logger.info(f"모의투자 브로커 준비 완료 (초기 예수금: {self.cash:,.0f}원)")
        return True

    def get_cash_balance(self) -> float:
        return self.cash

    def get_positions(self) -> Dict[str, Position]:
        return self.positions

    def update_config(self, config: StrategyConfig):
        self.config = config

    def update_quote(self, quote: Quote) -> Optional[Order]:
        """실시간 시세 수신 시 평가액 갱신 + 손절(-2%)/트레일링 스탑 체크 → 조건 충족 시 시장가 전량 청산"""
        self.latest_quotes[quote.symbol] = quote
        if quote.symbol not in self.positions:
            return None

        pos = self.positions[quote.symbol]
        pos.current_price = quote.current_price
        if not self.auto_exit:
            return None

        decision = check_exit(pos, quote.current_price, self.config)
        if decision is None:
            return None

        logger.info(f"[{pos.symbol} {pos.name}] 청산 트리거: {decision.reason}")
        return self.submit_order(
            pos.symbol, pos.name, OrderSide.SELL, pos.qty,
            price=quote.current_price, order_type=OrderType.MARKET, reason=decision.reason,
        )

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self.latest_quotes.get(symbol)

    def submit_order(
        self,
        symbol: str,
        name: str,
        side: OrderSide,
        qty: int,
        price: float = 0.0,
        order_type: OrderType = OrderType.MARKET,
        reason: str = ""
    ) -> Optional[Order]:
        if qty <= 0:
            logger.warning(f"[{symbol}] 주문 수량이 0 이하입니다: {qty}")
            return None

        # 현재가 참조
        current_quote = self.latest_quotes.get(symbol)
        reference_price = price if (order_type == OrderType.LIMIT and price > 0) else (
            current_quote.current_price if current_quote else price
        )

        if reference_price <= 0:
            logger.warning(f"[{symbol}] 유효한 주문 가격을 결정할 수 없습니다: {reference_price}")
            return None

        # 슬리피지 반영 체결가 산정
        if side == OrderSide.BUY:
            fill_price = reference_price * (1.0 + self.slippage_rate)
            # 호가단위 1원 단위 정수 절사
            fill_price = round(fill_price)
            order_cost = fill_price * qty
            fee = order_cost * self.commission_rate
            total_required = order_cost + fee

            if self.cash < total_required:
                logger.warning(
                    f"[{symbol}] 예수금 부족으로 매수 거절: "
                    f"필요금액 {total_required:,.0f}원 > 보유예수금 {self.cash:,.0f}원"
                )
                return None

            # 매수 체결 처리
            self.cash -= total_required
            order_id = f"ORD_{uuid.uuid4().hex[:8]}"
            now = datetime.now()

            order = Order(
                order_id=order_id,
                symbol=symbol,
                name=name,
                side=side,
                order_type=order_type,
                price=reference_price,
                qty=qty,
                status=OrderStatus.FILLED,
                created_at=now,
                filled_at=now,
                filled_price=fill_price,
                filled_qty=qty,
                commission=fee,
                tax=0.0,
                reason=reason,
            )
            self.orders[order_id] = order

            # 포지션 갱신 또는 신규 등록
            if symbol in self.positions:
                existing = self.positions[symbol]
                new_qty = existing.qty + qty
                new_avg = ((existing.qty * existing.avg_price) + (qty * fill_price)) / new_qty
                existing.qty = new_qty
                existing.avg_price = new_avg
                existing.current_price = fill_price
            else:
                self.positions[symbol] = Position(
                    symbol=symbol,
                    name=name,
                    qty=qty,
                    avg_price=fill_price,
                    current_price=fill_price,
                    stop_loss_price=stop_loss_price(fill_price, self.config),  # 기본 -2%
                    take_profit_price=(
                        fill_price * (1.0 + self.config.take_profit_rate / 100.0)
                        if self.config.take_profit_rate > 0 else 0.0
                    ),
                    entry_time=now,
                    highest_price=fill_price,
                )

            # 거래 기록
            trade = TradeRecord(
                trade_id=f"TRD_{uuid.uuid4().hex[:8]}",
                order_id=order_id,
                symbol=symbol,
                name=name,
                side=side,
                price=fill_price,
                qty=qty,
                commission=fee,
                tax=0.0,
                realized_pnl=0.0,
                executed_at=now,
                reason=reason,
            )
            self.trades.append(trade)

            logger.info(
                f"★ [모의매수 체결] {symbol} {name} {qty:,}주 @ {fill_price:,.0f}원 "
                f"(총액: {order_cost:,.0f}원, 수수료: {fee:,.0f}원, 잔여 예수금: {self.cash:,.0f}원)"
            )
            return order

        else:  # SELL
            if symbol not in self.positions or self.positions[symbol].qty < qty:
                logger.warning(f"[{symbol}] 매도 가능 잔고 부족 (보유: {self.positions.get(symbol, None)})")
                return None

            pos = self.positions[symbol]
            fill_price = reference_price * (1.0 - self.slippage_rate)
            fill_price = round(fill_price)

            gross_amount = fill_price * qty
            fee = gross_amount * self.commission_rate
            tax = gross_amount * self.tax_rate
            net_proceeds = gross_amount - fee - tax

            # 실현손익 계산
            cost_basis = pos.avg_price * qty
            buy_fee = cost_basis * self.commission_rate
            realized_pnl = net_proceeds - (cost_basis + buy_fee)

            self.cash += net_proceeds
            order_id = f"ORD_{uuid.uuid4().hex[:8]}"
            now = datetime.now()

            order = Order(
                order_id=order_id,
                symbol=symbol,
                name=name,
                side=side,
                order_type=order_type,
                price=reference_price,
                qty=qty,
                status=OrderStatus.FILLED,
                created_at=now,
                filled_at=now,
                filled_price=fill_price,
                filled_qty=qty,
                commission=fee,
                tax=tax,
                reason=reason,
            )
            self.orders[order_id] = order

            # 포지션 차감 또는 청산
            pos.qty -= qty
            if pos.qty <= 0:
                del self.positions[symbol]

            trade = TradeRecord(
                trade_id=f"TRD_{uuid.uuid4().hex[:8]}",
                order_id=order_id,
                symbol=symbol,
                name=name,
                side=side,
                price=fill_price,
                qty=qty,
                commission=fee,
                tax=tax,
                realized_pnl=realized_pnl,
                executed_at=now,
                reason=reason,
            )
            self.trades.append(trade)

            pnl_pct = (realized_pnl / cost_basis) * 100.0 if cost_basis > 0 else 0.0
            logger.info(
                f"★ [모의매도 체결] {symbol} {name} {qty:,}주 @ {fill_price:,.0f}원 "
                f"(실현손익: {realized_pnl:+,.0f}원 ({pnl_pct:+.2f}%), 잔여 예수금: {self.cash:,.0f}원)"
            )
            return order

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.orders and self.orders[order_id].status == OrderStatus.SUBMITTED:
            self.orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False
