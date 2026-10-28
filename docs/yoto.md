# Connect Yoto

This integration uses Yoto's official OAuth/PKCE login, REST player discovery, and
MQTT-over-TLS player controls. It supports listing players, live status, pause/resume/stop
and capped volume. Starting a specific card and browsing the library are not implemented
yet. Playback delivery is acknowledged by the broker; it is not proof the player acted.
Optional day/night tools read/change daily morning and bedtime settings, and offer
temporary sun/moon shortcuts with durable restoration of the usual schedule.

## 1. Register a personal app

At <https://dashboard.yoto.dev/>, create a **public client**, with callback URL
`http://127.0.0.1:8787/callback`. Leave logout URLs blank. Select scopes:

- `family:library:view` (reserved for future card browsing)
- `family:devices:view`
- `family:devices:control`
- `family:devices:manage` (for morning/bedtime settings and temporary sun/moon)
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
contains the Yoto tool names in `config/config.yaml`, then recreate the gateway. Unknown alias:
use the alias returned by player discovery or run `make yoto-configure` while stopped.
Expired/revoked login: stop the gateway and repeat the tunnel and login steps.

If direct player discovery succeeds in the container but the MCP tool reports not
authorised, update/rebuild and run `make yoto-configure` while the gateway is stopped.
The household MCP entry must explicitly set `env.HERMES_HOME` to the active profile
directory (`/opt/data` in this deployment). Hermes filters its MCP subprocess environment,
so inheriting this variable implicitly does not work. Existing tokens need no new login.

## Enable morning/bedtime settings and temporary sun/moon shortcuts

In your existing app at <https://dashboard.yoto.dev/>, enable
`family:devices:manage` while retaining the current scopes. This additional permission
is required for both reading and changing device configuration.

On the droplet:

```sh
git pull --ff-only
make build
make down
```

Open the same Mac SSH tunnel used for the original login (section 3), then run on the
droplet:

```sh
make yoto-login
make yoto-enable-times
make up
```

Use the existing client ID, open the printed URL on your Mac and grant the new
permission. There is no need to reselect players: `yoto-enable-times` preserves aliases,
volume ceilings, model, WhatsApp, Ocado and Google Calendar settings. It checks read
access, enables four tools and appends shortcut guidance; it changes no player settings.
If setup fails, run `make up` to bring the existing gateway back, then resolve the error.
Close the SSH tunnel after login completes. Send `/reload-mcp` on WhatsApp.

Use these phrases:

- **Yoto morning** or **Yoto sun**: temporarily select day mode now.
- **Yoto night** or **Yoto moon**: temporarily select night mode now.
- **Restore Felix's usual Yoto schedule**: end an override early.
- **What are Felix's morning and bedtime times?**: read saved times and any override.
- **Set Felix's morning to 07:00 and bedtime to 19:30**: explicitly change permanent
  daily times. If an override is active, restore the usual times first.

With one allowed player, shortcuts use it automatically. With several, specify the
alias. Shortcuts do not require another confirmation or a time from you. They calculate
the current time in the player's configured time zone and temporarily adjust its mode
boundary. A player already in the requested scheduled mode needs no change. A player
must be online for a shortcut; cloud restoration can proceed while it is offline.

The original times are restored at the **next usual morning or bedtime boundary**,
whichever comes first. For example, with usual times 07:00/19:30, “Yoto sun” at 06:00
restores normal settings at 07:00; “Yoto moon” at 18:00 restores at 19:30. A repeat
shortcut retains the original schedule, rather than saving the override as normal.
Requesting the opposite mode can also restore the normal schedule early.

The `yoto-schedule-worker` Compose service checks saved restoration tasks every 15
seconds. Tasks live privately in `data/yoto-overrides/`, are saved before the remote
update, and survive chat/container/droplet restarts. The worker must be healthy before
a new temporary override is allowed. If the deployment is stopped or Yoto is unreachable,
restoration waits until service resumes; the modified settings remain saved meanwhile.
The reply reports verified saved settings and a restore time, not proof that the player
display changed instantly. Allow time for Yoto's player to sync its cloud configuration.

Restoration reads fresh settings and changes only the two mode times, preserving later
unrelated app edits. If you changed the morning/bedtime or player time zone in the app,
your new schedule wins and the old restore task is cancelled. An uncertain initial
update retains the restore record; never loop the initial shortcut after an error.

Check worker health with:

```sh
docker compose ps yoto-schedule-worker
docker compose logs --tail=30 yoto-schedule-worker
```

These are day/night mode boundaries, not alarms, playback commands or weekday schedules.
No live schedule mutation is run by the repository's automated tests.

## Diagnose a persistent status connection error

For a connection reset on versions before the WebSocket TLS fix, update, rebuild and
recreate the gateway first. The integration previously advertised `x-amzn-mqtt-ca` in
TLS ALPN. AWS assigns this to raw MQTT with client certificates, not the HTTP WebSocket
upgrade used here. A credential-free probe reproduced a TLS connection reset with that
setting and HTTP 101 Switching Protocols with `http/1.1`. The integration now advertises
`http/1.1`; server certificate and hostname verification stay enabled. This transport
probe confirms the handshake, not account authorisation or successful player status.

Player discovery uses HTTPS; live status uses a separate MQTT-over-WebSocket connection.
A successful player list does not prove the status connection works or that its control
scope was granted. Repeated connection failures are not evidence of a transient outage.

After updating and rebuilding, restart the gateway, then run:

```sh
make yoto-check
```

This runs inside the existing container with its active profile. It prints discovery
success, each configured alias's online flag and whether its live status can be read.
It does not print tokens, device IDs or status contents, and does not change playback.
Avoid running it concurrently with other Yoto requests: Yoto uses a player-specific MQTT
client ID, so overlapping connections can displace each other.

The updated status tool also reports DNS, certificate/TLS, WebSocket upgrade and MQTT
authentication failures separately. The previous asynchronous connection path reduced
these to “Could not connect to Yoto's player service.” A WebSocket upgrade error happens
before MQTT authentication, so do not assume reauthorising will fix it. DNS/network errors
need investigation from the droplet; TLS verification remains enabled. Authentication
rejection needs a check of the app's `family:devices:control` grant and login. A status
timeout after connection/subscription is a different failure: check the player itself.

Sources:
- <https://yoto.dev/authentication/headless-cli-auth/>
- <https://yoto.dev/authentication/scopes/>
- <https://yoto.dev/api/devices/getdevices/>
- <https://yoto.dev/players-mqtt/connecting-to-players/>
- <https://yoto.dev/players-mqtt/getting-player-status/>
- <https://yoto.dev/players-mqtt/mqtt-docs/>
- <https://docs.aws.amazon.com/iot/latest/developerguide/protocols.html>
