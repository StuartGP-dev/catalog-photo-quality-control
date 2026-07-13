from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .models import ListingVariant, SourceListing, canonical_json
from .recipe_schema import classify_recipe_family


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    listing_code TEXT NOT NULL UNIQUE,
    active_source_set_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS listing_images (
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    source_set_hash TEXT NOT NULL,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY(listing_id, source_set_hash, image_index)
);
CREATE TABLE IF NOT EXISTS listing_variants (
    variant_id INTEGER PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    source_set_hash TEXT NOT NULL,
    recipe_hash TEXT NOT NULL,
    recipe_json TEXT NOT NULL,
    recipe_family TEXT NOT NULL DEFAULT 'appearance_only',
    bench_test_id INTEGER,
    selected_rank INTEGER NOT NULL CHECK(selected_rank > 0),
    expected_image_count INTEGER NOT NULL CHECK(expected_image_count > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'ready')),
    title_text TEXT,
    description_text TEXT,
    price_cents INTEGER CHECK(price_cents IS NULL OR price_cents >= 0),
    currency TEXT,
    metadata_json TEXT,
    metadata_status TEXT NOT NULL DEFAULT 'reserved',
    aggregate_metrics_json TEXT NOT NULL DEFAULT '{}',
    quality_score REAL NOT NULL DEFAULT 0,
    distance_from_original REAL NOT NULL DEFAULT 0,
    minimum_selected_distance REAL,
    minimum_distance_components_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(listing_id, source_set_hash, recipe_hash),
    UNIQUE(listing_id, source_set_hash, selected_rank)
);
CREATE INDEX IF NOT EXISTS idx_listing_variants_ready
    ON listing_variants(listing_id, source_set_hash, status, selected_rank);
CREATE TABLE IF NOT EXISTS listing_variant_images (
    variant_id INTEGER NOT NULL REFERENCES listing_variants(variant_id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    output_path TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_width INTEGER NOT NULL DEFAULT 1,
    output_height INTEGER NOT NULL DEFAULT 1,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY(variant_id, image_index)
);
CREATE TRIGGER IF NOT EXISTS reject_ready_variant_insert
BEFORE INSERT ON listing_variants WHEN NEW.status = 'ready'
BEGIN
    SELECT RAISE(ABORT, 'ready variants must be finalized after adding images');
END;
CREATE TRIGGER IF NOT EXISTS validate_ready_variant_update
BEFORE UPDATE OF status ON listing_variants
WHEN NEW.status = 'ready'
BEGIN
    SELECT CASE WHEN (
        SELECT COUNT(*) FROM listing_variant_images WHERE variant_id = NEW.variant_id
    ) != NEW.expected_image_count
    THEN RAISE(ABORT, 'incomplete variant') END;
    SELECT CASE WHEN (
        SELECT COUNT(*) FROM listing_images
        WHERE listing_id = NEW.listing_id AND source_set_hash = NEW.source_set_hash
    ) != NEW.expected_image_count
    THEN RAISE(ABORT, 'variant does not cover active source set') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM listing_images source
        LEFT JOIN listing_variant_images output
          ON output.variant_id = NEW.variant_id
         AND output.image_index = source.image_index
         AND output.source_hash = source.source_hash
        WHERE source.listing_id = NEW.listing_id
          AND source.source_set_hash = NEW.source_set_hash
          AND output.variant_id IS NULL
    ) THEN RAISE(ABORT, 'variant image coverage mismatch') END;
END;
CREATE TRIGGER IF NOT EXISTS protect_ready_variant_images_delete
BEFORE DELETE ON listing_variant_images
WHEN (SELECT status FROM listing_variants WHERE variant_id=OLD.variant_id) = 'ready'
BEGIN
    SELECT RAISE(ABORT, 'cannot remove image from ready variant');
END;
"""


class VariantsDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "VariantsDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(listing_variant_images)")}
        for column in ("output_width", "output_height"):
            if column not in columns:
                self.connection.execute(f"ALTER TABLE listing_variant_images ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1")
        variant_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(listing_variants)")}
        if "recipe_family" not in variant_columns:
            self.connection.execute(
                "ALTER TABLE listing_variants ADD COLUMN recipe_family TEXT NOT NULL DEFAULT 'appearance_only'"
            )
            for row in self.connection.execute(
                "SELECT variant_id, recipe_json FROM listing_variants"
            ).fetchall():
                self.connection.execute(
                    "UPDATE listing_variants SET recipe_family=? WHERE variant_id=?",
                    (classify_recipe_family(json.loads(row["recipe_json"])), row["variant_id"]),
                )
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
                """INSERT INTO listings(listing_id, listing_code, active_source_set_hash)
                   VALUES (?, ?, ?)
                   ON CONFLICT(listing_id) DO UPDATE SET
                     active_source_set_hash=excluded.active_source_set_hash,
                     updated_at=CURRENT_TIMESTAMP""",
                (listing.listing_id, listing.listing_code, listing.source_set_hash),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO listing_images
                   (listing_id, source_set_hash, image_index, source_hash, source_path)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        listing.listing_id,
                        listing.source_set_hash,
                        image.index,
                        image.source_hash,
                        str(image.path),
                    )
                    for image in listing.images
                ],
            )

    def ready_count(self, listing_id: str, source_set_hash: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) FROM listing_variants
               WHERE listing_id=? AND source_set_hash=? AND status='ready'""",
            (listing_id, source_set_hash),
        ).fetchone()
        return int(row[0])

    def save_complete_variant(
        self,
        variant: ListingVariant,
        image_rows: Sequence[Mapping[str, object]],
    ) -> int:
        if len(image_rows) != len(variant.image_paths) or not image_rows:
            raise ValueError("variant image rows must exactly match variant paths")
        with self.transaction() as connection:
            for row in image_rows:
                duplicate = connection.execute(
                    """SELECT 1 FROM listing_variant_images image
                       JOIN listing_variants variant USING(variant_id)
                       WHERE variant.listing_id=? AND variant.source_set_hash=?
                         AND image.output_width=? AND image.output_height=?""",
                    (variant.listing_id, variant.source_set_hash, int(row.get("output_width", row.get("metrics", {}).get("output_width", 1))), int(row.get("output_height", row.get("metrics", {}).get("output_height", 1)))),
                ).fetchone()
                if duplicate:
                    raise ValueError("duplicate output pixel dimensions for source image")
            cursor = connection.execute(
                """INSERT INTO listing_variants
                   (listing_id, source_set_hash, recipe_hash, recipe_json, recipe_family,
                    bench_test_id, selected_rank, expected_image_count, title_text,
                    description_text, price_cents, currency, metadata_json,
                    metadata_status, aggregate_metrics_json, quality_score,
                    distance_from_original, minimum_selected_distance,
                    minimum_distance_components_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    variant.listing_id,
                    variant.source_set_hash,
                    variant.recipe.recipe_hash,
                    canonical_json(variant.recipe.parameters),
                    variant.recipe_family,
                    variant.bench_test_id,
                    variant.selected_rank,
                    len(image_rows),
                    variant.title_text,
                    variant.description_text,
                    variant.price_cents,
                    variant.currency,
                    variant.metadata_json,
                    variant.metadata_status,
                    canonical_json(variant.aggregate_metrics),
                    float(variant.aggregate_metrics.get("quality_score", 0)),
                    variant.distance_from_original,
                    variant.minimum_selected_distance,
                    canonical_json(variant.minimum_distance_components),
                ),
            )
            variant_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO listing_variant_images
                   (variant_id, image_index, source_hash, output_path, output_hash, metrics_json, output_width, output_height)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        variant_id,
                        int(row["image_index"]),
                        str(row["source_hash"]),
                        str(row["output_path"]),
                        str(row["output_hash"]),
                        canonical_json(row.get("metrics", {})),
                        int(row.get("output_width", row.get("metrics", {}).get("output_width", 1))),
                        int(row.get("output_height", row.get("metrics", {}).get("output_height", 1))),
                    )
                    for row in image_rows
                ],
            )
            connection.execute(
                "UPDATE listing_variants SET status='ready' WHERE variant_id=?",
                (variant_id,),
            )
        return variant_id
