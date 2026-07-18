from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from common.catalog_photo_control.metadata_diagnostic import (
    compare_metadata,
    generate_metadata_report,
    inspect_image_metadata,
)


def test_metadata_diagnostic_is_read_only_and_report_assets_exist(tmp_path: Path) -> None:
    original = tmp_path / "original.jpg"
    filtered = tmp_path / "filtered.jpg"
    additional = tmp_path / "additional.jpg"
    Image.new("RGB", (80, 60), "white").save(original, quality=91, dpi=(72, 72))
    Image.new("RGB", (90, 60), "white").save(filtered, quality=95)
    Image.new("RGB", (70, 70), "white").save(additional, quality=90)
    before = original.read_bytes(), filtered.read_bytes(), additional.read_bytes()

    report, payload_path = generate_metadata_report(original, filtered, tmp_path / "report", additional)

    assert before == (original.read_bytes(), filtered.read_bytes(), additional.read_bytes())
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["original"]["stored_width"] == 80
    assert payload["filtered"]["stored_width"] == 90
    assert "stored_width" in payload["comparison"]["differences"]
    assert payload["additional"]["stored_width"] == 70
    assert "original_vs_additional_01" in payload["pair_comparisons"]
    content = report.read_text(encoding="utf-8")
    assert "Similitudes" in content and "Différences" in content
    assert (report.parent / "assets" / "original.jpg").is_file()
    assert (report.parent / "assets" / "filtered.jpg").is_file()
    assert (report.parent / "assets" / "additional_01.jpg").is_file()


def test_metadata_report_compares_multiple_additional_images(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("original.jpg", "filtered.jpg", "phone.jpg", "transfer.jpg")]
    for index, path in enumerate(paths):
        Image.new("RGB", (40 + index, 30), (index * 30, 10, 10)).save(path)

    report, payload_path = generate_metadata_report(paths[0], paths[1], tmp_path / "report", paths[2:])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    assert len(payload["additional_images"]) == 2
    assert "additional_01_vs_additional_02" in payload["pair_comparisons"]
    assert payload["pair_comparisons"]["additional_01_vs_additional_02"]["visual_similarity"]["verdict"]
    assert (report.parent / "assets" / "additional_01.jpg").is_file()
    assert (report.parent / "assets" / "additional_02.jpg").is_file()


def test_metadata_comparison_separates_equal_and_different_fields(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (20, 10), "red").save(first)
    Image.new("RGB", (20, 12), "red").save(second)
    comparison = compare_metadata(inspect_image_metadata(first), inspect_image_metadata(second))
    assert comparison["similarities"]["format"] == "PNG"
    assert "stored_height" in comparison["differences"]
