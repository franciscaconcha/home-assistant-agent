# Recipes in the weekly Ocado shop

Store each recipe's shopping products privately, then ask on WhatsApp:

> Weekly Ocado, with recipe one and recipe two

The bot resolves **every** requested saved recipe before opening an order. It combines
weekly staples and the selected recipes, sums quantities for identical product IDs,
then uses the existing clear/refill/verify workflow for the next booked order. It leaves
checkout to you. Your staples file is not changed by recipe selection.

An unknown or ambiguous name, unresolved product, duplicate selection or invalid quantity
stops the workflow before contacting Ocado. It never silently shops only the staples or
invents ingredients/products. Recipe names match case-insensitively and support aliases;
spelling guesses do not automatically select a recipe.

## Where recipes live

On the droplet, each recipe is a JSON file under:

```text
~/hermes-home/data/recipes/
```

Inside the container, this is `/opt/data/recipes/`. Files remain outside Git and Docker
images. Edit a recipe directly with `nano` or ask the bot to save/update it in WhatsApp.
No rebuild or restart is needed when adding or editing a recipe file.

The filename must match its `id`, using lowercase words separated by hyphens. Structure:

```json
{
  "id": "recipe-one",
  "name": "Recipe one",
  "servings": null,
  "aliases": [],
  "notes": "Shopping quantities for one preparation.",
  "items": [
    {
      "product_id": null,
      "name": "EXACT_PRODUCT_AND_PACK_SIZE_TO_CHOOSE",
      "quantity": 1
    }
  ]
}
```

`product_id: null` marks an unresolved draft ingredient. A recipe with any such entry is
listed as **needs_products** and cannot be used to modify an order. Replace null with the
actual internal Ocado product ID once the exact product and pack size are confirmed.
Never invent an ID or assume a legacy numeric product-page URL is its internal cart ID.

Recipe items are **packs to buy for one preparation**, not cooking weights or quantities
per person. Store cooking details in `notes`. `servings` is optional descriptive metadata;
this version does not automatically scale quantities, deduct pantry ingredients, combine
different products by ingredient name, or optimise multiple partial packs into one pack.
If staples need 4 packs and a selected recipe needs 1 of the same product, the shop buys 5.

## Working through WhatsApp

- **List my recipes** uses `household_list_recipes`, including mapping status.
- **Show recipe one** uses `household_get_recipe` to read its full shopping list.
- **Save/update recipe one with these items…** uses `household_save_recipe`. The bot can
  save drafts immediately. Exact products/pack sizes/counts must be confirmed before
  saving mapped products with `confirmed=true`. Existing recipe files are backed up under
  `data/recipe-backups/` when updated through the tool.
- **Preview weekly Ocado with recipe one and recipe two** uses `ocado_weekly_plan`.
  This reads local files only and shows which recipe/staples contributed each quantity.
- **Weekly Ocado, with recipe one and recipe two** uses `ocado_weekly_shop`, passing the
  complete message as `trigger` and all requested names in the `recipes` list. It prepares
  the booked order without another confirmation once the recipe mappings are complete.

Product IDs need a genuine Ocado source: live order details, trolley data, or the normal
browser session capture. If Ocado challenges search/product requests, use the normal
browser and capture the products with the existing [Ocado login helper](ocado.md).
Do not guess IDs, retry blocked endpoints in a loop, or replace requested products.

Every new direct **weekly Ocado** request rebuilds the next booked order, including an
already prepared delivery, without asking for confirmation. It replaces later additions
and uses only the recipes named in the new request. A plain **weekly Ocado** uses only
staples. **Redo weekly Ocado with recipe one and recipe two** works the same way. See
[repeat requests](ocado.md#redo-an-already-prepared-weekly-order). Failed/uncertain runs
still need manual review; the bot does not automatically retry a failed tool call.

## Upgrade the droplet

```sh
cd ~/hermes-home
git pull --ff-only
make build
make down
make ocado-enable-weekly
make up
```

Re-running `ocado-enable-weekly` adds the recipe tools and new recipe guidance while
preserving existing settings, the staples file, and personal instructions. The enable
command backs up config/instructions and does not run an Ocado shop. Then send
`/reload-mcp` on WhatsApp.

If recipes were created on the Mac, copy **only the recipes directory**, not the staples
file or the whole data directory:

```sh
scp -i ~/.ssh/hermes_digitalocean -r \
  /Users/fcr/repositories/hermes-home/data/recipes \
  hermes@YOUR_DROPLET_IP:~/hermes-home/data/
```

This copies files with their current mapping status; it does not turn drafts into ready
recipes. Avoid overwriting a newer recipe you have edited on the droplet. Alternatively,
create the recipes directly through WhatsApp after upgrading.

The combined recipe shop is covered by mocked HTTP, private-file validation and MCP
subprocess checks. No live order was changed during development. Verify the first actual
combined shop on Ocado and finish the order's save/checkout before its deadline.
