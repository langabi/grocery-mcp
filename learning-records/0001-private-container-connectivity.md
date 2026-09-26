# Private container connectivity verified

Hermes successfully resolved `grocery-mcp` and received `{"status":"ok"}` from inside its own container after joining `grocery-mcp_agent_internal`. This establishes that connectivity must be tested from the consuming container and that the private Docker network—not a published host port—is the Grocery MCP access boundary.

**Evidence:** The learner attached `hermes` to the internal network, ran the health request inside Hermes, and observed the expected response. Rebuilding Grocery MCP also removed unsafe upstream HTTP request-line logging.
