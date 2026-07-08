from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .photo_comparison_rules import compare_photo_pair
from .photo_adjustments import generate_quality_photo_adjustments
from .local_paths import default_annonces_root, default_output_root

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _safe_listing_code(listing_code: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", listing_code.strip().replace("\\", "/"))


def _parent_folder_from_code(code: str) -> str:
    code = (code or "").strip()
    if not code:
        return ""
    head = code.split("-", 1)[0].strip()
    if not head:
        return ""
    match = re.match(r"^[A-Za-z]+", head)
    if match:
        return match.group(0).upper()
    return head[:1].upper()


def resolve_listing_dir(listing_code: str, annonces_root: str | Path | None = None) -> Path:
    normalized = listing_code.strip().replace("\\", "/").strip("/")
    if "/" not in normalized:
        raise ValueError("Le code annonce doit inclure le mode, par exemple bijoux/O18.")

    parts = [part for part in normalized.split("/") if part]
    root = Path(annonces_root) if annonces_root is not None else default_annonces_root()

    direct = root.joinpath(*parts)
    if direct.is_dir():
        return direct

    mode = parts[0]
    code = parts[-1]
    dynamic = root / mode / _parent_folder_from_code(code) / code
    if dynamic.is_dir():
        return dynamic

    raise FileNotFoundError(
        f"Dossier annonce introuvable pour {listing_code!r}. "
        f"Chemins testes: {direct} puis {dynamic}."
    )


def _numbered_primary_image_files(listing_dir: Path) -> list[Path]:
    """Return only direct listing images named 0..N, ignoring helper files/folders.

    The production annonce images are expected to be direct files named like
    0.jpg, 1.jpg, 2.png...  Subfolders such as "autre" / "autres" are not
    traversed by this function, and non-numbered files in the listing folder are
    ignored.
    """
    numbered: list[tuple[int, Path]] = []
    seen_numbers: dict[int, Path] = {}

    for path in listing_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if not path.stem.isdigit():
            continue

        index = int(path.stem)
        if index in seen_numbers:
            other = seen_numbers[index]
            raise ValueError(
                f"Images annonce dupliquees pour l'index {index}: {other.name} et {path.name} dans {listing_dir}"
            )
        seen_numbers[index] = path
        numbered.append((index, path))

    numbered.sort(key=lambda item: item[0])
    indexes = [index for index, _ in numbered]
    expected = list(range(len(indexes)))
    if indexes and indexes != expected:
        missing = sorted(set(range(indexes[-1] + 1)) - set(indexes))
        raise ValueError(
            f"Images annonce non contigues dans {listing_dir}. "
            f"Trouve: {indexes}. Attendu: 0..{len(indexes) - 1}. Manquants: {missing}"
        )

    return [path for _, path in numbered]


def find_listing_images(listing_dir: str | Path) -> list[Path]:
    listing_dir = Path(listing_dir)
    return _numbered_primary_image_files(listing_dir)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _markdown_report(report: dict[str, Any]) -> str:
    status_counts = report["summary"]["statuses"]
    lines = [
        f"# Audit controle qualite images - {report['listing_code']}",
        "",
        f"- Annonce testee: `{report['listing_code']}`",
        f"- Dossier annonce: `{report['listing_dir']}`",
        f"- Sensibilite: `{report['sensitivity']}`",
        f"- Images originales trouvees: {report['summary']['original_images']}",
        f"- Ajustements generes: {report['summary']['photo_adjustments_total']}",
        f"- Statuts: match={status_counts.get('match', 0)}, "
        f"review={status_counts.get('review', 0)}, clear={status_counts.get('clear', 0)}",
        "",
        "## Detail par image",
        "",
        "| Image source | Ajustement | Statut | Raison | pHash | dHash | wHash | EXIF |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    clear_rows = []
    for item in report["comparisons"]:
        visual = item["comparison"]["visual_check"]["hashes"]
        exif_status = item["comparison"]["metadata_check"]["metadata_status"]
        row = (
            f"| `{item['source_image']}` | `{item['photo_adjustment']['adjustment_name']}` | "
            f"{item['comparison']['overall_check']['status']} | "
            f"{item['comparison']['overall_check']['reason']} | "
            f"{visual['phash']['hamming_distance']} ({visual['phash']['band']}) | "
            f"{visual['dhash']['hamming_distance']} ({visual['dhash']['band']}) | "
            f"{visual['whash']['hamming_distance']} ({visual['whash']['band']}) | "
            f"{exif_status} |"
        )
        lines.append(row)
        if item["comparison"]["overall_check"]["status"] == "clear":
            clear_rows.append(item)

    lines.extend(["", "## Cas de reference a revoir", ""])
    if not clear_rows:
        lines.append("Aucun ajustement classe clear dans cet audit.")
    else:
        lines.append(
            "Les cas ci-dessous sont des ajustements a etudier uniquement pour renforcer le controle qualite."
        )
        lines.append("")
        for item in clear_rows:
            lines.append(
                f"- `{item['source_image']}` -> `{item['photo_adjustment']['adjustment_name']}`: "
                f"{item['comparison']['reason']}"
            )

    lines.extend(
        [
            "",
            "## Limites",
            "",
            "- Cet outil local sert uniquement au controle qualite interne du catalogue.",
            "- Les seuils doivent etre calibres sur un dataset reel du projet.",
            "- Les metadonnees EXIF ne servent qu'a corroborer; leur absence n'augmente pas le risque.",
        ]
    )
    return "\n".join(lines) + "\n"


def audit_listing_images(
    listing_code: str,
    annonces_root: str | Path | None = None,
    output_root: str | Path | None = None,
    preset: str = "default",
    sensitivity: str = "standard",
    keep_photo_adjustments: bool = True,
) -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    if not images:
        raise FileNotFoundError(f"Aucune image numerotee 0..N trouvee dans le dossier annonce: {listing_dir}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_output_root = Path(output_root) if output_root is not None else default_output_root()
    output_dir = local_output_root / _safe_listing_code(listing_code) / timestamp
    photo_adjustments_dir = output_dir / "photo_adjustments"
    output_dir.mkdir(parents=True, exist_ok=True)

    comparisons: list[dict[str, Any]] = []
    for image_path in images:
        photo_photo_adjustments_dir = photo_adjustments_dir / image_path.stem
        adjustments = generate_quality_photo_adjustments(image_path, photo_photo_adjustments_dir, preset=preset)
        for adjustment in adjustments:
            comparison = compare_photo_pair(image_path, adjustment["path"], sensitivity=sensitivity)
            comparisons.append(
                {
                    "source_image": str(image_path),
                    "photo_adjustment": adjustment,
                    "comparison": comparison,
                }
            )

    counts = Counter(item["comparison"]["overall_check"]["status"] for item in comparisons)
    report = {
        "listing_code": listing_code,
        "listing_dir": str(listing_dir),
        "preset": preset,
        "sensitivity": sensitivity,
        "output_dir": str(output_dir),
        "reports": {
            "json": str(output_dir / "audit_report.json"),
            "markdown": str(output_dir / "audit_report.md"),
        },
        "summary": {
            "original_images": len(images),
            "photo_adjustments_total": len(comparisons),
            "statuses": dict(counts),
        },
        "comparisons": comparisons,
    }

    _write_json(output_dir / "audit_report.json", report)
    (output_dir / "audit_report.md").write_text(_markdown_report(report), encoding="utf-8")

    if not keep_photo_adjustments and photo_adjustments_dir.exists():
        shutil.rmtree(photo_adjustments_dir)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local de controle qualite des images.")
    parser.add_argument("--listing", required=True, help="Code annonce, par exemple bijoux/O18.")
    parser.add_argument("--annonces-root", default=str(default_annonces_root()), help="Racine externe des annonces catalogue.")
    parser.add_argument("--output-root", default=str(default_output_root()), help="Dossier local du repo pour rapports, catalogues JSON et bundles debug.")
    parser.add_argument("--preset", choices=("light", "default", "extended"), default="default")
    parser.add_argument("--sensitivity", choices=("standard", "wide"), default="standard")
    parser.add_argument(
        "--keep-photo-adjustments",
        action="store_true",
        default=True,
        help="Conserver les ajustements generes pour inspection visuelle (defaut).",
    )
    args = parser.parse_args(argv)

    try:
        report = audit_listing_images(
            args.listing,
            annonces_root=args.annonces_root,
            output_root=args.output_root,
            preset=args.preset,
            sensitivity=args.sensitivity,
            keep_photo_adjustments=args.keep_photo_adjustments,
        )
    except Exception as exc:
        print(f"Erreur: {exc}")
        return 1

    print(f"Rapport JSON: {report['reports']['json']}")
    print(f"Rapport Markdown: {report['reports']['markdown']}")
    print(f"Dossier debug: {report['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
