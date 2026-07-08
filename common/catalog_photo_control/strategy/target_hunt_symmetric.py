from __future__ import annotations

from itertools import product
from typing import Any

from .base import StrategyBase, StrategyResult


class SymmetricTargetHuntStrategy(StrategyBase):
    name = "symmetric_target_hunt"

    def __init__(self, space, seed=None, **options: Any) -> None:
        super().__init__(space, seed=seed, **options)
        self._index = 0
        self._plan = self._build_plan()

    def _option_bool(self, key: str, default: bool) -> bool:
        value = self.options.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

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

    def _levels(self) -> list[float]:
        count = max(3, self._option_int("levels", 15))
        min_fraction = max(0.0, min(1.0, self._option_float("min_fraction", 0.55)))
        values = [min_fraction + (1.0 - min_fraction) * i / max(1, count - 1) for i in range(count)]
        values = sorted(set(round(v, 6) for v in values), reverse=True)
        return values

    def _spec(self, name: str) -> tuple[float, float, float]:
        low, high, mode = self.space[name]
        return float(low), float(high), float(mode)

    def _coerce(self, name: str, value: float) -> Any:
        low, high, mode = self.space[name]
        value = max(float(low), min(float(high), float(value)))
        if all(isinstance(x, int) for x in (low, high, mode)):
            return int(round(value))
        return round(float(value), 4)

    def _at(self, name: str, side: str, fraction: float) -> Any:
        low, high, mode = self._spec(name)

        if side == "mode":
            return self._coerce(name, mode)

        if side == "high":
            return self._coerce(name, mode + (high - mode) * fraction)

        if side == "low":
            return self._coerce(name, mode - (mode - low) * fraction)

        if side == "mirror_high":
            return self._coerce(name, mode - (mode - low) * fraction)

        if side == "mirror_low":
            return self._coerce(name, mode + (high - mode) * fraction)

        return self._coerce(name, mode)

    def _base_values(self) -> dict[str, Any]:
        return {name: self._coerce(name, float(spec[2])) for name, spec in self.space.items()}

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
            delta = self._rng.uniform(-span * jitter_ratio, span * jitter_ratio)
            out[name] = self._coerce(name, float(out[name]) + delta)
        return out

    def _candidate(self, family: dict[str, str], fraction: float) -> dict[str, Any]:
        values = self._base_values()
        for name, side in family.items():
            if name in self.space:
                values[name] = self._at(name, side, fraction)
        return self._apply_jitter(values)

    def _build_plan(self) -> list[dict[str, Any]]:
        levels = self._levels()

        focus_params = self._option_list(
            "focus_params",
            ["angle", "crop", "blur", "quality", "zoom", "canvas_pad", "canvas_gray", "canvas_auto"],
        )
        focus_params = [name for name in focus_params if name in self.space]

        families: list[dict[str, str]] = []

        # Famille A : géométrie haute + fond clair + zoom haut.
        families.append({
            "angle": "high",
            "crop": "high",
            "blur": "high",
            "quality": "high",
            "zoom": "high",
            "canvas_pad": "high",
            "canvas_gray": "high",
            "canvas_auto": "high",
        })

        # Famille A miroir : angle négatif, mais le reste reste haut.
        families.append({
            "angle": "low",
            "crop": "high",
            "blur": "high",
            "quality": "high",
            "zoom": "high",
            "canvas_pad": "high",
            "canvas_gray": "high",
            "canvas_auto": "high",
        })

        # Famille B : angle négatif + qualité basse + dézoom + gris.
        families.append({
            "angle": "low",
            "crop": "low",
            "blur": "low",
            "quality": "low",
            "zoom": "low",
            "canvas_pad": "low",
            "canvas_gray": "low",
            "canvas_auto": "low",
        })

        # Famille B miroir : angle positif + qualité basse + dézoom + gris.
        families.append({
            "angle": "high",
            "crop": "low",
            "blur": "low",
            "quality": "low",
            "zoom": "low",
            "canvas_pad": "low",
            "canvas_gray": "low",
            "canvas_auto": "low",
        })

        # Hybrides : on croise angle +/- avec familles hautes/basses.
        angle_sides = ["high", "low"] if "angle" in self.space else ["mode"]
        geometry_sides = [
            {"crop": "high", "blur": "high", "canvas_pad": "high", "zoom": "high"},
            {"crop": "low", "blur": "low", "canvas_pad": "low", "zoom": "low"},
            {"crop": "high", "blur": "low", "canvas_pad": "high", "zoom": "low"},
            {"crop": "low", "blur": "high", "canvas_pad": "low", "zoom": "high"},
        ]
        output_sides = [
            {"quality": "high", "canvas_gray": "high", "canvas_auto": "high"},
            {"quality": "low", "canvas_gray": "low", "canvas_auto": "low"},
            {"quality": "high", "canvas_gray": "low", "canvas_auto": "high"},
            {"quality": "low", "canvas_gray": "high", "canvas_auto": "low"},
        ]

        for angle_side in angle_sides:
            for geo in geometry_sides:
                for out in output_sides:
                    family = {"angle": angle_side}
                    family.update(geo)
                    family.update(out)
                    families.append(family)

        # Ablation : un paramètre fort à la fois, dans les deux sens.
        for name in focus_params:
            families.append({name: "high"})
            families.append({name: "low"})

        # Paires symétriques : toutes les paires principales, high/low croisé.
        max_pairwise = self._option_int("max_pairwise", 500)
        pair_count = 0
        for i, first in enumerate(focus_params):
            for second in focus_params[i + 1:]:
                for first_side, second_side in product(["high", "low"], repeat=2):
                    families.append({first: first_side, second: second_side})
                    pair_count += 1
                    if pair_count >= max_pairwise:
                        break
                if pair_count >= max_pairwise:
                    break
            if pair_count >= max_pairwise:
                break

        # Construction du plan.
        plan: list[dict[str, Any]] = []
        max_plan = self._option_int("max_plan", 100_000)
        seen: set[str] = set()

        for fraction in levels:
            for family in families:
                filtered = {name: side for name, side in family.items() if name in self.space}
                candidate = self._candidate(filtered, fraction)
                key = repr(sorted(candidate.items()))
                if key in seen:
                    continue
                seen.add(key)
                plan.append(candidate)
                if len(plan) >= max_plan:
                    return plan

        return plan

    def next_values(self) -> StrategyResult:
        if not self._plan:
            return StrategyResult(values=self._apply_jitter(self._base_values()), attempts=1)

        if self._index < len(self._plan):
            values = self._plan[self._index]
            self._index += 1
            return StrategyResult(values=values, attempts=1)

        # Fallback : mélange aléatoire orienté extrêmes.
        fraction = self._rng.uniform(
            max(0.0, min(1.0, self._option_float("min_fraction", 0.55))),
            1.0,
        )
        values = self._base_values()
        for name in self.space:
            side = self._rng.choice(["high", "low", "mode"])
            values[name] = self._at(name, side, fraction)
        return StrategyResult(values=self._apply_jitter(values), attempts=1)
