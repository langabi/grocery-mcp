from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path
from typing import Any

from grocery_mcp.app import Application, build_application
from grocery_mcp.config import Settings
from grocery_mcp.domain.models import ConstraintLevel, Retailer
from grocery_mcp.domain.preferences import CanonicalItem, ItemPreference, RetailerItemMapping
from grocery_mcp.logging import configure_logging
from grocery_mcp.retailers.woolworths.auth import WoolworthsCredentials


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grocery-admin", description="Trusted operator administration for Grocery MCP"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="Authenticate a retailer")
    login.add_argument("retailer", choices=[retailer.value for retailer in Retailer])
    login.add_argument("--email")

    location = commands.add_parser("set-location", help="Persist encrypted retailer location")
    location.add_argument("retailer", choices=[retailer.value for retailer in Retailer])
    location.add_argument("--latitude", type=float)
    location.add_argument("--longitude", type=float)
    location.add_argument("--place-id")
    location.add_argument("--store-id")

    pin = commands.add_parser("pin-cart", help="Pin the exact Sixty60 cart allowed for writes")
    pin.add_argument("retailer", choices=[Retailer.SIXTY60.value])
    pin.add_argument("cart_id")

    locations = commands.add_parser(
        "list-locations", help="List saved retailer delivery contexts"
    )
    locations.add_argument("retailer", choices=[Retailer.WOOLWORTHS.value])

    add_item = commands.add_parser("add-household-item", help="Create/update a canonical item")
    add_item.add_argument("key")
    add_item.add_argument("--display-name", required=True)
    add_item.add_argument("--quantity", type=int, default=1)
    add_item.add_argument("--alias", action="append", default=[])
    add_item.add_argument("--must", action="append", default=[])
    add_item.add_argument("--prefer", action="append", default=[])
    add_item.add_argument("--avoid", action="append", default=[])
    add_item.add_argument("--never", action="append", default=[])

    mapping = commands.add_parser("map-product", help="Map a canonical item to a retailer ID")
    mapping.add_argument("key")
    mapping.add_argument("retailer", choices=[retailer.value for retailer in Retailer])
    mapping.add_argument("product_id")
    mapping.add_argument("--name", required=True)

    import_items = commands.add_parser(
        "import-household-items", help="Create/update canonical items from a JSON file"
    )
    import_items.add_argument("path", type=Path)

    commands.add_parser("list-household-items", help="List canonical household items")
    commands.add_parser("health", help="Check retailer capability health")
    return parser


def _preferences(args: argparse.Namespace) -> list[ItemPreference]:
    result: list[ItemPreference] = []
    for level in ConstraintLevel:
        for value in getattr(args, level.value):
            result.append(ItemPreference(attribute="name", value=value, level=level))
    return result


async def _run(args: argparse.Namespace) -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    credentials = None
    if args.command == "login" and args.retailer == Retailer.WOOLWORTHS.value:
        email = args.email or settings.woolworths_email
        if not email:
            email = (await asyncio.to_thread(input, "Woolworths email: ")).strip()
        password = await asyncio.to_thread(getpass.getpass, "Woolworths password: ")
        credentials = WoolworthsCredentials(email=email, password=password)

    application = await build_application(settings, woolworths_credentials=credentials)
    try:
        await _dispatch(application, args)
    finally:
        await application.close()


async def _dispatch(application: Application, args: argparse.Namespace) -> None:
    if args.command == "login":
        if args.retailer == Retailer.SIXTY60.value:
            phone = (await asyncio.to_thread(input, "Sixty60 phone number: ")).strip()
            challenge = await application.sixty60.start_login(phone)
            otp = await asyncio.to_thread(getpass.getpass, "SMS OTP: ")
            await application.sixty60.complete_login(challenge, otp)
        else:
            await application.woolworths_auth.login()
        print(json.dumps({"status": "authenticated", "retailer": args.retailer}))
        return

    if args.command == "set-location":
        retailer = Retailer(args.retailer)
        if retailer == Retailer.SIXTY60:
            if args.latitude is None or args.longitude is None:
                raise ValueError("Sixty60 requires --latitude and --longitude")
            if not -90 <= args.latitude <= 90 or not -180 <= args.longitude <= 180:
                raise ValueError("coordinates are outside valid ranges")
            payload: dict[str, Any] = {
                "latitude": args.latitude,
                "longitude": args.longitude,
            }
        else:
            if not args.place_id or not args.store_id:
                raise ValueError("Woolworths requires --place-id and --store-id")
            payload = {"place_id": args.place_id, "store_id": args.store_id}
        await application.state.save_location(retailer, payload)
        print(json.dumps({"status": "saved", "retailer": retailer.value}))
        return

    if args.command == "pin-cart":
        await application.sixty60.pin_cart(args.cart_id)
        print(json.dumps({"status": "pinned", "retailer": args.retailer}))
        return

    if args.command == "list-locations":
        locations = await application.woolworths.list_saved_locations()
        print(json.dumps(locations, indent=2))
        return

    if args.command == "add-household-item":
        item = CanonicalItem(
            key=args.key,
            display_name=args.display_name,
            default_quantity=args.quantity,
            aliases=args.alias,
            preferences=_preferences(args),
        )
        await application.preferences.upsert_item(item)
        print(item.model_dump_json())
        return

    if args.command == "map-product":
        mapping = RetailerItemMapping(
            canonical_key=args.key,
            retailer=Retailer(args.retailer),
            retailer_product_id=args.product_id,
            product_name=args.name,
        )
        await application.preferences.map_product(mapping)
        print(mapping.model_dump_json())
        return

    if args.command == "import-household-items":
        raw = json.loads(args.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("the household item file must contain a JSON array")
        items = [CanonicalItem.model_validate(entry) for entry in raw]
        for item in items:
            await application.preferences.upsert_item(item)
        print(json.dumps({"status": "imported", "items": [item.key for item in items]}))
        return

    if args.command == "list-household-items":
        items = await application.preferences.list_items()
        print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
        return

    if args.command == "health":
        results = []
        for _retailer, adapter in application.services.adapters.items():
            result = await adapter.health_check()
            results.append(result.model_dump(mode="json"))
        print(json.dumps(results, indent=2))
        return

    raise ValueError(f"unknown command: {args.command}")


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        raise SystemExit(f"error: {type(exc).__name__}: {exc}") from None


if __name__ == "__main__":
    main()
