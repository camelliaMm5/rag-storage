from abc import ABC, abstractmethod

from domain.order.entity import Order, Logistics


class OrderRepository(ABC):
    @abstractmethod
    def find_order(self, order_id: str, user_id: str) -> Order | None:
        ...

    @abstractmethod
    def find_logistics(self, order_id: str, user_id: str) -> Logistics | None:
        ...

    @abstractmethod
    def create_order(self, product: str, amount: float, recipient: str,
                     address: str, user_id: str) -> Order:
        ...

    @abstractmethod
    def list_orders(self) -> list[Order]:
        ...

    @abstractmethod
    def list_orders_by_user(self, user_id: str) -> list[Order]:
        ...
