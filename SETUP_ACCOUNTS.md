# Switching on user accounts and price alerts (Phase 3)

> **Do not start before CardTrader has agreed in writing.** Until then this
> feature stays switched off, and that is already the case: `docs/config.js`
> is empty (the account page just says "Accounts are not available yet") and
> the private repository's `SUPABASE_SYNC_ENABLED` variable is not set.

## What you are building, in plain words

- **Supabase** is a hosted database with a built-in sign-in system. It keeps
  the users, what they follow ("watches") and the latest prices. It runs in
  Frankfurt, so the data stays in the EU.
- **Two small programs ("Edge Functions") run on Supabase:**
  `telegram-webhook` answers people who write to your Telegram bot, and
  `send-alerts` checks everyone's watches once a day and sends the messages.
- **Your private repository** (`mtg-price-archive`) uploads the latest prices
  to Supabase every day and then calls `send-alerts`.
- **The website** gets a new page, `account.html`: sign in with Google or
  GitHub, pick prices to follow, connect Telegram, delete the account.
  Prices and charts stay public for everyone — an account only adds alerts
  (this also keeps the site within Wizards' Fan Content Policy).

**Cost:** €0 on the free plans of Supabase, Telegram, Google and GitHub.
The daily sync uses about 2 minutes of GitHub Actions a day (out of 2,000
free minutes a month). Supabase's free plan pauses projects that are unused
for a week; the daily sync keeps it awake. If it is ever paused, Supabase
emails you and you can restore it with one click.

**Time:** about an hour. Keep a note open: you will collect a few values on
the way (marked **write down**). Treat everything marked *secret* like a
password — a password manager is the best place.

In the steps below, replace:
- `<ref>` with your Supabase project's reference (a code like `abcdefghijklmnop`,
  visible in the project's address `https://<ref>.supabase.co`);
- `<BOT_TOKEN>`, `<WEBHOOK_SECRET>` etc. with the values you wrote down.

The website address is `https://deecadance.github.io/MTG-price-tracker/`.

---

## 1. Create the Supabase project (in Frankfurt)

1. Go to <https://supabase.com>, sign up (free) and click **New project**.
2. Name: `mtg-price-tracker`. Database password: click **Generate**, save it
   in your password manager (you will rarely need it).
3. **Region: choose `Central EU (Frankfurt)`.** This matters for privacy —
   the privacy page promises it. It cannot be changed later.
4. Plan: **Free**. If you see security options, keep the defaults
   (Data API **on**, using the **public** schema).
5. Click **Create new project** and wait a minute or two.
6. **Write down** the project URL `https://<ref>.supabase.co`
   (Project Settings → Data API, or the address bar).

## 2. Create the tables (run the SQL)

1. In Supabase: **SQL Editor** (left menu) → **New query**.
2. On your Mac, open `supabase/migrations/0001_init.sql` from this
   repository in a text editor, copy **all** of it, paste it into the editor.
3. Click **Run**. It should say "Success. No rows returned".
4. Check: **Table Editor** shows four tables — `alert_log`, `latest_prices`,
   `profiles`, `watches` — and none of them is marked "RLS disabled" /
   "Unrestricted". **Advisors → Security Advisor** should show no errors
   (red). A few warnings (yellow) are fine — for example that anyone may read
   `latest_prices` (on purpose: prices are public) or that signed-in users
   may run `delete_my_account` (on purpose: it only deletes their own account).

Run this file only once. (Already ran it and something went wrong? The
simplest fix is to delete the project and start again from step 1.)

## 3. Sign-in settings in Supabase

1. **Authentication → Sign In / Providers → Email**: switch it **off** and
   save. (We only offer Google and GitHub; no passwords to store or leak.)
   Make sure **Allow anonymous sign-ins** is off too.
2. **Authentication → URL Configuration**:
   - **Site URL**: `https://deecadance.github.io/MTG-price-tracker/`
   - **Redirect URLs** → Add URL:
     `https://deecadance.github.io/MTG-price-tracker/account.html`
3. Still under Sign In / Providers, open **Google**: you will see a
   **Callback URL** like `https://<ref>.supabase.co/auth/v1/callback`.
   **Write it down** — Google and GitHub both need it.

## 4. "Sign in with Google"

1. Go to <https://console.cloud.google.com>, create a new project
   (`MTG Price Tracker`).
2. Open **Google Auth Platform** (search "OAuth consent" in the top bar) →
   **Get started**: app name `MTG Price Tracker`, your email as support
   email, audience **External**, your email as contact → **Create**.
3. **Branding**: home page `https://deecadance.github.io/MTG-price-tracker/`,
   privacy policy `https://deecadance.github.io/MTG-price-tracker/privacy.html`.
   Under **Authorized domains** add **two** entries: `deecadance.github.io`
   (where the home page and privacy page live — Google refuses those links
   otherwise) and `<ref>.supabase.co`. Save.
4. **Clients → Create client** → type **Web application**:
   - Authorized JavaScript origins: `https://deecadance.github.io`
   - Authorized redirect URIs: the Callback URL from step 3.3
     (`https://<ref>.supabase.co/auth/v1/callback`)
   - **Create**, then copy the **Client ID** and **Client secret** (*secret*).
5. **Audience → Publish app** (otherwise only "test users" you list could
   sign in). Only basic scopes (email, profile) are used, so Google does not
   need to review the app.
6. Back in Supabase → **Authentication → Sign In / Providers → Google**:
   enable it, paste Client ID and Client secret, **Save**.

Note: Google's sign-in screen will say "continue to `<ref>.supabase.co`".
That is normal on the free plan.

## 5. "Sign in with GitHub"

1. On GitHub: your picture (top right) → **Settings** → **Developer
   settings** → **OAuth Apps** → **New OAuth App**:
   - Application name: `MTG Price Tracker`
   - Homepage URL: `https://deecadance.github.io/MTG-price-tracker/`
   - Authorization callback URL: the Callback URL from step 3.3
   - **Register application**.
2. Click **Generate a new client secret**. Copy the **Client ID** and the
   **Client secret** (*secret*; shown only once).
3. Supabase → **Authentication → Sign In / Providers → GitHub**: enable,
   paste both, **Save**.

## 6. Create the Telegram bot

Use a **new** bot for users, separate from any bot you use for your own
alerts (step 9 connects this bot to Supabase, after which other tools can no
longer read its messages).

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Display name: `MTG Price Alerts`. Username: must end in `bot`, e.g.
   `MtgPriceAlertsBot`. **Write down** the username (without @).
3. BotFather replies with a token like `123456789:AA...`: that is
   `<BOT_TOKEN>` (*secret*).
4. Optional, nicer: send BotFather `/setcommands`, pick your bot, and send
   `stop - stop the alerts in this chat`.

## 7. Make two random secrets

In the Terminal app on your Mac, run this **twice**:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

- The first result is `<WEBHOOK_SECRET>` (*secret*): proves to Supabase that
  a message really comes from Telegram.
- The second is `<ALERTS_SECRET>` (*secret*): proves to Supabase that the
  daily sync job is really yours.

## 8. Put the two functions on Supabase

1. Install the Supabase command-line tool (once):
   ```bash
   brew install supabase/tap/supabase
   supabase login          # opens the browser; approve
   ```
2. Go to this repository's folder and prepare it (once; answer **N** to the
   questions about VS Code / IntelliJ):
   ```bash
   cd ~/Desktop/MTG_tracker
   supabase init
   ```
3. Give the functions their settings (one line; replace the values):
   ```bash
   supabase secrets set --project-ref <ref> TELEGRAM_BOT_TOKEN=<BOT_TOKEN> TELEGRAM_WEBHOOK_SECRET=<WEBHOOK_SECRET> ALERTS_WEBHOOK_SECRET=<ALERTS_SECRET> SITE_URL=https://deecadance.github.io/MTG-price-tracker/
   ```
4. Deploy both functions. `--no-verify-jwt` is needed because Telegram and
   the sync job cannot "log in"; the secrets above protect them instead.
   ```bash
   supabase functions deploy telegram-webhook --project-ref <ref> --no-verify-jwt
   supabase functions deploy send-alerts --project-ref <ref> --no-verify-jwt
   ```
   If it complains that **Docker** is not running, you don't need to install
   Docker: add `--use-api` at the end of both lines and run them again.
5. Check: Supabase → **Edge Functions** lists both. Open each one: its
   details should say JWT verification is **off**.

Leave the project's **legacy API keys** enabled: the functions use the
service-role key Supabase gives them automatically.

## 9. Connect the bot to Supabase (the webhook)

Run (one command; replace the three values):

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<ref>.supabase.co/functions/v1/telegram-webhook" \
  -d "secret_token=<WEBHOOK_SECRET>" \
  -d 'allowed_updates=["message"]' \
  -d "drop_pending_updates=true"
```

It should answer `{"ok":true,"result":true,"description":"Webhook was set"}`.
Check any time with `curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"`.

Quick test: send `/start` to your bot in Telegram. It should reply "Hello!
This bot sends the price alerts…".

## 10. Fill in `docs/config.js` (switches the account page on)

1. Supabase → **Project Settings → API Keys**. Copy the **`anon` `public`**
   key (tab "Legacy API keys"). If your project has no legacy keys, copy the
   **Publishable key** (`sb_publishable_…`) instead.
   **Never use the `service_role` / secret key here** — this file is public.
2. Edit `docs/config.js`:
   ```js
   window.APP_CONFIG = {
     supabaseUrl: "https://<ref>.supabase.co",
     supabaseAnonKey: "<the anon / publishable key>",
     telegramBotName: "<bot username without @>",
   };
   ```
3. Fill in the privacy page `docs/privacy.html`: replace every
   `[NAME — to fill in]`, `[EMAIL — to fill in]` and `[DATE — to fill in]`
   (Italian and English parts). This page is a careful template, not legal
   advice — have it checked if you can. Supabase also offers a Data
   Processing Agreement (DPA): request it in the Supabase dashboard
   (Organization settings → Legal documents).
4. Save and publish (the daily bot also pushes, so pull first):
   ```bash
   cd ~/Desktop/MTG_tracker
   git pull
   git add docs/config.js docs/privacy.html
   git commit -m "Switch on accounts"
   git push
   ```
5. Optional: add a link to `account.html` on the main page (`docs/index.html`).

## 11. Give the private repository its secrets

On GitHub, open **deecadance/mtg-price-archive** → **Settings → Secrets and
variables → Actions**.

Tab **Secrets** → **New repository secret**, three times:

| Name | Value |
|---|---|
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API Keys → `service_role` (legacy) or a **Secret key** (`sb_secret_…`). *Secret* — only ever here. |
| `ALERTS_WEBHOOK_SECRET` | `<ALERTS_SECRET>` from step 7 (the same value as in step 8.3) |

## 12. Flip the switch

Same page, tab **Variables** → **New repository variable**:
name `SUPABASE_SYNC_ENABLED`, value `true`.

## 13. Test everything

1. Private repo → **Actions** → **Supabase sync (price alerts for users)** →
   **Run workflow**. Wait for the green tick. In the log of the last step
   you should see `Uploaded … rows to latest_prices` and a line
   `send-alerts says: {…}`.
2. Supabase → **Table Editor → latest_prices**: the products are there.
3. Open <https://deecadance.github.io/MTG-price-tracker/account.html>
   (GitHub Pages needs a minute or two after a push). Sign in with Google;
   sign out; sign in with GitHub.
4. Press **Connect Telegram**, then **Start** in Telegram. The bot says
   "Connected!" and the page shows "connected".
5. A real alert: search for a booster box, choose it, write `1` in
   "Alert me above €", press **Follow**. Run the workflow again (step 13.1).
   Within a minute the bot sends you a message (the price was above €1 on
   both of the last two snapshots). Then remove that test watch.
6. Send `/stop` to the bot: it says you are disconnected; the page shows
   "not connected" after a reload.
7. With a second account (e.g. the one you did not use), press **Delete my
   account**: it disappears from Supabase → **Authentication → Users**.
8. If something fails: Supabase → **Edge Functions** → the function →
   **Logs**; and the GitHub Actions log of the sync.

From now on the sync runs by itself twice a day.

## Switching it off again

- Private repo: set the variable `SUPABASE_SYNC_ENABLED` to `false` (or
  delete it). Prices stop being uploaded and no alerts are sent.
- Public repo: empty the three values in `docs/config.js` and push. The
  account page goes back to "Accounts are not available yet".
- Optional: `curl "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"`,
  and pause or delete the Supabase project (deleting it deletes all user data).

## Where things are

| What | Where |
|---|---|
| Database tables and security rules | `supabase/migrations/0001_init.sql` |
| The two Supabase functions | `supabase/functions/` (see `supabase/README.md`) |
| Account page, privacy page, settings | `docs/account.html`, `docs/js/account.js`, `docs/privacy.html`, `docs/config.js` |
| Daily upload of prices | private repo: `supabase_sync/push_latest.py`, `.github/workflows/supabase-sync.yml` |
| Tests | `python3 -m unittest discover -s supabase/tests -v` (here) and `python3 -m unittest discover -s supabase_sync/tests -v` (private repo) |
