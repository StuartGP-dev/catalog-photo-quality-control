from __future__ import annotations

from typing import Any

from .base import SpaceSpec, StrategyBase
from .registry import get_strategy_class


def create_strategy(name: str, space: SpaceSpec, seed: int | None = None, **options: Any) -> StrategyBase:
    strategy_cls = get_strategy_class(name)
    return strategy_cls(space, seed=seed, **options)