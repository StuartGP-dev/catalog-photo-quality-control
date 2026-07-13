from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import DEFAULT_FILTER_SPACE, load_filter_space
from common.catalog_photo_control.image_pipeline import apply_recipe
from common.catalog_photo_control.models import Recipe
from common.catalog_photo_control.recipe_generator import RecipeGenerator
from common.catalog_photo_control.recipe_schema import classify_recipe_family
from common.catalog_photo_control.selector import Candidate, select_max_min
from common.catalog_photo_control.source_loader import load_source_listing


def _product_image(background: tuple[int, int, int] = (242, 242, 242)) -> Image.Image:
    image = Image.new("RGB", (200, 160), background)
    ImageDraw.Draw(image).rectangle((65, 35, 135, 125), fill=(80, 70, 120))
    return image


def _dezoom(mode: str, background: tuple[int, int, int] = (242, 242, 242)):
    values: dict[str, object] = {
        "resize_scale": 0.98,
        "canvas_mode": mode,
    }
    if mode == "side_bands":
        values["side_band_width"] = 0.01
    if mode == "uniform_frame":
        values["uniform_frame_width"] = 0.006
    recipe = load_filter_space().schema.canonicalize(values)
    return apply_recipe(_product_image(background), recipe)


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("white", (255, 255, 255)), ("light_gray", (246, 246, 246))),
)
def test_dezoom_fixed_light_backgrounds(mode: str, expected: tuple[int, int, int]) -> None:
    output = _dezoom(mode)
    metadata = output.info["canvas_metadata"]
    assert metadata["background_rgb"] == expected
    assert metadata["foreground_scale_ratio"] == pytest.approx(0.98)
    assert output.getpixel((0, 0)) == expected


@pytest.mark.parametrize("mode", ("sampled_background", "sampled_edge"))
def test_dezoom_sampled_background_modes_are_light_and_deterministic(mode: str) -> None:
    first = _dezoom(mode, (238, 240, 242))
    second = _dezoom(mode, (238, 240, 242))
    first_metadata = first.info["canvas_metadata"]
    assert first_metadata == second.info["canvas_metadata"]
    assert first_metadata["background_origin"] == mode
    assert min(first_metadata["background_rgb"]) >= 238
    assert max(first_metadata["background_rgb"]) - min(first_metadata["background_rgb"]) <= 4


@pytest.mark.parametrize("background", ((20, 20, 20), (240, 20, 20)))
def test_dezoom_rejects_dark_or_saturated_sample(background: tuple[int, int, int]) -> None:
    output = _dezoom("sampled_background", background)
    metadata = output.info["canvas_metadata"]
    assert metadata["sampled_background_fallback_used"] is True
    assert metadata["sampled_background_rgb"] == (246, 246, 246)
    assert metadata["background_rgb"] == (247, 247, 247)
    assert metadata["background_origin"] == "fallback_light_gray"


def test_dezoom_requires_canvas_and_preserves_centered_proportions() -> None:
    schema = load_filter_space().schema
    with pytest.raises(ValueError, match="dezoom requires"):
        schema.canonicalize({"resize_scale": 0.98, "canvas_mode": "none"})

    for mode in ("side_bands", "uniform_frame"):
        output = _dezoom(mode)
        metadata = output.info["canvas_metadata"]
        left, top, right, bottom = metadata["content_box"]
        assert (right - left, bottom - top) == (196, 157)
        assert abs((right - left) / (bottom - top) - 200 / 160) < 0.01
        assert abs(left - (output.width - right)) <= 1
        assert abs(top - (output.height - bottom)) <= 1
        assert output.crop(metadata["content_box"]).getbbox() == (0, 0, 196, 157)
        assert metadata["canvas_fraction"] < 0.2
    bands = _dezoom("side_bands").info["canvas_metadata"]
    frame = _dezoom("uniform_frame").info["canvas_metadata"]
    assert bands["padding_x"] > bands["padding_y"]
    assert frame["padding_x"] > 0 and frame["padding_y"] > 0


def test_bounded_geometry_combinations_and_families_are_deterministic() -> None:
    space = load_filter_space()
    first = RecipeGenerator(space.schema, dict(space.proposal_allocation), seed=19)
    second = RecipeGenerator(space.schema, dict(space.proposal_allocation), seed=19)
    left = [first.random_recipe() for _ in range(400)]
    right = [second.random_recipe() for _ in range(400)]
    assert [recipe.recipe_hash for recipe in left] == [recipe.recipe_hash for recipe in right]
    families = Counter(classify_recipe_family(recipe.parameters) for recipe in left)
    assert {
        "appearance_only", "rotation_family", "crop_family", "zoom_family",
        "dezoom_canvas_family", "mixed_geometry_family",
    } <= families.keys()
    assert families["appearance_only"] > 0
    assert any(
        recipe.parameters["rotation_degrees"] != 0
        and recipe.parameters["crop_fraction"] > 0
        for recipe in left
    )
    assert any(
        recipe.parameters["zoom"] > 1 and recipe.parameters["offset_x"] != 0
        for recipe in left
    )
    assert any(
        classify_recipe_family(recipe.parameters) == "zoom_family"
        and recipe.parameters["offset_x"] == recipe.parameters["offset_y"] == 0
        and 1.001 <= recipe.parameters["zoom"] <= 1.004
        for recipe in left
    )
    for recipe in left:
        p = recipe.parameters
        if p["resize_scale"] < 1:
            assert p["canvas_mode"] != "none"
        space.schema.canonicalize(p)


def _candidate(index: int, family: str, quality: float = 0.8) -> Candidate:
    return Candidate(
        index,
        Recipe.from_parameters({"identity": index}),
        {
            "quality_score": quality,
            "mean_brightness": 0.5,
            "recipe_family": family,
        },
        (),
    )


def test_family_bonus_preserves_max_min_and_prevents_appearance_domination() -> None:
    candidates = [_candidate(1, "appearance_only", 0.95)]
    candidates += [_candidate(index, "appearance_only") for index in range(2, 7)]
    candidates += [
        _candidate(10, "rotation_family"),
        _candidate(11, "crop_family"),
        _candidate(12, "zoom_family"),
        _candidate(13, "dezoom_canvas_family"),
    ]
    selected = select_max_min(candidates, 5)
    families = Counter(item.candidate.recipe_family for item in selected)
    assert selected[0].candidate.test_id == 1
    assert set(families) == {
        "appearance_only", "rotation_family", "crop_family", "zoom_family",
        "dezoom_canvas_family",
    }
    assert families["appearance_only"] == 1
    assert selected[1].minimum_distance is not None


def test_recipe_family_persists_in_bench_and_evaluation_hash_changes(
    tmp_path: Path,
) -> None:
    listing_path = tmp_path / "listing"
    listing_path.mkdir()
    _product_image().save(listing_path / "01.png")
    listing = load_source_listing(listing_path, listing_code="geometry")
    space = load_filter_space()
    recipe = space.schema.canonicalize(
        {"resize_scale": 0.985, "canvas_mode": "sampled_background"}
    )
    database = BenchDatabase(tmp_path / "bench.sqlite3")
    database.initialize()
    database.register_source(listing)
    execution = database.execute_recipe_test(
        listing, recipe, tmp_path / "work", space.quality_thresholds,
        space.evaluation_config_hash,
    )
    assert execution.complete
    row = database.connection.execute(
        """SELECT recipes.recipe_family, recipe_tests.recipe_family,
                  recipe_tests.aggregate_metrics_json
           FROM recipe_tests JOIN recipes USING(recipe_id)"""
    ).fetchone()
    assert row[0] == row[1] == "dezoom_canvas_family"
    assert json.loads(row[2])["recipe_family"] == "dezoom_canvas_family"
    assert database.execute_recipe_test(
        listing, recipe, tmp_path / "work", space.quality_thresholds,
        space.evaluation_config_hash,
    ).cached
    database.close()

    raw = json.loads(DEFAULT_FILTER_SPACE.read_text(encoding="utf-8"))
    raw["geometry_template_probability"] = 0.45
    changed_path = tmp_path / "filter_space.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_filter_space(changed_path).evaluation_config_hash != space.evaluation_config_hash
