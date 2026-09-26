# Mission: Connect Grocery MCP to Hermes safely

## Why
Finish the live Grocery MCP integration so Hermes can search and inspect Sixty60 groceries, then make cart changes only through an explicit, reviewable approval flow.

## Success looks like
- Hermes discovers and can call all nine Grocery MCP tools over the private Docker network.
- Read-only catalogue and cart operations work without exposing retailer credentials.
- Sixty60 writes are enabled only after cart pinning and a controlled add/remove verification.

## Constraints
- Grocery MCP has no endpoint authentication, so only trusted containers may share its internal network.
- Retailer credentials, location data, and the encryption key remain outside Hermes configuration.
- Cart writes stay disabled until read-only integration checks pass.

## Out of scope
- Checkout, payment, order placement, and delivery-slot booking.
- Woolworths authentication and cart enablement during the initial Sixty60 integration.
