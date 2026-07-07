from __future__ import annotations

import os
import time
from dataclasses import dataclass
from random import Random
from typing import Any, Mapping

RangeSpec = tuple[float, float, float] | tuple[int, int, int]
SpaceSpec = Mapping[str, RangeSpec]


def make_auto_seed() -> int:
    return int(time.time_ns() % 2_147_483_647) ^ os.getpid()


@dataclass(frozen=True)
class StrategyResult:
    values: dict[str, Any]
    attempts: int


class SearchStrategy:
    def __init__(self, space: SpaceSpec, seed: int | None = None) -> None:
        self.space = dict(space)
        self.seed = make_auto_seed() if seed is None else int(seed)
        self._rng = Random(self.seed)

    def next_values(self) -> StrategyResult:
        return StrategyResult(
            values={name: self._draw(spec) for name, spec in self.space.items()},
            attempts=1,
        )

    def _draw(self, spec: RangeSpec) -> Any:
        low, high, mode = spec
        if all(isinstance(value, int) for value in spec):
            return int(round(self._rng.triangular(int(low), int(high), int(mode))))
        return round(float(self._rng.triangular(float(low), float(high), float(mode))), 4)
