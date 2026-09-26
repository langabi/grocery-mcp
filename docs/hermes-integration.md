# Hermes integration

Hermes supports remote HTTP/Streamable HTTP MCP servers through an entry in `~/.hermes/config.yaml`. Attach both containers to `agent_internal`, which carries no route off the host. Grocery MCP authenticates nothing, so reaching port 8000 is the whole of the access control: keep that network to Hermes and this service.

The URL host must be one of `GROCERY_ALLOWED_HOSTS`. A request carrying any other `Host` header is answered with a bare HTTP 421 rather than a JSON-RPC error, so if the Compose service is renamed, add the new name to that list.

```yaml
mcp_servers:
  grocery:
    url: "http://grocery-mcp:8000/mcp"
    tools:
      include:
        - search_products
        - compare_products
        - compare_baskets
        - get_cart
        - resolve_household_item
        - prepare_cart_changes
        - apply_cart_changes
        - discard_cart_changes
        - retailer_health
      prompts: false
      resources: false
```

Reload MCP servers in Hermes after changing configuration. Hermes registers these with its MCP server-name prefix.

Recommended Hermes instruction:

> Use Grocery MCP read tools freely. To change a cart — adding, reducing, or removing — first call `prepare_cart_changes`, show the user the exact retailer, products, the action and quantity for each one, estimated price change, warnings, and proposal expiry, and ask for explicit confirmation. Call `apply_cart_changes` only after an unambiguous confirmation referring to that proposal. Never describe preparation as having changed the cart. If apply reports expiry, cart drift, price change, failure, or reconfirmation, do not retry; prepare and present a new proposal. Never attempt checkout, payment, order placement, or delivery-slot booking.

## Smoke test

1. From the Hermes container, confirm `http://grocery-mcp:8000/health` returns `{"status":"ok"}`.
2. Reload MCP and confirm all nine tools are discovered.
3. Call `retailer_health` and verify catalogue capability separately from cart capability.
4. Search a product and confirm no retailer credential appears in Hermes logs/context.
5. With writes disabled, ensure `apply_cart_changes` cannot mutate either cart.
6. After the operator completes retailer verification and enables one write flag, run the exact prepare/show/approve/apply flow with one test item.
7. Repeat that flow with `{"mode": "set", "quantity": 0}` and confirm the test item leaves the cart.

Do not place retailer credentials, tokens, household coordinates, or the Grocery MCP encryption key in Hermes configuration.

