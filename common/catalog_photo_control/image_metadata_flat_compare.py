from __future__ import annotations

import argparse
import csv
import json
import warnings
from pathlib import Path
from typing import Any

from .image_metadata_inspector import compare_metadata, inspect_export_dir


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


def _short(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _relation(original: str, filtered: str) -> str:
    if original == filtered:
        return "SAME"
    if original == "":
        return "FILTERED_ONLY"
    if filtered == "":
        return "ORIGINAL_ONLY"
    return "DIFF"


def _comparison_rows(comparison: dict[str, Any], pair_index: int) -> list[dict[str, str]]:
    left_flat = _flatten(comparison.get("original", {}))
    right_flat = _flatten(comparison.get("filtered", {}))
    keys = sorted(set(left_flat) | set(right_flat))
    rows: list[dict[str, str]] = []
    for key in keys:
        original = left_flat.get(key, "")
        filtered = right_flat.get(key, "")
        rows.append(
            {
                "pair_index": str(pair_index),
                "relation": _relation(original, filtered),
                "key": key,
                "original": original,
                "filtered": filtered,
            }
        )
    return rows


def build_flat_report(report: dict[str, Any]) -> list[dict[str, str]]:
    comparisons = report.get("comparisons") or [report]
    rows: list[dict[str, str]] = []
    for idx, comparison in enumerate(comparisons, start=1):
        rows.extend(_comparison_rows(comparison, idx))
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pair_index", "relation", "key", "original", "filtered"])
        writer.writeheader()
        writer.writerows(rows)


def format_text(rows: list[dict[str, str]], *, max_value_len: int = 260, relation_filter: str = "all") -> str:
    relation_filter = relation_filter.upper()
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if relation_filter != "ALL" and row["relation"] != relation_filter:
            continue
        grouped.setdefault(row["pair_index"], []).append(row)

    lines: list[str] = ["FLAT IMAGE METADATA COMPARISON"]
    lines.append(f"rows: {sum(len(items) for items in grouped.values())}")
    lines.append(f"filter: {relation_filter}")
    for pair_index in sorted(grouped, key=lambda value: int(value)):
        pair_rows = grouped[pair_index]
        counts: dict[str, int] = {}
        for row in pair_rows:
            counts[row["relation"]] = counts.get(row["relation"], 0) + 1
        lines.append("")
        lines.append(f"PAIR {pair_index} | counts: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
        for row in pair_rows:
            original = _short(row["original"], max_value_len)
            filtered = _short(row["filtered"], max_value_len)
            lines.append(f"[{row['relation']}] {row['key']}")
            lines.append(f"  original: {original}")
            lines.append(f"  filtered: {filtered}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Affiche simplement toutes les metadonnees original vs filtre a plat.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export-dir", default=None, help="Dossier export contenant selected_filter.json")
    group.add_argument("--original", default=None, help="Image originale unique")
    parser.add_argument("--filtered", default=None, help="Image filtree si --original est utilise")
    parser.add_argument("--use-exiftool", action="store_true", help="Ajoute ExifTool si disponible")
    parser.add_argument("--require-exiftool", action="store_true", help="Echoue si ExifTool est absent")
    parser.add_argument("--exiftool-path", default=None, help="Chemin vers exiftool.exe")
    parser.add_argument("--relation", default="all", choices=["all", "same", "diff", "original_only", "filtered_only"], help="Filtrer l'affichage")
    parser.add_argument("--max-value-len", type=int, default=260, help="Longueur max affichee par valeur. 0 = pas de limite")
    parser.add_argument("--output-csv", default=None, help="CSV plat de toutes les metadonnees")
    parser.add_argument("--output-txt", default=None, help="TXT lisible de toutes les metadonnees")
    parser.add_argument("--no-print", action="store_true", help="N'affiche pas le detail dans le terminal")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    use_exiftool = bool(args.use_exiftool or args.require_exiftool)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="XMP data cannot be read without defusedxml dependency")
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
            default_root = Path(args.filtered).parent
        else:
            export_dir = Path(str(args.export_dir))
            report = inspect_export_dir(
                export_dir,
                use_exiftool=use_exiftool,
                exiftool_path=args.exiftool_path,
                require_exiftool=args.require_exiftool,
            )
            default_root = export_dir

    rows = build_flat_report(report)
    output_csv = Path(args.output_csv) if args.output_csv else default_root / "metadata_flat_all.csv"
    output_txt = Path(args.output_txt) if args.output_txt else default_root / "metadata_flat_all.txt"
    write_csv(rows, output_csv)
    text = format_text(rows, max_value_len=args.max_value_len, relation_filter=args.relation)
    output_txt.write_text(text, encoding="utf-8")

    if not args.no_print:
        print(text, end="")
    print(f"csv: {output_csv}")
    print(f"txt: {output_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
