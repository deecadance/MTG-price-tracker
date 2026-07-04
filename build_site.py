"""Export the SQLite price history to docs/data.json for the chart site.

Run after fetch_prices.py:

    python3 build_site.py

The docs/ folder is served by GitHub Pages; index.html reads data.json.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "prices.sqlite"
OUTPUT_PATH = PROJECT_DIR / "docs" / "data.json"


def cents_to_eur(cents):
    return round(cents / 100, 2) if cents is not None else None


def main():
    if not DB_PATH.exists():
        raise SystemExit("No database yet — run fetch_prices.py first.")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    sets = []
    expansions = db.execute(
        "SELECT * FROM expansions ORDER BY released_at DESC, name"
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

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "currency": "EUR",
        "sets": sets,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    n_products = sum(len(s["products"]) for s in sets)
    print(f"Wrote {OUTPUT_PATH.name}: {len(sets)} sets, {n_products} products")


if __name__ == "__main__":
    main()
