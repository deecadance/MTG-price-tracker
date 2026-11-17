"""Export the SQLite price history to docs/data.json for the chart site.

Run after fetch_prices.py:

    python3 build_site.py

The docs/ folder is served by GitHub Pages; index.html reads data.json.

Price projections (Phase 4) are OFF by default. They are added to data.json
only when config.json contains "show_projections": true — see FORECAST.md.
When they are off, data.json is exactly what it always was.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "prices.sqlite"
OUTPUT_PATH = PROJECT_DIR / "docs" / "data.json"
CONFIG_PATH = PROJECT_DIR / "config.json"

# The price line that projections are drawn for on the site.
PROJECTION_METRIC = "avg5_all"


def cents_to_eur(cents):
    return round(cents / 100, 2) if cents is not None else None


def load_config(config_path):
    """Read config.json; a missing or broken file means "all extras off"."""
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def add_projections(db, sets, payload, config, latest_date):
    """Add price projections to the site data (only called when switched on).

    If anything goes wrong here, the site is still built — just without
    projections — and a warning shows up in the GitHub Actions log.
    """
    try:
        import forecast  # imported only when needed, so it can never break the normal build

        site = forecast.for_site(
            db, PROJECTION_METRIC, latest_date, config.get("projection_events", [])
        )
    except Exception as error:  # noqa: BLE001 — never let projections break the site
        print(f"WARNING: projections skipped because of an error: {error!r}", file=sys.stderr)
        return
    n_with_projection = 0
    for s in sets:
        for product in s["products"]:
            block = site["products"].get(product["id"])
            if block:
                product["forecast"] = block
                n_with_projection += 1
    payload["projection"] = site["projection"]
    print(f"Added projections for {n_with_projection} products")


def main(db_path=DB_PATH, output_path=OUTPUT_PATH, config_path=CONFIG_PATH):
    db_path, output_path = Path(db_path), Path(output_path)
    if not db_path.exists():
        raise SystemExit("No database yet — run fetch_prices.py first.")

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    sets = []
    # Sets without a known release date (brand-new preorders, or CardTrader-only
    # groupings like holiday releases) go first, then newest to oldest.
    expansions = db.execute(
        "SELECT * FROM expansions ORDER BY released_at IS NULL DESC, released_at DESC, name"
    ).fetchall()

    for expansion in expansions:
        products = []
        blueprints = db.execute(
            "SELECT * FROM blueprints WHERE expansion_id = ? ORDER BY name",
            (expansion["id"],),
        ).fetchall()

        for bp in blueprints:
            rows = db.execute(
                "SELECT * FROM snapshots WHERE blueprint_id = ? ORDER BY date",
                (bp["id"],),
            ).fetchall()
            if not rows:
                continue
            products.append(
                {
                    "id": bp["id"],
                    "name": bp["name"],
                    "category": bp["category"],
                    "dates": [r["date"] for r in rows],
                    "min_all": [cents_to_eur(r["min_all_cents"]) for r in rows],
                    "avg5_all": [cents_to_eur(r["avg5_all_cents"]) for r in rows],
                    "min_zero": [cents_to_eur(r["min_zero_cents"]) for r in rows],
                    "avg5_zero": [cents_to_eur(r["avg5_zero_cents"]) for r in rows],
                }
            )

        if products:
            sets.append(
                {
                    "code": expansion["code"],
                    "name": expansion["name"],
                    "released_at": expansion["released_at"],
                    "products": products,
                }
            )

    latest_date = db.execute("SELECT max(date) FROM snapshots").fetchone()[0]
    last_run = None
    has_runs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    if has_runs:
        row = db.execute("SELECT date, status FROM runs ORDER BY date DESC LIMIT 1").fetchone()
        if row:
            last_run = {"date": row["date"], "status": row["status"]}

    output_path.parent.mkdir(exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "currency": "EUR",
        "latest_date": latest_date,
        "last_run": last_run,
        "sets": sets,
    }

    # Phase 4 switch: only `"show_projections": true` (exactly) turns this on.
    config = load_config(config_path)
    if config.get("show_projections") is True:
        add_projections(db, sets, payload, config, latest_date)

    output_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    n_products = sum(len(s["products"]) for s in sets)
    print(f"Wrote {output_path.name}: {len(sets)} sets, {n_products} products")


if __name__ == "__main__":
    main()
