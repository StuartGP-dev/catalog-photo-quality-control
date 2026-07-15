from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from common.catalog_photo_control.bench import build_parser, classify_stop_reason, run_benchmark


def test_every_stop_reason_is_classified() -> None:
    common = dict(
        selected=0,
        target=2,
        tests=0,
        max_tests=10,
        elapsed_seconds=0,
        max_duration_seconds=60,
        stale=0,
        patience=5,
    )
    assert classify_stop_reason(**{**common, "selected": 2}) == "target_reached"
    assert classify_stop_reason(**{**common, "tests": 10}) == "max_tests_reached"
    assert classify_stop_reason(**{**common, "elapsed_seconds": 60}) == "max_duration_reached"
    assert classify_stop_reason(**{**common, "stale": 5}) == "patience_exhausted"
    assert classify_stop_reason(**common, interrupted=True) == "interrupted"
    assert classify_stop_reason(**common, error=True) == "error"


def test_cli_generates_exactly_one_html_with_all_selected_images(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    args = build_parser().parse_args(
        [
            "--listing", str(synthetic_listing),
            "--local-root", str(tmp_path / "local"),
            "--target-variants", "2",
            "--max-tests", "20",
            "--max-duration-minutes", "1",
            "--patience", "20",
            "--seed", "7",
            "--quiet",
        ]
    )

    stop_reason, report, counters = run_benchmark(args)

    assert stop_reason == "target_reached"
    assert counters["obtained"] == 2
    assert counters["tested"] >= 6
    assert list((tmp_path / "local").rglob("*.html")) == [report]
    content = report.read_text(encoding="utf-8")
    assert content.count("<article class=\"variant\">") == 2
    assert content.count("<img ") == 4
    assert "stop reason: <strong>target_reached</strong>" in content.lower()
    assert "Recipe families tested:" in content
    assert "Recipe families valid:" in content
    assert "Recipe families selected:" in content
    assert content.count("Recipe family: ") == 2
    selected_family_total = sum(
        value for key, value in counters.items() if key.startswith("family_selected_")
    )
    assert selected_family_total == counters["obtained"]
    with sqlite3.connect(tmp_path / "local" / "databases" / "catalog_variants.sqlite3") as connection:
        payloads = [json.loads(row[0]) for row in connection.execute("SELECT nearest_same_listing_json FROM listing_variant_images") if row[0] != "{}"]
        assert payloads
        assert {"sha256_equal", "phash", "dhash", "whash", "verdict", "reason", "listing_id", "variant_id", "image_index"} <= payloads[0].keys()
    with sqlite3.connect(tmp_path / "local" / "databases" / "catalog_bench.sqlite3") as connection:
        row = connection.execute("SELECT phash_distance, phash_band, dhash_distance, dhash_band, whash_distance, whash_band, verdict, reason, engine_version FROM perceptual_comparisons LIMIT 1").fetchone()
        assert row is not None and all(value is not None for value in row)
