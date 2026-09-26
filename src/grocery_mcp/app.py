from __future__ import annotations

from dataclasses import dataclass

import httpx
from mcp.server import MCPServer

from grocery_mcp.config import Settings
from grocery_mcp.domain.models import Retailer
from grocery_mcp.http_debug import event_hooks
from grocery_mcp.mcp.server import Services, create_server
from grocery_mcp.retailers.sixty60 import Sixty60Adapter, Sixty60Client, Sixty60Config
from grocery_mcp.retailers.sixty60.auth import PersistentSixty60SessionStore
from grocery_mcp.retailers.woolworths.adapter import WoolworthsAdapter
from grocery_mcp.retailers.woolworths.auth import (
    PersistentWoolworthsSessionStore,
    WoolworthsAuth,
    WoolworthsCredentials,
)
from grocery_mcp.retailers.woolworths.client import WoolworthsClient, WoolworthsConfig
from grocery_mcp.services.cart_service import CartService
from grocery_mcp.services.comparison import ComparisonService
from grocery_mcp.services.preferences import PreferenceService
from grocery_mcp.services.registry import AdapterRegistry
from grocery_mcp.services.search import SearchService
from grocery_mcp.storage.db import Database
from grocery_mcp.storage.encryption import SecretBox
from grocery_mcp.storage.repositories import (
    AuditRepository,
    ChangeSetRepository,
    EncryptedRetailerStateStore,
    PreferenceRepository,
)


@dataclass(slots=True)
class Application:
    settings: Settings
    database: Database
    state: EncryptedRetailerStateStore
    preferences: PreferenceRepository
    services: Services
    server: MCPServer
    sixty60: Sixty60Adapter
    woolworths: WoolworthsAdapter
    woolworths_auth: WoolworthsAuth
    woolworths_http: httpx.AsyncClient

    async def close(self) -> None:
        await self.services.adapters.close()
        await self.woolworths_http.aclose()
        await self.database.close()


def _secret(value: object | None) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value)


async def build_application(
    settings: Settings,
    *,
    woolworths_credentials: WoolworthsCredentials | None = None,
) -> Application:
    database = Database(settings.resolved_database_url)
    await database.initialise()
    secret_box = SecretBox(
        settings.encryption_key.get_secret_value() if settings.encryption_key else None
    )
    state = EncryptedRetailerStateStore(database, secret_box)

    stored_sixty60_location = await state.load_location(Retailer.SIXTY60)
    latitude = settings.sixty60_latitude
    longitude = settings.sixty60_longitude
    if latitude is None and stored_sixty60_location:
        raw_latitude = stored_sixty60_location.get("latitude")
        latitude = float(raw_latitude) if raw_latitude is not None else None
    if longitude is None and stored_sixty60_location:
        raw_longitude = stored_sixty60_location.get("longitude")
        longitude = float(raw_longitude) if raw_longitude is not None else None

    sixty60_client = Sixty60Client(
        Sixty60Config(
            latitude=latitude,
            longitude=longitude,
            timeout_seconds=settings.retailer_timeout_seconds,
            app_version=settings.sixty60_app_version,
            app_build=settings.sixty60_app_build,
            dsl_api_key=_secret(settings.sixty60_dsl_api_key),
            auth_api_key=_secret(settings.sixty60_auth_api_key),
            profile_api_token=_secret(settings.sixty60_profile_api_token),
        ),
        debug_http=settings.debug_http,
    )
    sixty60 = Sixty60Adapter(
        sixty60_client,
        PersistentSixty60SessionStore(state),
        write_enabled=settings.enable_sixty60_writes,
    )

    if (
        woolworths_credentials is None
        and settings.woolworths_email
        and settings.woolworths_password
    ):
        woolworths_credentials = WoolworthsCredentials(
            email=settings.woolworths_email,
            password=settings.woolworths_password,
        )
    woolworths_http = httpx.AsyncClient(
        timeout=settings.retailer_timeout_seconds,
        event_hooks=event_hooks(settings.debug_http),
    )
    stored_woolworths_location = await state.load_location(Retailer.WOOLWORTHS)
    woolworths_place_id = settings.woolworths_place_id or (
        str(stored_woolworths_location.get("place_id"))
        if stored_woolworths_location and stored_woolworths_location.get("place_id")
        else None
    )
    woolworths_store_id = settings.woolworths_store_id or (
        str(stored_woolworths_location.get("store_id"))
        if stored_woolworths_location and stored_woolworths_location.get("store_id")
        else None
    )
    woolworths_config = WoolworthsConfig(
        timeout_seconds=settings.retailer_timeout_seconds,
        place_id=woolworths_place_id,
        store_id=woolworths_store_id,
    )
    woolworths_auth = WoolworthsAuth(
        woolworths_http,
        PersistentWoolworthsSessionStore(state),
        client_id=woolworths_config.cognito_client_id,
        cognito_url=woolworths_config.cognito_url,
        credentials=woolworths_credentials,
        timeout=settings.retailer_timeout_seconds,
    )
    woolworths = WoolworthsAdapter(
        WoolworthsClient(woolworths_http, woolworths_auth, config=woolworths_config),
        write_enabled=settings.enable_woolworths_writes,
    )

    adapters = AdapterRegistry({Retailer.SIXTY60: sixty60, Retailer.WOOLWORTHS: woolworths})
    preferences = PreferenceRepository(database)
    preference_service = PreferenceService(preferences)
    services = Services(
        adapters=adapters,
        search=SearchService(adapters),
        comparison=ComparisonService(adapters, preference_service),
        preferences=preference_service,
        carts=CartService(
            adapters,
            preference_service,
            ChangeSetRepository(database),
            AuditRepository(database),
            ttl_seconds=settings.change_set_ttl_seconds,
            price_tolerance_percent=settings.price_change_tolerance_percent,
        ),
    )
    server = create_server(services)
    return Application(
        settings=settings,
        database=database,
        state=state,
        preferences=preferences,
        services=services,
        server=server,
        sixty60=sixty60,
        woolworths=woolworths,
        woolworths_auth=woolworths_auth,
        woolworths_http=woolworths_http,
    )
