from __future__ import annotations

from pathlib import Path
import pytest
from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.quality import evaluate_quality
from common.catalog_photo_control.recipe_schema import analyze_recipe
from common.catalog_photo_control.source_loader import load_source_listing

def test_problematic_recipes_are_rejected_before_render() -> None:
    schema = load_filter_space().schema
    rejected = (
        {"warmth": 0.025, "red_gain": 1.015},
        {"saturation": 1.04, "sharpness": 1.08},
        {"resize_scale": 0.82}, {"unsharp_radius": 2.1},
        {"brightness": 1.01, "contrast": 1.01, "saturation": 1.01, "gamma": 1.01, "warmth": 0.01},
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
    values = {"clip_fraction": 0.0, "sharpness_ratio": 1.0, "brightness": 0.5, "pixel_mae": 0.01, "luminance_mae": 0.01, "ssim": 0.995}
    values.update(changes); return values

@pytest.mark.parametrize("changes,reason", [
    ({"ssim": 0.9}, "fidelity_ssim"), ({"pixel_mae": 0.2}, "fidelity_pixel_mae"),
    ({"luminance_mae": 0.2}, "fidelity_luminance_mae"), ({"sharpness_ratio": 2.1}, "fidelity_sharpness")])
def test_each_image_fidelity_barrier_rejects(changes, reason) -> None:
    result = evaluate_quality([_metrics(), _metrics(**changes)], load_filter_space().quality_thresholds)
    assert not result.valid and reason in result.reasons

def test_subtle_image_metrics_are_accepted() -> None:
    assert evaluate_quality([_metrics(), _metrics(ssim=0.98)], load_filter_space().quality_thresholds).valid

def test_cache_is_scoped_by_evaluation_config_hash(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="cache")
    space = load_filter_space(); recipe = space.schema.canonicalize({"brightness": 1.01})
    db = BenchDatabase(tmp_path / "bench.sqlite3"); db.initialize(); db.register_source(listing)
    first = db.execute_recipe_test(listing, recipe, tmp_path / "a", space.quality_thresholds, "config-a")
    second = db.execute_recipe_test(listing, recipe, tmp_path / "b", space.quality_thresholds, "config-b")
    assert not first.cached and not second.cached and first.test_id != second.test_id
    db.close()
