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

CALLBACK = "http://127.0.0.1:8787/callback"
TOOLS = ["household_status", "yoto_list_players", "yoto_player_status", "yoto_playback", "yoto_set_volume"]


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
