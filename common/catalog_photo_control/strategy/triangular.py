from __future__ import annotations

from .base import StrategyBase, StrategyResult


class TriangularStrategy(StrategyBase):
    name = "triangular"

    def next_values(self) -> StrategyResult:
        return StrategyResult(
            values={name: self.draw_triangular(self._rng, spec) for name, spec in self.space.items()},
            attempts=1,
        )
