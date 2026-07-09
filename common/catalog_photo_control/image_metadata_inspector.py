from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps

try:  # optional but installed through requirements for richer EXIF group parsing
    import piexif  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    piexif = None  # type: ignore

try:  # Pillow exposes JPEG sampling details through this module.
    from PIL import JpegImagePlugin
except Exception:  # pragma: no cover - Pillow always provides this for JPEG builds.
    JpegImagePlugin = None  # type: ignore

try:  # Pillow can decode ICC profile headers/descriptions when LittleCMS is available.
    from PIL import ImageCms
except Exception:  # pragma: no cover - depends on Pillow build.
    ImageCms = None  # type: ignore


EXIF_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.TAGS.items()}
GPS_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.GPSTAGS.items()}

SENSITIVE_TAG_NAMES = {
    "Make",
    "Model",
    "Software",
    "DateTime",
    "DateTimeOriginal",
    "DateTimeDigitized",
    "OffsetTime",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "GPSInfo",
    "ImageUniqueID",
    "BodySerialNumber",
    "CameraOwnerName",
    "LensModel",
    "LensMake",
    "LensSerialNumber",
    "UserComment",
    "HostComputer",
    "ProcessingSoftware",
    "Artist",
    "Copyright",
    "CreatorTool",
    "History",
    "SerialNumber",
    "OwnerName",
    "DocumentID",
    "InstanceID",
    "OriginalDocumentID",
    "DerivedFrom",
}

JPEG_MARKER_NAMES = {
    0x01: "TEM",
    0xC0: "SOF0_baseline",
    0xC1: "SOF1_extended_sequential",
    0xC2: "SOF2_progressive",
    0xC3: "SOF3_lossless",
    0xC4: "DHT",
    0xC5: "SOF5_differential_sequential",
    0xC6: "SOF6_differential_progressive",
    0xC7: "SOF7_differential_lossless",
    0xC8: "JPG",
    0xC9: "SOF9_arithmetic_sequential",
    0xCA: "SOF10_arithmetic_progressive",
    0xCB: "SOF11_arithmetic_lossless",
    0xCC: "DAC",
    0xCD: "SOF13_differential_arithmetic_sequential",
    0xCE: "SOF14_differential_arithmetic_progressive",
    0xCF: "SOF15_differential_arithmetic_lossless",
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

EXIFTOOL_ARGS = [
    "-j",
    "-G1",
    "-a",
    "-s",
    "-ee",
    "-u",
    "-api",
    "LargeFileSupport=1",
    "-validate",
]

VOLATILE_COMPARE_KEYS = {
    "path",
    "file.mtime_epoch",
    "exiftool.command",
    "exiftool.stderr",
    "exiftool.stdout_raw_len",
}

PATH_LIKE_COMPARE_SUFFIXES = (
    ".SourceFile",
    ".File:Directory",
    ".File:FileName",
    ".System:Directory",
    ".System:FileName",
)

XMP_PACKET_RE = re.compile(rb"(<\?xpacket[\s\S]{0,500000}?xpacket end=['\"][rw]['\"]\?>|<x:xmpmeta[\s\S]{0,500000}?</x:xmpmeta>)")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _short(value: Any, limit: int = 500) -> Any:
    if isinstance(value, bytes):
        return {"kind": "bytes", "length": len(value), "sha256": _sha256_bytes(value)}
    if isinstance(value, bytearray):
        data = bytes(value)
        return {"kind": "bytes", "length": len(data), "sha256": _sha256_bytes(data)}
    if isinstance(value, tuple):
        return [_short(v, limit=limit) for v in value]
    if isinstance(value, list):
        return [_short(v, limit=limit) for v in value[:100]]
    if isinstance(value, dict):
        return {str(k): _short(v, limit=limit) for k, v in value.items()}
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _decode_bytes_best_effort(value: bytes, *, limit: int = 2000) -> dict[str, Any]:
    for encoding in ("utf-8", "utf-16", "latin-1", "ascii"):
        try:
            decoded = value.decode(encoding).strip("\x00")
            if decoded:
                return {
                    "kind": "text",
                    "encoding": encoding,
                    "length": len(value),
                    "sha256": _sha256_bytes(value),
                    "preview": decoded[:limit],
                }
        except Exception:
            continue
    return {"kind": "bytes", "length": len(value), "sha256": _sha256_bytes(value)}


def _decode_exif_value(value: Any) -> Any:
    try:
        if isinstance(value, bytes):
            return _decode_bytes_best_effort(value, limit=600)
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            return f"{value.numerator}/{value.denominator}"
        if isinstance(value, tuple):
            return [_decode_exif_value(v) for v in value]
        if isinstance(value, list):
            return [_decode_exif_value(v) for v in value]
        return value
    except Exception:
        return _short(value)


def _tag_name(tag_id: int, *, gps: bool = False) -> str:
    if gps:
        return GPS_TAGS_BY_ID.get(int(tag_id), str(tag_id))
    return EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))


def _extract_pillow_exif(image: Image.Image) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        exif = image.getexif()
    except Exception:
        return out

    for tag_id, value in exif.items():
        name = _tag_name(int(tag_id))
        if name == "GPSInfo":
            # Expanded below when possible.
            continue
        out[name] = _decode_exif_value(value)

    ifd_enum = getattr(ExifTags, "IFD", None)
    if ifd_enum is not None:
        for ifd_name in ("GPSInfo", "Exif", "Interop", "IFD1", "MakerNote"):
            ifd_id = getattr(ifd_enum, ifd_name, None)
            if ifd_id is None:
                continue
            try:
                ifd = exif.get_ifd(ifd_id)
            except Exception:
                continue
            if not ifd:
                continue
            payload: dict[str, Any] = {}
            for tag_id, value in ifd.items():
                payload[_tag_name(int(tag_id), gps=(ifd_name == "GPSInfo"))] = _decode_exif_value(value)
            out[ifd_name] = payload

    return out


def _extract_pillow_exif_raw_index(image: Image.Image) -> dict[str, Any]:
    """Keep numeric tags too, so unknown/private tags are still visible."""
    try:
        exif = image.getexif()
    except Exception:
        return {}
    payload: dict[str, Any] = {
        "tag_count": len(exif),
        "raw_tags": {},
        "ifds": {},
    }
    for tag_id, value in exif.items():
        payload["raw_tags"][str(int(tag_id))] = {
            "name": _tag_name(int(tag_id)),
            "value": _decode_exif_value(value),
        }

    ifd_enum = getattr(ExifTags, "IFD", None)
    if ifd_enum is not None:
        for ifd_name in ("GPSInfo", "Exif", "Interop", "IFD1", "MakerNote"):
            ifd_id = getattr(ifd_enum, ifd_name, None)
            if ifd_id is None:
                continue
            try:
                ifd = exif.get_ifd(ifd_id)
            except Exception:
                continue
            if not ifd:
                continue
            payload["ifds"][ifd_name] = {
                str(int(tag_id)): {
                    "name": _tag_name(int(tag_id), gps=(ifd_name == "GPSInfo")),
                    "value": _decode_exif_value(value),
                }
                for tag_id, value in ifd.items()
            }
    return payload


def _extract_piexif_groups(exif_bytes: bytes | None) -> dict[str, Any]:
    if not exif_bytes or piexif is None:
        return {}
    try:
        loaded = piexif.load(exif_bytes)
    except Exception as exc:
        return {"_error": str(exc)}

    groups: dict[str, Any] = {}
    for ifd_name, tags in loaded.items():
        if ifd_name == "thumbnail":
            groups[ifd_name] = {
                "present": bool(tags),
                "length": len(tags) if isinstance(tags, (bytes, bytearray)) else 0,
                "sha256": _sha256_bytes(bytes(tags)) if isinstance(tags, (bytes, bytearray)) and tags else "",
            }
            continue
        if not isinstance(tags, dict):
            continue
        group_payload: dict[str, Any] = {}
        for tag_id, value in tags.items():
            try:
                tag_name = piexif.TAGS.get(ifd_name, {}).get(tag_id, {}).get("name", str(tag_id))
            except Exception:
                tag_name = str(tag_id)
            group_payload[str(tag_name)] = _decode_exif_value(value)
        groups[str(ifd_name)] = group_payload
    return groups


def _extract_image_info_payload(info: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in info.items():
        payload[str(key)] = _short(value, limit=2000)
    return payload


def _extract_xmp_like_info(info: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in info.items():
        norm = str(key).lower()
        if "xmp" in norm or "xml" in norm or "iptc" in norm:
            if isinstance(value, bytes):
                payload[str(key)] = _decode_bytes_best_effort(value, limit=4000)
            else:
                payload[str(key)] = _short(value, limit=4000)
    return payload


def _extract_xmp_packets(raw: bytes, image: Image.Image) -> dict[str, Any]:
    packets = []
    for idx, match in enumerate(XMP_PACKET_RE.finditer(raw), start=1):
        packet = match.group(1)
        decoded = _decode_bytes_best_effort(packet, limit=8000)
        root_tag = ""
        namespaces: list[str] = []
        if decoded.get("kind") == "text":
            text = str(decoded.get("preview") or "")
            try:
                root = ET.fromstring(text.encode("utf-8"))
                root_tag = str(root.tag)
                namespaces = sorted({str(elem.tag).split("}")[0].lstrip("{") for elem in root.iter() if "}" in str(elem.tag)})
            except Exception:
                pass
        packets.append(
            {
                "index": idx,
                "offset": match.start(1),
                "length": len(packet),
                "sha256": _sha256_bytes(packet),
                "root_tag": root_tag,
                "namespaces": namespaces,
                "decoded": decoded,
            }
        )

    pillow_xmp: dict[str, Any] = {}
    try:
        getxmp = getattr(image, "getxmp", None)
        if callable(getxmp):
            xmp = getxmp()
            if xmp:
                pillow_xmp = _short(xmp, limit=4000)
    except Exception as exc:
        pillow_xmp = {"_error": str(exc)}

    return {
        "packet_count": len(packets),
        "packets": packets,
        "pillow_getxmp": pillow_xmp,
    }


def _ascii_identifier(payload: bytes, limit: int = 120) -> str:
    raw = payload[:limit]
    chars = []
    for byte in raw:
        if 32 <= byte <= 126:
            chars.append(chr(byte))
        elif byte == 0:
            chars.append("\\0")
        else:
            chars.append(".")
    return "".join(chars)


def _classify_jpeg_payload(marker_name: str, payload: bytes) -> str:
    ident = _ascii_identifier(payload, limit=160)
    low = ident.lower()
    if marker_name == "APP0" and ident.startswith("JFIF"):
        return "JFIF"
    if marker_name == "APP0" and ident.startswith("JFXX"):
        return "JFXX_thumbnail"
    if marker_name == "APP1" and ident.startswith("Exif"):
        return "EXIF"
    if marker_name == "APP1" and ("xmp" in low or "adobe:ns:meta" in low):
        return "XMP"
    if marker_name == "APP2" and "ICC_PROFILE" in ident:
        return "ICC_PROFILE"
    if marker_name == "APP13" and "photoshop" in low:
        return "Photoshop_IRB_IPTC"
    if marker_name == "APP14" and ident.startswith("Adobe"):
        return "Adobe_APP14"
    if "c2pa" in low:
        return "C2PA"
    if "jumbf" in low:
        return "JUMBF"
    if marker_name == "COM":
        return "JPEG_COMMENT"
    if marker_name.startswith("APP"):
        return "APP_UNKNOWN"
    return ""


def _jpeg_marker_name(marker: int) -> str:
    return JPEG_MARKER_NAMES.get(marker, f"0xFF{marker:02X}")


def _extract_low_level_jpeg_segments(raw: bytes) -> dict[str, Any]:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return {
            "is_jpeg_container": False,
            "segment_count": 0,
            "marker_counts": {},
            "segments": [],
        }

    segments: list[dict[str, Any]] = [
        {"marker": "SOI", "code": "0xFFD8", "offset": 0, "length": 0, "payload_length": 0}
    ]
    marker_counts: dict[str, int] = {"SOI": 1}
    offset = 2
    entropy_payload_bytes = 0

    while offset < len(raw):
        ff = raw.find(b"\xff", offset)
        if ff < 0 or ff + 1 >= len(raw):
            break
        marker_pos = ff
        marker_offset = ff + 1
        while marker_offset < len(raw) and raw[marker_offset] == 0xFF:
            marker_offset += 1
        if marker_offset >= len(raw):
            break
        marker = raw[marker_offset]
        offset = marker_offset + 1
        if marker == 0x00:
            # Escaped 0xFF inside entropy-coded data.
            continue

        name = _jpeg_marker_name(marker)
        marker_counts[name] = marker_counts.get(name, 0) + 1

        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            segments.append(
                {
                    "marker": name,
                    "code": f"0xFF{marker:02X}",
                    "offset": marker_pos,
                    "length": 0,
                    "payload_length": 0,
                }
            )
            if marker == 0xD9:
                break
            continue

        if offset + 2 > len(raw):
            segments.append(
                {
                    "marker": name,
                    "code": f"0xFF{marker:02X}",
                    "offset": marker_pos,
                    "error": "missing_length",
                }
            )
            break

        declared_length = int.from_bytes(raw[offset : offset + 2], "big")
        if declared_length < 2:
            segments.append(
                {
                    "marker": name,
                    "code": f"0xFF{marker:02X}",
                    "offset": marker_pos,
                    "length": declared_length,
                    "error": "invalid_length",
                }
            )
            break
        payload_start = offset + 2
        payload_end = min(len(raw), offset + declared_length)
        payload = raw[payload_start:payload_end]
        segment = {
            "marker": name,
            "code": f"0xFF{marker:02X}",
            "offset": marker_pos,
            "length": declared_length,
            "payload_length": len(payload),
            "payload_sha256": _sha256_bytes(payload) if payload else "",
        }
        if name.startswith("APP") or name == "COM":
            segment["identifier"] = _ascii_identifier(payload)
            segment["kind"] = _classify_jpeg_payload(name, payload)
        segments.append(segment)

        offset += declared_length
        if marker == 0xDA:
            entropy_payload_bytes = max(0, len(raw) - offset)
            break

    app_segments = [segment for segment in segments if str(segment.get("marker", "")).startswith("APP")]
    com_segments = [segment for segment in segments if segment.get("marker") == "COM"]
    identifiers = "\n".join(str(segment.get("identifier", "")) for segment in app_segments + com_segments)
    app_kind_counts: dict[str, int] = {}
    for segment in app_segments + com_segments:
        kind = str(segment.get("kind") or "")
        if kind:
            app_kind_counts[kind] = app_kind_counts.get(kind, 0) + 1
    return {
        "is_jpeg_container": True,
        "segment_count": len(segments),
        "marker_counts": marker_counts,
        "app_segment_count": len(app_segments),
        "com_segment_count": len(com_segments),
        "app_kind_counts": app_kind_counts,
        "entropy_payload_bytes_after_sos": entropy_payload_bytes,
        "has_app1_exif": any(segment.get("kind") == "EXIF" for segment in app_segments),
        "has_app1_xmp": any(segment.get("kind") == "XMP" for segment in app_segments),
        "has_app2_icc": any(segment.get("kind") == "ICC_PROFILE" for segment in app_segments),
        "has_app13_photoshop": any(segment.get("kind") == "Photoshop_IRB_IPTC" for segment in app_segments),
        "has_c2pa_or_jumbf_hint": "c2pa" in identifiers.lower() or "jumbf" in identifiers.lower(),
        "segments": segments,
    }


def _extract_pillow_applist(image: Image.Image) -> dict[str, Any]:
    raw_applist = getattr(image, "applist", None)
    if not raw_applist:
        return {"present": False, "count": 0, "items": []}
    items = []
    for idx, item in enumerate(raw_applist, start=1):
        try:
            marker, payload = item
            payload_bytes = bytes(payload) if isinstance(payload, (bytes, bytearray)) else bytes(str(payload), "utf-8", errors="replace")
            marker_name = str(marker)
            items.append(
                {
                    "index": idx,
                    "marker": marker_name,
                    "kind": _classify_jpeg_payload(marker_name, payload_bytes),
                    "length": len(payload_bytes),
                    "sha256": _sha256_bytes(payload_bytes),
                    "identifier": _ascii_identifier(payload_bytes),
                }
            )
        except Exception as exc:
            items.append({"index": idx, "error": str(exc), "raw": _short(item)})
    return {"present": True, "count": len(items), "items": items}


def _jpeg_encoder_details(image: Image.Image) -> dict[str, Any]:
    details: dict[str, Any] = {}
    quantization = getattr(image, "quantization", None)
    if isinstance(quantization, dict) and quantization:
        normalized = {str(k): list(v) for k, v in quantization.items()}
        details["quantization_table_count"] = len(normalized)
        details["quantization_sha256"] = _sha256_bytes(json.dumps(normalized, sort_keys=True).encode("utf-8"))
        details["quantization_tables"] = normalized
    else:
        details["quantization_table_count"] = 0
        details["quantization_sha256"] = ""

    try:
        if JpegImagePlugin is not None and hasattr(JpegImagePlugin, "get_sampling"):
            details["subsampling"] = JpegImagePlugin.get_sampling(image)  # type: ignore[attr-defined]
    except Exception as exc:
        details["subsampling_error"] = str(exc)

    for attr in ("layer", "layers", "progression", "progressive"):
        try:
            value = getattr(image, attr, None)
            if value is not None:
                details[attr] = _short(value, limit=1200)
        except Exception:
            pass

    return details


def _icc_header_ascii(raw: bytes, start: int, end: int) -> str:
    part = raw[start:end]
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in part).strip(".")


def _extract_icc_profile_details(icc: bytes | None) -> dict[str, Any]:
    if not icc:
        return {"present": False}
    details: dict[str, Any] = {
        "present": True,
        "length": len(icc),
        "sha256": _sha256_bytes(icc),
    }
    if len(icc) >= 128:
        try:
            details["header"] = {
                "declared_size": int.from_bytes(icc[0:4], "big"),
                "preferred_cmm_type": _icc_header_ascii(icc, 4, 8),
                "version_raw_hex": icc[8:12].hex(),
                "profile_device_class": _icc_header_ascii(icc, 12, 16),
                "color_space": _icc_header_ascii(icc, 16, 20),
                "pcs": _icc_header_ascii(icc, 20, 24),
                "created_year": int.from_bytes(icc[24:26], "big"),
                "created_month": int.from_bytes(icc[26:28], "big"),
                "created_day": int.from_bytes(icc[28:30], "big"),
                "created_hour": int.from_bytes(icc[30:32], "big"),
                "created_minute": int.from_bytes(icc[32:34], "big"),
                "created_second": int.from_bytes(icc[34:36], "big"),
                "signature": _icc_header_ascii(icc, 36, 40),
                "primary_platform": _icc_header_ascii(icc, 40, 44),
                "flags_hex": icc[44:48].hex(),
                "device_manufacturer": _icc_header_ascii(icc, 48, 52),
                "device_model": _icc_header_ascii(icc, 52, 56),
                "rendering_intent": int.from_bytes(icc[64:68], "big"),
                "profile_creator": _icc_header_ascii(icc, 80, 84),
            }
        except Exception as exc:
            details["header_error"] = str(exc)
    if len(icc) >= 132:
        try:
            tag_count = int.from_bytes(icc[128:132], "big")
            tags = []
            for idx in range(min(tag_count, 80)):
                off = 132 + idx * 12
                if off + 12 > len(icc):
                    break
                sig = _icc_header_ascii(icc, off, off + 4)
                data_offset = int.from_bytes(icc[off + 4 : off + 8], "big")
                data_size = int.from_bytes(icc[off + 8 : off + 12], "big")
                tags.append({"signature": sig, "offset": data_offset, "size": data_size})
            details["tag_table"] = {"count": tag_count, "tags": tags}
        except Exception as exc:
            details["tag_table_error"] = str(exc)

    if ImageCms is not None:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            inner = getattr(profile, "profile", None)
            cms_payload: dict[str, Any] = {}
            for attr in (
                "profile_description",
                "manufacturer",
                "model",
                "copyright",
                "icc_version",
                "device_class",
                "xcolor_space",
                "connection_space",
                "rendering_intent",
            ):
                try:
                    value = getattr(inner, attr, None) if inner is not None else getattr(profile, attr, None)
                    if value not in (None, ""):
                        cms_payload[attr] = _short(value, limit=1200)
                except Exception:
                    pass
            details["imagecms"] = cms_payload
        except Exception as exc:
            details["imagecms_error"] = str(exc)
    return details


def _extract_pillow_core_details(image: Image.Image, transposed: Image.Image, info: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {
        "format": image.format,
        "format_description": getattr(image, "format_description", ""),
        "mime": "",
        "mode": image.mode,
        "bands": list(image.getbands()),
        "size": list(image.size),
        "transposed_size": list(transposed.size),
        "readonly": bool(getattr(image, "readonly", False)),
        "is_animated": bool(getattr(image, "is_animated", False)),
        "n_frames": int(getattr(image, "n_frames", 1)),
        "has_palette": image.palette is not None,
        "has_transparency_info": "transparency" in info,
        "info_keys": sorted(str(k) for k in info.keys()),
        "tile": _short(getattr(image, "tile", None), limit=1200),
        "encoderinfo": _short(getattr(image, "encoderinfo", None), limit=1200),
    }
    try:
        get_format_mimetype = getattr(image, "get_format_mimetype", None)
        if callable(get_format_mimetype):
            details["mime"] = str(get_format_mimetype() or "")
    except Exception:
        pass
    try:
        details["extrema"] = _short(image.getextrema(), limit=1200)
    except Exception:
        pass
    return details


def _find_exiftool(exiftool_path: str | None = None) -> str | None:
    if exiftool_path:
        candidate = Path(exiftool_path)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(exiftool_path)
        if found:
            return found
        return None

    for name in ("exiftool", "exiftool.exe"):
        found = shutil.which(name)
        if found:
            return found

    common_windows_paths = [
        Path("C:/Windows/exiftool.exe"),
        Path("C:/Program Files/ExifTool/exiftool.exe"),
        Path("C:/Program Files (x86)/ExifTool/exiftool.exe"),
    ]
    for candidate in common_windows_paths:
        if candidate.exists():
            return str(candidate)
    return None


def _run_exiftool(path: Path, *, use_exiftool: bool, exiftool_path: str | None = None, require_exiftool: bool = False) -> dict[str, Any]:
    if not use_exiftool and not require_exiftool:
        return {"enabled": False, "available": False}

    executable = _find_exiftool(exiftool_path)
    if not executable:
        if require_exiftool:
            raise RuntimeError("ExifTool introuvable. Installe ExifTool ou passe --exiftool-path.")
        return {"enabled": True, "available": False, "error": "exiftool_not_found"}

    cmd = [executable, *EXIFTOOL_ARGS, str(path)]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
    except Exception as exc:
        if require_exiftool:
            raise
        return {"enabled": True, "available": True, "executable": executable, "error": str(exc)}

    tags: dict[str, Any] = {}
    parse_error = ""
    try:
        parsed = json.loads(proc.stdout or "[]")
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            tags = parsed[0]
    except Exception as exc:
        parse_error = str(exc)

    group_counts: dict[str, int] = {}
    for key in tags:
        group = str(key).split(":", 1)[0] if ":" in str(key) else "Ungrouped"
        group_counts[group] = group_counts.get(group, 0) + 1

    return {
        "enabled": True,
        "available": True,
        "executable": executable,
        "exit_code": proc.returncode,
        "command": " ".join(cmd),
        "stderr": proc.stderr.strip(),
        "stdout_raw_len": len(proc.stdout or ""),
        "parse_error": parse_error,
        "tag_count": len(tags),
        "group_counts": group_counts,
        "tags": _short(tags, limit=4000),
    }


def inspect_image_metadata(
    path: Path,
    *,
    use_exiftool: bool = False,
    exiftool_path: str | None = None,
    require_exiftool: bool = False,
) -> dict[str, Any]:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)

    stat = path.stat()
    raw = path.read_bytes()
    with Image.open(path) as image:
        transposed = ImageOps.exif_transpose(image)
        info = dict(image.info)
        exif_bytes = info.get("exif") if isinstance(info.get("exif"), bytes) else None
        icc = info.get("icc_profile") if isinstance(info.get("icc_profile"), bytes) else None
        metadata = {
            "path": str(path),
            "file": {
                "name": path.name,
                "suffix": path.suffix,
                "size_bytes": stat.st_size,
                "mtime_epoch": stat.st_mtime,
                "ctime_epoch": stat.st_ctime,
                "sha256": _sha256_file(path),
            },
            "image": {
                "format": image.format,
                "mode": image.mode,
                "width": image.width,
                "height": image.height,
                "transposed_width": transposed.width,
                "transposed_height": transposed.height,
                "info_keys": sorted(str(k) for k in info.keys()),
                "is_animated": bool(getattr(image, "is_animated", False)),
                "n_frames": int(getattr(image, "n_frames", 1)),
            },
            "pillow_core": _extract_pillow_core_details(image, transposed, info),
            "pillow_info": _extract_image_info_payload(info),
            "container": {
                "exif_present": bool(exif_bytes),
                "exif_length": len(exif_bytes) if exif_bytes else 0,
                "exif_sha256": _sha256_bytes(exif_bytes) if exif_bytes else "",
                "icc_profile_present": bool(icc),
                "icc_profile_length": len(icc) if icc else 0,
                "icc_profile_sha256": _sha256_bytes(icc) if icc else "",
                "dpi": _short(info.get("dpi")),
                "jfif": _short({k: info.get(k) for k in info if str(k).lower().startswith("jfif")}),
                "progressive": _short(info.get("progressive")),
            },
            "icc_profile": _extract_icc_profile_details(icc),
            "jpeg": {
                "low_level_segments": _extract_low_level_jpeg_segments(raw),
                "pillow_applist": _extract_pillow_applist(image),
                "encoder_details": _jpeg_encoder_details(image),
            },
            "pillow_exif": _extract_pillow_exif(image),
            "pillow_exif_raw_index": _extract_pillow_exif_raw_index(image),
            "piexif_groups": _extract_piexif_groups(exif_bytes),
            "xmp_like_info": _extract_xmp_like_info(info),
            "xmp_packets": _extract_xmp_packets(raw, image),
            "exiftool": _run_exiftool(
                path,
                use_exiftool=use_exiftool,
                exiftool_path=exiftool_path,
                require_exiftool=require_exiftool,
            ),
        }
    metadata["risk_summary"] = build_metadata_risk_summary(metadata)
    return metadata


def _flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value, next_prefix))
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    else:
        out[prefix] = str(obj)
    return out


def build_metadata_risk_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    flat = _flatten(
        {
            "pillow_exif": metadata.get("pillow_exif", {}),
            "pillow_exif_raw_index": metadata.get("pillow_exif_raw_index", {}),
            "piexif_groups": metadata.get("piexif_groups", {}),
            "xmp_like_info": metadata.get("xmp_like_info", {}),
            "xmp_packets": metadata.get("xmp_packets", {}),
            "exiftool": metadata.get("exiftool", {}).get("tags", {}),
            "jpeg": metadata.get("jpeg", {}).get("low_level_segments", {}),
            "icc_profile": metadata.get("icc_profile", {}),
        }
    )
    sensitive_hits = []
    for key, value in flat.items():
        leaf = key.split(".")[-1]
        leaf_no_group = leaf.split(":")[-1]
        if (
            leaf in SENSITIVE_TAG_NAMES
            or leaf_no_group in SENSITIVE_TAG_NAMES
            or leaf.startswith("GPS")
            or "GPS" in key
            or "Serial" in key
            or "History" in key
            or "DocumentID" in key
            or "InstanceID" in key
            or "DerivedFrom" in key
            or "C2PA" in key.upper()
            or "JUMBF" in key.upper()
        ):
            if value not in ("", "None", "{}", "[]", "False", "0"):
                sensitive_hits.append(key)
    jpeg_segments = metadata.get("jpeg", {}).get("low_level_segments", {})
    return {
        "has_exif": bool(metadata.get("container", {}).get("exif_present")) or bool(jpeg_segments.get("has_app1_exif")),
        "has_gps": any("GPS" in hit for hit in sensitive_hits),
        "has_icc_profile": bool(metadata.get("container", {}).get("icc_profile_present")) or bool(jpeg_segments.get("has_app2_icc")),
        "has_xmp_like_info": bool(metadata.get("xmp_like_info")) or bool(jpeg_segments.get("has_app1_xmp")) or bool(metadata.get("xmp_packets", {}).get("packet_count")),
        "has_photoshop_metadata": bool(jpeg_segments.get("has_app13_photoshop")),
        "has_c2pa_or_jumbf_hint": bool(jpeg_segments.get("has_c2pa_or_jumbf_hint")),
        "sensitive_tag_paths": sorted(sensitive_hits),
    }


def _skip_compare_key(key: str) -> bool:
    if key in VOLATILE_COMPARE_KEYS:
        return True
    if key.startswith("file.mtime_epoch") or key.startswith("file.ctime_epoch"):
        return True
    if any(key.endswith(suffix) for suffix in PATH_LIKE_COMPARE_SUFFIXES):
        return True
    return False


def _comparison_summary(left_flat: dict[str, str], right_flat: dict[str, str], same_items: list[dict[str, str]], differences: list[dict[str, str]]) -> dict[str, Any]:
    same_keys = [item["key"] for item in same_items]
    original_only = [diff["key"] for diff in differences if diff.get("filtered", "") == ""]
    filtered_only = [diff["key"] for diff in differences if diff.get("original", "") == ""]
    changed = [diff["key"] for diff in differences if diff.get("original", "") != "" and diff.get("filtered", "") != ""]
    same_important_prefixes = (
        "image.",
        "pillow_core.",
        "container.",
        "icc_profile.",
        "jpeg.low_level_segments.marker_counts",
        "jpeg.low_level_segments.app_kind_counts",
        "jpeg.low_level_segments.has_",
        "jpeg.encoder_details.",
        "jpeg.pillow_applist.",
        "xmp_packets.",
        "exiftool.tags.",
    )
    important_same = [item for item in same_items if item["key"].startswith(same_important_prefixes)]
    return {
        "original_key_count": len(left_flat),
        "filtered_key_count": len(right_flat),
        "same_key_count": len(same_items),
        "different_key_count": len(differences),
        "original_only_count": len(original_only),
        "filtered_only_count": len(filtered_only),
        "changed_value_count": len(changed),
        "important_same_items": important_same[:160],
        "important_same_keys": [item["key"] for item in important_same[:160]],
        "original_only_keys_sample": original_only[:160],
        "filtered_only_keys_sample": filtered_only[:160],
        "changed_keys_sample": changed[:160],
        "same_keys_sample": same_keys[:160],
    }


def compare_metadata(
    original: Path,
    filtered: Path,
    *,
    use_exiftool: bool = False,
    exiftool_path: str | None = None,
    require_exiftool: bool = False,
) -> dict[str, Any]:
    left = inspect_image_metadata(
        original,
        use_exiftool=use_exiftool,
        exiftool_path=exiftool_path,
        require_exiftool=require_exiftool,
    )
    right = inspect_image_metadata(
        filtered,
        use_exiftool=use_exiftool,
        exiftool_path=exiftool_path,
        require_exiftool=require_exiftool,
    )
    left_flat = _flatten(left)
    right_flat = _flatten(right)
    keys = sorted(set(left_flat) | set(right_flat))
    differences = []
    same_items = []
    for key in keys:
        if _skip_compare_key(key):
            continue
        a = left_flat.get(key, "")
        b = right_flat.get(key, "")
        if a == b:
            same_items.append({"key": key, "value": a})
        else:
            differences.append({"key": key, "original": a, "filtered": b})
    summary = _comparison_summary(left_flat, right_flat, same_items, differences)
    return {
        "original": left,
        "filtered": right,
        "same_key_count": len(same_items),
        "different_key_count": len(differences),
        "same_keys": [item["key"] for item in same_items],
        "same_items": same_items,
        "differences": differences,
        "similarity_summary": summary,
    }


def _load_export_pairs(export_dir: Path) -> list[tuple[Path, Path]]:
    selected = export_dir / "selected_filter.json"
    if not selected.exists():
        raise RuntimeError(f"selected_filter.json introuvable dans: {export_dir}")
    payload = json.loads(selected.read_text(encoding="utf-8"))
    sources = payload.get("source_images") or []
    outputs = payload.get("output_paths") or []
    if not isinstance(sources, list) or not isinstance(outputs, list):
        raise RuntimeError("selected_filter.json invalide: source_images/output_paths")
    pairs: list[tuple[Path, Path]] = []
    for source, output in zip(sources, outputs):
        if not isinstance(source, dict):
            continue
        pairs.append((Path(str(source.get("source_path") or "")), Path(str(output))))
    if not pairs:
        raise RuntimeError(f"Aucune paire source/output trouvee dans: {selected}")
    return pairs


def write_report(report: dict[str, Any], output_json: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_diff_csv(comparisons: list[dict[str, Any]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pair_index", "key", "original", "filtered"])
        writer.writeheader()
        for idx, comparison in enumerate(comparisons, start=1):
            for diff in comparison.get("differences", []):
                writer.writerow({"pair_index": idx, **diff})


def write_same_csv(comparisons: list[dict[str, Any]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pair_index", "key", "value"])
        writer.writeheader()
        for idx, comparison in enumerate(comparisons, start=1):
            for item in comparison.get("same_items", []):
                writer.writerow({"pair_index": idx, "key": item.get("key", ""), "value": item.get("value", "")})


def inspect_export_dir(
    export_dir: Path,
    *,
    use_exiftool: bool = False,
    exiftool_path: str | None = None,
    require_exiftool: bool = False,
) -> dict[str, Any]:
    pairs = _load_export_pairs(export_dir)
    comparisons = [
        compare_metadata(
            original,
            filtered,
            use_exiftool=use_exiftool,
            exiftool_path=exiftool_path,
            require_exiftool=require_exiftool,
        )
        for original, filtered in pairs
    ]
    return {
        "export_dir": str(export_dir),
        "pair_count": len(comparisons),
        "exiftool_enabled": bool(use_exiftool or require_exiftool),
        "pairs": [
            {"original": str(original), "filtered": str(filtered)}
            for original, filtered in pairs
        ],
        "comparisons": comparisons,
    }


def print_summary(report: dict[str, Any]) -> None:
    comparisons = report.get("comparisons") or [report]
    print("IMAGE METADATA REPORT")
    print(f"pairs: {len(comparisons)}")
    print(f"exiftool_enabled: {bool(report.get('exiftool_enabled'))}")
    for idx, comparison in enumerate(comparisons, start=1):
        original = comparison["original"]
        filtered = comparison["filtered"]
        summary = comparison.get("similarity_summary", {})
        original_exiftool = original.get("exiftool", {})
        filtered_exiftool = filtered.get("exiftool", {})
        print("")
        print(f"PAIR {idx}")
        print(f"original: {original['path']}")
        print(f"filtered: {filtered['path']}")
        print(f"original_sha256: {original['file']['sha256']}")
        print(f"filtered_sha256: {filtered['file']['sha256']}")
        print(f"size: {original['image']['width']}x{original['image']['height']} -> {filtered['image']['width']}x{filtered['image']['height']}")
        print(f"format/mode: {original['image']['format']}/{original['image']['mode']} -> {filtered['image']['format']}/{filtered['image']['mode']}")
        print(f"mime: {original['pillow_core'].get('mime', '')} -> {filtered['pillow_core'].get('mime', '')}")
        print(f"bands: {original['pillow_core'].get('bands', [])} -> {filtered['pillow_core'].get('bands', [])}")
        print(f"exif_present: {original['container']['exif_present']} -> {filtered['container']['exif_present']}")
        print(f"icc_present: {original['container']['icc_profile_present']} -> {filtered['container']['icc_profile_present']}")
        print(f"xmp_like_present: {bool(original['xmp_like_info']) or bool(original.get('xmp_packets', {}).get('packet_count'))} -> {bool(filtered['xmp_like_info']) or bool(filtered.get('xmp_packets', {}).get('packet_count'))}")
        print(f"xmp_packet_count: {original.get('xmp_packets', {}).get('packet_count', 0)} -> {filtered.get('xmp_packets', {}).get('packet_count', 0)}")
        print(f"gps_present: {original['risk_summary']['has_gps']} -> {filtered['risk_summary']['has_gps']}")
        print(f"photoshop_metadata: {original['risk_summary']['has_photoshop_metadata']} -> {filtered['risk_summary']['has_photoshop_metadata']}")
        print(f"c2pa_or_jumbf_hint: {original['risk_summary']['has_c2pa_or_jumbf_hint']} -> {filtered['risk_summary']['has_c2pa_or_jumbf_hint']}")
        print(f"jpeg_app_segments: {original['jpeg']['low_level_segments']['app_segment_count']} -> {filtered['jpeg']['low_level_segments']['app_segment_count']}")
        print(f"jpeg_app_kinds: {original['jpeg']['low_level_segments'].get('app_kind_counts', {})} -> {filtered['jpeg']['low_level_segments'].get('app_kind_counts', {})}")
        print(f"jpeg_com_segments: {original['jpeg']['low_level_segments']['com_segment_count']} -> {filtered['jpeg']['low_level_segments']['com_segment_count']}")
        print(f"jpeg_quantization_sha256: {original['jpeg']['encoder_details'].get('quantization_sha256', '')} -> {filtered['jpeg']['encoder_details'].get('quantization_sha256', '')}")
        print(f"jpeg_subsampling: {original['jpeg']['encoder_details'].get('subsampling', '')} -> {filtered['jpeg']['encoder_details'].get('subsampling', '')}")
        if original.get("icc_profile", {}).get("present") or filtered.get("icc_profile", {}).get("present"):
            print(f"icc_description: {original.get('icc_profile', {}).get('imagecms', {}).get('profile_description', '')} -> {filtered.get('icc_profile', {}).get('imagecms', {}).get('profile_description', '')}")
        if original_exiftool.get("enabled") or filtered_exiftool.get("enabled"):
            print(f"exiftool_available: {original_exiftool.get('available')} -> {filtered_exiftool.get('available')}")
            print(f"exiftool_tag_count: {original_exiftool.get('tag_count', 0)} -> {filtered_exiftool.get('tag_count', 0)}")
            print(f"exiftool_group_counts: {original_exiftool.get('group_counts', {})} -> {filtered_exiftool.get('group_counts', {})}")
        print(f"sensitive_tags_original: {len(original['risk_summary']['sensitive_tag_paths'])}")
        print(f"sensitive_tags_filtered: {len(filtered['risk_summary']['sensitive_tag_paths'])}")
        print(f"same_keys: {comparison['same_key_count']}")
        print(f"different_keys: {comparison['different_key_count']}")
        if summary.get("important_same_items"):
            print("important_same_items_sample:")
            for item in summary["important_same_items"][:14]:
                print(f"  SAME: {item.get('key')} = {item.get('value')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspecte et compare les metadonnees d'images source/export.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export-dir", default=None, help="Dossier d'export contenant selected_filter.json")
    group.add_argument("--original", default=None, help="Image originale a comparer")
    parser.add_argument("--filtered", default=None, help="Image filtree si --original est utilise")
    parser.add_argument("--output-json", default=None, help="Chemin du rapport JSON")
    parser.add_argument("--output-csv", default=None, help="Chemin du CSV de differences")
    parser.add_argument("--output-same-csv", default=None, help="Chemin du CSV des cles identiques")
    parser.add_argument("--use-exiftool", action="store_true", help="Ajoute une passe ExifTool JSON complete si exiftool est installe.")
    parser.add_argument("--require-exiftool", action="store_true", help="Echoue si ExifTool n'est pas disponible.")
    parser.add_argument("--exiftool-path", default=None, help="Chemin explicite vers exiftool.exe si non present dans le PATH.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    use_exiftool = bool(args.use_exiftool or args.require_exiftool)
    if args.original:
        if not args.filtered:
            raise SystemExit("--filtered est requis avec --original")
        report = compare_metadata(
            Path(args.original),
            Path(args.filtered),
            use_exiftool=use_exiftool,
            exiftool_path=args.exiftool_path,
            require_exiftool=args.require_exiftool,
        )
        report["exiftool_enabled"] = use_exiftool
        default_root = Path(args.filtered).parent
        comparisons = [report]
    else:
        export_dir = Path(str(args.export_dir))
        report = inspect_export_dir(
            export_dir,
            use_exiftool=use_exiftool,
            exiftool_path=args.exiftool_path,
            require_exiftool=args.require_exiftool,
        )
        default_root = export_dir
        comparisons = report.get("comparisons", [])

    output_json = Path(args.output_json) if args.output_json else default_root / "metadata_compare_report.json"
    output_csv = Path(args.output_csv) if args.output_csv else default_root / "metadata_compare_diff.csv"
    output_same_csv = Path(args.output_same_csv) if args.output_same_csv else default_root / "metadata_compare_same.csv"
    write_report(report, output_json)
    write_diff_csv(list(comparisons), output_csv)
    write_same_csv(list(comparisons), output_same_csv)
    print_summary(report)
    print("")
    print(f"json: {output_json}")
    print(f"csv_diff: {output_csv}")
    print(f"csv_same: {output_same_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
