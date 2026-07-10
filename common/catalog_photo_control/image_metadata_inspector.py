from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

try:
    from PIL import ImageCms
except Exception:  # pragma: no cover - depends on the Pillow build
    ImageCms = None  # type: ignore


EXIF_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.TAGS.items()}
GPS_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.GPSTAGS.items()}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_value(value: Any) -> Any:
    """Convert Pillow values to lossless JSON-friendly data.

    Binary metadata is kept in full as base64, with its size and SHA-256.
    Nothing is truncated in the JSON report.
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
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


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
        raw_tags[str(int(tag_id))] = {
            "name": name,
            "value": _json_value(value),
        }

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
                raw_decoded[str(int(tag_id))] = {
                    "name": name,
                    "value": _json_value(value),
                }
            ifds[ifd_name] = {
                "tags": decoded,
                "raw_numeric_tags": raw_decoded,
            }

    return {
        "present": bool(exif),
        "tag_count": len(exif),
        "tags": tags,
        "raw_numeric_tags": raw_tags,
        "ifds": ifds,
    }


def _classify_app(marker: str, payload: bytes) -> str:
    prefix = payload[:160]
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


def _read_jpeg_details(image: Image.Image) -> dict[str, Any]:
    applist_payload: list[dict[str, Any]] = []
    for marker, payload in getattr(image, "applist", None) or []:
        raw = bytes(payload)
        applist_payload.append(
            {
                "marker": str(marker),
                "kind": _classify_app(str(marker), raw),
                "length": len(raw),
                "sha256": _sha256_bytes(raw),
                "payload": _json_value(raw),
            }
        )

    quantization = getattr(image, "quantization", None) or {}
    quantization_json = {str(key): list(values) for key, values in quantization.items()}
    quantization_bytes = json.dumps(quantization_json, sort_keys=True, separators=(",", ":")).encode("utf-8")

    return {
        "app_segment_count": len(applist_payload),
        "app_segments": applist_payload,
        "quantization_table_count": len(quantization_json),
        "quantization_tables": quantization_json,
        "quantization_sha256": _sha256_bytes(quantization_bytes) if quantization_json else "",
        "layers": _json_value(getattr(image, "layers", None)),
        "layer": _json_value(getattr(image, "layer", None)),
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
        return {
            "available": False,
            "error": "exiftool_not_found",
            "tags": {},
        }

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

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(path) as image:
            info = dict(image.info)
            icc_bytes = info.get("icc_profile") if isinstance(info.get("icc_profile"), bytes) else None
            exif_bytes = info.get("exif") if isinstance(info.get("exif"), bytes) else None
            xmp_value = info.get("xmp")
            photoshop_value = info.get("photoshop")

            metadata: dict[str, Any] = {
                "file": {
                    "name": path.name,
                    "suffix": path.suffix,
                    "size_bytes": path.stat().st_size,
                },
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
                "container": {
                    "info_keys": sorted(str(key) for key in info.keys()),
                    "pillow_info_full": _json_value(info),
                    "exif_block": _json_value(exif_bytes) if exif_bytes else None,
                    "icc_profile_block": _json_value(icc_bytes) if icc_bytes else None,
                    "xmp_block": _json_value(xmp_value) if xmp_value is not None else None,
                    "photoshop_block": _json_value(photoshop_value) if photoshop_value is not None else None,
                },
                "exif": _read_exif(image),
                "jpeg": _read_jpeg_details(image),
                "icc": {
                    "present": bool(icc_bytes),
                    "description": _icc_description(icc_bytes),
                },
                "xmp": {
                    "present": xmp_value is not None,
                },
                "photoshop_iptc": {
                    "present": photoshop_value is not None,
                },
            }

    metadata["exiftool"] = (
        _read_exiftool_metadata(path, exiftool_path=exiftool_path)
        if use_exiftool
        else {"available": False, "not_requested": True, "tags": {}}
    )
    return metadata


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


def _jpeg_block_names(metadata: dict[str, Any]) -> list[str]:
    segments = metadata.get("jpeg", {}).get("app_segments", [])
    return [str(item.get("kind") or item.get("marker") or "") for item in segments]


def build_terminal_summary(metadata: dict[str, Any]) -> list[tuple[str, Any]]:
    exif_tags = metadata.get("exif", {}).get("tags", {})
    exif_ifds = metadata.get("exif", {}).get("ifds", {})
    exif_detail = exif_ifds.get("Exif", {}).get("tags", {}) if isinstance(exif_ifds, dict) else {}

    make = exif_tags.get("Make", "")
    model = exif_tags.get("Model", "")
    device = " ".join(str(value) for value in (make, model) if value).strip()

    return [
        ("Format", metadata.get("image", {}).get("format", "")),
        ("Dimensions", f"{metadata.get('image', {}).get('width', '')} × {metadata.get('image', {}).get('height', '')}"),
        ("Mode couleur", metadata.get("image", {}).get("mode", "")),
        ("Taille du fichier", f"{metadata.get('file', {}).get('size_bytes', '')} octets"),
        ("Appareil", device or "absent"),
        ("Objectif", exif_detail.get("LensModel") or exif_tags.get("LensModel") or "absent"),
        ("Logiciel", exif_tags.get("Software") or "absent"),
        ("Date de prise", exif_detail.get("DateTimeOriginal") or exif_tags.get("DateTimeOriginal") or "absente"),
        ("Date de modification", exif_tags.get("DateTime") or "absente"),
        ("ISO", exif_detail.get("ISOSpeedRatings") or "absent"),
        ("Exposition", exif_detail.get("ExposureTime") or "absente"),
        ("Ouverture", exif_detail.get("FNumber") or "absente"),
        ("Focale", exif_detail.get("FocalLength") or "absente"),
        ("DPI", _first_value(metadata, "container.pillow_info_full.dpi") or "absent"),
        ("EXIF", "présent" if metadata.get("exif", {}).get("present") else "absent"),
        ("XMP", "présent" if metadata.get("xmp", {}).get("present") else "absent"),
        ("Profil ICC", metadata.get("icc", {}).get("description") or ("présent" if metadata.get("icc", {}).get("present") else "absent")),
        ("Photoshop/IPTC", "présent" if metadata.get("photoshop_iptc", {}).get("present") else "absent"),
        ("Blocs JPEG", ", ".join(_jpeg_block_names(metadata)) or "aucun"),
        ("Tables JPEG", metadata.get("jpeg", {}).get("quantization_table_count", 0)),
        ("ExifTool", "disponible" if metadata.get("exiftool", {}).get("available") else "non disponible"),
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
        "schema_version": 2,
        "binary_encoding": "base64",
        "note": "Les blocs binaires de métadonnées sont conservés intégralement en base64, avec longueur et SHA-256.",
        "pairs": pairs_report,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Affiche un résumé lisible des métadonnées et écrit tous les détails, y compris binaires, dans un JSON."
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
    for idx, (original, filtered) in enumerate(pairs, start=1):
        original_meta = read_metadata(original, use_exiftool=args.use_exiftool, exiftool_path=args.exiftool_path)
        filtered_meta = read_metadata(filtered, use_exiftool=args.use_exiftool, exiftool_path=args.exiftool_path)

        print("")
        print(f"==================== PAIRE {idx} ====================")
        print_metadata_block("Photo originale :", original, original_meta)
        print("")
        print_metadata_block("Photo filtrée :", filtered, filtered_meta)

        report.append(
            {
                "pair_index": idx,
                "original": {
                    "path": str(original),
                    "summary": {label: _json_value(value) for label, value in build_terminal_summary(original_meta)},
                    "metadata_full": original_meta,
                },
                "filtered": {
                    "path": str(filtered),
                    "summary": {label: _json_value(value) for label, value in build_terminal_summary(filtered_meta)},
                    "metadata_full": filtered_meta,
                },
            }
        )

    output_json = Path(args.output_json) if args.output_json else default_root / "metadata_original_filtered_full.json"
    write_json_report(report, output_json)
    print("")
    print(f"JSON détaillé : {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
