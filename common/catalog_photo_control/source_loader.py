from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from PIL import Image

from .models import SourceImage, SourceListing, ordered_source_set_hash, stable_hash


SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"})


def resolve_listing_reference(value: str, source_root: str | None = None) -> tuple[Path, str]:
    direct = Path(value)
    if direct.is_dir():
        return direct.resolve(), direct.name
    if source_root is None:
        raise FileNotFoundError(f"listing path does not exist and --source-root was not supplied: {value}")
    return (Path(source_root) / Path(value)).resolve(), value.replace("\\", "/")


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_source_images(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def load_source_listing(
    directory: str | Path,
    *,
    listing_code: str | None = None,
    image_paths: Iterable[Path] | None = None,
) -> SourceListing:
    source_dir = Path(directory).resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    paths = list(image_paths) if image_paths is not None else discover_source_images(source_dir)
    if not paths:
        raise ValueError(f"listing contains no supported images: {source_dir}")

    images: list[SourceImage] = []
    for index, path in enumerate(paths):
        resolved = Path(path).resolve()
        if resolved.parent != source_dir:
            raise ValueError(f"source image is outside listing directory: {resolved}")
        with Image.open(resolved) as opened:
            width, height = opened.size
        images.append(
            SourceImage(index, resolved, hash_file(resolved), width, height)
        )

    code = (listing_code or source_dir.name).replace("\\", "/").strip("/")
    if not code:
        raise ValueError("listing code cannot be empty")
    return SourceListing(
        listing_id=stable_hash({"listing_code": code}),
        listing_code=code,
        directory=source_dir,
        images=tuple(images),
        source_set_hash=ordered_source_set_hash(images),
    )
