# Hermes Home

Personal Hermes Agent on a DigitalOcean droplet, reached through WhatsApp, with
household tools maintained in this repository. The initial MCP server has a working
status tool and Yoto player controls. Follow [Yoto setup](docs/yoto.md) to connect your
account. [Ocado setup](docs/ocado.md) adds reads of your current trolley and next scheduled
Reserved order, plus optional confirmed draft quantity changes/removals. Final review
and saving/checking out remain on Ocado's website. The optional **weekly Ocado** workflow
replaces the next booked order with private saved staples plus selected
[recipes](docs/recipes.md), and sends a checkout link.

The deployment extends the official Hermes image with a separately locked Python MCP
package. Docker Compose keeps the gateway running. `data/` contains configuration,
keys, WhatsApp pairing, conversations, and memory; it is excluded from Git and image
builds. Nothing publishes a public port or mounts the host's Docker socket.

## First deployment

1. Create an Ubuntu 24.04 LTS droplet with an SSH key. Start with 2 vCPU / 4 GB RAM
   as a conservative baseline for remote model inference and room for future browser
   tools; measure usage before resizing. This does not host a language model.
2. Add a DigitalOcean Cloud Firewall: inbound TCP 22 from your IP only (IPv4 and IPv6
   as appropriate), and allow outbound traffic. No WhatsApp webhook port is needed.
3. Follow the [Ubuntu initial server setup](https://www.digitalocean.com/community/tutorials/initial-server-setup-with-ubuntu)
   to create a sudo user. Install Git, Make, and Docker Engine with its Compose plugin
   using [Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
   Run this repo as that user, not root. Docker access is required for the commands below.
4. Copy this repository to the droplet (or clone it after creating a private Git remote).
   Keep it at a stable path such as `~/hermes-home`.

Over SSH, from the repository directory:

```sh
make init
make build
make setup
make pair
```

`make setup` prompts for your model provider, model and credentials. It saves them in
`data/`. Choose whichever provider you already use. `make pair` displays a QR code:
scan it from the bot account in WhatsApp → Settings → Linked Devices. A dedicated bot
number lets you message it from your usual account. Hermes also supports self-chat;
choose that mode in the pairing wizard and use `WHATSAPP_MODE=self-chat` below.

Edit `data/.env`, using `config/whatsapp.env.example` as a reference. Set your sender
number in `WHATSAPP_ALLOWED_USERS` (country code included, no `+` or punctuation),
enable WhatsApp, and keep allow-all disabled. Groups remain excluded by the default
pairing policy. Verify `data/config.yaml` still has the household MCP entry and
`whatsapp.unauthorized_dm_behavior: ignore` after the setup wizard.

```sh
make up
make status
make logs
```

Send the bot a message from the allowed phone: "Use household_status to check your
tools." Expect server `ok`, with Yoto reported as not authorised until connected and
Ocado as not connected until its session is added. Restart
the container and verify WhatsApp still works without re-pairing. Model usage is billed
by your provider separately from the droplet.

The built-in bridge uses Baileys/WhatsApp Web, not the official Business API. Hermes
documents an account restriction risk and recommends a dedicated number. See the
[WhatsApp guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp).

## Local development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
make check
uv run household-tools
```

The last command speaks MCP on stdio; use an MCP client rather than typing chat into
it. `make check` starts a real client/server session and calls the status tool without
cloud credentials or an LLM. See [integration guidance](docs/integrations.md).

`make init` is idempotent: it copies missing templates and records your UID/GID for
the official image's runtime user. Tracked template changes do not update an existing
`data/` directory automatically; apply those changes deliberately and restart.

## Operations

- `make down` stops the deployment without deleting `data/`.
- `make doctor` runs Hermes diagnostics in the running container.
- Before re-pairing or running the setup wizard again, stop the gateway with `make down`.
  Avoid concurrent gateway containers sharing the same state.
- Back up the complete `data/` directory while the gateway is stopped. For example,
  run `make down`, then `tar -czf backups/hermes-data-YYYYMMDD.tar.gz data`, then
  `make up`. Store an encrypted copy off the droplet and test restoration. Backups
  contain credentials and account access; never commit or share them.
- Restore into an empty `data/` while stopped, keep permissions private, and check
  the UID/GID in `.env`. Restore the matching image version too.
- For upgrades, back up first, record the current image, rebuild, then run `make up`
  and repeat the WhatsApp/tool smoke test. Keep the prior image and matching backup
  for rollback. Do not run `hermes update` inside an immutable image.

The starter pins the official stable image's multi-architecture digest, resolved on
2026-10-12, and the uv build image. Before an upgrade, resolve the new stable digest
with `docker buildx imagetools inspect nousresearch/hermes-agent:stable`, update
`HERMES_IMAGE` in `.env`, rebuild and smoke-test. Record the tested reference in your
deployment notes and update the tracked defaults when adopting it for new installs.
The Python tool dependencies are pinned in `uv.lock`.

Upstream references: [Hermes Docker guide](https://hermes-agent.nousresearch.com/docs/user-guide/docker),
[MCP configuration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
[DigitalOcean droplet creation](https://docs.digitalocean.com/products/droplets/how-to/create/).

## Repository status

This is an initial deployment scaffold. Creating a remote repository, provisioning
the droplet, selecting a model, pairing WhatsApp and authenticating household services
are separate setup steps. No cloud resources are provisioned by this repo.
