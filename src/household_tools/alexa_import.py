"""Resolve Alexa item names to saved Ocado preferences; unknown names never shop."""

import json

from household_tools import occasional, recipes
from household_tools.integrations import alexa, ocado, ocado_edit as edit, ocado_weekly as weekly, yoto


def mapping_path():
    return yoto.home() / "alexa-products.json"


def mappings():
    if not mapping_path().exists():
        return {}
    try:
        value = json.loads(mapping_path().read_text())
        if not isinstance(value, dict) or len(value) > 300:
            raise ValueError
        for name, row in value.items():
            if (name != recipes.key(name) or not name or not isinstance(row, dict)
                    or set(row) != {"source", "target"} or row["source"] not in {"weekly", "occasional"}
                    or not isinstance(row["target"], str)):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError):
        raise ocado.OcadoError("The private Alexa product aliases need fixing before import; no order was changed.") from None


def save_mapping(name: str, source: str, target: str, confirmed: bool = False):
    if not isinstance(name, str) or not recipes.key(name) or len(name) > 120 or source not in {"weekly", "occasional"}:
        raise ocado.OcadoError("Use a short Alexa item name and a saved weekly product or occasional preference.")
    if confirmed is not True:
        raise ocado.OcadoError("Confirm which saved Ocado product/preference this Alexa name should mean before saving the alias.")
    if source == "weekly":
        if target not in {item["product_id"] for item in weekly.load_recipe()["items"]}:
            raise ocado.OcadoError("The target must be an exact product UUID already in the weekly staples.")
    else:
        occasional.resolve([target])  # Reject unknown/ambiguous/unqualified flavours.
    with edit.edit_lock():
        value = mappings()
        value[recipes.key(name)] = {"source": source, "target": target}
        if len(value) > 300:
            raise ocado.OcadoError("The Alexa alias collection is full.")
        yoto.write_private(mapping_path(), value)
    return {"status": "saved", "name": name, "source": source, "target": target,
            "note": "Saved only a private name mapping; no Alexa list or Ocado order changed."}


def resolve(captured):
    aliases = mappings()
    staples = weekly.load_recipe()["items"]
    by_id = {item["product_id"]: item for item in staples}
    names = {}
    for item in staples:
        names.setdefault(recipes.key(item["name"]), []).append(item)
    entries, unresolved, preferences = [], [], {}
    for row in captured["items"]:
        term = recipes.key(row["name"])
        mapped = aliases.get(term)
        source, target = (mapped["source"], mapped["target"]) if mapped else (None, row["name"])
        if source == "weekly" or (source is None and term in names):
            candidates = [by_id[target]] if source == "weekly" and target in by_id else names.get(term, []) if source is None else []
            if len(candidates) != 1:
                unresolved.append({"name": row["name"], "reason": "Saved weekly product is missing or ambiguous."})
            else:
                entries.append({**row, "product": {**candidates[0], "quantity": 1}})
            continue
        try:
            preference = occasional.resolve([target])[0]
            identity = (preference["id"], preference.get("selected_product", {}).get("product_id"))
            preferences[identity] = preference
            entries.append({**row, "preference": identity})
        except ocado.OcadoError:
            unresolved.append({"name": row["name"], "reason": "No unambiguous saved product/preference. Confirm a product or jam flavour and save an alias first."})
    if unresolved:
        return {"status": "needs_products", "unresolved": unresolved, "items": [], "list_id": captured["list_id"]}
    chosen = occasional.choose(list(preferences.values())) if preferences else []
    products = {identity: value["product"] for identity, value in zip(preferences, chosen)}
    for entry in entries:
        if "preference" in entry:
            entry["product"] = products[entry.pop("preference")]
    return {"status": "ready", "items": entries, "unresolved": [], "list_id": captured["list_id"]}


def preview():
    return resolve(alexa.snapshot())


def combine(base, captured, previous=None):
    # Completed entries from this exact order survive explicit Alexa-enabled rebuilds.
    old = previous.get("recipe", {}).get("alexa") if previous else None
    if old and old["list_id"] != captured["list_id"]:
        raise ocado.OcadoError("The Alexa list changed since this order's import. Resolve its saved import before rebuilding; no order changed.")
    rows = {item["id"]: {k: v for k, v in item.items() if k in {"id", "name", "version", "status", "verified_for_order"}}
            for item in old["items"] if previous["state"] == "ready_for_checkout" or item.get("verified_for_order") is True} if old else {}
    rows.update({item["id"]: item for item in captured["items"]})
    resolved = resolve({"list_id": captured["list_id"], "items": list(rows.values())})
    if resolved["unresolved"]:
        names = ", ".join(row["name"] for row in resolved["unresolved"])
        raise ocado.OcadoError(f"Alexa items need saved product choices: {names}. Clarify them before shopping; no order was changed.")
    result = {**base, "items": [dict(item) for item in base["items"]],
              "breakdown": [{**row, "sources": list(row["sources"])} for row in base["breakdown"]], "alexa": resolved}
    lookup = {item["product_id"]: item for item in result["items"]}
    breakdown = {item["product_id"]: item for item in result["breakdown"]}
    for row in resolved["items"]:
        product = row["product"]
        pid, wanted = product["product_id"], product["quantity"]
        existing = lookup.get(pid, {}).get("quantity", 0)
        # Alexa is a needs list: already covered quantities are not added again.
        increment = max(0, wanted - existing)
        row["already_covered"] = increment == 0
        if pid not in lookup:
            lookup[pid] = {**product, "quantity": 0}
            result["items"].append(lookup[pid])
            breakdown[pid] = {**product, "quantity": 0, "sources": []}
            result["breakdown"].append(breakdown[pid])
        lookup[pid]["quantity"] += increment
        breakdown[pid]["quantity"] += increment
        breakdown[pid]["sources"].append({"source": "Alexa: " + row["name"], "quantity": increment})
    result["items"] = weekly.validate_items(result["items"])
    return result
