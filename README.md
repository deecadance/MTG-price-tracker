# MTG Sealed Price Tracker

Tracks daily prices of MTG sealed products (booster boxes, collector
boosters, prerelease kits) on [CardTrader](https://www.cardtrader.com),
to study how prices move over time — preorder vs. wait, set by set.

Every day it records, per product:

| Metric | Meaning |
|---|---|
| `min_all` | Cheapest listing from any seller |
| `avg5_all` | Average of the 5 cheapest units from any seller |
| `min_zero` | Cheapest listing shippable via CardTrader Zero |
| `avg5_zero` | Average of the 5 cheapest Zero units |

Only English-language listings count (see `config.json`). Data lives in
`data/prices.sqlite`; charts are served from `docs/` as a static site.

## How it works

- `fetch_prices.py` — pulls prices from the CardTrader API and appends
  today's snapshot to the database. New sets and products are picked up
  automatically.
- `build_site.py` — exports the database to `docs/data.json`, which
  `docs/index.html` turns into interactive charts.
- `.github/workflows/daily.yml` — runs both scripts every morning on
  GitHub's servers and commits the result. Your computer can be off.

## One-time setup

### 1. Get your CardTrader API token

Log in to cardtrader.com → your profile → **Settings** → find the
**API** section and copy your access token (the long text string).
Treat it like a password.

### 2. Put the project on GitHub

Create a free account at github.com if you don't have one, then create
a new **private** repository named `mtg-tracker` (no README, empty).
Then, in Terminal, from this folder:

```bash
git add .
git commit -m "Initial version"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/mtg-tracker.git
git push -u origin main
```

(GitHub will ask you to authenticate in the browser the first time.)

### 3. Give GitHub your CardTrader token

On the repository page: **Settings → Secrets and variables → Actions →
New repository secret**. Name: `CARDTRADER_TOKEN`, value: the token
from step 1.

### 4. Turn on the website

**Settings → Pages → Source: Deploy from a branch → Branch: `main`,
folder: `/docs` → Save.** After the first data run, your charts will be
at `https://YOUR_USERNAME.github.io/mtg-tracker/`.

> Note: with a **private** repo, GitHub Pages requires a paid plan. If
> you stay on the free plan, either make the repo public (fine — it's
> just prices, your token stays secret) or view charts locally (below).

### 5. Do a first test run

Repository page → **Actions** tab → "Daily price snapshot" → **Run
workflow**. Watch it go green, then check your Pages URL. From then on
it runs by itself every morning.

## Running locally instead

```bash
CARDTRADER_TOKEN=your_token_here python3 fetch_prices.py
python3 build_site.py
python3 -m http.server -d docs 8000   # then open http://localhost:8000
```

## Tweaking

Everything adjustable is in `config.json`:

- `min_release_date` — how far back to track sets
- `listing_language` — `"en"` for English listings only
- `sealed_category_patterns` — which product categories count as sealed
- `lowest_n` — how many cheapest units go into the average
