
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import StrategyBase, StrategyResult


class ClusterAwareHuntStrategy(StrategyBase):
    name = "cluster_aware_hunt"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self._index = 0
        self._clusters = self._load_clusters()
        self._plan = self._build_plan()

    def _option_float(self, key: str, default: float) -> float:
        try:
            return float(self.options.get(key, default))
        except Exception:
            return default

    def _option_int(self, key: str, default: int) -> int:
        try:
            return int(self.options.get(key, default))
        except Exception:
            return default

    def _option_list(self, key: str, default: list[str]) -> list[str]:
        value = self.options.get(key)
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(part).strip() for part in value if str(part).strip()]
        return list(default)

    def _spec(self, name: str) -> tuple[float, float, float]:
        low, high, mode = self.space[name]
        return float(low), float(high), float(mode)

    def _coerce(self, name: str, value: float) -> Any:
        low, high, mode = self.space[name]
        value = max(float(low), min(float(high), float(value)))
        if all(isinstance(x, int) for x in (low, high, mode)):
            return int(round(value))
        return round(float(value), 4)

    def _base_values(self) -> dict[str, Any]:
        return {name: self._coerce(name, float(spec[2])) for name, spec in self.space.items()}

    def _at(self, name: str, side: str, fraction: float) -> Any:
        low, high, mode = self._spec(name)
        fraction = max(0.0, min(1.0, float(fraction)))
        if side == "high":
            return self._coerce(name, mode + (high - mode) * fraction)
        if side == "low":
            return self._coerce(name, mode - (mode - low) * fraction)
        return self._coerce(name, mode)

    def _load_clusters(self) -> list[dict[str, Any]]:
        path = self.options.get("clusters_json") or self.options.get("cluster_json") or self.options.get("clusters")
        if not path:
            return []
        p = Path(str(path))
        if not p.exists():
            return []
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        clusters = payload.get("clusters", [])
        if not isinstance(clusters, list):
            return []
        return [c for c in clusters if isinstance(c, dict)]

    def _vector(self, values: dict[str, Any], names: list[str]) -> list[float]:
        out: list[float] = []
        for name in names:
            if name not in self.space:
                continue
            low, high, mode = self._spec(name)
            raw = values.get(name, mode)
            try:
                value = float(raw)
            except Exception:
                value = mode
            if high <= low:
                out.append(0.0)
            else:
                out.append(max(0.0, min(1.0, (value - low) / (high - low))))
        return out

    def _distance(self, a: dict[str, Any], b: dict[str, Any], names: list[str]) -> float:
        va = self._vector(a, names)
        vb = self._vector(b, names)
        if not va or not vb:
            return 0.0
        return sum(abs(x - y) for x, y in zip(va, vb)) / len(va)

    def _coerce_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            return {}
        values = self._base_values()
        for name, value in params.items():
            if name in self.space:
                try:
                    values[name] = self._coerce(name, float(value))
                except Exception:
                    pass
        return values

    def _cluster_param_sets(self, cluster: dict[str, Any]) -> list[dict[str, Any]]:
        """Return useful anchors for a cluster.

        V1 only used params_mean, which is dangerous when a cluster accidentally
        merges opposite signs: +2 and -2 average around 0.  V2 prioritises
        top_params and also samples min/max corners so the stage 2 search keeps
        the strong edges of every family.
        """
        anchors: list[dict[str, Any]] = []
        preferred = str(self.options.get("cluster_source", "top")).strip().lower()
        ordered_keys = ["top_params", "params_mean", "params", "params_min", "params_max"]
        if preferred == "mean":
            ordered_keys = ["params_mean", "top_params", "params", "params_min", "params_max"]

        for key in ordered_keys:
            params = cluster.get(key)
            if isinstance(params, dict):
                coerced = self._coerce_params(params)
                if coerced:
                    anchors.append(coerced)

        # Add deterministic mixed corners from min/max ranges.
        pmin = cluster.get("params_min")
        pmax = cluster.get("params_max")
        if isinstance(pmin, dict) and isinstance(pmax, dict):
            for choose_max in (False, True):
                values = self._base_values()
                for name in self.space:
                    src = pmax if choose_max else pmin
                    if name in src:
                        try:
                            values[name] = self._coerce(name, float(src[name]))
                        except Exception:
                            pass
                anchors.append(values)

            # Hybrid corners: geometric high/low groups separated from quality/canvas groups.
            for high_names in (
                {"blur", "crop", "zoom", "canvas_pad"},
                {"quality", "canvas_gray", "canvas_auto"},
                {"angle", "blur", "crop", "zoom", "canvas_pad"},
            ):
                values = self._base_values()
                for name in self.space:
                    src = pmax if name in high_names else pmin
                    if name in src:
                        try:
                            values[name] = self._coerce(name, float(src[name]))
                        except Exception:
                            pass
                anchors.append(values)

        # Stable de-duplication after coercion.
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for values in anchors:
            key = repr(sorted(values.items()))
            if key not in seen:
                seen.add(key)
                out.append(values)
        return out

    def _apply_jitter(self, values: dict[str, Any]) -> dict[str, Any]:
        jitter_ratio = max(0.0, self._option_float("jitter_ratio", 0.0))
        if jitter_ratio <= 0:
            return values
        out = dict(values)
        for name, spec in self.space.items():
            low, high, mode = spec
            if all(isinstance(x, int) for x in spec):
                continue
            span = float(high) - float(low)
            if span <= 0:
                continue
            out[name] = self._coerce(name, float(out.get(name, mode)) + self._rng.uniform(-span * jitter_ratio, span * jitter_ratio))
        return out

    def _mirror(self, values: dict[str, Any]) -> dict[str, Any]:
        out = dict(values)
        if "angle" in out and "angle" in self.space:
            out["angle"] = self._coerce("angle", -float(out["angle"]))
        return out

    def _flip_name(self, values: dict[str, Any], name: str, fraction: float) -> dict[str, Any]:
        out = dict(values)
        if name not in self.space:
            return out
        low, high, mode = self._spec(name)
        current = float(out.get(name, mode))
        side = "low" if current >= mode else "high"
        out[name] = self._at(name, side, fraction)
        return out

    def _novelty(self, values: dict[str, Any], cluster_values: list[dict[str, Any]], names: list[str]) -> float:
        distances = [self._distance(values, c, names) for c in cluster_values]
        return min(distances) if distances else 1.0

    def _dedupe_append(self, plan: list[dict[str, Any]], seen: set[str], values: dict[str, Any], names: list[str], min_plan_distance: float) -> None:
        values = self._apply_jitter(values)
        key = repr(sorted(values.items()))
        if key in seen:
            return
        if min_plan_distance > 0 and plan:
            if min(self._distance(values, existing, names) for existing in plan[-300:]) < min_plan_distance:
                return
        seen.add(key)
        plan.append(values)

    def _random_extreme(self, names: list[str], min_fraction: float) -> dict[str, Any]:
        values = self._base_values()
        for name in names:
            if name not in self.space:
                continue
            choice = self._rng.choice(["high", "low", "mode"])
            if choice == "mode":
                values[name] = self._at(name, "mode", min_fraction)
            else:
                values[name] = self._at(name, choice, self._rng.uniform(min_fraction, 1.0))
        return values

    def _build_plan(self) -> list[dict[str, Any]]:
        focus = self._option_list(
            "focus_params",
            ["angle", "crop", "blur", "quality", "zoom", "canvas_pad", "canvas_gray", "canvas_auto"],
        )
        focus = [name for name in focus if name in self.space]
        min_fraction = max(0.0, min(1.0, self._option_float("min_fraction", 0.45)))
        max_plan = self._option_int("max_plan", 100000)
        pool_size = self._option_int("pool_size", 12000)
        min_plan_distance = max(0.0, self._option_float("min_plan_distance", 0.015))

        cluster_values: list[dict[str, Any]] = []
        for cluster in self._clusters:
            cluster_values.extend(self._cluster_param_sets(cluster))
        cluster_values = [c for c in cluster_values if c]

        plan: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 1) Variantes autour des moyennes de clusters, avec flips explicites.
        for cluster_base in cluster_values:
            bases = [cluster_base, self._mirror(cluster_base)]
            for base in bases:
                self._dedupe_append(plan, seen, base, focus, min_plan_distance)
                for name in focus:
                    for frac in (0.55, 0.70, 0.85, 1.0):
                        self._dedupe_append(plan, seen, self._flip_name(base, name, frac), focus, min_plan_distance)

                # Group flips utiles pour chercher des familles non vues.
                groups = [
                    ["angle"],
                    ["blur", "crop", "canvas_pad", "zoom"],
                    ["quality", "canvas_gray", "canvas_auto"],
                    ["blur", "quality"],
                    ["crop", "zoom"],
                    ["canvas_pad", "canvas_gray"],
                    ["angle", "quality", "canvas_gray", "canvas_auto"],
                    ["angle", "blur", "crop", "canvas_pad", "zoom"],
                ]
                for group in groups:
                    current = dict(base)
                    for name in group:
                        current = self._flip_name(current, name, 1.0)
                    self._dedupe_append(plan, seen, current, focus, min_plan_distance)
                if len(plan) >= max_plan:
                    return plan[:max_plan]

        # 2) Pool anti-clusters : candidates loin des clusters et du plan.
        scored: list[tuple[float, dict[str, Any]]] = []
        for _ in range(max(0, pool_size)):
            values = self._random_extreme(focus, min_fraction)
            novelty = self._novelty(values, cluster_values, focus)
            # Slight preference for candidates not too close to the current plan.
            if plan:
                novelty = min(novelty, min(self._distance(values, existing, focus) for existing in plan[-500:]))
            scored.append((novelty, values))
        scored.sort(key=lambda item: item[0], reverse=True)
        for novelty, values in scored:
            self._dedupe_append(plan, seen, values, focus, min_plan_distance)
            if len(plan) >= max_plan:
                break

        # Fallback if no cluster file was provided or no plan generated.
        while len(plan) < min(max_plan, 1000):
            self._dedupe_append(plan, seen, self._random_extreme(focus, min_fraction), focus, min_plan_distance=0.0)

        return plan[:max_plan]

    def next_values(self) -> StrategyResult:
        if self._index < len(self._plan):
            values = self._plan[self._index]
            self._index += 1
            return StrategyResult(values=values, attempts=1)
        focus = self._option_list("focus_params", ["angle", "crop", "blur", "quality", "zoom", "canvas_pad", "canvas_gray", "canvas_auto"])
        values = self._random_extreme([name for name in focus if name in self.space], self._option_float("min_fraction", 0.45))
        return StrategyResult(values=self._apply_jitter(values), attempts=1)
