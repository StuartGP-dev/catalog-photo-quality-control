from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .image_similarity import (
    ImageHashes,
    SimilarityResult,
    compare_hashes,
    compute_hashes,
    similarity_sort_key,
    validate_similarity_config,
)


@dataclass(frozen=True, slots=True)
class ImageReference:
    listing_id: str
    listing_code: str
    source_set_hash: str
    image_index: int
    path: Path
    output_hash: str
    variant_id: int | None
    reference_kind: str


@dataclass(frozen=True, slots=True)
class NearestReference:
    reference: ImageReference
    comparison: SimilarityResult


@dataclass(frozen=True, slots=True)
class ImageDiversityVerdict:
    image_index: int
    valid: bool
    status: str
    nearest: NearestReference | None
    neighbors: tuple[NearestReference, ...]
    reference_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VariantDiversityVerdict:
    valid: bool
    images: tuple[ImageDiversityVerdict, ...]
    reasons: tuple[str, ...]


def validate_diversity_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not bool(config.get("compare_same_image_index_only", True)):
        raise ValueError("only same-image-index similarity comparison is supported")
    normalized = validate_similarity_config(config)
    normalized["include_ready_variants"] = bool(config.get("include_ready_variants", True))
    # Source images are quality references, not already-kept final variants.
    normalized["include_source_images"] = False
    return normalized


class DiversityGate:
    """Atomic per-index barrier against complete variants already marked ready."""

    def __init__(self, variants_connection: Any, config: Mapping[str, Any]):
        self.connection = variants_connection
        self.config = validate_diversity_config(config)
        self._hash_cache: dict[tuple[str, int, int], ImageHashes] = {}

    def _hashes(self, path: Path) -> ImageHashes:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        value = self._hash_cache.get(key)
        if value is None:
            value = compute_hashes(path)
            self._hash_cache[key] = value
        return value

    def references(self, listing_id: str, source_set_hash: str, image_index: int) -> list[ImageReference]:
        if not self.config["include_ready_variants"]:
            return []
        rows = self.connection.execute(
            """SELECT variant.listing_id, listing.listing_code, variant.source_set_hash,
                      image.image_index, image.output_path AS path, image.output_hash,
                      variant.variant_id
               FROM listing_variant_images image
               JOIN listing_variants variant USING(variant_id)
               JOIN listings listing USING(listing_id)
               WHERE image.image_index=? AND variant.status='ready'
                 AND variant.source_set_hash=listing.active_source_set_hash""",
            (image_index,),
        ).fetchall()
        seen: set[str] = set()
        references: list[ImageReference] = []
        for row in rows:
            path = Path(row["path"])
            output_hash = str(row["output_hash"])
            if output_hash in seen or not path.is_file():
                continue
            seen.add(output_hash)
            references.append(ImageReference(
                str(row["listing_id"]), str(row["listing_code"]), str(row["source_set_hash"]),
                int(row["image_index"]), path, output_hash, int(row["variant_id"]), "ready_variant",
            ))
        return references

    def evaluate_image(
        self,
        listing_id: str,
        source_set_hash: str,
        image_index: int,
        candidate_path: Path,
        recipe_family: str = "appearance_only",
    ) -> ImageDiversityVerdict:
        del recipe_family  # similarity thresholds never vary with recipe family
        candidate_resolved = candidate_path.resolve()
        references = [row for row in self.references(listing_id, source_set_hash, image_index) if row.path.resolve() != candidate_resolved]
        candidate_hashes = self._hashes(candidate_path)
        neighbors = sorted(
            (
                NearestReference(
                    reference,
                    compare_hashes(self._hashes(reference.path), candidate_hashes, self.config["band_limits"], self.config["consensus"]),
                )
                for reference in references
            ),
            key=lambda row: (*similarity_sort_key(row.comparison), row.reference.output_hash),
        )
        nearest = neighbors[0] if neighbors else None
        rejected = [row for row in neighbors if row.comparison.verdict in self.config["reject_verdicts"]]
        reasons = () if not rejected else (
            "perceptual_duplicate",
            f"perceptual_duplicate_image_{image_index}",
            f"perceptual_{rejected[0].comparison.verdict}_image_{image_index}",
        )
        return ImageDiversityVerdict(
            image_index,
            not rejected,
            "no_reference_yet" if not references else ("accepted" if not rejected else "rejected"),
            nearest,
            tuple(neighbors[: self.config["nearest_neighbors_to_persist"]]),
            len(references),
            reasons,
        )

    def evaluate_variant(
        self,
        listing_id: str,
        source_set_hash: str,
        images: Sequence[tuple[int, Path]],
        recipe_family: str = "appearance_only",
    ) -> VariantDiversityVerdict:
        verdicts = tuple(
            self.evaluate_image(listing_id, source_set_hash, index, path, recipe_family)
            for index, path in images
        )
        reasons = tuple(reason for verdict in verdicts for reason in verdict.reasons)
        return VariantDiversityVerdict(not reasons, verdicts, reasons)


def nearest_to_json(nearest: NearestReference | None) -> str:
    if nearest is None:
        return "{}"
    reference = nearest.reference
    comparison = nearest.comparison
    return json.dumps({
        "listing_id": reference.listing_id,
        "listing_code": reference.listing_code,
        "source_set_hash": reference.source_set_hash,
        "variant_id": reference.variant_id,
        "image_index": reference.image_index,
        "output_hash": reference.output_hash,
        "path": str(reference.path),
        "reference_kind": reference.reference_kind,
        "sha256_equal": comparison.sha256_equal,
        "phash": {"distance": comparison.phash.distance, "bits": 64, "band": comparison.phash.band},
        "dhash": {"distance": comparison.dhash.distance, "bits": 64, "band": comparison.dhash.band},
        "whash": {"distance": comparison.whash.distance, "bits": 64, "band": comparison.whash.band},
        "verdict": comparison.verdict,
        "reason": comparison.reason,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
