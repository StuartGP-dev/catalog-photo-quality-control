from pathlib import Path

from PIL import Image, ImageCms

from common.catalog_photo_control.apply_metadata import apply_standard_metadata, main
from common.catalog_photo_control.metadata_diagnostic import inspect_image_metadata


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
    Image.new("RGB", (20, 20), "blue").save(reference, icc_profile=profile)
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
    assert "Make" not in metadata["exif"]
    assert "Model" not in metadata["exif"]
    assert "GPSInfo" not in metadata["exif"]


def test_apply_metadata_cli_refuses_in_place_output(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    Image.new("RGB", (10, 10), "white").save(source)
    try:
        main(["--input", str(source), "--reference", str(source), "--output", str(source)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("in-place output must be rejected")
