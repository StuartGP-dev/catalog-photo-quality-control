from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image


EXIF_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.TAGS.items()}
GPS_TAGS_BY_ID = {int(k): str(v) for k, v in ExifTags.GPSTAGS.items()}


def _short(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        preview = value[:32].hex()
        return f"<bytes len={len(value)} hex={preview}>"
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _decode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16", "latin-1", "ascii"):
            try:
                decoded = value.decode(encoding).strip("\x00")
                if decoded:
                    return decoded
            except Exception:
                pass
        return f"<bytes len={len(value)}>"
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, tuple):
        return [_decode_value(v) for v in value]
    if isinstance(value, list):
        return [_decode_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _decode_value(v) for k, v in value.items()}
    return value


def _read_exif(image: Image.Image) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        exif = image.getexif()
    except Exception as exc:
        return {"EXIF_ERROR": str(exc)}

    for tag_id, value in exif.items():
        name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
        if name == "GPSInfo":
            continue
        out[name] = _decode_value(value)

    ifd_enum = getattr(ExifTags, "IFD", None)
    if ifd_enum is not None:
        ifd_names = ("Exif", "GPSInfo", "Interop", "IFD1")
        for ifd_name in ifd_names:
            ifd_id = getattr(ifd_enum, ifd_name, None)
            if ifd_id is None:
                continue
            try:
                ifd = exif.get_ifd(ifd_id)
            except Exception:
                continue
            if not ifd:
                continue
            nested: dict[str, Any] = {}
            for tag_id, value in ifd.items():
                if ifd_name == "GPSInfo":
                    tag_name = GPS_TAGS_BY_ID.get(int(tag_id), str(tag_id))
                else:
                    tag_name = EXIF_TAGS_BY_ID.get(int(tag_id), str(tag_id))
                nested[tag_name] = _decode_value(value)
            out[ifd_name] = nested

    return out


def _read_pillow_metadata(path: Path) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(path) as image:
            info = dict(image.info)
            meta: dict[str, Any] = {
                "file.name": path.name,
                "file.suffix": path.suffix,
                "file.size_bytes": path.stat().st_size,
                "image.format": image.format,
                "image.mode": image.mode,
                "image.width": image.width,
                "image.height": image.height,
                "image.mime": image.get_format_mimetype() if hasattr(image, "get_format_mimetype") else "",
                "image.bands": list(image.getbands()),
                "image.is_animated": bool(getattr(image, "is_animated", False)),
                "image.n_frames": int(getattr(image, "n_frames", 1)),
                "pillow.info_keys": sorted(str(k) for k in info.keys()),
            }

            for key, value in sorted(info.items(), key=lambda item: str(item[0])):
                meta[f"pillow.info.{key}"] = _decode_value(value)

            exif = _read_exif(image)
            if exif:
                for key, value in sorted(exif.items(), key=lambda item: str(item[0])):
                    meta[f"exif.{key}"] = value
            else:
                meta["exif"] = "<absent>"

            quantization = getattr(image, "quantization", None)
            if quantization:
                meta["jpeg.quantization_table_count"] = len(quantization)
                meta["jpeg.quantization_tables"] = {str(k): v for k, v in quantization.items()}
            applist = getattr(image, "applist", None)
            if applist:
                meta["jpeg.applist_count"] = len(applist)
                meta["jpeg.applist"] = [
                    {"marker": str(marker), "length": len(payload), "preview": _short(payload)}
                    for marker, payload in applist
                ]
            return meta


def _find_exiftool(explicit_path: str | None = None) -> str | None:
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return str(path)
        found = shutil.which(explicit_path)
        if found:
            return found
        return None
    for name in ("exiftool", "exiftool.exe"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        Path("C:/Windows/exiftool.exe"),
        Path("C:/Program Files/ExifTool/exiftool.exe"),
        Path("C:/Program Files (x86)/ExifTool/exiftool.exe"),
    ):
        if path.exists():
            return str(path)
    return None


def _read_exiftool_metadata(path: Path, *, exiftool_path: str | None = None) -> dict[str, Any]:
    exe = _find_exiftool(exiftool_path)
    if not exe:
        return {"exiftool": "<not found>"}
    cmd = [exe, "-j", "-G1", "-a", "-s", "-ee", "-u", "-validate", str(path)]
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
        tags = {"exiftool_parse_error": str(exc)}
    if proc.stderr.strip():
        tags["exiftool_stderr"] = proc.stderr.strip()
    return tags


def read_metadata(path: Path, *, use_exiftool: bool = False, exiftool_path: str | None = None) -> dict[str, Any]:
    path = Path(path)
    meta = _read_pillow_metadata(path)
    if use_exiftool:
        exiftool_meta = _read_exiftool_metadata(path, exiftool_path=exiftool_path)
        for key, value in sorted(exiftool_meta.items(), key=lambda item: str(item[0])):
            meta[f"exiftool.{key}"] = value
    return meta


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
    print(f"path: {path}")
    print("metadata:")
    for key in sorted(metadata):
        value = metadata[key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            rendered = str(value)
        print(f"- {key}: {_short(rendered, limit=1200)}")


def write_json_report(pairs_report: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"pairs": pairs_report}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Affiche simplement toutes les métadonnées d'une photo originale et filtrée.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export-dir", default=None, help="Dossier d'export contenant selected_filter.json")
    group.add_argument("--original", default=None, help="Photo originale")
    parser.add_argument("--filtered", default=None, help="Photo filtrée si --original est utilisé")
    parser.add_argument("--use-exiftool", action="store_true", help="Ajoute les tags ExifTool si ExifTool est installé")
    parser.add_argument("--exiftool-path", default=None, help="Chemin vers exiftool.exe si besoin")
    parser.add_argument("--output-json", default=None, help="Chemin du rapport JSON")
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
        print_metadata_block("Photo originale:", original, original_meta)
        print("")
        print_metadata_block("Photo filtrée:", filtered, filtered_meta)
        report.append(
            {
                "pair_index": idx,
                "original_path": str(original),
                "filtered_path": str(filtered),
                "original_metadata": original_meta,
                "filtered_metadata": filtered_meta,
            }
        )

    output_json = Path(args.output_json) if args.output_json else default_root / "metadata_original_filtered.json"
    write_json_report(report, output_json)
    print("")
    print(f"json: {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
