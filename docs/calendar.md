# Shared Google Calendar

The bot can read upcoming events and add individual timed or all-day events to one
configured calendar. Your existing WhatsApp users can use the same tools. This uses
the official Google Calendar API and a service account: a separate bot identity that
you share the calendar with. The calendar does not need to be public.

## Create the bot identity

1. Open [Google Cloud Console](https://console.cloud.google.com/) using your personal
   Google account. Create or select a project for the household assistant.
2. Under **APIs & Services → Library**, find **Google Calendar API** and enable it.
3. Open **IAM & Admin → Service Accounts → Create service account**. Give it a name
   such as `home-assistant-agent`. You can leave project roles and user access blank:
   permission to your calendar will come from sharing it directly.
4. Open the new service account, then **Keys → Add key → Create new key → JSON**.
   Download the key privately. Do not paste it into WhatsApp or commit it to Git.
5. Copy the service account's email address, ending in `.iam.gserviceaccount.com`.

No OAuth consent screen, redirect URL or domain-wide delegation is needed for this
setup. A work account's administrator may restrict service-account keys or sharing;
use your personal project/calendar for the household setup.

## Share the existing calendar

On a computer, open [Google Calendar](https://calendar.google.com/):

1. Find the shared calendar under **My calendars → ⋮ → Settings and sharing**.
2. Under its sharing settings, add the service-account email. Give it **Make changes
   and see event details** permission (some interfaces call this **Make changes to
   events**). It does not need permission to manage sharing.
3. Under **Integrate calendar**, copy the **Calendar ID**. This may look like an email
   address ending in `@group.calendar.google.com`. Use the exact ID, not the calendar
   name, browser URL or `primary`.

## Copy the key privately

On your Mac, save the downloaded JSON as
`data/credentials/google-calendar.json` inside your local repository. From that
repository directory, restrict permissions and copy it to the droplet:

```sh
chmod 600 data/credentials/google-calendar.json
ssh hermes@YOUR_DROPLET_IP 'mkdir -p ~/hermes-home/data/credentials'
scp data/credentials/google-calendar.json hermes@YOUR_DROPLET_IP:~/hermes-home/data/credentials/google-calendar.json
ssh hermes@YOUR_DROPLET_IP 'chmod 600 ~/hermes-home/data/credentials/google-calendar.json'
```

Replace `YOUR_DROPLET_IP` with the droplet address and use your usual SSH key/options
if needed. Copy only this file, rather than replacing the droplet's entire `data/`.

## Enable on the droplet

In `~/hermes-home`, run:

```sh
git pull --ff-only
make build
make down
make calendar-configure
make calendar-check
make up
```

The configure command prompts for the Calendar ID, a friendly name, time zone
(default `Europe/London`) and default event duration (60 minutes). It checks access
by reading the calendar; it creates no test event. It then enables the three calendar
tools and appends guidance to the bot's existing instructions. Existing model,
WhatsApp, Yoto and Ocado settings are preserved, with private configuration backups.
If configuration or the check fails, run `make up` to restore the gateway, fix the
reported problem and repeat configuration while the gateway is stopped.

Then send `/reload-mcp` on WhatsApp. First ask it to show your upcoming events to
verify the intended calendar. You can then request events, for example:

> Add a dentist appointment to our calendar tomorrow from 10 to 10:30am.

> Add school inset day to our calendar on 4 January as an all-day event.

A clear request to add an event executes without another confirmation. Missing or
ambiguous details need clarification. If you omit the end time, it uses your configured
default duration and tells you the resulting time range. Relative dates use the
configured time zone and current date. The bot returns the verified event and a Google
Calendar link.

## Files and tools

- `data/calendar.json`: Calendar ID, display name, time zone and default duration.
- `data/credentials/google-calendar.json`: private service-account key.
- `calendar_status`: configuration and current local date/time; not a live access check.
- `calendar_list_events`: read a date range (default next seven days, at most 50 results).
- `calendar_create_event`: add a single event to the configured calendar.

All-day end dates are exclusive in the API; omitting the end creates one day. Time-zone
clock changes are checked, and ambiguous local times require clarification. Identical
event details produce a stable event ID: retrying those same details verifies the
existing event rather than adding a duplicate. After an uncertain result, retain the
same title, dates and other details when retrying. The tool never automatically repeats
an insert, overwrites a changed event or reports success without reading it back.

This version does not support invitations, recurring event creation, editing or
deleting events. Event contents are treated as data, never as bot instructions.

## Troubleshooting

`make calendar-check` is read-only and prints the calendar name, time zone and access
role, without listing private events. For HTTP 401/403/404, check that Calendar API is
enabled in the key's project, the exact calendar was shared with the service-account
email, the Calendar ID is correct, and edit permission was granted. A readable calendar
with insufficient permission will not pass setup. Invalid credentials require the
original service-account JSON file, not an OAuth client download or a copied token.

Official references: [service-account authentication](https://developers.google.com/identity/protocols/oauth2/service-account),
[calendar sharing](https://support.google.com/calendar/answer/37082),
[creating events](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert),
[creating service-account keys](https://docs.cloud.google.com/iam/docs/keys-create-delete).
