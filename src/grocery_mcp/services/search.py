from __future__ import annotations

import asyncio

from grocery_mcp.domain.models import Retailer, RetailerProduct
from grocery_mcp.services.registry import AdapterRegistry


class SearchService:
    def __init__(self, adapters: AdapterRegistry) -> None:
        self._adapters = adapters

    async def search(
        self,
        query: str,
        retailer: Retailer | None = None,
        limit: int = 20,
    ) -> dict[Retailer, list[RetailerProduct]]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if retailer:
            return {retailer: await self._adapters.get(retailer).search_products(query, limit)}

        pairs = self._adapters.items()
        results = await asyncio.gather(
            *(adapter.search_products(query, limit) for _, adapter in pairs),
            return_exceptions=True,
        )
        output: dict[Retailer, list[RetailerProduct]] = {}
        for (retailer_key, _), result in zip(pairs, results, strict=True):
            if not isinstance(result, BaseException):
                output[retailer_key] = result
        if not output and results:
            raise RuntimeError("all retailer searches failed")
        return output
