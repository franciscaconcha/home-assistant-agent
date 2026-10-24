"""Read-only Ocado trolley and upcoming orders using the owner's browser session.

Endpoint and response-shape reference: abracadabra50/open-supermarkets,
revision aff6880435ab3c43f4f79f3896dd4cf40142171c; see THIRD_PARTY_NOTICES.md.
"""

import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from http.cookiejar import Cookie
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from household_tools.integrations.yoto import home

ORIGIN = "https://www.ocado.com"
CART = "/api/cart/v1/carts/active?cartProductSorting=CATEGORIES"
PRODUCTS = "/api/webproductpagews/v6/products"
ORDERS_PAGE = "/orders"
# Fixed read-only selection checked against Ocado's public application bundle
# /static/index-CKdmGrK1.js and the account owner's normal Orders page.
UPCOMING_QUERY = """query GetUpcomingOrders {
  ordersUpcoming {
    orderId status isRecurring region { regionId }
    prices { total { amount currency } }
    editInfo { isEditable isUnderEdit edited guaranteedEditableBy restrictions { description } }
    slot { slotId type timeWindow { start end timeZone } }
  }
}"""
UPCOMING_REQUEST = {"operationName": "GetUpcomingOrders", "query": UPCOMING_QUERY, "variables": {}}
CART_CONTEXT_QUERY = """query GetCartEditInfo {
  shopper { cart { orderId isOrderUnderEdit order { editInfo { guaranteedEditableBy } } } }
}"""
CART_CONTEXT_REQUEST = {"operationName": "GetCartEditInfo", "query": CART_CONTEXT_QUERY, "variables": {}}
READ_REQUESTS = {("GET", CART), ("GET", ORDERS_PAGE)}
PRODUCT_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


class OcadoError(ValueError):
    """Safe to return to a user; never include cookies or raw response bodies."""


def require_product_id(value, name: str = "product") -> str:
    if not isinstance(value, str) or not PRODUCT_ID_PATTERN.fullmatch(value):
        raise OcadoError(f"Invalid Ocado product_id for {name!r}: use the internal UUID from browser capture or live order data, not the numeric code/URL from a product page. No edit request was sent.")
    return value.lower()


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
    detail_read = method == "GET" and re.fullmatch(r"/api/order/v6/orders/[A-Za-z0-9_-]{1,200}", path)
    query_read = (method == "POST" and path == "/graphql" and
                  kwargs.get("json") in (UPCOMING_REQUEST, CART_CONTEXT_REQUEST) and set(kwargs) <= {"json", "headers"})
    if (method, path) not in READ_REQUESTS and not detail_read and not query_read:
        raise OcadoError("This integration permits only trolley and upcoming-order read operations.")
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


def order_timestamp(value, zone: str | None = None) -> datetime:
    if not isinstance(value, str):
        raise OcadoError("Ocado returned an order without a valid delivery timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if not zone:
                raise ValueError
            parsed = parsed.replace(tzinfo=ZoneInfo(zone))
        return parsed
    except (ValueError, ZoneInfoNotFoundError):
        raise OcadoError("Ocado returned an ambiguous order timestamp; check the order on its website.") from None


def order_summary(order: dict) -> dict:
    """Expose only order identity, delivery, total, and the supplied editing information."""
    try:
        order_id = order["orderId"]
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise ValueError
        slot = order["slot"]
        window = slot["timeWindow"]
        start = order_timestamp(window["start"], window.get("timeZone"))
        end = order_timestamp(window["end"], window.get("timeZone"))
        if end <= start:
            raise ValueError
        edit = order.get("editInfo") or {}
        total = (order.get("prices") or {}).get("total") or {}
        restrictions = edit.get("restrictions") or []
        return {
            "order_id": order_id, "status": order.get("status"),
            "region_id": (order.get("region") or {}).get("regionId"),
            "is_reserved": order.get("isRecurring") is True,
            "delivery": {"start": start.isoformat(), "end": end.isoformat(),
                         "time_zone": window.get("timeZone"), "slot_id": slot.get("slotId")},
            "is_editable": edit.get("isEditable") if type(edit.get("isEditable")) is bool else None,
            "is_under_edit": edit.get("isUnderEdit") if type(edit.get("isUnderEdit")) is bool else None,
            "guaranteed_editable_by": edit.get("guaranteedEditableBy"),
            "editing_restrictions": [r["description"] for r in restrictions
                                     if isinstance(r, dict) and isinstance(r.get("description"), str)],
            "total": amount(total.get("amount")), "currency": total.get("currency"),
        }
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, OcadoError):
            raise
        raise OcadoError("Ocado's upcoming-order format was not recognised; no order was selected.") from None


def select_next_order(orders: list, reserved_only: bool = True, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    candidates = []
    for order in orders:
        if not isinstance(order, dict):
            raise OcadoError("Ocado returned an invalid upcoming-order entry.")
        if reserved_only and order.get("isRecurring") is not True:
            continue
        summary = order_summary(order)
        if order_timestamp(summary["delivery"]["end"]) > now:
            candidates.append(summary)
    if not candidates:
        return None
    # Choose the nearest delivery, even when it is no longer editable. Do not silently
    # skip it and accidentally target the following week's order.
    return min(candidates, key=lambda item: order_timestamp(item["delivery"]["start"]))


def order_items(detail: dict, order_id: str) -> list[dict]:
    try:
        entry = detail["entities"]["order"][order_id]
        if entry.get("orderId") != order_id:
            raise ValueError
        products = entry["groupedProducts"]["products"]
        if not isinstance(products, list):
            raise ValueError
        items = []
        for product in products:
            if not isinstance(product, dict) or not isinstance(product.get("productId"), str):
                raise ValueError
            quantity = product.get("quantity")
            if type(quantity) is not int or quantity < 0:
                raise ValueError
            prices = product.get("prices") or {}
            unit, total = prices.get("unit") or {}, prices.get("total") or {}
            items.append({"product_id": product["productId"],
                          "name": product.get("name") if isinstance(product.get("name"), str) else None,
                          "quantity": quantity, "unit_price": amount(unit.get("amount")),
                          "line_total": amount(total.get("amount"))})
        return items
    except (KeyError, TypeError, AttributeError, ValueError):
        raise OcadoError("Ocado's order item details were not recognised or did not match the selected order.") from None


def next_order(reserved_only: bool = True) -> dict:
    session = load_session()
    with httpx.Client(cookies=cookie_jar(session), headers={"User-Agent": session["user_agent"],
                      "Accept": "application/json"}, timeout=20, follow_redirects=False) as client:
        page = read_request(client, "GET", ORDERS_PAGE, headers={"Accept": "text/html"})
        match = re.search(r'"csrf"\s*:\s*\{\s*"token"\s*:\s*"([^"\r\n]+)"', page.text)
        if not match:
            raise OcadoError("Ocado's Orders page did not provide its request token. Sign in normally and refresh the saved session.")
        value = read_json(read_request(client, "POST", "/graphql", json=UPCOMING_REQUEST,
                                       headers={"x-csrf-token": match[1]}))
        if value.get("errors"):
            raise OcadoError("Ocado could not answer its read-only upcoming-orders query. No order was selected or changed.")
        orders = (value.get("data") or {}).get("ordersUpcoming")
        if not isinstance(orders, list):
            raise OcadoError("Ocado's upcoming-order list was not recognised; refusing to report that no orders exist.")
        summary = select_next_order(orders, reserved_only)
        if summary is None:
            return {"order": None, "reserved_only": reserved_only,
                    "note": "No matching upcoming orders were returned. No trolley was created or changed."}
        summary["items"] = None
        summary["items_source"] = "Live selected-order details, separate from the active trolley."
        try:
            detail = read_json(read_request(client, "GET", "/api/order/v6/orders/" + summary["order_id"]))
            summary["items"] = order_items(detail, summary["order_id"])
        except OcadoError as error:
            summary["warning"] = "Order summary was read, but item details are unavailable: " + str(error)
        return {"order": summary, "reserved_only": reserved_only,
                "note": "Read only. This does not enter edit mode or alter the active trolley. guaranteed_editable_by is Ocado's supplied guarantee, not a promise that later editing is possible."}
