from pathlib import Path

from PIL import Image, ImageCms

from common.catalog_photo_control.metadata_diagnostic import inspect_image_metadata
from common.catalog_photo_control.metadata_restore import restore_technical_metadata


def test_restore_technical_metadata_is_non_destructive_and_truthful(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    reference = tmp_path / "reference.jpg"
    output = tmp_path / "output.jpg"
    Image.new("RGB", (40, 30), "red").save(source)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    reference_exif = Image.Exif()
    reference_exif[271] = "Example Camera"
    reference_exif[34853] = {1: "N"}
    Image.new("RGB", (30, 40), "blue").save(reference, icc_profile=profile, exif=reference_exif)
    original_bytes = source.read_bytes()

    restore_technical_metadata(source, reference, output)
    metadata = inspect_image_metadata(output)

    assert source.read_bytes() == original_bytes
    assert output.is_file()
    assert metadata["icc_profile"] is not None
    assert metadata["exif"]["Orientation"] == 1
    assert metadata["exif"]["Software"].startswith("Catalog Photo Control")
    assert metadata["exif"]["YCbCrPositioning"] == 1
    assert metadata["embedded_info"]["dpi"] == [300, 300]
    assert "Make" not in metadata["exif"]
    assert "GPSInfo" not in metadata["exif"]
