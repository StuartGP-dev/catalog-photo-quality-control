from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def _segments(data: bytes) -> tuple[list[bytes], bytes]:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("only JPEG inputs are supported")
    segments: list[bytes] = []
    offset = 2
    while offset + 1 < len(data):
        if data[offset] != 0xFF:
            raise ValueError("invalid JPEG marker stream")
        marker = data[offset + 1]
        if marker == 0xDA:  # Start of scan: the remainder is entropy-coded data.
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            end = offset + 2 + length
            return segments, data[offset:end] + data[end:]
        if marker == 0xD9:
            return segments, data[offset:]
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        end = offset + 2 + length
        if length < 2 or end > len(data):
            raise ValueError("invalid JPEG segment length")
        segments.append(data[offset:end])
        offset = end
    raise ValueError("JPEG has no image scan")


def _is_icc(segment: bytes) -> bool:
    return segment.startswith(b"\xff\xe2") and segment[4:16] == b"ICC_PROFILE\x00"


def _reference_identity_exif(reference: Path) -> bytes | None:
    """Build a minimal EXIF block from four explicitly allowed identity tags."""
    allowed_tags = (271, 272, 305, 316)  # Make, Model, Software, HostComputer
    with Image.open(reference) as image:
        source_exif = image.getexif()
        copied = Image.Exif()
        for tag in allowed_tags:
            value = source_exif.get(tag)
            if isinstance(value, str) and value:
                copied[tag] = value
    if not copied:
        return None
    payload = copied.tobytes()
    if len(payload) + 2 > 65535:
        raise ValueError("reference identity EXIF block is too large")
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def apply_standard_metadata(
    input_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Write a new JPEG with technical metadata and four reference identity tags.

    ICC, resolution, Make, Model, Software and HostComputer are copied. Lens,
    date, GPS, maker-note and capture settings are deliberately omitted.
    """
    source = Path(input_path).resolve()
    reference = Path(reference_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise ValueError("output must differ from input")
    if not source.is_file() or not reference.is_file():
        raise FileNotFoundError("input and reference images must exist")

    source_segments, source_scan = _segments(source.read_bytes())
    reference_segments, _ = _segments(reference.read_bytes())
    reference_icc = [segment for segment in reference_segments if _is_icc(segment)]
    reference_jfif = next(
        (segment for segment in reference_segments if segment.startswith(b"\xff\xe0") and segment[4:9] == b"JFIF\x00"),
        None,
    )
    identity_exif = _reference_identity_exif(reference)
    kept = [
        segment for segment in source_segments
        if not segment.startswith(b"\xff\xe1")  # EXIF/XMP capture provenance
        and not _is_icc(segment)
        and not (reference_jfif is not None and segment.startswith(b"\xff\xe0") and segment[4:9] == b"JFIF\x00")
    ]
    technical = (
        ([reference_jfif] if reference_jfif else [])
        + ([identity_exif] if identity_exif else [])
        + reference_icc
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\xff\xd8" + b"".join(technical + kept) + source_scan)
    with Image.open(output) as verified:
        verified.verify()
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply reference ICC/resolution and four explicit device identity tags."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    if input_path == output_path:
        parser.error("--output must differ from --input")
    result = apply_standard_metadata(input_path, args.reference, output_path)
    print(f"image={result}")
    print("metadata=icc_resolution_and_reference_identity; capture_settings=omitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
