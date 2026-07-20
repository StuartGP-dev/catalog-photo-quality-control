from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from PIL import ExifTags, Image

from .models import canonical_json
from .apply_metadata import apply_standard_metadata


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


def apply_reference_to_ready_variants(
    database_path: str | Path,
    reference_path: str | Path,
) -> tuple[int, int]:
    """Backfill technical metadata on ready generated copies not yet processed."""
    from .variants_db import VariantsDatabase

    database = Path(database_path).resolve()
    reference = Path(reference_path).resolve()
    if not reference.is_file():
        raise FileNotFoundError(reference)
    with VariantsDatabase(database) as variants:
        variants.initialize()
        rows = variants.connection.execute(
            """SELECT image.variant_id, image.image_index, image.output_path
               FROM listing_variant_images image
               JOIN listing_variants variant USING(variant_id)
               WHERE variant.status='ready' AND image.metadata_status!='stored'
               ORDER BY image.variant_id, image.image_index"""
        ).fetchall()
        if not rows:
            return 0, 0
        source_paths = {
            Path(row[0]).resolve()
            for row in variants.connection.execute("SELECT source_path FROM listing_images")
        }
        output_paths = [Path(row["output_path"]).resolve() for row in rows]
        if any(path in source_paths for path in output_paths):
            raise ValueError("a variant output resolves to a source image")
        if any(not path.is_file() for path in output_paths):
            raise FileNotFoundError("a ready variant output is missing")

        backup_root = Path(tempfile.mkdtemp(prefix="catalog-metadata-backfill-"))
        backups: list[tuple[Path, Path]] = []
        try:
            payloads: list[tuple[str, str, int, int]] = []
            for position, row in enumerate(rows):
                output = Path(row["output_path"]).resolve()
                backup = backup_root / f"{position:06d}{output.suffix}"
                shutil.copy2(output, backup)
                backups.append((backup, output))
                staged = output.with_suffix(output.suffix + ".metadata-staged")
                try:
                    apply_standard_metadata(output, reference, staged)
                    os.replace(staged, output)
                finally:
                    staged.unlink(missing_ok=True)
                payloads.append((
                    hashlib.sha256(output.read_bytes()).hexdigest(),
                    canonical_json(read_image_metadata(output)),
                    int(row["variant_id"]),
                    int(row["image_index"]),
                ))
            variant_ids = sorted({payload[2] for payload in payloads})
            with variants.connection:
                variants.connection.executemany(
                    """UPDATE listing_variant_images
                       SET output_hash=?, metadata_json=?, metadata_status='stored'
                       WHERE variant_id=? AND image_index=?""",
                    payloads,
                )
                variants.connection.executemany(
                    """UPDATE listing_variants
                       SET metadata_json=?, metadata_status='stored'
                       WHERE variant_id=?""",
                    [
                        (canonical_json({"policy": "technical_only", "image_count": sum(payload[2] == variant_id for payload in payloads)}), variant_id)
                        for variant_id in variant_ids
                    ],
                )
            return len(variant_ids), len(payloads)
        except BaseException:
            for backup, output in backups:
                shutil.copy2(backup, output)
            raise
        finally:
            shutil.rmtree(backup_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store factual per-image metadata in the final variants database.")
    parser.add_argument("--database", required=True)
    parser.add_argument("--variant-id", type=int)
    parser.add_argument("--reference", help="Backfill all unprocessed ready variants using this JPEG.")
    args = parser.parse_args(argv)
    if args.reference:
        variants, images = apply_reference_to_ready_variants(args.database, args.reference)
        print(f"updated_variants={variants}")
        print(f"updated_images={images}")
    elif args.variant_id is not None:
        count = write_variant_image_metadata(args.database, args.variant_id)
        print(f"updated_images={count}")
    else:
        parser.error("either --variant-id or --reference is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
