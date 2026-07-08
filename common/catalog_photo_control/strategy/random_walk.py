from __future__ import annotations

from typing import Any

from .base import RangeSpec, StrategyBase, StrategyResult


class RandomWalkStrategy(StrategyBase):
    """Generic random-walk strategy over a numeric search space.

    Options:
    - step_ratio: fraction of each parameter range used as the max step size.
    - start: "mode", "low", "high" or "random".
    - clamp: keep values inside each parameter range. Defaults to True.

    The strategy is intentionally generic: it only manipulates parameter names
    and numeric ranges from the provided space.
    """

    name = "random_walk"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self.step_ratio = max(0.0, float(options.get("step_ratio", 0.10)))
        self.start = str(options.get("start", "mode")).strip().lower()
        self.clamp = bool(options.get("clamp", True))
        self._current = {key: self._initial_value(spec) for key, spec in self.space.items()}

    def next_values(self) -> StrategyResult:
        values: dict[str, Any] = {}
        for key, spec in self.space.items():
            low, high, _mode = spec
            span = float(high) - float(low)
            step = self._rng.uniform(-span * self.step_ratio, span * self.step_ratio)
            raw_value = float(self._current[key]) + step
            value = self._coerce_value(spec, raw_value)
            self._current[key] = value
            values[key] = value
        return StrategyResult(values=values, attempts=1)

    def _initial_value(self, spec: RangeSpec) -> Any:
        low, high, mode = spec
        if self.start == "low":
            raw_value = float(low)
        elif self.start == "high":
            raw_value = float(high)
        elif self.start == "random":
            raw_value = float(self.draw_uniform(self._rng, spec))
        else:
            raw_value = float(mode)
        return self._coerce_value(spec, raw_value)

    def _coerce_value(self, spec: RangeSpec, raw_value: float) -> Any:
        low, high, _mode = spec
        value = raw_value
        if self.clamp:
            value = min(float(high), max(float(low), value))
        if all(isinstance(item, int) for item in spec):
            return int(round(value))
        return round(float(value), 4)