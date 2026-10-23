# Connect Yoto

This integration uses Yoto's official OAuth/PKCE login, REST player discovery, and
MQTT-over-TLS player controls. It supports listing players, live status, pause/resume/stop
and capped volume. Starting a specific card and browsing the library are not implemented
yet. Playback delivery is acknowledged by the broker; it is not proof the player acted.

## 1. Register a personal app

At <https://dashboard.yoto.dev/>, create a **public client**, with callback URL
`http://127.0.0.1:8787/callback`. Leave logout URLs blank. Select scopes:

- `family:library:view` (reserved for future card browsing)
- `family:devices:view`
- `family:devices:control`
- `offline_access`

No client secret is used. The app remains unverified for personal use; Yoto shows a
consent warning. Keep the issued refresh/access tokens private.

## 2. Update the droplet

From `~/hermes-home` on the droplet:

```sh
git pull --ff-only
make build
make down
```

Stopping here prevents config and token changes racing with the live agent. Your model,
WhatsApp pairing, memories, and other runtime data remain under `data/`.

## 3. Forward the local login callback

In a **separate Mac Terminal**, open an SSH tunnel and leave it running:

```sh
ssh -i ~/.ssh/hermes_digitalocean -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8787:127.0.0.1:8787 hermes@YOUR_DROPLET_IP
```

Adjust your SSH key path if different. There is no public callback port and no firewall
change. The temporary login container publishes port 8787 on the droplet's loopback only.

In the **droplet terminal**:

```sh
make yoto-login
```

Paste your public client ID at its prompt. Open the printed Yoto login URL in your **Mac
browser**. Sign in to the Yoto account that owns the household players and grant the
requested permissions. Wait for the terminal to say **Yoto authorised**. The callback
screen alone does not confirm the token exchange succeeded. Login expires after five
minutes; repeat the command if necessary.

Tokens are stored at `data/credentials/yoto.json` with owner-only permissions. A private
lock file serializes token refresh; Yoto refresh tokens rotate after each use. Do not copy
these tokens into WhatsApp or Git. When the command exits, close the SSH tunnel with Ctrl+C.

## 4. Select players and enable tools

```sh
make yoto-configure
```

Select the displayed player numbers, assign short aliases such as `bedroom`, and set each
maximum volume percentage. The default ceiling is 30%; choose an appropriate limit for
your household. This saves `data/yoto-players.json` and updates only the household MCP
entry in the live config. The previous config is backed up privately alongside it. Existing
WhatsApp and model settings are preserved; YAML formatting may change.

Tools cannot send commands to unselected players or exceed the configured maximum. The
shared agent's shell can edit its runtime files; this is not isolation from an agent with
shell access. Use a separate constrained service if that boundary is needed.

```sh
make up
make logs
```

## 5. Test over WhatsApp

First ask **List my Yoto players and their configured aliases**. Check that the expected
players appear. Then ask **What is the bedroom player's battery level?**

When ready to affect playback, try **Pause the bedroom Yoto** or **Set the bedroom Yoto
to 20 percent volume**. Commands connect through Yoto's cloud, so the player must be online.
This repository's tests use mocks; verify your own account and player behavior live before
relying on the integration.

If a tool is missing, check `data/config.yaml` → `mcp_servers.household.tools.include`
contains the five names in `config/config.yaml`, then recreate the gateway. Unknown alias:
use the alias returned by player discovery or run `make yoto-configure` while stopped.
Expired/revoked login: stop the gateway and repeat the tunnel and login steps.

If direct player discovery succeeds in the container but the MCP tool reports not
authorised, update/rebuild and run `make yoto-configure` while the gateway is stopped.
The household MCP entry must explicitly set `env.HERMES_HOME` to the active profile
directory (`/opt/data` in this deployment). Hermes filters its MCP subprocess environment,
so inheriting this variable implicitly does not work. Existing tokens need no new login.

Sources:
- <https://yoto.dev/authentication/headless-cli-auth/>
- <https://yoto.dev/authentication/scopes/>
- <https://yoto.dev/api/devices/getdevices/>
- <https://yoto.dev/players-mqtt/connecting-to-players/>
- <https://yoto.dev/players-mqtt/getting-player-status/>
- <https://yoto.dev/players-mqtt/mqtt-docs/>
