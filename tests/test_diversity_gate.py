from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

from common.catalog_photo_control.audit_diversity import build_parser, run_audit
from common.catalog_photo_control.diversity_gate import DiversityGate
from common.catalog_photo_control.models import ListingVariant, Recipe, SourceImage, SourceListing, ordered_source_set_hash, stable_hash
from common.catalog_photo_control.variants_db import VariantsDatabase
from common.catalog_photo_control.visual_distance import DISTANCE_METRICS_VERSION, image_distance, visual_signature


def _image(path: Path, color: tuple[int, int, int], *, shift: int = 0, size: tuple[int, int] = (80, 60)) -> str:
    canvas = Image.new("RGB", size, (245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((18 + shift, 12, 58 + shift, 48), fill=color)
    canvas.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listing(root: Path, code: str, colors: list[tuple[int, int, int]]) -> SourceListing:
    directory = root / code
    directory.mkdir()
    rows = []
    for index, color in enumerate(colors):
        path = directory / f"{index}.png"
        digest = _image(path, color)
        rows.append(SourceImage(index, path, digest, 80, 60))
    images = tuple(rows)
    return SourceListing(stable_hash({"code": code}), code, directory, images, ordered_source_set_hash(images))


def _config(scope: str = "both", threshold: float = 0.02) -> dict[str, object]:
    return {
        "enabled": True,
        "scope": scope,
        "compare_same_image_index_only": True,
        "include_ready_variants": True,
        "include_source_images": True,
        "minimum_same_listing_distance": threshold,
        "minimum_catalog_distance": threshold,
        "reject_complete_variant_on_single_image_failure": True,
        "metrics_version": DISTANCE_METRICS_VERSION,
        "nearest_neighbors_to_persist": 5,
    }


def _save_variant(db: VariantsDatabase, listing: SourceListing, root: Path, shifts: list[int], *, rank: int = 1) -> int:
    recipe = Recipe.from_parameters({"rotation_degrees": rank})
    paths, rows = [], []
    for source, shift in zip(listing.images, shifts, strict=True):
        path = root / f"{listing.listing_code}-{rank}-{source.index}.png"
        digest = _image(path, (90 + source.index * 20, 80, 130), shift=shift, size=(80 + rank, 60 + rank))
        paths.append(path)
        rows.append({"image_index": source.index, "source_hash": source.source_hash, "output_path": path, "output_hash": digest, "metrics": {"output_width": 80 + rank, "output_height": 60 + rank}})
    return db.save_complete_variant(ListingVariant(None, listing.listing_id, listing.source_set_hash, recipe, tuple(paths), rank, recipe_family="rotation_family", diversity_gate_version=DISTANCE_METRICS_VERSION, diversity_valid=True), rows)


def test_image_distance_is_deterministic_bounded_and_explicit(tmp_path: Path) -> None:
    left, right = tmp_path / "left.png", tmp_path / "right.png"
    _image(left, (100, 80, 120))
    _image(right, (100, 80, 120), shift=5)
    first = image_distance(visual_signature(left), visual_signature(right))
    second = image_distance(visual_signature(left), visual_signature(right))
    assert first == second
    assert 0 < first.total_distance <= 1
    assert set(first.components()) == {"structural_distance", "luminance_distance", "color_distance", "edge_distance", "geometry_distance", "canvas_distance"}


def test_gate_compares_only_equal_image_indexes_and_reports_no_reference(tmp_path: Path) -> None:
    listing = _listing(tmp_path, "A", [(220, 20, 20), (20, 20, 220)])
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize(); database.register_source(listing)
    candidate = tmp_path / "candidate.png"
    _image(candidate, (20, 20, 220))  # identical to source index 1, not index 0
    verdict = DiversityGate(database.connection, _config("listing", 0.01)).evaluate_image(listing.listing_id, listing.source_set_hash, 0, candidate, "offset_family")
    assert verdict.valid
    assert verdict.reference_count_same_listing == 1
    empty = DiversityGate(database.connection, _config("catalog", 0.99)).evaluate_image(listing.listing_id, listing.source_set_hash, 0, candidate, "offset_family")
    assert empty.valid and empty.status == "no_reference_yet"
    database.close()


def test_single_close_image_rejects_complete_five_image_variant_with_exact_index(tmp_path: Path) -> None:
    colors = [(50 + index * 25, 80, 130) for index in range(5)]
    listing = _listing(tmp_path, "A", colors)
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize(); database.register_source(listing)
    candidates = []
    for source in listing.images:
        path = tmp_path / f"candidate-{source.index}.png"
        _image(path, colors[source.index], shift=0 if source.index == 0 else 8)
        candidates.append((source.index, path))
    verdict = DiversityGate(database.connection, _config("listing", 0.01)).evaluate_variant(listing.listing_id, listing.source_set_hash, candidates, "mixed_geometry_family")
    assert not verdict.valid
    assert "same_listing_distance_too_small_image_0" in verdict.reasons
    assert len(verdict.images) == 5 and all(item.image_index == index for index, item in enumerate(verdict.images))
    database.close()


def test_listing_catalog_and_both_scopes_use_correct_pools(tmp_path: Path) -> None:
    listing_a = _listing(tmp_path, "A", [(200, 20, 20)])
    listing_b = _listing(tmp_path, "B", [(20, 20, 200)])
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize(); database.register_source(listing_a); database.register_source(listing_b)
    candidate = tmp_path / "candidate.png"; _image(candidate, (20, 20, 200))
    listing_verdict = DiversityGate(database.connection, _config("listing", 0.01)).evaluate_image(listing_a.listing_id, listing_a.source_set_hash, 0, candidate, "appearance_only")
    catalog_verdict = DiversityGate(database.connection, _config("catalog", 0.01)).evaluate_image(listing_a.listing_id, listing_a.source_set_hash, 0, candidate, "appearance_only")
    both_verdict = DiversityGate(database.connection, _config("both", 0.01)).evaluate_image(listing_a.listing_id, listing_a.source_set_hash, 0, candidate, "appearance_only")
    assert listing_verdict.valid
    assert not catalog_verdict.valid and "catalog_distance_too_small_image_0" in catalog_verdict.reasons
    assert not both_verdict.valid
    database.close()


def test_reference_pool_deduplicates_hashes_excludes_drafts_and_handles_fewer_images(tmp_path: Path) -> None:
    listing_a = _listing(tmp_path, "A", [(100, 20, 20), (20, 100, 20)])
    listing_b = _listing(tmp_path, "B", [(100, 20, 20)])
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize(); database.register_source(listing_a); database.register_source(listing_b)
    database.connection.execute("""INSERT INTO listing_variants(listing_id,source_set_hash,recipe_hash,recipe_json,selected_rank,expected_image_count,status) VALUES(?,?,?,?,?,?, 'draft')""", (listing_a.listing_id, listing_a.source_set_hash, "draft", "{}", 99, 1))
    same, catalog = DiversityGate(database.connection, _config()).references(listing_a.listing_id, listing_a.source_set_hash, 1)
    assert len(same) == 1 and catalog == []
    same0, catalog0 = DiversityGate(database.connection, _config()).references(listing_a.listing_id, listing_a.source_set_hash, 0)
    assert len(same0) == 1 and len(catalog0) == 1
    database.close()


def test_ready_variant_requires_diversity_and_audit_is_read_only_with_html(tmp_path: Path) -> None:
    listing = _listing(tmp_path, "A", [(120, 40, 60)])
    local = tmp_path / "local"; database_path = local / "databases" / "catalog_variants.sqlite3"
    database = VariantsDatabase(database_path)
    database.initialize(); database.register_source(listing)
    _save_variant(database, listing, tmp_path, [5])
    database.close()
    config = json.loads((Path(__file__).parents[1] / "config" / "filter_space.json").read_text())
    config["diversity_gate"] = _config("both", 0.01)
    config_path = tmp_path / "filter.json"; config_path.write_text(json.dumps(config))
    html_path = tmp_path / "audit" / "index.html"
    args = build_parser().parse_args(["--local-root", str(local), "--scope", "both", "--top-nearest", "5", "--html", str(html_path), "--filter-space", str(config_path)])
    result = run_audit(args)
    assert result["read_only_unchanged"] is True
    assert result["variant_count"] == 1 and html_path.is_file()
    assert "Candidat" in html_path.read_text(encoding="utf-8")


def test_threshold_and_metrics_version_change_evaluation_hash(tmp_path: Path) -> None:
    raw = json.loads((Path(__file__).parents[1] / "config" / "filter_space.json").read_text())
    raw["diversity_gate"] = _config("both", 0.01)
    first = stable_hash(raw)
    raw["diversity_gate"]["minimum_same_listing_distance"] = 0.02
    second = stable_hash(raw)
    raw["diversity_gate"]["metrics_version"] = "next-version"
    third = stable_hash(raw)
    assert len({first, second, third}) == 3


def test_two_listing_smoke_rejects_collisions_atomically_at_indexes_zero_and_three(tmp_path: Path) -> None:
    colors_a = [(90 + index * 20, 80, 130) for index in range(5)]
    colors_b = [(70, 100 + index * 20, 150) for index in range(5)]
    listings = (_listing(tmp_path, "A", colors_a), _listing(tmp_path, "B", colors_b))
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize()
    for listing in listings:
        database.register_source(listing)

    rejected_indexes = (0, 3)
    for listing, collision_index in zip(listings, rejected_indexes, strict=True):
        accepted_paths: list[Path] = []
        for candidate_number, shift in enumerate((7, 7, -8), start=1):
            paths = []
            rows = []
            for source in listing.images:
                path = tmp_path / f"{listing.listing_code}-candidate-{candidate_number}-{source.index}.png"
                color = colors_a[source.index] if listing.listing_code == "A" else colors_b[source.index]
                digest = _image(path, color, shift=shift + candidate_number, size=(90 + candidate_number, 70 + candidate_number))
                if candidate_number == 2 and source.index == collision_index:
                    path.write_bytes(accepted_paths[source.index].read_bytes())
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                paths.append(path)
                rows.append({"image_index": source.index, "source_hash": source.source_hash, "output_path": path, "output_hash": digest, "metrics": {"output_width": 90 + candidate_number, "output_height": 70 + candidate_number}})
            verdict = DiversityGate(database.connection, _config("listing", 0.005)).evaluate_variant(
                listing.listing_id, listing.source_set_hash,
                tuple((index, path) for index, path in enumerate(paths)),
                "mixed_geometry_family",
            )
            if candidate_number == 2:
                assert not verdict.valid
                assert f"same_listing_distance_too_small_image_{collision_index}" in verdict.reasons
                continue
            recipe = Recipe.from_parameters({"candidate": candidate_number})
            database.save_complete_variant(
                ListingVariant(None, listing.listing_id, listing.source_set_hash, recipe, tuple(paths), 1 if candidate_number == 1 else 2, minimum_same_listing_distance=verdict.minimum_same_listing_distance, diversity_gate_version=DISTANCE_METRICS_VERSION, diversity_valid=verdict.valid),
                rows,
            )
            if candidate_number == 1:
                accepted_paths = paths

    ready = database.connection.execute("SELECT variant_id, expected_image_count FROM listing_variants WHERE status='ready'").fetchall()
    assert len(ready) == 4
    assert all(row["expected_image_count"] == 5 for row in ready)
    assert database.connection.execute("SELECT COUNT(*) FROM listing_variant_images").fetchone()[0] == 20
    assert not database.connection.execute("PRAGMA foreign_key_check").fetchall()
    database.close()


def test_family_specific_gate_is_checked_in_both_directions(tmp_path: Path) -> None:
    listing = _listing(tmp_path, "A", [(100, 80, 130)])
    database = VariantsDatabase(tmp_path / "variants.sqlite3")
    database.initialize(); database.register_source(listing)
    _save_variant(database, listing, tmp_path, [5])  # rotation_family reference
    candidate = tmp_path / "candidate.png"; _image(candidate, (100, 80, 130), shift=-7)
    config = _config("listing", 0.0)
    config["family_thresholds"] = {
        "appearance_only": {"minimum_same_listing_distance": 0.0},
        "rotation_family": {"minimum_same_listing_distance": 1.0},
    }
    verdict = DiversityGate(database.connection, config).evaluate_image(
        listing.listing_id, listing.source_set_hash, 0, candidate, "appearance_only"
    )
    assert not verdict.valid
    assert "same_listing_reverse_distance_too_small_image_0" in verdict.reasons
    database.close()
