# supabase/ — user accounts and price alerts (Phase 3)

**Status: switched off.** Nothing in this folder runs until you follow
[`../SETUP_ACCOUNTS.md`](../SETUP_ACCOUNTS.md) (only after CardTrader's
written OK). Until then `docs/config.js` is empty, so the account page only
says "Accounts are not available yet" and loads nothing.

## What is in here

| File | What it does |
|---|---|
| `migrations/0001_init.sql` | Creates the database: `latest_prices` (public prices), `profiles` (Telegram chat per user), `watches` (what each user follows, max 50), `alert_log` (alerts sent, kept 90 days) — plus the security rules. Run once in Supabase's SQL Editor. |
| `functions/send-alerts/index.ts` | Edge Function called by the daily sync job (with the secret header `x-alerts-secret`). Sends Telegram alerts. |
| `functions/send-alerts/alert_job.js` | The steps of one alert run (read watches and prices → apply rule → send → save → clean up). |
| `functions/send-alerts/alert_rule.js` | **The alert rule (contract C4)** and the message text, on their own, so they can be compared with the Python version in the private repo (`alerts/`). |
| `functions/telegram-webhook/index.ts` | Edge Function Telegram calls for every message to the bot. Checks Telegram's secret header. |
| `functions/telegram-webhook/commands.js` | The bot's answers: `/start <code>` connects, `/stop` disconnects. |
| `functions/_shared/supabase_rest.ts` | Small helpers both functions use to talk to the database. |
| `tests/` | Tests (see below). |

The website side is `docs/account.html`, `docs/js/account.js`,
`docs/config.js` and `docs/privacy.html`.

## How the security works (in plain words)

- The website only ever has the **public "anon" key**. Everything a visitor
  may do is decided **inside the database**: every table has Row Level
  Security switched on, with rules like "a signed-in user may only see and
  change watches whose `user_id` is their own".
- The website never chooses a `user_id`: the database fills it in from the
  login (`DEFAULT auth.uid()`), refuses the column if sent anyway, and the
  rules double-check it.
- Visitors who are not signed in can only **read** `latest_prices`.
- A user cannot set their own Telegram chat (that would let them send
  alerts to someone else); only the bot can, after the user opens the
  one-time link (random, valid 15 minutes) from their account page.
- `alert_log` can only be written by our own code (the service role).
- "Delete my account" removes the sign-in record; profile, watches and alert
  log go with it automatically.
- The **service-role key** (which skips all rules) only exists on Supabase's
  servers and as a secret in the private repository — never in this repo.
- Both functions refuse to do anything until their secrets are set.

## The alert rule (contract C4)

A watch has an optional "above" and "below" threshold. "Above" fires when
the price is at or over the threshold on **two snapshots in a row**, then
waits until the price drops below 98% of the threshold before it can fire
again; "below" is the mirror image (102%). A missing price on either day
changes nothing. Only users who connected Telegram are checked, and only
items present in the newest snapshot. A message that could not be delivered
is retried with the next snapshot. The examples in `tests/c4_cases.json`
pin this down; the owner-alert Python code can be checked against the same file.

## Tests

```bash
python3 -m unittest discover -s supabase/tests -v
```

- `test_migration.py` starts a **throwaway PostgreSQL** on your Mac (Homebrew
  `postgresql@16` is found automatically; or set `PG_BIN`), loads a tiny
  fake of Supabase's sign-in system (`tests/fake_supabase.sql`), runs the
  migration, and checks the security rules by acting as an anonymous
  visitor, as two different users and as the service role. Skipped if
  PostgreSQL isn't installed.
- `test_send_alerts.py` and `test_telegram_bot.py` run the JavaScript with
  `jsc`, the JavaScript engine built into macOS (nothing to install), against
  a fake database and a fake Telegram. Skipped on computers without `jsc`.
- `test_account_page.py` runs the real `docs/js/account.js` with `jsc` on a
  tiny pretend web page: with the empty `docs/config.js` of today it must load
  nothing at all; switched on (with fake settings) it checks the search box.

Nothing here talks to the internet, Supabase or Telegram.

## Cost

Free: Supabase Free plan (500 MB database, generous function limits),
Telegram bots are free. See `SETUP_ACCOUNTS.md`.

## How to check it worked (once switched on)

- Supabase → **Edge Functions → send-alerts → Logs**: one
  `send-alerts finished {…}` line per sync, with counts such as
  `alerts_sent`.
- Supabase → **Table Editor → alert_log**: one row per alert sent.
- `telegram-webhook` logs show nothing unless something failed.
