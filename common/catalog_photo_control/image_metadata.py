from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from PIL import ExifTags, Image, ImageCms

from .models import canonical_json
from .apply_metadata import apply_standard_metadata


def _safe_metadata_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (tuple, list)):
        return [_safe_metadata_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_metadata_value(item) for key, item in value.items()}
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _named_tags(values: object, *, gps: bool = False) -> dict[str, object]:
    if not hasattr(values, "items"):
        return {}
    names = ExifTags.GPSTAGS if gps else ExifTags.TAGS
    return {
        names.get(tag, str(tag)): _safe_metadata_value(value)
        for tag, value in values.items()
    }


def read_image_metadata(path: str | Path) -> dict[str, object]:
    """Return every factual metadata field Pillow can expose without deriving values."""
    resolved = Path(path).resolve()
    with Image.open(resolved) as image:
        raw_exif = image.getexif()
        exif_ifds: dict[str, object] = {}
        for ifd_name in ("Exif", "GPSInfo", "Interop", "IFD1"):
            ifd_id = getattr(ExifTags.IFD, ifd_name, None)
            if ifd_id is None:
                exif_ifds[ifd_name] = {}
                continue
            try:
                values = raw_exif.get_ifd(ifd_id)
            except (KeyError, OSError, TypeError, ValueError):
                values = {}
            exif_ifds[ifd_name] = _named_tags(values, gps=ifd_name == "GPSInfo")
        dpi = image.info.get("dpi")
        embedded_info = {
            str(key): _safe_metadata_value(value)
            for key, value in image.info.items()
        }
        icc_profile = image.info.get("icc_profile")
        icc_details: dict[str, object] | None = None
        if isinstance(icc_profile, bytes):
            icc_details = _safe_metadata_value(icc_profile)
            assert isinstance(icc_details, dict)
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                icc_details.update({
                    "name": ImageCms.getProfileName(profile).strip(),
                    "description": ImageCms.getProfileDescription(profile).strip(),
                    "info": ImageCms.getProfileInfo(profile).strip(),
                })
            except (OSError, TypeError, ValueError):
                icc_details["parse_status"] = "unavailable"
        return {
            "file": {
                "byte_length": resolved.stat().st_size,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            },
            "format": image.format,
            "format_description": getattr(image, "format_description", None),
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "bands": list(image.getbands()),
            "frame_count": int(getattr(image, "n_frames", 1)),
            "animated": bool(getattr(image, "is_animated", False)),
            "icc_profile_present": icc_profile is not None,
            "dpi": [round(float(value), 4) for value in dpi] if dpi else None,
            "embedded_info": embedded_info,
            "icc_profile": icc_details,
            "exif": {
                "IFD0": _named_tags(raw_exif),
                **exif_ifds,
            },
            "jpeg": {
                "layers": _safe_metadata_value(getattr(image, "layer", [])),
                "quantization_tables": _safe_metadata_value(getattr(image, "quantization", {})),
            } if image.format == "JPEG" else None,
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
    *,
    force: bool = False,
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
               WHERE variant.status='ready' AND (? OR image.metadata_status!='stored')
               ORDER BY image.variant_id, image.image_index""",
            (int(force),),
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
                        (canonical_json({"policy": "technical_plus_reference_identity", "image_count": sum(payload[2] == variant_id for payload in payloads)}), variant_id)
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
    parser.add_argument("--force", action="store_true", help="Reapply metadata to variants already marked stored.")
    args = parser.parse_args(argv)
    if args.reference:
        variants, images = apply_reference_to_ready_variants(args.database, args.reference, force=args.force)
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
