# Implementation notes

Updated: 2026-09-28

## Decisions

### MCP transport

The service uses the official Python MCP SDK v2 and stateless Streamable HTTP at `/mcp`. The v2 server supports the current 2026 protocol and older Streamable HTTP clients. Hermes supports HTTP MCP entries through a `url` configuration, so this is preferable to SSE or coupling Hermes to a subprocess.

DNS-rebinding protection is enabled. Accepted Host values default to `grocery-mcp`, `localhost`, and `127.0.0.1`, with any port. The Docker Compose example exposes the service only on an internal Docker network and does not publish a host port.

The exact installed Hermes version was not available in this repository. Connectivity must therefore be confirmed from the deployed Hermes container using the smoke test in `docs/hermes-integration.md`.

### Retailer API status

- Sixty60 bootstrap, store-context lookup, and catalogue search were live on 2026-09-13. OTP login and authenticated cart operations remain unverified.
- Woolworths Constructor.io catalogue search was live on 2026-09-13. Cognito and WFS were reachable, but login and authenticated cart operations remain unverified.
- Private API response shapes are validated strictly and isolated in retailer mappers. No raw payload is part of the MCP contract.

### Database schema

Alembic is the only thing that creates or alters schema. The service runs `alembic upgrade head` itself at startup, from migration scripts packaged inside `grocery_mcp`, so local runs, tests, and the container all build the database the same way and a missing migration fails immediately rather than in production only.

### Authentication and sessions

- Sixty60 uses an operator-driven phone/OTP flow. The observed upstream implementation returns a refresh token but exposes no known refresh operation. The adapter therefore fails closed when its access token expires/rejects and requires deterministic operator reauthentication.
- Woolworths uses Cognito `USER_PASSWORD_AUTH` and `REFRESH_TOKEN_AUTH`. Passwords are input or injected only for login and are never persisted by Grocery MCP. Id/refresh tokens are encrypted before SQLite storage.
- Retailer credentials and precise locations use Fernet authenticated encryption with `GROCERY_ENCRYPTION_KEY`. Missing encryption configuration prevents saving secrets.
- Session persistence survives a container restart through `/app/state/grocery.db`. Actual retailer token longevity and refresh-token inactivity expiry remain live-verification items.

### Cart identity and quantity

- Carts and catalogue availability are delivery-context/store-specific. Sixty60 requires an exact pinned cart ID before writes; it never falls back to the first cart.
- Woolworths exposes an absolute quantity operation using its internal cart-line `commerceId`. The adapter identifies the on-demand cart by its delivery type, and refuses to act when the account returns more than one candidate cart.
- Sixty60's observed mutation rewrites the full cart collection. The adapter serializes local writes, requires a pinned cart and exact delivery address, submits an absolute target quantity, refreshes promotions, and verifies by rereading.
- A service-level change set is atomically claimed before the first write. An interrupted or partially failed apply is never automatically retried, preventing an uncertain additive call from duplicating items.
- Requested items carry an `add` or `set` mode, and `set` with a quantity of zero is a removal. Stock and price gates apply only to changes that increase a quantity, so an item that has gone out of stock or moved in price can still be taken out of the cart.

### Product identity and pricing

- Product-ID stability across stores and time is not proven. Preferred mappings are retailer-specific and are always revalidated against the current retailer context.
- Pack sizes are parsed from common `2 L`, `500 g`, and multipack forms and normalized to litres/kilograms/items. Unparseable or dimensionally different results carry warnings rather than being presented as equivalent.
- Catalogue price and stock are provisional until checked through the selected account/delivery context. Apply re-fetches each product and requires reconfirmation above the configured price-change threshold.

### Order history

- `list_orders` and `get_order` are read-only and require an authenticated retailer session.
- Retailer payloads are normalized into a bounded order model. Delivery addresses, account fields,
  payment data, tokens, and raw payloads are excluded from the MCP response.
- Sixty60 uses its observed order-history endpoint; its response shape remains a
  live-verification gate.
- Woolworths lists orders from WFS `GET /order`, which returns `upcomingOrders` and `pastOrders`
  (the 20 most recent; `totalOrderCount` can be higher) with state, total, item count and
  `submittedDate`, but no line items. `submittedDate` is a `DD/MM/YYYY HH:MM:SS` South African
  local time, and the list reports every time as `12:00:00`, so only the date is meaningful.
- Woolworths `get_order` takes the state, total and date from the list entry and the line items
  from `GET /order/{orderId}`. The detail payload's own summary is not used: its `completedDate`
  is in a different date order, and its total and item count differ from the list entry's.
- Woolworths cart and order lines carry prices in `priceInfo`. `listPrice` is the unit shelf
  price; `amount` is the line total after discounts; `salePrice` is `0.0` for an item not on sale.
- Sixty60 `get_order` selects from the history returned by the retailer rather than calling an
  unverified detail endpoint. It can therefore return only the detail embedded in that history
  response.

### Rate limits and anti-bot controls

No documented limits were found. HTTP clients use explicit timeouts and classify 429/5xx as retailer unavailability. The service performs no blind retry of cart writes. A future deployment may add conservative read-only retry/backoff after measuring live behaviour.

### Licence constraints

- `yashiels/woolworths-cli` is MIT licensed. Attribution is preserved in `THIRD_PARTY_NOTICES.md` and the Woolworths adapter notice.
- `stephancill/checkers-sixty60` had no licence grant. The Sixty60 adapter is independently written from observed protocol behaviour; upstream source must not be copied without permission.

## Deliberate exclusions

The codebase contains no checkout, order placement, payment, 3-D Secure, saved-card retrieval, committed delivery-slot booking, or general-purpose MCP add/remove tool. Those capabilities require a separate future security design and explicit scope extension.

## Remaining live-verification gates

Before enabling each retailer's write flag:

1. Complete operator login using a dedicated test account or deliberately empty cart.
2. Verify session reload after restarting the service.
3. Confirm exact address, store/service context, and cart identity.
4. Run a read-only search/cart contract check.
5. Prepare one inexpensive item change and inspect it.
6. Explicitly approve one add/set/remove round trip.
7. Verify the original cart is restored and record any promotion/price effects.
