from __future__ import annotations

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
    assert list((tmp_path / "local").rglob("*.html")) == [report]
    content = report.read_text(encoding="utf-8")
    assert content.count("<article class=\"variant\">") == 2
    assert content.count("<img ") == 4
    assert "stop reason: <strong>target_reached</strong>" in content.lower()
