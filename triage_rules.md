# My triage rules

Edit these in plain English. Claude reads this file every time you run the
"triage_inbox" prompt, so changes apply immediately — no restart needed.

## Categories

- **Needs reply** — a real person is asking me something or waiting on me.
- **FYI** — worth reading, but no action needed (updates, shared docs, announcements).
- **Receipts** — orders, invoices, bills, booking confirmations, shipping updates.
- **Newsletters** — anything from a mailing list, marketing, promos.
- **Low priority** — notifications, automated alerts, social media, anything else.

## Specific rules

- Anything from my manager is always **Needs reply**.
- Emails from my bank go to **FYI**, unless they mention a payment problem — then **Needs reply**.
- Calendar invites go to **FYI**.
- If `is_mailing_list` is true and it isn't a receipt, it's a **Newsletter**.
- When unsure between two categories, pick the more important one.
