from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageCms

from common.catalog_photo_control.apply_metadata import apply_standard_metadata, main
from common.catalog_photo_control.image_metadata import read_image_metadata


def _pixel_digest(path: Path) -> str:
    with Image.open(path) as image:
        return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def test_applies_only_technical_metadata_without_changing_pixels(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    reference = tmp_path / "reference.jpg"
    output = tmp_path / "output.jpg"
    source_exif = Image.Exif()
    source_exif[271] = "Source camera"
    source_exif[36867] = "2020:01:02 03:04:05"
    Image.new("RGB", (64, 48), "white").save(source, exif=source_exif)
    reference_exif = Image.Exif()
    reference_exif[271] = "Reference camera"
    reference_exif[34853] = {1: "N"}
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (20, 20), "blue").save(
        reference, exif=reference_exif, icc_profile=profile, dpi=(240, 240)
    )
    original = source.read_bytes()

    apply_standard_metadata(source, reference, output)

    assert source.read_bytes() == original
    assert _pixel_digest(output) == _pixel_digest(source)
    with Image.open(output) as image:
        assert image.info.get("icc_profile") == profile
        assert not image.getexif()
    stored = read_image_metadata(output)
    assert stored["icc_profile_present"] is True
    assert stored["width"] == 64 and stored["height"] == 48
    assert all(not fields for fields in stored["exif"].values())


def test_cli_refuses_in_place_output(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    Image.new("RGB", (10, 10), "white").save(source)
    try:
        main(["--input", str(source), "--reference", str(source), "--output", str(source)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("in-place output must be rejected")
