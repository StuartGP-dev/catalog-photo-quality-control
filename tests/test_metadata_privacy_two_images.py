from __future__ import annotations

from pathlib import Path

from PIL import Image

from metadata_privacy_two_images import export_two_metadata_free_images


def test_exports_two_copies_without_mutating_sources(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    sources = sorted(synthetic_listing.iterdir())
    before = [source.read_bytes() for source in sources]

    outputs = export_two_metadata_free_images(*sources, tmp_path / "output")

    assert [output.name for output in outputs] == ["image_01.png", "image_02.png"]
    assert all(output.is_file() for output in outputs)
    assert [source.read_bytes() for source in sources] == before
    with Image.open(outputs[0]) as output:
        assert not output.getexif()


def test_refuses_to_write_beside_sources(synthetic_listing: Path) -> None:
    sources = sorted(synthetic_listing.iterdir())

    try:
        export_two_metadata_free_images(*sources, synthetic_listing)
    except ValueError as error:
        assert "must differ" in str(error)
    else:
        raise AssertionError("source directory was accepted as output")
