from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, TiffImagePlugin

from .metadata_restore import restore_technical_metadata


ISO_VALUES = (50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800)
EXPOSURE_DENOMINATOR_RANGE = (30, 500)
BRIGHTNESS_VALUE_RANGE = (-2.0, 9.0)
BRIGHTNESS_CALIBRATION_RANGE = (-0.5, 0.5)
FLASH_VALUES = (16, 24)
METERING_MODE_VALUES = (3, 5)
EXPOSURE_MODE_VALUES = (0, 1)
EXPOSURE_PROGRAM_VALUES = (1, 2, 3)
WHITE_BALANCE_VALUES = (0, 1)
ULTRAWIDE_FOCAL_RANGE = (1.50, 1.58)
WIDE_FOCAL_RANGE = (5.90, 6.02)
ULTRAWIDE_APERTURE_RANGE = (2.35, 2.45)
WIDE_APERTURE_RANGE = (1.55, 1.65)
ULTRAWIDE_35MM_RANGE = (12, 14)
WIDE_35MM_RANGE = (25, 27)
SUBJECT_DISTANCE_PERCENTILE = 75.0
SUBJECT_MIN_DISTANCE = 12.0
SUBJECT_MIN_FRACTION = 0.01


def _rational(value: float, denominator: int = 1_000_000) -> TiffImagePlugin.IFDRational:
    return TiffImagePlugin.IFDRational(round(value * denominator), denominator)


def _detect_subject_area(path: Path) -> tuple[int, int, int, int]:
    """Estimate a foreground subject rectangle from its contrast with image borders."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    thumbnail = image.copy()
    thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
    pixels = np.asarray(thumbnail, dtype=np.float32)
    border = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]))
    background = np.median(border, axis=0)
    distance = np.linalg.norm(pixels - background, axis=2)
    threshold = max(float(np.percentile(distance, SUBJECT_DISTANCE_PERCENTILE)), SUBJECT_MIN_DISTANCE)
    mask = distance > threshold
    if float(mask.mean()) < SUBJECT_MIN_FRACTION:
        return width // 2, height // 2, max(1, width // 2), max(1, height // 2)
    ys, xs = np.nonzero(mask)
    scale_x = width / thumbnail.width
    scale_y = height / thumbnail.height
    left = max(0, round(xs.min() * scale_x))
    right = min(width, round((xs.max() + 1) * scale_x))
    top = max(0, round(ys.min() * scale_y))
    bottom = min(height, round((ys.max() + 1) * scale_y))
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    return left + box_width // 2, top + box_height // 2, box_width, box_height


def _random_capture_overrides(
    input_path: Path,
    rng: random.Random,
) -> dict[int, object]:
    iso = rng.choice(ISO_VALUES)
    denominator = rng.randint(*EXPOSURE_DENOMINATOR_RANGE)
    exposure_time = 1.0 / denominator
    use_wide = bool(rng.getrandbits(1))
    if use_wide:
        focal_length = rng.uniform(*WIDE_FOCAL_RANGE)
        f_number = rng.uniform(*WIDE_APERTURE_RANGE)
        focal_35mm = rng.randint(*WIDE_35MM_RANGE)
    else:
        focal_length = rng.uniform(*ULTRAWIDE_FOCAL_RANGE)
        f_number = rng.uniform(*ULTRAWIDE_APERTURE_RANGE)
        focal_35mm = rng.randint(*ULTRAWIDE_35MM_RANGE)
    shutter_speed = math.log2(denominator)
    aperture_value = math.log2(f_number * f_number)
    sensitivity_value = math.log2(iso / 3.125)
    brightness = aperture_value + shutter_speed - sensitivity_value
    brightness += rng.uniform(*BRIGHTNESS_CALIBRATION_RANGE)
    brightness = min(max(brightness, BRIGHTNESS_VALUE_RANGE[0]), BRIGHTNESS_VALUE_RANGE[1])
    min_focal = rng.uniform(*ULTRAWIDE_FOCAL_RANGE)
    max_focal = rng.uniform(*WIDE_FOCAL_RANGE)
    wide_aperture = rng.uniform(*WIDE_APERTURE_RANGE)
    ultrawide_aperture = rng.uniform(*ULTRAWIDE_APERTURE_RANGE)
    if use_wide:
        max_focal = max(max_focal, focal_length)
        wide_aperture = min(wide_aperture, f_number)
    else:
        min_focal = min(min_focal, focal_length)
        ultrawide_aperture = min(ultrawide_aperture, f_number)
    exposure_mode = rng.choice(EXPOSURE_MODE_VALUES)
    exposure_program = 1 if exposure_mode == 1 else rng.choice(EXPOSURE_PROGRAM_VALUES[1:])
    lens_model = f"iPhone 15 back dual wide camera {focal_length:.2f}mm f/{f_number:.1f}"
    return {
        33434: _rational(exposure_time),
        33437: _rational(f_number),
        34850: exposure_program,
        34855: iso,
        37377: _rational(shutter_speed),
        37379: _rational(brightness),
        37383: rng.choice(METERING_MODE_VALUES),
        37385: rng.choice(FLASH_VALUES),
        37386: _rational(focal_length),
        37396: _detect_subject_area(input_path),
        41986: exposure_mode,
        41987: rng.choice(WHITE_BALANCE_VALUES),
        41989: focal_35mm,
        42034: (
            _rational(min_focal),
            _rational(max_focal),
            _rational(wide_aperture),
            _rational(ultrawide_aperture),
        ),
        42036: lens_model,
        42080: 1,
    }


def apply_standard_metadata(
    input_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    rng: random.Random | None = None,
) -> Path:
    """Apply the reference ICC profile and compatible capture metadata to a new copy."""
    input_resolved = Path(input_path).resolve()
    generator = rng if rng is not None else random.SystemRandom()
    return restore_technical_metadata(
        input_resolved,
        reference_path,
        output_path,
        capture_metadata_path=reference_path,
        software_tag="17.6.1",
        capture_overrides=_random_capture_overrides(input_resolved, generator),
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
    print("metadata=single-frame JPEG, detected subject area, randomized coherent capture settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
