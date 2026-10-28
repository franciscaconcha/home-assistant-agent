"""Durable temporary sun/moon overrides, restored by the deployment worker."""

import json
import signal
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from household_tools.integrations import yoto, yoto_schedule as schedule


def now():
    return datetime.now(timezone.utc)


def record_path(device_id):
    return yoto.home() / "yoto-overrides" / f"{device_id}.json"


def load_record(device_id):
    path = record_path(device_id)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        for key in ("usual", "applied"):
            for field in ("dayTime", "nightTime"):
                schedule.clock(value[key][field])
        if datetime.fromisoformat(value["restore_at"]).tzinfo is None:
            raise ValueError
        ZoneInfo(value["time_zone"])
        if (value["device_id"] != device_id or value["mode"] not in {"sun", "moon"}
                or not isinstance(value.get("alias"), str)):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise yoto.YotoError("The temporary Yoto restore record needs review; no settings were changed.") from None


def times(config):
    return {key: config[key] for key in ("dayTime", "nightTime")}


def choose_player(alias):
    if alias is None:
        players = [p for p in yoto.list_players() if p.get("alias")]
        if len(players) != 1:
            raise yoto.YotoError("Specify which configured Yoto player to change.")
        alias = players[0]["alias"]
    player, _ = yoto.selected_player(alias)
    return alias, player


def next_boundary(usual, zone, current):
    # Walk real UTC minutes, handling repeated/missing local minutes at clock changes.
    candidate = current.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for _ in range(60 * 51):
        candidate += timedelta(minutes=1)
        if candidate.astimezone(ZoneInfo(zone)).strftime("%H:%M") in usual.values():
            return candidate.isoformat()
    raise yoto.YotoError("Could not determine the next usual morning/bedtime boundary.")


def mode_at(config, local):
    day, night, current = config["dayTime"], config["nightTime"], local.strftime("%H:%M")
    if day == night:
        raise yoto.YotoError("The usual morning and bedtime are identical; set distinct times before using a temporary override.")
    daytime = day <= current < night if day < night else current >= day or current < night
    return "sun" if daytime else "moon"


def heartbeat_path():
    return yoto.home() / "yoto-override-worker.json"


def require_worker():
    try:
        health = json.loads(heartbeat_path().read_text())
        if not 0 <= time.time() - health["heartbeat"] < 90:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise yoto.YotoError("The Yoto restore worker is not running/healthy. Run make up and check docker compose logs yoto-schedule-worker before requesting a temporary override.") from None


def switch_now(mode: str, alias: str | None = None):
    mode = {"morning": "sun", "night": "moon"}.get(mode, mode)
    if mode not in {"sun", "moon"}:
        raise yoto.YotoError("Choose sun/morning or moon/night.")
    require_worker()
    alias, player = choose_player(alias)
    device_id = player["device_id"]
    with schedule.config_lock(device_id):
        config, zone = schedule.read_config(device_id)
        old = load_record(device_id)
        if old and (times(config) not in (old["applied"], old["usual"]) or zone != old["time_zone"]):
            raise yoto.YotoError("The Yoto schedule changed outside this override. Restore/review it before requesting another override.")
        usual = old["usual"] if old else times(config)
        current = now()
        local = current.astimezone(ZoneInfo(zone))
        if mode_at(config, local) == mode and not old:
            return {"status": "already_in_requested_mode", "mode": mode, "player": alias,
                    "time_zone": zone, "note": "Saved schedule already selects this mode; no settings changed."}
        # End any previous override when the requested mode matches the usual schedule.
        if old and mode_at(usual, local) == mode:
            return restore_locked(alias, player, old, config, zone)
        applied = times(config)
        applied["dayTime" if mode == "sun" else "nightTime"] = local.strftime("%H:%M")
        if applied["dayTime"] == applied["nightTime"]:
            other = "nightTime" if mode == "sun" else "dayTime"
            applied[other] = (local - timedelta(minutes=1)).strftime("%H:%M")
        restore_at = next_boundary(usual, zone, current)
        record = {"device_id": device_id, "alias": alias, "mode": mode, "usual": usual,
                  "applied": applied, "time_zone": zone, "restore_at": restore_at}
        # Persist recovery information BEFORE writing to Yoto. Uncertain writes leave
        # this record intact, and the worker reads live settings before restoration.
        yoto.write_private(record_path(device_id), record)
        if times(config) != applied:
            schedule.write_config(player, {**config, **applied}, zone)
        return {"status": "temporary_settings_verified", "mode": mode, "player": alias,
                "time_zone": zone, "restore_at": restore_at,
                "usual_morning": usual["dayTime"], "usual_bedtime": usual["nightTime"],
                "note": "Saved temporary mode settings verified; allow the online player time to sync. Usual schedule restores at the next normal boundary."}


def restore_locked(alias, player, record, config, zone):
    if zone != record["time_zone"] or times(config) not in (record["applied"], record["usual"]):
        # External schedule changes take precedence. Do not overwrite the user's app edits.
        record_path(player["device_id"]).unlink()
        return {"status": "cancelled_after_external_change", "player": alias,
                "note": "Current app schedule kept; old restore task cancelled."}
    if times(config) != record["usual"]:
        schedule.write_config(player, {**config, **record["usual"]}, zone)
    record_path(player["device_id"]).unlink()
    return {"status": "usual_schedule_restored", **schedule.public_settings(alias, record["usual"], zone)}


def restore(alias: str | None = None, *, due_only=False, expected_device_id=None):
    # Restoration changes saved cloud settings even if a player is currently offline.
    if alias is None:
        players = [p for p in yoto.list_players() if p.get("alias")]
        if len(players) != 1:
            raise yoto.YotoError("Specify which configured player to restore.")
        alias = players[0]["alias"]
    player, _ = yoto.selected_player(alias, require_online=False)
    if expected_device_id is not None and player["device_id"] != expected_device_id:
        raise yoto.YotoError("Configured player identity changed; restore needs review.")
    with schedule.config_lock(player["device_id"]):
        record = load_record(player["device_id"])
        if not record:
            return {"status": "no_temporary_override", "player": alias}
        # A new shortcut may have replaced the task since the worker scanned it.
        if due_only and datetime.fromisoformat(record["restore_at"]) > now():
            return {"status": "not_due", "player": alias}
        config, zone = schedule.read_config(player["device_id"])
        return restore_locked(alias, player, record, config, zone)


def run_due():
    for path in (yoto.home() / "yoto-overrides").glob("*.json"):
        try:
            record = load_record(path.stem)
            if record and datetime.fromisoformat(record["restore_at"]) <= now():
                result = restore(record["alias"], due_only=True, expected_device_id=path.stem)
                print("Yoto restore:", result["status"], flush=True)
        except (yoto.YotoError, OSError, ValueError, TypeError):
            # Keep the durable task on network/auth failure. Next pass rereads actual
            # state before any idempotent restore; never replays an old full config.
            print("Yoto restore pending: check credentials, connection and restore record.", flush=True)


def worker():
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Yoto schedule restore worker started.", flush=True)
    try:
        while not stopping:
            run_due()
            yoto.write_private(heartbeat_path(), {"heartbeat": time.time()})
            for _ in range(15):
                if stopping:
                    break
                time.sleep(1)
    finally:
        heartbeat_path().unlink(missing_ok=True)
