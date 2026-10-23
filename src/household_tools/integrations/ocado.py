"""Read-only Ocado trolley adapter using the account owner's browser session.

Endpoint and response-shape reference: abracadabra50/open-supermarkets,
revision aff6880435ab3c43f4f79f3896dd4cf40142171c; see THIRD_PARTY_NOTICES.md.
"""

import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie

import httpx

from household_tools.integrations.yoto import home

ORIGIN = "https://www.ocado.com"
CART = "/api/cart/v1/carts/active?cartProductSorting=CATEGORIES"
PRODUCTS = "/api/webproductpagews/v6/products"
READ_REQUESTS = {("GET", CART)}


class OcadoError(ValueError):
    """Safe to return to a user; never include cookies or raw response bodies."""


def session_path():
    return home() / "credentials" / "ocado.json"


def ocado_domain(domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return domain == "ocado.com" or domain.endswith(".ocado.com")


def product_names(products) -> dict[str, str]:
    """Keep only product ID/name pairs from an ordinary browser product response."""
    if not isinstance(products, list):
        return {}
    return {p["productId"]: p["name"] for p in products if isinstance(p, dict)
            and isinstance(p.get("productId"), str) and isinstance(p.get("name"), str)
            and 0 < len(p["productId"]) <= 200 and 0 < len(p["name"]) <= 1000}


def page_product_names(html: str) -> dict[str, str]:
    """Read productEntities JSON already embedded in the user's visible page."""
    names = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"productEntities"\s*:\s*', html):
        try:
            entities, _ = decoder.raw_decode(html, match.end())
        except ValueError:
            continue
        if isinstance(entities, dict):
            names.update(product_names(list(entities.values())))
    return names


def validated_session(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("cookies"), list):
        raise OcadoError("Ocado session is invalid. Run make ocado-login on your Mac.")
    cookies = []
    for item in value["cookies"]:
        if not isinstance(item, dict):
            continue
        if not all(isinstance(item.get(key), str) for key in ("name", "value", "domain")):
            continue
        if not item["name"] or not ocado_domain(item["domain"]):
            continue
        if any(char in item["name"] + item["value"] for char in "\r\n"):
            raise OcadoError("Ocado session contains invalid cookies; sign in again.")
        try:
            expires = float(item.get("expires", -1))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(expires):
            continue
        if expires > 0 and expires <= time.time():
            continue
        cookies.append({**item, "expires": expires})
    if not cookies:
        raise OcadoError("Ocado has no usable session cookies. Run make ocado-login on your Mac.")
    agent = value.get("user_agent", "HermesHome/0.1 (+personal read-only trolley)")
    if not isinstance(agent, str) or "\n" in agent or "\r" in agent:
        raise OcadoError("Invalid Ocado session metadata; sign in again.")
    saved = value.get("product_names", {})
    if not isinstance(saved, dict):
        saved = {}
    saved = product_names([{"productId": key, "name": name} for key, name in saved.items()])
    return {"cookies": cookies, "user_agent": agent, "captured_at": value.get("captured_at"),
            "product_names": saved}


def load_session() -> dict:
    try:
        path = session_path()
        if path.stat().st_size > 2_000_000:
            raise OcadoError("Ocado session file is too large; sign in again.")
        return validated_session(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        if isinstance(error, OcadoError):
            raise
        raise OcadoError("Ocado is not connected. Run make ocado-login on your Mac and copy its session to the droplet.") from None


def cookie_jar(session: dict) -> httpx.Cookies:
    jar = httpx.Cookies()
    for item in session["cookies"]:
        domain = item["domain"]
        expires = int(item["expires"]) if item["expires"] > 0 else None
        path = item.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/"):
            path = "/"
        jar.jar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path=path, path_specified=True, secure=bool(item.get("secure", True)),
            expires=expires, discard=expires is None, comment=None, comment_url=None, rest={},
        ))
    return jar


def read_request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    if (method, path) not in READ_REQUESTS:
        raise OcadoError("This integration permits only current-trolley reads.")
    try:
        response = client.request(method, ORIGIN + path, **kwargs)
    except httpx.HTTPError:
        raise OcadoError("Ocado could not be reached. Try again later.") from None
    if response.status_code in {202, 403, 429}:
        raise OcadoError(f"Ocado blocked or challenged this request (HTTP {response.status_code}). Retry later or sign in again.")
    if response.status_code == 401 or 300 <= response.status_code < 400:
        raise OcadoError("Ocado login is no longer accepted. Sign in again on your Mac and replace the droplet session.")
    if response.status_code != 200:
        raise OcadoError(f"Ocado returned HTTP {response.status_code}; no trolley result is available.")
    return response


def read_json(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except ValueError:
        raise OcadoError("Ocado returned a login/challenge page or changed its response. No trolley data was read.") from None
    if not isinstance(value, dict):
        raise OcadoError("Ocado returned an unexpected response; no trolley result is available.")
    return value


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def normalise_trolley(cart: dict, products: list[dict]) -> dict:
    # Missing data is not an empty trolley. Fail clearly on unrecognised schemas.
    if "items" not in cart or not isinstance(cart["items"], (list, dict)):
        raise OcadoError("Ocado's trolley format was not recognised; refusing to report it as empty.")
    entries = cart["items"] if isinstance(cart["items"], list) else list(cart["items"].values())
    names = {p["productId"]: p["name"] for p in products if isinstance(p, dict)
             and isinstance(p.get("productId"), str) and isinstance(p.get("name"), str)}
    items = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("productId"), str):
            raise OcadoError("Ocado returned an unrecognised trolley item; no partial item list was returned.")
        quantity = item.get("quantity")
        if type(quantity) is not int or quantity < 0:
            raise OcadoError("Ocado returned an unrecognised item quantity.")
        final = item.get("finalPrice") or {}
        items.append({"product_id": item["productId"], "name": names.get(item["productId"]),
                      "quantity": quantity, "line_total": amount(final.get("amount")) if isinstance(final, dict) else None})
    totals, groups = cart.get("totals"), cart.get("activeCheckoutGroupTotals")
    total = (totals.get("subTotal") if isinstance(totals, dict) else None) or \
            (groups.get("itemPriceAfterPromos") if isinstance(groups, dict) else None) or {}
    if not isinstance(total, dict):
        total = {}
    return {"items": items, "total_quantity": sum(item["quantity"] for item in items),
            "subtotal": amount(total.get("amount")), "currency": total.get("currency", "GBP"),
            "note": "Current trolley, not an order or saved shopping list. Missing names/prices are unknown; checkout totals may differ."}


def basket() -> dict:
    session = load_session()
    with httpx.Client(cookies=cookie_jar(session), headers={"User-Agent": session["user_agent"],
                      "Accept": "application/json"}, timeout=20, follow_redirects=False) as client:
        cart = read_json(read_request(client, "GET", CART))
        # Validate before metadata requests and before interpreting a missing cart as empty.
        normalise_trolley(cart, [])
        products = [{"productId": key, "name": name} for key, name in session["product_names"].items()]
        result = normalise_trolley(cart, products)
        result["name_source"] = "Names saved during the operator's Mac browser visit, matched by product ID; not live metadata."
        result["names_captured_at"] = session["captured_at"]
        unknown = sum(item["name"] is None for item in result["items"])
        if unknown:
            result["warning"] = f"{unknown} item lines have no saved name. Open the current trolley with make ocado-login on your Mac and copy the refreshed session to the droplet."
        return result
