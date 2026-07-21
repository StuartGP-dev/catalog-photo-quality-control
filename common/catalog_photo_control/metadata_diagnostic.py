from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import struct
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import ExifTags, Image, ImageCms, ImageOps
from PIL.IptcImagePlugin import getiptcinfo

from .image_similarity import compare_images


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    return str(value)


def _jpeg_segments(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        return []
    names = {0xE0: "APP0/JFIF", 0xE1: "APP1/EXIF-or-XMP", 0xE2: "APP2/ICC", 0xED: "APP13/IPTC-Photoshop", 0xEE: "APP14/Adobe", 0xFE: "COM"}
    segments: list[dict[str, Any]] = []
    offset = 2
    while offset + 4 <= len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            segments.append({"marker": "SOS", "offset": offset - 2})
            break
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        payload = data[offset + 2:offset + length]
        segments.append({
            "marker": names.get(marker, f"0xFF{marker:02X}"),
            "offset": offset - 2,
            "payload_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
        offset += length
    return segments


def _icc_details(profile: bytes | None) -> Any:
    if not profile:
        return None
    details: dict[str, Any] = _safe(profile)
    try:
        parsed = ImageCms.ImageCmsProfile(BytesIO(profile))
        details["profile_name"] = ImageCms.getProfileName(parsed).strip()
        details["profile_description"] = ImageCms.getProfileDescription(parsed).strip()
        details["profile_info"] = ImageCms.getProfileInfo(parsed).strip()
    except (OSError, TypeError, ValueError):
        details["parse_status"] = "unavailable"
    return details


def _alternate_streams(path: Path) -> dict[str, Any]:
    if os.name != "nt":
        return {}
    result: dict[str, Any] = {}
    for name in ("Zone.Identifier",):
        try:
            data = Path(f"{path}:{name}").read_bytes()
        except OSError:
            continue
        result[name] = _safe(data)
    return result


def inspect_image_metadata(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    stat = source.stat()
    with Image.open(source) as opened:
        raw_exif = opened.getexif()
        exif = {
            ExifTags.TAGS.get(tag, str(tag)): _safe(value)
            for tag, value in raw_exif.items()
        }
        exif_ifds: dict[str, Any] = {}
        for ifd_name in ("Exif", "GPSInfo", "Interop", "IFD1"):
            ifd_id = getattr(ExifTags.IFD, ifd_name, None)
            if ifd_id is None:
                continue
            try:
                values = raw_exif.get_ifd(ifd_id)
            except (KeyError, OSError, TypeError, ValueError):
                continue
            names = ExifTags.GPSTAGS if ifd_name == "GPSInfo" else ExifTags.TAGS
            if values:
                exif_ifds[ifd_name] = {names.get(tag, str(tag)): _safe(value) for tag, value in values.items()}
        embedded = {
            str(key): _safe(value)
            for key, value in opened.info.items()
            if key not in {"exif", "icc_profile"}
        }
        icc = opened.info.get("icc_profile")
        iptc = getiptcinfo(opened)
        quantization = getattr(opened, "quantization", None)
        normalized_size = ImageOps.exif_transpose(opened).size
        return {
            "path": str(source),
            "filename": source.name,
            "sha256": _sha256(source),
            "file_size_bytes": stat.st_size,
            "filesystem_modified_utc": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(),
            "filesystem_created_utc": datetime.fromtimestamp(
                stat.st_ctime, timezone.utc
            ).isoformat(),
            "format": opened.format,
            "mode": opened.mode,
            "stored_width": opened.width,
            "stored_height": opened.height,
            "orientation_normalized_width": normalized_size[0],
            "orientation_normalized_height": normalized_size[1],
            "exif": exif,
            "exif_ifds": exif_ifds,
            "embedded_info": embedded,
            "icc_profile": _icc_details(icc),
            "iptc": _safe(iptc) if iptc else None,
            "jpeg_quantization_tables": len(quantization or {}),
            "jpeg_segments": _jpeg_segments(source),
            "alternate_data_streams": _alternate_streams(source),
            "frame_count": int(getattr(opened, "n_frames", 1)),
        }


def compare_metadata(
    original: Mapping[str, Any], filtered: Mapping[str, Any]
) -> dict[str, Any]:
    ignored = {"path", "filename", "sha256", "filesystem_modified_utc"}
    shared: dict[str, Any] = {}
    different: dict[str, Any] = {}
    for key in sorted(set(original) | set(filtered)):
        if key in ignored:
            continue
        left, right = original.get(key), filtered.get(key)
        if left == right:
            shared[key] = left
        else:
            different[key] = {"original": left, "filtered": right}
    return {"similarities": shared, "differences": different}


def _table_rows(values: Mapping[str, Any]) -> str:
    return "".join(
        f"<tr><th>{html.escape(str(key))}</th><td><pre>{html.escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre></td></tr>"
        for key, value in values.items()
    )


def _flatten_metadata(values: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flattened.update(_flatten_metadata(value, name))
        else:
            flattened[name] = value
    return flattened


def _comparison_matrix(images: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    flattened = [(label, _flatten_metadata(metadata)) for label, metadata in images]
    keys = sorted({key for _, values in flattened for key in values})
    header = "".join(f"<th>{html.escape(label)}</th>" for label, _ in flattened)
    rows = []
    for key in keys:
        cells = []
        for _, values in flattened:
            value = values.get(key, "—")
            cells.append(f"<td><pre>{html.escape(json.dumps(value, ensure_ascii=False))}</pre></td>")
        rows.append(f"<tr><th>{html.escape(key)}</th>{''.join(cells)}</tr>")
    return f"<thead><tr><th>Propriété</th>{header}</tr></thead><tbody>{''.join(rows)}</tbody>"


def _visual_summary_rows(comparisons: Mapping[str, Any]) -> str:
    return "".join(
        "<tr>"
        f"<th>{html.escape(name.replace('_', ' '))}</th>"
        f"<td>{'oui' if values['visual_similarity']['sha256_equal'] else 'non'}</td>"
        f"<td>{values['visual_similarity']['phash']['distance']}/64</td>"
        f"<td>{values['visual_similarity']['dhash']['distance']}/64</td>"
        f"<td>{values['visual_similarity']['whash']['distance']}/64</td>"
        f"<td><strong>{html.escape(values['visual_similarity']['verdict'])}</strong></td>"
        f"<td>{html.escape(values['visual_similarity']['reason'])}</td>"
        "</tr>"
        for name, values in comparisons.items()
    )


def _pair_sections(comparisons: Mapping[str, Any], metadata_only: bool) -> str:
    sections = []
    for name, values in comparisons.items():
        visual = ""
        if not metadata_only:
            visual = f'<h3>Comparaison visuelle</h3><table>{_table_rows(values["visual_similarity"])}</table>'
        sections.append(
            f'<section><h2>{html.escape(name.replace("_", " "))}</h2>{visual}'
            f'<h3>Similitudes de métadonnées</h3><table>{_table_rows(values["metadata"]["similarities"])}</table>'
            f'<h3>Différences de métadonnées</h3><table>{_table_rows(values["metadata"]["differences"])}</table></section>'
        )
    return "".join(sections)


def generate_metadata_report(
    original_path: str | Path,
    filtered_path: str | Path,
    output_dir: str | Path,
    additional_path: str | Path | Sequence[str | Path] | None = None,
    metadata_only: bool = False,
) -> tuple[Path, Path]:
    original_source = Path(original_path).resolve()
    filtered_source = Path(filtered_path).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    assets = destination / "assets"
    assets.mkdir(exist_ok=True)
    original_asset = assets / f"original{original_source.suffix.lower()}"
    filtered_asset = assets / f"filtered{filtered_source.suffix.lower()}"
    shutil.copy2(original_source, original_asset)
    shutil.copy2(filtered_source, filtered_asset)
    if additional_path is None:
        additional_sources: list[Path] = []
    elif isinstance(additional_path, (str, Path)):
        additional_sources = [Path(additional_path).resolve()]
    else:
        additional_sources = [Path(value).resolve() for value in additional_path]
    additional_assets: list[Path] = []
    for index, source in enumerate(additional_sources, 1):
        asset = assets / f"additional_{index:02d}{source.suffix.lower()}"
        shutil.copy2(source, asset)
        additional_assets.append(asset)

    original = inspect_image_metadata(original_source)
    filtered = inspect_image_metadata(filtered_source)
    comparison = compare_metadata(original, filtered)
    additional_images = [inspect_image_metadata(source) for source in additional_sources]
    named_images = [("original", original_source, original), ("filtered", filtered_source, filtered)] + [
        (f"additional_{index:02d}", source, metadata)
        for index, (source, metadata) in enumerate(zip(additional_sources, additional_images), 1)
    ]
    pair_comparisons: dict[str, Any] = {}
    for left_index, (left_name, left_path, left_metadata) in enumerate(named_images):
        for right_name, right_path, right_metadata in named_images[left_index + 1:]:
            values: dict[str, Any] = {"metadata": compare_metadata(left_metadata, right_metadata)}
            if not metadata_only:
                values["visual_similarity"] = compare_images(left_path, right_path).as_dict()
            pair_comparisons[f"{left_name}_vs_{right_name}"] = values
    matrix_images = [("Originale O18", original), ("Variante filtrée", filtered)] + [
        (source.name, metadata) for source, metadata in zip(additional_sources, additional_images)
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "original": original,
        "filtered": filtered,
        "comparison": comparison,
        "additional": additional_images[0] if additional_images else None,
        "additional_images": additional_images,
        "pair_comparisons": pair_comparisons,
        "metadata_only": metadata_only,
    }
    json_path = destination / "metadata_report.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    report = destination / "index.html"
    report.write_text(
        f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Diagnostic de métadonnées</title><style>
body{{font:14px system-ui;background:#f3f4f6;color:#111827;margin:1.5rem}}main{{max-width:1500px;margin:auto}}section{{background:white;padding:1rem;margin:1rem 0;border-radius:10px}}.images{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}figure{{margin:0}}img{{width:100%;height:520px;object-fit:contain;background:#eee}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #d1d5db;padding:.5rem;text-align:left;vertical-align:top}}th{{width:25%}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}}@media(max-width:850px){{.images{{grid-template-columns:1fr}}}}</style></head><body><main>
<h1>Diagnostic comparé des métadonnées</h1><p>Lecture seule : aucun fichier source n'a été modifié. Les dates du système de fichiers sont affichées séparément des métadonnées intégrées.</p>
<section class="images"><figure><figcaption><strong>Originale O18 image 0</strong></figcaption><img src="{html.escape(original_asset.relative_to(destination).as_posix())}" alt="originale"></figure><figure><figcaption><strong>Variante filtrée</strong></figcaption><img src="{html.escape(filtered_asset.relative_to(destination).as_posix())}" alt="filtrée"></figure>{''.join(f'<figure><figcaption><strong>{html.escape(source.name)}</strong></figcaption><img src="{html.escape(asset.relative_to(destination).as_posix())}" alt="photo supplémentaire"></figure>' for source, asset in zip(additional_sources, additional_assets))}</section>
{'' if metadata_only else f'<section><h2>Résumé simplifié des comparaisons visuelles</h2><p>Une distance de 0/64 signifie que le hash perceptuel voit les images comme identiques. Le SHA-256 exige que les fichiers soient strictement identiques octet par octet.</p><table><thead><tr><th>Paire</th><th>SHA identique</th><th>pHash</th><th>dHash</th><th>wHash</th><th>Verdict</th><th>Explication</th></tr></thead><tbody>{_visual_summary_rows(pair_comparisons)}</tbody></table></section>'}
<section><h2>Tableau comparatif complet</h2><p>Toutes les propriétés détectées sont réunies ici. « — » signifie que la propriété n'existe pas dans ce fichier. Les sections suivantes conservent la représentation technique détaillée.</p><table>{_comparison_matrix(matrix_images)}</table></section>
<section><h2>Similitudes</h2><table>{_table_rows(comparison['similarities'])}</table></section>
<section><h2>Différences</h2><table>{_table_rows(comparison['differences'])}</table></section>
{_pair_sections(pair_comparisons, metadata_only)}
<section><h2>Métadonnées complètes de l'originale</h2><table>{_table_rows(original)}</table></section>
<section><h2>Métadonnées complètes de la variante</h2><table>{_table_rows(filtered)}</table></section>
{''.join(f'<section><h2>Métadonnées complètes de {html.escape(source.name)}</h2><table>{_table_rows(metadata)}</table></section>' for source, metadata in zip(additional_sources, additional_images))}
</main></body></html>''',
        encoding="utf-8",
    )
    return report, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare image metadata without modifying either source file.")
    parser.add_argument("--original", required=True)
    parser.add_argument("--filtered", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--additional", action="append", default=[])
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args(argv)
    report, payload = generate_metadata_report(
        args.original, args.filtered, args.output, args.additional, metadata_only=args.metadata_only
    )
    print(f"report={report}")
    print(f"json={payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
