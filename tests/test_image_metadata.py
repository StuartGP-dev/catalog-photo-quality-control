from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from common.catalog_photo_control.image_metadata import apply_reference_to_ready_variants, write_variant_image_metadata
from common.catalog_photo_control.models import ListingVariant
from common.catalog_photo_control.source_loader import load_source_listing
from common.catalog_photo_control.variants_db import VariantsDatabase
from common.catalog_photo_control.config import load_filter_space


def test_writes_factual_metadata_for_each_variant_image(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    database_path = tmp_path / "variants.sqlite3"
    outputs = tuple(tmp_path / f"output-{image.index}.jpg" for image in listing.images)
    for output in outputs:
        Image.new("RGB", (32, 24), "white").save(output, dpi=(200, 200))
    with VariantsDatabase(database_path) as database:
        database.initialize()
        database.register_source(listing)
        variant = ListingVariant(None, listing.listing_id, listing.source_set_hash, load_filter_space().schema.canonicalize({}), outputs, 1)
        rows = [{"image_index": image.index, "source_hash": image.source_hash, "output_path": outputs[image.index], "output_hash": str(image.index), "metrics": {}} for image in listing.images]
        variant_id = database.save_complete_variant(variant, rows)

    assert write_variant_image_metadata(database_path, variant_id) == len(outputs)
    with VariantsDatabase(database_path) as database:
        rows = database.connection.execute("SELECT metadata_json, metadata_status FROM listing_variant_images ORDER BY image_index").fetchall()
        assert all(row["metadata_status"] == "stored" for row in rows)
        assert all(json.loads(row["metadata_json"])["width"] == 32 for row in rows)
        payload = json.loads(rows[0]["metadata_json"])
        assert {"file", "embedded_info", "icc_profile", "exif", "jpeg"} <= payload.keys()
        assert {"IFD0", "Exif", "GPSInfo", "Interop", "IFD1"} <= payload["exif"].keys()
        assert database.connection.execute("SELECT metadata_status FROM listing_variants").fetchone()[0] == "stored"


def test_backfills_ready_generated_copies_and_is_idempotent(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    database_path = tmp_path / "variants.sqlite3"
    outputs = tuple(tmp_path / f"selected-{image.index}.jpg" for image in listing.images)
    for output in outputs:
        Image.new("RGB", (32, 24), "white").save(output)
    reference = tmp_path / "reference.jpg"
    Image.new("RGB", (8, 8), "white").save(reference, dpi=(240, 240))
    with VariantsDatabase(database_path) as database:
        database.initialize(); database.register_source(listing)
        variant = ListingVariant(None, listing.listing_id, listing.source_set_hash, load_filter_space().schema.canonicalize({}), outputs, 1)
        rows = [{"image_index": image.index, "source_hash": image.source_hash, "output_path": outputs[image.index], "output_hash": "old", "metrics": {}} for image in listing.images]
        database.save_complete_variant(variant, rows)

    assert apply_reference_to_ready_variants(database_path, reference) == (1, len(outputs))
    assert apply_reference_to_ready_variants(database_path, reference) == (0, 0)
    with VariantsDatabase(database_path) as database:
        rows = database.connection.execute("SELECT output_hash, metadata_status FROM listing_variant_images").fetchall()
        assert all(row[0] != "old" and row[1] == "stored" for row in rows)
