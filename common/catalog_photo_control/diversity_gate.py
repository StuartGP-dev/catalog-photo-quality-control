from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .visual_distance import DISTANCE_METRICS_VERSION, ImageDistanceResult, VisualSignature, image_distance, visual_signature


@dataclass(frozen=True, slots=True)
class ImageReference:
    listing_id: str
    source_set_hash: str
    image_index: int
    path: Path
    output_hash: str
    variant_id: int | None
    recipe_family: str
    reference_kind: str


@dataclass(frozen=True, slots=True)
class NearestReference:
    reference: ImageReference
    distance: ImageDistanceResult


@dataclass(frozen=True, slots=True)
class ImageDiversityVerdict:
    image_index: int
    valid: bool
    status: str
    minimum_same_listing_distance: float | None
    minimum_catalog_distance: float | None
    nearest_same_listing: NearestReference | None
    nearest_catalog: NearestReference | None
    same_listing_neighbors: tuple[NearestReference, ...]
    catalog_neighbors: tuple[NearestReference, ...]
    reference_count_same_listing: int
    reference_count_catalog: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VariantDiversityVerdict:
    valid: bool
    images: tuple[ImageDiversityVerdict, ...]
    minimum_same_listing_distance: float | None
    minimum_catalog_distance: float | None
    reasons: tuple[str, ...]


def validate_diversity_config(config: Mapping[str, Any]) -> dict[str, Any]:
    scope = str(config.get("scope", "both"))
    if scope not in {"listing", "catalog", "both"}:
        raise ValueError("diversity_gate.scope must be listing, catalog, or both")
    if not bool(config.get("compare_same_image_index_only", True)):
        raise ValueError("only same-image-index diversity comparison is supported")
    normalized = dict(config)
    normalized["scope"] = scope
    normalized["minimum_same_listing_distance"] = float(config.get("minimum_same_listing_distance", 0.0))
    normalized["minimum_catalog_distance"] = float(config.get("minimum_catalog_distance", 0.0))
    normalized["metrics_version"] = str(config.get("metrics_version", DISTANCE_METRICS_VERSION))
    normalized["nearest_neighbors_to_persist"] = max(1, int(config.get("nearest_neighbors_to_persist", 5)))
    return normalized


class DiversityGate:
    def __init__(self, variants_connection: Any, config: Mapping[str, Any]):
        self.connection = variants_connection
        self.config = validate_diversity_config(config)
        self._signature_cache: dict[tuple[str, int, int], VisualSignature] = {}

    def _signature(self, path: Path) -> VisualSignature:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        signature = self._signature_cache.get(key)
        if signature is None:
            signature = visual_signature(path)
            self._signature_cache[key] = signature
        return signature

    def references(self, listing_id: str, source_set_hash: str, image_index: int) -> tuple[list[ImageReference], list[ImageReference]]:
        same: list[ImageReference] = []
        catalog: list[ImageReference] = []
        if self.config.get("include_source_images", True):
            rows = self.connection.execute(
                """SELECT image.listing_id, image.source_set_hash, image.image_index,
                          image.source_path AS path, image.source_hash AS output_hash
                   FROM listing_images image JOIN listings listing USING(listing_id)
                   WHERE image.image_index=? AND image.source_set_hash=listing.active_source_set_hash""",
                (image_index,),
            ).fetchall()
            for row in rows:
                reference = ImageReference(str(row["listing_id"]), str(row["source_set_hash"]), int(row["image_index"]), Path(row["path"]), str(row["output_hash"]), None, "source", "source")
                (same if reference.listing_id == listing_id and reference.source_set_hash == source_set_hash else catalog).append(reference)
        if self.config.get("include_ready_variants", True):
            rows = self.connection.execute(
                """SELECT variant.listing_id, variant.source_set_hash, image.image_index,
                          image.output_path AS path, image.output_hash, variant.variant_id,
                          variant.recipe_family
                   FROM listing_variant_images image
                   JOIN listing_variants variant USING(variant_id)
                   JOIN listings listing USING(listing_id)
                   WHERE image.image_index=? AND variant.status='ready'
                     AND variant.source_set_hash=listing.active_source_set_hash""",
                (image_index,),
            ).fetchall()
            for row in rows:
                reference = ImageReference(str(row["listing_id"]), str(row["source_set_hash"]), int(row["image_index"]), Path(row["path"]), str(row["output_hash"]), int(row["variant_id"]), str(row["recipe_family"]), "ready_variant")
                (same if reference.listing_id == listing_id and reference.source_set_hash == source_set_hash else catalog).append(reference)
        deduplicated: list[list[ImageReference]] = []
        for references in (same, catalog):
            seen: set[str] = set()
            unique = []
            for reference in references:
                if reference.output_hash in seen or not reference.path.is_file():
                    continue
                seen.add(reference.output_hash)
                unique.append(reference)
            deduplicated.append(unique)
        return deduplicated[0], deduplicated[1]

    def _neighbors(self, candidate_path: Path, references: Sequence[ImageReference], weights: Mapping[str, float]) -> tuple[NearestReference, ...]:
        if not references:
            return ()
        candidate = self._signature(candidate_path)
        rows = sorted(
            (NearestReference(reference, image_distance(candidate, self._signature(reference.path), weights)) for reference in references),
            key=lambda item: (item.distance.total_distance, item.reference.output_hash),
        )
        return tuple(rows[: self.config["nearest_neighbors_to_persist"]])

    def _threshold(self, scope: str, recipe_family: str) -> float:
        key = "minimum_same_listing_distance" if scope == "listing" else "minimum_catalog_distance"
        family = self.config.get("family_thresholds", {}).get(recipe_family, {})
        return float(family.get(key, self.config[key]))

    def evaluate_image(self, listing_id: str, source_set_hash: str, image_index: int, candidate_path: Path, recipe_family: str) -> ImageDiversityVerdict:
        same, catalog = self.references(listing_id, source_set_hash, image_index)
        weights = dict(self.config.get("weights", {}))
        weights.update(dict(self.config.get("family_weights", {}).get(recipe_family, {})))
        same_neighbors = self._neighbors(candidate_path, same, weights)
        catalog_neighbors = self._neighbors(candidate_path, catalog, weights)
        nearest_same = same_neighbors[0] if same_neighbors else None
        nearest_catalog = catalog_neighbors[0] if catalog_neighbors else None
        scope = self.config["scope"]
        reasons: list[str] = []
        if scope in {"listing", "both"} and nearest_same and nearest_same.distance.total_distance < self._threshold("listing", recipe_family):
            reasons.extend(("same_listing_distance_too_small", f"same_listing_distance_too_small_image_{image_index}"))
        if scope in {"catalog", "both"} and nearest_catalog and nearest_catalog.distance.total_distance < self._threshold("catalog", recipe_family):
            reasons.extend(("catalog_distance_too_small", f"catalog_distance_too_small_image_{image_index}"))
        missing = []
        if scope in {"listing", "both"} and nearest_same is None:
            missing.append("same_listing")
        if scope in {"catalog", "both"} and nearest_catalog is None:
            missing.append("catalog")
        return ImageDiversityVerdict(
            image_index=image_index,
            valid=not reasons,
            status="no_reference_yet" if missing else ("accepted" if not reasons else "rejected"),
            minimum_same_listing_distance=nearest_same.distance.total_distance if nearest_same else None,
            minimum_catalog_distance=nearest_catalog.distance.total_distance if nearest_catalog else None,
            nearest_same_listing=nearest_same,
            nearest_catalog=nearest_catalog,
            same_listing_neighbors=same_neighbors,
            catalog_neighbors=catalog_neighbors,
            reference_count_same_listing=len(same),
            reference_count_catalog=len(catalog),
            reasons=tuple(reasons),
        )

    def evaluate_variant(self, listing_id: str, source_set_hash: str, images: Sequence[tuple[int, Path]], recipe_family: str) -> VariantDiversityVerdict:
        verdicts = tuple(self.evaluate_image(listing_id, source_set_hash, index, path, recipe_family) for index, path in images)
        reasons = tuple(reason for verdict in verdicts for reason in verdict.reasons)
        same = [value.minimum_same_listing_distance for value in verdicts if value.minimum_same_listing_distance is not None]
        catalog = [value.minimum_catalog_distance for value in verdicts if value.minimum_catalog_distance is not None]
        return VariantDiversityVerdict(not reasons, verdicts, min(same) if same else None, min(catalog) if catalog else None, reasons)


def nearest_to_json(nearest: NearestReference | None) -> str:
    if nearest is None:
        return "{}"
    reference = nearest.reference
    return json.dumps({
        "listing_id": reference.listing_id,
        "source_set_hash": reference.source_set_hash,
        "variant_id": reference.variant_id,
        "image_index": reference.image_index,
        "output_hash": reference.output_hash,
        "path": str(reference.path),
        "reference_kind": reference.reference_kind,
        "recipe_family": reference.recipe_family,
        "total_distance": nearest.distance.total_distance,
        "components": nearest.distance.components(),
    }, sort_keys=True, separators=(",", ":"))
