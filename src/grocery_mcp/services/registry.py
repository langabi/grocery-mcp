from __future__ import annotations

from grocery_mcp.domain.models import Retailer
from grocery_mcp.retailers.base import RetailerAdapter


class AdapterRegistry:
    def __init__(self, adapters: dict[Retailer, RetailerAdapter]) -> None:
        self._adapters = adapters

    def get(self, retailer: Retailer) -> RetailerAdapter:
        try:
            return self._adapters[retailer]
        except KeyError as exc:
            raise ValueError(f"retailer is not configured: {retailer.value}") from exc

    def items(self) -> list[tuple[Retailer, RetailerAdapter]]:
        return list(self._adapters.items())

    async def close(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close()
