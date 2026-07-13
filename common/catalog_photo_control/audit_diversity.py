from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import load_filter_space
from .diversity_analysis import analysis_summary, analyze_pairs, load_analysis_images, nearest_pairs, threshold_outcomes, write_analysis_html
from .paths import LocalPaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit the final catalog diversity database without modifying it.")
    parser.add_argument("--scope", choices=("listing", "catalog", "both"), default="both")
    parser.add_argument("--top-nearest", type=int, default=50)
    parser.add_argument("--html")
    parser.add_argument("--local-root", default="local")
    parser.add_argument("--listing-code")
    parser.add_argument("--filter-space", default=str(Path(__file__).resolve().parents[2] / "config" / "filter_space.json"))
    return parser


def run_audit(args: argparse.Namespace) -> dict[str, object]:
    database = LocalPaths.from_root(args.local_root).variants_database.resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    before = database.stat()
    try:
        images = load_analysis_images(connection, args.listing_code)
        pairs = analyze_pairs(images, args.scope)
        summary = analysis_summary(pairs)
        config = load_filter_space(args.filter_space).diversity_gate
        thresholds = sorted({float(config.get("minimum_same_listing_distance", 0)), float(config.get("minimum_catalog_distance", 0))})
        nearest = nearest_pairs(pairs)
        result = {
            "database": str(database),
            "listing_count": len({image.listing_id for image in images}),
            "variant_count": len({(image.listing_id, image.variant_id) for image in images if image.variant_id is not None}),
            "image_count": sum(image.variant_id is not None for image in images),
            "source_image_count": sum(image.variant_id is None for image in images),
            "missing_references": sum(1 for image in images if image.variant_id is not None and not any(pair.candidate == image or pair.reference == image for pair in pairs)),
            "summary": summary,
            "threshold_outcomes": threshold_outcomes(nearest, thresholds),
            "foreign_key_violations": [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")],
        }
        if args.html:
            write_analysis_html(Path(args.html).resolve(), pairs, summary, thresholds, args.top_nearest)
            result["html"] = str(Path(args.html).resolve())
    finally:
        connection.close()
    after = database.stat()
    result["read_only_unchanged"] = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_nearest <= 0:
        print("error: --top-nearest must be positive", file=sys.stderr)
        return 2
    try:
        print(json.dumps(run_audit(args), indent=2, ensure_ascii=False, sort_keys=True))
    except (FileNotFoundError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
