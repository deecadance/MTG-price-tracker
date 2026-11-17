"""Tests for forecast.py and the projection switch in build_site.py.

Run from the repository folder:

    python3 -m unittest discover -s tests

Everything here uses made-up price series in temporary folders. Nothing reads
or writes data/prices.sqlite or docs/data.json, and nothing touches the network.
"""

import contextlib
import hashlib
import io
import json
import math
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import build_site  # noqa: E402
import forecast  # noqa: E402

START = date(2026, 7, 1)


# ---------------------------------------------------------------------------
# Helpers that make synthetic price series
# ---------------------------------------------------------------------------

def iso(offset):
    return (START + timedelta(days=offset)).isoformat()


def series(values, skip=()):
    """[(date, cents)] for consecutive days from START; days in `skip` get None."""
    return [(iso(i), None if i in skip else v) for i, v in enumerate(values)]


def random_walk(seed, n_days, start_cents=10000, step=0.01):
    rng = random.Random(seed)
    level, out = math.log(start_cents), []
    for _ in range(n_days):
        out.append(int(round(math.exp(level))))
        level += rng.gauss(0, step)
    return out


def item(key, points, group="booster_box"):
    return {"key": key, "points": points, "group": group}


def quiet(function, *args, **kwargs):
    """Run something without its progress messages cluttering test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args, **kwargs)


SCHEMA = """
CREATE TABLE expansions (id INTEGER PRIMARY KEY, code TEXT, name TEXT, released_at TEXT);
CREATE TABLE blueprints (id INTEGER PRIMARY KEY, expansion_id INTEGER, category TEXT, name TEXT,
                         first_seen TEXT, last_seen TEXT);
CREATE TABLE snapshots (date TEXT, blueprint_id INTEGER, currency TEXT, min_all_cents INTEGER,
                        avg5_all_cents INTEGER, min_zero_cents INTEGER, avg5_zero_cents INTEGER,
                        n_listings INTEGER, captured_at TEXT, PRIMARY KEY (date, blueprint_id));
CREATE TABLE runs (date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
                   n_products INTEGER, n_errors INTEGER, n_warnings INTEGER, message TEXT);
"""


def make_sealed_db(path, n_boxes=12, n_days=90):
    """A small fake copy of the public sealed database.

    Two sets: twelve booster boxes with a long random-walk history, plus a
    scene box (alone in its group) and a brand-new product with 20 days.
    """
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO expansions VALUES (1, 'aaa', 'Set A', '2025-01-01')")
    db.execute("INSERT INTO expansions VALUES (2, 'bbb', 'Set B', NULL)")
    products = []
    for n in range(n_boxes):
        products.append((100 + n, 1, "Magic Booster Boxes", f"Box {n:02d} Play Booster Box",
                         random_walk(n, n_days)))
    products.append((200, 2, "Magic Booster Boxes", 'Set B | "Hero" Scene Box',
                     random_walk(99, n_days, 5000)))
    products.append((201, 2, "Magic Tournament Prerelease Packs", "Set B Prerelease Pack",
                     [None] * (n_days - 20) + random_walk(7, 20, 3000)))
    for bp_id, exp_id, category, name, values in products:
        db.execute("INSERT INTO blueprints VALUES (?, ?, ?, ?, ?, ?)",
                   (bp_id, exp_id, category, name, iso(0), iso(n_days - 1)))
        for day, cents in enumerate(values):
            if cents is None:
                continue
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, 'EUR', ?, ?, ?, ?, 10, ?)",
                (iso(day), bp_id, cents - 50, cents, cents - 40, cents + 10, iso(day) + "T05:00:00Z"),
            )
    db.execute("INSERT INTO runs VALUES (?, '', '', 'complete', 14, 0, 0, '')", (iso(n_days - 1),))
    db.commit()
    db.close()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------

class QuantileMathTest(unittest.TestCase):
    def test_quantile_interpolates_like_a_spreadsheet(self):
        values = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(forecast.quantile(values, 0.10), 1.4)
        self.assertAlmostEqual(forecast.quantile(values, 0.50), 3.0)
        self.assertAlmostEqual(forecast.quantile(values, 0.90), 4.6)
        self.assertEqual(forecast.quantile(values, 0.0), 1)
        self.assertEqual(forecast.quantile(values, 1.0), 5)

    def test_quantile_of_one_value_is_that_value(self):
        self.assertEqual(forecast.quantile([7.5], 0.1), 7.5)

    def test_quantile_of_nothing_is_an_error(self):
        with self.assertRaises(ValueError):
            forecast.quantile([], 0.5)

    def test_band_from_errors(self):
        errors = [-0.2, -0.1, 0.0, 0.1, 0.2]  # log ratios, already sorted
        band = forecast.band_from_errors(1000, errors)
        self.assertEqual(band["p10"], round(1000 * math.exp(-0.16)))
        self.assertEqual(band["p50"], 1000)
        self.assertEqual(band["p90"], round(1000 * math.exp(0.16)))
        self.assertTrue(band["p10"] <= band["p50"] <= band["p90"])

    def test_log_ratio_and_percentage_error(self):
        self.assertAlmostEqual(forecast.log_ratio(110, 100), math.log(1.1))
        self.assertAlmostEqual(forecast.abs_pct_error(80, 100), 25.0)


class NaiveForecastTest(unittest.TestCase):
    def test_flat_price_gives_flat_band(self):
        result = forecast.build_forecasts(
            [item("ct_sealed:1:0:avg5_all", series([5000] * 100))], min_pool=10)
        entry = result["items"]["ct_sealed:1:0:avg5_all"]
        self.assertEqual(entry["last_date"], iso(99))
        self.assertEqual(entry["last_cents"], 5000)
        self.assertEqual(entry["n_history_days"], 100)
        for h in ("7", "30"):
            self.assertEqual(entry["bands"][h], {"p10": 5000, "p50": 5000, "p90": 5000})

    def test_band_is_centred_on_the_last_price_not_the_average(self):
        values = [5000] * 99 + [6000]  # jumps on the very last day
        result = forecast.build_forecasts([item("k", series(values))], min_pool=10)
        self.assertEqual(result["items"]["k"]["last_cents"], 6000)
        self.assertEqual(result["items"]["k"]["bands"]["7"]["p50"], 6000)

    def test_missing_days_are_skipped_not_treated_as_zero(self):
        values = [5000] * 100
        result = forecast.build_forecasts(
            [item("k", series(values, skip={10, 50, 99}))], min_pool=10)
        entry = result["items"]["k"]
        self.assertEqual(entry["last_date"], iso(98))  # day 99 had no price
        self.assertEqual(entry["n_history_days"], 97)
        self.assertEqual(entry["bands"]["30"]["p10"], 5000)

    def test_later_prices_are_never_used(self):
        # Standing on day 100, nothing after day 100 may change the answer:
        # tripling every later price must give exactly the same result.
        items = [item(f"k{n}", series(random_walk(n, 150))) for n in range(10)]
        changed = [dict(it, points=it["points"][:101] + [(d, c * 3) for d, c in it["points"][101:]])
                   for it in items]
        before = forecast.build_forecasts(items, as_of=iso(100), generated="x")
        after = forecast.build_forecasts(changed, as_of=iso(100), generated="x")
        self.assertEqual(before["items"], after["items"])
        self.assertEqual(before["track_record"], after["track_record"])
        self.assertEqual(before["items"]["k0"]["last_date"], iso(100))

    def test_stale_product_starts_its_band_at_its_own_last_price(self):
        # The anchor has a price today (day 119); the other product's last
        # price is 3 days old but it still has enough recent prices.
        anchor = item("anchor", series([5000] * 120))
        stale = item("stale", series([3000] * 117))
        result = forecast.build_forecasts([anchor, stale], min_pool=10)
        entry = result["items"]["stale"]
        self.assertEqual(result["as_of"], iso(119))
        self.assertEqual(entry["last_date"], iso(116))
        block = forecast.site_block(entry, result["events"], result["horizons"])
        self.assertEqual([b["date"] for b in block["bands"]], [iso(123), iso(146)])

    def test_output_has_the_c6_shape(self):
        result = forecast.build_forecasts(
            [item("k", series([5000] * 100))], min_pool=10, generated="2026-11-17T00:00:00Z")
        for key in ("generated", "model", "horizons", "items", "track_record", "events"):
            self.assertIn(key, result)
        self.assertEqual(result["model"], "naive+empirical-quantiles v1")
        self.assertEqual(result["horizons"], [7, 30])
        entry = result["items"]["k"]
        for key in ("last_date", "last_cents", "bands", "group", "n_history_days"):
            self.assertIn(key, entry)
        self.assertEqual(set(result["track_record"]["booster_box"]), {"7", "30"})


class TrendModelTest(unittest.TestCase):
    def prepared(self, values):
        return forecast.prepare_item({"key": "k", "points": series(values)})

    def test_straight_line_is_extended(self):
        p = self.prepared([1000 + 10 * d for d in range(40)])
        line = forecast.trend_line(p["values"], p["days"], p["days"][-1])
        self.assertAlmostEqual(forecast.trend_forecast(line, 7), 1390 + 70)

    def test_needs_enough_points(self):
        p = self.prepared([1000] * 10)
        self.assertIsNone(forecast.trend_line(p["values"], p["days"], p["days"][-1]))

    def test_never_goes_below_one_cent(self):
        p = self.prepared([5000 - 150 * d for d in range(30)])
        line = forecast.trend_line(p["values"], p["days"], p["days"][-1])
        self.assertEqual(forecast.trend_forecast(line, 30), 1.0)


# ---------------------------------------------------------------------------
# Suppression rules (contract C6)
# ---------------------------------------------------------------------------

class SuppressionTest(unittest.TestCase):
    def run_with(self, *items, min_pool=10):
        # A long, healthy "anchor" product keeps the group's error pool full.
        anchor = item("anchor", series([5000] * 120))
        return forecast.build_forecasts([anchor, *items], min_pool=min_pool)["items"]

    def test_44_days_is_too_short_45_is_enough(self):
        short = item("short", [(iso(120 - 44 + i), 3000) for i in range(44)])
        enough = item("enough", [(iso(120 - 45 + i), 3000) for i in range(45)])
        items = self.run_with(short, enough)
        self.assertNotIn("short", items)
        self.assertIn("enough", items)

    def test_quiet_lately_is_suppressed(self):
        # 120 days of history, but only 4 (then 5) prices in the last 14 days.
        recent_four = set(range(106, 120)) - {110, 112, 115, 119}
        recent_five = set(range(106, 120)) - {110, 112, 115, 117, 119}
        items = self.run_with(item("four", series([3000] * 120, skip=recent_four)),
                              item("five", series([3000] * 120, skip=recent_five)))
        self.assertNotIn("four", items)
        self.assertIn("five", items)

    def test_no_prices_at_all_is_suppressed(self):
        items = self.run_with(item("empty", series([None] * 120)))
        self.assertNotIn("empty", items)

    def test_too_few_past_mistakes_means_no_band(self):
        items = self.run_with(min_pool=10_000)
        self.assertEqual(items, {})

    def test_group_without_enough_past_mistakes_is_suppressed(self):
        # 60 days is enough history for the item itself, but its group has
        # no 30-day test yet (needs 45 days + 30 days), so no band.
        lonely = item("lonely", [(iso(60 + i), 3000) for i in range(60)], group="scene_other")
        items = self.run_with(lonely)
        self.assertIn("anchor", items)
        self.assertNotIn("lonely", items)


# ---------------------------------------------------------------------------
# Honest scorecard (coverage)
# ---------------------------------------------------------------------------

def pair(origin, target, error, naive=1.0, trend=2.0):
    return {"origin": origin, "target": target, "group": "g", "error": error,
            "naive_ape": naive, "trend_ape": trend}


class CoverageTest(unittest.TestCase):
    def test_counts_how_often_the_real_price_landed_inside(self):
        # 50 known past mistakes spread evenly between -0.1 and +0.1 ...
        pool = [pair(k, k + 7, -0.1 + 0.2 * k / 49) for k in range(50)]
        # ... then 10 tests on day 200: 7 land inside the band, 3 far outside.
        tests = [pair(200, 207, 0.0, naive=1.0, trend=3.0) for _ in range(7)]
        tests += [pair(200, 207, 0.5, naive=40.0, trend=30.0) for _ in range(3)]
        record = forecast.evaluate_pairs(pool + tests, min_pool=50)
        self.assertEqual(record["n_backtests"], 10)
        self.assertEqual(record["coverage_p10_p90"], 0.7)
        self.assertEqual(record["median_abs_pct_error_naive"], 1.0)
        self.assertEqual(record["median_abs_pct_error_trend"], 3.0)

    def test_never_peeks_at_the_future(self):
        # Known by day 15: five mistakes of exactly 0 -> a zero-width band.
        known = [pair(-10 + k, k, 0.0) for k in range(5)]
        # Only known AFTER day 15: wide mistakes (not tests themselves).
        later = [pair(20 + k, 100 + k, (-1) ** k * 1.0, trend=None) for k in range(50)]
        test = pair(15, 22, 0.5)  # on day 15 the band is [0, 0] -> outside
        record = forecast.evaluate_pairs(known + later + [test], min_pool=5)
        self.assertEqual(record["n_backtests"], 1)
        self.assertEqual(record["coverage_p10_p90"], 0.0)

    def test_tests_without_a_trend_guess_are_left_out(self):
        pool = [pair(k, k + 7, 0.0) for k in range(10)]
        tests = [pair(100, 107, 0.0, trend=None), pair(100, 107, 0.0)]
        record = forecast.evaluate_pairs(pool + tests, min_pool=5)
        self.assertEqual(record["n_backtests"], 1)

    def test_no_tests_gives_empty_scorecard(self):
        self.assertEqual(forecast.evaluate_pairs([]), {
            "coverage_p10_p90": None, "median_abs_pct_error_naive": None,
            "median_abs_pct_error_trend": None, "n_backtests": 0})

    def test_random_walk_coverage_is_close_to_80_percent(self):
        # For a pure random walk the band should be roughly honest.
        items = [item(f"k{n}", series(random_walk(n, 200))) for n in range(20)]
        result = forecast.build_forecasts(items)
        record = result["track_record"]["booster_box"]["7"]
        self.assertGreater(record["n_backtests"], 1000)
        self.assertGreater(record["coverage_p10_p90"], 0.70)
        self.assertLess(record["coverage_p10_p90"], 0.90)
        # For a random walk "no change" beats a fitted line.
        self.assertLess(record["median_abs_pct_error_naive"], record["median_abs_pct_error_trend"])


# ---------------------------------------------------------------------------
# Events (Banned & Restricted announcement etc.)
# ---------------------------------------------------------------------------

class EventFlagTest(unittest.TestCase):
    EVENTS = [
        {"date": "2026-11-30", "label": "Banned & Restricted announcement"},
        {"date": "2026-11-24", "label": "Exactly 7 days later"},
        {"date": "2026-11-17", "label": "Same day as last price"},
        {"date": "2026-12-18", "label": "One day too late"},
    ]

    def test_window_edges(self):
        events = forecast.parse_events(self.EVENTS)
        crossing = forecast.events_in_horizons(events, forecast.to_day("2026-11-17"), (7, 30))
        self.assertEqual(crossing["7"], ["2026-11-24"])
        self.assertEqual(crossing["30"], ["2026-11-24", "2026-11-30"])

    def test_broken_events_are_ignored(self):
        with contextlib.redirect_stderr(io.StringIO()):
            events = forecast.parse_events(
                [{"date": "not a date"}, {"label": "no date"}, "junk", {"date": "2026-11-30"}])
        self.assertEqual(events, [{"date": "2026-11-30", "label": ""}])

    def test_events_that_are_not_a_list_do_not_crash(self):
        # Before the review fix, a number here crashed the whole projection
        # step, so a typo in config.json silently removed all projections.
        with contextlib.redirect_stderr(io.StringIO()):
            for junk in (5, "2026-11-30", None, 3.5, True):
                with self.subTest(junk=junk):
                    self.assertEqual(forecast.parse_events(junk), [])

    def test_single_event_without_square_brackets_is_accepted(self):
        events = forecast.parse_events({"date": "2026-11-30", "label": "B&R"})
        self.assertEqual(events, [{"date": "2026-11-30", "label": "B&R"}])

    def test_empty_label_is_blank_not_the_word_none(self):
        events = forecast.parse_events([{"date": "2026-11-30", "label": None}])
        self.assertEqual(events, [{"date": "2026-11-30", "label": ""}])

    def test_items_get_flagged(self):
        last = date(2026, 11, 17)
        points = [((last - timedelta(days=99 - i)).isoformat(), 4000) for i in range(100)]
        result = forecast.build_forecasts(
            [item("k", points)], min_pool=10,
            events=[{"date": "2026-11-30", "label": "Banned & Restricted announcement"}])
        entry = result["items"]["k"]
        self.assertTrue(entry["crosses_event"])
        self.assertEqual(entry["events_in_horizon"], {"7": [], "30": ["2026-11-30"]})
        self.assertEqual(result["events"], [{"date": "2026-11-30",
                                             "label": "Banned & Restricted announcement"}])

    def test_no_events_no_flag(self):
        result = forecast.build_forecasts([item("k", series([4000] * 100))], min_pool=10)
        self.assertFalse(result["items"]["k"]["crosses_event"])


# ---------------------------------------------------------------------------
# Groups and names
# ---------------------------------------------------------------------------

class GroupTest(unittest.TestCase):
    def test_sealed_groups(self):
        g = forecast.sealed_group
        self.assertEqual(g("Magic Booster Boxes", "Bloomburrow: Play Booster Box"), "booster_box")
        self.assertEqual(g("Magic Tournament Prerelease Packs", "X Prerelease Pack"), "prerelease_pack")
        self.assertEqual(g("Magic Booster Boxes", 'Marvel | "Heroes United" Scene Box'), "scene_other")
        self.assertEqual(g("Magic Booster Boxes", "Star Trek Scene Box Set"), "scene_other")
        self.assertEqual(g("Something new", "Mystery thing"), "scene_other")
        self.assertEqual(g(None, None), "scene_other")

    def test_card_price_bands(self):
        self.assertEqual(forecast.price_band_group(199), "card_under_2")
        self.assertEqual(forecast.price_band_group(200), "card_2_to_10")
        self.assertEqual(forecast.price_band_group(4999), "card_10_to_50")
        self.assertEqual(forecast.price_band_group(5000), "card_50_plus")

    def test_item_key(self):
        self.assertEqual(forecast.item_key("ct_sealed", 123, False, "avg5_all"),
                         "ct_sealed:123:0:avg5_all")
        self.assertEqual(forecast.item_key("ct_card", 9, True, "ref_nm"), "ct_card:9:1:ref_nm")


# ---------------------------------------------------------------------------
# Reading the database and the command line
# ---------------------------------------------------------------------------

class DatabaseAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "prices.sqlite"
        make_sealed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_is_opened_read_only(self):
        db = forecast.open_db_readonly(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM snapshots")
        db.close()

    def test_unknown_metric_is_refused(self):
        db = forecast.open_db_readonly(self.db_path)
        with self.assertRaises(ValueError):
            forecast.load_sealed_series(db, "avg5_all_cents; DROP TABLE snapshots")
        db.close()

    def test_cli_writes_c6_file_and_leaves_db_untouched(self):
        before = file_hash(self.db_path)
        config = self.dir / "config.json"
        config.write_text(json.dumps({"projection_events": [
            {"date": iso(95), "label": "Banned & Restricted announcement"}]}))
        out = self.dir / "out" / "forecasts.json"
        quiet(forecast.main, ["--db", str(self.db_path), "--metric", "avg5_all",
                              "--out", str(out), "--config", str(config)])
        self.assertEqual(file_hash(self.db_path), before)
        result = json.loads(out.read_text())
        self.assertIn("ct_sealed:100:0:avg5_all", result["items"])
        self.assertNotIn("ct_sealed:201:0:avg5_all", result["items"])  # only 20 days
        self.assertNotIn("ct_sealed:200:0:avg5_all", result["items"])  # group too small
        self.assertTrue(result["items"]["ct_sealed:100:0:avg5_all"]["crosses_event"])
        self.assertEqual(result["items"]["ct_sealed:100:0:avg5_all"]["group"], "booster_box")

    def test_cli_refuses_to_overwrite_the_database(self):
        with self.assertRaises(SystemExit):
            quiet(forecast.main, ["--db", str(self.db_path), "--out", str(self.db_path)])

    def test_cli_survives_a_broken_config(self):
        out = self.dir / "forecasts.json"
        for text in ("[1, 2]", "{not json", '{"projection_events": 5}'):
            with self.subTest(config=text):
                config = self.dir / "config.json"
                config.write_text(text)
                with contextlib.redirect_stderr(io.StringIO()):
                    quiet(forecast.main, ["--db", str(self.db_path), "--out", str(out),
                                          "--config", str(config)])
                result = json.loads(out.read_text())
                self.assertEqual(result["events"], [])
                self.assertIn("ct_sealed:100:0:avg5_all", result["items"])

    def test_prices_in_another_currency_are_ignored(self):
        # If CardTrader ever answered in USD, those rows must not be mixed
        # into euro bands. Add USD rows for product 100 after its last day.
        db = sqlite3.connect(self.db_path)
        for day in range(90, 100):
            db.execute("INSERT INTO snapshots VALUES (?, 100, 'USD', 1, 999999, 1, 1, 10, '')",
                       (iso(day),))
        db.commit()
        db.close()
        db = forecast.open_db_readonly(self.db_path)
        series_100 = next(s for s in forecast.load_sealed_series(db) if s["item_id"] == 100)
        db.close()
        self.assertEqual(len(series_100["points"]), 90)
        self.assertNotIn(999999, [cents for _, cents in series_100["points"]])


# ---------------------------------------------------------------------------
# build_site.py: unchanged when switched off, projections when switched on
# ---------------------------------------------------------------------------

TOP_LEVEL_KEYS_TODAY = ["generated", "currency", "latest_date", "last_run", "sets"]
SET_KEYS_TODAY = ["code", "name", "released_at", "products"]
PRODUCT_KEYS_TODAY = ["id", "name", "category", "dates", "min_all", "avg5_all", "min_zero",
                      "avg5_zero"]


class BuildSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "prices.sqlite"
        make_sealed_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, config):
        """Run build_site with a temp DB, temp output and the given config."""
        config_path = self.dir / "config.json"
        if config is None:
            config_path = self.dir / "missing.json"
        else:
            config_path.write_text(json.dumps(config))
        out = self.dir / "docs" / "data.json"
        with contextlib.redirect_stderr(io.StringIO()):
            quiet(build_site.main, self.db_path, out, config_path)
        return json.loads(out.read_text())

    def without_timestamp(self, payload):
        payload = dict(payload)
        payload.pop("generated")
        return payload

    def test_switched_off_output_has_exactly_todays_structure(self):
        payload = self.build({"show_projections": False, "projection_events": [
            {"date": iso(95), "label": "Banned & Restricted announcement"}]})
        self.assertEqual(list(payload), TOP_LEVEL_KEYS_TODAY)
        for s in payload["sets"]:
            self.assertEqual(list(s), SET_KEYS_TODAY)
            for product in s["products"]:
                self.assertEqual(list(product), PRODUCT_KEYS_TODAY)

    def test_anything_but_true_counts_as_off(self):
        reference = self.without_timestamp(self.build(None))
        for config in ({}, {"show_projections": False}, {"show_projections": "true"},
                       {"show_projections": 1}):
            with self.subTest(config=config):
                self.assertEqual(self.without_timestamp(self.build(config)), reference)

    def test_switched_off_does_not_even_load_forecast(self):
        with mock.patch.object(forecast, "for_site") as for_site:
            self.build({"show_projections": False})
        for_site.assert_not_called()

    def test_switched_on_adds_projection_block_and_bands(self):
        payload = self.build({"show_projections": True, "projection_events": [
            {"date": iso(95), "label": "Banned & Restricted announcement"}]})
        self.assertEqual(list(payload), TOP_LEVEL_KEYS_TODAY + ["projection"])
        projection = payload["projection"]
        self.assertEqual(projection["model"], "naive+empirical-quantiles v1")
        self.assertEqual(projection["disclaimer"], (
            "Statistical estimate from past prices only — not financial advice. "
            "Bans, reprints and spoilers can move prices far outside this range."))
        self.assertIn("booster_box", projection["track_record"])
        products = {p["id"]: p for s in payload["sets"] for p in s["products"]}
        box = products[100]["forecast"]
        self.assertEqual([b["h"] for b in box["bands"]], [7, 30])
        self.assertEqual(box["bands"][0]["date"], iso(96))
        self.assertTrue(box["bands"][1]["p10"] <= box["bands"][1]["p50"] <= box["bands"][1]["p90"])
        self.assertEqual(box["events"][0]["label"], "Banned & Restricted announcement")
        self.assertNotIn("forecast", products[201])  # too young

    def test_switched_on_with_broken_events_still_shows_projections(self):
        payload = self.build({"show_projections": True, "projection_events": 5})
        self.assertIn("projection", payload)
        self.assertEqual(payload["projection"]["events"], [])
        products = {p["id"]: p for s in payload["sets"] for p in s["products"]}
        self.assertIn("forecast", products[100])

    def test_switched_on_but_broken_forecast_still_builds_the_site(self):
        with mock.patch.object(forecast, "for_site", side_effect=RuntimeError("boom")):
            payload = self.build({"show_projections": True})
        self.assertEqual(list(payload), TOP_LEVEL_KEYS_TODAY)


if __name__ == "__main__":
    unittest.main()
