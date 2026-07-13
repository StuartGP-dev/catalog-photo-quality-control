from __future__ import annotations

from pathlib import Path

from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.models import Recipe
from common.catalog_photo_control.selector import Candidate, CandidateImage, select_and_persist, select_max_min
from common.catalog_photo_control.source_loader import load_source_listing
from common.catalog_photo_control.variants_db import VariantsDatabase


def _candidate(name: str, quality: float, brightness: float) -> Candidate:
    return Candidate(
        int(name),
        Recipe.from_parameters({"identity": name}),
        {"quality_score": quality, "mean_brightness": brightness},
        (),
    )


def test_max_min_starts_with_quality_then_chooses_diversity() -> None:
    candidates = [
        _candidate("1", 0.9, 0.5),
        _candidate("2", 0.8, 0.51),
        _candidate("3", 0.7, 0.9),
    ]

    selected = select_max_min(candidates, 2)

    assert [item.candidate.test_id for item in selected] == [1, 3]
    assert selected[1].minimum_distance is not None
    assert "mean_brightness" in selected[1].distance_components


def test_max_min_excludes_dimensions_only_after_selection(tmp_path: Path) -> None:
    def with_dimensions(index: int, width: int) -> Candidate:
        return Candidate(
            index,
            Recipe.from_parameters({"identity": index}),
            {"quality_score": 1 - index / 100, "mean_brightness": index / 10},
            (
                CandidateImage(
                    0, "source", tmp_path / str(index), "output",
                    {"output_width": width, "output_height": 100},
                ),
            ),
        )

    first = with_dimensions(1, 101)
    collision = with_dimensions(2, 101)
    distinct = with_dimensions(3, 103)
    selected = select_max_min([first, collision, distinct], 3)
    assert [item.candidate.test_id for item in selected] == [1, 3]


def test_complete_selection_resumes_and_stops_at_target(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="synthetic")
    space = load_filter_space()
    bench = BenchDatabase(tmp_path / "bench.sqlite3")
    bench.initialize()
    bench.register_source(listing)
    variants = VariantsDatabase(tmp_path / "variants.sqlite3")
    variants.initialize()
    variants.register_source(listing)
    for index, brightness in enumerate((0.98, 1.0, 1.02), start=1):
        recipe = space.schema.canonicalize({"brightness": brightness})
        execution = bench.execute_recipe_test(
            listing, recipe, tmp_path / f"work-{index}", space.quality_thresholds
        )
        assert execution.eligible

    first_ids = select_and_persist(
        bench.connection, variants, listing, 2, tmp_path / "selected"
    )
    second_ids = select_and_persist(
        bench.connection, variants, listing, 2, tmp_path / "selected"
    )
    third_ids = select_and_persist(
        bench.connection, variants, listing, 3, tmp_path / "selected"
    )

    assert len(first_ids) == 2
    assert second_ids == []
    assert len(third_ids) == 1
    assert variants.ready_count(listing.listing_id, listing.source_set_hash) == 3
    rows = variants.connection.execute(
        """SELECT variant_id, expected_image_count, minimum_distance_components_json
           FROM listing_variants WHERE status='ready' ORDER BY selected_rank"""
    ).fetchall()
    assert all(row[1] == len(listing.images) for row in rows)
    assert any(row[2] != "{}" for row in rows[1:])
    selected_stats = bench.connection.execute(
        "SELECT SUM(selected_count) FROM recipe_global_stats"
    ).fetchone()[0]
    assert selected_stats == 3
    assert bench.connection.execute(
        "SELECT COUNT(*) FROM recipe_pair_distances"
    ).fetchone()[0] == 3
    bench.close()
    variants.close()
