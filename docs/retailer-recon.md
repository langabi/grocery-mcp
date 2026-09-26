# Retailer reconnaissance

Date: 2026-09-13  
Scope: Phase 0 only  
Verdict: catalogue feasibility is proven; authenticated cart compatibility is not yet proven.

## Executive summary

Both reference projects are recent and can be run locally. Live, read-only catalogue searches work for Woolworths and Checkers. The private account/cart APIs are reachable but could not be verified without an operator-supplied retailer account/session. No OTP was requested and no real cart was read or changed during this reconnaissance.

This is enough evidence to retain Python and the proposed retailer-adapter boundary. It is not enough evidence to enable production cart tools. The remaining build must put authenticated adapter work behind a test-account gate and must not rely on either upstream CLI's error handling, credential storage, or mutation semantics.

Status key: **green** = live-verified; **amber** = source-confirmed/reachable but not authenticated live-verified; **red** = unsuitable to carry into the service.

| Capability | Checkers Sixty60 | Woolworths Dash |
| --- | --- | --- |
| Clone/build/run | **Green** | **Green** |
| Catalogue search | **Green** (safe anonymous API probe) | **Green** (live CLI search) |
| Login | **Amber** (OTP flow identified) | **Amber** (Cognito flow identified) |
| Token refresh | **Red** upstream: token saved but no refresh implementation | **Amber** implementation present, lifetime unverified |
| Cart read | **Amber** | **Amber** |
| Add/remove/set quantity | **Amber** | **Amber** |
| Licence for reuse | **Red**: no licence found | **Green**: MIT |
| Suitable credential handling | **Red** | **Red** |
| Checkout in Grocery MCP | Out of scope | **Red**: upstream checkout code must be excluded |

## Reproduction record

Temporary clones were inspected outside this repository:

- [stephancill/checkers-sixty60](https://github.com/stephancill/checkers-sixty60), commit `f20114850f15b1d276124c294ad826866595d7c9`, dated 2026-07-17, package version `0.1.7`.
- [yashiels/woolworths-cli](https://github.com/yashiels/woolworths-cli), commit `7cb35535713251cbf9c009eda60f5e7be5a1578c`, dated 2026-09-06, package version/tag `1.1.2`.

Environment: Node `20.17.0`. Bun was not installed.

Checks performed:

- Checkers: `npm install`, TypeScript build, Biome lint, built CLI help, anonymous BFF-token bootstrap, anonymous store-context lookup, and anonymous one-result catalogue search succeeded.
- Checkers CLI search/cart without local auth stopped with `No local auth found`, as expected.
- Woolworths: syntax/lint, four smoke tests, CLI help/version, and a live unauthenticated search for `milk` succeeded. The search returned 20 current names, prices, SKUs, and product URLs.
- Woolworths WFS cart endpoint was reachable and rejected a credential-free request with HTTP 401, as expected. Cognito was reachable and rejected an incomplete request with HTTP 400.
- Woolworths CLI cart without credentials stopped with a missing-credentials error, as expected.

Not performed:

- Sending an OTP or logging in to either retailer.
- Reading a real account's cart, addresses, or store assignment.
- Adding, setting, or removing any real cart item.
- Calling checkout, booking a delivery slot, retrieving payment cards, or placing an order.

These omissions are deliberate: login needs user-controlled credentials/OTP, and mutation needs an explicitly authorized test cart and reversible test window.

## Checkers Sixty60

### Upstream condition

The project is a small TypeScript CLI, not an MCP server. It declares Node 18+ and uses Bun for its development command, but it builds and its compiled CLI runs under Node 20. There are no automated tests or CI checks in the repository.

No `LICENSE`, `COPYING`, or `NOTICE` file and no package `license` field were found. Public source is not the same as permission to copy or create derivatives. The Grocery MCP implementation should therefore be clean-room code based on observed behaviour and independently written contracts unless the author grants permission. A legal/product review should also consider Checkers' terms and authorization to automate the private API before distribution.

### Actual API dependencies

The upstream flow depends on five hosts:

| Purpose | Host / operation |
| --- | --- |
| Bootstrap | `dc-app-backend-for-frontend.sixty60.co.za`, `POST /api/v1/token/dsl` |
| Account/OTP DSL | `api.shopritegroup.co.za/dsl/brands/checkers/countries/ZA` |
| Profile | `auth.sixty60.co.za` |
| Store and catalogue | `catalog.sixty60.co.za` |
| Orders/cart | `orders-api.sixty60.co.za` |

Observed login sequence:

1. Obtain a BFF/DSL bearer token.
2. Verify the phone/user.
3. Request an SMS OTP.
4. Verify the OTP and obtain access/refresh tokens.
5. Fetch the customer profile.
6. Resolve store contexts from latitude/longitude.

The profile request embeds the user access token in the URL and uses a separate hard-coded profile authorization value. API keys, app version/build values, and device/app headers are also hard-coded. All should be treated as volatile adapter configuration and always redacted.

Store context and catalogue search were live-verified without a user login. A generic Cape Town location returned five store contexts, and a one-result product search returned a successful product response. The upstream CLI unnecessarily gates search on saved user auth; our adapter should separate anonymous catalogue context from authenticated account context.

### Cart semantics and risks

Cart read is implemented as a location-specific `POST /api/v2/carts/user`. Add and remove are not narrow item operations. They:

1. resolve store contexts again;
2. read all carts;
3. select the `sixty-min-delivery` cart, falling back to the first cart;
4. modify an in-memory line-item list;
5. submit the full carts collection to `POST /api/v3/carts/update`; and
6. refresh promotions for every cart.

Add increments an existing quantity. Remove decrements it or marks the line removed at quantity zero. There is no upstream token-refresh implementation and no explicit absolute-quantity API wrapper.

Consequences for the build:

- Never fall back to the first cart for a write. Require an explicit resolved delivery context and cart ID.
- Serialize Checkers mutations per account/cart and use a fresh snapshot immediately before a full-cart rewrite.
- Diff the intended and submitted cart, then read back and verify the result.
- Treat store IDs and product availability as location-specific. Do not assume a product ID is ranged identically across stores.
- Implement idempotency above the adapter. A retry of the upstream additive flow can duplicate quantities.
- Initially disable broad remove-all behaviour and fail closed on cart drift, ambiguous carts, or promotion/update failure.

### Security and operational gaps

The upstream stores phone, email, customer/user IDs, access token, and refresh token in plaintext JSON without explicitly restrictive file/directory permissions. Its HTTP wrapper has no explicit timeout, retry policy, schema validation, or redaction, and errors may include full response bodies. Some requests declare form encoding while the wrapper serializes JSON.

Do not port those mechanics. The service needs a dedicated state directory, restrictive permissions, encrypted sensitive values where practical, typed/redacted errors, explicit timeouts, response validation, and separate reachability/auth/schema health signals.

## Woolworths Dash

### Upstream condition

The project is a CommonJS Node CLI/client requiring Node 16+ with no runtime dependencies. It runs on Node 20. Its current automated suite contains only four smoke checks for entrypoint, syntax, and help output; it has no live API contract coverage.

The project is MIT licensed. Any copied or substantially derived portions must preserve the copyright and licence notice. A direct Python implementation based on documented/observed behaviour remains preferable to shelling out to Node in production.

### Actual API dependencies

| Purpose | Host / operation |
| --- | --- |
| Authentication | AWS Cognito `cognito-idp.eu-west-1.amazonaws.com`, `InitiateAuth` |
| Search | Constructor.io `wpkmgeuco-zone.cnstrc.com` |
| Product/cart/delivery | WFS `wfs-appserver.wigroup.co/wfs/app/v4` |
| Checkout/payment support | `www.woolworths.co.za/server` — explicitly out of scope |

Authentication uses `USER_PASSWORD_AUTH` to obtain a Cognito IdToken and RefreshToken. The JWT `exp` claim drives refresh 60 seconds early, and `REFRESH_TOKEN_AUTH` obtains a new IdToken. The IdToken is sent to WFS as `Sessiontoken`, alongside hard-coded Android version/device headers and an APK-level SHA1 value. The upstream description of a roughly 24-hour IdToken and weeks/months-long refresh token was not independently verified.

Search uses Constructor.io `/search/{query}`, falling back to `/autocomplete/{query}`. It requires no user authentication and is live today. Its normalized result contains SKU, name, price, image, and URL, but no proven store availability or normalized pack/unit information. Search price and availability must therefore be treated as provisional until checked in a selected delivery context.

### Cart semantics and risks

Relevant WFS operations in the upstream client are:

- read: `GET /cartV2`;
- add: `POST /cart/OnDemand/itemV2` — **additive** quantity;
- set absolute quantity: `PUT /cartV2/item/{commerceId}`;
- remove: `DELETE /cartV2/item` with a `commerceId` body.

The cart-line `commerceId` is not the product SKU. It must be retained as retailer-internal cart state so set/remove operations target the right line.

Consequences for the build:

- Prefer absolute `PUT` for existing lines after a fresh cart read.
- For a new line, issue additive `POST` once under a persisted immutable change-set ID, then read back and verify.
- Validate positive integer quantities before calling the adapter. Upstream uses `quantity || 1`, so zero becomes one and negatives are not rejected.
- Keep `commerceId` internal; Hermes should receive stable canonical product/cart fields rather than raw WFS payloads.
- Do not treat public Constructor search as proof that an item is available in the household's Dash store.

### Security and correctness gaps

The upstream writes email, plaintext password, IdToken, RefreshToken, and optional checkout data to a fixed JSON file. Its diagnostic token command prints a token prefix. It also contains checkout, shipping, saved-card, clear-cart, and best-effort order-history paths that are outside this project's scope and must not be copied into the MCP surface.

More importantly, the HTTP helper resolves non-2xx responses rather than raising consistently. After one 401 refresh/retry, callers can parse an error payload as an empty cart or report a mutation without proving success. The new adapter must enforce success status, validate response schemas, use typed redacted exceptions, and verify all writes by rereading the cart.

## Impact on the remaining phases

### Phase 1 — Sixty60 adapter

Proceed only as a clean-room adapter. Anonymous search can be developed and contract-tested immediately. Authenticated cart work must remain disabled until an operator supplies a test account/OTP session and approves one controlled add/remove round trip. Add token-expiry/re-auth handling to the Phase 1 scope; upstream does not provide it. Checkers' full-cart rewrite makes snapshot checks, serialization, and read-after-write verification Phase 1 prerequisites, not later hardening.

### Phase 2 — Woolworths adapter

The MIT client is a useful behavioural reference. Search can be built immediately. Auth, refresh, delivery context, cart read, absolute set, add, and remove remain gated on an operator-controlled authenticated check. Retain `commerceId` in internal cart models. Exclude checkout-related code entirely.

### Phase 3 — MCP server

The MCP can expose capability-aware health and catalogue search before write capability is enabled. It must not advertise a retailer's cart tools as healthy merely because its host is reachable or search works. Health should distinguish `catalogue`, `authentication`, `cart_read`, and `cart_write`.

### Phase 4 — safety mechanics

Move a minimal version of safety earlier. Persist the change-set and idempotency claim before the first live mutation. Both adapters require read-after-write verification; Checkers additionally requires per-cart locking and exact cart identity. Price/availability must be refreshed at apply time.

### Phase 5 — household model

No fundamental change. Retailer mappings must be scoped by retailer and, where evidence requires it, store/delivery context. A preferred SKU cannot imply current availability.

### Phase 6 — comparison

Pack size is mostly embedded in names and is not normalized reliably by either reference client. Unit pricing needs an explicit parser with confidence/warnings. Comparisons must label public catalogue prices as provisional until store-context validation.

### Phase 7 — hardening

Several items should move earlier: explicit timeouts, redacted errors/logs, secure persisted sessions, schema validation, adapter fixtures, and a write circuit breaker. Private API fingerprints and response shapes will change; adapter health must expose actionable failure categories without leaking secrets.

## Gate to close Phase 0 completely

Use dedicated retailer test accounts or deliberately empty personal carts. In an operator-controlled session:

1. Authenticate through the admin flow and record token expiry metadata without recording tokens.
2. Restart the process/container and verify session recovery/refresh.
3. Resolve the intended address, store, service option, and exact cart.
4. Search one inexpensive, unambiguous product and record its normalized metadata.
5. Read and hash the starting cart.
6. After explicit confirmation, add quantity one exactly once.
7. Read back and verify the exact delta.
8. Set quantity absolutely where supported and verify again.
9. Remove the test item, verify restoration of the original cart, and record any promotion side effects.

Until that gate is run, Phase 0 is **partially complete by evidence and blocked only on authenticated manual verification**. Phase 1/2 scaffolding is feasible, but production cart support is not yet established.
