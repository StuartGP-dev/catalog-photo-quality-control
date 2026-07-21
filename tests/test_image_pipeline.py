from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.recipe_schema import classify_recipe_family
from common.catalog_photo_control.image_pipeline import apply_recipe, render_listing
from common.catalog_photo_control.source_loader import load_source_listing


def test_complete_listing_uses_one_recipe_and_is_reproducible(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize(
        {"brightness": 1.02, "rotation_degrees": 0.6, "jpeg_quality": 90}
    )

    first = render_listing(listing, recipe, tmp_path / "first")
    second = render_listing(listing, recipe, tmp_path / "second")

    assert first.recipe_hash == second.recipe_hash == recipe.recipe_hash
    assert len(first.images) == len(listing.images)
    assert [image.output_hash for image in first.images] == [
        image.output_hash for image in second.images
    ]
    assert [image.source_hash for image in first.images] == [
        image.source_hash for image in listing.images
    ]


def test_one_image_failure_rolls_back_whole_variant(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize({})
    destination = tmp_path / "variant"

    def fail_second(index: int) -> None:
        if index == 1:
            raise OSError("synthetic image failure")

    with pytest.raises(OSError, match="synthetic image failure"):
        render_listing(listing, recipe, destination, before_image=fail_second)

    assert not destination.exists()
    assert not list(tmp_path.glob(".variant-*"))


def test_horizontal_mirror_is_supported_and_vertical_mirror_is_unknown() -> None:
    schema = load_filter_space().schema
    recipe = schema.canonicalize({"horizontal_mirror": "on"})
    image = Image.new("RGB", (60, 40), "white"); ImageDraw.Draw(image).rectangle((5, 10, 20, 30), fill="red")
    output = apply_recipe(image, recipe)
    red_x = [x for y in range(output.height) for x in range(output.width) if output.getpixel((x, y))[0] > 150 and output.getpixel((x, y))[1] < 80]
    assert red_x and min(red_x) > 30
    with pytest.raises(ValueError, match="unknown recipe parameters"):
        schema.canonicalize({"vertical_mirror": "on"})


def test_horizontal_mirror_rejects_directional_text_like_content() -> None:
    schema = load_filter_space().schema; recipe = schema.canonicalize({"horizontal_mirror": "on"})
    image = Image.new("RGB", (160, 80), "white"); draw = ImageDraw.Draw(image)
    for x in range(5, 150, 8): draw.rectangle((x, 20, x + 3, 60), fill="black")
    with pytest.raises(ValueError, match="directional text"):
        apply_recipe(image, recipe)


def test_at_most_four_compatible_active_parameters_are_allowed() -> None:
    schema = load_filter_space().schema
    recipe = schema.canonicalize({"rotation_degrees": 1, "crop_fraction": .01, "brightness": 1.02, "warmth": .01})
    defaults = schema.canonicalize({}).parameters
    assert len([name for name in ("rotation_degrees", "crop_fraction", "brightness", "warmth") if recipe.parameters[name] != defaults[name]]) == 4
    with pytest.raises(ValueError, match="too_many_active_parameters"):
        schema.canonicalize({"rotation_degrees": 2, "crop_fraction": .02, "offset_x": .02, "brightness": 1.04, "contrast": 1.04, "warmth": .02})


def test_mirror_is_only_standalone_or_with_one_light_adjustment() -> None:
    schema = load_filter_space().schema
    recipe = schema.canonicalize({"horizontal_mirror": "on", "brightness": 1.02})
    assert classify_recipe_family(recipe.parameters) == "mirror_only_family"
    with pytest.raises(ValueError, match="mirror_forbidden_combination"):
        schema.canonicalize({"horizontal_mirror": "on", "crop_fraction": .02, "offset_y": .02, "perspective_y": .02})
    with pytest.raises(ValueError, match="mirror_too_many_active_parameters"):
        schema.canonicalize({"horizontal_mirror": "on", "brightness": 1.02, "contrast": 1.02})
