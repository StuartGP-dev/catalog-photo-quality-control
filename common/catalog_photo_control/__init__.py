"""Local catalog photo quality-control helpers.

The package init is intentionally lightweight so command modules such as
``python -m common.catalog_photo_control.ingest_annonces`` do not import the
older visual-comparison stack and its optional dependencies at startup.
"""

from __future__ import annotations

__all__ = [
    "compare_photo_metadata",
    "compare_photo_pair",
    "compare_photo_visual_markers",
    "sha256_file",
]


def __getattr__(name: str):
    if name in __all__:
        from . import photo_comparison_rules

        return getattr(photo_comparison_rules, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
