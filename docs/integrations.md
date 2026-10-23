# Adding household tools

The server exposes `household_status` and Yoto player tools. Follow [Yoto setup](yoto.md)
to authorise an account and select allowed players. [Ocado setup](ocado.md) connects
current-trolley and next-order tools using an operator-provided browser session, plus
optional confirmed draft quantity edits for the next Reserved order. The separately enabled
weekly workflow clears and refills the next booked order with a private fixed list; checkout
remains manual. [Recipe shopping lists](recipes.md) can be added to that workflow.
Recipe files and their product mappings remain private runtime data.
No real credentials or fake shopping/player data are included.

Add adapter modules under `src/household_tools/integrations/` when implementing each
service, and register narrowly scoped functions in `server.py` with `@mcp.tool()`.
Hermes starts this server over stdio; no separate HTTP server or public port is needed.
Log to stderr, never stdout. Use timeouts for network calls and return actionable errors
without tokens or raw account responses.

1. Verify the service's current API/authentication options before implementation.
2. Keep tokens in `data/.env` or service credential files below `data/home/`.
   Hermes can pass env values through the MCP server's `env` configuration using
   `${VARIABLE_NAME}` interpolation. Verify this with a harmless auth check.
3. Add dependencies with `uv add ...`; commit the updated lockfile.
4. Add integration tests using fixtures and mock HTTP responses; keep live tests opt-in.
5. Add explicitly approved tool names to `mcp_servers.household.tools.include`
   in both the template and existing `data/config.yaml`.
6. Run `make check`, rebuild the image, and recreate the gateway.

## Yoto first slice

Authenticate the household account, list players, then read player state. Next add
play/pause and volume with an operator-configured player allowlist, friendly aliases,
and a maximum volume enforced by code. Do not assume a droplet can reach LAN-only
controls: if needed, use a private network or a home-side connector.

## Ocado first slice

Start with a read-only shopping-list/basket tool. Establish how authentication and
session renewal work, including any browser login requirement. Keep cookies private.
Add basket editing separately; leave checkout and payments unexposed initially.

SOUL.md is behavior guidance, not a security boundary. Enforce restrictions in adapter
code. Credentials shared with an agent's shell can still be accessed by that agent;
use separate constrained services if stronger isolation is needed later.
