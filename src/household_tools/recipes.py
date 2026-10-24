"""Private, manually editable recipe shopping lists; no network requests."""

import json
import re
import time
import unicodedata

from household_tools.integrations import ocado, ocado_edit as edit, ocado_weekly as weekly, yoto

MAX_RECIPES = 200
MAX_SELECTED = 20


def directory():
    return yoto.home() / "recipes"


def recipe_path(recipe_id: str):
    if not isinstance(recipe_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", recipe_id) or len(recipe_id) > 100:
        raise ocado.OcadoError("Recipe IDs must use lowercase words separated by hyphens, such as greek-chicken.")
    return directory() / (recipe_id + ".json")


def key(name: str) -> str:
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", name).casefold()).strip()


def validate(value, expected_id: str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - {"id", "name", "servings", "aliases", "notes", "items"}:
        raise ocado.OcadoError("A recipe needs id, name and items; optional fields are servings, aliases and notes.")
    recipe_id, name = value.get("id"), value.get("name")
    recipe_path(recipe_id)
    if expected_id is not None and recipe_id != expected_id:
        raise ocado.OcadoError("The recipe ID must match its JSON filename.")
    if not isinstance(name, str) or not key(name) or len(name) > 120:
        raise ocado.OcadoError("A recipe needs a name of at most 120 characters.")
    servings = value.get("servings")
    if servings is not None and (type(servings) is not int or not 1 <= servings <= 30):
        raise ocado.OcadoError("Recipe servings must be an integer from 1 to 30, or null if unspecified.")
    aliases, notes, items = value.get("aliases", []), value.get("notes", ""), value.get("items")
    if not isinstance(aliases, list) or len(aliases) > 20 or any(
        not isinstance(alias, str) or not key(alias) or len(alias) > 120 for alias in aliases):
        raise ocado.OcadoError("Recipe aliases must be a list of up to 20 short names.")
    if not isinstance(notes, str) or len(notes) > 10000:
        raise ocado.OcadoError("Recipe notes must be text of at most 10000 characters.")
    if not isinstance(items, list) or len(items) > weekly.MAX_ITEMS:
        raise ocado.OcadoError("Recipe items must be a list of up to 100 products.")
    clean, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"product_id", "name", "quantity"}:
            raise ocado.OcadoError("Each recipe item needs product_id (or null for a draft), name and quantity in packs.")
        pid = item["product_id"]
        checked = weekly.validate_items([{**item, "product_id": "00000000-0000-0000-0000-000000000000" if pid is None else pid}])[0]
        if pid is not None:
            pid = checked["product_id"]
            if pid in seen:
                raise ocado.OcadoError("Combine repeated products within a recipe into one quantity.")
            seen.add(pid)
        clean.append({**checked, "product_id": pid})
    return {"id": recipe_id, "name": name.strip(), "servings": servings,
            "aliases": list(dict.fromkeys(alias.strip() for alias in aliases)), "notes": notes, "items": clean}


def ready(recipe: dict) -> bool:
    return all(item["product_id"] is not None for item in recipe["items"])


def names(recipe: dict) -> set[str]:
    return {key(name) for name in [recipe["id"], recipe["name"], *recipe["aliases"]]}


def index(values: list[dict]) -> dict:
    result = {}
    for recipe in values:
        for name in names(recipe):
            if name in result and result[name]["id"] != recipe["id"]:
                raise ocado.OcadoError("Recipe names or aliases are ambiguous. Give each recipe a distinct name/alias before shopping.")
            result[name] = recipe
    return result


def load_all() -> list[dict]:
    try:
        paths = sorted(directory().glob("*.json"))
        if len(paths) > MAX_RECIPES:
            raise ocado.OcadoError(f"The recipe collection is limited to {MAX_RECIPES} recipes.")
        values = []
        for path in paths:
            if path.is_symlink() or path.stat().st_size > 500000:
                raise ocado.OcadoError("Recipe files must be ordinary JSON files smaller than 500KB.")
            values.append(validate(json.loads(path.read_text()), path.stem))
        index(values)
        return values
    except (OSError, ValueError) as error:
        if isinstance(error, ocado.OcadoError):
            raise
        raise ocado.OcadoError("A recipe file could not be read. Fix its JSON before planning a recipe shop; no order was changed.") from None


def list_recipes() -> list[dict]:
    return [{"id": recipe["id"], "name": recipe["name"], "aliases": recipe["aliases"],
             "servings": recipe["servings"], "status": "ready" if ready(recipe) else "needs_products",
             "shopping_item_lines": len(recipe["items"])} for recipe in load_all()]


def resolve(requested: list[str]) -> list[dict]:
    if not isinstance(requested, list) or len(requested) > MAX_SELECTED or any(
        not isinstance(name, str) or not key(name) or len(name) > 120 for name in requested):
        raise ocado.OcadoError(f"Choose up to {MAX_SELECTED} saved recipe names.")
    if not requested:
        return []  # A plain weekly shop does not depend on the recipe collection.
    values = load_all()
    lookup, result, seen = index(values), [], set()
    for name in requested:
        recipe = lookup.get(key(name))
        if recipe is None:
            available = ", ".join(recipe["name"] for recipe in values[:10]) or "none saved yet"
            raise ocado.OcadoError(f"No exact saved recipe matches '{name}'. Available recipes: {available}. Save it or clarify the name before shopping; no order was changed.")
        if recipe["id"] in seen:
            raise ocado.OcadoError("The same recipe was selected more than once, possibly through an alias. Clarify before shopping.")
        seen.add(recipe["id"])
        result.append(recipe)
    return result


def get_recipe(name: str) -> dict:
    recipe = resolve([name])[0]
    return {**recipe, "status": "ready" if ready(recipe) else "needs_products"}


def save_recipe(recipe_id: str, name: str, items: list[dict], servings: int | None = None,
                aliases: list[str] | None = None, notes: str = "", confirmed: bool = False) -> dict:
    value = validate({"id": recipe_id, "name": name, "items": items, "servings": servings,
                      "aliases": aliases if aliases is not None else [], "notes": notes})
    if ready(value) and confirmed is not True:
        raise ocado.OcadoError("Confirm the recipe's exact products, pack sizes and quantities before saving it as ready to shop. Use product_id=null for unresolved draft items.")
    with edit.edit_lock():
        values = load_all()
        others = [recipe for recipe in values if recipe["id"] != recipe_id]
        if len(others) >= MAX_RECIPES:
            raise ocado.OcadoError("The recipe collection is full.")
        index([*others, value])  # Refuse colliding aliases without replacing the original.
        previous = next((recipe for recipe in values if recipe["id"] == recipe_id), None)
        if previous is not None:
            yoto.write_private(yoto.home() / "recipe-backups" / f"{recipe_id}-{time.time_ns()}.json", previous)
        yoto.write_private(recipe_path(recipe_id), value)
    return {"status": "recipe_saved", "recipe": {**value, "status": "ready" if ready(value) else "needs_products"},
            "note": "Only the private recipe file changed. No Ocado order or weekly staples changed. Quantities are packs for one preparation; servings are descriptive, not automatic scaling."}


def check_request_text(text: str, selected: list[dict]) -> None:
    # Account for the entire 'with ...' phrase, not only names supplied by the model.
    # Names/aliases can themselves contain 'and'; never split ingredient-like phrases.
    remaining = key(text)
    for position, recipe in enumerate(selected):
        variants = [remaining]
        if position and remaining.startswith("and "):
            variants.append(remaining[4:])
        matches = [(alias, variant) for variant in variants for alias in names(recipe)
                   if variant == alias or variant.startswith(alias + " ")]
        if not matches:
            raise ocado.OcadoError("The requested recipe phrase does not match the selected recipes in order. Resolve every named recipe; no order was changed.")
        alias, variant = max(matches, key=lambda pair: len(pair[0]))
        remaining = variant[len(alias):].strip()
    if remaining:
        raise ocado.OcadoError("Some requested recipe text was not resolved. Include every named recipe or clarify the request; no order was changed.")


def shopping_plan(requested: list[str] | None = None, request_text: str | None = None) -> dict:
    base = weekly.load_recipe()
    selected = resolve(requested if requested is not None else [])
    if request_text is not None:
        check_request_text(request_text, selected)
    combined, sources = {}, {}

    def add(item, source):
        pid = item["product_id"]
        if pid not in combined:
            combined[pid] = {**item, "quantity": 0}
            sources[pid] = []
        combined[pid]["quantity"] += item["quantity"]
        sources[pid].append({"source": source, "quantity": item["quantity"]})

    for item in base["items"]:
        add(item, "weekly staples")
    for recipe in selected:
        missing = [item["name"] for item in recipe["items"] if item["product_id"] is None]
        if missing:
            raise ocado.OcadoError(f"Recipe '{recipe['name']}' still needs exact Ocado products for: {', '.join(missing)}. Resolve them before shopping; no order was changed.")
        for item in recipe["items"]:
            add(item, recipe["name"])
    items = weekly.validate_items(list(combined.values()))  # Bounds also apply to summed quantities.
    return {"mode": "ready_for_checkout", "items": items,
            "recipes": [{"id": recipe["id"], "name": recipe["name"], "servings": recipe["servings"]} for recipe in selected],
            "breakdown": [{**item, "sources": sources[item["product_id"]]} for item in items],
            "note": "Plan only; no Ocado order changed. Quantities add across staples and recipes, in packs. No automatic serving scaling, pantry deduction or merging of different product IDs."}
