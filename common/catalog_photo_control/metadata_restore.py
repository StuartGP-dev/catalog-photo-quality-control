from __future__ import annotations

import argparse
import html
import json
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageCms, ImageOps
import piexif

from .metadata_diagnostic import _comparison_matrix, _flatten_metadata, inspect_image_metadata


SOFTWARE_TAG = "Catalog Photo Control; pixels transformed from a filtered catalogue image"
COMPOSITE_IMAGE_TAG = 42080
OUTPUT_DPI = 300

# piexif predates this EXIF 2.32 tag, but can preserve it once its type is known.
piexif.TAGS["Exif"].setdefault(COMPOSITE_IMAGE_TAG, {"name": "CompositeImage", "type": 3})


def _ensure_jfif_300_dpi(path: Path) -> None:
    data = path.read_bytes()
    jfif = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x01\x2c\x01\x2c\x00\x00"
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"not a JPEG file: {path}")
    if data[2:4] == b"\xff\xe0" and data[6:11] == b"JFIF\x00":
        segment_length = int.from_bytes(data[4:6], "big")
        data = data[:2] + jfif + data[4 + segment_length:]
    else:
        data = data[:2] + jfif + data[2:]
    path.write_bytes(data)


def _copy_raw_mpf(reference: Path, output: Path) -> None:
    """Copy the reference MPF block verbatim; its image offsets remain reference-specific."""
    with Image.open(reference) as reference_opened:
        mp = reference_opened.info.get("mp")
    if not mp:
        return
    data = output.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"not a JPEG file: {output}")
    payload = b"MPF\x00" + mp
    segment = b"\xff\xe2" + (len(payload) + 2).to_bytes(2, "big") + payload
    insert_at = 2
    if data[2:4] == b"\xff\xe0":
        insert_at = 4 + int.from_bytes(data[4:6], "big")
    output.write_bytes(data[:insert_at] + segment + data[insert_at:])


def _copy_zone_identifier(reference: Path, output: Path) -> None:
    """Copy the Windows download-origin alternate data stream when available."""
    try:
        payload = Path(f"{reference}:Zone.Identifier").read_bytes()
        Path(f"{output}:Zone.Identifier").write_bytes(payload)
    except OSError:
        # Alternate data streams are unavailable on non-NTFS filesystems.
        return


def _remove_zone_identifier(output: Path) -> None:
    """Remove a stale Windows download-origin stream from the generated output."""
    try:
        Path(f"{output}:Zone.Identifier").unlink()
    except OSError:
        return


def _scaled_subject_area(
    value: object,
    reference_size: tuple[int, int],
    output_size: tuple[int, int],
) -> tuple[int, ...] | None:
    if not isinstance(value, (tuple, list)) or len(value) not in (2, 3, 4):
        return None
    ref_width, ref_height = reference_size
    out_width, out_height = output_size
    scaled = []
    for index, item in enumerate(value):
        if not isinstance(item, (int, float)):
            return None
        axis_scale = out_width / ref_width if index % 2 == 0 else out_height / ref_height
        scaled.append(max(1, round(item * axis_scale)))
    return tuple(scaled)


def restore_technical_metadata(
    source_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    capture_metadata_path: str | Path | None = None,
    strip_capture_metadata: bool = False,
    copy_reference_specific_metadata: bool = False,
    software_tag: str = SOFTWARE_TAG,
    capture_overrides: Mapping[int, object] | None = None,
    image_overrides: Mapping[int, object] | None = None,
) -> Path:
    """Create a new image with technical metadata selected from the reference."""
    source = Path(source_path).resolve()
    reference = Path(reference_path).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    capture_source = Path(capture_metadata_path).resolve() if capture_metadata_path else None
    with (
        Image.open(source) as source_opened,
        Image.open(reference) as reference_opened,
        Image.open(capture_source) if capture_source else Image.open(source) as capture_opened,
    ):
        image = ImageOps.exif_transpose(source_opened).convert("RGB")
        source_profile = source_opened.info.get("icc_profile")
        target_profile = reference_opened.info.get("icc_profile")
        if target_profile:
            input_profile = (
                ImageCms.ImageCmsProfile(BytesIO(source_profile))
                if source_profile
                else ImageCms.createProfile("sRGB")
            )
            image = ImageCms.profileToProfile(
                image,
                input_profile,
                ImageCms.ImageCmsProfile(BytesIO(target_profile)),
                outputMode="RGB",
            )

        exif = Image.Exif() if strip_capture_metadata else capture_opened.getexif()
        # GPS is intentionally omitted even when it exists in the capture source.
        if 34853 in exif:
            del exif[34853]
        exif[274] = 1  # Orientation: pixels are already normalized.
        exif[282] = OUTPUT_DPI
        exif[283] = OUTPUT_DPI
        exif[296] = 2  # inches
        exif[305] = software_tag
        exif[531] = 1  # centered YCbCr, like the reference iPhone file
        exif[306] = datetime.now().astimezone().strftime("%Y:%m:%d %H:%M:%S")
        if image_overrides:
            exif.update(image_overrides)
        if capture_source:
            exif_ifd = exif.get_ifd(34665)
            # These values describe the rendered JPEG, not the capture source.
            exif_ifd[40962] = image.width
            exif_ifd[40963] = image.height
            if copy_reference_specific_metadata:
                subject = exif_ifd.get(37396) or exif_ifd.get(41492)
                scaled_subject = _scaled_subject_area(
                    subject, capture_opened.size, image.size
                )
                exif_ifd.pop(41492, None)
                if scaled_subject:
                    exif_ifd[37396] = scaled_subject
            else:
                # Apple MakerNote and subject coordinates are tied to the
                # reference sensor data and dimensions.
                exif_ifd.pop(37500, None)
                exif_ifd.pop(37396, None)
                exif_ifd.pop(41492, None)
            if capture_overrides:
                exif_ifd.update(capture_overrides)
        image.save(
            output,
            format="JPEG",
            quality=95,
            subsampling=0,
            dpi=(300, 300),  # Match the reference JFIF header; EXIF remains 72 dpi.
            exif=exif,
            icc_profile=target_profile,
        )
        thumbnail = image.copy()
        thumbnail.thumbnail((160, 160), Image.Resampling.LANCZOS)
        thumbnail_buffer = BytesIO()
        thumbnail.save(thumbnail_buffer, format="JPEG", quality=85)
        exif_dict = piexif.load(str(output))
        for tag in (36864, 37121, 37500, 40960, 41728, 41729):
            value = exif_dict["Exif"].get(tag)
            if isinstance(value, tuple) and all(isinstance(item, int) for item in value):
                exif_dict["Exif"][tag] = bytes(value)
            elif tag in (41728, 41729) and isinstance(value, int):
                exif_dict["Exif"][tag] = bytes([value])
        exif_dict["thumbnail"] = thumbnail_buffer.getvalue()
        exif_dict["1st"].update({
            piexif.ImageIFD.Compression: 6,
            piexif.ImageIFD.XResolution: (OUTPUT_DPI, 1),
            piexif.ImageIFD.YResolution: (OUTPUT_DPI, 1),
            piexif.ImageIFD.ResolutionUnit: 2,
        })
        piexif.insert(piexif.dump(exif_dict), str(output))
        _ensure_jfif_300_dpi(output)
        if copy_reference_specific_metadata:
            _copy_raw_mpf(reference, output)
            _copy_zone_identifier(reference, output)
        else:
            _remove_zone_identifier(output)
    return output


def _change_table(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    left = _flatten_metadata(before)
    right = _flatten_metadata(after)
    rows = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key, "—")
        new = right.get(key, "—")
        if old == new:
            change = "inchangé"
        elif old == "—":
            change = "ajouté"
        elif new == "—":
            change = "retiré"
        else:
            change = "modifié"
        rows.append(
            f"<tr><th>{html.escape(key)}</th>"
            f"<td><pre>{html.escape(json.dumps(old, ensure_ascii=False))}</pre></td>"
            f"<td><pre>{html.escape(json.dumps(new, ensure_ascii=False))}</pre></td>"
            f"<td>{change}</td></tr>"
        )
    return "".join(rows)


def generate_restoration_report(
    source_path: str | Path,
    restored_path: str | Path,
    reference_path: str | Path,
    output_dir: str | Path,
    original_path: str | Path | None = None,
    two_image_comparison: bool = False,
    capture_metadata_restored: bool = False,
) -> Path:
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    assets = destination / "assets"
    assets.mkdir(exist_ok=True)
    before_asset = assets / "before.jpg"
    after_asset = assets / "after.jpg"
    reference_asset = assets / "reference.jpg"
    shutil.copy2(source_path, before_asset)
    shutil.copy2(restored_path, after_asset)
    shutil.copy2(reference_path, reference_asset)
    before = inspect_image_metadata(source_path)
    after = inspect_image_metadata(restored_path)
    reference = inspect_image_metadata(reference_path)
    original = inspect_image_metadata(original_path) if original_path else None
    payload = {"original": original, "before": before, "after": after, "reference": reference}
    (destination / "metadata_changes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    report = destination / "index.html"
    if two_image_comparison:
        matrix_images = [
            ("Photo filtrée O18", after),
            ("IMG_3206.jpg", reference),
        ]
        image_panel = '<figure><figcaption><strong>Photo filtrée O18</strong></figcaption><img src="assets/after.jpg"></figure><figure><figcaption><strong>IMG_3206.jpg</strong></figcaption><img src="assets/reference.jpg"></figure>'
    else:
        matrix_images = []
        if original:
            matrix_images.append(("Originale O18", original))
        matrix_images.extend([
            ("Variante filtrée — avant", before),
            ("Variante filtrée — après", after),
            ("IMG_3206.jpg — référence iPhone 15", reference),
        ])
        image_panel = '<figure><figcaption><strong>Avant</strong></figcaption><img src="assets/before.jpg"></figure><figure><figcaption><strong>Après</strong></figcaption><img src="assets/after.jpg"></figure>'
    report.write_text(
        f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Métadonnées avant/après</title><style>
body{{font:14px system-ui;background:#f3f4f6;margin:1.5rem;color:#111827}}main{{max-width:1500px;margin:auto}}section{{background:#fff;padding:1rem;margin:1rem 0;border-radius:10px}}.images{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}img{{width:100%;height:520px;object-fit:contain;background:#eee}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #d1d5db;padding:.5rem;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}}@media(max-width:850px){{.images{{grid-template-columns:1fr}}}}</style></head><body><main>
<h1>Variante filtrée — métadonnées avant/après</h1>
<section><p>{"La copie « après » utilise le profil Display P3 et restaure les métadonnées de capture compatibles de la référence iPhone 15 (appareil, objectif et réglages de prise de vue). Le GPS, les coordonnées de sujet et les notes constructeur liées au fichier de référence sont exclus." if capture_metadata_restored else "La copie « après » utilise le profil Display P3 et la résolution technique de la référence iPhone. Les pixels ont été convertis correctement vers ce profil. Les champs de provenance de prise de vue — appareil, objectif, date et GPS — ne sont pas falsifiés."}</p></section>
<section class="images">{image_panel}</section>
<section><h2>Complétude des métadonnées</h2><table><thead><tr><th>Groupe</th><th>État</th><th>Explication</th></tr></thead><tbody>
<tr><th>Miniature EXIF IFD1</th><td>générée</td><td>Miniature JPEG réelle, avec compression et résolution cohérentes.</td></tr>
<tr><th>GPS</th><td>exclu</td><td>Suppression volontaire demandée.</td></tr>
<tr><th>Apple MakerNote / SubjectLocation</th><td>exclus</td><td>Données privées dépendantes du capteur et des dimensions de la prise de vue.</td></tr>
<tr><th>MP/MPO</th><td>non applicable</td><td>La variante est un JPEG simple et ne contient pas une seconde prise de vue.</td></tr>
<tr><th>Zone.Identifier</th><td>non applicable</td><td>Flux Windows externe au fichier image, créé éventuellement lors d'un téléchargement.</td></tr>
</tbody></table></section>
<section><h2>Tableau comparatif complet des métadonnées</h2><p>Il reprend la matrice du rapport précédent et ajoute la variante après traitement. « — » signifie que la propriété est absente.</p><table>{_comparison_matrix(matrix_images)}</table></section>
{'' if two_image_comparison else f'<section><h2>Tableau des modifications</h2><table><thead><tr><th>Propriété</th><th>Avant</th><th>Après</th><th>Modification</th></tr></thead><tbody>{_change_table(before, after)}</tbody></table></section>'}
</main></body></html>''', encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore safe technical metadata on a filtered image copy.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--original")
    parser.add_argument("--capture-metadata")
    parser.add_argument("--two-image-report", action="store_true")
    args = parser.parse_args(argv)
    restored = restore_technical_metadata(
        args.source, args.reference, args.output, capture_metadata_path=args.capture_metadata
    )
    report = generate_restoration_report(
        args.source,
        restored,
        args.reference,
        args.report_dir,
        original_path=args.original,
        two_image_comparison=args.two_image_report,
        capture_metadata_restored=bool(args.capture_metadata),
    )
    print(f"image={restored}")
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
