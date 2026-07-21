"""Create metadata-free copies of exactly two images.

Source files are opened read-only. Outputs are written to a distinct directory
and replace neither input, preserving the repository's source-data boundary.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps


def _metadata_free_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix or ".jpg"
    with Image.open(source) as opened:
        rendered = ImageOps.exif_transpose(opened).convert("RGB")
        with NamedTemporaryFile(
            dir=destination.parent, suffix=suffix, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            rendered.save(temporary_path, quality=95)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)


def export_two_metadata_free_images(
    first: str | Path, second: str | Path, output_dir: str | Path
) -> tuple[Path, Path]:
    sources = (Path(first).resolve(), Path(second).resolve())
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(source)

    destination_root = Path(output_dir).resolve()
    if any(destination_root == source.parent for source in sources):
        raise ValueError("output directory must differ from both source directories")

    destinations = tuple(
        destination_root / f"image_{index:02d}{source.suffix.lower() or '.jpg'}"
        for index, source in enumerate(sources, start=1)
    )
    for source, destination in zip(sources, destinations, strict=True):
        _metadata_free_copy(source, destination)
    return destinations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create metadata-free copies of exactly two source images."
    )
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    for path in export_two_metadata_free_images(
        args.first, args.second, args.output_dir
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
