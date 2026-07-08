from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Final

from .base import StrategyBase

StrategyClass = type[StrategyBase]

_RESERVED_MODULES: Final[set[str]] = {
    "__init__",
    "base",
    "factory",
    "registry",
}

_STRATEGIES: dict[str, StrategyClass] = {}
_DISCOVERED_PACKAGES: set[str] = set()


def _normalize_strategy_name(name: str) -> str:
    normalized = str(name).strip()
    if not normalized:
        raise ValueError("Nom de strategie vide.")
    return normalized


def register_strategy(strategy_cls: StrategyClass) -> StrategyClass:
    """Register a StrategyBase subclass.

    This can be used as a decorator by plugin modules, but it is optional:
    discovery also auto-registers StrategyBase subclasses found in strategy/*.py.
    """

    if not inspect.isclass(strategy_cls) or not issubclass(strategy_cls, StrategyBase):
        raise TypeError("register_strategy attend une classe heritant de StrategyBase.")

    if strategy_cls is StrategyBase:
        raise ValueError("StrategyBase ne peut pas etre enregistree comme strategie concrete.")

    name = _normalize_strategy_name(getattr(strategy_cls, "name", ""))
    if name == StrategyBase.name:
        raise ValueError(f"Nom de strategie reserve: {name!r}")

    existing = _STRATEGIES.get(name)
    if existing is not None and existing is not strategy_cls:
        raise ValueError(
            f"Strategie deja enregistree pour {name!r}: "
            f"{existing.__module__}.{existing.__name__}"
        )

    _STRATEGIES[name] = strategy_cls
    return strategy_cls


def _iter_strategy_modules(package: ModuleType):
    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return

    for module_info in pkgutil.iter_modules(package_path):
        module_name = module_info.name
        if module_info.ispkg:
            continue
        if module_name.startswith("_"):
            continue
        if module_name in _RESERVED_MODULES:
            continue
        yield module_name


def _register_from_module(module: ModuleType) -> None:
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if obj is StrategyBase:
            continue
        if not issubclass(obj, StrategyBase):
            continue
        if obj.__module__ != module.__name__:
            continue
        register_strategy(obj)


def discover_strategies(package_name: str | None = None, *, force: bool = False) -> None:
    """Import strategy plugin modules and register their concrete classes.

    By default, discovery scans the current package directory. Any file directly
    placed in common/catalog_photo_control/strategy/ is a plugin candidate.
    """

    package_name = package_name or __package__
    if package_name is None:
        raise RuntimeError("Impossible de determiner le package strategy a scanner.")

    if not force and package_name in _DISCOVERED_PACKAGES:
        return

    package = importlib.import_module(package_name)
    for module_name in _iter_strategy_modules(package):
        importlib.import_module(f"{package_name}.{module_name}")

    for module_name in _iter_strategy_modules(package):
        module = importlib.import_module(f"{package_name}.{module_name}")
        _register_from_module(module)

    _DISCOVERED_PACKAGES.add(package_name)


def available_strategies() -> tuple[str, ...]:
    discover_strategies()
    return tuple(sorted(_STRATEGIES))


def get_strategy_class(name: str) -> StrategyClass:
    discover_strategies()
    normalized = _normalize_strategy_name(name)
    try:
        return _STRATEGIES[normalized]
    except KeyError as exc:
        available = ", ".join(available_strategies())
        raise ValueError(f"Strategie inconnue: {normalized}. Disponibles: {available}") from exc