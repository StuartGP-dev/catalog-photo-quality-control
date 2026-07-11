from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .diversity import Distance, listing_distance
from .models import ListingVariant, Recipe, SourceListing
from .variants_db import VariantsDatabase


@dataclass(frozen=True, slots=True)
class CandidateImage:
    image_index: int
    source_hash: str
    output_path: Path
    output_hash: str
    metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Candidate:
    test_id: int
    recipe: Recipe
    aggregate_metrics: Mapping[str, float]
    images: tuple[CandidateImage, ...]

    @property
    def quality_score(self) -> float:
        return float(self.aggregate_metrics.get("quality_score", 0))

    @property
    def distance_from_original(self) -> float:
        return float(self.aggregate_metrics.get("mean_pixel_mae", 0))


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    candidate: Candidate
    minimum_distance: float | None
    distance_components: Mapping[str, float]


def select_max_min(
    candidates: Sequence[Candidate],
    count: int,
    *,
    existing_metrics: Sequence[Mapping[str, float]] = (),
) -> list[SelectedCandidate]:
    remaining = list(candidates)
    selected: list[SelectedCandidate] = []
    comparison_metrics = list(existing_metrics)
    while remaining and len(selected) < count:
        if not comparison_metrics:
            chosen = max(
                remaining,
                key=lambda candidate: (candidate.quality_score, candidate.recipe.recipe_hash),
            )
            selection = SelectedCandidate(chosen, None, {})
        else:
            scored: list[tuple[float, float, str, Candidate, Distance]] = []
            for candidate in remaining:
                distances = [
                    listing_distance(candidate.aggregate_metrics, metrics)
                    for metrics in comparison_metrics
                ]
                nearest = min(distances, key=lambda distance: distance.total)
                scored.append(
                    (
                        nearest.total,
                        candidate.quality_score,
                        candidate.recipe.recipe_hash,
                        candidate,
                        nearest,
                    )
                )
            _, _, _, chosen, nearest = max(scored, key=lambda item: item[:3])
            selection = SelectedCandidate(chosen, nearest.total, nearest.components)
        selected.append(selection)
        comparison_metrics.append(chosen.aggregate_metrics)
        remaining.remove(chosen)
    return selected


def load_eligible_candidates(
    connection: sqlite3.Connection, listing: SourceListing
) -> list[Candidate]:
    expected = {image.index: image.source_hash for image in listing.images}
    rows = connection.execute(
        """SELECT t.test_id, r.recipe_hash, r.parameters_json,
                  t.aggregate_metrics_json
           FROM recipe_tests t JOIN recipes r USING(recipe_id)
           WHERE t.listing_id=? AND t.source_set_hash=?
             AND t.complete=1 AND t.quality_valid=1 AND t.eligible=1
           ORDER BY t.test_id""",
        (listing.listing_id, listing.source_set_hash),
    ).fetchall()
    candidates: list[Candidate] = []
    for row in rows:
        image_rows = connection.execute(
            """SELECT image_index, source_hash, output_path, output_hash, metrics_json
               FROM recipe_test_images WHERE test_id=? ORDER BY image_index""",
            (row["test_id"],),
        ).fetchall()
        if len(image_rows) != len(expected):
            continue
        images = tuple(
            CandidateImage(
                int(image["image_index"]),
                image["source_hash"],
                Path(image["output_path"]) if image["output_path"] else Path(),
                image["output_hash"],
                json.loads(image["metrics_json"]),
            )
            for image in image_rows
        )
        if any(
            image.image_index not in expected
            or expected[image.image_index] != image.source_hash
            or not image.output_path.is_file()
            for image in images
        ):
            continue
        recipe = Recipe.from_parameters(json.loads(row["parameters_json"]))
        if recipe.recipe_hash != row["recipe_hash"]:
            continue
        candidates.append(
            Candidate(
                int(row["test_id"]),
                recipe,
                json.loads(row["aggregate_metrics_json"]),
                images,
            )
        )
    return candidates


def select_and_persist(
    bench_connection: sqlite3.Connection,
    variants: VariantsDatabase,
    listing: SourceListing,
    target_count: int,
    selected_root: str | Path,
) -> list[int]:
    existing_rows = variants.connection.execute(
        """SELECT recipe_hash, aggregate_metrics_json FROM listing_variants
           WHERE listing_id=? AND source_set_hash=? AND status='ready'
           ORDER BY selected_rank""",
        (listing.listing_id, listing.source_set_hash),
    ).fetchall()
    needed = max(0, target_count - len(existing_rows))
    if needed == 0:
        return []
    existing_hashes = {row["recipe_hash"] for row in existing_rows}
    existing_metrics = [json.loads(row["aggregate_metrics_json"]) for row in existing_rows]
    candidates = [
        candidate
        for candidate in load_eligible_candidates(bench_connection, listing)
        if candidate.recipe.recipe_hash not in existing_hashes
    ]
    selections = select_max_min(candidates, needed, existing_metrics=existing_metrics)
    root = Path(selected_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    variant_ids: list[int] = []
    next_rank = len(existing_rows) + 1
    for offset, selection in enumerate(selections):
        rank = next_rank + offset
        destination = root / f"variant_{rank:04d}"
        if destination.exists():
            raise FileExistsError(destination)
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=root))
        try:
            copied_paths: list[Path] = []
            for image in selection.candidate.images:
                copied = temporary / image.output_path.name
                shutil.copy2(image.output_path, copied)
                copied_paths.append(copied)
            os.replace(temporary, destination)
            final_paths = tuple(destination / path.name for path in copied_paths)
            variant = ListingVariant(
                None,
                listing.listing_id,
                listing.source_set_hash,
                selection.candidate.recipe,
                final_paths,
                rank,
                bench_test_id=selection.candidate.test_id,
                aggregate_metrics=selection.candidate.aggregate_metrics,
                distance_from_original=selection.candidate.distance_from_original,
                minimum_selected_distance=selection.minimum_distance,
            )
            image_rows = [
                {
                    "image_index": image.image_index,
                    "source_hash": image.source_hash,
                    "output_path": final_path,
                    "output_hash": image.output_hash,
                    "metrics": image.metrics,
                }
                for image, final_path in zip(
                    selection.candidate.images, final_paths, strict=True
                )
            ]
            variant_id = variants.save_complete_variant(variant, image_rows)
            with bench_connection:
                bench_connection.execute(
                    "UPDATE recipe_tests SET selected=1 WHERE test_id=?",
                    (selection.candidate.test_id,),
                )
            variant_ids.append(variant_id)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return variant_ids
