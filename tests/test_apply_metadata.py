import math
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageCms, ImageDraw

from common.catalog_photo_control.apply_metadata import (
    BRIGHTNESS_VALUE_RANGE,
    EXPOSURE_DENOMINATOR_RANGE,
    EXPOSURE_MODE_VALUES,
    EXPOSURE_PROGRAM_VALUES,
    FLASH_VALUES,
    ISO_VALUES,
    METERING_MODE_VALUES,
    ULTRAWIDE_FOCAL_RANGE,
    WHITE_BALANCE_VALUES,
    WIDE_FOCAL_RANGE,
    _estimate_brightness_value,
    apply_standard_metadata,
    main,
)
from common.catalog_photo_control.metadata_diagnostic import inspect_image_metadata


def _add_synthetic_mpf(path: Path) -> bytes:
    mp = bytes.fromhex(
        "4d4d002a000000080003b00000070000000430313030"
        "b00100040000000100000002b00200070000002000000032"
        "000000000003000000000000000000000000000000000000"
        "00000000000000000000000000000000"
    )
    payload = b"MPF\x00" + mp
    segment = b"\xff\xe2" + (len(payload) + 2).to_bytes(2, "big") + payload
    data = path.read_bytes()
    path.write_bytes(data[:2] + segment + data[2:])
    return mp


def test_apply_standard_metadata_to_arbitrary_image(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    reference = tmp_path / "reference.jpg"
    output = tmp_path / "output.jpg"
    exif = Image.Exif()
    exif[271] = "Old camera"
    exif[272] = "Old model"
    exif[34853] = {1: "N"}
    source_image = Image.new("RGB", (64, 48), "white")
    ImageDraw.Draw(source_image).rectangle((16, 12, 47, 35), fill="black")
    source_image.save(source, exif=exif)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    reference_exif = Image.Exif()
    reference_exif[271] = "Apple"
    reference_exif[272] = "iPhone 15"
    reference_exif[316] = "iPhone 15"
    reference_exif[34853] = {1: "N", 2: (47.0, 12.0, 36.72)}
    reference_exif[700] = b'<x:xmpmeta xmpMM:InstanceID="xmp.iid:forbidden"/>'
    reference_exif[34665] = {
        33434: (1, 39),
        33437: (8, 5),
        34855: 640,
        36867: "2026:07:18 15:54:34",
        37386: (596, 100),
        40962: 5712,
        40963: 4284,
        42035: "Apple",
        42036: "iPhone 15 back dual wide camera 5.96mm f/1.6",
        42080: 2,
        42016: "forbidden-image-unique-id",
        42032: "forbidden-owner",
        42033: "forbidden-body-serial",
        42037: "forbidden-lens-serial",
        37500: b"reference-specific-maker-note",
        37396: (10, 10, 5, 5),
    }
    Image.new("RGB", (20, 20), "blue").save(reference, icc_profile=profile, exif=reference_exif)
    _add_synthetic_mpf(reference)
    zone_payload = b"[ZoneTransfer]\r\nZoneId=3\r\n"
    if os.name == "nt":
        Path(f"{reference}:Zone.Identifier").write_bytes(zone_payload)
        Image.new("RGB", (5, 5), "white").save(output)
        Path(f"{output}:Zone.Identifier").write_bytes(b"stale-zone")
    original = source.read_bytes()

    apply_standard_metadata(source, reference, output, rng=random.Random(7))
    metadata = inspect_image_metadata(output)

    assert source.read_bytes() == original
    assert metadata["stored_width"] == 64
    assert metadata["stored_height"] == 48
    assert metadata["icc_profile"] is not None
    assert metadata["embedded_info"]["jfif_density"] == [300, 300]
    assert metadata["exif"]["XResolution"] == "300.0"
    assert metadata["exif"]["YResolution"] == "300.0"
    assert metadata["exif"]["YCbCrPositioning"] == 1
    assert metadata["exif_ifds"]["IFD1"]["JpegIFByteCount"] > 0
    assert metadata["exif"]["Make"] == "Apple"
    assert metadata["exif"]["Model"] == "iPhone 15"
    assert metadata["exif"]["HostComputer"] == "iPhone 15"
    assert metadata["exif"]["Software"] == "17.6.1"
    assert "GPSInfo" not in metadata["exif"]
    assert "XMLPacket" not in metadata["exif"]
    capture = metadata["exif_ifds"]["Exif"]
    assert capture["LensMake"] == "Apple"
    focal_length = float(capture["FocalLength"])
    f_number = float(capture["FNumber"])
    exposure_time = float(capture["ExposureTime"])
    shutter_speed = float(capture["ShutterSpeedValue"])
    assert (
        ULTRAWIDE_FOCAL_RANGE[0] <= focal_length <= ULTRAWIDE_FOCAL_RANGE[1]
        or WIDE_FOCAL_RANGE[0] <= focal_length <= WIDE_FOCAL_RANGE[1]
    )
    assert f"{focal_length:.2f}mm" in capture["LensModel"]
    assert capture["ISOSpeedRatings"] in ISO_VALUES
    assert EXPOSURE_DENOMINATOR_RANGE[0] <= round(1 / exposure_time) <= EXPOSURE_DENOMINATOR_RANGE[1]
    assert shutter_speed == pytest.approx(math.log2(1 / exposure_time), abs=1e-3)
    measured_brightness = _estimate_brightness_value(source)
    assert BRIGHTNESS_VALUE_RANGE[0] <= float(capture["BrightnessValue"]) <= BRIGHTNESS_VALUE_RANGE[1]
    assert float(capture["BrightnessValue"]) == pytest.approx(measured_brightness, abs=0.3)
    assert capture["Flash"] in FLASH_VALUES
    assert capture["MeteringMode"] in METERING_MODE_VALUES
    assert capture["ExposureMode"] in EXPOSURE_MODE_VALUES
    assert capture["ExposureProgram"] in EXPOSURE_PROGRAM_VALUES
    if capture["ExposureMode"] == 1:
        assert capture["ExposureProgram"] == 1
    else:
        assert capture["ExposureProgram"] in (2, 3)
    assert capture["WhiteBalance"] in WHITE_BALANCE_VALUES
    capture_datetime = datetime.strptime(capture["DateTimeOriginal"], "%Y:%m:%d %H:%M:%S")
    now = datetime.now()
    assert now - timedelta(days=7, seconds=2) <= capture_datetime <= now + timedelta(seconds=2)
    assert capture["DateTimeDigitized"] == capture["DateTimeOriginal"]
    assert metadata["exif"]["DateTime"] == capture["DateTimeOriginal"]
    assert capture["ExifImageWidth"] == 64
    assert capture["ExifImageHeight"] == 48
    assert capture["CompositeImage"] == 1
    assert "MakerNote" not in capture
    for name in ("ImageUniqueID", "CameraOwnerName", "BodySerialNumber", "LensSerialNumber"):
        assert name not in capture
        assert name not in metadata["exif"]
    assert b"xmpmeta" not in output.read_bytes().lower()
    assert "SubjectLocation" not in capture
    assert "mp" not in metadata["embedded_info"]
    assert metadata["format"] == "JPEG"
    assert metadata["frame_count"] == 1
    lens_specification = [float(value) for value in capture["LensSpecification"]]
    assert len(lens_specification) == 4
    assert lens_specification[0] <= focal_length <= lens_specification[1]
    if focal_length < 2:
        assert lens_specification[3] <= f_number
    else:
        assert lens_specification[2] <= f_number
    if os.name == "nt":
        with pytest.raises(OSError):
            Path(f"{output}:Zone.Identifier").read_bytes()


def test_apply_metadata_cli_refuses_in_place_output(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    Image.new("RGB", (10, 10), "white").save(source)
    try:
        main(["--input", str(source), "--reference", str(source), "--output", str(source)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("in-place output must be rejected")
