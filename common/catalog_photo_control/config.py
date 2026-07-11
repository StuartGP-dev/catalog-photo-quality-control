from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .recipe_schema import RecipeSchema


DEFAULT_FILTER_SPACE = Path(__file__).resolve().parents[2] / "config" / "filter_space.json"


@dataclass(frozen=True, slots=True)
class FilterSpace:
    schema: RecipeSchema
    proposal_allocation: Mapping[str, float]
    quality_thresholds: Mapping[str, float]
    selection_pool_multiplier: int
    raw: Mapping[str, Any]


def load_filter_space(path: str | Path = DEFAULT_FILTER_SPACE) -> FilterSpace:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid filter-space JSON: {error}") from error
    if not isinstance(raw, Mapping):
        raise ValueError("filter space must be a JSON object")
    schema = RecipeSchema.from_mapping(raw)
    allocation = raw.get("proposal_allocation", {})
    required = {"random", "proven", "mutation"}
    if not isinstance(allocation, Mapping) or set(allocation) != required:
        raise ValueError(f"proposal_allocation must contain exactly {sorted(required)}")
    if any(not isinstance(value, (int, float)) or value < 0 for value in allocation.values()):
        raise ValueError("proposal allocation values must be non-negative")
    if abs(sum(float(value) for value in allocation.values()) - 1.0) > 1e-9:
        raise ValueError("proposal allocation values must sum to 1")
    thresholds = raw.get("quality_thresholds", {})
    if not isinstance(thresholds, Mapping):
        raise ValueError("quality_thresholds must be an object")
    pool_multiplier = raw.get("selection_pool_multiplier", 3)
    if not isinstance(pool_multiplier, int) or pool_multiplier < 1:
        raise ValueError("selection_pool_multiplier must be a positive integer")
    return FilterSpace(
        schema,
        {str(key): float(value) for key, value in allocation.items()},
        {str(key): float(value) for key, value in thresholds.items()},
        pool_multiplier,
        raw,
    )
