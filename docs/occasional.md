# Occasional items in the weekly shop

Save products you buy less often under short names, then ask:

> Weekly Ocado, detergent

Or combine them with recipes:

> Weekly Ocado with recipe one and detergent and toilet paper

The bot resolves all saved names/aliases, then combines **staples + requested recipes +
requested occasional items**. Quantities for the same product UUID add together. Items
not named in the current request are not added or carried over from an earlier run.
Unknown, ambiguous or unmapped names stop the request before any order change.
The existing repeat/recovery behavior and manual checkout remain unchanged.

## Private list and WhatsApp tools

The list lives at `~/hermes-home/data/ocado-occasional.json` on the droplet, or
`/opt/data/ocado-occasional.json` inside the container. It is excluded from Git/images.
Edit it manually without restarting, or use these through WhatsApp:

- `ocado_list_occasional_items`: view saved names, products and rules.
- `ocado_get_occasional_item`: inspect one entry or named variant.
- `ocado_save_occasional_item`: add/update an entry. Exact products, pack sizes, usual
  quantity and selection rule must be confirmed before saving mapped product UUIDs.
  Updates preserve other entries and back up the previous file under `occasional-backups/`.
- `ocado_weekly_plan` with `recipes` and `extras`: preview the combined list. Conditional
  rules perform read-only price checks; fixed/named products do not require a price lookup.
- `ocado_weekly_shop` with the actual `trigger`, all `recipes` and all `extras`: execute.

The JSON structure is:

```json
{
  "items": [
    {
      "id": "extra-item",
      "name": "Extra item",
      "aliases": ["short name"],
      "quantity": 1,
      "selection": "fixed",
      "minimum_discount_percent": 45,
      "products": [
        {"product_id": null, "name": "EXACT_PRODUCT_AND_PACK_SIZE"}
      ],
      "notes": "Usual quantity is one pack."
    }
  ]
}
```

Use the real internal product UUID from a normal browser capture or live order data.
Numeric catalogue codes in product-page URLs are invalid. `null` saves an unresolved
draft, which cannot be purchased. Quantity is packs/bottles/rolls, not items within a pack.
A fixed entry must have exactly one product option.

## Product selection rules

- `fixed`: add the one saved product and usual quantity.
- `lowest_price`: compare current **bottle/pack prices**, not price per wash/kilo/unit.
  Choose the lowest-price available option from the saved, approved products. The list
  of saved options defines the allowed brands and sizes; it does not search the whole shop.
- `discount`: require at least `minimum_discount_percent` off the currently displayed
  original price, then choose the lowest qualifying available pack price. Only verified
  current/original GBP prices count; unverified coupons or conditional multibuys are not
  assumed. If no option qualifies, ask the user instead of omitting or substituting it.
- `named`: the user chooses a flavour/variant via an option alias. Each option has an
  `aliases` list such as `["red spread"]`, alongside its exact product UUID and name.
  Save the preferred brand/pack size in that option and the group's notes. A bare group
  name requires a variant choice. Named variants are not rotated or price-shopped.
  One mapped variant can be used while another remains a draft. New flavours require
  their own product UUID once, while keeping the saved brand/size preference.

Multiple named variants from the same group can be selected in one request. Selecting
the same variant twice through different aliases is rejected to avoid accidental duplicates.

## Price checks and stock

Conditional rules use a bounded, read-only PUT to Ocado's product metadata endpoint,
with the normal Orders-page request token. That PUT reads product information; it does
not edit the trolley. Only UUID lists of up to 100 products are allowed. It returns current
and original price plus availability; arbitrary routes/requests remain blocked.

**Current limitation:** the product metadata request returned HTTP 202 in a live Mac check.
That means current prices cannot be verified through this endpoint in that session.
The conditional rules stop before opening/clearing an order when blocked, rather than
pretending a saved price is current. A fixed saved product can still use the working cart
workflow. To proceed when prices are blocked, choose and save a specific fixed product
or obtain a working current price source; do not loop retries or invent a cheapest offer.
No stale-price fallback is implemented.

For conditional rules, unavailable options can be excluded only in favour of another
saved qualifying option. Fixed/named products are verified through the normal cart
readback. If stock or quantity limits are discovered only when adding them, the request
stops and the draft may be partial; checkout is never submitted. The bot must report the
problem rather than silently substitute or claim a completed shop.

## Enable on the droplet

```sh
cd ~/hermes-home
git pull --ff-only
make build
make down
make ocado-enable-weekly
make up
```

Send `/reload-mcp` on WhatsApp. The enable command adds the new tools and instructions
while preserving existing model, WhatsApp, staples and recipes.

If the private list was created on the Mac, copy just that file with your usual SSH options:

```sh
scp /Users/fcr/repositories/hermes-home/data/ocado-occasional.json \
  hermes@YOUR_DROPLET_IP:~/hermes-home/data/ocado-occasional.json
```

Copying drafts does not resolve their products. Use `make ocado-login` on the Mac and
browse/select exact products normally, then save the capture to obtain UUIDs. Register
all approved bottle/pack alternatives for conditional rules, and each desired named flavour.
Do not overwrite newer edits made on the droplet with an older Mac file.
