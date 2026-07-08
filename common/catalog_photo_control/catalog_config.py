from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ANNONCES_ROOT = Path.home() / "Documents" / "Code" / "Bot" / "annonces"
DEFAULT_OUTPUT_ROOT = Path("local") / "catalog_filter_engine"
DEFAULT_SQLITE_DSN = f"sqlite:///{DEFAULT_OUTPUT_ROOT.as_posix()}/catalog_filters.sqlite3"


@dataclass(frozen=True)
class CatalogSettings:
    annonces_root: Path
    output_root: Path
    db_dsn: str
    db_backend: str

    @property
    def is_postgres(self) -> bool:
        return self.db_backend == "postgres"

    @property
    def is_sqlite(self) -> bool:
        return self.db_backend == "sqlite"


def _coerce_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    return Path(value).expanduser()


def _detect_backend(dsn: str) -> str:
    lowered = dsn.lower().strip()
    if lowered.startswith(("postgresql://", "postgres://")):
        return "postgres"
    if lowered.startswith("sqlite:///") or lowered in {"sqlite", "sqlite://"}:
        return "sqlite"
    raise ValueError(
        "Unsupported CATALOG_DB_DSN. Expected postgresql://... or sqlite:///..."
    )


def load_settings(
    *,
    annonces_root: str | Path | None = None,
    output_root: str | Path | None = None,
    db_dsn: str | None = None,
) -> CatalogSettings:
    resolved_annonces_root = Path(
        annonces_root
        or _coerce_path(os.getenv("CATALOG_PHOTO_ANNONCES_ROOT"), DEFAULT_ANNONCES_ROOT)
    ).expanduser()
    resolved_output_root = Path(
        output_root
        or _coerce_path(os.getenv("CATALOG_PHOTO_OUTPUT_ROOT"), DEFAULT_OUTPUT_ROOT)
    ).expanduser()

    resolved_dsn = (
        db_dsn
        or os.getenv("CATALOG_DB_DSN")
        or os.getenv("DATABASE_URL")
        or DEFAULT_SQLITE_DSN
    )
    backend = _detect_backend(resolved_dsn)

    return CatalogSettings(
        annonces_root=resolved_annonces_root,
        output_root=resolved_output_root,
        db_dsn=resolved_dsn,
        db_backend=backend,
    )
