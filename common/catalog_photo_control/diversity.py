from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .metrics import metric_distance


@dataclass(frozen=True, slots=True)
class Distance:
    total: float
    components: Mapping[str, float]


def listing_distance(
    left: Mapping[str, float], right: Mapping[str, float]
) -> Distance:
    total, components = metric_distance(left, right)
    return Distance(total, components)
