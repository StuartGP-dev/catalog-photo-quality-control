from __future__ import annotations

from pathlib import Path
import sqlite3
import pytest
from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.quality import evaluate_quality
from common.catalog_photo_control.recipe_schema import analyze_recipe
from common.catalog_photo_control.source_loader import load_source_listing

def test_problematic_recipes_are_rejected_before_render() -> None:
    schema = load_filter_space().schema
    rejected = (
        {"warmth": 0.2}, {"saturation": 1.5},
        {"resize_scale": 0.79}, {"unsharp_radius": 2.1},
        {"brightness": 1.01, "contrast": 1.01, "saturation": 1.01, "gamma": 1.01, "warmth": 0.01, "tint": 0.01, "sharpness": 1.01},
    )
    for values in rejected:
        with pytest.raises(ValueError): schema.canonicalize(values)

def test_subtle_recipe_analysis_is_deterministic() -> None:
    schema = load_filter_space().schema
    recipe = schema.canonicalize({"brightness": 1.01, "contrast": 0.99})
    analysis = analyze_recipe(recipe.parameters, schema.parameters)
    assert analysis.active_parameters == ("brightness", "contrast")
    assert analysis.active_parameter_count == 2
    assert analysis.recipe_intensity < schema.maximum_recipe_intensity

def _metrics(**changes: float) -> dict[str, float]:
    values = {"clip_fraction": 0.0, "sharpness_ratio": 1.0, "brightness": 0.5, "pixel_mae": 0.01, "luminance_mae": 0.01, "ssim": 0.995, "perceptual_geometry_ssim": 0.995}
    values.update(changes); return values

@pytest.mark.parametrize("changes,reason", [
    ({"ssim": 0.7}, "fidelity_ssim"), ({"pixel_mae": 0.2}, "fidelity_pixel_mae"),
    ({"luminance_mae": 0.2}, "fidelity_luminance_mae"), ({"sharpness_ratio": 2.1}, "fidelity_sharpness")])
def test_each_image_fidelity_barrier_rejects(changes, reason) -> None:
    result = evaluate_quality([_metrics(), _metrics(**changes)], load_filter_space().quality_thresholds)
    assert not result.valid and reason in result.reasons

def test_subtle_image_metrics_are_accepted() -> None:
    assert evaluate_quality([_metrics(), _metrics(ssim=0.98)], load_filter_space().quality_thresholds).valid


def test_geometry_uses_multiscale_fidelity_without_hiding_direct_ssim() -> None:
    thresholds = load_filter_space().quality_thresholds
    accepted = evaluate_quality(
        [_metrics(ssim=0.86, perceptual_geometry_ssim=0.985)],
        thresholds,
        geometry_active=True,
    )
    rejected = evaluate_quality(
        [_metrics(ssim=0.999, perceptual_geometry_ssim=0.7)],
        thresholds,
        geometry_active=True,
    )
    assert accepted.valid
    assert not rejected.valid
    assert "fidelity_geometry_ssim" in rejected.reasons

def test_cache_is_scoped_by_evaluation_config_hash(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="cache")
    space = load_filter_space(); recipe = space.schema.canonicalize({"brightness": 1.01})
    db = BenchDatabase(tmp_path / "bench.sqlite3"); db.initialize(); db.register_source(listing)
    first = db.execute_recipe_test(listing, recipe, tmp_path / "a", space.quality_thresholds, "config-a")
    second = db.execute_recipe_test(listing, recipe, tmp_path / "b", space.quality_thresholds, "config-b")
    assert not first.cached and not second.cached and first.test_id != second.test_id
    db.close()


def test_legacy_cache_identity_is_migrated(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE recipe_tests (
                test_id INTEGER PRIMARY KEY,
                listing_id TEXT NOT NULL,
                source_set_hash TEXT NOT NULL,
                recipe_id INTEGER NOT NULL,
                complete INTEGER NOT NULL,
                quality_valid INTEGER NOT NULL,
                eligible INTEGER NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                aggregate_metrics_json TEXT NOT NULL,
                error_text TEXT,
                retained_output_dir TEXT,
                context_key TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(listing_id, source_set_hash, recipe_id)
            )"""
        )
    listing = load_source_listing(synthetic_listing, listing_code="legacy-cache")
    space = load_filter_space()
    recipe = space.schema.canonicalize({"brightness": 1.01})
    db = BenchDatabase(path)
    db.initialize()
    db.register_source(listing)
    first = db.execute_recipe_test(
        listing, recipe, tmp_path / "a", space.quality_thresholds, "config-a"
    )
    second = db.execute_recipe_test(
        listing, recipe, tmp_path / "b", space.quality_thresholds, "config-b"
    )
    assert first.test_id != second.test_id
    identities = {
        tuple(item[2] for item in db.connection.execute(f"PRAGMA index_info({row[1]})"))
        for row in db.connection.execute("PRAGMA index_list(recipe_tests)")
        if row[2]
    }
    assert (
        "listing_id", "source_set_hash", "recipe_id", "evaluation_config_hash"
    ) in identities
    assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
