from __future__ import annotations

import json
from pathlib import Path

from common.catalog_photo_control.listing_content import (
    load_listing_content,
    write_variant_content,
)


def test_loads_listing_content_and_writes_variant_manifest(tmp_path: Path) -> None:
    listing = tmp_path / "O18"
    listing.mkdir()
    (listing / "config.json").write_text(json.dumps({
        "vinted": {"titre": "Boucles", "desc": "Description", "prix": 9.95}
    }), encoding="utf-8")
    content = load_listing_content(listing)
    assert (content.title, content.description, content.price_cents, content.currency) == (
        "Boucles", "Description", 995, "EUR"
    )
    manifest = write_variant_content(tmp_path / "variant", content, ("image_0000.jpg", "image_0001.jpg"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["images"] == ["image_0000.jpg", "image_0001.jpg"]
    assert payload["price_cents"] == 995


def test_missing_config_keeps_reserved_content_empty(tmp_path: Path) -> None:
    content = load_listing_content(tmp_path)
    assert content.title is None and content.price_cents is None
