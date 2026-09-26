from __future__ import annotations

from grocery_mcp.config import Settings


def test_blank_optional_coordinates_in_env_file_are_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GROCERY_SIXTY60_LATITUDE", raising=False)
    monkeypatch.delenv("GROCERY_SIXTY60_LONGITUDE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GROCERY_SIXTY60_LATITUDE=\nGROCERY_SIXTY60_LONGITUDE=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.sixty60_latitude is None
    assert settings.sixty60_longitude is None
