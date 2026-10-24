"""Replace a booked order with a privately saved weekly list; never check out."""

import json
import os
import re
import secrets
import time

from household_tools.integrations import ocado, ocado_edit as edit, yoto

MAX_ITEMS = 100
RUN_STATES = {"starting", "ready_for_checkout", "needs_review"}


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
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise ocado.OcadoError("Each weekly product needs its exact confirmed name.")
        pid = ocado.require_product_id(pid, name)
        if pid in seen:
            raise ocado.OcadoError("Weekly product IDs must be distinct.")
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, ocado.OcadoError):
            raise
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
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("state") not in RUN_STATES:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ocado.OcadoError("A previous weekly record is unreadable or has an unknown format. Repair the private record before rebuilding; no order was changed.") from None


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


def redo_path(plan_id: str):
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ocado.OcadoError("Invalid weekly redo plan. Prepare a new preview.")
    return yoto.home() / "ocado-weekly-redo-plans" / (plan_id + ".json")


def load_redo(plan_id: str) -> dict:
    try:
        value = json.loads(redo_path(plan_id).read_text())
        if not isinstance(value, dict) or value.get("state") != "prepared" or value.get("expires_at", 0) <= time.time():
            raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ocado.OcadoError("Weekly redo preview expired, was already attempted, or could not be read. Prepare a new preview; never replay it.") from None


def validate_redo_order(order):
    # A confirmed redo can reuse the correct order's existing edit session.
    # Its identity and full contents are verified separately through cart_context.
    if order is None or type(order.get("is_under_edit")) is not bool:
        raise ocado.OcadoError("Could not verify the next order's edit status.")
    edit.validate_editable_order({**order, "is_under_edit": False})


def prepare_redo(recipes: list[str] | None = None) -> dict:
    if os.environ.get("OCADO_WEEKLY_ENABLED") != "true":
        raise ocado.OcadoError("Weekly Ocado is disabled. Run make ocado-enable-weekly first.")
    with edit.edit_lock():
        from household_tools.recipes import shopping_plan
        target = shopping_plan(recipes)
        order = ocado.next_order(reserved_only=False)["order"]
        validate_redo_order(order)
        previous = read_run(run_path(order["order_id"]))
        if not previous:
            raise ocado.OcadoError("There is no previous weekly attempt to redo. Use a normal weekly request.")
        session = ocado.load_session()
        with edit.client_for(session) as client:
            token = edit.request_token(client)
            context = edit.cart_context(client, token)
            cart = edit.raw_cart(client)
            if context["isOrderUnderEdit"]:
                if context.get("orderId") != order["order_id"]:
                    raise ocado.OcadoError("Another order is being edited. No redo preview can be prepared for this session.")
                baseline = {p: q for p, q in edit.quantities(cart).items() if q}
            else:
                baseline = {item["product_id"]: item["quantity"] for item in order["items"] if item["quantity"]}
                if len({item["product_id"] for item in order["items"]}) != len(order["items"]):
                    raise ocado.OcadoError("The order has ambiguous duplicate products.")
        labels = {item["product_id"]: item["name"] for item in order["items"]}
        labels.update(session.get("product_names", {}))
        labels.update({item["product_id"]: item["name"] for item in previous.get("recipe", {}).get("items", [])})
        labels.update({item["product_id"]: item["name"] for item in target["items"]})
        desired = {item["product_id"]: item["quantity"] for item in target["items"]}
        changes = [{"product_id": pid, "name": labels.get(pid), "before": baseline.get(pid, 0), "after": desired.get(pid, 0)}
                   for pid in sorted(baseline.keys() | desired.keys()) if baseline.get(pid, 0) != desired.get(pid, 0)]
        plan_id = secrets.token_hex(16)
        plan = {"state": "prepared", "expires_at": time.time() + edit.PLAN_TTL,
                "order_id": order["order_id"], "delivery": order["delivery"], "target": target,
                "previous_fingerprint": edit.fingerprint(previous), "order_fingerprint": edit.order_fingerprint(order),
                "cart_fingerprint": edit.fingerprint(edit.quantities(cart)), "already_editing": context["isOrderUnderEdit"],
                "baseline": baseline, "changes": changes}
        yoto.write_private(redo_path(plan_id), plan)
        return {"plan_id": plan_id, "delivery": order["delivery"], "changes": changes,
                "items": target["items"], "recipes": target["recipes"], "expires_in_seconds": edit.PLAN_TTL,
                "note": "Preview only. Show the delivery, complete replacement list and before/after changes. Redo removes ALL current order trolley items, including later additions, then rebuilds from staples plus these recipes. Wait for explicit user confirmation before applying. Checkout remains manual."}


def apply_redo(plan_id: str, confirmed: bool = False) -> dict:
    if confirmed is not True:
        raise ocado.OcadoError("Show the redo preview and obtain explicit user confirmation before replacing this order again.")
    plan = load_redo(plan_id)
    return run("weekly ocado", [recipe["id"] for recipe in plan["target"]["recipes"]], _redo_plan_id=plan_id)


def run(trigger: str, recipes: list[str] | None = None, *, _redo_plan_id: str | None = None) -> dict:
    if os.environ.get("OCADO_WEEKLY_ENABLED") != "true":
        raise ocado.OcadoError("Weekly Ocado is disabled. Run make ocado-enable-weekly on the droplet first.")
    match = re.fullmatch(r"(?:(?:redo|rebuild) )?(?:make my )?weekly ocado(?: again)?(?:\s*,?\s+with\s+(.+))?", trigger.strip(), re.IGNORECASE) if isinstance(trigger, str) else None
    if not match:
        raise ocado.OcadoError("Run this workflow only for the user's explicit 'weekly ocado' request.")
    if match[1] and not recipes:
        raise ocado.OcadoError("This request names recipes. Resolve every requested recipe and pass their names; do not run a staples-only shop.")
    with edit.edit_lock():
        from household_tools.recipes import shopping_plan
        recipe = shopping_plan(recipes, match[1])
        # Include one-off bookings as well: choose the next delivery, never skip to a later editable one.
        order = ocado.next_order(reserved_only=False)["order"]
        if order is None:
            raise ocado.OcadoError("There is no upcoming booked order. No trolley was created.")
        path = run_path(order["order_id"])
        previous = read_run(path)
        redo = load_redo(_redo_plan_id) if _redo_plan_id is not None else None
        repeat = bool(previous) and redo is None
        if redo:
            if not previous:
                raise ocado.OcadoError("The previous weekly record changed. Prepare a new preview.")
            validate_redo_order(order)
            if (redo["order_id"] != order["order_id"] or redo["order_fingerprint"] != edit.order_fingerprint(order)
                    or redo["previous_fingerprint"] != edit.fingerprint(previous) or redo["target"] != recipe):
                raise ocado.OcadoError("The order, weekly list, recipes or prior run changed since the redo preview. Prepare a new preview and confirm it again.")
        elif previous:
            validate_redo_order(order)
        if not redo and not repeat:
            edit.validate_editable_order(order)
        baseline = redo["baseline"] if redo else {item["product_id"]: item["quantity"] for item in order["items"] if item["quantity"]}
        if len({item["product_id"] for item in order["items"]}) != len(order["items"]):
            raise ocado.OcadoError("The order contains ambiguous duplicate products. Use Ocado's website.")
        session = ocado.load_session()
        state = {"state": "starting", "order_id": order["order_id"], "delivery": order["delivery"],
                 "recipe": recipe, "original_order": order, "prepared_at": time.time()}
        phase = "preflight"
        with edit.client_for(session) as client:
            try:
                token = edit.request_token(client)
                context = edit.cart_context(client, token)
                current_cart = edit.raw_cart(client)
                already_editing = False
                if redo:
                    if (context["isOrderUnderEdit"] != redo["already_editing"]
                            or (context["isOrderUnderEdit"] and context.get("orderId") != order["order_id"])
                            or edit.fingerprint(edit.quantities(current_cart)) != redo["cart_fingerprint"]):
                        raise ocado.OcadoError("The trolley or editing session changed since the redo preview. Prepare a fresh preview and confirmation.")
                    already_editing = context["isOrderUnderEdit"]
                    archive = yoto.home() / "ocado-weekly-history" / f"{edit.fingerprint(order['order_id'])}-{time.time_ns()}.json"
                    yoto.write_private(archive, previous)
                    redo["state"] = "applying"
                    yoto.write_private(redo_path(_redo_plan_id), redo)
                elif repeat:
                    if context["isOrderUnderEdit"] and context.get("orderId") != order["order_id"]:
                        raise ocado.OcadoError("Another order is being edited. The requested weekly order was not changed.")
                    already_editing = context["isOrderUnderEdit"]
                    if already_editing:
                        baseline = {p: q for p, q in edit.quantities(current_cart).items() if q}
                    archive = yoto.home() / "ocado-weekly-history" / f"{edit.fingerprint(order['order_id'])}-{time.time_ns()}.json"
                    yoto.write_private(archive, previous)
                elif context["isOrderUnderEdit"]:
                    raise ocado.OcadoError("An order is already being edited. Finish or cancel it on Ocado before running weekly Ocado.")
                for product_id in baseline:
                    ocado.require_product_id(product_id, "existing order item")
                # Keep the standalone trolley snapshot private for diagnosing an interrupted run.
                state["original_standalone_trolley"] = current_cart if not already_editing else None
                if redo:
                    state["redo_plan_id"] = _redo_plan_id
                if redo or repeat:
                    state["original_edit_trolley"] = current_cart if already_editing else None
                if repeat:
                    state["repeated_by_user_request"] = True
                    state["recovered_from_state"] = previous["state"]
                yoto.write_private(path, state)  # Claim this order before the first remote write.
                phase = "opening_order"
                if not already_editing:
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
                if redo:
                    redo["state"] = "applied"
                    yoto.write_private(redo_path(_redo_plan_id), redo)
                return {"status": "ready_for_checkout", "delivery": order["delivery"], "items": recipe["items"],
                        "recipes": recipe["recipes"], "breakdown": recipe["breakdown"],
                        "subtotal": summary["subtotal"], "currency": summary["currency"],
                        "checkout_url": ocado.ORIGIN + "/checkout/summary",
                        "note": "Notify the user: the booked order's edit trolley now contains exactly the weekly list. Open Ocado and review/save/check out before the deadline. Checkout was NOT submitted and the order is NOT confirmed as updated. Stock, minimum spend and checkout checks remain for Ocado."}
            except (ocado.OcadoError, OSError) as error:
                if phase != "preflight":
                    state.update(state="needs_review", failed_phase=phase, failed_at=time.time())
                    status = re.search(r"HTTP (\d{3})", str(error)) if isinstance(error, ocado.OcadoError) else None
                    if status:
                        state["failure_http_status"] = int(status[1])
                    try:
                        yoto.write_private(path, state)
                    except OSError:
                        pass  # Persisted starting state still blocks a duplicate attempt.
                    message = str(error) if isinstance(error, ocado.OcadoError) else "Private workflow state could not be saved."
                    raise ocado.OcadoError(message + f" Weekly preparation stopped during {phase}; the order may be partially changed. Notify the user. Do not retry automatically or claim completion. A new explicit weekly request can rebuild from fresh order/trolley reads; this record is not a permanent lock.") from None
                raise
