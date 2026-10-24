"""Private named occasional products and explicit price-selection rules."""

import json
import time
from decimal import Decimal

from household_tools import recipes
from household_tools.integrations import ocado, ocado_edit as edit, yoto

MAX_ENTRIES = 100


def path():
    return yoto.home() / "ocado-occasional.json"


def validate(entry) -> dict:
    fields = {"id", "name", "aliases", "quantity", "selection", "minimum_discount_percent", "products", "notes"}
    if not isinstance(entry, dict) or set(entry) - fields:
        raise ocado.OcadoError("Invalid occasional-item fields.")
    products, quantity = entry.get("products"), entry.get("quantity", 1)
    if not isinstance(products, list) or not 1 <= len(products) <= 20:
        raise ocado.OcadoError("Save 1 to 20 exact product options for an occasional item.")
    if any(not isinstance(p, dict) or not {"product_id", "name"} <= set(p) or set(p) - {"product_id", "name", "aliases"} for p in products):
        raise ocado.OcadoError("Each occasional option needs product_id (UUID or null for a draft), name and optional variant aliases.")
    checked = recipes.validate({"id": entry.get("id"), "name": entry.get("name"),
        "aliases": entry.get("aliases", []), "notes": entry.get("notes", ""),
        "items": [{"product_id": p["product_id"], "name": p["name"], "quantity": quantity} for p in products]})
    selection = entry.get("selection", "fixed")
    if selection not in {"fixed", "lowest_price", "discount", "named"}:
        raise ocado.OcadoError("Selection must be fixed, lowest_price, discount or named.")
    if selection == "fixed" and len(products) != 1:
        raise ocado.OcadoError("A fixed occasional item must have exactly one product.")
    discount = entry.get("minimum_discount_percent", 45)
    if type(discount) is not int or not 1 <= discount <= 100:
        raise ocado.OcadoError("Minimum discount must be an integer percentage from 1 to 100.")
    clean_products = []
    for product, checked_product in zip(products, checked["items"]):
        aliases = product.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 20 or any(
            not isinstance(alias, str) or not recipes.key(alias) or len(alias) > 120 for alias in aliases):
            raise ocado.OcadoError("Variant aliases must be short names such as 'strawberry jam'.")
        if selection == "named" and not aliases:
            raise ocado.OcadoError("Named product variants need aliases, such as 'strawberry jam'.")
        clean_products.append({"product_id": checked_product["product_id"], "name": checked_product["name"],
                               **({"aliases": list(dict.fromkeys(aliases))} if aliases else {})})
    return {"id": checked["id"], "name": checked["name"], "aliases": checked["aliases"],
            "quantity": quantity, "selection": selection, "minimum_discount_percent": discount,
            "products": clean_products,
            "notes": checked["notes"]}


def ready(entry):
    return all(p["product_id"] is not None for p in entry["products"])


def lookup(entries):
    result = {}
    for entry in entries:
        candidates = [(name, None) for name in recipes.names(entry)]
        if entry["selection"] == "named":
            candidates += [(recipes.key(alias), index) for index, product in enumerate(entry["products"])
                           for alias in product.get("aliases", [])]
        for name, variant in candidates:
            if name in result and (result[name][0]["id"], result[name][1]) != (entry["id"], variant):
                raise ocado.OcadoError("Occasional names/variant aliases are ambiguous; give each a distinct name.")
            result[name] = (entry, variant)
    return result


def load() -> list[dict]:
    if not path().exists():
        return []
    try:
        if path().is_symlink() or path().stat().st_size > 1000000:
            raise ValueError
        value = json.loads(path().read_text())
        if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list) or len(value["items"]) > MAX_ENTRIES:
            raise ValueError
        entries = [validate(item) for item in value["items"]]
        if len({item["id"] for item in entries}) != len(entries):
            raise ocado.OcadoError("Occasional item IDs must be distinct.")
        lookup(entries)
        return entries
    except (OSError, ValueError) as error:
        if isinstance(error, ocado.OcadoError):
            raise
        raise ocado.OcadoError("The occasional-item file is invalid. Fix its JSON before selecting extras; no order was changed.") from None


def list_items() -> list[dict]:
    return [{**item, "status": "ready" if ready(item) else
             "partially_ready" if item["selection"] == "named" and any(p["product_id"] for p in item["products"]) else "needs_products"}
            for item in load()]


def resolve(requested: list[str]) -> list[dict]:
    if not isinstance(requested, list) or len(requested) > recipes.MAX_SELECTED or any(
        not isinstance(name, str) or not recipes.key(name) or len(name) > 120 for name in requested):
        raise ocado.OcadoError("Choose up to 20 saved occasional-item names.")
    if not requested:
        return []
    by_name, chosen, seen = lookup(load()), [], set()
    for name in requested:
        match = by_name.get(recipes.key(name))
        if match is None:
            raise ocado.OcadoError(f"No saved occasional item matches '{name}'. Save the exact product/rule or clarify the name; no order was changed.")
        entry, variant = match
        if entry["selection"] == "named" and variant is None:
            choices = ", ".join(p["aliases"][0] for p in entry["products"])
            raise ocado.OcadoError(f"Specify the flavour/variant for '{entry['name']}', such as {choices}. No order was changed.")
        identity = (entry["id"], variant)
        if identity in seen:
            raise ocado.OcadoError("The same occasional item was selected more than once, possibly through an alias.")
        seen.add(identity)
        selected = {**entry, "requested_name": name}
        if variant is not None:
            selected["selected_product"] = entry["products"][variant]
            selected["aliases"] = [*entry["aliases"], *entry["products"][variant]["aliases"]]
        chosen.append(selected)
    return chosen


def get_item(name: str) -> dict:
    match = lookup(load()).get(recipes.key(name))
    if match is None:
        raise ocado.OcadoError(f"No saved occasional item matches '{name}'.")
    item, variant = match
    if variant is not None:
        return {**item, "selected_product": item["products"][variant],
                "status": "ready" if item["products"][variant]["product_id"] else "needs_products"}
    return {**item, "status": "ready" if ready(item) else "needs_products"}


def save_item(item_id: str, name: str, products: list[dict], quantity: int = 1,
              aliases: list[str] | None = None, selection: str = "fixed", minimum_discount_percent: int = 45,
              notes: str = "", confirmed: bool = False) -> dict:
    entry = validate({"id": item_id, "name": name, "products": products, "quantity": quantity,
        "aliases": aliases if aliases is not None else [], "selection": selection,
        "minimum_discount_percent": minimum_discount_percent, "notes": notes})
    if ready(entry) and confirmed is not True:
        raise ocado.OcadoError("Confirm the exact occasional products, pack sizes, quantity and selection rule before saving mapped IDs.")
    with edit.edit_lock():
        previous = load()
        updated = [item for item in previous if item["id"] != item_id] + [entry]
        if len(updated) > MAX_ENTRIES:
            raise ocado.OcadoError("The occasional-item list is full.")
        lookup(updated)
        if path().exists():
            yoto.write_private(yoto.home() / "occasional-backups" / f"{time.time_ns()}.json", {"items": previous})
        yoto.write_private(path(), {"items": updated})
    return {"status": "occasional_item_saved", "item": {**entry, "status": "ready" if ready(entry) else "needs_products"},
            "note": "Only the private occasional list changed. No staples, recipes or Ocado order changed."}


def price_quotes(product_ids: list[str]) -> dict:
    ids = list(dict.fromkeys(ocado.require_product_id(pid) for pid in product_ids))
    if not 1 <= len(ids) <= 100:
        raise ocado.OcadoError("Price comparison is limited to 100 saved product options.")
    session = ocado.load_session()
    with edit.client_for(session) as client:
        token = edit.request_token(client)
        response = ocado.read_request(client, "PUT", ocado.PRODUCTS, json=ids, headers={"x-csrf-token": token})
        value = ocado.read_json(response)
    rows = value.get("products")
    if not isinstance(rows, list):
        raise ocado.OcadoError("Ocado's price response was not recognised. No order was changed.")
    quotes = {}
    for product in rows:
        if not isinstance(product, dict):
            raise ocado.OcadoError("Unrecognised product-price data.")
        pid = ocado.require_product_id(product.get("productId"))
        if pid not in ids:
            continue
        if pid in quotes or type(product.get("available")) is not bool:
            raise ocado.OcadoError("Could not verify product availability for the price comparison.")
        price = product.get("price") or {}
        if not isinstance(price, dict):
            raise ocado.OcadoError("Ocado's product price format was not recognised.")
        current, original = price.get("current") or {}, price.get("original") or {}
        if not isinstance(current, dict) or not isinstance(original, dict):
            raise ocado.OcadoError("Ocado's current/original price format was not recognised.")
        if original.get("currency", current.get("currency", "GBP")) != current.get("currency", "GBP"):
            raise ocado.OcadoError("The original and current prices use different currencies; the discount cannot be verified.")
        quotes[pid] = {"available": product["available"], "current": ocado.amount(current.get("amount")),
            "original": ocado.amount(original.get("amount")), "currency": current.get("currency", "GBP")}
    if set(quotes) != set(ids):
        raise ocado.OcadoError("Some saved product prices were not returned; the cheapest/offer choice cannot be verified.")
    return quotes


def choose(entries: list[dict]) -> list[dict]:
    for entry in entries:
        mapped = entry.get("selected_product", {}).get("product_id") is not None if entry["selection"] == "named" else ready(entry)
        if not mapped:
            raise ocado.OcadoError(f"Occasional item '{entry['name']}' still needs exact Ocado product UUIDs. Capture/select its products first; no order was changed.")
    ids = [p["product_id"] for entry in entries if entry["selection"] in {"lowest_price", "discount"} for p in entry["products"]]
    try:
        quotes = price_quotes(ids) if ids else {}
    except ocado.OcadoError as error:
        raise ocado.OcadoError("Could not verify current prices/offers for the requested occasional items: " + str(error) + " No order was changed. Ask the user to choose a fixed product or obtain a current price check; never guess.") from None
    chosen = []
    for entry in entries:
        info = {"id": entry["id"], "name": entry["name"], "selection": entry["selection"], "requested_name": entry["requested_name"]}
        if entry["selection"] == "fixed":
            product = entry["products"][0]
        elif entry["selection"] == "named":
            product = entry["selected_product"]
        else:
            eligible = []
            for candidate in entry["products"]:
                quote = quotes[candidate["product_id"]]
                if not quote["available"]:
                    continue
                if quote["current"] is None or quote["currency"] != "GBP":
                    raise ocado.OcadoError(f"A current GBP price for '{entry['name']}' could not be verified. No order was changed.")
                current, discount = Decimal(quote["current"]), None
                if entry["selection"] == "discount":
                    if quote["original"] is None or Decimal(quote["original"]) <= 0:
                        continue
                    discount = (1 - current / Decimal(quote["original"])) * 100
                    if discount < entry["minimum_discount_percent"]:
                        continue
                eligible.append((current, candidate["product_id"], candidate, quote, discount))
            if not eligible:
                raise ocado.OcadoError(f"No available saved option meets the price/offer rule for '{entry['name']}'. Ask the user before changing the order; do not omit or substitute it.")
            _, _, product, quote, discount = min(eligible, key=lambda row: (row[0], row[1]))
            info.update(price=quote["current"], currency="GBP")
            if discount is not None:
                info["discount_percent"] = str(discount.quantize(Decimal("0.01")))
        chosen.append({**info, "product": {"product_id": product["product_id"], "name": product["name"], "quantity": entry["quantity"]}})
    return chosen
