from __future__ import annotations

from pathlib import Path

from PIL import Image

from common.catalog_photo_control.catalog_report import write_catalog_report
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.models import ListingVariant
from common.catalog_photo_control.source_loader import load_source_listing
from common.catalog_photo_control.variants_db import VariantsDatabase


def test_catalog_report_shows_all_database_columns_and_images(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    database_path = tmp_path / "variants.sqlite3"
    outputs = tuple(tmp_path / f"output-{image.index}.jpg" for image in listing.images)
    for output in outputs:
        Image.new("RGB", (24, 18), "white").save(output)
    with VariantsDatabase(database_path) as database:
        database.initialize(); database.register_source(listing)
        variant = ListingVariant(None, listing.listing_id, listing.source_set_hash, load_filter_space().schema.canonicalize({}), outputs, 1)
        database.save_complete_variant(variant, [{"image_index": image.index, "source_hash": image.source_hash, "output_path": outputs[image.index], "output_hash": str(image.index), "metrics": {}} for image in listing.images])

    report = write_catalog_report(database_path, tmp_path / "catalog" / "index.html")
    content = report.read_text(encoding="utf-8")
    assert "synthetic" in content
    assert "active_source_set_hash" in content
    assert "average_ready_distance" in content
    assert "metadata_status" in content
    assert content.count("<img ") == len(listing.images)
    assert list(report.parent.glob("*.html")) == [report]
