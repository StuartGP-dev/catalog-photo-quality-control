from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import ExifTags, Image, ImageOps

try:  # optional but installed through requirements for richer EXIF group parsing
    import piexif  # type: ignore
except Exception:  # pragma: no cover - optional dependency guard
    piexif = None  # type: ignore


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
}


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
        return [_short(v, limit=limit) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k): _short(v, limit=limit) for k, v in value.items()}
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _decode_exif_value(value: Any) -> Any:
    try:
        if isinstance(value, bytes):
            for encoding in ("utf-8", "latin-1", "ascii"):
                try:
                    decoded = value.decode(encoding).strip("\x00")
                    if decoded:
                        return decoded
                except Exception:
                    continue
            return {"kind": "bytes", "length": len(value), "sha256": _sha256_bytes(value)}
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            return f"{value.numerator}/{value.denominator}"
        if isinstance(value, tuple):
            return [_decode_exif_value(v) for v in value]
        if isinstance(value, list):
            return [_decode_exif_value(v) for v in value]
        return value
    except Exception:
        return _short(value)


def _extract_pillow_exif(image: Image.Image) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        exif = image.getexif()
    except Exception:
        return out

    for tag_id, value in exif.items():
        name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
        if name == "GPSInfo":
            # Expanded below when possible.
            continue
        out[name] = _decode_exif_value(value)

    # Pillow exposes nested IFDs through get_ifd on recent versions.
    try:
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)  # type: ignore[attr-defined]
        if gps_ifd:
            out["GPSInfo"] = {
                GPS_TAGS_BY_ID.get(int(tag_id), str(tag_id)): _decode_exif_value(value)
                for tag_id, value in gps_ifd.items()
            }
    except Exception:
        pass

    try:
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)  # type: ignore[attr-defined]
        if exif_ifd:
            for tag_id, value in exif_ifd.items():
                name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
                out.setdefault(name, _decode_exif_value(value))
    except Exception:
        pass

    return out


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


def _extract_xmp_like_info(info: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in info.items():
        norm = str(key).lower()
        if "xmp" in norm or "xml" in norm or "iptc" in norm:
            payload[str(key)] = _short(value)
    return payload


def inspect_image_metadata(path: Path) -> dict[str, Any]:
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
            "pillow_exif": _extract_pillow_exif(image),
            "piexif_groups": _extract_piexif_groups(exif_bytes),
            "xmp_like_info": _extract_xmp_like_info(info),
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
    flat = _flatten({"pillow_exif": metadata.get("pillow_exif", {}), "piexif_groups": metadata.get("piexif_groups", {}), "xmp_like_info": metadata.get("xmp_like_info", {})})
    sensitive_hits = []
    for key, value in flat.items():
        leaf = key.split(".")[-1]
        if leaf in SENSITIVE_TAG_NAMES or leaf.startswith("GPS") or "GPS" in key or "Serial" in key or "History" in key:
            if value not in ("", "None", "{}", "[]"):
                sensitive_hits.append(key)
    return {
        "has_exif": bool(metadata.get("container", {}).get("exif_present")),
        "has_gps": any("GPS" in hit for hit in sensitive_hits),
        "has_icc_profile": bool(metadata.get("container", {}).get("icc_profile_present")),
        "has_xmp_like_info": bool(metadata.get("xmp_like_info")),
        "sensitive_tag_paths": sorted(sensitive_hits),
    }


def compare_metadata(original: Path, filtered: Path) -> dict[str, Any]:
    left = inspect_image_metadata(original)
    right = inspect_image_metadata(filtered)
    left_flat = _flatten(left)
    right_flat = _flatten(right)
    keys = sorted(set(left_flat) | set(right_flat))
    differences = []
    same = []
    for key in keys:
        if key.startswith("file.mtime_epoch") or key == "path":
            continue
        a = left_flat.get(key, "")
        b = right_flat.get(key, "")
        if a == b:
            same.append(key)
        else:
            differences.append({"key": key, "original": a, "filtered": b})
    return {
        "original": left,
        "filtered": right,
        "same_key_count": len(same),
        "different_key_count": len(differences),
        "same_keys": same,
        "differences": differences,
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


def inspect_export_dir(export_dir: Path) -> dict[str, Any]:
    pairs = _load_export_pairs(export_dir)
    comparisons = [compare_metadata(original, filtered) for original, filtered in pairs]
    return {
        "export_dir": str(export_dir),
        "pair_count": len(comparisons),
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
    for idx, comparison in enumerate(comparisons, start=1):
        original = comparison["original"]
        filtered = comparison["filtered"]
        print("")
        print(f"PAIR {idx}")
        print(f"original: {original['path']}")
        print(f"filtered: {filtered['path']}")
        print(f"original_sha256: {original['file']['sha256']}")
        print(f"filtered_sha256: {filtered['file']['sha256']}")
        print(f"size: {original['image']['width']}x{original['image']['height']} -> {filtered['image']['width']}x{filtered['image']['height']}")
        print(f"format/mode: {original['image']['format']}/{original['image']['mode']} -> {filtered['image']['format']}/{filtered['image']['mode']}")
        print(f"exif_present: {original['container']['exif_present']} -> {filtered['container']['exif_present']}")
        print(f"icc_present: {original['container']['icc_profile_present']} -> {filtered['container']['icc_profile_present']}")
        print(f"xmp_like_present: {bool(original['xmp_like_info'])} -> {bool(filtered['xmp_like_info'])}")
        print(f"gps_present: {original['risk_summary']['has_gps']} -> {filtered['risk_summary']['has_gps']}")
        print(f"sensitive_tags_original: {len(original['risk_summary']['sensitive_tag_paths'])}")
        print(f"sensitive_tags_filtered: {len(filtered['risk_summary']['sensitive_tag_paths'])}")
        print(f"different_keys: {comparison['different_key_count']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspecte et compare les metadonnees d'images source/export.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export-dir", default=None, help="Dossier d'export contenant selected_filter.json")
    group.add_argument("--original", default=None, help="Image originale a comparer")
    parser.add_argument("--filtered", default=None, help="Image filtree si --original est utilise")
    parser.add_argument("--output-json", default=None, help="Chemin du rapport JSON")
    parser.add_argument("--output-csv", default=None, help="Chemin du CSV de differences")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.original:
        if not args.filtered:
            raise SystemExit("--filtered est requis avec --original")
        report = compare_metadata(Path(args.original), Path(args.filtered))
        default_root = Path(args.filtered).parent
        comparisons = [report]
    else:
        export_dir = Path(str(args.export_dir))
        report = inspect_export_dir(export_dir)
        default_root = export_dir
        comparisons = report.get("comparisons", [])

    output_json = Path(args.output_json) if args.output_json else default_root / "metadata_compare_report.json"
    output_csv = Path(args.output_csv) if args.output_csv else default_root / "metadata_compare_diff.csv"
    write_report(report, output_json)
    write_diff_csv(list(comparisons), output_csv)
    print_summary(report)
    print("")
    print(f"json: {output_json}")
    print(f"csv: {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
