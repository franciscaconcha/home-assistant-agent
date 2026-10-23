# Read the current Ocado trolley

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
`GetUpcomingOrders` GraphQL query, and a GET of the selected order's detail. Only that
fixed query is allowed; arbitrary GraphQL requests and mutations are rejected. The
nearest order is reported even if no longer editable, rather than silently selecting the
following week's order. No matching order is reported explicitly without creating a trolley.

`guaranteedEditableBy` is the name of the timestamp Ocado supplies. We do not promise
editing after that point or equate it to a later possible cut-off. If item details fail,
the valid delivery summary remains available and items are marked unavailable.

**Order editing is not implemented.** Selecting an order for editing, handling any existing
trolley, applying changes, and saving/checkout require a separate implementation and live
verification against the correct order identity.

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
