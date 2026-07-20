from __future__ import annotations

import argparse
from pathlib import Path

from .metadata_restore import restore_technical_metadata


def apply_standard_metadata(
    input_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Apply the validated technical metadata profile to a new image copy."""
    return restore_technical_metadata(
        input_path,
        reference_path,
        output_path,
        strip_capture_metadata=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the validated Display P3/JFIF/EXIF metadata profile to an image copy."
    )
    parser.add_argument("--input", required=True, help="Image to process; it is never overwritten.")
    parser.add_argument("--reference", required=True, help="Image providing the target ICC profile.")
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
