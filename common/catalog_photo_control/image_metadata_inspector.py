from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imagehash
import numpy as np
from PIL import ExifTags, Image, ImageOps

try:
    from PIL import ImageCms
except Exception:  # pragma: no cover - depends on the Pillow build
    ImageCms = None  # type: ignore

try:
    from skimage.metrics import structural_similarity
except Exception:  # pragma: no cover - dependency guard
    structural_similarity = None  # type: ignore


EXIF_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.TAGS.items()}
GPS_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.GPSTAGS.items()}

JPEG_MARKER_NAMES = {
    0x01: "TEM",
    0xC0: "SOF0_BASELINE_DCT",
    0xC1: "SOF1_EXTENDED_SEQUENTIAL_DCT",
    0xC2: "SOF2_PROGRESSIVE_DCT",
    0xC3: "SOF3_LOSSLESS_SEQUENTIAL",
    0xC4: "DHT",
    0xC5: "SOF5_DIFFERENTIAL_SEQUENTIAL_DCT",
    0xC6: "SOF6_DIFFERENTIAL_PROGRESSIVE_DCT",
    0xC7: "SOF7_DIFFERENTIAL_LOSSLESS",
    0xC8: "JPG_RESERVED",
    0xC9: "SOF9_EXTENDED_SEQUENTIAL_ARITHMETIC",
    0xCA: "SOF10_PROGRESSIVE_ARITHMETIC",
    0xCB: "SOF11_LOSSLESS_ARITHMETIC",
    0xCC: "DAC",
    0xCD: "SOF13_DIFFERENTIAL_SEQUENTIAL_ARITHMETIC",
    0xCE: "SOF14_DIFFERENTIAL_PROGRESSIVE_ARITHMETIC",
    0xCF: "SOF15_DIFFERENTIAL_LOSSLESS_ARITHMETIC",
    0xD0: "RST0",
    0xD1: "RST1",
    0xD2: "RST2",
    0xD3: "RST3",
    0xD4: "RST4",
    0xD5: "RST5",
    0xD6: "RST6",
    0xD7: "RST7",
    0xD8: "SOI",
    0xD9: "EOI",
    0xDA: "SOS",
    0xDB: "DQT",
    0xDC: "DNL",
    0xDD: "DRI",
    0xDE: "DHP",
    0xDF: "EXP",
    0xE0: "APP0",
    0xE1: "APP1",
    0xE2: "APP2",
    0xE3: "APP3",
    0xE4: "APP4",
    0xE5: "APP5",
    0xE6: "APP6",
    0xE7: "APP7",
    0xE8: "APP8",
    0xE9: "APP9",
    0xEA: "APP10",
    0xEB: "APP11",
    0xEC: "APP12",
    0xED: "APP13",
    0xEE: "APP14",
    0xEF: "APP15",
    0xFE: "COM",
}

JPEG_STANDALONE_MARKERS = {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
PROGRESSIVE_SOF_MARKERS = {0xC2, 0xC6, 0xCA, 0xCE}

WINDOWS_FILE_ATTRIBUTES = {
    0x0001: "READONLY",
    0x0002: "HIDDEN",
    0x0004: "SYSTEM",
    0x0010: "DIRECTORY",
    0x0020: "ARCHIVE",
    0x0040: "DEVICE",
    0x0080: "NORMAL",
    0x0100: "TEMPORARY",
    0x0200: "SPARSE_FILE",
    0x0400: "REPARSE_POINT",
    0x0800: "COMPRESSED",
    0x1000: "OFFLINE",
    0x2000: "NOT_CONTENT_INDEXED",
    0x4000: "ENCRYPTED",
    0x8000: "INTEGRITY_STREAM",
    0x20000: "NO_SCRUB_DATA",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    """Convert values to lossless JSON-friendly data.

    Binary metadata is preserved in full as base64, with size, SHA-256 and a
    short hexadecimal preview. No metadata binary payload is truncated.
    """
    if isinstance(value, bytes):
        return {
            "type": "binary",
            "length": len(value),
            "sha256": _sha256_bytes(value),
            "base64": base64.b64encode(value).decode("ascii"),
            "hex_preview": value[:64].hex(),
        }
    if isinstance(value, bytearray):
        return _json_value(bytes(value))
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        numerator = int(value.numerator)
        denominator = int(value.denominator)
        return {
            "type": "rational",
            "numerator": numerator,
            "denominator": denominator,
            "text": f"{numerator}/{denominator}",
            "decimal": (numerator / denominator) if denominator else None,
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _timestamp_payload(epoch: float | None) -> dict[str, Any] | None:
    if epoch is None:
        return None
    local_tz = datetime.now().astimezone().tzinfo
    return {
        "epoch": float(epoch),
        "utc_iso": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
        "local_iso": datetime.fromtimestamp(epoch, tz=local_tz).isoformat(),
    }


def _read_file_system_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    creation_epoch: float | None = None
    creation_source = "unavailable"
    if hasattr(stat, "st_birthtime"):
        creation_epoch = float(stat.st_birthtime)  # type: ignore[attr-defined]
        creation_source = "st_birthtime"
    elif os.name == "nt":
        creation_epoch = float(stat.st_ctime)
        creation_source = "windows_st_ctime_creation_time"

    raw_attributes = getattr(stat, "st_file_attributes", None)
    attribute_names: list[str] = []
    if isinstance(raw_attributes, int):
        attribute_names = [name for bit, name in WINDOWS_FILE_ATTRIBUTES.items() if raw_attributes & bit]

    return {
        "name": path.name,
        "stem": path.stem,
        "suffix": path.suffix,
        "absolute_path": str(path.resolve()),
        "parent": str(path.resolve().parent),
        "size_bytes": stat.st_size,
        "sha256_complete_file": _sha256_file(path),
        "windows_creation_time": _timestamp_payload(creation_epoch),
        "windows_creation_time_source": creation_source,
        "modified_time": _timestamp_payload(float(stat.st_mtime)),
        "last_access_time": _timestamp_payload(float(stat.st_atime)),
        "ctime_raw": _timestamp_payload(float(stat.st_ctime)),
        "windows_file_attributes_raw": raw_attributes,
        "windows_file_attributes": attribute_names,
        "mode_octal": oct(stat.st_mode),
    }


def _read_exif(image: Image.Image) -> dict[str, Any]:
    try:
        exif = image.getexif()
    except Exception as exc:
        return {"error": str(exc), "present": False, "tags": {}, "ifds": {}}

    tags: dict[str, Any] = {}
    raw_tags: dict[str, Any] = {}
    for tag_id, value in exif.items():
        name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
        tags[name] = _json_value(value)
        raw_tags[str(int(tag_id))] = {"name": name, "value": _json_value(value)}

    ifds: dict[str, Any] = {}
    ifd_enum = getattr(ExifTags, "IFD", None)
    if ifd_enum is not None:
        for ifd_name in ("Exif", "GPSInfo", "Interop", "IFD1", "MakerNote"):
            ifd_id = getattr(ifd_enum, ifd_name, None)
            if ifd_id is None:
                continue
            try:
                ifd = exif.get_ifd(ifd_id)
            except Exception:
                continue
            if not ifd:
                continue
            decoded: dict[str, Any] = {}
            raw_decoded: dict[str, Any] = {}
            for tag_id, value in ifd.items():
                if ifd_name == "GPSInfo":
                    name = GPS_TAGS_BY_ID.get(int(tag_id), str(tag_id))
                else:
                    name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
                decoded[name] = _json_value(value)
                raw_decoded[str(int(tag_id))] = {"name": name, "value": _json_value(value)}
            ifds[ifd_name] = {"tags": decoded, "raw_numeric_tags": raw_decoded}

    return {
        "present": bool(exif),
        "tag_count": len(exif),
        "tags": tags,
        "raw_numeric_tags": raw_tags,
        "ifds": ifds,
    }


def _marker_name(code: int) -> str:
    return JPEG_MARKER_NAMES.get(code, f"UNKNOWN_FF{code:02X}")


def _classify_app(marker: str, payload: bytes) -> str:
    prefix = payload[:200]
    low = prefix.lower()
    if marker == "APP0" and payload.startswith(b"JFIF"):
        return "JFIF"
    if marker == "APP0" and payload.startswith(b"JFXX"):
        return "JFXX"
    if marker == "APP1" and payload.startswith(b"Exif\x00\x00"):
        return "EXIF"
    if marker == "APP1" and (b"xmp" in low or b"adobe:ns:meta" in low):
        return "XMP"
    if marker == "APP2" and payload.startswith(b"ICC_PROFILE"):
        return "ICC_PROFILE"
    if marker == "APP13" and b"photoshop" in low:
        return "PHOTOSHOP_IPTC"
    if marker == "APP14" and payload.startswith(b"Adobe"):
        return "ADOBE_APP14"
    if marker == "COM":
        return "JPEG_COMMENT"
    return "OTHER"


def _parse_sof(payload: bytes) -> dict[str, Any]:
    if len(payload) < 6:
        return {"error": "SOF payload too short"}
    component_count = payload[5]
    components = []
    cursor = 6
    for _ in range(component_count):
        if cursor + 3 > len(payload):
            break
        component_id, sampling, quant_table = payload[cursor : cursor + 3]
        components.append(
            {
                "component_id": component_id,
                "horizontal_sampling_factor": sampling >> 4,
                "vertical_sampling_factor": sampling & 0x0F,
                "quantization_table_id": quant_table,
            }
        )
        cursor += 3
    return {
        "sample_precision_bits": payload[0],
        "height": int.from_bytes(payload[1:3], "big"),
        "width": int.from_bytes(payload[3:5], "big"),
        "component_count": component_count,
        "components": components,
    }


def _parse_dht(payload: bytes) -> dict[str, Any]:
    tables = []
    cursor = 0
    while cursor < len(payload):
        if cursor + 17 > len(payload):
            tables.append({"error": "truncated DHT table", "offset": cursor})
            break
        table_info = payload[cursor]
        cursor += 1
        counts = list(payload[cursor : cursor + 16])
        cursor += 16
        symbol_count = sum(counts)
        symbols = payload[cursor : cursor + symbol_count]
        cursor += len(symbols)
        tables.append(
            {
                "table_class": "AC" if table_info >> 4 else "DC",
                "table_id": table_info & 0x0F,
                "code_length_counts_1_to_16": counts,
                "symbol_count": symbol_count,
                "symbols": _json_value(symbols),
            }
        )
        if len(symbols) != symbol_count:
            tables[-1]["error"] = "truncated DHT symbols"
            break
    return {"table_count": len(tables), "tables": tables}


def _parse_dqt(payload: bytes) -> dict[str, Any]:
    tables = []
    cursor = 0
    while cursor < len(payload):
        info = payload[cursor]
        cursor += 1
        precision_bits = 16 if info >> 4 else 8
        table_id = info & 0x0F
        byte_count = 128 if precision_bits == 16 else 64
        raw_values = payload[cursor : cursor + byte_count]
        cursor += len(raw_values)
        if precision_bits == 16:
            values = [int.from_bytes(raw_values[i : i + 2], "big") for i in range(0, len(raw_values), 2)]
        else:
            values = list(raw_values)
        tables.append(
            {
                "table_id": table_id,
                "precision_bits": precision_bits,
                "value_count": len(values),
                "values_zigzag_order": values,
                "raw": _json_value(raw_values),
            }
        )
        if len(raw_values) != byte_count:
            tables[-1]["error"] = "truncated DQT table"
            break
    return {"table_count": len(tables), "tables": tables}


def _parse_sos(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {"error": "empty SOS payload"}
    component_count = payload[0]
    components = []
    cursor = 1
    for _ in range(component_count):
        if cursor + 2 > len(payload):
            break
        component_id = payload[cursor]
        selectors = payload[cursor + 1]
        components.append(
            {
                "component_id": component_id,
                "dc_huffman_table_id": selectors >> 4,
                "ac_huffman_table_id": selectors & 0x0F,
            }
        )
        cursor += 2
    spectral_start = payload[cursor] if cursor < len(payload) else None
    spectral_end = payload[cursor + 1] if cursor + 1 < len(payload) else None
    approx = payload[cursor + 2] if cursor + 2 < len(payload) else None
    return {
        "component_count": component_count,
        "components": components,
        "spectral_selection_start": spectral_start,
        "spectral_selection_end": spectral_end,
        "successive_approximation_high": (approx >> 4) if approx is not None else None,
        "successive_approximation_low": (approx & 0x0F) if approx is not None else None,
    }


def _parse_jpeg_markers(raw: bytes) -> dict[str, Any]:
    if len(raw) < 2 or raw[:2] != b"\xff\xd8":
        return {"is_jpeg": False, "markers": [], "marker_counts": {}}

    markers: list[dict[str, Any]] = []
    marker_counts: Counter[str] = Counter()
    restart_counts: Counter[str] = Counter()
    frame_markers: list[str] = []
    pos = 0

    while pos < len(raw):
        marker_start = raw.find(b"\xff", pos)
        if marker_start < 0 or marker_start + 1 >= len(raw):
            break
        cursor = marker_start + 1
        while cursor < len(raw) and raw[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(raw):
            break
        code = raw[cursor]
        if code == 0x00:
            pos = cursor + 1
            continue

        name = _marker_name(code)
        marker_counts[name] += 1
        entry: dict[str, Any] = {
            "index": len(markers),
            "name": name,
            "code_hex": f"FF{code:02X}",
            "offset": marker_start,
        }

        pos = cursor + 1
        if code in JPEG_STANDALONE_MARKERS:
            entry["standalone"] = True
            markers.append(entry)
            if code == 0xD9:
                break
            continue

        if pos + 2 > len(raw):
            entry["error"] = "missing segment length"
            markers.append(entry)
            break

        declared_length = int.from_bytes(raw[pos : pos + 2], "big")
        if declared_length < 2:
            entry["declared_length"] = declared_length
            entry["error"] = "invalid segment length"
            markers.append(entry)
            break

        payload_start = pos + 2
        payload_end = min(len(raw), pos + declared_length)
        payload = raw[payload_start:payload_end]
        entry.update(
            {
                "declared_length_including_length_field": declared_length,
                "payload_offset": payload_start,
                "payload_length": len(payload),
                "payload": _json_value(payload),
            }
        )

        if name.startswith("APP") or name == "COM":
            entry["kind"] = _classify_app(name, payload)
            entry["ascii_identifier_preview"] = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in payload[:120])
        if code in SOF_MARKERS:
            entry["frame"] = _parse_sof(payload)
            frame_markers.append(name)
        elif code == 0xC4:
            entry["huffman_tables"] = _parse_dht(payload)
        elif code == 0xDB:
            entry["quantization_tables"] = _parse_dqt(payload)
        elif code == 0xDA:
            entry["scan_header"] = _parse_sos(payload)
        elif code == 0xDD and len(payload) >= 2:
            entry["restart_interval_mcu"] = int.from_bytes(payload[:2], "big")
        elif code == 0xFE:
            for encoding in ("utf-8", "latin-1"):
                try:
                    entry["comment_text"] = payload.decode(encoding)
                    entry["comment_encoding"] = encoding
                    break
                except Exception:
                    pass

        pos = payload_end

        if code == 0xDA:
            entropy_start = pos
            scan_cursor = pos
            restart_offsets = []
            while scan_cursor < len(raw):
                ff = raw.find(b"\xff", scan_cursor)
                if ff < 0 or ff + 1 >= len(raw):
                    scan_cursor = len(raw)
                    break
                next_cursor = ff + 1
                while next_cursor < len(raw) and raw[next_cursor] == 0xFF:
                    next_cursor += 1
                if next_cursor >= len(raw):
                    scan_cursor = len(raw)
                    break
                next_code = raw[next_cursor]
                if next_code == 0x00:
                    scan_cursor = next_cursor + 1
                    continue
                if 0xD0 <= next_code <= 0xD7:
                    restart_name = _marker_name(next_code)
                    restart_counts[restart_name] += 1
                    restart_offsets.append({"name": restart_name, "offset": ff})
                    scan_cursor = next_cursor + 1
                    continue
                entropy = raw[entropy_start:ff]
                entry["entropy_coded_scan"] = {
                    "offset": entropy_start,
                    "length": len(entropy),
                    "sha256": _sha256_bytes(entropy),
                    "restart_markers": restart_offsets,
                    "note": "Entropy-coded pixel data is hashed but not duplicated as base64 in the metadata JSON.",
                }
                pos = ff
                break
            else:
                entropy = raw[entropy_start:]
                entry["entropy_coded_scan"] = {
                    "offset": entropy_start,
                    "length": len(entropy),
                    "sha256": _sha256_bytes(entropy),
                    "restart_markers": restart_offsets,
                    "note": "Entropy-coded pixel data is hashed but not duplicated as base64 in the metadata JSON.",
                }
                pos = len(raw)

        markers.append(entry)

    progressive = any(name in {_marker_name(code) for code in PROGRESSIVE_SOF_MARKERS} for name in frame_markers)
    scan_count = marker_counts.get("SOS", 0)
    return {
        "is_jpeg": True,
        "file_length": len(raw),
        "marker_count": len(markers),
        "marker_order": [item["name"] for item in markers],
        "marker_counts": dict(sorted(marker_counts.items())),
        "restart_marker_counts_inside_scans": dict(sorted(restart_counts.items())),
        "frame_markers": frame_markers,
        "progressive": progressive,
        "scan_count": scan_count,
        "comment_segment_count": marker_counts.get("COM", 0),
        "dht_segment_count": marker_counts.get("DHT", 0),
        "sof_segment_count": sum(marker_counts.get(_marker_name(code), 0) for code in SOF_MARKERS),
        "sos_segment_count": scan_count,
        "markers": markers,
    }


def _icc_description(icc_bytes: bytes | None) -> str:
    if not icc_bytes or ImageCms is None:
        return ""
    try:
        import io

        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        inner = getattr(profile, "profile", None)
        description = getattr(inner, "profile_description", None) if inner is not None else None
        return str(description or "")
    except Exception:
        return ""


def _normalized_rgb(image: Image.Image) -> Image.Image:
    return ImageOps.exif_transpose(image).convert("RGB")


def _pixel_fingerprints(image: Image.Image) -> dict[str, Any]:
    normalized = _normalized_rgb(image)
    pixel_bytes = normalized.tobytes()
    phash = imagehash.phash(normalized)
    dhash = imagehash.dhash(normalized)
    whash = imagehash.whash(normalized)
    return {
        "normalization": "ImageOps.exif_transpose then convert to RGB; no resize",
        "mode": normalized.mode,
        "width": normalized.width,
        "height": normalized.height,
        "decoded_pixel_byte_count": len(pixel_bytes),
        "sha256_decoded_pixels": _sha256_bytes(pixel_bytes),
        "perceptual_hashes": {
            "phash": str(phash),
            "dhash": str(dhash),
            "whash": str(whash),
            "hash_size": 8,
            "bits": 64,
        },
    }


def _dominant_colors(image: Image.Image, count: int = 8) -> list[dict[str, Any]]:
    sample = image.copy()
    sample.thumbnail((256, 256), Image.Resampling.LANCZOS)
    quantized = sample.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    colors = quantized.getcolors(maxcolors=count * 4) or []
    total = sum(pixel_count for pixel_count, _ in colors) or 1
    output = []
    for pixel_count, palette_index in sorted(colors, reverse=True):
        base = int(palette_index) * 3
        rgb = palette[base : base + 3]
        output.append(
            {
                "rgb": rgb,
                "count": pixel_count,
                "percentage": pixel_count * 100.0 / total,
            }
        )
    return output


def _visual_characteristics(image: Image.Image) -> dict[str, Any]:
    normalized = _normalized_rgb(image)
    array = np.asarray(normalized, dtype=np.float32)
    channel_names = ("R", "G", "B")
    mean_rgb = array.mean(axis=(0, 1))
    std_rgb = array.std(axis=(0, 1))
    min_rgb = array.min(axis=(0, 1))
    max_rgb = array.max(axis=(0, 1))

    luminance = 0.2126 * array[:, :, 0] + 0.7152 * array[:, :, 1] + 0.0722 * array[:, :, 2]
    max_channel = array.max(axis=2)
    min_channel = array.min(axis=2)
    saturation = np.where(max_channel > 0, (max_channel - min_channel) / max_channel, 0.0)

    gray_u8 = np.clip(np.rint(luminance), 0, 255).astype(np.uint8)
    gray_hist = np.bincount(gray_u8.ravel(), minlength=256).astype(np.int64)
    probabilities = gray_hist / max(int(gray_hist.sum()), 1)
    nonzero = probabilities[probabilities > 0]
    entropy_bits = float(-(nonzero * np.log2(nonzero)).sum())

    if luminance.shape[0] >= 3 and luminance.shape[1] >= 3:
        center = luminance[1:-1, 1:-1]
        laplacian = (
            luminance[:-2, 1:-1]
            + luminance[2:, 1:-1]
            + luminance[1:-1, :-2]
            + luminance[1:-1, 2:]
            - 4.0 * center
        )
        sharpness_variance_laplacian = float(laplacian.var())
    else:
        sharpness_variance_laplacian = 0.0

    gradient_x = np.diff(luminance, axis=1)
    gradient_y = np.diff(luminance, axis=0)
    shared_h = min(gradient_x.shape[0], gradient_y.shape[0])
    shared_w = min(gradient_x.shape[1], gradient_y.shape[1])
    gradient_magnitude = np.sqrt(
        gradient_x[:shared_h, :shared_w] ** 2 + gradient_y[:shared_h, :shared_w] ** 2
    )

    rgb_histograms: dict[str, list[int]] = {}
    for index, channel in enumerate(channel_names):
        rgb_histograms[channel] = np.bincount(array[:, :, index].astype(np.uint8).ravel(), minlength=256).astype(int).tolist()

    return {
        "width": normalized.width,
        "height": normalized.height,
        "aspect_ratio": normalized.width / normalized.height if normalized.height else None,
        "pixel_count": normalized.width * normalized.height,
        "mean_rgb": {channel: float(mean_rgb[index]) for index, channel in enumerate(channel_names)},
        "std_rgb": {channel: float(std_rgb[index]) for index, channel in enumerate(channel_names)},
        "min_rgb": {channel: float(min_rgb[index]) for index, channel in enumerate(channel_names)},
        "max_rgb": {channel: float(max_rgb[index]) for index, channel in enumerate(channel_names)},
        "luminance": {
            "mean": float(luminance.mean()),
            "std": float(luminance.std()),
            "min": float(luminance.min()),
            "max": float(luminance.max()),
            "percentiles": {
                str(percentile): float(np.percentile(luminance, percentile))
                for percentile in (1, 5, 25, 50, 75, 95, 99)
            },
            "histogram_256": gray_hist.astype(int).tolist(),
            "entropy_bits": entropy_bits,
        },
        "saturation": {
            "mean": float(saturation.mean()),
            "std": float(saturation.std()),
            "percentiles": {
                str(percentile): float(np.percentile(saturation, percentile))
                for percentile in (1, 5, 25, 50, 75, 95, 99)
            },
        },
        "sharpness_variance_laplacian": sharpness_variance_laplacian,
        "edge_density_gradient_over_20": float((gradient_magnitude > 20.0).mean()) if gradient_magnitude.size else 0.0,
        "rgb_histograms_256": rgb_histograms,
        "dominant_colors": _dominant_colors(normalized),
    }


def _find_exiftool(explicit_path: str | None = None) -> str | None:
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.exists():
            return str(candidate)
        return shutil.which(explicit_path)
    for name in ("exiftool", "exiftool.exe"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        Path("C:/Windows/exiftool.exe"),
        Path("C:/Program Files/ExifTool/exiftool.exe"),
        Path("C:/Program Files (x86)/ExifTool/exiftool.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _read_exiftool_metadata(path: Path, *, exiftool_path: str | None = None) -> dict[str, Any]:
    executable = _find_exiftool(exiftool_path)
    if not executable:
        return {"available": False, "error": "exiftool_not_found", "tags": {}}
    cmd = [executable, "-j", "-G1", "-a", "-s", "-ee", "-u", "-validate", str(path)]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=90,
    )
    try:
        parsed = json.loads(proc.stdout or "[]")
        tags = parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) else {}
    except Exception as exc:
        tags = {"parse_error": str(exc)}
    return {
        "available": True,
        "executable": executable,
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
        "tags": _json_value(tags),
    }


def read_metadata(path: Path, *, use_exiftool: bool = False, exiftool_path: str | None = None) -> dict[str, Any]:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)

    raw_file = path.read_bytes()
    file_metadata = _read_file_system_metadata(path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(path) as image:
            info = dict(image.info)
            icc_bytes = info.get("icc_profile") if isinstance(info.get("icc_profile"), bytes) else None
            exif_bytes = info.get("exif") if isinstance(info.get("exif"), bytes) else None
            xmp_value = info.get("xmp")
            photoshop_value = info.get("photoshop")

            metadata: dict[str, Any] = {
                "file_system": file_metadata,
                "image": {
                    "format": image.format,
                    "mime": image.get_format_mimetype() if hasattr(image, "get_format_mimetype") else "",
                    "mode": image.mode,
                    "bands": list(image.getbands()),
                    "width": image.width,
                    "height": image.height,
                    "is_animated": bool(getattr(image, "is_animated", False)),
                    "n_frames": int(getattr(image, "n_frames", 1)),
                },
                "pixel_fingerprints": _pixel_fingerprints(image),
                "visual_characteristics": _visual_characteristics(image),
                "container": {
                    "info_keys": sorted(str(key) for key in info.keys()),
                    "pillow_info_full": _json_value(info),
                    "exif_block": _json_value(exif_bytes) if exif_bytes else None,
                    "icc_profile_block": _json_value(icc_bytes) if icc_bytes else None,
                    "xmp_block": _json_value(xmp_value) if xmp_value is not None else None,
                    "photoshop_block": _json_value(photoshop_value) if photoshop_value is not None else None,
                },
                "exif": _read_exif(image),
                "jpeg": {
                    "pillow_quantization_tables": {
                        str(key): list(values) for key, values in (getattr(image, "quantization", None) or {}).items()
                    },
                    "full_marker_inventory": _parse_jpeg_markers(raw_file),
                },
                "icc": {
                    "present": bool(icc_bytes),
                    "description": _icc_description(icc_bytes),
                },
                "xmp": {"present": xmp_value is not None},
                "photoshop_iptc": {"present": photoshop_value is not None},
            }

    metadata["exiftool"] = (
        _read_exiftool_metadata(path, exiftool_path=exiftool_path)
        if use_exiftool
        else {"available": False, "not_requested": True, "tags": {}}
    )
    return metadata


def _histogram_similarity(original: dict[str, Any], filtered: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    intersections = []
    correlations = []
    for channel in ("R", "G", "B"):
        first = np.asarray(original.get(channel, []), dtype=np.float64)
        second = np.asarray(filtered.get(channel, []), dtype=np.float64)
        if first.size != 256 or second.size != 256:
            continue
        first /= max(first.sum(), 1.0)
        second /= max(second.sum(), 1.0)
        intersection = float(np.minimum(first, second).sum())
        correlation = float(np.corrcoef(first, second)[0, 1]) if first.std() and second.std() else 0.0
        output[channel] = {"intersection": intersection, "correlation": correlation}
        intersections.append(intersection)
        correlations.append(correlation)
    output["mean_intersection"] = float(np.mean(intersections)) if intersections else None
    output["mean_correlation"] = float(np.mean(correlations)) if correlations else None
    return output


def compare_images(
    original_path: Path,
    filtered_path: Path,
    original_metadata: dict[str, Any],
    filtered_metadata: dict[str, Any],
) -> dict[str, Any]:
    with Image.open(original_path) as original_image, Image.open(filtered_path) as filtered_image:
        original_rgb = _normalized_rgb(original_image)
        filtered_rgb = _normalized_rgb(filtered_image)
        same_dimensions = original_rgb.size == filtered_rgb.size
        filtered_for_comparison = filtered_rgb
        resized_for_comparison = False
        if not same_dimensions:
            filtered_for_comparison = filtered_rgb.resize(original_rgb.size, Image.Resampling.LANCZOS)
            resized_for_comparison = True

        first = np.asarray(original_rgb, dtype=np.float64)
        second = np.asarray(filtered_for_comparison, dtype=np.float64)
        difference = first - second
        absolute = np.abs(difference)
        mse = float(np.mean(difference**2))
        rmse = math.sqrt(mse)
        psnr = float("inf") if mse == 0 else 20.0 * math.log10(255.0 / rmse)

        ssim_payload: dict[str, Any]
        if structural_similarity is None:
            ssim_payload = {"available": False, "error": "scikit-image_not_installed"}
        else:
            try:
                score = structural_similarity(
                    first.astype(np.uint8),
                    second.astype(np.uint8),
                    channel_axis=2,
                    data_range=255,
                )
                ssim_payload = {
                    "available": True,
                    "score": float(score),
                    "data_range": 255,
                    "channel_axis": 2,
                    "resized_filtered_to_original": resized_for_comparison,
                }
            except Exception as exc:
                ssim_payload = {"available": True, "error": str(exc)}

    original_hashes = original_metadata["pixel_fingerprints"]["perceptual_hashes"]
    filtered_hashes = filtered_metadata["pixel_fingerprints"]["perceptual_hashes"]
    hash_distances = {
        name: int(imagehash.hex_to_hash(original_hashes[name]) - imagehash.hex_to_hash(filtered_hashes[name]))
        for name in ("phash", "dhash", "whash")
    }

    original_visual = original_metadata["visual_characteristics"]
    filtered_visual = filtered_metadata["visual_characteristics"]
    mean_color_delta = {
        channel: float(filtered_visual["mean_rgb"][channel] - original_visual["mean_rgb"][channel])
        for channel in ("R", "G", "B")
    }

    return {
        "file_sha256_equal": (
            original_metadata["file_system"]["sha256_complete_file"]
            == filtered_metadata["file_system"]["sha256_complete_file"]
        ),
        "decoded_pixel_sha256_equal": (
            original_metadata["pixel_fingerprints"]["sha256_decoded_pixels"]
            == filtered_metadata["pixel_fingerprints"]["sha256_decoded_pixels"]
        ),
        "dimensions_equal": same_dimensions,
        "original_dimensions": list(original_rgb.size),
        "filtered_dimensions": list(filtered_rgb.size),
        "resized_filtered_to_original_for_numeric_comparison": resized_for_comparison,
        "aspect_ratio_delta": float(filtered_visual["aspect_ratio"] - original_visual["aspect_ratio"]),
        "perceptual_hash_hamming_distances_64_bits": hash_distances,
        "ssim": ssim_payload,
        "pixel_error": {
            "mae_0_to_255": float(absolute.mean()),
            "mae_normalized_0_to_1": float(absolute.mean() / 255.0),
            "mse": mse,
            "rmse": rmse,
            "psnr_db": psnr,
            "max_absolute_error": float(absolute.max()),
            "exactly_equal_pixel_component_ratio": float((first == second).mean()),
            "per_channel_mae": {
                channel: float(absolute[:, :, index].mean())
                for index, channel in enumerate(("R", "G", "B"))
            },
        },
        "visual_characteristic_deltas_filtered_minus_original": {
            "mean_rgb": mean_color_delta,
            "mean_luminance": float(filtered_visual["luminance"]["mean"] - original_visual["luminance"]["mean"]),
            "std_luminance": float(filtered_visual["luminance"]["std"] - original_visual["luminance"]["std"]),
            "mean_saturation": float(filtered_visual["saturation"]["mean"] - original_visual["saturation"]["mean"]),
            "sharpness_variance_laplacian": float(
                filtered_visual["sharpness_variance_laplacian"]
                - original_visual["sharpness_variance_laplacian"]
            ),
            "edge_density": float(
                filtered_visual["edge_density_gradient_over_20"]
                - original_visual["edge_density_gradient_over_20"]
            ),
        },
        "rgb_histogram_similarity": _histogram_similarity(
            original_visual["rgb_histograms_256"],
            filtered_visual["rgb_histograms_256"],
        ),
    }


def _first_value(metadata: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = metadata
        found = True
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found and current not in (None, "", {}, []):
            return current
    return ""


def _display_value(value: Any) -> str:
    if isinstance(value, dict) and value.get("type") == "rational":
        return str(value.get("text") or "")
    if isinstance(value, list):
        return ", ".join(_display_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def build_terminal_summary(metadata: dict[str, Any]) -> list[tuple[str, Any]]:
    exif_tags = metadata.get("exif", {}).get("tags", {})
    exif_ifds = metadata.get("exif", {}).get("ifds", {})
    exif_detail = exif_ifds.get("Exif", {}).get("tags", {}) if isinstance(exif_ifds, dict) else {}
    make = exif_tags.get("Make", "")
    model = exif_tags.get("Model", "")
    device = " ".join(str(value) for value in (make, model) if value).strip()
    markers = metadata.get("jpeg", {}).get("full_marker_inventory", {})
    hashes = metadata.get("pixel_fingerprints", {}).get("perceptual_hashes", {})
    file_system = metadata.get("file_system", {})
    return [
        ("Format", metadata.get("image", {}).get("format", "")),
        ("Dimensions", f"{metadata.get('image', {}).get('width', '')} × {metadata.get('image', {}).get('height', '')}"),
        ("Taille du fichier", f"{file_system.get('size_bytes', '')} octets"),
        ("SHA-256 fichier", file_system.get("sha256_complete_file", "")),
        ("SHA-256 pixels décodés", metadata.get("pixel_fingerprints", {}).get("sha256_decoded_pixels", "")),
        ("pHash", hashes.get("phash", "")),
        ("dHash", hashes.get("dhash", "")),
        ("wHash", hashes.get("whash", "")),
        ("Nom Windows", file_system.get("name", "")),
        ("Création Windows", _first_value(metadata, "file_system.windows_creation_time.local_iso") or "indisponible"),
        ("Dernière modification", _first_value(metadata, "file_system.modified_time.local_iso") or "indisponible"),
        ("Dernier accès", _first_value(metadata, "file_system.last_access_time.local_iso") or "indisponible"),
        ("Appareil", device or "absent"),
        ("Objectif", exif_detail.get("LensModel") or exif_tags.get("LensModel") or "absent"),
        ("Logiciel", exif_tags.get("Software") or "absent"),
        ("Date de prise", exif_detail.get("DateTimeOriginal") or exif_tags.get("DateTimeOriginal") or "absente"),
        ("EXIF", "présent" if metadata.get("exif", {}).get("present") else "absent"),
        ("XMP", "présent" if metadata.get("xmp", {}).get("present") else "absent"),
        ("Profil ICC", metadata.get("icc", {}).get("description") or ("présent" if metadata.get("icc", {}).get("present") else "absent")),
        ("Photoshop/IPTC", "présent" if metadata.get("photoshop_iptc", {}).get("present") else "absent"),
        ("Mode JPEG", "progressif" if markers.get("progressive") else "séquentiel/baseline"),
        ("Marqueurs JPEG", ", ".join(markers.get("marker_order", [])) or "aucun"),
        ("DHT / SOF / SOS / COM", f"{markers.get('dht_segment_count', 0)} / {markers.get('sof_segment_count', 0)} / {markers.get('sos_segment_count', 0)} / {markers.get('comment_segment_count', 0)}"),
        ("ExifTool", "disponible" if metadata.get("exiftool", {}).get("available") else "non disponible"),
    ]


def build_comparison_terminal_summary(comparison: dict[str, Any]) -> list[tuple[str, Any]]:
    ssim = comparison.get("ssim", {})
    errors = comparison.get("pixel_error", {})
    hashes = comparison.get("perceptual_hash_hamming_distances_64_bits", {})
    histogram = comparison.get("rgb_histogram_similarity", {})
    return [
        ("Dimensions identiques", comparison.get("dimensions_equal")),
        ("SHA fichier identique", comparison.get("file_sha256_equal")),
        ("SHA pixels identique", comparison.get("decoded_pixel_sha256_equal")),
        ("SSIM", ssim.get("score") if ssim.get("available") else ssim.get("error", "indisponible")),
        ("Distance pHash / 64", hashes.get("phash")),
        ("Distance dHash / 64", hashes.get("dhash")),
        ("Distance wHash / 64", hashes.get("whash")),
        ("MAE pixels", errors.get("mae_0_to_255")),
        ("RMSE pixels", errors.get("rmse")),
        ("PSNR dB", errors.get("psnr_db")),
        ("Intersection histogrammes RGB", histogram.get("mean_intersection")),
        ("Corrélation histogrammes RGB", histogram.get("mean_correlation")),
    ]


def _load_export_pairs(export_dir: Path) -> list[tuple[Path, Path]]:
    selected = Path(export_dir) / "selected_filter.json"
    if not selected.exists():
        raise RuntimeError(f"selected_filter.json introuvable: {selected}")
    payload = json.loads(selected.read_text(encoding="utf-8"))
    sources = payload.get("source_images") or []
    outputs = payload.get("output_paths") or []
    pairs: list[tuple[Path, Path]] = []
    for source, output in zip(sources, outputs):
        if isinstance(source, dict):
            pairs.append((Path(str(source.get("source_path") or "")), Path(str(output))))
    if not pairs:
        raise RuntimeError("Aucune paire originale/filtrée trouvée dans selected_filter.json")
    return pairs


def print_metadata_block(title: str, path: Path, metadata: dict[str, Any]) -> None:
    print(title)
    print(f"- Fichier : {path}")
    for label, value in build_terminal_summary(metadata):
        print(f"- {label} : {_display_value(value)}")


def write_json_report(pairs_report: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 3,
        "binary_encoding": "base64",
        "pixel_hash_normalization": "EXIF orientation applied, then RGB conversion, without resizing",
        "note": (
            "All metadata binary blocks and JPEG marker payloads are preserved in full as base64. "
            "Entropy-coded JPEG scan data is not duplicated in base64; its offset, length and SHA-256 are recorded."
        ),
        "pairs": pairs_report,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Affiche un résumé lisible et écrit un audit image complet dans un JSON."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export-dir", default=None, help="Dossier d'export contenant selected_filter.json")
    group.add_argument("--original", default=None, help="Photo originale")
    parser.add_argument("--filtered", default=None, help="Photo filtrée si --original est utilisé")
    parser.add_argument("--use-exiftool", action="store_true", help="Ajoute les tags ExifTool si ExifTool est installé")
    parser.add_argument("--exiftool-path", default=None, help="Chemin vers exiftool.exe si besoin")
    parser.add_argument("--output-json", default=None, help="Chemin du rapport JSON détaillé")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.original:
        if not args.filtered:
            raise SystemExit("--filtered est requis avec --original")
        pairs = [(Path(args.original), Path(args.filtered))]
        default_root = Path(args.filtered).parent
    else:
        default_root = Path(args.export_dir)
        pairs = _load_export_pairs(default_root)

    report: list[dict[str, Any]] = []
    for index, (original, filtered) in enumerate(pairs, start=1):
        original_meta = read_metadata(original, use_exiftool=args.use_exiftool, exiftool_path=args.exiftool_path)
        filtered_meta = read_metadata(filtered, use_exiftool=args.use_exiftool, exiftool_path=args.exiftool_path)
        comparison = compare_images(original, filtered, original_meta, filtered_meta)

        print("")
        print(f"==================== PAIRE {index} ====================")
        print_metadata_block("Photo originale :", original, original_meta)
        print("")
        print_metadata_block("Photo filtrée :", filtered, filtered_meta)
        print("")
        print("Comparaison visuelle :")
        for label, value in build_comparison_terminal_summary(comparison):
            print(f"- {label} : {_display_value(value)}")

        report.append(
            {
                "pair_index": index,
                "original": {
                    "path": str(original),
                    "summary": {label: _json_value(value) for label, value in build_terminal_summary(original_meta)},
                    "audit_full": original_meta,
                },
                "filtered": {
                    "path": str(filtered),
                    "summary": {label: _json_value(value) for label, value in build_terminal_summary(filtered_meta)},
                    "audit_full": filtered_meta,
                },
                "comparison": comparison,
            }
        )

    output_json = Path(args.output_json) if args.output_json else default_root / "image_audit_original_filtered_full.json"
    write_json_report(report, output_json)
    print("")
    print(f"JSON détaillé : {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
