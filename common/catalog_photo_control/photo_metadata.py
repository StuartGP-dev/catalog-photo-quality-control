from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps


EXIF_TAGS = {value: key for key, value in ExifTags.TAGS.items()}
GPS_TAG_ID = EXIF_TAGS.get("GPSInfo")
COMPARABLE_FIELDS = ("camera_model", "taken_at", "original_dimensions", "gps_present")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return " ".join(text.lower().split())


def normalize_camera_model(value: Any) -> str | None:
    return _clean_text(value)


def _get_exif_dict(image: Image.Image) -> dict[str, Any]:
    raw = image.getexif()
    if not raw:
        return {}
    result: dict[str, Any] = {}
    for tag_id, value in raw.items():
        result[ExifTags.TAGS.get(tag_id, str(tag_id))] = value
    return result


def extract_exif_info(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        exif = _get_exif_dict(image)
        current_dimensions = {"width": image.width, "height": image.height}

    width = exif.get("ExifImageWidth") or exif.get("ImageWidth")
    height = exif.get("ExifImageHeight") or exif.get("ImageLength")
    original_dimensions = None
    if width and height:
        try:
            original_dimensions = {"width": int(width), "height": int(height)}
        except (TypeError, ValueError):
            original_dimensions = None

    model = normalize_camera_model(exif.get("Model"))
    taken_at = _clean_text(
        exif.get("DateTimeOriginal") or exif.get("DateTimeDigitized") or exif.get("DateTime")
    )
    gps_present = bool(exif.get("GPSInfo")) if GPS_TAG_ID is not None else False

    return {
        "camera_model": model,
        "taken_at": taken_at,
        "original_dimensions": original_dimensions,
        "current_dimensions": current_dimensions,
        "gps_present": gps_present,
        "has_any_exif": bool(exif),
    }


def _field_is_missing(metadata: dict[str, Any], field: str) -> bool:
    if field == "gps_present":
        return not metadata.get("has_any_exif")
    return metadata.get(field) in (None, "", {})


def _field_payload(ref_value: Any, candidate_value: Any) -> dict[str, Any]:
    return {"reference": ref_value, "candidate": candidate_value}


def compare_photo_metadata_records(
    ref_metadata: dict[str, Any],
    candidate_metadata: dict[str, Any],
) -> dict[str, Any]:
    matched_fields: dict[str, dict[str, Any]] = {}
    mismatched_fields: dict[str, dict[str, Any]] = {}
    missing_fields: dict[str, dict[str, Any]] = {}

    for field in COMPARABLE_FIELDS:
        ref_missing = _field_is_missing(ref_metadata, field)
        candidate_missing = _field_is_missing(candidate_metadata, field)
        ref_value = ref_metadata.get(field)
        candidate_value = candidate_metadata.get(field)
        payload = _field_payload(ref_value, candidate_value)
        if ref_missing or candidate_missing:
            missing_fields[field] = payload
        elif ref_value == candidate_value:
            matched_fields[field] = payload
        else:
            mismatched_fields[field] = payload

    exif_status = classify_exif_status(ref_metadata, candidate_metadata, matched_fields, mismatched_fields)

    return {
        "reference": ref_metadata,
        "candidate": candidate_metadata,
        "exif_status": exif_status,
        "matched_fields": matched_fields,
        "mismatched_fields": mismatched_fields,
        "missing_fields": missing_fields,
        "supports_visual_context": exif_status in {"supportive", "strong_supportive"},
    }


def classify_exif_status(
    ref_metadata: dict[str, Any],
    candidate_metadata: dict[str, Any],
    matched_fields: dict[str, Any],
    mismatched_fields: dict[str, Any],
) -> str:
    if mismatched_fields:
        return "conflict"

    has_ref_signal = any(not _field_is_missing(ref_metadata, field) for field in COMPARABLE_FIELDS)
    has_candidate_signal = any(not _field_is_missing(candidate_metadata, field) for field in COMPARABLE_FIELDS)
    if not has_ref_signal and not has_candidate_signal:
        return "unavailable"

    if "taken_at" in matched_fields:
        return "strong_supportive"
    if {"camera_model", "original_dimensions"}.issubset(matched_fields):
        return "strong_supportive"
    if len(matched_fields) >= 3:
        return "strong_supportive"
    if matched_fields:
        return "supportive"
    return "neutral"


def compare_photo_metadata_info(ref_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    ref = extract_exif_info(ref_path)
    candidate = extract_exif_info(candidate_path)
    return compare_photo_metadata_records(ref, candidate)
