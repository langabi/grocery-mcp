from __future__ import annotations

from abc import ABC, abstractmethod

from grocery_mcp.domain.models import RetailerCart, RetailerHealth, RetailerProduct


class RetailerError(RuntimeError):
    """A safe, retailer-neutral integration error."""


class RetailerAuthenticationError(RetailerError):
    pass


class RetailerUnavailableError(RetailerError):
    pass


class RetailerProtocolError(RetailerError):
    pass


class RetailerWriteDisabledError(RetailerError):
    pass


class RetailerAdapter(ABC):
    @abstractmethod
    async def search_products(self, query: str, limit: int = 20) -> list[RetailerProduct]: ...

    @abstractmethod
    async def get_product(self, retailer_product_id: str) -> RetailerProduct: ...

    @abstractmethod
    async def get_cart(self) -> RetailerCart: ...

    @abstractmethod
    async def add_to_cart(self, retailer_product_id: str, quantity: int) -> RetailerCart: ...

    @abstractmethod
    async def remove_from_cart(self, retailer_product_id: str) -> RetailerCart: ...

    @abstractmethod
    async def set_quantity(self, retailer_product_id: str, quantity: int) -> RetailerCart: ...

    @abstractmethod
    async def health_check(self) -> RetailerHealth: ...

    async def close(self) -> None:
        return None
