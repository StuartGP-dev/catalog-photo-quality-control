from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common.catalog_photo_control.calibrate import (
    CalibrationSpec,
    _sample_steps,
    build_parser,
    calibration_config_hash,
    calibration_recipe,
    classify_calibration,
    deterministic_bisection,
    generate_coarse_specs,
    neutral_parameters,
    run_calibration,
    MINIMUM_REVIEW_RANKING_DISTANCE,
)
from common.catalog_photo_control.image_pipeline import apply_recipe


def _metrics(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ssim": 0.985,
        "pixel_mae": 0.02,
        "luminance_mae": 0.018,
        "sharpness_ratio": 1.0,
        "clip_fraction": 0.0,
        "canvas_fraction": 0.03,
        "foreground_clipped": False,
        "background_rgb": (246, 246, 246),
    }
    values.update(changes)
    return values


def test_coarse_steps_are_deterministic_and_keep_range_edges() -> None:
    values = (0.25, 0.5, 0.8, 1.2, 2.0, 3.5, 5.0, 6.5, 8.0)
    assert _sample_steps(values, 6) == (0.25, 0.8, 1.2, 3.5, 5.0, 8.0)
    assert _sample_steps(values, 6) == _sample_steps(values, 6)


def test_generation_covers_directions_zoom_dezoom_and_combinations() -> None:
    specs = generate_coarse_specs(
        (
            "rotation", "crop", "zoom", "dezoom", "offset",
            "rotation_crop_compensated", "rotation_zoom", "zoom_offset",
            "rotation_dezoom_canvas", "crop_offset",
        ),
        2,
    )
    assert {spec.branch for spec in specs if spec.family == "rotation"} == {"left", "right"}
    assert {spec.branch for spec in specs if spec.family == "offset"} == {"left", "right", "up", "down"}
    assert all(float(spec.parameters["zoom"]) > 1 for spec in specs if spec.family == "zoom")
    dezooms = [spec for spec in specs if spec.family == "dezoom"]
    assert {spec.branch for spec in dezooms} == {
        "white", "light_gray", "sampled_background", "sampled_edge",
        "side_bands", "uniform_frame",
    }
    assert all(spec.parameters["canvas_mode"] != "none" for spec in dezooms)
    assert {
        "rotation_crop_compensated", "rotation_zoom", "zoom_offset",
        "rotation_dezoom_canvas", "crop_offset",
    } <= {spec.family for spec in specs}


def test_non_studied_parameters_are_neutral_and_dezoom_requires_canvas() -> None:
    defaults = neutral_parameters()
    recipe = calibration_recipe({"rotation_degrees": -2.5})
    assert recipe.parameters["rotation_degrees"] == -2.5
    assert all(
        recipe.parameters[name] == value
        for name, value in defaults.items()
        if name != "rotation_degrees"
    )
    with pytest.raises(ValueError, match="dezoom requires"):
        calibration_recipe({"resize_scale": 0.98})


def test_bisection_is_deterministic() -> None:
    lower = CalibrationSpec("rotation", "right", 0.25, {"rotation_degrees": 0.25})
    upper = CalibrationSpec("rotation", "right", 0.8, {"rotation_degrees": 0.8})
    first = deterministic_bisection(lower, upper, 4)
    second = deterministic_bisection(lower, upper, 4)
    assert first == second
    assert [item.parameters["rotation_degrees"] for item in first] == pytest.approx(
        [0.525, 0.3875, 0.31875, 0.284375]
    )
    assert all(item.stage == "bisection" for item in first)


def test_classification_is_family_specific_and_prudent() -> None:
    rows = [_metrics(), _metrics()]
    assert classify_calibration("rotation", 0.5, rows)[0] == "very_subtle"
    assert classify_calibration("rotation", 0.8, rows)[0] == "perceptible_candidate"
    assert classify_calibration("rotation", 2.0, rows)[0] == "strong_candidate"
    assert classify_calibration("rotation_zoom", 0.5, rows)[0] == "perceptible_candidate"
    classification, reasons = classify_calibration("crop", 0.01, [_metrics(ssim=0.9)])
    assert classification == "rejected" and "ssim" in reasons


def test_sampled_background_uses_gray_then_white_fallback() -> None:
    saturated = Image.new("RGB", (100, 80), (240, 20, 20))
    gray_recipe = calibration_recipe(
        {"resize_scale": 0.98, "canvas_mode": "sampled_background"}
    )
    gray = apply_recipe(saturated, gray_recipe)
    gray_metadata = gray.info["canvas_metadata"]
    assert gray_metadata["detected_background_rgb"] == (240, 20, 20)
    assert gray_metadata["sampled_background_fallback_used"] is True
    assert gray_metadata["fallback_origin"] == "fallback_light_gray"
    assert min(gray_metadata["background_rgb"]) >= 246

    white_recipe = calibration_recipe(
        {
            "resize_scale": 0.98,
            "canvas_mode": "sampled_background",
            "fixed_background_gray": 0,
        }
    )
    white = apply_recipe(saturated, white_recipe)
    assert white.info["canvas_metadata"]["background_rgb"] == (255, 255, 255)
    assert white.info["canvas_metadata"]["fallback_origin"] == "fallback_white"


def _visual_listing(root: Path) -> Path:
    listing = root / "visual-listing"
    listing.mkdir()
    for index in range(2):
        image = Image.new("RGB", (160, 120), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.ellipse((45 + index * 3, 25, 115 + index * 3, 95), fill=(90, 70, 130))
        draw.rectangle((72, 42, 88, 78), fill=(220, 185, 80))
        image.save(listing / f"{index + 1:02d}.png")
    return listing


def test_calibration_run_is_isolated_resumable_and_report_is_exhaustive(
    tmp_path: Path,
) -> None:
    listing = _visual_listing(tmp_path)
    source_before = {path.name: path.read_bytes() for path in listing.iterdir()}
    output_root = tmp_path / "local" / "calibration_runs"
    args = build_parser().parse_args(
        [
            "--listing", str(listing),
            "--families", "rotation,crop",
            "--output-root", str(output_root),
            "--coarse-steps", "2",
            "--bisection-steps", "1",
        ]
    )
    report, summary = run_calibration(args)
    manifest_path = report.parent / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    second_report, second_summary = run_calibration(args)

    assert second_report == report and second_summary == summary
    assert manifest_path.read_bytes() == manifest_before
    assert list(report.parent.rglob("*.html")) == [report]
    assert not list((tmp_path / "local").rglob("catalog_variants.sqlite3"))
    assert source_before == {path.name: path.read_bytes() for path in listing.iterdir()}
    content = report.read_text(encoding="utf-8")
    for expected in (
        "Différence amplifiée", "Crop central", "Bords original / variant",
        "Boîte du contenu", "Recette canonique neutralisée", "Fond détecté",
        "very_subtle", "perceptible_candidate", "strong_candidate", "rejected",
        "Sauvegarder mes choix", "Originale", "Photo filtrée",
        "Accepter", "Refuser", "À revoir",
        "localStorage", "human_filter_choices_",
    ):
        assert expected in content
    assert 'class="overlay"' not in content
    assert 'class="slider"' not in content
    manifest = json.loads(manifest_before)
    assert manifest["html_count"] == 1
    assert manifest["example_count"] == sum(row["examples"] for row in summary.values())
    assert manifest["source_hashes"] and len(manifest["source_hashes"]) == 2
    example_count = manifest["example_count"]
    assert len(list(report.parent.rglob("difference.jpg"))) == example_count * 2
    assert len(list(report.parent.rglob("central.jpg"))) == example_count * 2
    assert len(list(report.parent.rglob("edges.jpg"))) == example_count * 2
    assert len(list(report.parent.rglob("content_box.jpg"))) == example_count * 2


def test_calibration_hash_changes_with_effective_configuration() -> None:
    first = calibration_config_hash(("rotation",), 6, 4)
    assert first == calibration_config_hash(("rotation",), 6, 4)
    assert first != calibration_config_hash(("rotation",), 7, 4)
    assert first != calibration_config_hash(("crop",), 6, 4)
    assert MINIMUM_REVIEW_RANKING_DISTANCE == 40
