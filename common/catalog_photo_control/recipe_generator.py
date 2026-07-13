from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from .models import Recipe
from .recipe_schema import ParameterSpec, RecipeSchema


@dataclass(frozen=True, slots=True)
class RecipeProposal:
    recipe: Recipe
    source: str


class RecipeGenerator:
    def __init__(
        self,
        schema: RecipeSchema,
        allocation: dict[str, float],
        *,
        seed: int | None = None,
        mutation_fraction: float = 0.12,
    ):
        self.schema = schema
        self.allocation = allocation
        self.random = random.Random(seed)
        self.mutation_fraction = mutation_fraction

    def _sample_value(self, spec: ParameterSpec):
        if spec.kind == "choice":
            return self.random.choice(spec.choices)
        assert spec.minimum is not None and spec.maximum is not None
        if spec.kind == "int" and spec.distribution == "choice":
            candidates = list(range(int(spec.minimum), int(spec.maximum) + 1, 2))
            if int(spec.default) not in candidates:
                candidates.append(int(spec.default))
            return self.random.choice(candidates)
        low = spec.active_minimum if spec.active_minimum is not None else spec.minimum
        high = spec.active_maximum if spec.active_maximum is not None else spec.maximum
        mode = min(high, max(low, spec.default))
        if spec.distribution == "triangular":
            value = self.random.triangular(low, high, mode)
        else:
            value = self.random.uniform(low, high)
        return round(value) if spec.kind == "int" else value

    def _defaults(self) -> dict[str, object]:
        return {name: spec.default for name, spec in self.schema.parameters.items()}

    def _between(self, name: str, low: float, high: float, mode: float | None = None) -> float:
        spec = self.schema.parameters[name]
        assert spec.minimum is not None and spec.maximum is not None
        low = max(float(spec.minimum), low)
        high = min(float(spec.maximum), high)
        return self.random.triangular(low, high, mode if mode is not None else (low + high) / 2)

    def _geometry_template(self) -> Recipe:
        templates = (
            "crop", "crop", "rotation", "rotation_crop", "rotation_crop_zoom",
            "zoom_offset_x", "zoom_offset_y",
            "zoom_offsets", "crop_offsets", "dezoom_canvas", "dezoom_bands",
            "dezoom_frame", "rotation_dezoom_canvas", "rotation_dezoom_offset",
            "dezoom_sampled",
        )
        for _ in range(100):
            values = self._defaults()
            template = self.random.choice(templates)
            rotation = self._between("rotation_degrees", -1.2, 1.2, 0.0)
            if abs(rotation) < 0.18:
                rotation = 0.18 if rotation >= 0 else -0.18
            crop = self._between(
                "crop_fraction", 0.003, 0.006 if template == "crop" else 0.012, 0.004
            )
            zoom = self._between("zoom", 1.005, 1.018, 1.008)
            offset_x = self._between("offset_x", -0.014, 0.014, 0.0)
            offset_y = self._between("offset_y", -0.014, 0.014, 0.0)
            dezoom = self._between("resize_scale", 0.972, 0.995, 0.988)
            if template.startswith("rotation"):
                values["rotation_degrees"] = rotation
            if "crop" in template:
                values["crop_fraction"] = crop
            if "zoom" in template and "dezoom" not in template:
                values["zoom"] = zoom
            if template in {"zoom_offset_x", "zoom_offsets", "crop_offsets"}:
                values["offset_x"] = offset_x
            if template in {"zoom_offset_y", "zoom_offsets", "crop_offsets", "rotation_dezoom_offset"}:
                values["offset_y"] = offset_y
            if "dezoom" in template:
                values["resize_scale"] = dezoom
                values["canvas_mode"] = self.random.choice(
                    ["white", "light_gray", "sampled_background", "sampled_edge"]
                )
            if template == "dezoom_bands":
                values["canvas_mode"] = "side_bands"
                values["side_band_width"] = self._between("side_band_width", 0.006, 0.025, 0.012)
            elif template == "dezoom_frame":
                values["canvas_mode"] = "uniform_frame"
                values["uniform_frame_width"] = self._between("uniform_frame_width", 0.004, 0.014, 0.007)
            elif template == "dezoom_sampled":
                values["canvas_mode"] = "sampled_background"
            try:
                return self.schema.canonicalize(values)
            except ValueError:
                continue
        return self.schema.canonicalize({})

    def random_recipe(self) -> Recipe:
        if self.random.random() < self.schema.geometry_template_probability:
            return self._geometry_template()
        for _ in range(100):
            values = {}
            for name, spec in self.schema.parameters.items():
                active = spec.enabled and self.random.random() < spec.activation_probability
                values[name] = self._sample_value(spec) if active else spec.default
            mode_spec = self.schema.parameters.get("canvas_mode")
            if mode_spec:
                values["canvas_mode"] = mode_spec.default
            if mode_spec and mode_spec.enabled and self.random.random() < mode_spec.activation_probability:
                mode = self.random.choice(["white", "white", "light_gray", "light_gray", "sampled_background", "sampled_background", "sampled_edge", "side_bands", "uniform_frame"])
                values["canvas_mode"] = mode
                if mode == "side_bands":
                    values["side_band_width"] = self.random.triangular(0.008, 0.035, 0.014)
                elif mode == "uniform_frame":
                    values["uniform_frame_width"] = self.random.triangular(0.005, 0.02, 0.009)
                else:
                    values["canvas_padding_x"] = self.random.triangular(0.004, 0.03, 0.01)
                    values["canvas_padding_y"] = self.random.triangular(0.002, 0.018, 0.006)
            try:
                return self.schema.canonicalize(values)
            except ValueError:
                continue
        return self.schema.canonicalize({})

    def mutate(self, parent: Recipe) -> Recipe:
        for _ in range(100):
            values = dict(parent.parameters)
            enabled = [spec for spec in self.schema.parameters.values() if spec.enabled]
            count = max(1, min(4, round(len(enabled) * self.mutation_fraction)))
            for spec in self.random.sample(enabled, count):
                if spec.kind == "choice":
                    values[spec.name] = self.random.choice(spec.choices)
                    continue
                assert spec.minimum is not None and spec.maximum is not None
                span = spec.maximum - spec.minimum
                proposed = float(values[spec.name]) + self.random.gauss(0, span * self.mutation_fraction)
                proposed = min(spec.maximum, max(spec.minimum, proposed))
                values[spec.name] = round(proposed) if spec.kind == "int" else proposed
            try:
                mutated = self.schema.canonicalize(values)
                if mutated.recipe_hash != parent.recipe_hash:
                    return mutated
            except ValueError:
                continue
        return self.random_recipe()

    def propose(self, proven: Sequence[Recipe]) -> RecipeProposal:
        draw = self.random.random()
        random_limit = self.allocation["random"]
        proven_limit = random_limit + self.allocation["proven"]
        if draw < random_limit or not proven:
            return RecipeProposal(self.random_recipe(), "random")
        parent = self.random.choice(list(proven))
        if draw < proven_limit:
            try:
                return RecipeProposal(self.schema.canonicalize(parent.parameters), "proven")
            except ValueError:
                return RecipeProposal(self.random_recipe(), "random")
        return RecipeProposal(self.mutate(parent), "mutation")
