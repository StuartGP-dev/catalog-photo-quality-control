from __future__ import annotations

from collections import deque
from itertools import combinations, product
from typing import Any, Iterable

from .base import RangeSpec, StrategyBase, StrategyResult


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


class TargetHuntStrategy(StrategyBase):
    """Generic high-yield boundary search over a numeric search space.

    The strategy does not read reports, DB rows, labels, or outputs. It only
    produces the next candidate values from `(low, high, mode)` ranges.

    Compared with `boundary`, this strategy spends more candidates away from
    the mode and combines several focused parameters earlier. It is intended
    for benches where target outcomes are more likely around boundary zones or
    parameter interactions.

    Options:
    - focus_params: optional comma-separated parameter names to prioritize.
    - levels: number of fractions between mode and edges. Default: 11.
    - min_fraction: smallest distance from mode used in the main plan. Default: 0.50.
    - combo_size: maximum interaction size after one-parameter sweeps. Default: 3.
    - max_combos: cap for planned combination candidates. Default: 900.
    - jitter_ratio: optional local jitter as a fraction of each range. Default: 0.006.
    - shuffle: shuffle planned candidates with the strategy RNG. Default: false.
    - include_center: include the all-mode candidate. Default: false.
    - fallback: "target", "uniform" or "triangular" once the plan is exhausted.
    """

    name = "target_hunt"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self.levels = max(3, int(options.get("levels", 11)))
        self.min_fraction = _clamp(float(options.get("min_fraction", 0.50)), 0.0, 1.0)
        self.combo_size = max(1, int(options.get("combo_size", 3)))
        self.max_combos = max(0, int(options.get("max_combos", 900)))
        self.jitter_ratio = max(0.0, float(options.get("jitter_ratio", 0.006)))
        self.shuffle = _as_bool(options.get("shuffle"), False)
        self.include_center = _as_bool(options.get("include_center"), False)
        self.fallback = str(options.get("fallback", "target")).strip().lower()
        self.focus_params = self._parse_focus_params(options.get("focus_params"))
        self._keys = self._ordered_keys()
        planned = self._build_plan()
        if self.shuffle:
            self._rng.shuffle(planned)
        self._queue = deque(planned)

    def next_values(self) -> StrategyResult:
        if self._queue:
            values = self._queue.popleft()
        else:
            values = self._fallback_values()
        if self.jitter_ratio > 0:
            values = self._jitter(values)
        return StrategyResult(values=values, attempts=1)

    def _parse_focus_params(self, raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(raw, Iterable):
            return [str(part).strip() for part in raw if str(part).strip()]
        return []

    def _ordered_keys(self) -> list[str]:
        keys = list(self.space)
        focus = [key for key in self.focus_params if key in self.space]
        rest = [key for key in keys if key not in focus]
        return focus + rest

    def _build_plan(self) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        seen: set[tuple[tuple[str, Any], ...]] = set()
        mode_values = {key: self._value_at_mode(self.space[key]) for key in self._keys}

        def add(candidate: dict[str, Any]) -> None:
            signature = tuple(sorted(candidate.items()))
            if signature in seen:
                return
            seen.add(signature)
            plan.append(candidate)

        if self.include_center:
            add(dict(mode_values))

        fractions = self._fractions()

        # 1) Focused one-parameter sweeps, strongest fractions first.
        for key in self._keys:
            for fraction in fractions:
                for direction in (1, -1):
                    candidate = dict(mode_values)
                    candidate[key] = self._value_towards_edge(self.space[key], fraction, direction)
                    add(candidate)

        # 2) Same-direction global rings across focus params. This quickly probes
        # coherent high-distance zones without requiring a huge sample count.
        focus_or_all = [key for key in self.focus_params if key in self.space] or self._keys
        for fraction in fractions:
            for direction in (1, -1):
                candidate = dict(mode_values)
                for key in focus_or_all:
                    candidate[key] = self._value_towards_edge(self.space[key], fraction, direction)
                add(candidate)

        # 3) Pairwise / n-way interactions on prioritized params first.
        combo_keys = focus_or_all + [key for key in self._keys if key not in focus_or_all]
        combo_count = 0
        max_size = min(self.combo_size, len(combo_keys))
        for size in range(2, max_size + 1):
            for selected in combinations(combo_keys, size):
                for fraction in fractions:
                    for directions in product((-1, 1), repeat=size):
                        candidate = dict(mode_values)
                        for key, direction in zip(selected, directions):
                            candidate[key] = self._value_towards_edge(self.space[key], fraction, direction)
                        add(candidate)
                        combo_count += 1
                        if combo_count >= self.max_combos:
                            return plan
        return plan

    def _fractions(self) -> list[float]:
        # Highest-distance candidates first. Keep bisection-like fractions, but
        # start near the edges because this strategy is target-oriented.
        base = [1.0, 0.875, 0.75, 0.625, 0.50, 0.375, 0.25, 0.125]
        generated = [idx / (self.levels - 1) for idx in range(1, self.levels)]
        values: list[float] = []
        for value in base + sorted(generated, reverse=True):
            value = round(_clamp(float(value), 0.0, 1.0), 6)
            if value < self.min_fraction:
                continue
            if value > 0 and value not in values:
                values.append(value)
        return values or [1.0]

    def _value_at_mode(self, spec: RangeSpec) -> Any:
        return self._coerce_value(spec, float(spec[2]))

    def _value_towards_edge(self, spec: RangeSpec, fraction: float, direction: int) -> Any:
        low, high, mode = (float(spec[0]), float(spec[1]), float(spec[2]))
        target = low if direction < 0 else high
        value = mode + (target - mode) * _clamp(float(fraction), 0.0, 1.0)
        return self._coerce_value(spec, value)

    def _coerce_value(self, spec: RangeSpec, raw_value: float) -> Any:
        low, high, _mode = spec
        value = _clamp(float(raw_value), float(low), float(high))
        if all(isinstance(item, int) for item in spec):
            return int(round(value))
        return round(value, 4)

    def _jitter(self, values: dict[str, Any]) -> dict[str, Any]:
        jittered: dict[str, Any] = {}
        for key, value in values.items():
            spec = self.space[key]
            low, high, _mode = spec
            span = float(high) - float(low)
            if span <= 0:
                jittered[key] = value
                continue
            delta = self._rng.uniform(-span * self.jitter_ratio, span * self.jitter_ratio)
            jittered[key] = self._coerce_value(spec, float(value) + delta)
        return jittered

    def _fallback_values(self) -> dict[str, Any]:
        if self.fallback == "uniform":
            return {key: self.draw_uniform(self._rng, self.space[key]) for key in self._keys}
        if self.fallback == "triangular":
            return {key: self.draw_triangular(self._rng, self.space[key]) for key in self._keys}

        values: dict[str, Any] = {}
        fractions = self._fractions()
        for key in self._keys:
            spec = self.space[key]
            if self._rng.random() < 0.82:
                fraction = self._rng.choice(fractions)
                direction = -1 if self._rng.random() < 0.5 else 1
                values[key] = self._value_towards_edge(spec, fraction, direction)
            else:
                values[key] = self.draw_uniform(self._rng, spec)
        return values
