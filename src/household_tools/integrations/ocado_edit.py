"""Preview and stage bounded quantity edits in a specific Reserved order.

Endpoint contracts inspected in Ocado's public /static/index-CKdmGrK1.js.
There is no checkout, payment, slot change, cancellation, or trolley-merge operation.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager

import httpx

from household_tools.integrations import ocado, yoto

PLAN_TTL = 600
MAX_CHANGES = 20
MAX_QUANTITY = 50
START_EDIT = "/api/customersessions/v2/sessions/order-edit"
APPLY_QUANTITY = "/api/cart/v1/carts/active/apply-quantity?cartProductSorting=CATEGORIES"


def plan_path(plan_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ocado.OcadoError("Invalid edit plan ID. Prepare a new order edit preview.")
    return yoto.home() / "ocado-plans" / (plan_id + ".json")


@contextmanager
def edit_lock():
    path = yoto.home() / "ocado-edit.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ocado.OcadoError("Another household Ocado edit is running. Wait for its result before trying another edit.") from None
        yield


def client_for(session: dict):
    return httpx.Client(cookies=ocado.cookie_jar(session), headers={"User-Agent": session["user_agent"],
                        "Accept": "application/json"}, timeout=20, follow_redirects=False)


def request_token(client) -> str:
    page = ocado.read_request(client, "GET", ocado.ORDERS_PAGE, headers={"Accept": "text/html"})
    match = re.search(r'"csrf"\s*:\s*\{\s*"token"\s*:\s*"([^"\r\n]+)"', page.text)
    if not match:
        raise ocado.OcadoError("Ocado did not supply its normal request token. Refresh the browser session.")
    return match[1]


def cart_context(client, token: str) -> dict:
    value = ocado.read_json(ocado.read_request(client, "POST", "/graphql",
        json=ocado.CART_CONTEXT_REQUEST, headers={"x-csrf-token": token}))
    try:
        cart = value["data"]["shopper"]["cart"]
        if value.get("errors") or type(cart["isOrderUnderEdit"]) is not bool:
            raise ValueError
        return cart
    except (KeyError, TypeError, ValueError):
        raise ocado.OcadoError("Could not verify which order the active trolley belongs to. No quantity change is permitted.") from None


def raw_cart(client) -> dict:
    cart = ocado.read_json(ocado.read_request(client, "GET", ocado.CART))
    ocado.normalise_trolley(cart, [])
    return cart


def quantities(cart: dict) -> dict[str, int]:
    items = ocado.normalise_trolley(cart, [])["items"]
    result = {item["product_id"]: item["quantity"] for item in items}
    if len(result) != len(items):
        raise ocado.OcadoError("Trolley has ambiguous duplicate products; use Ocado's website.")
    return result


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def order_fingerprint(order: dict) -> str:
    return fingerprint({"order_id": order["order_id"], "region_id": order.get("region_id"),
        "delivery": order["delivery"], "total": order.get("total"),
        "items": sorted(order["items"], key=lambda item: item["product_id"])})


def editable_order() -> dict:
    order = ocado.next_order()["order"]
    return validate_editable_order(order)


def validate_editable_order(order) -> dict:
    if order is None or not isinstance(order.get("items"), list):
        raise ocado.OcadoError("No readable upcoming order is available for editing.")
    if order.get("is_editable") is not True or order.get("is_under_edit") is not False:
        raise ocado.OcadoError("The next order is not editable or is already being edited. Review it on Ocado's website.")
    deadline = order.get("guaranteed_editable_by")
    if not deadline or ocado.order_timestamp(deadline).timestamp() <= time.time():
        raise ocado.OcadoError("The guaranteed editing window has passed or is unknown. Review this order on Ocado's website.")
    region = order.get("region_id")
    if not isinstance(region, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", region):
        raise ocado.OcadoError("The order's region could not be verified; no edit can be started.")
    return order


def prepare(changes: list[dict]) -> dict:
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ocado.OcadoError(f"Choose between 1 and {MAX_CHANGES} existing product quantity changes.")
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"product_id", "quantity"}:
            raise ocado.OcadoError("Each change must contain only product_id and the desired quantity.")
        product_id, quantity = change["product_id"], change["quantity"]
        if not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", product_id) or product_id in seen:
            raise ocado.OcadoError("Product IDs must be distinct strings.")
        if type(quantity) is not int or not 0 <= quantity <= MAX_QUANTITY:
            raise ocado.OcadoError(f"Each desired quantity must be an integer from 0 to {MAX_QUANTITY}; 0 removes an item.")
        seen.add(product_id)
    with edit_lock():
        order = editable_order()
        items = {item["product_id"]: item for item in order["items"]}
        if len(items) != len(order["items"]):
            raise ocado.OcadoError("Order has duplicate product entries; edit it on Ocado's website.")
        diff = []
        for change in changes:
            item = items.get(change["product_id"])
            if item is None:
                raise ocado.OcadoError("This version only changes items already in the scheduled order. Adding new products is not enabled.")
            if item["quantity"] != change["quantity"]:
                diff.append({"product_id": item["product_id"], "name": item["name"],
                             "before": item["quantity"], "after": change["quantity"]})
        if not diff:
            raise ocado.OcadoError("Those quantities are already set; no edit plan is needed.")
        session = ocado.load_session()
        with client_for(session) as client:
            token = request_token(client)
            context = cart_context(client, token)
            if context["isOrderUnderEdit"]:
                raise ocado.OcadoError("An order editing session is already active. Complete or cancel it on Ocado's website first.")
            cart = raw_cart(client)
        plan_id = secrets.token_hex(16)
        plan = {"version": 1, "plan_id": plan_id, "state": "prepared", "expires_at": time.time() + PLAN_TTL,
                "order_id": order["order_id"], "region_id": order["region_id"], "delivery": order["delivery"],
                "order_fingerprint": order_fingerprint(order), "original_cart_fingerprint": fingerprint(cart),
                "changes": diff}
        yoto.write_private(plan_path(plan_id), plan)
        return {"plan_id": plan_id, "order_id": order["order_id"], "delivery": order["delivery"],
                "changes": diff, "expires_in_seconds": PLAN_TTL,
                "editing_enabled": os.environ.get("OCADO_ORDER_EDITING_ENABLED") == "true",
                "standalone_trolley_item_lines": len(quantities(cart)),
                "note": "Preview only; no Ocado order or trolley has changed. Show the exact delivery and changes to the user, then wait for explicit confirmation. Applying stages draft changes; the user must save/check out on Ocado."}


def persist_cookies(client, session: dict) -> None:
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "expires": c.expires if c.expires is not None else -1, "secure": c.secure}
               for c in client.cookies.jar if ocado.ocado_domain(c.domain)]
    yoto.write_private(ocado.session_path(), {**session, "cookies": cookies})


def post_edit(client, token: str, path: str, body) -> None:
    # This narrow internal writer has no checkout, order cancellation, or merge route.
    if path == START_EDIT:
        valid = isinstance(body, dict) and set(body) == {"orderId", "regionId"} and all(
            isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) for value in body.values())
    elif path == APPLY_QUANTITY:
        valid = isinstance(body, list) and 1 <= len(body) <= MAX_CHANGES and all(
            isinstance(item, dict) and set(item) == {"productId", "quantity"}
            and isinstance(item["productId"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item["productId"])
            and type(item["quantity"]) is int and item["quantity"] != 0 for item in body)
    else:
        valid = False
    if not valid:
        raise ocado.OcadoError("This order-edit request is not permitted.")
    try:
        response = client.post(ocado.ORIGIN + path, json=body, headers={"x-csrf-token": token})
    except httpx.HTTPError:
        raise ocado.OcadoError("The editing request had a connection error; its outcome is unknown. Do not repeat it automatically. Check Ocado's website.") from None
    if response.status_code != 200:
        raise ocado.OcadoError(f"Ocado editing returned HTTP {response.status_code}. Do not retry automatically; check the order on Ocado's website.")


def apply(plan_id: str, confirmed: bool = False) -> dict:
    if os.environ.get("OCADO_ORDER_EDITING_ENABLED") != "true":
        raise ocado.OcadoError("Order editing is disabled. An operator must run make ocado-enable-editing on the droplet.")
    if confirmed is not True:
        raise ocado.OcadoError("Show the preview and obtain explicit user confirmation before applying this plan.")
    with edit_lock():
        try:
            plan = json.loads(plan_path(plan_id).read_text())
        except (OSError, ValueError):
            raise ocado.OcadoError("Edit plan could not be read. Prepare a new preview.") from None
        if plan.get("state") != "prepared" or plan.get("expires_at", 0) <= time.time():
            raise ocado.OcadoError("This plan expired or has already been attempted. Do not replay it; review Ocado and prepare a new preview.")
        order = editable_order()
        if order_fingerprint(order) != plan["order_fingerprint"]:
            raise ocado.OcadoError("The scheduled order changed since the preview. Prepare a new preview and ask for confirmation again.")
        session = ocado.load_session()
        phase = "preflight"
        try:
            with client_for(session) as client:
                token = request_token(client)
                context = cart_context(client, token)
                if context["isOrderUnderEdit"]:
                    raise ocado.OcadoError("Another order edit is already active. Review it on Ocado before proceeding.")
                if fingerprint(raw_cart(client)) != plan["original_cart_fingerprint"]:
                    raise ocado.OcadoError("The standalone trolley changed since the preview. Prepare a new preview; it will not be merged or cleared by this tool.")
                plan["state"] = "applying"
                yoto.write_private(plan_path(plan_id), plan)
                phase = "opening_order_edit"
                try:
                    post_edit(client, token, START_EDIT, {"orderId": plan["order_id"], "regionId": plan["region_id"]})
                finally:
                    persist_cookies(client, session)
                token = request_token(client)
                context = cart_context(client, token)
                if context["isOrderUnderEdit"] is not True or context.get("orderId") != plan["order_id"]:
                    raise ocado.OcadoError("Could not verify that the edit trolley belongs to the previewed order. No quantity request was sent; review the website.")
                current = quantities(raw_cart(client))
                expected = {item["product_id"]: item["quantity"] for item in order["items"] if item["quantity"]}
                if {p: q for p, q in current.items() if q} != expected:
                    raise ocado.OcadoError("Editing trolley quantities differ from the preview. No quantity request was sent; review the website.")
                body = [{"productId": change["product_id"], "quantity": change["after"] - change["before"]}
                        for change in plan["changes"]]
                phase = "updating_quantities"
                try:
                    post_edit(client, token, APPLY_QUANTITY, body)
                finally:
                    persist_cookies(client, session)
                phase = "verifying_draft"
                context = cart_context(client, token)
                if context["isOrderUnderEdit"] is not True or context.get("orderId") != plan["order_id"]:
                    raise ocado.OcadoError("The active order changed during verification. Review Ocado; do not repeat this request.")
                after = quantities(raw_cart(client))
                expected.update({change["product_id"]: change["after"] for change in plan["changes"]})
                if {p: q for p, q in after.items() if q} != {p: q for p, q in expected.items() if q}:
                    raise ocado.OcadoError("Not all requested draft quantities were verified. Review Ocado; do not repeat this request automatically.")
                plan["state"] = "staged_pending_checkout"
                persist_cookies(client, session)
                yoto.write_private(plan_path(plan_id), plan)
                return {"status": "staged_pending_checkout", "order_id": plan["order_id"],
                        "delivery": plan["delivery"], "changes": plan["changes"],
                        "note": "Draft quantities verified in this order's edit trolley. The order is NOT confirmed as updated. Review and save/check out on Ocado; no checkout or payment was submitted."}
        except (ocado.OcadoError, OSError) as error:
            if phase != "preflight":
                plan.update(state="outcome_requires_review", failed_phase=phase)
                try:
                    yoto.write_private(plan_path(plan_id), plan)
                except OSError:
                    pass  # The persisted 'applying' state already prevents replay.
                message = str(error) if isinstance(error, ocado.OcadoError) else "Private edit state could not be saved. Check Ocado manually."
                raise ocado.OcadoError(message + " This plan is locked against replay; the order may be in edit mode.") from None
            raise
