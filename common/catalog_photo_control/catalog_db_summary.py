from __future__ import annotations

import argparse
import sys

from .catalog_config import load_settings
from .catalog_db import open_catalog_db


def _scalar(db, sql: str, params=None) -> int:
    row = db.execute(sql, params or []).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a compact summary of the shared catalog DB.")
    parser.add_argument("--annonce-key", default=None, help="Optional annonce key filter, e.g. bijoux/O18.")
    parser.add_argument("--db-dsn", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(db_dsn=args.db_dsn)
    with open_catalog_db(settings) as db:
        if args.annonce_key:
            ph = db.placeholder
            row = db.execute(f"SELECT annonce_id, image_count FROM annonces WHERE annonce_key={ph}", [args.annonce_key]).fetchone()
            if not row:
                print(f"ANNONCE NOT FOUND: {args.annonce_key}")
                return 2
            annonce_id = str(row[0])
            image_count = int(row[1])
            candidates = _scalar(db, f"SELECT count(*) FROM annonce_filter_candidates WHERE annonce_id={ph}", [annonce_id])
            selected = _scalar(db, f"SELECT count(*) FROM annonce_filter_candidates WHERE annonce_id={ph} AND selected_at IS NOT NULL", [annonce_id])
            runs = _scalar(db, f"SELECT count(*) FROM annonce_filter_runs WHERE annonce_id={ph}", [annonce_id])
            print("CATALOG DB SUMMARY")
            print(f"db_backend: {settings.db_backend}")
            print(f"annonce_key: {args.annonce_key}")
            print(f"images: {image_count}")
            print(f"runs: {runs}")
            print(f"filter_candidates: {candidates}")
            print(f"selected_candidates: {selected}")
            return 0

        print("CATALOG DB SUMMARY")
        print(f"db_backend: {settings.db_backend}")
        print(f"annonces: {_scalar(db, 'SELECT count(*) FROM annonces')}")
        print(f"annonce_images: {_scalar(db, 'SELECT count(*) FROM annonce_images')}")
        print(f"filter_recipes: {_scalar(db, 'SELECT count(*) FROM filter_recipes')}")
        print(f"filter_candidates: {_scalar(db, 'SELECT count(*) FROM annonce_filter_candidates')}")
        print(f"filter_runs: {_scalar(db, 'SELECT count(*) FROM annonce_filter_runs')}")
        print(f"filter_selections: {_scalar(db, 'SELECT count(*) FROM annonce_filter_selections')}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
