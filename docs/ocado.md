# Ocado trolley and scheduled orders

The read-only MCP tool `ocado_basket` returns the current
trolley's live quantities and known line prices/subtotal, with names saved during a normal
Mac browser visit and matched by product ID. Names are a snapshot, not a live lookup. It does
not read saved shopping lists or modify your basket, delivery details, orders or payments.

`ocado_next_order` separately reads the next scheduled **Ocado Reserved** order, including
its delivery window, order total, current editing eligibility, Ocado's
`guaranteedEditableBy` timestamp, and line items. These item names come from the live order
details, not the trolley-name snapshot. It does not enter edit mode or change the active
trolley. Use `reserved_only=false` to include one-off scheduled orders as well.

The connection uses Ocado's internal website API and your own browser cookies. It is
unofficial and may break when Ocado changes its website. Sessions expire; bot protection
may reject requests from a datacentre IP even when login on the Mac works. A saved session
alone is not a successful connection: run the live check on the droplet before relying on it.

## 1. Login on your Mac

In **Mac Terminal**, from the local repository:

```sh
cd /Users/fcr/repositories/hermes-home
git pull --ff-only
make ocado-login
```

This requires [uv](https://docs.astral.sh/uv/getting-started/installation/). The command
installs the optional Playwright browser dependency and Chromium, then opens a separate
visible browser. Sign in directly to Ocado and open your current trolley. Return to the
terminal and press Enter only once you can see your trolley in that browser.

The helper also saves product ID/name pairs from product responses and page data that the
normal browser visit already loaded. It prints the number captured. No extra product API
requests are sent by the helper. Names for items you add later may be unknown until you
repeat this browser sync and copy the updated session.

The helper does not read your password or automate checkout. It saves only Ocado-domain
cookies and browser metadata to ignored `data/credentials/ocado.json` with mode 0600.
The browser closes after export. Treat the session like a password: never send it over
WhatsApp, commit it, or paste it into chat.

## 2. Update the droplet and transfer the session

In **droplet Terminal**, from `~/hermes-home`:

```sh
git pull --ff-only
make build
```

In **Mac Terminal**, copy just the Ocado session (not the whole credentials directory):

```sh
scp -i ~/.ssh/hermes_digitalocean \
  /Users/fcr/repositories/hermes-home/data/credentials/ocado.json \
  hermes@YOUR_DROPLET_IP:~/hermes-home/data/credentials/ocado.json
```

Adjust the SSH key path as needed. The existing Yoto credentials directory already exists.
If setting up Ocado first, create `~/hermes-home/data/credentials` on the droplet as the
`hermes` user with mode 0700 before copying.

## 3. Enable and test on the droplet

```sh
make down
make ocado-configure
make ocado-check
make up
```

Run each command separately and inspect errors. `ocado-configure` backs up the existing
live config, enables `ocado_basket`, and explicitly passes `HERMES_HOME` to MCP. Model,
WhatsApp and Yoto settings are preserved. `ocado-check` reports how many item lines it
read without printing your full shopping data. If the check fails, `make up` still restores
your other agent features while we diagnose Ocado.

On **WhatsApp**, send `/reload-mcp`, then **Use ocado_basket to show my current trolley**.

## Read your next scheduled order

After pulling and rebuilding the latest version on the droplet, run:

```sh
make down
make ocado-configure
make ocado-order-check
make up
```

Existing sessions can be reused. On WhatsApp send `/reload-mcp`, then ask **Use
ocado_next_order to show my next Reserved delivery, whether it is editable, and its items**.
The check command prints the delivery window, editability, guaranteed-editing timestamp,
and item-line count; it does not dump the order ID, address, payment details or item names.

The reader uses the Orders page's normal request token, the website's fixed
`GetUpcomingOrders` GraphQL query, and a GET of the selected order's detail. Only fixed read queries are allowed; arbitrary GraphQL requests and mutations are rejected. The
nearest order is reported even if no longer editable, rather than silently selecting the
following week's order. No matching order is reported explicitly without creating a trolley.

`guaranteedEditableBy` is the name of the timestamp Ocado supplies. We do not promise
editing after that point or equate it to a later possible cut-off. If item details fail,
the valid delivery summary remains available and items are marked unavailable.

## Preview and stage changes to a Reserved order

This first editing version changes quantities or removes items **already in the next
Reserved order**. It does not add new products or submit checkout/payment. The writer is
disabled by default. To enable it on the droplet after pulling and rebuilding:

```sh
make down
make ocado-enable-editing
make up
```

The enable command backs up your runtime config and SOUL.md, preserves their existing
settings/instructions, and adds the editing tool and confirmation guidance. Then send
`/reload-mcp` on WhatsApp.

Ask, for example: **Show my next Reserved delivery and preview changing the milk to 1.**
The agent should identify the product from the live order, use `ocado_plan_order_changes`,
and show the delivery and exact before/after quantities. This preview does not change
anything at Ocado. Explicitly confirm that preview to stage it. A preview expires after
10 minutes; changed orders/trolleys require a fresh preview and confirmation.

The apply tool opens that specific order for editing, verifies the active trolley belongs
to it, sends quantity deltas once, and reads back the draft quantities. Quantity 0 removes
an item. It refuses an existing editing session and does not request a merge or clear a
standalone trolley. Avoid editing concurrently in the website/app while the bot runs:
the local lock coordinates this shared bot, not separate Ocado clients.

**After a successful draft edit, open Ocado, review the correct delivery, and save/check
out the changes yourself before the deadline.** `staged_pending_checkout` does not mean
the scheduled order has been saved. Finish or cancel that editing session on Ocado before
requesting another bot edit.

If opening or updating has an uncertain outcome, the plan is locked against replay. Check
Ocado manually; the tool does not automatically retry or roll back. If Ocado returns a
challenge such as HTTP 202, use the normal website and report the error. Never treat it
as a successful edit.

The write flow follows Ocado's public website code and passes mocked HTTP and container
checks. No live order was changed during development; the first real draft edit still
needs verification on the droplet.

## Limits and troubleshooting

- Authentication failure, a redirect, bot challenge, or unrecognised response causes an
  explicit error. It is not reported as an empty trolley. Empty `items` from the active-cart
  endpoint is reported as empty; check the account and website if that is unexpected.
- Product names come from the last normal Mac browser visit. Products not present in
  that snapshot have unknown names and trigger a warning; run the login/sync again.
  The droplet no longer calls the product-information endpoint because it returned HTTP
  202 during live testing. It reads only the trolley and uses saved names from the browser.
  This limitation is specific to `ocado_basket`; order-detail names use a different read endpoint.
- Missing prices remain unknown; delivery fees/promotions at checkout may change totals.
- HTTP 202/403/429 may indicate Ocado blocked the request. Do not loop retries or try to
  bypass the challenge. Sign in normally or retry later. A datacentre restriction may mean
  this connection needs a home-side component instead of direct droplet requests.
- If login expires, repeat the Mac login and copy only the new session. The tool reads
  the file on each call; it does not need an additional stored password.
- The shared agent's shell can read its mounted credential files. Read-only restrictions
  apply to the provided tool, not to arbitrary code run through the agent's terminal.

## Implementation reference

API routes and response shapes were checked against the community provider in
[open-supermarkets at aff6880](https://github.com/abracadabra50/open-supermarkets/blob/aff6880435ab3c43f4f79f3896dd4cf40142171c/src/providers/ocado.ts).
This is a source reference, not a claim that your account has been tested. We do not install
that provider's broader checkout/basket-editing toolset. See `THIRD_PARTY_NOTICES.md`.

Upcoming-order fields were also verified in Ocado's public application bundle
`/static/index-CKdmGrK1.js` (`GetUpcomingOrders`) and its normal authenticated Orders view.
The order-list and next Reserved order detail reads were verified on the Mac with the
operator's session. Droplet access remains subject to Ocado's session and network controls.

Editing contracts were inspected in that public bundle: the order-edit customer session,
active-cart quantity deltas, and the fixed cart-order identity query. These are internal
website routes, not a supported public customer API. No checkout, cancellation, payment,
or trolley-merge route is exposed by this implementation.

## Weekly Ocado: replace the next booked order

The optional `ocado_weekly_shop` workflow handles a direct **weekly Ocado** or
**make my weekly Ocado** message, including when addressing the bot in WhatsApp. That
request authorizes the whole saved workflow, so there is no extra preview/confirmation
for the replacement itself. The saved products must have been confirmed beforehand.

It selects the next booked delivery (Reserved or one-off), refuses a non-editable order
without skipping to a later delivery, opens its edit session, verifies the full starting
trolley, removes all its items, verifies that it is empty, adds the saved products, and
verifies the exact final quantities. It then replies with the delivery, item summary,
known subtotal and a checkout link. It never submits checkout or changes the slot.

**Ready for checkout means the draft contents were verified.** The order is not confirmed
as saved, and minimum spend, availability and payment/checkout checks remain on Ocado.
Open the correct booked order and complete its save/checkout before the deadline.

### Private weekly list

The file is `data/ocado-weekly.json` on the host (`/opt/data/ocado-weekly.json` inside the
container). It is excluded from Git and Docker builds. It has this structure; replace the
capitalised placeholders with confirmed values before saving:

```json
{
  "mode": "ready_for_checkout",
  "items": [
    {
      "product_id": "EXACT_PRODUCT_ID_FROM_OCADO",
      "name": "EXACT_CONFIRMED_PRODUCT_NAME_AND_PACK_SIZE",
      "quantity": 1
    }
  ]
}
```

Edit it directly on the droplet with:

```sh
nano ~/hermes-home/data/ocado-weekly.json
```

No restart is required: the list is read for every new run. Quantities are pack counts,
not litres or individual units within a multipack. Use the actual Ocado product ID when
changing products; names are labels, not search terms. The list must be nonempty, with
unique IDs and integer quantities from 1 to 50. Invalid JSON or invalid entries stop the
workflow before it opens an order. Keep `mode` as `ready_for_checkout`.

Alternatively, tell the bot to change the weekly list. It should read real IDs from
Ocado/browser data, confirm exact products, pack sizes and quantities with you, then use
`ocado_set_weekly_list`. That tool only updates the private file and backs up its previous
contents. It never updates an order. `ocado_weekly_list` shows the saved list.

### Deploy and enable

After pulling and building the latest code, copy an existing private list from the Mac:

```sh
scp -i ~/.ssh/hermes_digitalocean \
  /Users/fcr/repositories/hermes-home/data/ocado-weekly.json \
  hermes@YOUR_DROPLET_IP:~/hermes-home/data/ocado-weekly.json
```

Then on the droplet:

```sh
make down
make ocado-enable-weekly
make up
```

The enable command preserves and backs up your runtime config and instructions, adds the
weekly tools, explicitly enables the weekly workflow and allows a longer tool
request timeout. It does not enable the separate general order-edit writer. Send
`/reload-mcp` on WhatsApp, then ask to see the weekly list before the first run.

A saved list can also be set through WhatsApp after enabling the tools. If it is absent,
the workflow refuses to alter an order. Keep the shared bot out of concurrent edits on
the same order through the Ocado app/website while a run is in progress.

### Repeat requests and interrupted runs

A private run record under `data/ocado-weekly-runs/` is saved before the first write. It
includes the original order and standalone trolley for diagnosis. Every new direct weekly
request rebuilds the next booked order immediately, even when already prepared. It uses
the current staples and only recipes named in the new request, replacing later additions.
The previous successful run is archived under `data/ocado-weekly-history/`. The correct
order's open edit trolley can be reused. Another order's active edit session is refused.
Existing standalone trolleys are not intentionally merged or cleared by this workflow.

If a request times out, an item is unavailable, the order identity changes or any readback
fails, the workflow stops and the same order is blocked from automatic replay. The edit
trolley may be partly changed or empty. Review it on Ocado manually; do not delete the
run record to force a retry without inspecting the actual order. No substitutions or
checkout occur. The bot reports failure instead of claiming the weekly shop is ready.

The workflow is tested with mocked HTTP, including multi-batch clearing, product additions,
partial failure and duplicate requests. No live order replacement was performed during
development. The first real run still needs verification on the droplet.

## Add recipes to the weekly shop

See [Recipe shopping lists](recipes.md) to store private recipe files and request
**weekly Ocado, with recipe one and recipe two**. Quantities for the same product add
across staples and all selected recipes. Draft or unknown recipes stop the shop before
an order is opened. The staples file and manual checkout boundary stay in effect.

## Redo an already prepared weekly order

Simply send **weekly Ocado** again, optionally **with recipe one and recipe two**.
**Redo weekly Ocado** and **Rebuild weekly Ocado** work too. Every direct request executes
immediately without a preview or confirmation step. It clears and rebuilds the next
booked order, including later additions. With no named recipes it uses only staples;
previous recipe selections are not carried over.

The tool verifies the active order identity and quantities and archives the previous
successful run before writing. It can reuse that order's current edit trolley or open
an edit session for a saved order. It never submits checkout. Open Ocado to review and
save/check out the completed draft before the deadline.

If you explicitly ask for a preview, `ocado_plan_weekly_redo` shows the proposed replacement
without modifying Ocado. Optional preview plans expire after 10 minutes, require
confirmation to apply, and cannot be replayed. Changes to the order, trolley or target
invalidate a preview. These optional tools are not part of the normal weekly execution.

A prior failed or uncertain attempt still needs manual review; neither automatic retries
nor deleting run records are supported. This is separate from a new request following a
successful preparation, which executes immediately.

To install this behavior, pull and build the latest code, then run `make down`,
`make ocado-enable-weekly`, and `make up`. Send `/reload-mcp` on WhatsApp. Re-enabling
weekly appends the updated preference while preserving existing settings and instructions.
No live repeat was performed during development; mocked HTTP covers active-session reuse,
new recipe selections, immediate repeats, wrong-order refusal and partial failure.
