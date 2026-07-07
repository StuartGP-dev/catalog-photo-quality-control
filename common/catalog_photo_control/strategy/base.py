from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from random import Random
from typing import Any, Mapping

RangeSpec = tuple[float, float, float] | tuple[int, int, int]
SpaceSpec = Mapping[str, RangeSpec]


def make_auto_seed() -> int:
    return int(time.time_ns() % 2_147_483_647) ^ os.getpid()


def coerce_option_value(raw: str) -> Any:
    value = raw.strip()
    if value.lower() in {"true", "yes", "on"}:
        return True
    if value.lower() in {"false", "no", "off"}:
        return False
    if value.lower() in {"none", "null"}:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_option_items(items: list[str] | None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Option invalide: {item!r}. Format attendu: cle=valeur")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Option invalide: {item!r}. Cle vide.")
        options[key] = coerce_option_value(raw_value)
    return options


def parse_options(json_payload: str | None, items: list[str] | None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if json_payload:
        loaded = json.loads(json_payload)
        if not isinstance(loaded, dict):
            raise ValueError("--strategy-params doit contenir un objet JSON.")
        options.update(loaded)
    options.update(parse_option_items(items))
    return options


@dataclass(frozen=True)
class StrategyResult:
    values: dict[str, Any]
    attempts: int


class StrategyBase:
    name = "base"

    def __init__(self, space: SpaceSpec, seed: int | None = None, **options: Any) -> None:
        self.space = dict(space)
        self.seed = make_auto_seed() if seed is None else int(seed)
        self.options = dict(options)
        self._rng = Random(self.seed)

    def next_values(self) -> StrategyResult:
        raise NotImplementedError

    @staticmethod
    def draw_triangular(rng: Random, spec: RangeSpec) -> Any:
        low, high, mode = spec
        if all(isinstance(value, int) for value in spec):
            return int(round(rng.triangular(int(low), int(high), int(mode))))
        return round(float(rng.triangular(float(low), float(high), float(mode))), 4)

    @staticmethod
    def draw_uniform(rng: Random, spec: RangeSpec) -> Any:
        low, high, _mode = spec
        if all(isinstance(value, int) for value in spec):
            return int(rng.randint(int(low), int(high)))
        return round(float(rng.uniform(float(low), float(high))), 4)
