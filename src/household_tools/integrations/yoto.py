"""Yoto cloud API and short-lived MQTT connections. Never return credentials."""

import fcntl
import json
import os
import re
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import paho.mqtt.client as mqtt

API = "https://api.yotoplay.com"
TOKEN_URL = "https://login.yotoplay.com/oauth/token"
BROKER = "aqrphjqbp3u2z-ats.iot.eu-west-2.amazonaws.com"
SCOPES = "family:library:view family:devices:view family:devices:control offline_access"


class YotoError(ValueError):
    """An actionable error that is safe to show in chat."""


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def token_path() -> Path:
    return home() / "credentials" / "yoto.json"


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def token_lock():
    path = token_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def token_request(form: dict) -> dict:
    try:
        response = httpx.post(TOKEN_URL, data=form, timeout=15)
        if response.status_code != 200:
            raise YotoError(f"Yoto login/refresh failed (HTTP {response.status_code}); run make yoto-login.")
        value = response.json()
        if not value.get("access_token") or not value.get("refresh_token"):
            raise YotoError("Yoto did not return both tokens; check offline_access and sign in again.")
        return {
            "access_token": value["access_token"],
            "refresh_token": value["refresh_token"],
            "expires_at": time.time() + float(value.get("expires_in", 3600)),
            "scope": value.get("scope", SCOPES),
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, YotoError):
            raise
        raise YotoError("Yoto token request failed; check connectivity or sign in again.") from None


def access_token(force: bool = False) -> str:
    # Refresh tokens rotate on every use; serialize read/refresh/write across processes.
    with token_lock():
        try:
            value = json.loads(token_path().read_text())
            if force or value["expires_at"] <= time.time() + 60:
                refreshed = token_request({
                    "grant_type": "refresh_token", "client_id": value["client_id"],
                    "refresh_token": value["refresh_token"],
                })
                value.update(refreshed)
                write_private(token_path(), value)
            return value["access_token"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, YotoError):
                raise
            raise YotoError("Yoto is not authorised. Run make yoto-login on the droplet.") from None


def api_get(path: str) -> dict:
    token = access_token()
    try:
        response = httpx.get(API + path, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if response.status_code == 401:
            token = access_token(force=True)
            response = httpx.get(API + path, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if response.status_code != 200:
            raise YotoError(f"Yoto API returned HTTP {response.status_code}; check account permissions.")
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, YotoError):
            raise
        raise YotoError("Yoto API could not be reached or returned an invalid response.") from None


def list_players() -> list[dict]:
    devices = api_get("/device-v2/devices/mine").get("devices")
    if not isinstance(devices, list):
        raise YotoError("Yoto returned an unexpected player list.")
    players = [
        {"device_id": item["deviceId"], "name": item.get("name", "Unnamed player"),
         "online": item.get("online", False)}
        for item in devices if isinstance(item, dict) and item.get("deviceId")
    ]
    try:
        allowed = json.loads((home() / "yoto-players.json").read_text())["players"]
    except (OSError, ValueError, KeyError):
        allowed = {}
    if not isinstance(allowed, dict):
        allowed = {}
    for player in players:
        for alias, setting in allowed.items():
            if isinstance(setting, dict) and setting.get("device_id") == player["device_id"]:
                player.update(alias=alias, max_volume=setting.get("max_volume"))
    return players


def selected_player(alias: str) -> tuple[dict, int]:
    try:
        config = json.loads((home() / "yoto-players.json").read_text())
        item = config["players"][alias]
        device_id, maximum = item["device_id"], item["max_volume"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", device_id):
            raise ValueError
        if type(maximum) is not int or not 0 <= maximum <= 100:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise YotoError("Player alias is not configured. Run make yoto-configure; use an exact configured alias.") from None
    player = next((p for p in list_players() if p["device_id"] == device_id), None)
    if player is None:
        raise YotoError("The configured player is not in the authorised Yoto account.")
    if not player["online"]:
        raise YotoError("The selected Yoto player is offline.")
    return player, maximum


def mqtt_request(device_id: str, command: str, payload: str, read_status: bool = False) -> dict:
    """Publish once; acknowledge broker delivery without claiming device execution."""
    connected, received = threading.Event(), threading.Event()
    errors, result = [], {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"DASH{device_id}",
                         transport="websockets", reconnect_on_failure=False)
    client.connect_timeout = 8
    context = ssl.create_default_context()
    context.set_alpn_protocols(["x-amzn-mqtt-ca"])
    client.tls_set_context(context)
    client.ws_set_options(path="/mqtt")
    client.username_pw_set(f"{device_id}?x-amz-customauthorizer-name=PublicJWTAuthorizer", access_token())
    topic = f"device/{device_id}/data/status"

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            errors.append("Yoto MQTT authentication failed; try signing in again.")
        connected.set()

    def on_connect_fail(c, userdata):
        errors.append("Could not connect to Yoto's player service.")
        connected.set()

    def on_subscribe(c, userdata, mid, reasons, properties):
        if any(reason.is_failure for reason in reasons):
            errors.append("Yoto denied the status subscription.")
            received.set()
        else:
            c.publish(f"device/{device_id}/command/status/request", "", qos=1)

    def on_message(c, userdata, message):
        if message.topic == topic:
            try:
                status = json.loads(message.payload)["status"]
                if not isinstance(status, dict):
                    raise ValueError
                result.update(status)
            except (ValueError, KeyError, TypeError):
                errors.append("Yoto returned an invalid status response.")
            received.set()

    client.on_connect, client.on_connect_fail = on_connect, on_connect_fail
    client.on_subscribe, client.on_message = on_subscribe, on_message
    try:
        client.connect_async(BROKER, 443, keepalive=60)
        client.loop_start()
        if not connected.wait(10):
            raise YotoError("Timed out connecting to Yoto's player service.")
        if errors:
            raise YotoError(errors[0])
        if read_status:
            rc, _ = client.subscribe(topic, qos=1)
            if rc != mqtt.MQTT_ERR_SUCCESS or not received.wait(10):
                raise YotoError("Timed out waiting for the player status; check it is online.")
            if errors:
                raise YotoError(errors[0])
            return result
        info = client.publish(f"device/{device_id}/command/{command}", payload, qos=1)
        info.wait_for_publish(timeout=8)
        if not info.is_published():
            raise YotoError("Command delivery could not be confirmed; check the player before retrying.")
        return {"sent": True, "execution_confirmed": False,
                "note": "Broker accepted the command; player execution is not confirmed."}
    except (OSError, RuntimeError) as error:
        raise YotoError("Yoto connection failed; command execution is unknown. Check the player before retrying.") from None
    finally:
        client.disconnect()
        client.loop_stop()


def player_status(alias: str) -> dict:
    player, _ = selected_player(alias)
    return mqtt_request(player["device_id"], "status/request", "", read_status=True)


def playback(alias: str, action: str) -> dict:
    if action not in {"pause", "resume", "stop"}:
        raise YotoError("Playback action must be pause, resume or stop.")
    player, _ = selected_player(alias)
    return mqtt_request(player["device_id"], f"card/{action}", "{}")


def set_volume(alias: str, volume: int) -> dict:
    # Validate before any network call; use each player's configured ceiling.
    if type(volume) is not int or not 0 <= volume <= 100:
        raise YotoError("Volume must be an integer percentage from 0 to 100.")
    player, maximum = selected_player(alias)
    if volume > maximum:
        raise YotoError(f"Volume exceeds this player's configured maximum of {maximum}%.")
    return mqtt_request(player["device_id"], "volume/set", json.dumps({"volume": volume}))
