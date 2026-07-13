from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import Recipe


OPERATORS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
}


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    kind: str
    enabled: bool
    default: Any
    minimum: float | int | None
    maximum: float | int | None
    activation_probability: float
    distribution: str
    choices: tuple[str, ...] = ()
    active_minimum: float | int | None = None
    active_maximum: float | int | None = None
    active_absolute_minimum: float | int | None = None

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> "ParameterSpec":
        kind = raw.get("type")
        if kind not in {"float", "int", "choice"}:
            raise ValueError(f"{name}: unsupported type {kind!r}")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"{name}: enabled must be boolean")
        probability = raw.get("activation_probability", 1.0)
        if not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
            raise ValueError(f"{name}: activation_probability must be between 0 and 1")
        distribution = raw.get("distribution", "uniform")
        if distribution not in {"uniform", "triangular", "choice"}:
            raise ValueError(f"{name}: unsupported distribution {distribution!r}")

        minimum = raw.get("min")
        maximum = raw.get("max")
        choices = tuple(raw.get("choices", ()))
        default = raw.get("default")
        active_minimum = raw.get("active_min")
        active_maximum = raw.get("active_max")
        active_absolute_minimum = raw.get("active_min_abs")
        if kind in {"float", "int"}:
            if not isinstance(minimum, (int, float)) or not isinstance(
                maximum, (int, float)
            ):
                raise ValueError(f"{name}: numeric min and max are required")
            if minimum > maximum:
                raise ValueError(f"{name}: min exceeds max")
            if not isinstance(default, (int, float)) or not minimum <= default <= maximum:
                raise ValueError(f"{name}: default is outside configured range")
            if active_minimum is not None and (
                not isinstance(active_minimum, (int, float))
                or not minimum <= active_minimum <= maximum
            ):
                raise ValueError(f"{name}: active_min is outside configured range")
            if active_maximum is not None and (
                not isinstance(active_maximum, (int, float))
                or not minimum <= active_maximum <= maximum
            ):
                raise ValueError(f"{name}: active_max is outside configured range")
            if active_minimum is not None and active_maximum is not None and active_minimum > active_maximum:
                raise ValueError(f"{name}: active_min exceeds active_max")
            if active_absolute_minimum is not None and (
                not isinstance(active_absolute_minimum, (int, float))
                or active_absolute_minimum <= 0
                or active_absolute_minimum > max(abs(minimum), abs(maximum))
            ):
                raise ValueError(f"{name}: active_min_abs is outside configured range")
        elif not choices or default not in choices:
            raise ValueError(f"{name}: choice default must occur in choices")
        return cls(
            name,
            kind,
            enabled,
            default,
            minimum,
            maximum,
            float(probability),
            distribution,
            choices,
            active_minimum,
            active_maximum,
            active_absolute_minimum,
        )

    def normalize(self, value: Any) -> Any:
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"{self.name}: expected one of {self.choices}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.name}: expected a number")
        if not self.minimum <= value <= self.maximum:  # type: ignore[operator]
            raise ValueError(
                f"{self.name}: {value} is outside [{self.minimum}, {self.maximum}]"
            )
        if value != self.default:
            if self.active_minimum is not None and value < self.active_minimum:
                raise ValueError(
                    f"{self.name}: active value is below {self.active_minimum}"
                )
            if self.active_maximum is not None and value > self.active_maximum:
                raise ValueError(
                    f"{self.name}: active value exceeds {self.active_maximum}"
                )
            if (
                self.active_absolute_minimum is not None
                and abs(value) < self.active_absolute_minimum
            ):
                raise ValueError(
                    f"{self.name}: active absolute value is below "
                    f"{self.active_absolute_minimum}"
                )
        if not self.enabled and value != self.default:
            raise ValueError(f"{self.name}: parameter is disabled")
        return int(value) if self.kind == "int" else float(value)


@dataclass(frozen=True, slots=True)
class RecipeSchema:
    parameters: Mapping[str, ParameterSpec]
    compatibility_rules: tuple[Mapping[str, Any], ...]
    maximum_active_parameters: int
    maximum_recipe_intensity: float
    geometry_template_probability: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RecipeSchema":
        if raw.get("version") not in {1, 2, 3}:
            raise ValueError("unsupported filter space version")
        parameter_data = raw.get("parameters")
        if not isinstance(parameter_data, Mapping) or not parameter_data:
            raise ValueError("filter space parameters must be a non-empty mapping")
        parameters = {
            str(name): ParameterSpec.from_mapping(str(name), data)
            for name, data in parameter_data.items()
        }
        rules = raw.get("compatibility_rules", [])
        if not isinstance(rules, list):
            raise ValueError("compatibility_rules must be a list")
        quality = raw.get("quality_thresholds", {})
        schema = cls(
            parameters,
            tuple(rules),
            int(quality.get("maximum_active_parameters", 4)),
            float(quality.get("maximum_recipe_intensity", 1.35)),
            float(raw.get("geometry_template_probability", 0.0)),
        )
        if not 0 <= schema.geometry_template_probability <= 1:
            raise ValueError("geometry_template_probability must be between 0 and 1")
        schema.canonicalize({})
        return schema

    def canonicalize(self, supplied: Mapping[str, Any]) -> Recipe:
        unknown = set(supplied) - set(self.parameters)
        if unknown:
            raise ValueError(f"unknown recipe parameters: {sorted(unknown)}")
        values = {
            name: spec.normalize(supplied.get(name, spec.default))
            for name, spec in sorted(self.parameters.items())
        }
        self._validate_compatibility(values)
        analysis = analyze_recipe(values, self.parameters)
        if analysis.active_parameter_count > self.maximum_active_parameters:
            raise ValueError("too_many_active_parameters")
        if analysis.recipe_intensity > self.maximum_recipe_intensity:
            raise ValueError("recipe_too_intense")
        return Recipe.from_parameters(values)

    def _matches(self, condition: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
        parameter = condition.get("parameter")
        operator = condition.get("operator", "eq")
        if parameter not in self.parameters or operator not in OPERATORS:
            raise ValueError(f"invalid compatibility condition: {condition}")
        return bool(OPERATORS[operator](values[parameter], condition.get("value")))

    def _validate_compatibility(self, values: Mapping[str, Any]) -> None:
        for rule in self.compatibility_rules:
            when = rule.get("when")
            require = rule.get("require")
            forbid = rule.get("forbid")
            if not isinstance(when, Mapping):
                raise ValueError(f"compatibility rule lacks when condition: {rule}")
            if self._matches(when, values):
                if isinstance(require, Mapping) and not self._matches(require, values):
                    raise ValueError(rule.get("message", "compatibility requirement failed"))
                if isinstance(forbid, Mapping) and self._matches(forbid, values):
                    raise ValueError(rule.get("message", "forbidden parameter combination"))


GEOMETRY_PARAMETERS = (
    "rotation_degrees",
    "crop_fraction",
    "zoom",
    "resize_scale",
    "offset_x",
    "offset_y",
)


def classify_recipe_family(values: Mapping[str, Any]) -> str:
    """Return the deterministic, user-facing geometric family of a recipe.

    Offsets are treated as companions of zoom and dezoom. Rotation with its
    compensating crop remains a rotation recipe; genuinely cross-family mixes
    are classified as mixed geometry.
    """
    rotation = abs(float(values.get("rotation_degrees", 0.0))) > 1e-12
    crop = float(values.get("crop_fraction", 0.0)) > 0
    zoom = float(values.get("zoom", 1.0)) > 1.0
    dezoom = float(values.get("resize_scale", 1.0)) < 1.0
    offset = abs(float(values.get("offset_x", 0.0))) > 1e-12 or abs(
        float(values.get("offset_y", 0.0))
    ) > 1e-12
    if dezoom:
        return "mixed_geometry_family" if (crop or zoom) else "dezoom_canvas_family"
    if rotation:
        return "mixed_geometry_family" if (zoom or offset) else "rotation_family"
    if zoom:
        return "zoom_family"
    if crop:
        return "mixed_geometry_family" if offset else "crop_family"
    if offset:
        return "offset_family"
    return "appearance_only"


@dataclass(frozen=True, slots=True)
class RecipeAnalysis:
    active_parameter_count: int
    recipe_intensity: float
    active_parameters: tuple[str, ...]


def analyze_recipe(
    values: Mapping[str, Any], specs: Mapping[str, ParameterSpec]
) -> RecipeAnalysis:
    active: list[str] = []
    intensity = 0.0
    for name, spec in sorted(specs.items()):
        if name == "jpeg_quality":
            continue
        value = values.get(name, spec.default)
        if value == spec.default:
            continue
        active.append(name)
        if spec.kind == "choice":
            intensity += 0.25
        else:
            span = max(
                abs(float(spec.minimum) - float(spec.default)),
                abs(float(spec.maximum) - float(spec.default)),
            )
            intensity += abs(float(value) - float(spec.default)) / max(span, 1e-12)
    return RecipeAnalysis(len(active), round(intensity, 12), tuple(active))
