"""
Korea3MinBreakoutTrader - Abstract Broker Base
브로커 인터페이스 추상 클래스
모의투자(PaperBroker)와 실계좌(KiwoomRestBroker)가 동일한 인터페이스를 구현하여 무결점 전환 지원.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from python_app.models.types import Order, OrderSide, OrderType, Position, Quote


class BrokerBase(ABC):
    @abstractmethod
    def connect(self) -> bool:
        """브로커 연결 또는 세션 초기화"""
        pass

    @abstractmethod
    def get_cash_balance(self) -> float:
        """주문 가능 예수금 조회"""
        pass

    @abstractmethod
    def get_positions(self) -> Dict[str, Position]:
        """현재 보유 포지션 목록 조회"""
        pass

    @abstractmethod
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
        """주문 전송"""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """미체결 주문 취소"""
        pass

    @abstractmethod
    def get_quote(self, symbol: str) -> Optional[Quote]:
        """현재 시세 조회"""
        pass
