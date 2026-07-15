from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.catalog_photo_control.bench_db import BenchDatabase, initialize_databases
from common.catalog_photo_control.models import ListingVariant, RecipeTest
from common.catalog_photo_control.source_loader import load_source_listing
from common.catalog_photo_control.variants_db import VariantsDatabase
from common.catalog_photo_control.config import load_filter_space


def test_one_command_initializes_both_databases(tmp_path: Path) -> None:
    paths = initialize_databases(tmp_path / "local")

    assert paths.bench_database.is_file()
    assert paths.variants_database.is_file()
    with sqlite3.connect(paths.bench_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "perceptual_comparisons" in tables and "image_pair_distances" not in tables
        columns = {row[1] for row in connection.execute("PRAGMA table_info(perceptual_comparisons)")}
        assert {"sha256_equal", "phash_distance", "phash_band", "dhash_distance", "dhash_band", "whash_distance", "whash_band", "verdict", "reason", "engine_version"} <= columns
    with sqlite3.connect(paths.variants_database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(listing_variants)")
        }
        assert {"title_text", "description_text", "price_cents", "currency", "metadata_json", "metadata_status"} <= columns


def test_duplicate_recipe_test_is_prevented(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize({"brightness": 1.02})
    test = RecipeTest(
        None, listing.listing_id, listing.source_set_hash, recipe,
        True, True, True, {"quality": 0.8}
    )
    database = BenchDatabase(tmp_path / "bench.sqlite3")
    database.initialize()
    database.register_source(listing)
    database.record_test(test, [])

    with pytest.raises(sqlite3.IntegrityError):
        database.record_test(test, [])
    database.close()


def test_final_database_rejects_incomplete_ready_variant(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize({})
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize()
    database.register_source(listing)
    with database.connection:
        variant_id = database.connection.execute(
            """INSERT INTO listing_variants
               (listing_id, source_set_hash, recipe_hash, recipe_json,
                selected_rank, expected_image_count)
               VALUES (?, ?, ?, '{}', 1, ?)""",
            (listing.listing_id, listing.source_set_hash, recipe.recipe_hash, len(listing.images)),
        ).lastrowid

    with pytest.raises(sqlite3.IntegrityError, match="incomplete variant"):
        with database.connection:
            database.connection.execute(
                "UPDATE listing_variants SET status='ready' WHERE variant_id=?",
                (variant_id,),
            )
    assert database.ready_count(listing.listing_id, listing.source_set_hash) == 0
    database.close()


def test_complete_variant_is_committed_atomically(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize({})
    output_paths = tuple(tmp_path / f"output-{image.index}.jpg" for image in listing.images)
    variant = ListingVariant(
        None, listing.listing_id, listing.source_set_hash, recipe, output_paths, 1
    )
    rows = [
        {
            "image_index": image.index,
            "source_hash": image.source_hash,
            "output_path": output_paths[image.index],
            "output_hash": f"hash-{image.index}",
            "metrics": {},
        }
        for image in listing.images
    ]
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize()
    database.register_source(listing)

    variant_id = database.save_complete_variant(variant, rows)

    row = database.connection.execute(
        "SELECT status, metadata_status FROM listing_variants WHERE variant_id=?",
        (variant_id,),
    ).fetchone()
    assert tuple(row) == ("ready", "reserved")
    database.close()


def test_only_one_ready_mirror_is_allowed_per_source_set(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    schema = load_filter_space().schema
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize()
    database.register_source(listing)

    def save(recipe, rank: int) -> int:
        paths = tuple(tmp_path / f"mirror-{rank}-{image.index}.jpg" for image in listing.images)
        variant = ListingVariant(None, listing.listing_id, listing.source_set_hash, recipe, paths, rank)
        rows = [{
            "image_index": image.index, "source_hash": image.source_hash,
            "output_path": paths[image.index], "output_hash": f"mirror-{rank}-{image.index}",
            "output_width": 100 + rank, "output_height": 100 + image.index, "metrics": {},
        } for image in listing.images]
        return database.save_complete_variant(variant, rows)

    save(schema.canonicalize({"horizontal_mirror": "on"}), 1)
    assert database.has_ready_mirror(listing.listing_id, listing.source_set_hash)
    with pytest.raises(ValueError, match="ready mirror variant already exists"):
        save(schema.canonicalize({"horizontal_mirror": "on", "brightness": 1.02}), 2)
    assert database.ready_count(listing.listing_id, listing.source_set_hash) == 1
    database.close()
