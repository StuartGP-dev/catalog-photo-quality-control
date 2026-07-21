from __future__ import annotations

from pathlib import Path

from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.source_loader import load_source_listing


def test_recipe_test_is_cached_and_source_change_gets_new_test(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    space = load_filter_space()
    recipe = space.schema.canonicalize({"brightness": 1.02})
    database = BenchDatabase(tmp_path / "bench.sqlite3")
    database.initialize()
    database.register_source(listing)

    first = database.execute_recipe_test(
        listing, recipe, tmp_path / "work", space.quality_thresholds
    )
    second = database.execute_recipe_test(
        listing, recipe, tmp_path / "work", space.quality_thresholds
    )

    assert first.complete
    assert not first.cached
    assert second.cached
    assert second.test_id == first.test_id
    assert database.connection.execute("SELECT COUNT(*) FROM recipe_tests").fetchone()[0] == 1

    changed = sorted(synthetic_listing.iterdir())[1]
    changed.write_bytes(changed.read_bytes() + b"changed-source-version")
    new_listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    database.register_source(new_listing)
    third = database.execute_recipe_test(
        new_listing, recipe, tmp_path / "work-new", space.quality_thresholds
    )
    assert not third.cached
    assert third.test_id != first.test_id
    assert database.connection.execute("SELECT COUNT(*) FROM recipe_tests").fetchone()[0] == 2
    database.close()


def test_rejected_test_keeps_metrics_but_removes_outputs(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    space = load_filter_space()
    recipe = space.schema.canonicalize({"brightness": 1.02})
    database = BenchDatabase(tmp_path / "bench.sqlite3")
    database.initialize()
    database.register_source(listing)

    execution = database.execute_recipe_test(
        listing,
        recipe,
        tmp_path / "work",
        {**space.quality_thresholds, "minimum_quality": 1.1},
    )

    assert execution.complete
    assert not execution.quality_valid
    assert execution.aggregate_metrics
    assert execution.output_dir is None
    assert not (tmp_path / "work" / recipe.recipe_hash).exists()
    rows = database.connection.execute(
        "SELECT output_path, output_hash, metrics_json FROM recipe_test_images WHERE test_id=?",
        (execution.test_id,),
    ).fetchall()
    assert len(rows) == len(listing.images)
    assert all(row[0] is None and row[1] and row[2] != "{}" for row in rows)
    database.close()
