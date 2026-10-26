"""Google Calendar events on one operator-configured shared calendar."""

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
from urllib.parse import quote, urlparse
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from household_tools.integrations import yoto

API = "https://www.googleapis.com/calendar/v3/calendars/"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalendarError(ValueError):
    """Safe errors: no keys, bearer tokens or raw Google responses."""


def config_path():
    return yoto.home() / "calendar.json"


def credentials_path():
    return yoto.home() / "credentials" / "google-calendar.json"


def validate_config(value) -> dict:
    if not isinstance(value, dict):
        raise CalendarError("Calendar configuration must be an object.")
    calendar_id = value.get("calendar_id")
    if not isinstance(calendar_id, str) or "@" not in calendar_id or "://" in calendar_id or len(calendar_id) > 1024 or any(c.isspace() for c in calendar_id):
        raise CalendarError("Set the shared calendar's exact Calendar ID, not 'primary', its display name or a browser link.")
    name = value.get("name", "Shared calendar")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise CalendarError("Choose a short calendar display name.")
    zone = value.get("time_zone", "Europe/London")
    try:
        ZoneInfo(zone)
    except (ValueError, TypeError, KeyError):
        raise CalendarError("Use an IANA calendar time zone such as Europe/London.") from None
    duration = value.get("default_duration_minutes", 60)
    if type(duration) is not int or not 1 <= duration <= 1440:
        raise CalendarError("Default event duration must be 1 to 1440 minutes.")
    return {"calendar_id": calendar_id, "name": name.strip(), "time_zone": zone,
            "default_duration_minutes": duration}


def load_config() -> dict:
    try:
        return validate_config(json.loads(config_path().read_text()))
    except (OSError, ValueError) as error:
        if isinstance(error, CalendarError):
            raise
        raise CalendarError("Google Calendar is not configured. Run make calendar-configure on the droplet.") from None


def load_credentials():
    try:
        path = credentials_path()
        if path.stat().st_size > 100000:
            raise ValueError
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or value.get("type") != "service_account"
                or value.get("token_uri", TOKEN_URL) != TOKEN_URL
                or value.get("universe_domain", "googleapis.com") != "googleapis.com"
                or not isinstance(value.get("client_email"), str)
                or not value["client_email"].endswith(".gserviceaccount.com")):
            raise ValueError
        return service_account.Credentials.from_service_account_info({**value, "token_uri": TOKEN_URL}, scopes=[SCOPE])
    except (OSError, ValueError, KeyError, TypeError):
        raise CalendarError("Calendar credentials are missing or invalid. Copy the service-account JSON privately to data/credentials/google-calendar.json.") from None


def access_token() -> str:
    credentials = load_credentials()
    try:
        credentials.refresh(partial(Request(), timeout=20))
    except (GoogleAuthError, OSError, ValueError):
        raise CalendarError("Google Calendar authentication failed. Check the service-account key and Calendar API setup; no event was created.") from None
    if not isinstance(credentials.token, str) or not credentials.token:
        raise CalendarError("Google did not return a calendar access token.")
    return credentials.token


def status() -> dict:
    if not config_path().exists():
        return {"configured": False, "credentials_saved": credentials_path().exists(),
                "time_zone": "Europe/London", "now": datetime.now(ZoneInfo("Europe/London")).isoformat()}
    config = load_config()
    return {"configured": True, "credentials_saved": credentials_path().exists(), "name": config["name"],
            "time_zone": config["time_zone"], "now": datetime.now(ZoneInfo(config["time_zone"])).isoformat(),
            "default_duration_minutes": config["default_duration_minutes"], "note": "Configuration status, not a live Google access check."}


def instant(value: str, zone: ZoneInfo, allow_date: bool = False) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            if not allow_date:
                raise CalendarError("A timed event needs a time. Ask the user, or use all_day=true only when they requested an all-day event.")
            parsed = datetime.combine(date.fromisoformat(value), time.min)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(zone)
        candidates = {}
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=zone, fold=fold)
            utc = candidate.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == parsed:
                candidates[utc] = candidate
        if len(candidates) != 1:
            raise CalendarError("That local time is ambiguous or does not exist because of a clock change. Clarify the time/UTC offset before adding the event.")
        return next(iter(candidates.values()))
    except (ValueError, TypeError, OverflowError) as error:
        if isinstance(error, CalendarError):
            raise
        raise CalendarError("Use an ISO date/time, with an explicit UTC offset if needed.") from None


def event_body(config, title: str, start: str, end: str | None, all_day: bool, location: str, description: str) -> dict:
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise CalendarError("An event needs a title of at most 500 characters.")
    if type(all_day) is not bool or not isinstance(location, str) or len(location) > 2000 or not isinstance(description, str) or len(description) > 10000:
        raise CalendarError("Invalid event details.")
    zone = ZoneInfo(config["time_zone"])
    if all_day:
        try:
            if not isinstance(start, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
                raise ValueError
            first = date.fromisoformat(start)
            last = first + timedelta(days=1) if end is None else date.fromisoformat(end)
            if last <= first:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise CalendarError("All-day events use YYYY-MM-DD dates; the end is exclusive and must be after the start. Omit end for one day.") from None
        starts, ends = {"date": first.isoformat()}, {"date": last.isoformat()}
    else:
        first = instant(start, zone)
        last = (first.astimezone(timezone.utc) + timedelta(minutes=config["default_duration_minutes"])).astimezone(zone) if end is None else instant(end, zone)
        if last.astimezone(timezone.utc) <= first.astimezone(timezone.utc):
            raise CalendarError("The event end must be after its start.")
        starts, ends = {"dateTime": first.isoformat(), "timeZone": zone.key}, {"dateTime": last.isoformat(), "timeZone": zone.key}
    return {"summary": title.strip(), "start": starts, "end": ends,
            **({"location": location} if location else {}), **({"description": description} if description else {})}


def client():
    return httpx.Client(headers={"Authorization": "Bearer " + access_token(), "Accept": "application/json"}, timeout=20, follow_redirects=False)


def events_url(config):
    return API + quote(config["calendar_id"], safe="") + "/events"


def response_json(response):
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError:
        raise CalendarError("Google Calendar returned an unrecognised response; no result can be confirmed.") from None


def api_error(code):
    if code in {401, 403, 404}:
        return CalendarError(f"Google Calendar returned HTTP {code}. Check that the API is enabled, the Calendar ID is correct and the calendar is shared with the service account with event-edit permission.")
    return CalendarError(f"Google Calendar returned HTTP {code}; the requested result could not be confirmed.")


def public_event(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not isinstance(value.get("start"), dict) or not isinstance(value.get("end"), dict):
        raise CalendarError("Google returned an unrecognised event; check the calendar before repeating the request.")
    if any(not isinstance(value[key].get("date") or value[key].get("dateTime"), str) for key in ("start", "end")):
        raise CalendarError("Google returned an event without recognised dates/times.")
    result = {"event_id": value["id"], "title": value.get("summary", "Busy (details unavailable)"),
              "start": {k: v for k, v in value["start"].items() if k in {"date", "dateTime", "timeZone"}},
              "end": {k: v for k, v in value["end"].items() if k in {"date", "dateTime", "timeZone"}},
              "location": value.get("location", "")}
    link = value.get("htmlLink", "")
    if isinstance(link, str) and urlparse(link).scheme == "https" and urlparse(link).hostname in {"www.google.com", "calendar.google.com"}:
        result["url"] = link
    return result


def list_events(start: str | None = None, end: str | None = None, limit: int = 20, query: str = "") -> dict:
    config = load_config()
    zone = ZoneInfo(config["time_zone"])
    first = datetime.now(zone) if start is None else instant(start, zone, True)
    last = first + timedelta(days=7) if end is None else instant(end, zone, True)
    if last.astimezone(timezone.utc) <= first.astimezone(timezone.utc) or last - first > timedelta(days=366):
        raise CalendarError("Choose a calendar window of at most 366 days, ending after its start.")
    if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(query, str) or len(query) > 500:
        raise CalendarError("Use a limit of 1 to 50 events and a short search query.")
    params = {"timeMin": first.isoformat(), "timeMax": last.isoformat(), "singleEvents": "true",
              "orderBy": "startTime", "maxResults": limit, "timeZone": zone.key, "showDeleted": "false"}
    if query:
        params["q"] = query
    try:
        with client() as connection:
            response = connection.get(events_url(config), params=params)
    except httpx.HTTPError:
        raise CalendarError("Google Calendar could not be reached; no events were changed.") from None
    if response.status_code != 200:
        raise api_error(response.status_code)
    value = response_json(response)
    if not isinstance(value.get("items"), list) or any(not isinstance(item, dict) for item in value["items"]):
        raise CalendarError("Google did not return an event list.")
    return {"calendar": config["name"], "time_zone": zone.key, "now": datetime.now(zone).isoformat(),
            "events": [public_event(item) for item in value["items"] if item.get("status") != "cancelled"],
            "has_more": bool(value.get("nextPageToken")), "access_role": value.get("accessRole")}


def verify_created(value, body, config):
    if value.get("status") == "cancelled" or value.get("id") != body["id"]:
        raise CalendarError("The matching event was changed/deleted. Review Google Calendar instead of creating a duplicate.")
    properties = value.get("extendedProperties")
    private = properties.get("private") if isinstance(properties, dict) else None
    if not isinstance(private, dict) or private.get("household_key") != body["id"]:
        raise CalendarError("Could not verify the matching event's identity; check Google Calendar.")
    zone = ZoneInfo(config["time_zone"])
    same = all(value.get(key, "") == body.get(key, "") for key in ("summary", "location", "description"))
    for key in ("start", "end"):
        actual, wanted = value.get(key, {}), body[key]
        if not isinstance(actual, dict):
            raise CalendarError("Google returned unrecognised event dates; check the calendar before retrying.")
        if "date" in wanted:
            same = same and actual.get("date") == wanted["date"]
        else:
            same = same and instant(actual.get("dateTime"), zone).astimezone(timezone.utc) == instant(wanted["dateTime"], zone).astimezone(timezone.utc)
    if not same:
        raise CalendarError("The matching event exists but its details changed. Review it on Google Calendar instead of overwriting or duplicating it.")
    return public_event(value)


def create_event(title: str, start: str, end: str | None = None, all_day: bool = False,
                 location: str = "", description: str = "") -> dict:
    config = load_config()
    body = event_body(config, title, start, end, all_day, location, description)
    key = uuid5(NAMESPACE_URL, json.dumps({"calendar": config["calendar_id"], "event": body}, sort_keys=True)).hex
    body.update(id=key, extendedProperties={"private": {"household_key": key}})
    event_url = events_url(config) + "/" + key
    try:
        with client() as connection:
            existing = connection.get(event_url)
            if existing.status_code == 200:
                event = verify_created(response_json(existing), body, config)
                return {"status": "already_exists", "calendar": config["name"], **event}
            if existing.status_code != 404:
                raise api_error(existing.status_code)
            # Exactly one insert. A timeout/500 is reconciled by GET, never by another POST.
            inserted = False
            try:
                response = connection.post(events_url(config), json=body, params={"sendUpdates": "none"})
                inserted = response.status_code in {200, 201}
                if not inserted and response.status_code not in {409, 500, 502, 503, 504}:
                    raise api_error(response.status_code)
            except httpx.HTTPError:
                pass
            verified = connection.get(event_url)
            if verified.status_code != 200:
                raise CalendarError("The create outcome could not be verified. Check Google Calendar or repeat the SAME event details; do not change details to retry, as that could duplicate it.")
            event = verify_created(response_json(verified), body, config)
            return {"status": "created" if inserted else "found_after_uncertain_create", "calendar": config["name"], **event}
    except httpx.HTTPError:
        raise CalendarError("Google Calendar could not be reached. The create outcome may be unknown; check the calendar or retry the SAME details.") from None
