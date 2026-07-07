from __future__ import annotations

from typing import Any

from .base import RangeSpec, SpaceSpec, StrategyBase, StrategyResult, make_auto_seed, parse_options
from .grid import GridStrategy
from .triangular import TriangularStrategy
from .uniform import UniformStrategy

_STRATEGIES: dict[str, type[StrategyBase]] = {
    TriangularStrategy.name: TriangularStrategy,
    UniformStrategy.name: UniformStrategy,
    GridStrategy.name: GridStrategy,
}


def available_strategies() -> tuple[str, ...]:
    return tuple(sorted(_STRATEGIES))


def create_strategy(name: str, space: SpaceSpec, seed: int | None = None, **options: Any) -> StrategyBase:
    try:
        strategy_cls = _STRATEGIES[name]
    except KeyError as exc:
        raise ValueError(f"Strategie inconnue: {name}. Disponibles: {', '.join(available_strategies())}") from exc
    return strategy_cls(space, seed=seed, **options)


__all__ = [
    "RangeSpec",
    "SpaceSpec",
    "StrategyBase",
    "StrategyResult",
    "available_strategies",
    "create_strategy",
    "make_auto_seed",
    "parse_options",
]
