from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import Recipe, RecipeTest, SourceListing, canonical_json
from .paths import LocalPaths


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS source_listings (
    listing_id TEXT PRIMARY KEY,
    listing_code TEXT NOT NULL UNIQUE,
    current_source_set_hash TEXT NOT NULL,
    source_directory TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_images (
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    width INTEGER NOT NULL CHECK(width > 0),
    height INTEGER NOT NULL CHECK(height > 0),
    PRIMARY KEY (listing_id, source_set_hash, image_index)
);
CREATE INDEX IF NOT EXISTS idx_source_images_hash
    ON source_images(listing_id, source_set_hash, source_hash);
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    target_variants INTEGER NOT NULL CHECK(target_variants >= 0),
    status TEXT NOT NULL,
    stop_reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counters_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id INTEGER PRIMARY KEY,
    recipe_hash TEXT NOT NULL UNIQUE,
    parameters_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recipe_tests (
    test_id INTEGER PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id),
    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
    quality_valid INTEGER NOT NULL CHECK(quality_valid IN (0, 1)),
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
    aggregate_metrics_json TEXT NOT NULL,
    error_text TEXT,
    retained_output_dir TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (listing_id, source_set_hash, recipe_id)
);
CREATE INDEX IF NOT EXISTS idx_recipe_tests_candidates
    ON recipe_tests(listing_id, source_set_hash, complete, quality_valid, eligible);
CREATE TABLE IF NOT EXISTS recipe_test_images (
    test_id INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    success INTEGER NOT NULL CHECK(success IN (0, 1)),
    output_path TEXT,
    output_hash TEXT,
    metrics_json TEXT NOT NULL,
    error_text TEXT,
    PRIMARY KEY (test_id, image_index)
);
CREATE TABLE IF NOT EXISTS run_tests (
    run_id TEXT NOT NULL REFERENCES bench_runs(run_id) ON DELETE CASCADE,
    test_id INTEGER NOT NULL REFERENCES recipe_tests(test_id),
    proposal_source TEXT NOT NULL,
    cached INTEGER NOT NULL CHECK(cached IN (0, 1)),
    PRIMARY KEY (run_id, test_id)
);
CREATE TABLE IF NOT EXISTS recipe_pair_distances (
    listing_id TEXT NOT NULL,
    source_set_hash TEXT NOT NULL,
    test_a INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    test_b INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    components_json TEXT NOT NULL,
    distance REAL NOT NULL,
    CHECK(test_a < test_b),
    UNIQUE(listing_id, source_set_hash, test_a, test_b)
);
CREATE TABLE IF NOT EXISTS recipe_global_stats (
    recipe_id INTEGER PRIMARY KEY REFERENCES recipes(recipe_id) ON DELETE CASCADE,
    tested_count INTEGER NOT NULL DEFAULT 0,
    complete_count INTEGER NOT NULL DEFAULT 0,
    quality_valid_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recipe_context_stats (
    recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE,
    context_key TEXT NOT NULL,
    tested_count INTEGER NOT NULL DEFAULT 0,
    complete_count INTEGER NOT NULL DEFAULT 0,
    quality_valid_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(recipe_id, context_key)
);
"""


class BenchDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "BenchDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.connection:
                yield self.connection
        except Exception:
            self.connection.rollback()
            raise

    def register_source(self, listing: SourceListing) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO source_listings
                   (listing_id, listing_code, current_source_set_hash, source_directory)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(listing_id) DO UPDATE SET
                     current_source_set_hash=excluded.current_source_set_hash,
                     source_directory=excluded.source_directory,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    listing.listing_id,
                    listing.listing_code,
                    listing.source_set_hash,
                    str(listing.directory),
                ),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO source_images
                   (listing_id, source_set_hash, image_index, source_hash,
                    source_path, width, height) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        listing.listing_id,
                        listing.source_set_hash,
                        image.index,
                        image.source_hash,
                        str(image.path),
                        image.width,
                        image.height,
                    )
                    for image in listing.images
                ],
            )

    def recipe_id(self, recipe: Recipe) -> int:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO recipes(recipe_hash, parameters_json) VALUES (?, ?)",
                (recipe.recipe_hash, canonical_json(recipe.parameters)),
            )
            row = connection.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_hash=?", (recipe.recipe_hash,)
            ).fetchone()
        assert row is not None
        return int(row[0])

    def cached_test(
        self, listing_id: str, source_set_hash: str, recipe_hash: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT t.*, r.recipe_hash, r.parameters_json
               FROM recipe_tests t JOIN recipes r USING(recipe_id)
               WHERE t.listing_id=? AND t.source_set_hash=? AND r.recipe_hash=?""",
            (listing_id, source_set_hash, recipe_hash),
        ).fetchone()

    def record_test(
        self,
        test: RecipeTest,
        image_rows: Sequence[Mapping[str, Any]],
        *,
        retained_output_dir: str | None = None,
    ) -> int:
        recipe_id = self.recipe_id(test.recipe)
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO recipe_tests
                   (listing_id, source_set_hash, recipe_id, complete, quality_valid,
                    eligible, aggregate_metrics_json, error_text, retained_output_dir)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test.listing_id,
                    test.source_set_hash,
                    recipe_id,
                    int(test.complete),
                    int(test.quality_valid),
                    int(test.eligible),
                    canonical_json(test.aggregate_metrics),
                    test.error,
                    retained_output_dir,
                ),
            )
            test_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO recipe_test_images
                   (test_id, image_index, source_hash, success, output_path,
                    output_hash, metrics_json, error_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        test_id,
                        int(row["image_index"]),
                        str(row["source_hash"]),
                        int(bool(row["success"])),
                        row.get("output_path"),
                        row.get("output_hash"),
                        canonical_json(row.get("metrics", {})),
                        row.get("error"),
                    )
                    for row in image_rows
                ],
            )
        return test_id

    def add_run_test(
        self, run_id: str, test_id: int, proposal_source: str, *, cached: bool
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO run_tests VALUES (?, ?, ?, ?)",
                (run_id, test_id, proposal_source, int(cached)),
            )


def initialize_databases(local_root: str | Path = "local") -> LocalPaths:
    from .variants_db import VariantsDatabase

    paths = LocalPaths.from_root(local_root)
    paths.ensure_runtime_directories()
    with BenchDatabase(paths.bench_database) as bench:
        bench.initialize()
    with VariantsDatabase(paths.variants_database) as variants:
        variants.initialize()
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize both catalog SQLite databases.")
    parser.add_argument("--local-root", default="local")
    args = parser.parse_args(argv)
    paths = initialize_databases(args.local_root)
    print(paths.bench_database)
    print(paths.variants_database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
