from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from grocery_mcp.domain.models import Retailer
from grocery_mcp.storage.db import Database, RetailerSessionRow
from grocery_mcp.storage.encryption import EncryptionNotConfiguredError, SecretBox
from grocery_mcp.storage.repositories import EncryptedRetailerStateStore


async def test_retailer_session_is_encrypted_at_rest(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.initialise()
    store = EncryptedRetailerStateStore(database, SecretBox(Fernet.generate_key().decode()))

    await store.save_session(Retailer.WOOLWORTHS, {"token": "top-secret"})

    async with database.session() as session:
        row = await session.get(RetailerSessionRow, Retailer.WOOLWORTHS.value)
        assert row is not None
        assert "top-secret" not in row.encrypted_payload
    assert await store.load_session(Retailer.WOOLWORTHS) == {"token": "top-secret"}
    await database.close()


async def test_missing_key_fails_closed_when_saving_credentials(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.initialise()
    store = EncryptedRetailerStateStore(database, SecretBox(None))

    with pytest.raises(EncryptionNotConfiguredError):
        await store.save_session(Retailer.SIXTY60, {"token": "secret"})
    await database.close()
