from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, TiffImagePlugin

from .metadata_restore import restore_technical_metadata


ISO_VALUES = (50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800)
EXPOSURE_DENOMINATOR_RANGE = (30, 2000)
BRIGHTNESS_VALUE_RANGE = (-2.0, 9.0)
BRIGHTNESS_EXPOSURE_JITTER_RANGE = (-0.25, 0.25)
BRIGHTNESS_MIDDLE_GRAY = 0.18
BRIGHTNESS_MIDDLE_GRAY_VALUE = 5.0
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
CAPTURE_AGE_DAYS_RANGE = (0.0, 7.0)


def _rational(value: float, denominator: int = 1_000_000) -> TiffImagePlugin.IFDRational:
    return TiffImagePlugin.IFDRational(round(value * denominator), denominator)


def _estimate_brightness_value(path: Path) -> float:
    """Estimate scene brightness from mean linear-light luminance."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    thumbnail = image.copy()
    thumbnail.thumbnail((256, 256), Image.Resampling.LANCZOS)
    pixels = np.asarray(thumbnail, dtype=np.float64) / 255.0
    linear = np.where(
        pixels <= 0.04045,
        pixels / 12.92,
        ((pixels + 0.055) / 1.055) ** 2.4,
    )
    luminance = 0.2126 * linear[:, :, 0] + 0.7152 * linear[:, :, 1] + 0.0722 * linear[:, :, 2]
    mean_luminance = max(float(luminance.mean()), 1e-6)
    brightness = math.log2(mean_luminance / BRIGHTNESS_MIDDLE_GRAY)
    brightness += BRIGHTNESS_MIDDLE_GRAY_VALUE
    return min(max(brightness, BRIGHTNESS_VALUE_RANGE[0]), BRIGHTNESS_VALUE_RANGE[1])


def _random_capture_overrides(
    input_path: Path,
    rng: random.Random,
) -> tuple[dict[int, object], dict[int, object]]:
    use_wide = bool(rng.getrandbits(1))
    if use_wide:
        focal_length = rng.uniform(*WIDE_FOCAL_RANGE)
        f_number = rng.uniform(*WIDE_APERTURE_RANGE)
        focal_35mm = rng.randint(*WIDE_35MM_RANGE)
    else:
        focal_length = rng.uniform(*ULTRAWIDE_FOCAL_RANGE)
        f_number = rng.uniform(*ULTRAWIDE_APERTURE_RANGE)
        focal_35mm = rng.randint(*ULTRAWIDE_35MM_RANGE)
    aperture_value = math.log2(f_number * f_number)
    target_brightness = _estimate_brightness_value(input_path)
    target_brightness += rng.uniform(*BRIGHTNESS_EXPOSURE_JITTER_RANGE)
    exposure_candidates = []
    for candidate_iso in ISO_VALUES:
        sensitivity = math.log2(candidate_iso / 3.125)
        candidate_denominator = round(2 ** (target_brightness + sensitivity - aperture_value))
        if EXPOSURE_DENOMINATOR_RANGE[0] <= candidate_denominator <= EXPOSURE_DENOMINATOR_RANGE[1]:
            exposure_candidates.append((candidate_iso, candidate_denominator))
    if exposure_candidates:
        iso, denominator = rng.choice(exposure_candidates)
    else:
        iso = ISO_VALUES[0]
        sensitivity = math.log2(iso / 3.125)
        denominator = round(2 ** (target_brightness + sensitivity - aperture_value))
        denominator = min(max(denominator, EXPOSURE_DENOMINATOR_RANGE[0]), EXPOSURE_DENOMINATOR_RANGE[1])
    exposure_time = 1.0 / denominator
    shutter_speed = math.log2(denominator)
    sensitivity_value = math.log2(iso / 3.125)
    brightness = aperture_value + shutter_speed - sensitivity_value
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
    now = datetime.now().astimezone()
    capture_datetime = now - timedelta(days=rng.uniform(*CAPTURE_AGE_DAYS_RANGE))
    capture_date = capture_datetime.strftime("%Y:%m:%d %H:%M:%S")
    subsecond = capture_datetime.strftime("%f")[:3]
    utc_offset = capture_datetime.strftime("%z")
    utc_offset = f"{utc_offset[:3]}:{utc_offset[3:]}"
    lens_model = f"iPhone 15 back dual wide camera {focal_length:.2f}mm f/{f_number:.1f}"
    capture_overrides = {
        33434: _rational(exposure_time),
        33437: _rational(f_number),
        34850: exposure_program,
        34855: iso,
        36867: capture_date,
        36868: capture_date,
        36880: utc_offset,
        36881: utc_offset,
        36882: utc_offset,
        37377: _rational(shutter_speed),
        37379: _rational(brightness),
        37383: rng.choice(METERING_MODE_VALUES),
        37385: rng.choice(FLASH_VALUES),
        37386: _rational(focal_length),
        37521: subsecond,
        37522: subsecond,
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
    return capture_overrides, {306: capture_date}


def apply_standard_metadata(
    input_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    rng: random.Random | None = None,
) -> Path:
    """Apply the reference ICC profile and compatible capture metadata to a new copy."""
    input_resolved = Path(input_path).resolve()
    generator = rng if rng is not None else random.SystemRandom()
    capture_overrides, image_overrides = _random_capture_overrides(input_resolved, generator)
    return restore_technical_metadata(
        input_resolved,
        reference_path,
        output_path,
        capture_metadata_path=reference_path,
        software_tag="17.6.1",
        capture_overrides=capture_overrides,
        image_overrides=image_overrides,
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
    print("metadata=single-frame JPEG, measured brightness, randomized coherent capture settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
