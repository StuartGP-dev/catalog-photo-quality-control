from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _normalize(value: Any) -> JsonValue:
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support non-finite numbers")
        return int(value) if value.is_integer() else value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceImage:
    index: int
    path: Path
    source_hash: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SourceListing:
    listing_id: str
    listing_code: str
    directory: Path
    images: tuple[SourceImage, ...]
    source_set_hash: str


@dataclass(frozen=True, slots=True)
class Recipe:
    parameters: Mapping[str, JsonValue]
    recipe_hash: str

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]) -> "Recipe":
        normalized = _normalize(parameters)
        if not isinstance(normalized, dict):
            raise TypeError("recipe parameters must be a mapping")
        return cls(parameters=normalized, recipe_hash=stable_hash(normalized))


@dataclass(frozen=True, slots=True)
class ImageMetrics:
    source_index: int
    output_hash: str
    quality_score: float
    distance_score: float
    components: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecipeTest:
    test_id: int | None
    listing_id: str
    source_set_hash: str
    recipe: Recipe
    complete: bool
    quality_valid: bool
    eligible: bool
    aggregate_metrics: Mapping[str, float]
    images: tuple[ImageMetrics, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ListingVariant:
    variant_id: int | None
    listing_id: str
    source_set_hash: str
    recipe: Recipe
    image_paths: tuple[Path, ...]
    selected_rank: int
    bench_test_id: int | None = None
    aggregate_metrics: Mapping[str, float] = field(default_factory=dict)
    distance_from_original: float = 0.0
    minimum_selected_distance: float | None = None
    minimum_distance_components: Mapping[str, float] = field(default_factory=dict)
    recipe_family: str = "appearance_only"
    title_text: str | None = None
    description_text: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    metadata_json: str | None = None
    metadata_status: str = "reserved"


@dataclass(frozen=True, slots=True)
class BenchRun:
    run_id: str
    listing_id: str
    source_set_hash: str
    target_variants: int
    started_at: str
    status: str = "running"
    stop_reason: str | None = None


def ordered_source_set_hash(images: Sequence[SourceImage]) -> str:
    ordered = [
        {"index": image.index, "source_hash": image.source_hash} for image in images
    ]
    return stable_hash(ordered)
