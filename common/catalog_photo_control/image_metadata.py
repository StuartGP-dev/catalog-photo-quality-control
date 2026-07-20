from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from PIL import ExifTags, Image

from .models import canonical_json


CAPTURE_TAGS = {
    "Make", "Model", "DateTime", "DateTimeOriginal", "DateTimeDigitized",
    "GPSInfo", "MakerNote", "LensMake", "LensModel", "BodySerialNumber",
}


def read_image_metadata(path: str | Path) -> dict[str, object]:
    """Return factual metadata present in the file, without deriving capture data."""
    resolved = Path(path).resolve()
    with Image.open(resolved) as image:
        exif_names = {
            ExifTags.TAGS.get(tag, str(tag)): value
            for tag, value in image.getexif().items()
            if ExifTags.TAGS.get(tag, str(tag)) not in CAPTURE_TAGS
            and isinstance(value, (str, int, float))
        }
        dpi = image.info.get("dpi")
        return {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "icc_profile_present": bool(image.info.get("icc_profile")),
            "dpi": [round(float(value), 4) for value in dpi] if dpi else None,
            "technical_exif": exif_names,
        }


def write_variant_image_metadata(database_path: str | Path, variant_id: int) -> int:
    """Refresh per-image metadata fields from the ready variant's output files."""
    connection = sqlite3.connect(Path(database_path))
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(listing_variant_images)")}
        if not {"metadata_json", "metadata_status"} <= columns:
            raise ValueError("variants database must be initialized/migrated first")
        rows = connection.execute(
            "SELECT image_index, output_path FROM listing_variant_images WHERE variant_id=? ORDER BY image_index",
            (variant_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"unknown or empty variant: {variant_id}")
        payloads = [(canonical_json(read_image_metadata(row["output_path"])), variant_id, row["image_index"]) for row in rows]
        with connection:
            connection.executemany(
                "UPDATE listing_variant_images SET metadata_json=?, metadata_status='stored' WHERE variant_id=? AND image_index=?",
                payloads,
            )
            connection.execute(
                "UPDATE listing_variants SET metadata_status='stored' WHERE variant_id=?",
                (variant_id,),
            )
        return len(payloads)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store factual per-image metadata in the final variants database.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--variant-id", required=True, type=int)
    args = parser.parse_args(argv)
    count = write_variant_image_metadata(args.database, args.variant_id)
    print(f"updated_images={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
