"""One configured Alexa shopping list via Amazon's unofficial mobile API."""

import asyncio
import fcntl
import json
import logging
import os
import sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http import HTTPMethod
from urllib.parse import quote

from household_tools.integrations import ocado, yoto

SITES = {"https://www.amazon.co.uk", "https://www.amazon.com", "https://www.amazon.es", "https://www.amazon.de", "https://www.amazon.fr", "https://www.amazon.it"}
MAX_ACTIVE = 100


def config_path():
    return yoto.home() / "alexa.json"


def credentials_path():
    return yoto.home() / "credentials" / "alexa.json"


def load_config():
    try:
        value = json.loads(config_path().read_text())
        if value["site"] not in SITES or not isinstance(value["list_id"], str) or not 1 <= len(value["list_id"]) <= 1024:
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise ocado.OcadoError("Alexa is not configured. Run make alexa-login and make alexa-enable on the droplet.") from None


@contextmanager
def credential_lock():
    path = credentials_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def dependencies():
    if sys.version_info < (3, 12):
        raise ocado.OcadoError("Alexa requires Python 3.12 or newer; use the deployment container.")
    from aiohttp import ClientSession, ClientTimeout
    from aioamazondevices.http_wrapper import AmazonHttpWrapper, AmazonSessionStateData
    from aioamazondevices.login import AmazonLogin
    from aioamazondevices.structures import AmazonSaveDataConfig
    # The upstream debug logger can contain cookies, URLs and raw account responses.
    logging.getLogger("aioamazondevices").disabled = True
    return ClientSession, ClientTimeout, AmazonHttpWrapper, AmazonSessionStateData, AmazonLogin, AmazonSaveDataConfig


class ShoppingSession:
    def __init__(self, wrapper, site):
        self.wrapper, self.site = wrapper, site

    async def request(self, method, path, body=None, query=None):
        from yarl import URL
        url = URL(self.site + "/alexashoppinglists/api/v2/lists/" + path, encoded=True)
        if query:
            url = url.with_query(query)
        _, response = await self.wrapper.session_request(method=method, url=url,
            input_data=body or {}, json_data=True, fail_fast=True)
        if response.status != 200:
            raise ocado.OcadoError("Alexa returned an unexpected response; list changes could not be confirmed.")
        value = await response.json()
        if not isinstance(value, dict):
            raise ocado.OcadoError("Alexa returned an unrecognised list response.")
        return value

    async def lists(self):
        value = await self.request(HTTPMethod.POST, "fetch")
        rows = value.get("listInfoList")
        if not isinstance(rows, list):
            raise ocado.OcadoError("Alexa did not return shopping lists.")
        return [{"id": row["listId"], "name": row.get("listName") or "Shopping list"}
                for row in rows if row.get("listType") == "SHOP" and isinstance(row.get("listId"), str)]

    async def items(self, list_id):
        result, tokens, seen, token = [], set(), set(), None
        for _ in range(30):
            value = await self.request(HTTPMethod.POST, quote(list_id, safe="") + "/items/fetch",
                {"nextToken": token} if token else {}, {"limit": 100})
            rows = value.get("itemInfoList")
            if not isinstance(rows, list):
                raise ocado.OcadoError("Alexa did not return a complete item list.")
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("itemId"), str) or not 1 <= len(row["itemId"]) <= 1024
                        or not isinstance(row.get("itemName"), str) or not 1 <= len(row["itemName"]) <= 500
                        or type(row.get("version")) is not int or row["version"] < 0
                        or row.get("itemStatus") not in {"ACTIVE", "COMPLETE"} or row["itemId"] in seen):
                    raise ocado.OcadoError("Alexa list items changed during pagination or were not recognised. Try reading the list again before shopping.")
                seen.add(row["itemId"])
                result.append({"id": row["itemId"], "name": row["itemName"],
                    "version": row["version"], "status": row["itemStatus"]})
            token = value.get("nextToken")
            if not token:
                if sum(item["status"] == "ACTIVE" for item in result) > MAX_ACTIVE:
                    raise ocado.OcadoError("Alexa import is limited to 100 active items; no order was changed.")
                return result
            if not isinstance(token, str) or len(token) > 8192 or token in tokens:
                raise ocado.OcadoError("Alexa pagination could not be completed; no order was changed.")
            tokens.add(token)
        raise ocado.OcadoError("Alexa list is too large to read completely; no order was changed.")

    async def complete(self, list_id, item):
        await self.request(HTTPMethod.PUT, quote(list_id, safe="") + "/items/" + quote(item["id"], safe=""),
            {"itemAttributesToUpdate": [{"type": "itemStatus", "value": "COMPLETE"}], "itemAttributesToRemove": []},
            {"version": item["version"]})


async def connection(operation, *, site, email="", password="", otp=None, stored=None):
    ClientSession, ClientTimeout, Wrapper, State, Login, Save = dependencies()
    async with ClientSession(timeout=ClientTimeout(total=20)) as http:
        state = State(site, email, password, stored)
        wrapper = Wrapper(http, state, Save(path=yoto.home(), callback=None))
        login = Login(wrapper, state)
        try:
            if otp is not None:
                await login.login_mode_interactive(otp)
            else:
                await login.login_mode_stored_data()
            return await operation(ShoppingSession(wrapper, state.retail_site_url.human_repr().rstrip("/")))
        finally:
            data = state.login_stored_data
            if data.get("refresh_token") and data.get("device_info"):
                yoto.write_private(credentials_path(), data)


def run(operation):
    config = load_config()
    with credential_lock():
        try:
            stored = json.loads(credentials_path().read_text())
            if not isinstance(stored, dict) or stored.get("site") != config["site"]:
                raise ValueError
            # FastMCP may call synchronous tools from its already-running event loop.
            # Give the async connector its own loop rather than nesting asyncio.run.
            async def request():
                return await asyncio.wait_for(connection(operation, site=config["site"], stored=stored), 120)
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(request())).result()
        except ocado.OcadoError:
            raise
        except Exception:
            # SDK/network exceptions may contain passwords, tokens or full responses.
            raise ocado.OcadoError("Alexa request failed. Check the connection or renew the private login with make alexa-login; no shopping result can be confirmed.") from None


def snapshot():
    config = load_config()
    async def read(client):
        return {"list_id": config["list_id"], "items": [item for item in await client.items(config["list_id"]) if item["status"] == "ACTIVE"]}
    return run(read)


def complete_snapshot(captured):
    config = load_config()
    if captured["list_id"] != config["list_id"]:
        return {"status": "pending", "note": "Configured Alexa list changed; captured entries were left untouched."}
    async def finish(client):
        current = {item["id"]: item for item in await client.items(config["list_id"])}
        completed, unchanged, failed = [], [], []
        for item in captured["items"]:
            live = current.get(item["id"])
            if live and live["name"] == item["name"] and live["status"] == "COMPLETE":
                completed.append(item["name"])
            elif live and live["name"] == item["name"] and live["version"] == item["version"]:
                try:
                    await client.complete(config["list_id"], item)
                    completed.append(item["name"])
                except Exception:
                    failed.append(item["name"])
            else:
                unchanged.append(item["name"])
        verified = {item["id"]: item for item in await client.items(config["list_id"])}
        confirmed = [item["name"] for item in captured["items"] if verified.get(item["id"], {}).get("status") == "COMPLETE"
                     and verified[item["id"]]["name"] == item["name"]]
        return {"status": "completed" if len(confirmed) == len(captured["items"]) else "partially_completed",
                "completed": confirmed, "left_unchanged": unchanged,
                "unconfirmed": sorted(set(completed + failed) - set(confirmed))}
    try:
        return run(finish)
    except ocado.OcadoError:
        return {"status": "pending", "note": "Ocado trolley was prepared, but Alexa completion could not be verified. Do not rerun Ocado just to clear the list."}
