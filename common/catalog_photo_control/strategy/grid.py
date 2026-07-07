from __future__ import annotations

from typing import Any

from .base import RangeSpec, StrategyBase, StrategyResult


class GridStrategy(StrategyBase):
    name = "grid"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self.levels = max(2, int(options.get("levels", 5)))
        self.offset = int(options.get("offset", self.seed % max(1, self.levels ** max(1, len(self.space)))))
        self._index = 0

    def next_values(self) -> StrategyResult:
        keys = list(self.space)
        total = self.levels ** max(1, len(keys))
        n = (self.offset + self._index) % total
        self._index += 1

        values: dict[str, Any] = {}
        for key in keys:
            level = n % self.levels
            n //= self.levels
            values[key] = self._value_at(self.space[key], level, self.levels)

        return StrategyResult(values=values, attempts=1)

    @staticmethod
    def _value_at(spec: RangeSpec, level: int, levels: int) -> Any:
        low, high, _mode = spec
        ratio = 0.0 if levels <= 1 else level / (levels - 1)
        if all(isinstance(value, int) for value in spec):
            return int(round(int(low) + (int(high) - int(low)) * ratio))
        return round(float(low) + (float(high) - float(low)) * ratio, 4)
