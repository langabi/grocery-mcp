from __future__ import annotations

import asyncio

from mcp.server.transport_security import TransportSecuritySettings

from grocery_mcp.app import build_application
from grocery_mcp.config import get_settings
from grocery_mcp.logging import configure_logging


async def _serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    application = await build_application(settings)
    allowed_hosts = [host if ":" in host else f"{host}:*" for host in settings.allowed_hosts]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[],
    )
    try:
        await application.server.run_streamable_http_async(
            host=settings.host,
            port=settings.port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            max_request_body_size=1024 * 1024,
            transport_security=security,
        )
    finally:
        await application.close()


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
