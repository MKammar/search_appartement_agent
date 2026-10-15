# Rental listing monitor

A small local app that reads the official listing-alert emails you subscribe to, filters listing links against local criteria, remembers links in SQLite, and emails new matches through SMTP. It does not scrape SeLoger, Bien’ici, or PAP, and it does not bypass their access restrictions.

## Requirements

- Python 3.11 or newer
- An email account that supports IMAP and SMTP
- Official saved-search alert emails from the listing sites delivered to that mailbox

## Setup

From the project folder:

```powershell
python -m rental_monitor init
```

Edit `config.json` with your criteria and set `notification_email` to the recipient address. This is user configuration, not a value fixed in the app. Set the IMAP/SMTP host, login, app-specific password, and verified sender address in `.env`. Do not commit `.env` or `config.json`; both are ignored by Git. In each listing website, create its own rental-only saved search and enable email alerts. Optionally fill `mailbox.allowed_senders` with the sender addresses/domains used by those alert emails so unrelated messages are ignored.

Criteria:

- `include_any`: at least one phrase must appear in the alert's listing text (use area names or alternatives).
- `include_all`: every phrase must appear.
- `exclude_any`: reject a listing if any phrase appears.
- `min_rent`, `max_rent`: monthly euro bounds parsed from the alert text.
- `min_surface`: minimum square metres.
- `max_surface`: maximum square metres.
- `min_rooms`: minimum number of rooms.

Unknown numeric values are included as possible matches and identified for manual checking; a known value outside a configured bound is filtered out. Prefer setting location/price/type on the websites' own saved searches too, since alert email layouts vary and the app can only evaluate details present in those emails.

## Run

```powershell
python -m rental_monitor test-email
python -m rental_monitor once
python -m rental_monitor run
```

The first mailbox check records the current highest IMAP UID as a baseline and deliberately does not notify on old messages. Later checks process only newer messages. `run` checks at the configured interval (minimum 30 seconds); it runs only while the process and PC are running. Stop it with Ctrl+C. For unattended operation, use Windows Task Scheduler to launch `python -m rental_monitor run` at sign-in/startup.

The monitor stores listing URLs in `.data/monitor.sqlite3`, so duplicate links are not re-notified. Alert email delivery is retried if SMTP fails; because SMTP cannot guarantee exactly-once delivery if the process stops immediately after a successful send, a rare duplicate notification is possible.

## Limitations

- This uses official email alerts as its only listing source. It does not poll site search results.
- Alert email formats differ, so the app extracts supported-site links and nearby text. It never follows those listing URLs.
- Criteria checks can only confirm details included in the alert email; unverified numeric criteria are flagged in notifications.
- Credentials are loaded from `.env` or the process environment and are never written into the database.
