from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def synthetic_listing(tmp_path: Path) -> Path:
    """Create an ordered listing fixture without reading real catalog data."""
    listing = tmp_path / "synthetic-listing"
    listing.mkdir()
    for index, color in enumerate(((220, 80, 60), (40, 130, 210)), start=1):
        image = Image.new("RGB", (48 + index, 36 + index), color)
        image.save(listing / f"{index:02d}.png")
    return listing
