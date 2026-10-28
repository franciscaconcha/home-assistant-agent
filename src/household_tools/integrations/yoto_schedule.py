"""Narrow day/night settings tools, using Yoto's official configuration API."""

import fcntl
import os
import re
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import httpx

from household_tools.integrations import yoto

SCOPE = "family:devices:manage"


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise yoto.YotoError("Use a local 24-hour time in HH:MM format, for example 07:00 or 19:30.")
    return value


def read_config(device_id):
    value = yoto.api_get(f"/device-v2/{device_id}/config", permission=SCOPE)
    try:
        device = value["device"]
        config = device["config"]
        if not isinstance(config, dict):
            raise ValueError
        clock(config["dayTime"])
        clock(config["nightTime"])
        zone = device["geoTimezone"]
        ZoneInfo(zone)
        return config, zone
    except (KeyError, TypeError, ValueError):
        raise yoto.YotoError("Yoto did not return recognised morning/bedtime settings and a player time zone; no settings update can be confirmed.") from None


def public_settings(alias, config, zone):
    return {"player": alias, "morning_time": config["dayTime"],
            "bedtime": config["nightTime"], "time_zone": zone,
            "note": "Saved day/night mode boundaries in the player's local time; these are not alarms."}


def get_schedule(alias: str) -> dict:
    player, _ = yoto.selected_player(alias, require_online=False)
    config, zone = read_config(player["device_id"])
    result = public_settings(alias, config, zone)
    from household_tools.integrations import yoto_override
    record = yoto_override.load_record(player["device_id"])
    if record:
        result["temporary_override"] = {"mode": record["mode"], "restore_at": record["restore_at"],
            "usual_morning": record["usual"]["dayTime"], "usual_bedtime": record["usual"]["nightTime"]}
    return result


@contextmanager
def config_lock(device_id):
    lock_path = yoto.home() / ".locks" / f"yoto-schedule-{device_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_config(player, wanted, zone):
    device_id = player["device_id"]
    token = yoto.access_token()
    uncertain = False
    try:
        response = httpx.put(yoto.API + f"/device-v2/{device_id}/config",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": player["name"], "config": wanted}, timeout=15)
        if response.status_code in {401, 403}:
            raise yoto.YotoError(f"Yoto rejected the settings update (HTTP {response.status_code}). Enable {SCOPE} and run make yoto-login again.")
        uncertain = response.status_code != 200
    except httpx.HTTPError:
        uncertain = True
    try:
        actual, actual_zone = read_config(device_id)
    except yoto.YotoError:
        raise yoto.YotoError("The settings update could not be verified. Check the morning/bedtime in the Yoto app before retrying; do not automatically repeat the update.") from None
    if actual != wanted or actual_zone != zone:
        raise yoto.YotoError("Yoto's saved settings do not match the requested update or other settings changed. Check the Yoto app; do not automatically retry or restore an old configuration.")
    return "verified_after_uncertain_response" if uncertain else "updated"


def set_schedule(alias: str, morning_time: str | None = None, bedtime: str | None = None) -> dict:
    changes = {}
    if morning_time is not None:
        changes["dayTime"] = clock(morning_time)
    if bedtime is not None:
        changes["nightTime"] = clock(bedtime)
    if not changes:
        raise yoto.YotoError("Specify a morning time, bedtime, or both.")
    player, _ = yoto.selected_player(alias, require_online=False)
    device_id = player["device_id"]
    with config_lock(device_id):
        from household_tools.integrations import yoto_override
        if yoto_override.load_record(device_id):
            raise yoto.YotoError("A temporary override is active. Restore the usual schedule with yoto_restore_day_night_times before changing permanent times.")
        before, zone = read_config(device_id)
        wanted = {**before, **changes}
        if wanted["dayTime"] == wanted["nightTime"]:
            raise yoto.YotoError("Morning and bedtime must be different local times.")
        if wanted == before:
            return {"status": "already_set", **public_settings(alias, before, zone)}
        status = write_config(player, wanted, zone)
        return {"status": status, **public_settings(alias, wanted, zone),
                "note": "Verified saved Yoto settings. The player applies these when connected; no alarm was created."}
