from __future__ import annotations

from .base import StrategyBase, StrategyResult


class UniformStrategy(StrategyBase):
    name = "uniform"

    def next_values(self) -> StrategyResult:
        return StrategyResult(
            values={name: self.draw_uniform(self._rng, spec) for name, spec in self.space.items()},
            attempts=1,
        )
