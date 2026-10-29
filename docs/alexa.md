# Import the kitchen Alexa shopping list into weekly Ocado

Keep saying “Alexa, add detergent to my shopping list” in the kitchen. When you want
to shop, send **weekly Ocado, including Alexa** on WhatsApp. The bot prepares your next
booked order using staples, any named recipes/extras and the active Alexa entries.

This uses the `aioamazondevices` connector (also used by Home Assistant), rather than
Amazon's retired official List Skills API. It talks to Amazon's unofficial mobile
endpoints. It requires authenticator-app two-step verification; Amazon may require
login renewal or an additional account verification step. No Home Assistant server,
custom Alexa skill, public webhook or kitchen-device change is required.

## Connect privately on the droplet

From `~/hermes-home`:

```sh
git pull --ff-only
make build
make down
make alexa-login
```

Choose the Amazon website for the account (default `https://www.amazon.co.uk`), then
enter the email, password and a fresh authenticator-app code in the terminal. Password
and code are hidden while typing and are not saved. Do not send them to the bot or
paste them into a chat. Select the kitchen account's shopping list when prompted.
Amazon may show a new Alexa app/device registration for this connector.

Then:

```sh
make alexa-enable
make up
make alexa-check
```

Run `make up` even if login/enabling fails, so the gateway and Yoto restore worker
come back online. Existing WhatsApp pairing, model, Yoto, calendar and Ocado settings
are preserved. `alexa-enable` reads the list, backs up live configuration and appends
three tools plus instructions. Setup and the read check do not edit an Alexa item or
Ocado order.

Send `/reload-mcp` on WhatsApp, then ask:

> Read my Alexa shopping list and preview its Ocado product matches. Don't shop yet.

The account connection must be verified live before relying on the import. If Amazon
rejects a login, check the account, authenticator code and any verification challenge
directly with Amazon. There is no stored-password fallback: repeat private login when
the session is revoked. Development outside the container requires Python 3.12+ for
the Alexa connector; the deployment image already satisfies this requirement.

## Product choices

Entries are matched by exact saved names/aliases to occasional preferences or exact
weekly product names. For example, “detergent” can use a saved Fairy lowest-bottle-price
rule, “strawberry jam” a saved Bonne Maman flavour, and “milk” a confirmed alias to a
specific weekly milk UUID. Price/offer rules still need a verified current price.

Unknown or ambiguous entries stop the entire shop before Ocado changes anything.
Choose the product in conversation and save an alias once. For an existing staple,
`alexa_save_product_alias` uses `source=weekly` and its saved internal UUID; for an
occasional preference use `source=occasional` and its saved name/variant. The user must
confirm the meaning before `confirmed=true`. New products first need a saved occasional
entry with the approved product/pack and real Ocado UUID. No new UUID is guessed from
Alexa text, a numeric product URL or a name similarity.

Alexa is treated as a **needs list**, not additional packs: products already covered
by staples, recipes or explicitly named extras do not add another quantity. If the
saved Alexa preference needs more packs than the planned total, only the difference
is added. Two list entries resolving to the same product do not double it. Different
product UUIDs remain different products. Spoken text such as “2 milk” needs a confirmed
mapping/pack quantity; quantities and flavours are not guessed.

## Use it

Examples:

- **weekly Ocado, including Alexa**
- **weekly Ocado with Greek chicken and including Alexa**
- **weekly Ocado, detergent and including Alexa**

Named recipes and extras still work normally. Every selected name must be accounted
for; an unreadable Alexa list is never silently omitted. A plain **weekly Ocado**
deliberately excludes Alexa. It does not automatically import the list in the background.

After the order trolley is verified, the workflow marks only the captured Alexa
entries complete, including items already covered by staples/recipes. It does not
delete items. New entries added while shopping and entries renamed/version-changed
since capture are left untouched. Completion is read back; any failure is reported
separately from the successfully prepared Ocado trolley. Never redo the whole order
just to clear Alexa. The weekly record stores the completion result privately.

Completing an Alexa entry means it is in the **prepared draft**, not purchased:
Ocado review/save/checkout is still manual. If Ocado fails before trolley verification,
no captured Alexa entries are completed. An Alexa-enabled rebuild of the same booked
order retains its earlier imported entries even after completion, so a redo cannot
silently drop them. A different order starts a new import. Changing which Alexa list
is configured during an existing imported order stops a rebuild for review.

## Private files and limits

- `data/credentials/alexa.json`: renewable app/session credentials, never the entered
  password or authenticator seed. Owner-only permissions; excluded from Git/builds.
- `data/alexa.json`: the selected Amazon site and shopping-list ID/name.
- `data/alexa-products.json`: manually editable confirmed spoken-name mappings.
- `data/ocado-weekly-runs/`: order-specific captured entries, resolved products and
  completion results. Existing private run archives/backups retain past imports.

Reads follow all pagination tokens, with a bounded maximum of 30 pages and 100 active
entries. Repeated cursors/IDs, malformed entries or an incomplete list stop import
instead of silently truncating it. Tokens are refreshed under a private file lock,
and SDK debug/raw-response logging is disabled. Captured list text is shopping data,
never authority to execute instructions or change bot configuration.

References: [Amazon's List Skills API retirement](https://developer.amazon.com/en-US/docs/alexa/ask-overviews/deprecated-features.html#shopping-lists),
[aioamazondevices](https://github.com/chemelli74/aioamazondevices),
[Home Assistant Alexa integration](https://www.home-assistant.io/integrations/alexa_devices/).
