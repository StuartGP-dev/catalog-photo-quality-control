from __future__ import annotations

from pathlib import Path

import pytest

from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.image_pipeline import render_listing
from common.catalog_photo_control.source_loader import load_source_listing


def test_complete_listing_uses_one_recipe_and_is_reproducible(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    recipe = load_filter_space().schema.canonicalize(
        {"brightness": 1.02, "rotation_degrees": 0.4, "jpeg_quality": 90}
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
