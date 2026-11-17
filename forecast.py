"""Price projections (Phase 4): a simple, honest "where could the price be?" range.

WHAT IT DOES, IN PLAIN WORDS
----------------------------
For each product we take its LAST known price and assume it stays the same
(this is called a "naive" forecast — it sounds dumb, but for prices it is
very hard to beat). Then we ask the past: "when we made this same guess
before, how wrong were we after 7 days, and after 30 days?" We collect all
those past mistakes for similar products (booster boxes together, prerelease
packs together, scene boxes together) and use them to draw a range:

    - p10: only 10% of past outcomes ended up below this price
    - p50: the middle outcome
    - p90: only 10% of past outcomes ended up above this price

So the band between p10 and p90 is where the price landed 8 times out of 10
in the past. It says nothing about bans, reprints or spoilers, which can push
prices far outside it.

We also keep score honestly ("track record"): we replay history day by day,
build the band using ONLY what was known on that day, and check whether the
real price later landed inside. We do the same for a second, slightly
smarter model (a straight line through the last 28 days) to see whether
"assume no change" really is the better guess.

DORMANT BY DEFAULT
------------------
This file only reads a database and writes a JSON file you choose. Nothing is
published unless config.json has "show_projections": true, which makes
build_site.py call this module and add projections to docs/data.json.

Run it by hand (the database is opened READ-ONLY):

    python3 forecast.py --db data/prices.sqlite --metric avg5_all --out /tmp/forecasts.json

The output follows contract C6 in the build spec. Money is integer euro cents,
dates are YYYY-MM-DD.
"""

import argparse
import bisect
import json
import math
import sqlite3
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "prices.sqlite"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"

MODEL_NAME = "naive+empirical-quantiles v1"

# How far ahead we project, in days.
HORIZONS = (7, 30)

# Suppression rules (contract C6): we do not publish a projection for a
# product with too little history, or whose price has gone quiet lately.
MIN_HISTORY_DAYS = 45      # needs at least this many days with a price
RECENT_WINDOW_DAYS = 14    # "lately" means the last 14 days...
MIN_RECENT_POINTS = 5      # ...and it needs a price on at least 5 of them

# The comparison model: a straight line fitted to the last 28 days.
TREND_WINDOW_DAYS = 28
MIN_TREND_POINTS = 14      # no line through fewer than 14 prices

# A band built from very few past mistakes is not trustworthy. Below this
# many, we do not draw one at all.
MIN_POOL_SIZE = 50

# The three percentiles that make up a band.
QUANTILES = (("p10", 0.10), ("p50", 0.50), ("p90", 0.90))

DISCLAIMER = (
    "Statistical estimate from past prices only — not financial advice. "
    "Bans, reprints and spoilers can move prices far outside this range."
)

# Metrics stored in the sealed database (column name = metric + "_cents").
SEALED_METRICS = ("min_all", "avg5_all", "min_zero", "avg5_zero")

# Human-readable names for the groups we pool products into.
GROUP_LABELS = {
    "booster_box": "Booster boxes",
    "prerelease_pack": "Prerelease packs",
    "scene_other": "Scene boxes and other",
    "card_under_2": "Cards under €2",
    "card_2_to_10": "Cards €2–10",
    "card_10_to_50": "Cards €10–50",
    "card_50_plus": "Cards €50 and up",
}


# ---------------------------------------------------------------------------
# Small helpers: names, groups, dates
# ---------------------------------------------------------------------------

def item_key(source, item_id, foil, metric):
    """The id of one forecast line, e.g. "ct_sealed:12345:0:avg5_all"."""
    return f"{source}:{item_id}:{1 if foil else 0}:{metric}"


def sealed_group(category, name):
    """Which group a sealed product is pooled with.

    Scene boxes behave differently from booster boxes even though CardTrader
    files them in the same category, so we spot them by name first.
    """
    if "scene box" in (name or "").lower():
        return "scene_other"
    if category == "Magic Tournament Prerelease Packs":
        return "prerelease_pack"
    if category == "Magic Booster Boxes":
        return "booster_box"
    return "scene_other"


def price_band_group(cents):
    """Group for single cards (later phase): pooled by how expensive they are."""
    if cents < 200:
        return "card_under_2"
    if cents < 1000:
        return "card_2_to_10"
    if cents < 5000:
        return "card_10_to_50"
    return "card_50_plus"


def item_group(item, cents):
    """Default way to pick a group: the item says which group it belongs to."""
    return item.get("group") or "scene_other"


def card_group(item, cents):
    """Group picker for single cards: by price at that moment."""
    return price_band_group(cents)


def to_day(iso_date):
    """'2026-11-17' -> a day number, so we can do arithmetic on dates."""
    return date.fromisoformat(iso_date).toordinal()


def from_day(day_number):
    """Day number -> '2026-11-17'."""
    return date.fromordinal(day_number).isoformat()


def utc_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The maths: percentiles, bands, the straight-line model
# ---------------------------------------------------------------------------

def quantile(sorted_values, q):
    """The value below which a share q (0..1) of the list falls.

    The list must already be sorted. Between two list entries we interpolate
    in a straight line (the same rule spreadsheets use for PERCENTILE).
    Example: quantile([1, 2, 3, 4, 5], 0.10) == 1.4
    """
    if not sorted_values:
        raise ValueError("quantile of an empty list")
    position = q * (len(sorted_values) - 1)
    below = math.floor(position)
    above = min(below + 1, len(sorted_values) - 1)
    share = position - below
    return sorted_values[below] + (sorted_values[above] - sorted_values[below]) * share


def log_ratio(actual, guess):
    """How wrong a guess was, as a log ratio: 0 = spot on, +0.1 ≈ 10% too low.

    Log ratios treat "price doubled" and "price halved" symmetrically and let
    one band fit both a €5 pack and a €500 box.
    """
    return math.log(actual / guess)


def abs_pct_error(actual, guess):
    """|actual - guess| as a percentage of the actual price."""
    return abs(actual - guess) / actual * 100


def band_from_errors(last_cents, sorted_errors):
    """Turn past mistakes into a price band around the last price.

    Naive model: the guess is "same as the last price". If in the past the
    guess was off by a log ratio e, the price ended at last * exp(e). So the
    10th percentile of e gives the low end, the 90th the high end.
    """
    return {
        name: int(round(last_cents * math.exp(quantile(sorted_errors, q))))
        for name, q in QUANTILES
    }


def trend_line(values, days, origin):
    """Fit a straight line to the prices of the 28 days ending on `origin`.

    Returns (slope per day, value of the line on the origin day), or None when
    there are fewer than MIN_TREND_POINTS prices to fit.
    """
    start = bisect.bisect_left(days, origin - TREND_WINDOW_DAYS + 1)
    end = bisect.bisect_right(days, origin)
    window = days[start:end]
    if len(window) < MIN_TREND_POINTS:
        return None
    xs = [d - origin for d in window]
    ys = [values[d] for d in window]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread
    return slope, mean_y - slope * mean_x


def trend_forecast(line, horizon):
    """Extend the straight line `horizon` days ahead (never below 1 cent)."""
    slope, value_at_origin = line
    return max(1.0, value_at_origin + slope * horizon)


# ---------------------------------------------------------------------------
# Preparing one price series
# ---------------------------------------------------------------------------

def prepare_item(item):
    """Turn the raw (date, cents) list into a day -> cents lookup.

    Missing prices (None) and nonsense prices (0 or less) are dropped, so a
    day without a price simply does not exist for the maths.
    """
    values = {}
    for iso_date, cents in item.get("points", []):
        if cents is None or cents <= 0:
            continue
        values[to_day(iso_date)] = int(cents)
    return {**item, "values": values, "days": sorted(values)}


def count_points(days, first_day, last_day):
    """How many days with a price fall between first_day and last_day (included)."""
    return bisect.bisect_right(days, last_day) - bisect.bisect_left(days, first_day)


def passes_history_rules(n_history, n_recent):
    """Contract C6 suppression: enough history overall, and enough lately."""
    return n_history >= MIN_HISTORY_DAYS and n_recent >= MIN_RECENT_POINTS


def recent_points(days, day):
    """Prices in the RECENT_WINDOW_DAYS days ending on `day`."""
    return count_points(days, day - RECENT_WINDOW_DAYS + 1, day)


# ---------------------------------------------------------------------------
# Backtest: replaying history to see how wrong we would have been
# ---------------------------------------------------------------------------

def backtest_pairs(item, horizons, as_of_day, group_fn):
    """Every past moment where we could have made a projection for this item.

    For each day ("origin") on which the item would have passed the
    publication rules, and each horizon h with a known real price h days
    later, record how wrong the naive guess and the trend guess were.
    Returns {horizon: [pair, ...]}.
    """
    values, days = item["values"], item["days"]
    pairs = {h: [] for h in horizons}
    for index, origin in enumerate(days):
        if origin + min(horizons) > as_of_day:
            break
        if not passes_history_rules(index + 1, recent_points(days, origin)):
            continue
        start_cents = values[origin]
        group = group_fn(item, start_cents)
        line = trend_line(values, days, origin)
        for h in horizons:
            target = origin + h
            if target > as_of_day or target not in values:
                continue
            actual = values[target]
            pairs[h].append({
                "origin": origin,
                "target": target,
                "group": group,
                "error": log_ratio(actual, start_cents),
                "naive_ape": abs_pct_error(actual, start_cents),
                "trend_ape": abs_pct_error(actual, trend_forecast(line, h)) if line else None,
            })
    return pairs


def evaluate_pairs(pairs, min_pool=MIN_POOL_SIZE):
    """Honest scorecard for one group and one horizon (rolling origin).

    We walk through history in date order. For a test made on day t, the band
    is built ONLY from mistakes whose real outcome was already known on day t
    (target date <= t) — no peeking at the future. Then we check whether the
    real price landed inside the 10–90% band, and how far off the naive and
    the trend guesses were. Only tests where both models could make a guess
    and the band had at least `min_pool` past mistakes are counted.

    coverage_p10_p90 is a share (0.80 = 80% of tests landed inside);
    the two error figures are percentages (3.5 = 3.5%).
    """
    by_target = sorted(pairs, key=lambda p: p["target"])
    by_origin = sorted(pairs, key=lambda p: p["origin"])
    pool, next_known = [], 0
    inside, naive_errors, trend_errors = 0, [], []
    for test in by_origin:
        while next_known < len(by_target) and by_target[next_known]["target"] <= test["origin"]:
            bisect.insort(pool, by_target[next_known]["error"])
            next_known += 1
        if len(pool) < min_pool or test["trend_ape"] is None:
            continue
        low, high = quantile(pool, 0.10), quantile(pool, 0.90)
        inside += low <= test["error"] <= high
        naive_errors.append(test["naive_ape"])
        trend_errors.append(test["trend_ape"])
    return scorecard(inside, naive_errors, trend_errors)


def scorecard(inside, naive_errors, trend_errors):
    n = len(naive_errors)
    if n == 0:
        return {
            "coverage_p10_p90": None,
            "median_abs_pct_error_naive": None,
            "median_abs_pct_error_trend": None,
            "n_backtests": 0,
        }
    return {
        "coverage_p10_p90": round(inside / n, 3),
        "median_abs_pct_error_naive": round(statistics.median(naive_errors), 2),
        "median_abs_pct_error_trend": round(statistics.median(trend_errors), 2),
        "n_backtests": n,
    }


# ---------------------------------------------------------------------------
# Events (e.g. a Banned & Restricted announcement)
# ---------------------------------------------------------------------------

def parse_events(raw_events):
    """Keep only well-formed events from config.json, sorted by date.

    Each event is {"date": "YYYY-MM-DD", "label": "..."}. Broken entries are
    skipped with a warning instead of crashing the site build.

    "projection_events" should be a list [...]. A single event written
    without the square brackets is accepted too; anything else (a number,
    a piece of text) is ignored with a warning.
    """
    if raw_events is None:
        return []
    if isinstance(raw_events, dict):
        raw_events = [raw_events]  # one event without the [ ] around it
    if not isinstance(raw_events, list):
        print("WARNING: projection_events in config.json should be a list like "
              '[{"date": "YYYY-MM-DD", "label": "..."}] - ignoring it.', file=sys.stderr)
        return []
    events = []
    for raw in raw_events:
        try:
            day = to_day(str(raw["date"]))
        except (KeyError, TypeError, ValueError):
            print(f"WARNING: ignoring projection event without a valid date: {raw!r}",
                  file=sys.stderr)
            continue
        label = raw.get("label")
        events.append({"date": from_day(day), "label": "" if label is None else str(label)})
    return sorted(events, key=lambda e: e["date"])


def events_in_horizons(events, last_day, horizons):
    """For each horizon, the dates of events between the last price and the
    projected date (after last_day, up to and including last_day + h)."""
    return {
        str(h): [e["date"] for e in events if last_day < to_day(e["date"]) <= last_day + h]
        for h in horizons
    }


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def forecast_one(item, as_of_day, horizons, pools, events, group_fn, min_pool):
    """The published projection for one item, or None if it is suppressed."""
    days = item["days"]
    n_history = bisect.bisect_right(days, as_of_day)
    if n_history == 0 or not passes_history_rules(n_history, recent_points(days, as_of_day)):
        return None
    last_day = days[n_history - 1]
    last_cents = item["values"][last_day]
    group = group_fn(item, last_cents)
    bands = {}
    for h in horizons:
        pool = pools.get(group, {}).get(h, [])
        if len(pool) < min_pool:
            return None  # not enough past mistakes to draw an honest band
        bands[str(h)] = band_from_errors(last_cents, pool)
    crossing = events_in_horizons(events, last_day, horizons)
    return {
        "last_date": from_day(last_day),
        "last_cents": last_cents,
        "bands": bands,
        "group": group,
        "n_history_days": n_history,
        "crosses_event": any(crossing.values()),
        "events_in_horizon": crossing,
    }


def build_forecasts(items, as_of=None, horizons=HORIZONS, events=(), group_fn=None,
                    generated=None, min_pool=MIN_POOL_SIZE):
    """Compute projections and the track record for a list of price series.

    items: list of {"key": "<source>:<item_id>:<foil>:<metric>",
                    "points": [("YYYY-MM-DD", cents or None), ...],
                    "group": "booster_box" (optional; see group_fn)}
    as_of: the date we stand on (default: the latest date in the data).
    group_fn(item, cents) -> group name; default reads item["group"].
    Returns a dict shaped like contract C6.
    """
    group_fn = group_fn or item_group
    horizons = tuple(sorted(horizons))
    events = parse_events(events)
    result = {
        "generated": generated or utc_timestamp(),
        "model": MODEL_NAME,
        "horizons": list(horizons),
        "as_of": None,
        "items": {},
        "track_record": {},
        "events": events,
    }
    prepared = [p for p in map(prepare_item, items) if p["days"]]
    if not prepared:
        return result
    as_of_day = to_day(as_of) if as_of else max(p["days"][-1] for p in prepared)
    result["as_of"] = from_day(as_of_day)

    pairs = collect_pairs(prepared, horizons, as_of_day, group_fn)
    result["track_record"] = {
        group: {str(h): evaluate_pairs(by_h.get(h, []), min_pool) for h in horizons}
        for group, by_h in sorted(pairs.items())
    }
    pools = {
        group: {h: sorted(p["error"] for p in by_h.get(h, [])) for h in horizons}
        for group, by_h in pairs.items()
    }
    for item in prepared:
        entry = forecast_one(item, as_of_day, horizons, pools, events, group_fn, min_pool)
        if entry:
            result["items"][item["key"]] = entry
    return result


def collect_pairs(prepared, horizons, as_of_day, group_fn):
    """All backtest pairs of all items, sorted into {group: {horizon: [...]}}."""
    by_group = {}
    for item in prepared:
        for h, item_pairs in backtest_pairs(item, horizons, as_of_day, group_fn).items():
            for pair in item_pairs:
                by_group.setdefault(pair["group"], {}).setdefault(h, []).append(pair)
    return by_group


# ---------------------------------------------------------------------------
# Reading the sealed database (public repo, data/prices.sqlite)
# ---------------------------------------------------------------------------

def open_db_readonly(db_path):
    """Open SQLite so that writing is impossible (any write raises an error)."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"No database at {path}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def load_sealed_series(db, metric="avg5_all"):
    """One series per sealed product for the chosen metric (EUR rows only)."""
    if metric not in SEALED_METRICS:
        raise ValueError(f"Unknown metric {metric!r}; choose one of {SEALED_METRICS}")
    column = metric + "_cents"  # safe: checked against the fixed list above
    rows = db.execute(
        f"SELECT s.blueprint_id, s.date, s.{column}, b.category, b.name "
        "FROM snapshots s LEFT JOIN blueprints b ON b.id = s.blueprint_id "
        "WHERE s.currency = 'EUR' ORDER BY s.blueprint_id, s.date"
    ).fetchall()
    items = {}
    for blueprint_id, day, cents, category, name in rows:
        if blueprint_id not in items:
            items[blueprint_id] = {
                "key": item_key("ct_sealed", blueprint_id, False, metric),
                "item_id": blueprint_id,
                "name": name,
                "group": sealed_group(category, name),
                "points": [],
            }
        items[blueprint_id]["points"].append((day, cents))
    return list(items.values())


def forecast_sealed_db(db, metric="avg5_all", as_of=None, events=(), generated=None):
    """Convenience: load the sealed series from an open DB and forecast them."""
    items = load_sealed_series(db, metric)
    return build_forecasts(items, as_of=as_of, events=events, generated=generated)


# ---------------------------------------------------------------------------
# Helper for build_site.py (only called when show_projections is true)
# ---------------------------------------------------------------------------

def cents_to_eur(cents):
    return round(cents / 100, 2)


def site_block(entry, events, horizons):
    """One product's projection in the shape docs/index.html draws (euros)."""
    last_day = to_day(entry["last_date"])
    bands = []
    for h in horizons:
        band = entry["bands"][str(h)]
        bands.append({
            "h": h,
            "date": from_day(last_day + h),
            "p10": cents_to_eur(band["p10"]),
            "p50": cents_to_eur(band["p50"]),
            "p90": cents_to_eur(band["p90"]),
        })
    longest = str(max(horizons))
    crossing = set(entry["events_in_horizon"].get(longest, []))
    return {
        "group": entry["group"],
        "last_date": entry["last_date"],
        "last_eur": cents_to_eur(entry["last_cents"]),
        "n_history_days": entry["n_history_days"],
        "bands": bands,
        "events": [e for e in events if e["date"] in crossing],
    }


def for_site(db, metric, as_of, raw_events):
    """Everything build_site.py adds to data.json when projections are on.

    Returns {"projection": top-level block, "products": {blueprint_id: block}}.
    """
    items = load_sealed_series(db, metric)
    ids = {item["key"]: item["item_id"] for item in items}
    result = build_forecasts(items, as_of=as_of, events=raw_events)
    horizons = result["horizons"]
    products = {
        ids[key]: site_block(entry, result["events"], horizons)
        for key, entry in result["items"].items()
    }
    projection = {
        "model": result["model"],
        "metric": metric,
        "generated": result["generated"],
        "as_of": result["as_of"],
        "horizons": horizons,
        "track_record": result["track_record"],
        "group_labels": {g: GROUP_LABELS.get(g, g) for g in result["track_record"]},
        "events": result["events"],
        "disclaimer": DISCLAIMER,
    }
    return {"projection": projection, "products": products}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def load_events_from_config(config_path):
    """The projection_events list from config.json ([] if absent or broken)."""
    path = Path(config_path)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as error:
        print(f"WARNING: could not read {path} ({error}); running without events.",
              file=sys.stderr)
        return []
    if not isinstance(config, dict):
        print(f"WARNING: {path} is not a {{...}} object; running without events.",
              file=sys.stderr)
        return []
    return config.get("projection_events", [])


def print_summary(result, n_series):
    """A short human-readable report of what was computed."""
    n_items = len(result["items"])
    print(f"Projections as of {result['as_of']}: {n_items} of {n_series} series "
          f"published, {n_series - n_items} suppressed.")
    print(f"{'group':<16}{'h':>4}{'tests':>8}{'inside band':>13}"
          f"{'naive err':>11}{'trend err':>11}")
    for group, by_h in result["track_record"].items():
        for h, r in by_h.items():
            cov = "-" if r["coverage_p10_p90"] is None else f"{r['coverage_p10_p90']:.1%}"
            naive = "-" if r["median_abs_pct_error_naive"] is None else f"{r['median_abs_pct_error_naive']:.2f}%"
            trend = "-" if r["median_abs_pct_error_trend"] is None else f"{r['median_abs_pct_error_trend']:.2f}%"
            print(f"{group:<16}{h:>4}{r['n_backtests']:>8}{cov:>13}{naive:>11}{trend:>11}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compute price projections (reads the DB read-only).")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to prices.sqlite")
    parser.add_argument("--metric", default="avg5_all", choices=SEALED_METRICS)
    parser.add_argument("--out", required=True, help="where to write forecasts.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="config.json with projection_events (optional)")
    parser.add_argument("--as-of", default=None, help="pretend today is YYYY-MM-DD (default: latest data)")
    args = parser.parse_args(argv)

    out_path = Path(args.out).resolve()
    if out_path == Path(args.db).resolve():
        raise SystemExit("Refusing to overwrite the database with the output file.")
    db = open_db_readonly(args.db)
    try:
        items = load_sealed_series(db, args.metric)
    finally:
        db.close()
    result = build_forecasts(items, as_of=args.as_of, events=load_events_from_config(args.config))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print_summary(result, len(items))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
