from __future__ import annotations

from collections import deque
from itertools import combinations
from typing import Any, Iterable

from .base import RangeSpec, StrategyBase, StrategyResult


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class BoundaryStrategy(StrategyBase):
    """Generic boundary-oriented search strategy over a numeric search space.

    The strategy is intentionally generic. It only manipulates parameter names and
    `(low, high, mode)` tuples. It does not read the DB and does not know how
    results are evaluated.

    Options:
    - levels: number of bisection-like fractions per direction. Default: 9.
    - jitter_ratio: optional local jitter as a fraction of each range. Default: 0.01.
    - pairwise: include two-parameter boundary combinations. Default: true.
    - max_pairwise: cap for pairwise candidates. Default: 240.
    - shuffle: shuffle the initial candidate queue with the strategy RNG. Default: false.
    - focus_params: optional list or comma-separated names to prioritize.
    - fallback: "mixed", "triangular" or "uniform" once the planned queue is exhausted.
    """

    name = "boundary"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self.levels = max(3, int(options.get("levels", 9)))
        self.jitter_ratio = max(0.0, float(options.get("jitter_ratio", 0.01)))
        self.pairwise = bool(options.get("pairwise", True))
        self.max_pairwise = max(0, int(options.get("max_pairwise", 240)))
        self.shuffle = bool(options.get("shuffle", False))
        self.fallback = str(options.get("fallback", "mixed")).strip().lower()
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

        def add(values: dict[str, Any]) -> None:
            signature = tuple(sorted(values.items()))
            if signature in seen:
                return
            seen.add(signature)
            plan.append(values)

        mode_values = {key: self._value_at_mode(self.space[key]) for key in self._keys}
        add(mode_values)

        # One-parameter sweeps using bisection-like fractions from mode to each edge.
        for key in self._keys:
            for ratio in self._ratios_for_spec(self.space[key]):
                candidate = dict(mode_values)
                candidate[key] = self._value_at_ratio(self.space[key], ratio)
                add(candidate)

        # Balanced all-parameter rings. These produce coherent points where all
        # parameters move a comparable distance away from their mode values.
        ring_fractions = self._ring_fractions()
        for fraction in ring_fractions:
            for direction in (-1, 1):
                candidate = {}
                for key in self._keys:
                    candidate[key] = self._value_towards_edge(self.space[key], fraction, direction)
                add(candidate)

        # Pairwise rings to explore interactions without exploding the search space.
        if self.pairwise and len(self._keys) >= 2 and self.max_pairwise > 0:
            pair_count = 0
            for left, right in combinations(self._keys, 2):
                for fraction in ring_fractions:
                    for left_dir, right_dir in ((-1, -1), (1, 1), (-1, 1), (1, -1)):
                        candidate = dict(mode_values)
                        candidate[left] = self._value_towards_edge(self.space[left], fraction, left_dir)
                        candidate[right] = self._value_towards_edge(self.space[right], fraction, right_dir)
                        add(candidate)
                        pair_count += 1
                        if pair_count >= self.max_pairwise:
                            return plan

        return plan

    def _ring_fractions(self) -> list[float]:
        # Bisection-like order: middle, quarters, eighths, then edge.
        base = [0.50, 0.25, 0.75, 0.125, 0.375, 0.625, 0.875, 1.0]
        if self.levels <= len(base):
            return base[: self.levels]
        extra = [idx / (self.levels - 1) for idx in range(1, self.levels - 1)]
        values = []
        for value in base + extra + [1.0]:
            value = round(float(value), 6)
            if 0.0 < value <= 1.0 and value not in values:
                values.append(value)
        return values

    def _ratios_for_spec(self, spec: RangeSpec) -> list[float]:
        low, high, mode = (float(spec[0]), float(spec[1]), float(spec[2]))
        if high == low:
            return [0.0]
        mode_ratio = _clamp((mode - low) / (high - low), 0.0, 1.0)
        ratios: list[float] = []
        for fraction in self._ring_fractions():
            for edge in (0.0, 1.0):
                ratio = mode_ratio + (edge - mode_ratio) * fraction
                ratio = round(_clamp(ratio, 0.0, 1.0), 6)
                if ratio not in ratios:
                    ratios.append(ratio)
        return ratios

    def _value_at_mode(self, spec: RangeSpec) -> Any:
        return self._coerce_value(spec, float(spec[2]))

    def _value_at_ratio(self, spec: RangeSpec, ratio: float) -> Any:
        low, high, _mode = spec
        value = float(low) + (float(high) - float(low)) * _clamp(float(ratio), 0.0, 1.0)
        return self._coerce_value(spec, value)

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

        # Mixed fallback: mostly boundary-biased, sometimes pure uniform.
        values: dict[str, Any] = {}
        for key in self._keys:
            spec = self.space[key]
            roll = self._rng.random()
            if roll < 0.25:
                values[key] = self.draw_uniform(self._rng, spec)
            elif roll < 0.60:
                fraction = self._rng.choice(self._ring_fractions())
                direction = -1 if self._rng.random() < 0.5 else 1
                values[key] = self._value_towards_edge(spec, fraction, direction)
            else:
                values[key] = self.draw_triangular(self._rng, spec)
        return values
