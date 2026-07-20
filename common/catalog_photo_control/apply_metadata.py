from __future__ import annotations

import argparse
from pathlib import Path

from .metadata_restore import restore_technical_metadata


def apply_standard_metadata(
    input_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Apply the reference ICC profile and compatible capture metadata to a new copy."""
    return restore_technical_metadata(
        input_path,
        reference_path,
        output_path,
        capture_metadata_path=reference_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the reference Display P3/JFIF/EXIF profile and compatible camera, lens, "
            "and capture settings to an image copy."
        )
    )
    parser.add_argument("--input", required=True, help="Image to process; it is never overwritten.")
    parser.add_argument(
        "--reference",
        required=True,
        help="Image providing the target ICC profile and compatible capture metadata.",
    )
    parser.add_argument("--output", required=True, help="New JPEG output path.")
    args = parser.parse_args(argv)
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    if input_path == output_path:
        parser.error("--output must differ from --input")
    result = apply_standard_metadata(input_path, args.reference, output_path)
    print(f"image={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
