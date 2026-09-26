# Operations guide

## State and secrets

Persist only `/app/state`. It contains the SQLite database with encrypted retailer sessions, encrypted delivery context, change sets, and audit records.

Set `GROCERY_ENCRYPTION_KEY` from a Docker secret or an uncommitted `.env`. Back up the key separately from the database; losing it makes retailer sessions and locations unrecoverable. Restrict both to the Grocery MCP operator.

Recommended backup while the container is stopped:

```bash
docker compose -f docker-compose.example.yml stop grocery-mcp
docker run --rm -v grocery_mcp_state:/state -v "$PWD/backups:/backup" alpine \
  cp /state/grocery.db /backup/grocery-$(date +%Y%m%d).db
docker compose -f docker-compose.example.yml start grocery-mcp
```

Never include `.env` or the encryption key in the database backup archive.

## Sixty60 setup

Configure the current API/app fingerprints through `GROCERY_SIXTY60_DSL_API_KEY`, `GROCERY_SIXTY60_AUTH_API_KEY`, and `GROCERY_SIXTY60_PROFILE_API_TOKEN`. These are volatile private-API adapter settings and must be redacted from support output.

Persist the household location and authenticate:

```bash
grocery-admin set-location sixty60 --latitude LAT --longitude LNG
grocery-admin login sixty60
```

Read the cart with writes disabled, identify the single intended Sixty60 delivery cart, then pin it:

```bash
grocery-admin pin-cart sixty60 CART_ID
```

Only after the controlled verification gate succeeds should `GROCERY_ENABLE_SIXTY60_WRITES=true` be set.

If authentication fails, keep writes disabled, rerun `grocery-admin login sixty60`, then verify cart identity before re-enabling. The service does not guess at refresh-token behaviour.

## Woolworths setup

Persist the verified Dash place and store identifiers before any authenticated cart call:

```bash
grocery-admin set-location woolworths --place-id PLACE_ID --store-id STORE_ID
```

Supply `GROCERY_WOOLWORTHS_EMAIL` and `GROCERY_WOOLWORTHS_PASSWORD` only to the trusted admin login process, or enter the password interactively:

```bash
grocery-admin login woolworths --email person@example.com
```

The password is not persisted. The resulting Cognito Id/refresh tokens are encrypted in SQLite. Enable `GROCERY_ENABLE_WOOLWORTHS_WRITES=true` only after the controlled cart verification gate succeeds.

If token refresh fails, rerun the admin login with credentials. Do not expose the admin command through Hermes.

## Household preferences

Create at least five canonical products and map their verified retailer IDs:

```bash
grocery-admin add-household-item milk --display-name "Full cream milk 2L" \
  --quantity 2 --alias "usual milk" --must "full cream" --never "low fat"
grocery-admin map-product milk sixty60 PRODUCT_ID --name "Clover Full Cream Milk 2L"
grocery-admin map-product milk woolworths SKU --name "Fresh Full Cream Ayrshire Milk 2 L"
```

`config/household-items.example.json` contains five starter concepts but deliberately contains no guessed retailer IDs. Load it, or your own file in the same shape, in one call:

```bash
grocery-admin import-household-items config/household-items.example.json
```

Retailer IDs still have to be mapped individually with `map-product`, against a product you have verified in the current store context.

## Health and incident handling

`GET /health` is process liveness only and discloses no retailer state. The `retailer_health` MCP tool separates catalogue, authentication, cart-read, and cart-write readiness.

On an adapter failure:

1. Disable that retailer's write flag.
2. Preserve the state database and sanitized JSON logs.
3. Check catalogue health separately from auth/cart health.
4. Reproduce with read-only calls and update recorded contract fixtures.
5. Reauthenticate only through the admin CLI.
6. Re-enable writes only after a controlled add/read/remove verification.

For short-lived contract diagnosis, set `GROCERY_DEBUG_HTTP=true`. This logs only method,
hostname, a redacted path, status, and latency; query parameters, headers, and payloads are never
logged. Disable it again after capture.

An `applying` or `failed` change set represents an uncertain write and must not be retried automatically. Inspect the retailer cart, reconcile manually, and prepare a new change set.
