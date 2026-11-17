"""Daily CardTrader price snapshot for MTG sealed products.

Fetches every sealed product (booster boxes, collector boosters, prerelease
kits) for recent MTG sets from the CardTrader API, computes four price
metrics per product, and appends them to data/prices.sqlite:

  - lowest price across all listings
  - average of the 5 cheapest units across all listings
  - lowest price among CardTrader Zero listings
  - average of the 5 cheapest Zero units

Listings are filtered to English-language products (configurable in
config.json). Run it with the CARDTRADER_TOKEN environment variable set:

    CARDTRADER_TOKEN=xxx python3 fetch_prices.py

Reliability rules (so one hiccup never throws away a whole day):

  - A problem with one set or one product is logged and skipped; everything
    else is still saved.
  - If CardTrader refuses us outright (HTTP 401/403 — its bot protection
    sometimes blocks GitHub's servers), or too many requests fail in a row,
    the run stops early and keeps what it already saved.
  - Every run is recorded in the `runs` table as complete / partial / failed.
    The script exits with an error code unless the run was complete, so
    GitHub emails you about it.
  - If today's snapshot is already complete, a second run does nothing
    (set FORCE=1 to fetch again anyway). Otherwise it fetches again and
    overwrites today's rows, so it is always safe to re-run.
"""

import gzip
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.cardtrader.com/api/v2"
SCRYFALL_SETS_URL = "https://api.scryfall.com/sets"
PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "prices.sqlite"
CONFIG_PATH = PROJECT_DIR / "config.json"
USER_AGENT = "mtg-sealed-tracker/1.1"

# CardTrader allows 10 requests/second on the marketplace endpoint;
# we stay well below that.
REQUEST_PAUSE_SECONDS = 0.15

# This many failed requests in a row means something is wrong on a larger
# scale (CardTrader down, token or currency changed...): stop instead of
# hammering the API.
MAX_CONSECUTIVE_ERRORS = 8

# A run that saves fewer products than this share of the last complete run
# is marked "partial", even if no request failed — it usually means a bug
# or a catalog change worth a look.
MIN_SHARE_OF_PREVIOUS_RUN = 0.8


class BlockedError(Exception):
    """CardTrader refused the request (HTTP 401/403): no point continuing."""


class StopRun(Exception):
    """Something is wrong on a scale where carrying on makes no sense."""


class TooManyErrors(StopRun):
    """Too many requests of the same kind failed in a row."""


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def utc_now():
    return datetime.now(timezone.utc)


def http_get_json(url, headers=None, retries=4):
    """GET a URL and parse the JSON response, retrying on rate limits
    and transient server errors with increasing pauses."""
    # Scryfall rejects requests without an Accept header (HTTP 400).
    all_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                host = urllib.parse.urlsplit(url).netloc
                raise BlockedError(f"HTTP {error.code} from {host}") from error
            if error.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 2 ** (attempt + 1)
                print(f"    HTTP {error.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                wait = 2 ** (attempt + 1)
                print(f"    network error, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise


def cardtrader_get(path, token, params=None):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}"}
    return http_get_json(url, headers=headers)


def as_list(payload):
    """Some CardTrader endpoints wrap lists as {"array": [...]}."""
    if isinstance(payload, dict) and "array" in payload:
        return payload["array"]
    if isinstance(payload, list):
        return payload
    return []


def find_magic_game_id(token):
    games = as_list(cardtrader_get("/games", token))
    for game in games:
        name = (game.get("name") or game.get("display_name") or "").lower()
        if "magic" in name:
            return game["id"]
    raise RuntimeError("Could not find the Magic game in CardTrader's game list")


def find_sealed_categories(token, game_id, config):
    """Return {category_id: category_name} for sealed categories we track,
    plus the id of the plain 'Booster' category (used to pick up single
    collector boosters by name)."""
    categories = as_list(cardtrader_get("/categories", token, {"game_id": game_id}))
    patterns = [p.lower() for p in config["sealed_category_patterns"]]
    sealed = {}
    booster_category_id = None
    for category in categories:
        name = (category.get("name") or "").lower()
        if any(p in name for p in patterns):
            sealed[category["id"]] = category["name"]
        if name == "booster":
            booster_category_id = category["id"]
    if not sealed:
        raise RuntimeError(
            "No sealed categories matched config.json patterns; "
            f"CardTrader returned: {[c.get('name') for c in categories]}"
        )
    return sealed, booster_category_id


def scryfall_release_dates():
    """Map lowercase set code -> release date (ISO string) using Scryfall,
    which is free and requires no authentication."""
    payload = http_get_json(SCRYFALL_SETS_URL)
    dates = {}
    for s in payload.get("data", []):
        code = (s.get("code") or "").lower()
        released = s.get("released_at")
        if code and released:
            dates[code] = released
    return dates


def known_release_dates(db):
    """Release dates saved on earlier runs — the fallback when Scryfall
    is unreachable."""
    return {
        code: released
        for code, released in db.execute(
            "SELECT code, released_at FROM expansions WHERE released_at IS NOT NULL"
        )
    }


def recent_expansions(token, game_id, config, release_dates):
    """Magic expansions on CardTrader to check for sealed products.

    Included: expansions Scryfall says were released on/after
    min_release_date, plus expansions Scryfall doesn't know (brand-new sets
    still in preorder, CardTrader-only groupings) as long as CardTrader
    created them after the oldest set we track. CardTrader's expansion ids
    grow over time, so a higher id means a more recently added expansion."""
    expansions = [
        e for e in as_list(cardtrader_get("/expansions", token))
        if e.get("game_id") == game_id
    ]
    min_date = config["min_release_date"]

    def code_of(expansion):
        return (expansion.get("code") or "").lower()

    recent_ids = [
        e["id"] for e in expansions
        if (release_dates.get(code_of(e)) or "") >= min_date
    ]
    oldest_recent_id = min(recent_ids) if recent_ids else None

    selected = []
    n_undated = 0
    for expansion in expansions:
        released = release_dates.get(code_of(expansion))
        if released is not None:
            if released < min_date:
                continue
        elif oldest_recent_id is None or expansion["id"] < oldest_recent_id:
            continue
        else:
            n_undated += 1
        selected.append(
            {
                "id": expansion["id"],
                "code": code_of(expansion),
                "name": expansion.get("name") or code_of(expansion),
                "released_at": released,
            }
        )
    return selected, n_undated


def sealed_blueprints(token, expansion_id, sealed_categories, booster_category_id, config):
    """Blueprints (products) in one expansion that belong to a sealed
    category, plus single collector boosters from the Booster category."""
    blueprints = as_list(
        cardtrader_get("/blueprints/export", token, {"expansion_id": expansion_id})
    )
    extra_patterns = [
        p.lower() for p in config["extra_blueprint_name_patterns_in_booster_category"]
    ]
    result = []
    for bp in blueprints:
        category_id = bp.get("category_id")
        name = bp.get("name") or ""
        if category_id in sealed_categories:
            result.append({"id": bp["id"], "name": name, "category_id": category_id,
                           "category": sealed_categories[category_id]})
        elif (
            booster_category_id is not None
            and category_id == booster_category_id
            and any(p in name.lower() for p in extra_patterns)
        ):
            result.append({"id": bp["id"], "name": name, "category_id": category_id,
                           "category": "Booster"})
    return result


def price_cents(product):
    """Extract the price in cents from a marketplace product, tolerating
    the couple of shapes the API uses."""
    price = product.get("price")
    if isinstance(price, dict) and "cents" in price:
        return price["cents"], price.get("currency", "EUR")
    if "price_cents" in product:
        return product["price_cents"], product.get("price_currency", "EUR")
    return None, None


def listing_language(product):
    properties = product.get("properties_hash") or {}
    for key, value in properties.items():
        if "language" in key.lower() and isinstance(value, str):
            return value.lower()
    return None


def is_zero_listing(product):
    user = product.get("user") or {}
    return bool(user.get("can_sell_via_hub"))


def fetch_listings(token, blueprint_id):
    payload = cardtrader_get("/marketplace/products", token, {"blueprint_id": blueprint_id})
    # Response shape: {"<blueprint_id>": [products]} or a plain list.
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return value
        return []
    return as_list(payload)


def unit_prices(products, language, zero_only):
    """Sorted per-unit prices (cents). A listing with quantity 3 contributes
    up to 3 units, so a single deep-stocked cheap seller can fill the
    5-lowest average — that's intentional, those units are really buyable."""
    units = []
    for product in products:
        if zero_only and not is_zero_listing(product):
            continue
        lang = listing_language(product)
        if lang is not None and not lang.startswith(language):
            continue
        cents, _currency = price_cents(product)
        if cents is None or cents <= 0:
            continue
        quantity = max(1, int(product.get("quantity") or 1))
        units.extend([cents] * min(quantity, 5))
    units.sort()
    return units


def metrics(units, lowest_n):
    if not units:
        return None, None
    cheapest = units[:lowest_n]
    return units[0], round(sum(cheapest) / len(cheapest))


def add_column_if_missing(db, table, column, declaration):
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    if column in columns:
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return True


def open_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS expansions (
            id INTEGER PRIMARY KEY,
            code TEXT,
            name TEXT,
            released_at TEXT
        );
        CREATE TABLE IF NOT EXISTS blueprints (
            id INTEGER PRIMARY KEY,
            expansion_id INTEGER,
            category TEXT,
            name TEXT,
            first_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            date TEXT,
            blueprint_id INTEGER,
            currency TEXT,
            min_all_cents INTEGER,
            avg5_all_cents INTEGER,
            min_zero_cents INTEGER,
            avg5_zero_cents INTEGER,
            n_listings INTEGER,
            PRIMARY KEY (date, blueprint_id)
        );
        -- One row per day: how the fetch went.
        CREATE TABLE IF NOT EXISTS runs (
            date TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,          -- complete / partial / failed
            n_products INTEGER,
            n_errors INTEGER,
            n_warnings INTEGER,
            message TEXT
        );
        """
    )
    # Columns added after the first version. Older rows keep NULL there.
    # captured_at: exact UTC time of the snapshot (GitHub starts scheduled
    # runs anywhere from on time to ~12 hours late).
    add_column_if_missing(db, "snapshots", "captured_at", "TEXT")
    # last_seen: last day the product was still listed in CardTrader's
    # catalog, so products that silently disappear can be spotted.
    if add_column_if_missing(db, "blueprints", "last_seen", "TEXT"):
        db.execute(
            "UPDATE blueprints SET last_seen ="
            " (SELECT max(date) FROM snapshots WHERE blueprint_id = blueprints.id)"
        )
    db.commit()
    return db


def today_already_complete(db, today):
    row = db.execute("SELECT status FROM runs WHERE date = ?", (today,)).fetchone()
    return row is not None and row[0] == "complete"


def previous_complete_product_count(db, today):
    row = db.execute(
        "SELECT n_products FROM runs WHERE status = 'complete' AND date < ?"
        " ORDER BY date DESC LIMIT 1",
        (today,),
    ).fetchone()
    return row[0] if row else None


class RunLog:
    """Counts errors and warnings during a run and decides when to give up."""

    def __init__(self):
        self.errors = 0
        self.warnings = 0
        # Failures in a row, counted separately for each kind of request
        # ("catalog", "listings"), so successes of one kind can't hide a
        # streak of failures of the other.
        self.streaks = {}
        self.notes = []

    def warning(self, text):
        self.warnings += 1
        print(f"  warning: {text}")
        self.notes.append(f"warning: {text}")

    def error(self, kind, text):
        self.errors += 1
        self.streaks[kind] = self.streaks.get(kind, 0) + 1
        print(f"  ERROR: {text}")
        self.notes.append(f"error: {text}")
        if self.streaks[kind] >= MAX_CONSECUTIVE_ERRORS:
            raise TooManyErrors(f"{self.streaks[kind]} {kind} requests failed in a row")

    def success(self, kind):
        self.streaks[kind] = 0


def fetch_all(token, config, db, today, captured_at, log):
    """Fetch and save today's snapshot. Returns the number of products saved.
    Commits after every set, so an interruption keeps everything before it."""
    expected_currency = config.get("expected_currency", "EUR")

    print("Finding Magic game and sealed categories...")
    game_id = find_magic_game_id(token)
    sealed_categories, booster_category_id = find_sealed_categories(token, game_id, config)
    print(f"  sealed categories: {list(sealed_categories.values())}")

    print("Selecting recent expansions...")
    try:
        release_dates = scryfall_release_dates()
    except Exception as error:  # Scryfall down: fall back to dates we already know
        log.warning(f"Scryfall unreachable ({error}); using release dates saved earlier")
        release_dates = known_release_dates(db)
    expansions, n_undated = recent_expansions(token, game_id, config, release_dates)
    print(f"  {len(expansions)} expansions since {config['min_release_date']}"
          f" ({n_undated} without a Scryfall release date yet)")

    total_products = 0
    for expansion in expansions:
        time.sleep(REQUEST_PAUSE_SECONDS)
        try:
            blueprints = sealed_blueprints(
                token, expansion["id"], sealed_categories, booster_category_id, config
            )
        except (BlockedError, StopRun):
            raise
        except urllib.error.HTTPError as error:
            if error.code == 404:
                # Seen with brand-new expansions: CardTrader lists them a
                # day or so before their catalog can be exported.
                log.warning(f"{expansion['name']}: catalog not available (HTTP 404), skipped today")
                continue
            log.error("catalog", f"{expansion['name']}: catalog request failed ({error})")
            continue
        except Exception as error:
            log.error("catalog", f"{expansion['name']}: catalog request failed ({error})")
            continue
        log.success("catalog")
        if not blueprints:
            continue
        print(f"{expansion['name']}: {len(blueprints)} sealed products")

        db.execute(
            "INSERT OR REPLACE INTO expansions (id, code, name, released_at) VALUES (?,?,?,?)",
            (expansion["id"], expansion["code"], expansion["name"], expansion["released_at"]),
        )
        for bp in blueprints:
            db.execute(
                "INSERT INTO blueprints (id, expansion_id, category, name, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen",
                (bp["id"], expansion["id"], bp["category"], bp["name"], today, today),
            )
            time.sleep(REQUEST_PAUSE_SECONDS)
            try:
                listings = fetch_listings(token, bp["id"])
            except (BlockedError, StopRun):
                raise
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    log.warning(f"{bp['name']} ({bp['id']}): no longer on CardTrader (HTTP 404)")
                    continue
                log.error("listings", f"{bp['name']} ({bp['id']}): listings request failed ({error})")
                continue
            except Exception as error:
                log.error("listings", f"{bp['name']} ({bp['id']}): listings request failed ({error})")
                continue

            currency = expected_currency
            for product in listings:
                _, found_currency = price_cents(product)
                if found_currency:
                    currency = found_currency
                    break
            if currency != expected_currency:
                # CardTrader quotes prices in the token owner's account
                # currency, so this affects every product. Mixing currencies
                # would silently corrupt the history: stop without saving.
                raise StopRun(f"Prices came back in {currency}, expected {expected_currency}."
                              " Check the currency setting of your CardTrader account.")
            log.success("listings")

            all_units = unit_prices(listings, config["listing_language"], zero_only=False)
            zero_units = unit_prices(listings, config["listing_language"], zero_only=True)
            min_all, avg5_all = metrics(all_units, config["lowest_n"])
            min_zero, avg5_zero = metrics(zero_units, config["lowest_n"])

            db.execute(
                "INSERT OR REPLACE INTO snapshots"
                " (date, blueprint_id, currency, min_all_cents, avg5_all_cents,"
                "  min_zero_cents, avg5_zero_cents, n_listings, captured_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (today, bp["id"], currency, min_all, avg5_all, min_zero, avg5_zero,
                 len(listings), captured_at),
            )
            total_products += 1

        db.commit()
    return total_products


def main():
    token = os.environ.get("CARDTRADER_TOKEN")
    if not token:
        sys.exit(
            "CARDTRADER_TOKEN is not set.\n"
            "Get your token from your CardTrader profile settings page, then run:\n"
            "  CARDTRADER_TOKEN=xxx python3 fetch_prices.py"
        )

    config = load_config()
    started = utc_now()
    # Dates are UTC days — the same calendar GitHub's servers use.
    today = started.date().isoformat()
    captured_at = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    db = open_db()

    if today_already_complete(db, today) and os.environ.get("FORCE") != "1":
        print(f"Snapshot for {today} is already complete — nothing to do.")
        db.close()
        return 0

    log = RunLog()
    total_products = 0
    stopped_early = None
    try:
        total_products = fetch_all(token, config, db, today, captured_at, log)
    except BlockedError as error:
        stopped_early = (f"CardTrader refused the request ({error}). Its bot protection"
                         " sometimes blocks GitHub's servers; a later run will retry.")
    except StopRun as error:
        stopped_early = f"Stopped early: {error}"
    except Exception:
        stopped_early = "Unexpected problem:\n" + traceback.format_exc()
    finally:
        db.commit()

    if total_products == 0:
        total_products = db.execute(
            "SELECT count(*) FROM snapshots WHERE date = ?", (today,)
        ).fetchone()[0]

    previous = previous_complete_product_count(db, today)
    if stopped_early:
        status = "partial" if total_products else "failed"
        message = stopped_early
    elif log.errors:
        status = "partial"
        message = f"{log.errors} request(s) failed; see notes."
    elif previous and total_products < MIN_SHARE_OF_PREVIOUS_RUN * previous:
        status = "partial"
        message = (f"Only {total_products} products saved, against {previous} on the last"
                   " complete run — check whether CardTrader's catalog changed.")
    else:
        status = "complete"
        message = ""

    notes = "\n".join(log.notes[-50:])
    db.execute(
        "INSERT OR REPLACE INTO runs (date, started_at, finished_at, status, n_products,"
        " n_errors, n_warnings, message) VALUES (?,?,?,?,?,?,?,?)",
        (today, captured_at, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), status,
         total_products, log.errors, log.warnings,
         (message + ("\n" + notes if notes else "")).strip()),
    )
    db.commit()
    db.close()

    print(f"\nRun {status}: snapshot {today} saved for {total_products} products"
          f" ({log.errors} errors, {log.warnings} warnings,"
          f" {utc_now().strftime('%H:%M UTC')})")
    if message:
        print(message)
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
