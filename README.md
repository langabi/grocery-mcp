# Grocery MCP for Hermes

A standalone, safety-first MCP service for searching South African grocery retailers, resolving household preferences, preparing cart proposals, and applying only previously persisted proposals after explicit approval.

Supported adapters:

- Checkers Sixty60
- Woolworths Dash

The service intentionally has no checkout, payment, order-placement, saved-card, or general-purpose cart mutation MCP tools.

## Safety model

Cart changes use two separate calls:

1. `prepare_cart_changes` reads the cart, resolves products, and persists an immutable proposal.
2. After the user reviews and approves it, `apply_cart_changes(change_set_id)` checks expiry, cart drift, price, and availability before writing.

Each change set is claimed atomically and can be applied only once. Uncertain or partially failed writes are marked failed and require reconciliation rather than an automatic retry.

Each requested item carries a `mode`. `add` (the default) adds to whatever the cart already holds; `set` makes the quantity exact, so a quantity of `0` proposes removing the product. Reductions and removals go through the same prepare/approve/apply path as additions, and are not blocked by a stock or price change.

Retailer writes are disabled by default. Enable them individually only after completing the authenticated checks in [the operations guide](docs/operations.md).

The MCP endpoint itself is unauthenticated: anything that can reach port 8000 can call `apply_cart_changes`. Network isolation is the only access control, so run the service on a private network shared with nothing but the agent that is meant to use it, and never publish a host port.

## Local development

Requirements: Python 3.12+ and `uv`.

```bash
uv sync --extra dev
cp .env.example .env
uv run pytest
uv run grocery-mcp
```

Normal tests never call retailer APIs. Run the opt-in anonymous catalogue check with
`RUN_LIVE_GROCERY_TESTS=1 uv run pytest -m live`.

The MCP endpoint uses Streamable HTTP at `http://localhost:8000/mcp`.

Alembic owns the database schema in every environment. The service runs `alembic upgrade head` against `GROCERY_STATE_DIR` on startup, so there is no separate migration step.

Generate an encryption key without printing it into application logs:

```bash
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store that value as `GROCERY_ENCRYPTION_KEY` in an operator-controlled secret or uncommitted `.env` file.

## Administration

Authentication and household preference changes are operator-only CLI operations, never MCP tools:

```bash
uv run grocery-admin login sixty60
uv run grocery-admin login woolworths
uv run grocery-admin set-location sixty60 --latitude LAT --longitude LNG
uv run grocery-admin add-household-item milk --display-name Milk --quantity 2 --alias "usual milk"
uv run grocery-admin import-household-items config/household-items.example.json
uv run grocery-admin map-product milk sixty60 PRODUCT_ID --name "Preferred milk"
```

Run `uv run grocery-admin --help` for complete usage.

## Docker

```bash
docker build -t grocery-mcp .
docker compose -f docker-compose.example.yml up -d
```

The Compose example exposes no host port. Hermes connects over the private Docker network at `http://grocery-mcp:8000/mcp`. `/app/state` is the only writable container path.

## Documentation

- [Retailer reconnaissance](docs/retailer-recon.md)
- [Implementation notes](docs/implementation-notes.md)
- [Operations and authentication](docs/operations.md)
- [Hermes integration](docs/hermes-integration.md)
