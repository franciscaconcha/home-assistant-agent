"""Replace a booked order with a privately saved weekly list; never check out."""

import json
import os
import re
import time

from household_tools.integrations import ocado, ocado_edit as edit, yoto

MAX_ITEMS = 100


def recipe_path():
    return yoto.home() / "ocado-weekly.json"


def validate_items(items) -> list[dict]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ocado.OcadoError(f"The weekly list must contain 1 to {MAX_ITEMS} exact products.")
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"product_id", "name", "quantity"}:
            raise ocado.OcadoError("Each weekly item needs product_id, name and quantity.")
        pid, name, quantity = item["product_id"], item["name"], item["quantity"]
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", pid) or pid in seen:
            raise ocado.OcadoError("Weekly product IDs must be valid and distinct.")
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ocado.OcadoError("Each weekly product needs its exact confirmed name.")
        if type(quantity) is not int or not 1 <= quantity <= edit.MAX_QUANTITY:
            raise ocado.OcadoError(f"Weekly quantities must be integers from 1 to {edit.MAX_QUANTITY}.")
        seen.add(pid)
        result.append({"product_id": pid, "name": name.strip(), "quantity": quantity})
    return result


def load_recipe() -> dict:
    try:
        value = json.loads(recipe_path().read_text())
        if value.get("mode") != "ready_for_checkout":
            raise ValueError
        return {"mode": "ready_for_checkout", "items": validate_items(value["items"])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ocado.OcadoError("The weekly list is missing or invalid. Confirm exact products and quantities, then save it with ocado_set_weekly_list. No order was changed.") from None


def save_recipe(items: list[dict], confirmed: bool = False) -> dict:
    if confirmed is not True:
        raise ocado.OcadoError("Confirm the exact weekly products, pack sizes and quantities with the user before saving the list.")
    items = validate_items(items)
    with edit.edit_lock():
        path = recipe_path()
        if path.exists():
            try:
                previous = json.loads(path.read_text())
            except (OSError, ValueError):
                raise ocado.OcadoError("The existing weekly list could not be backed up; it has not been replaced.") from None
            yoto.write_private(path.with_name(f"ocado-weekly.before-{time.time_ns()}.json"), previous)
        yoto.write_private(path, {"mode": "ready_for_checkout", "items": items})
    return {"status": "weekly_list_saved", "mode": "ready_for_checkout", "items": items,
            "note": "Only the private weekly list changed. No Ocado order changed."}


def run_path(order_id: str):
    return yoto.home() / "ocado-weekly-runs" / (edit.fingerprint(order_id) + ".json")


def read_run(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise ocado.OcadoError("A previous weekly run could not be read. Check the order manually; no repeat will be attempted.") from None


def assert_cart(client, token: str, order_id: str, expected: dict) -> dict:
    context = edit.cart_context(client, token)
    if context["isOrderUnderEdit"] is not True or context.get("orderId") != order_id:
        raise ocado.OcadoError("The edit trolley does not belong to the expected booked order. Stopping.")
    cart = edit.raw_cart(client)
    actual = {p: q for p, q in edit.quantities(cart).items() if q}
    if actual != {p: q for p, q in expected.items() if q}:
        raise ocado.OcadoError("The order trolley does not match the expected quantities. Items may be unavailable or another client may have edited it. Review Ocado manually.")
    return cart


def batches(items):
    for start in range(0, len(items), edit.MAX_CHANGES):
        yield items[start:start + edit.MAX_CHANGES]


def run(trigger: str) -> dict:
    if os.environ.get("OCADO_WEEKLY_ENABLED") != "true":
        raise ocado.OcadoError("Weekly Ocado is disabled. Run make ocado-enable-weekly on the droplet first.")
    if not isinstance(trigger, str) or trigger.strip().lower() not in {"weekly ocado", "make my weekly ocado"}:
        raise ocado.OcadoError("Run this workflow only for the user's explicit 'weekly ocado' request.")
    with edit.edit_lock():
        recipe = load_recipe()
        # Include one-off bookings as well: choose the next delivery, never skip to a later editable one.
        order = ocado.next_order(reserved_only=False)["order"]
        if order is None:
            raise ocado.OcadoError("There is no upcoming booked order. No trolley was created.")
        path = run_path(order["order_id"])
        previous = read_run(path)
        if previous:
            if previous.get("state") == "ready_for_checkout":
                return {"status": "already_prepared", "delivery": previous["delivery"],
                        "prepared_at": previous["prepared_at"],
                        "note": "Weekly Ocado already prepared this booked order. This request made no changes, preserving any later edits. Check Ocado for its current contents and saving/checkout status."}
            raise ocado.OcadoError("A weekly run was already attempted for this order and needs manual review. It will not be repeated or cleared automatically.")
        edit.validate_editable_order(order)
        baseline = {item["product_id"]: item["quantity"] for item in order["items"] if item["quantity"]}
        if len({item["product_id"] for item in order["items"]}) != len(order["items"]):
            raise ocado.OcadoError("The order contains ambiguous duplicate products. Use Ocado's website.")
        session = ocado.load_session()
        state = {"state": "starting", "order_id": order["order_id"], "delivery": order["delivery"],
                 "recipe": recipe, "original_order": order, "prepared_at": time.time()}
        phase = "preflight"
        with edit.client_for(session) as client:
            try:
                token = edit.request_token(client)
                if edit.cart_context(client, token)["isOrderUnderEdit"]:
                    raise ocado.OcadoError("An order is already being edited. Finish or cancel it on Ocado before running weekly Ocado.")
                # Keep the standalone trolley snapshot private for diagnosing an interrupted run.
                state["original_standalone_trolley"] = edit.raw_cart(client)
                yoto.write_private(path, state)  # Claim this order before the first remote write.
                phase = "opening_order"
                try:
                    edit.post_edit(client, token, edit.START_EDIT, {"orderId": order["order_id"], "regionId": order["region_id"]})
                finally:
                    edit.persist_cookies(client, session)
                token = edit.request_token(client)
                assert_cart(client, token, order["order_id"], baseline)
                expected = dict(baseline)
                phase = "clearing_order"
                removals = [{"productId": pid, "quantity": -quantity} for pid, quantity in baseline.items()]
                for batch in batches(removals):
                    assert_cart(client, token, order["order_id"], expected)
                    try:
                        edit.post_edit(client, token, edit.APPLY_QUANTITY, batch)
                    finally:
                        edit.persist_cookies(client, session)
                    expected.update({item["productId"]: 0 for item in batch})
                    assert_cart(client, token, order["order_id"], expected)
                # Verify the order is empty before adding only the fixed weekly products.
                assert_cart(client, token, order["order_id"], {})
                phase = "adding_weekly_items"
                additions = [{"productId": item["product_id"], "quantity": item["quantity"]} for item in recipe["items"]]
                for batch in batches(additions):
                    assert_cart(client, token, order["order_id"], expected)
                    try:
                        edit.post_edit(client, token, edit.APPLY_QUANTITY, batch)
                    finally:
                        edit.persist_cookies(client, session)
                    expected.update({item["productId"]: item["quantity"] for item in batch})
                    assert_cart(client, token, order["order_id"], expected)
                phase = "verifying_weekly_trolley"
                cart = assert_cart(client, token, order["order_id"], expected)
                summary = ocado.normalise_trolley(cart, [])
                edit.persist_cookies(client, session)
                state.update(state="ready_for_checkout", subtotal=summary["subtotal"], currency=summary["currency"])
                yoto.write_private(path, state)
                return {"status": "ready_for_checkout", "delivery": order["delivery"], "items": recipe["items"],
                        "subtotal": summary["subtotal"], "currency": summary["currency"],
                        "checkout_url": ocado.ORIGIN + "/checkout/summary",
                        "note": "Notify the user: the booked order's edit trolley now contains exactly the weekly list. Open Ocado and review/save/check out before the deadline. Checkout was NOT submitted and the order is NOT confirmed as updated. Stock, minimum spend and checkout checks remain for Ocado."}
            except (ocado.OcadoError, OSError) as error:
                if phase != "preflight":
                    state.update(state="needs_review", failed_phase=phase)
                    try:
                        yoto.write_private(path, state)
                    except OSError:
                        pass  # Persisted starting state still blocks a duplicate attempt.
                    message = str(error) if isinstance(error, ocado.OcadoError) else "Private workflow state could not be saved."
                    raise ocado.OcadoError(message + " Weekly preparation stopped; the order may be partially changed. Notify the user to review Ocado. Do not retry, substitute products or claim completion.") from None
                raise
