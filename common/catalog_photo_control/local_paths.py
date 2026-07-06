from __future__ import annotations

import os
from pathlib import Path

# Repo root for this standalone catalog photo quality-control project.
# Keeping generated reports, local catalogs and debug bundles here makes runs
# independent from the caller working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ANNONCES_ROOT = Path(
    os.environ.get(
        "CATALOG_PHOTO_ANNONCES_ROOT",
        r"C:\Users\yanis\Documents\Code\Bot-Vinted\annonces",
    )
)

DEFAULT_LOCAL_OUTPUT_ROOT = Path(
    os.environ.get(
        "CATALOG_PHOTO_OUTPUT_ROOT",
        str(REPO_ROOT / "local" / "debug_catalog_photo_control"),
    )
)


def default_annonces_root() -> Path:
    """Folder where catalogue listing folders are read from by default."""
    return DEFAULT_ANNONCES_ROOT


def default_output_root() -> Path:
    """Repo-local folder for JSON catalogs, reports and debug bundles."""
    return DEFAULT_LOCAL_OUTPUT_ROOT


def describe_local_paths(
    annonces_root: str | Path | None = None,
    output_root: str | Path | None = None,
) -> dict[str, str]:
    return {
        "repo_root": str(REPO_ROOT),
        "annonces_root": str(Path(annonces_root) if annonces_root is not None else default_annonces_root()),
        "output_root": str(Path(output_root) if output_root is not None else default_output_root()),
    }
