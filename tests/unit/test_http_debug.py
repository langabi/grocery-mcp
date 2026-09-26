from grocery_mcp.http_debug import redacted_path


def test_debug_path_redacts_sixty60_profile_token() -> None:
    path = "/customers/customer-1/customer-profile/v2/VERY_SECRET_TOKEN"

    rendered = redacted_path(path)

    assert rendered == "/customers/customer-1/customer-profile/v2/[REDACTED]"
    assert "VERY_SECRET_TOKEN" not in rendered
