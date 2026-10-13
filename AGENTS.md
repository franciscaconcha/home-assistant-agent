# Hermes Home

Personal Hermes Agent deployment for WhatsApp and household integrations.

- Keep upstream Hermes in the official container; do not fork its core for integrations.
- Add household capabilities to the Python stdio MCP server in `src/household_tools`.
- Read `docs/integrations.md` before adding Yoto or Ocado functionality.
- Never commit runtime state, tokens, phone numbers, WhatsApp sessions, or shopping data.
- Runtime state lives in ignored `data/`; tracked templates live in `config/`.
- Do not overwrite an existing runtime configuration during setup.
- Update `uv.lock` when changing dependencies. Run `make check` after code changes.
- Build and smoke-test the container when Docker is available; report when it is not.
- Deployments and billable cloud resources require an explicit user request.
