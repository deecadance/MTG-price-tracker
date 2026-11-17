# Price projections (Phase 4)

A shaded "likely range" on the chart that shows where a product's price could be
7 and 30 days from now, together with an honest score of how often that range
was right in the past.

**Status: OFF (dormant).** Nothing is published until you switch it on in
`config.json` (see [How to switch it on](#how-to-switch-it-on)). While it is off,
`docs/data.json` and the website are exactly as before.

---

## What it does, in plain words

1. **The guess.** For each product we take its last price and assume it stays
   the same. That's called a *naive* forecast. It sounds too simple, but for
   prices it's very hard to beat (the numbers below show this).
2. **The range.** We replay the past: on every day we could have made that
   guess, how wrong was it 7 days later, and 30 days later? We pool these past
   mistakes for similar products:
   - **Booster boxes**: CardTrader category "Magic Booster Boxes"
   - **Prerelease packs**: category "Magic Tournament Prerelease Packs"
   - **Scene boxes and other**: anything with "Scene Box" in its name, plus anything else

   From all those past mistakes we take three points:
   - **p10**: only 1 past outcome in 10 ended lower than this
   - **p50**: the middle outcome
   - **p90**: only 1 past outcome in 10 ended higher than this

   The band from p10 to p90 is the range the price ended inside **8 times out of
   10** in the past. Mistakes are measured as ratios (for example "+4%"), not
   euros, so the same band shape fits a €25 prerelease pack and a €400 collector
   box.
3. **The honest score ("track record").** We replay history one day at a time.
   On each day we build the band using **only what was known on that day**,
   then check whether the real price later landed inside it. That share is the
   *coverage*. If the method is honest, coverage should be close to 80%.
4. **A sanity check against a smarter-looking model.** We also draw a straight
   line through the last 28 days of prices and extend it ("the trend"). We
   compare how far off each guess was (the median error, in %). If the trend
   line ever starts winning, the naive model should be reconsidered.
5. **Events.** `config.json` lists known dates that can shake prices, such as a
   Banned & Restricted announcement. If one falls inside a product's 7- or
   30-day window, the product is flagged and the site shows a "Heads up" line.
   Events do **not** change the band. They are a warning only.

### When a product gets no projection

To avoid publishing nonsense, a product is skipped when:

- it has prices on **fewer than 45 days** (new releases are too jumpy and too new), or
- it has **fewer than 5 prices in the last 14 days** (sold out or delisted), or
- its group has **fewer than 50 past mistakes** to learn from (no honest band yet).

A day without a price, such as a failed daily run, is skipped. It never counts as €0.

---

## Results on the current history

Computed on 2026-11-17 from a read-only copy of `data/prices.sqlite`. The copy
covers 132 daily snapshots from 2026-07-04 to 2026-11-17 (5 days are missing
because those runs failed) and 144 products. The metric is `avg5_all`, the
average of the 5 cheapest units from all sellers.

**114 of 144 products get a projection** (72 booster boxes, 34 prerelease packs
and 8 scene boxes). Thirty are skipped:

- 26 have fewer than 45 days of prices. These are new releases such as Nauctis,
  Marvel: Darkhold Destiny and the holiday scene boxes, the Hobbit scene boxes
  that stopped being listed in August, and a few products with many empty days.
- 2 had no price in the last 14 days.
- 2 never had a price.

### How honest are the bands?

The target is 80% inside the band. "Below" and "above" show where the misses went.

| Group | Window | Tests | Inside band | Below band | Above band |
|---|---|---:|---:|---:|---:|
| Booster boxes | 7 days | 4,643 | **80.5%** | 11.1% | 8.4% |
| Booster boxes | 30 days | 1,884 | **77.4%** | 14.6% | 8.0% |
| Prerelease packs | 7 days | 2,313 | **78.9%** | 10.2% | 10.9% |
| Prerelease packs | 30 days | 924 | **76.5%** | 23.5% | 0.0% |
| Scene boxes and other | 7 days | 468 | **83.8%** | 8.3% | 7.9% |
| Scene boxes and other | 30 days | 162 | **60.5%** | 14.2% | 25.3% |
| *All groups together* | 7 days | 7,503 | 80.2% | | |
| *All groups together* | 30 days | 3,060 | 78.1% | | |

What this means:

- **The 7-day bands are about right**: 79–84% inside, with misses split evenly
  above and below.
- **The 30-day bands are a bit too narrow.** For booster boxes and prerelease
  packs, read them as "about 3 times in 4", not 8 in 10. Prerelease packs
  missed only on the downside: from mid-September to mid-October their prices
  fell faster than in the weeks before.
- **Scene boxes are unreliable at 30 days** (61% inside). That result comes
  from only 8 products and 162 tests.

### "No change" against the 28-day trend line

These are median errors. Lower is better.

| Group | Window | "No change" (naive) | 28-day trend line | Trend was closer in |
|---|---|---:|---:|---:|
| Booster boxes | 7 days | **0.43%** | 2.47% | 20% of tests |
| Booster boxes | 30 days | **4.05%** | 7.45% | 26% of tests |
| Prerelease packs | 7 days | **0.00%** | 2.61% | 14% of tests |
| Prerelease packs | 30 days | **2.80%** | 14.56% | 12% of tests |
| Scene boxes and other | 7 days | **0.52%** | 1.24% | 24% of tests |
| Scene boxes and other | 30 days | **1.42%** | 4.27% | 33% of tests |

"No change" wins clearly in every group. Following the recent trend made the
typical error about 2 to 6 times larger, so the naive model stays.

### How wide the bands are today

These are the median values over the published products, relative to the last price:

| Group | 7 days | 30 days |
|---|---|---|
| Booster boxes | −3.2% to +4.5% | −9.6% to +14.0% |
| Prerelease packs | −3.2% to +4.5% | −17.8% to +25.0% |
| Scene boxes and other | −3.2% to +1.4% | −5.3% to +0.7% |

### Please read these numbers with care

- **The history is short.** It covers about 4.5 months, all in one season
  (summer and autumn 2026). The 7-day tests were made between 2026-08-25 and
  2026-11-10. The 30-day tests were made only between 2026-09-17 and 2026-10-18.
- **The tests overlap heavily.** A test made on Monday and one made on Tuesday
  for the same product share almost the same future. The real amount of
  independent evidence is much smaller than the "Tests" column suggests.
- **The numbers will change** as history grows. They are recomputed on every
  build, and the site always shows the latest track record next to the band.

---

## How to switch it on

> **Before you switch it on:** projections are built from CardTrader prices and
> published on your public site. Under the project's rule, wait for CardTrader's
> written OK on market-data use first.

No GitHub secrets or variables are needed. The switch lives in `config.json`.

1. Open `config.json` and change `"show_projections": false` to
   `"show_projections": true`. Only the exact value `true` works. `"true"` in
   quotes, or `1`, still counts as off.
2. Optional: edit `"projection_events"`. Each event needs a date (`YYYY-MM-DD`)
   and a label:
   ```json
   "projection_events": [
     {"date": "2026-11-30", "label": "Banned & Restricted announcement"}
   ]
   ```
   A wrongly written event (for example a date like `30/11/2026`) is skipped
   with a `WARNING` line in the Actions log. The projections still appear.
3. Commit and push the change.
4. Wait for the next daily run that saves new prices (the morning run). That run
   calls `build_site.py`, which computes the projections and adds them to
   `docs/data.json`. The workflow file does not change. A manual run after a
   complete morning run saves nothing new, so it won't publish projections
   either.

**To switch it off:** set it back to `false` and push. The next daily run
removes the projections from the site.

## How to check it worked

- **Actions log:** open the latest "Daily price snapshot" run, then the step
  "Rebuild the chart site data". It should say `Added projections for N
  products`.
  - If it says `WARNING: projections skipped because of an error…`, the site
    was still built normally, just without projections. Copy that line and ask
    for help.
- **Website:** a new menu "Projection: …" appears next to the toggles. The
  chosen product gets a shaded cone after its last date, as long as "Average of
  5 cheapest" and "All sellers" are on. A box under the chart shows the
  disclaimer and the track record ("Over the past N tests, the real price
  landed inside the band X% of the time").
- **On your computer, without touching the site:**
  ```bash
  python3 forecast.py --db data/prices.sqlite --metric avg5_all --out /tmp/forecasts.json
  ```
  This opens the database read-only, prints the track-record table and writes
  the projections to the file you name.
- **Tests:** `python3 -m unittest discover -s tests` should end with `OK`.

## What it costs

Nothing. It runs inside the existing free daily GitHub Action and adds about
0.1 seconds. When switched on, `docs/data.json` grows by about 38 KB (about 6%).

---

## Technical notes

**Files**

- `forecast.py`: the method, plus a command line tool. It works on any price
  series given as `(date, value_cents)` per item, so it can be reused for
  single cards later: `card_group` pools cards by price band (under €2, €2–10,
  €10–50, €50 and up).
- `build_site.py`: when the switch is on, it calls `forecast.for_site()` and
  adds a `forecast` block to each product that has a projection, plus a
  top-level `projection` block to `docs/data.json`. The projection block holds
  the model name, track record, date, events and disclaimer. When the switch is
  off, the output is the same as before. This was checked on a copy of the real
  database: before and after the change, the output was byte-for-byte the same
  apart from the "generated" time.
- `docs/index.html`: it draws the band and the explanation only when
  `data.json` has a `projection` block.
- `tests/test_forecast.py`: the tests, which use synthetic data only.

**Output of `forecast.py --out` (contract C6).** Money is in integer euro cents:

```text
{"generated": "2026-11-17T21:28:07Z", "model": "naive+empirical-quantiles v1",
 "horizons": [7, 30], "as_of": "2026-11-17",
 "items": {"ct_sealed:<blueprint_id>:0:avg5_all": {
     "last_date", "last_cents",
     "bands": {"7": {"p10", "p50", "p90"}, "30": {...}},
     "group", "n_history_days",
     "crosses_event": true/false, "events_in_horizon": {"7": [dates], "30": [dates]}}},
 "track_record": {"<group>": {"7": {"coverage_p10_p90": 0.805,          (share, 0–1)
                                    "median_abs_pct_error_naive": 0.43, (percent)
                                    "median_abs_pct_error_trend": 2.47, (percent)
                                    "n_backtests": 4643}, "30": {...}}},
 "events": [{"date", "label"}]}
```

**Method details**

- **Error measure:** the log ratio `ln(real price / last price)`. Band edges
  are `last price × e^(percentile of past errors)`. Percentiles interpolate
  between neighbouring values, like a spreadsheet's PERCENTILE.
- **Which past days count:** a backtest uses only days on which the product
  would itself have passed the publication rules (at least 45 days of prices,
  at least 5 in the last 14 days). The band is then judged on the same kind of
  situation it is published for.
- **Coverage tests:** a test on day *t* only uses mistakes whose outcome was
  known by day *t*. Tests count only when the band had at least 50 past
  mistakes and the trend line could be fitted (at least 14 prices in its 28-day
  window). Both models are scored on exactly the same tests.
- **The published band** uses all past mistakes in the group, up to the latest
  date. Bands start from the product's last price date: the 7-day band is for
  the last date + 7 days.

**Known limitations and ideas for a v2**

- Bands are pooled by group, so a stable product and a volatile product in the
  same group get the same relative width.
- All past mistakes count equally. If the market changes character, the band
  adapts slowly. A rolling window, for example the last 6 months, could help
  once there is more history.
- The 30-day bands are currently a little too narrow. A v2 could widen them
  using the measured coverage.
- Scene boxes, 30 days: the band is currently *narrower on the upside* than
  the 7-day band (about +0.7% against +1.4%), because the few scene boxes
  mostly drifted down during the short history. Together with the 61% score,
  this band is the weakest part of the site. It is still shown, with its score
  next to it.
- The model knows nothing about news: bans, reprints, spoilers, restocks and
  holidays. That is why the disclaimer is always shown.
