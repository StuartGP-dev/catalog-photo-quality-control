from pathlib import Path

from PIL import Image, ImageCms

from common.catalog_photo_control.metadata_diagnostic import inspect_image_metadata
from common.catalog_photo_control.metadata_restore import generate_restoration_report, restore_technical_metadata


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


def test_restore_uses_capture_source_and_removes_gps(tmp_path: Path) -> None:
    filtered = tmp_path / "filtered.jpg"
    reference = tmp_path / "reference.jpg"
    capture = tmp_path / "capture.jpg"
    output = tmp_path / "output.jpg"
    Image.new("RGB", (40, 30), "white").save(filtered)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (40, 30), "white").save(reference, icc_profile=profile)
    exif = Image.Exif()
    exif[271] = "Apple"
    exif[272] = "iPhone 13"
    exif[36867] = "2024:08:23 14:25:23"
    exif[34853] = {1: "N", 2: (47.0, 0.0, 0.0)}
    Image.new("RGB", (40, 30), "white").save(capture, exif=exif)

    restore_technical_metadata(filtered, reference, output, capture)
    metadata = inspect_image_metadata(output)

    assert metadata["exif"]["Make"] == "Apple"
    assert metadata["exif"]["Model"] == "iPhone 13"
    assert metadata["exif"]["DateTimeOriginal"] == "2024:08:23 14:25:23"
    assert "GPSInfo" not in metadata["exif"]
    assert metadata["exif"]["DateTime"].startswith("20")


def test_restoration_report_has_original_before_after_and_reference_columns(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("original.jpg", "before.jpg", "after.jpg", "reference.jpg")]
    for index, path in enumerate(paths):
        Image.new("RGB", (30 + index, 20), "white").save(path)

    report = generate_restoration_report(paths[1], paths[2], paths[3], tmp_path / "report", paths[0])
    content = report.read_text(encoding="utf-8")

    assert "Originale O18" in content
    assert "Variante filtrée — avant" in content
    assert "Variante filtrée — après" in content
    assert "IMG_3206.jpg — référence iPhone 15" in content


def test_two_image_report_contains_only_filtered_and_reference_columns(tmp_path: Path) -> None:
    before, after, reference = [tmp_path / name for name in ("before.jpg", "after.jpg", "reference.jpg")]
    for path in (before, after, reference):
        Image.new("RGB", (30, 20), "white").save(path)

    report = generate_restoration_report(
        before, after, reference, tmp_path / "report", two_image_comparison=True
    )
    content = report.read_text(encoding="utf-8")

    assert "Photo filtrée O18" in content
    assert "IMG_3206.jpg" in content
    assert "Originale O18" not in content
    assert "Variante filtrée — avant" not in content
    assert 'src="assets/after.jpg"' in content
    assert 'src="assets/reference.jpg"' in content
