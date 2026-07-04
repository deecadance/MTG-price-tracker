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

Running it twice on the same day simply overwrites that day's snapshot,
so it is safe to re-run.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

API_BASE = "https://api.cardtrader.com/api/v2"
SCRYFALL_SETS_URL = "https://api.scryfall.com/sets"
PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "prices.sqlite"
CONFIG_PATH = PROJECT_DIR / "config.json"

# CardTrader allows 10 requests/second on the marketplace endpoint;
# we stay well below that.
REQUEST_PAUSE_SECONDS = 0.15


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def http_get_json(url, headers=None, retries=4):
    """GET a URL and parse the JSON response, retrying on rate limits
    and transient server errors with increasing pauses."""
    # Scryfall rejects requests without an Accept header (HTTP 400).
    all_headers = {"Accept": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
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
    payload = http_get_json(
        SCRYFALL_SETS_URL, headers={"User-Agent": "mtg-sealed-tracker/1.0"}
    )
    dates = {}
    for s in payload.get("data", []):
        code = (s.get("code") or "").lower()
        released = s.get("released_at")
        if code and released:
            dates[code] = released
    return dates


def recent_expansions(token, game_id, config):
    """All Magic expansions on CardTrader released on/after the configured
    date. Expansions whose code Scryfall doesn't know keep a null date and
    are included only if they look new (they may be upcoming preorders)."""
    expansions = as_list(cardtrader_get("/expansions", token))
    release_dates = scryfall_release_dates()
    min_date = config["min_release_date"]
    selected = []
    for expansion in expansions:
        if expansion.get("game_id") != game_id:
            continue
        code = (expansion.get("code") or "").lower()
        released = release_dates.get(code)
        if released is None:
            # Unknown to Scryfall: skip, but log so we can whitelist later.
            print(f"  note: no release date for '{expansion.get('name')}' ({code}), skipped")
            continue
        if released >= min_date:
            selected.append(
                {
                    "id": expansion["id"],
                    "code": code,
                    "name": expansion.get("name") or code,
                    "released_at": released,
                }
            )
    return selected


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
        """
    )
    return db


def main():
    token = os.environ.get("CARDTRADER_TOKEN")
    if not token:
        sys.exit(
            "CARDTRADER_TOKEN is not set.\n"
            "Get your token from your CardTrader profile settings page, then run:\n"
            "  CARDTRADER_TOKEN=xxx python3 fetch_prices.py"
        )

    config = load_config()
    today = date.today().isoformat()
    db = open_db()

    print("Finding Magic game and sealed categories...")
    game_id = find_magic_game_id(token)
    sealed_categories, booster_category_id = find_sealed_categories(token, game_id, config)
    print(f"  sealed categories: {list(sealed_categories.values())}")

    print("Selecting recent expansions...")
    expansions = recent_expansions(token, game_id, config)
    print(f"  {len(expansions)} expansions since {config['min_release_date']}")

    total_products = 0
    for expansion in expansions:
        db.execute(
            "INSERT OR REPLACE INTO expansions (id, code, name, released_at) VALUES (?,?,?,?)",
            (expansion["id"], expansion["code"], expansion["name"], expansion["released_at"]),
        )
        time.sleep(REQUEST_PAUSE_SECONDS)
        blueprints = sealed_blueprints(
            token, expansion["id"], sealed_categories, booster_category_id, config
        )
        if not blueprints:
            continue
        print(f"{expansion['name']}: {len(blueprints)} sealed products")

        for bp in blueprints:
            db.execute(
                "INSERT OR IGNORE INTO blueprints (id, expansion_id, category, name, first_seen)"
                " VALUES (?,?,?,?,?)",
                (bp["id"], expansion["id"], bp["category"], bp["name"], today),
            )
            time.sleep(REQUEST_PAUSE_SECONDS)
            listings = fetch_listings(token, bp["id"])

            currency = "EUR"
            for product in listings:
                _, found_currency = price_cents(product)
                if found_currency:
                    currency = found_currency
                    break

            all_units = unit_prices(listings, config["listing_language"], zero_only=False)
            zero_units = unit_prices(listings, config["listing_language"], zero_only=True)
            min_all, avg5_all = metrics(all_units, config["lowest_n"])
            min_zero, avg5_zero = metrics(zero_units, config["lowest_n"])

            db.execute(
                "INSERT OR REPLACE INTO snapshots"
                " (date, blueprint_id, currency, min_all_cents, avg5_all_cents,"
                "  min_zero_cents, avg5_zero_cents, n_listings)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (today, bp["id"], currency, min_all, avg5_all, min_zero, avg5_zero,
                 len(listings)),
            )
            total_products += 1

        db.commit()

    db.commit()
    db.close()
    print(f"\nDone: snapshot {today} saved for {total_products} products "
          f"({datetime.now(timezone.utc).strftime('%H:%M UTC')})")


if __name__ == "__main__":
    main()
