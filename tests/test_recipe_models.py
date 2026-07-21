from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.catalog_photo_control.config import DEFAULT_FILTER_SPACE, load_filter_space
from common.catalog_photo_control.models import Recipe, canonical_json
from common.catalog_photo_control.recipe_schema import RecipeSchema
from common.catalog_photo_control.source_loader import load_source_listing


def test_canonical_json_and_recipe_hash_ignore_mapping_order() -> None:
    left = Recipe.from_parameters({"contrast": 1.0, "nested": {"b": 2, "a": 1}})
    right = Recipe.from_parameters({"nested": {"a": 1.0, "b": 2.0}, "contrast": 1})

    assert canonical_json(left.parameters) == canonical_json(right.parameters)
    assert left.recipe_hash == right.recipe_hash


def test_filter_space_loads_and_fills_canonical_defaults() -> None:
    space = load_filter_space()

    first = space.schema.canonicalize({"brightness": 1.02, "contrast": 1})
    second = space.schema.canonicalize({"contrast": 1.0, "brightness": 1.020})

    assert first.recipe_hash == second.recipe_hash
    assert set(first.parameters) == set(space.schema.parameters)
    assert space.selection_pool_multiplier >= 1


def test_invalid_range_fails_early() -> None:
    raw = json.loads(DEFAULT_FILTER_SPACE.read_text(encoding="utf-8"))
    raw["parameters"]["brightness"]["min"] = 2

    with pytest.raises(ValueError, match="min exceeds max"):
        RecipeSchema.from_mapping(raw)


def test_unknown_and_incompatible_settings_fail_early() -> None:
    schema = load_filter_space().schema

    with pytest.raises(ValueError, match="unknown recipe"):
        schema.canonicalize({"fixed_profile": "vivid"})
    raw = json.loads(DEFAULT_FILTER_SPACE.read_text(encoding="utf-8"))
    raw["parameters"]["grayscale_blend"].update(enabled=True, activation_probability=0.1)
    raw["parameters"]["sepia_blend"].update(enabled=True, activation_probability=0.1)
    enabled_style_schema = RecipeSchema.from_mapping(raw)
    with pytest.raises(ValueError, match="cannot be active together"):
        enabled_style_schema.canonicalize(
            {"grayscale_blend": 0.5, "sepia_blend": 0.2}
        )
    with pytest.raises(ValueError, match="disabled"):
        schema.canonicalize({"rounded_radius": 10, "canvas_padding_x": 0.01})
    with pytest.raises(ValueError, match="active value is below"):
        schema.canonicalize({"crop_fraction": 0.001})
    with pytest.raises(ValueError, match="active value is below"):
        schema.canonicalize({"zoom": 1.0005})
    with pytest.raises(ValueError, match="active value exceeds"):
        schema.canonicalize(
            {"resize_scale": 0.999, "canvas_mode": "sampled_background"}
        )


def test_source_set_hash_is_ordered_and_changes_with_source(
    synthetic_listing: Path,
) -> None:
    original = load_source_listing(synthetic_listing, listing_code="demo/item")
    repeated = load_source_listing(synthetic_listing, listing_code="demo/item")
    reversed_listing = load_source_listing(
        synthetic_listing,
        listing_code="demo/item",
        image_paths=reversed(sorted(synthetic_listing.iterdir())),
    )

    assert original.source_set_hash == repeated.source_set_hash
    assert original.source_set_hash != reversed_listing.source_set_hash

    changed_path = sorted(synthetic_listing.iterdir())[1]
    changed_path.write_bytes(changed_path.read_bytes() + b"synthetic-change")
    changed = load_source_listing(synthetic_listing, listing_code="demo/item")
    assert changed.source_set_hash != original.source_set_hash
    assert changed.images[0].source_hash == original.images[0].source_hash
