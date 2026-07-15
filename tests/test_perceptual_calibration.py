from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageDraw

from common.catalog_photo_control.perceptual_calibration import generate_calibration_report


class _Sources(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            self.values.extend(value for key, value in attrs if key == "src" and value)


def test_calibration_html_has_only_existing_relative_assets(tmp_path: Path) -> None:
    listing = tmp_path / "O18"; listing.mkdir()
    for index in range(2):
        image = Image.new("RGB", (80, 60), "white")
        ImageDraw.Draw(image).ellipse((12 + index * 5, 8, 60, 52), fill=(90, 60 + index * 40, 130))
        image.save(listing / f"{index}.jpg")
    report, payload = generate_calibration_report(listing, tmp_path / "report with spaces")
    parser = _Sources(); parser.feed(report.read_text(encoding="utf-8"))
    assert parser.values and payload["pair_count"] >= 20
    assert all(not value.startswith(("file:", "/")) for value in parser.values)
    assert all((report.parent / Path(unquote(value))).is_file() for value in parser.values)
    assert {case["verdict"] for case in payload["cases"]} <= {"exact", "same", "near_duplicate", "different"}
    cases = payload["cases"]
    verdict_order = {"different": 0, "near_duplicate": 1, "same": 2, "exact": 3}
    assert cases == sorted(cases, key=lambda row: (verdict_order[row["verdict"]], -row["distance_sum"], -row["phash"]["distance"], -row["dhash"]["distance"], -row["whash"]["distance"]))
    content = report.read_text(encoding="utf-8")
    for label in ("Plus différentes d’abord", "Plus similaires d’abord", "20 paires les plus différentes", "20 paires les plus proches", "Paires proches des seuils", "Distribution des verdicts"):
        assert label in content
    assert all(f'data-rank="{rank}"' in content for rank in range(1, payload["pair_count"] + 1))
    assert sum(payload["verdict_counts"].values()) == payload["pair_count"]


def test_repository_has_one_perceptual_verdict_engine() -> None:
    package = Path(__file__).parents[1] / "common" / "catalog_photo_control"
    assert not (package / "visual_distance.py").exists()
    definitions = []
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if 'verdict, reason = "exact"' in text:
            definitions.append(path.name)
    assert definitions == ["image_similarity.py"]
