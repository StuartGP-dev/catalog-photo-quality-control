from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    import common.catalog_photo_control as package

    assert package.__name__ == "common.catalog_photo_control"


def test_synthetic_listing_contains_only_generated_images(
    synthetic_listing: Path,
) -> None:
    paths = sorted(synthetic_listing.iterdir())
    assert [path.name for path in paths] == ["01.png", "02.png"]
    assert [Image.open(path).size for path in paths] == [(49, 37), (50, 38)]


def test_generated_artifacts_are_ignored() -> None:
    candidates = (
        "local/bench_runs/example/index.html",
        "local/databases/catalog_bench.sqlite3",
        "local/bench.log",
        "debug.zip",
    )
    for candidate in candidates:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", candidate],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, candidate
