from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class EncryptionNotConfiguredError(RuntimeError):
    pass


class InvalidEncryptedValueError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key: str | None) -> None:
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt_json(self, value: dict[str, Any]) -> str:
        if self._fernet is None:
            raise EncryptionNotConfiguredError(
                "GROCERY_ENCRYPTION_KEY is required before storing retailer credentials"
            )
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return self._fernet.encrypt(raw).decode()

    def decrypt_json(self, value: str) -> dict[str, Any]:
        if self._fernet is None:
            raise EncryptionNotConfiguredError(
                "GROCERY_ENCRYPTION_KEY is required before loading retailer credentials"
            )
        try:
            decoded = self._fernet.decrypt(value.encode())
        except InvalidToken as exc:
            raise InvalidEncryptedValueError(
                "stored retailer credentials cannot be decrypted"
            ) from exc
        result = json.loads(decoded)
        if not isinstance(result, dict):
            raise InvalidEncryptedValueError("stored retailer credentials are not an object")
        return result
