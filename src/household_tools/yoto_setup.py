"""Operator-only Yoto authentication and configuration commands."""

import argparse
import base64
import hashlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import yaml

from household_tools.integrations import yoto
from household_tools.integrations import yoto_schedule

CALLBACK = "http://127.0.0.1:8787/callback"
TOOLS = ["household_status", "yoto_list_players", "yoto_player_status", "yoto_playback", "yoto_set_volume"]
SCHEDULE_TOOLS = ["yoto_get_day_night_times", "yoto_set_day_night_times", "yoto_set_mode_now", "yoto_restore_day_night_times"]
SCHEDULE_GUIDANCE = """\n\n<!-- household-yoto-day-night-v1 -->
Use yoto_get_day_night_times to read the configured player's daily morning/bedtime
and local time zone. Use yoto_set_day_night_times for an explicit request to change
these times; no extra confirmation is needed when the player and times are clear.
Times are local 24-hour HH:MM; ask about ambiguous am/pm. These are persistent daily
day/night mode settings, not alarms.
For 'yoto morning' or 'yoto sun', immediately call yoto_set_mode_now(mode='sun');
for 'yoto night' or 'yoto moon', call it with mode='moon'. These shortcuts are TEMPORARY:
the worker restores normal times at the next usual morning/bedtime boundary. Do not
change permanent times for a shortcut, ask for a time, or ask for extra confirmation.
When only one player is configured, omit player. Say the usual schedule resumes and
give restore_at in the returned local time zone. Never promise an instant display change.
Use yoto_restore_day_night_times for 'restore usual Yoto schedule' or before an explicit
permanent time change when an override is active. Weekday schedules are unsupported.
Report verified saved settings; do not claim immediate device application while offline.
After an uncertain update, read/check the saved settings instead of automatically retrying.
"""


def enable_schedule() -> None:
    """Enable settings tools without changing existing player aliases or volume caps."""
    try:
        players = [p for p in yoto.list_players() if p.get("alias")]
        if not players:
            raise yoto.YotoError("No allowed players configured. Run make yoto-configure first.")
        for player in players:
            yoto_schedule.get_schedule(player["alias"])  # Read-only permission check.
        path = yoto.home() / "config.yaml"
        original = path.read_text()
        config = yaml.safe_load(original) or {}
        entry = config.setdefault("mcp_servers", {}).setdefault("household", {})
        entry.setdefault("command", "/opt/household-tools/.venv/bin/household-tools")
        entry.setdefault("env", {})["HERMES_HOME"] = str(yoto.home())
        entry["timeout"] = max(entry.get("timeout", 90), 120)
        tools = entry.setdefault("tools", {})
        if "include" in tools:
            tools["include"] = list(dict.fromkeys(tools["include"] + SCHEDULE_TOOLS))
        elif not tools:
            tools["include"] = TOOLS + SCHEDULE_TOOLS
        if "exclude" in tools:
            tools["exclude"] = [name for name in tools["exclude"] if name not in SCHEDULE_TOOLS]
        entry.setdefault("sampling", {"enabled": False})
        backup = path.with_name(f"config.before-yoto-times-{time.time_ns()}.yaml")
        backup.touch(mode=0o600, exist_ok=False)
        backup.write_text(original)
        yoto.write_private(path, config)
        soul = yoto.home() / "SOUL.md"
        previous = soul.read_text() if soul.exists() else ""
        if "<!-- household-yoto-day-night-v1 -->" not in previous:
            if soul.exists():
                backup = soul.with_name(f"SOUL.before-yoto-times-{time.time_ns()}.md")
                backup.touch(mode=0o600, exist_ok=False)
                backup.write_text(previous)
            soul.touch(mode=0o600, exist_ok=True)
            soul.write_text(previous + SCHEDULE_GUIDANCE)
        print("Yoto day/night permission verified and tools enabled. No player settings changed.")
        print("Existing aliases, volume caps and other integrations preserved. Run make up, then /reload-mcp on WhatsApp.")
    except (yoto.YotoError, OSError, ValueError, TypeError) as error:
        raise SystemExit(str(error) if isinstance(error, yoto.YotoError) else "Could not enable Yoto time tools; check data/config.yaml and permissions.") from None


def check() -> None:
    """Read discovery and live status separately; never change playback."""
    try:
        players = yoto.list_players()
    except yoto.YotoError as error:
        raise SystemExit(f"Player discovery failed: {error}") from None
    print("Player discovery succeeded.", flush=True)
    configured = [player for player in players if player.get("alias")]
    if not configured:
        raise SystemExit("No configured player aliases found. Run make yoto-configure while the gateway is stopped.")
    failed = False
    for player in configured:
        alias = player["alias"]
        print(f"{alias}: online={bool(player['online'])}", flush=True)
        try:
            yoto.player_status(alias)
            print(f"{alias}: live status read succeeded.", flush=True)
        except yoto.YotoError as error:
            failed = True
            print(f"{alias}: live status read failed: {error}", flush=True)
    print("No playback change was requested.", flush=True)
    if failed:
        raise SystemExit(1)


def login_url(client_id: str, verifier: str, state: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://login.yotoplay.com/authorize?" + urlencode({
        "client_id": client_id, "audience": yoto.API, "response_type": "code",
        "scope": yoto.SCOPES, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })


def callback_value(path: str, state: str) -> str | None:
    url = urlparse(path)
    query = parse_qs(url.query)
    if url.path != "/callback" or not secrets.compare_digest(query.get("state", [""])[0], state):
        raise yoto.YotoError("Invalid login callback or state.")
    if "error" in query:
        raise yoto.YotoError("Yoto authorisation was declined; run login again when ready.")
    return query.get("code", [None])[0]


def login() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    args = parser.parse_args()
    client_id = input("Yoto public client ID: ").strip()
    if not client_id:
        raise SystemExit("A client ID is required.")
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    outcome = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                code = callback_value(self.path, state)
                if not code:
                    raise yoto.YotoError("No code returned.")
                outcome["code"] = code
                status, message = 200, "Authorisation received. Return to the terminal to check completion."
            except yoto.YotoError:
                status, message = 400, "Login callback rejected. Use the original login link from the terminal."
                query = parse_qs(urlparse(self.path).query)
                if secrets.compare_digest(query.get("state", [""])[0], state) and "error" in query:
                    outcome["error"] = "Yoto authorisation was declined. Run make yoto-login again when ready."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message.encode())

        def log_message(self, *args):
            pass  # Callback query strings contain short-lived authorisation codes.

    try:
        with HTTPServer((args.bind, 8787), Handler) as server:
            server.timeout = 1
            print("Open this URL in your Mac browser (keep the SSH tunnel open):", flush=True)
            print(login_url(client_id, verifier, state), flush=True)
            deadline = time.monotonic() + 300
            while not outcome and time.monotonic() < deadline:
                server.handle_request()
        if "error" in outcome:
            raise yoto.YotoError(outcome["error"])
        if "code" not in outcome:
            raise yoto.YotoError("Login timed out after five minutes. Run make yoto-login again.")
        with yoto.token_lock():
            value = yoto.token_request({"grant_type": "authorization_code", "client_id": client_id,
                                       "code_verifier": verifier, "code": outcome["code"],
                                       "redirect_uri": CALLBACK})
            value["client_id"] = client_id
            yoto.write_private(yoto.token_path(), value)
        print("Yoto authorised. Tokens saved privately; next run make yoto-configure.")
    except (yoto.YotoError, OSError) as error:
        raise SystemExit(str(error) if isinstance(error, yoto.YotoError) else "Could not open/save the login callback. Check port 8787 and data permissions.") from None


def configure() -> None:
    try:
        devices = yoto.list_players()
        if not devices:
            raise yoto.YotoError("No players were found in the authorised Yoto account.")
        for number, device in enumerate(devices, 1):
            print(f"{number}. {device['name']} ({'online' if device['online'] else 'offline'})")
        selected = input("Player numbers to allow, comma-separated (for example 1,2): ")
        indexes = [int(item.strip()) - 1 for item in selected.split(",")]
        if not indexes or len(set(indexes)) != len(indexes) or any(i < 0 or i >= len(devices) for i in indexes):
            raise yoto.YotoError("Choose valid distinct player numbers.")
        players = {}
        for index in indexes:
            alias = input(f"Short alias for {devices[index]['name']} (for example bedroom): ").strip()
            if not alias or alias in players:
                raise yoto.YotoError("Choose a non-empty, unique player alias.")
            maximum = int(input(f"Maximum volume percentage for {alias} [30]: ") or "30")
            if not 0 <= maximum <= 100:
                raise yoto.YotoError("Maximum volume must be from 0 to 100.")
            players[alias] = {"device_id": devices[index]["device_id"], "max_volume": maximum}

        path = yoto.home() / "config.yaml"
        original = path.read_text()
        config = yaml.safe_load(original) or {}
        server = config.setdefault("mcp_servers", {}).setdefault("household", {})
        server.setdefault("command", "/opt/household-tools/.venv/bin/household-tools")
        # MCP strips HERMES_HOME unless explicitly configured. Login helpers inherit it,
        # so both paths must be pinned to the same active profile directory.
        server.setdefault("env", {})["HERMES_HOME"] = str(yoto.home())
        server["timeout"] = max(server.get("timeout", 30), 90)
        tools = server.setdefault("tools", {})
        if "include" in tools:
            tools["include"] = list(dict.fromkeys(tools["include"] + TOOLS))
        # Preserve an existing unfiltered tool configuration; new installs get a narrow list.
        elif not tools:
            tools["include"] = TOOLS.copy()
        server.setdefault("sampling", {"enabled": False})
        stamp = time.time_ns()
        # Backups remain private and outside Git alongside all runtime state.
        backup = path.with_name(f"config.before-yoto-{stamp}.yaml")
        backup.touch(mode=0o600, exist_ok=False)
        backup.write_text(original)
        yoto.write_private(yoto.home() / "yoto-players.json", {"players": players})
        # JSON is valid YAML and allows reuse of the atomic private writer.
        yoto.write_private(path, config)
        print("Players and MCP tools configured; existing model and WhatsApp settings preserved.")
        print("Run make up, then ask on WhatsApp: List my Yoto players.")
    except (yoto.YotoError, ValueError, OSError, TypeError) as error:
        raise SystemExit(str(error) if isinstance(error, yoto.YotoError) else "Could not configure Yoto; check selections and data/config.yaml.") from None
