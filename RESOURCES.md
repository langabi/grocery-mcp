# Hermes–Grocery MCP Resources

## Knowledge

- [Hermes integration guide](docs/hermes-integration.md)
  Repository-specific configuration, safety instruction, and end-to-end smoke test. Use for connecting and validating Hermes.
- [Operations guide](docs/operations.md)
  Repository-specific authentication, cart pinning, write-enablement, logging, and incident procedures. Use before any live mutation.
- [Project README](README.md)
  Service safety model and Docker deployment overview. Use for understanding the prepare/approve/apply boundary.

## Wisdom (Communities)

- The deployed Hermes and Grocery MCP logs and observed cart state
  Treat controlled live verification as the final authority for private retailer API behavior.

## Gaps

- The exact deployed Hermes version and its reload command are not recorded in this workspace; determine them from the running container before reloading MCP configuration.
