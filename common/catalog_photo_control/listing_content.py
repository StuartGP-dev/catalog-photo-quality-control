from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import stable_hash


@dataclass(frozen=True, slots=True)
class ListingContent:
    title: str | None
    description: str | None
    price_cents: int | None
    currency: str | None
    source_section: str | None


def _repair_text(value: Any) -> Any:
    if isinstance(value, str) and any(marker in value for marker in ("Ã", "â", "Â")):
        try:
            return value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    if isinstance(value, list):
        return [_repair_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_text(item) for key, item in value.items()}
    return value


def load_listing_content(directory: str | Path) -> ListingContent:
    config = Path(directory).resolve() / "config.json"
    if not config.is_file():
        return ListingContent(None, None, None, None, None)
    payload = _repair_text(json.loads(config.read_text(encoding="utf-8")))
    if not isinstance(payload, Mapping):
        raise ValueError("listing config must contain an object")
    section_name = next(
        (name for name in ("vinted", "lbc", "ebay", "etsy") if isinstance(payload.get(name), Mapping)),
        None,
    )
    if section_name is None:
        return ListingContent(None, None, None, None, None)
    section = payload[section_name]
    price = section.get("prix")
    cents = None if price is None else int(
        (Decimal(str(price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return ListingContent(
        str(section["titre"]) if section.get("titre") else None,
        str(section["desc"]) if section.get("desc") else None,
        cents,
        "EUR" if cents is not None else None,
        section_name,
    )


def write_variant_content(
    directory: str | Path,
    content: ListingContent,
    image_paths: Sequence[str | Path],
) -> Path:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "listing.json"
    destination.write_text(
        json.dumps(
            {
                "title": content.title,
                "description": content.description,
                "price_cents": content.price_cents,
                "currency": content.currency,
                "source_section": content.source_section,
                "images": [Path(path).name for path in image_paths],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return destination


def sync_existing_variants(listing_dir: str | Path, database_path: str | Path) -> int:
    directory = Path(listing_dir).resolve()
    listing_code = directory.name
    listing_id = stable_hash({"listing_code": listing_code})
    content = load_listing_content(directory)
    connection = sqlite3.connect(Path(database_path).resolve())
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT variant_id FROM listing_variants
               WHERE listing_id=? AND status='ready'""",
            (listing_id,),
        ).fetchall()
        with connection:
            connection.execute(
                """UPDATE listing_variants SET title_text=?, description_text=?,
                          price_cents=?, currency=?
                   WHERE listing_id=? AND status='ready'""",
                (content.title, content.description, content.price_cents, content.currency, listing_id),
            )
        for row in rows:
            images = connection.execute(
                "SELECT output_path FROM listing_variant_images WHERE variant_id=? ORDER BY image_index",
                (row["variant_id"],),
            ).fetchall()
            if images:
                paths = [Path(image["output_path"]) for image in images]
                write_variant_content(paths[0].parent, content, paths)
        return len(rows)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate reserved final catalog content from a read-only listing config.")
    parser.add_argument("--listing", required=True)
    parser.add_argument("--database", default="local/databases/catalog_variants.sqlite3")
    args = parser.parse_args(argv)
    print(f"updated_variants={sync_existing_variants(args.listing, args.database)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
