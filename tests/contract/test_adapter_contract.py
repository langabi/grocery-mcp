from __future__ import annotations

import inspect

import pytest

from grocery_mcp.retailers.base import RetailerAdapter
from grocery_mcp.retailers.sixty60.adapter import Sixty60Adapter
from grocery_mcp.retailers.woolworths.adapter import WoolworthsAdapter


@pytest.mark.parametrize("adapter_type", [Sixty60Adapter, WoolworthsAdapter])
def test_adapter_implements_the_shared_async_contract(adapter_type: type[RetailerAdapter]) -> None:
    assert issubclass(adapter_type, RetailerAdapter)
    assert not inspect.isabstract(adapter_type)
    for method_name in RetailerAdapter.__abstractmethods__:
        assert inspect.iscoroutinefunction(getattr(adapter_type, method_name))
