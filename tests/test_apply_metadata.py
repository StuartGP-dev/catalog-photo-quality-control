import os
from pathlib import Path

from PIL import Image, ImageCms

from common.catalog_photo_control.apply_metadata import apply_standard_metadata, main
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
    Image.new("RGB", (64, 48), "red").save(source, exif=exif)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    reference_exif = Image.Exif()
    reference_exif[271] = "Apple"
    reference_exif[272] = "iPhone 15"
    reference_exif[316] = "iPhone 15"
    reference_exif[34853] = {1: "N", 2: (47.0, 12.0, 36.72)}
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
        37500: b"reference-specific-maker-note",
        37396: (10, 10, 5, 5),
    }
    Image.new("RGB", (20, 20), "blue").save(reference, icc_profile=profile, exif=reference_exif)
    reference_mp = _add_synthetic_mpf(reference)
    zone_payload = b"[ZoneTransfer]\r\nZoneId=3\r\n"
    if os.name == "nt":
        Path(f"{reference}:Zone.Identifier").write_bytes(zone_payload)
    original = source.read_bytes()

    apply_standard_metadata(source, reference, output)
    metadata = inspect_image_metadata(output)

    assert source.read_bytes() == original
    assert metadata["stored_width"] == 64
    assert metadata["stored_height"] == 48
    assert metadata["icc_profile"] is not None
    assert metadata["embedded_info"]["jfif_density"] == [300, 300]
    assert metadata["exif"]["XResolution"] == "72.0"
    assert metadata["exif"]["YCbCrPositioning"] == 1
    assert metadata["exif_ifds"]["IFD1"]["JpegIFByteCount"] > 0
    assert metadata["exif"]["Make"] == "Apple"
    assert metadata["exif"]["Model"] == "iPhone 15"
    assert metadata["exif"]["HostComputer"] == "iPhone 15"
    assert metadata["exif"]["Software"] == "17.6.1"
    assert "GPSInfo" not in metadata["exif"]
    capture = metadata["exif_ifds"]["Exif"]
    assert capture["LensMake"] == "Apple"
    assert capture["LensModel"] == "iPhone 15 back dual wide camera 5.96mm f/1.6"
    assert capture["FocalLength"] == "5.96"
    assert capture["FNumber"] == "1.6"
    assert capture["ISOSpeedRatings"] == 640
    assert capture["ExposureTime"] == str(1 / 39)
    assert capture["DateTimeOriginal"] == "2026:07:18 15:54:34"
    assert capture["ExifImageWidth"] == 64
    assert capture["ExifImageHeight"] == 48
    assert capture["CompositeImage"] == 2
    assert capture["MakerNote"]["byte_length"] == len(b"reference-specific-maker-note")
    assert capture["SubjectLocation"] == [32, 24, 16, 12]
    assert metadata["embedded_info"]["mp"]["byte_length"] == len(reference_mp)
    if os.name == "nt":
        assert Path(f"{output}:Zone.Identifier").read_bytes() == zone_payload


def test_apply_metadata_cli_refuses_in_place_output(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    Image.new("RGB", (10, 10), "white").save(source)
    try:
        main(["--input", str(source), "--reference", str(source), "--output", str(source)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("in-place output must be rejected")
