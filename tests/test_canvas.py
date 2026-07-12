from __future__ import annotations

from PIL import Image, ImageDraw

from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.image_pipeline import apply_recipe, detect_background_color


def _render(mode: str, **values):
    image = Image.new("RGB", (100, 80), (245, 245, 245))
    ImageDraw.Draw(image).rectangle((25, 15, 75, 65), fill=(80, 60, 140))
    recipe = load_filter_space().schema.canonicalize({"canvas_mode": mode, **values})
    return image, recipe, apply_recipe(image, recipe)


def test_white_and_light_gray_canvas_are_subtle_and_preserve_content() -> None:
    for mode in ("white", "light_gray"):
        source, recipe, output = _render(mode, canvas_padding_x=0.01, canvas_padding_y=0.006)
        metadata = output.info["canvas_metadata"]
        assert output.width != source.width and output.height != source.height
        assert metadata["foreground_scale_ratio"] == 1.0
        assert metadata["canvas_fraction"] < 0.25
        box = metadata["content_box"]
        assert output.crop(box).size == source.size


def test_sampled_white_and_gray_background_are_deterministic() -> None:
    for gray in (245, 235):
        image = Image.new("RGB", (80, 60), (gray, gray, gray))
        first = detect_background_color(image); second = detect_background_color(image)
        assert first == second and not first[2]
        assert max(abs(channel - gray) for channel in first[0]) <= 1


def test_colored_edges_use_light_fallback() -> None:
    image = Image.new("RGB", (80, 60), (240, 20, 20))
    color, confidence, fallback = detect_background_color(image)
    assert fallback and color == (246, 246, 246) and confidence < 0.25


def test_side_bands_and_uniform_frame_geometry() -> None:
    source, _, bands = _render("side_bands", side_band_width=0.012)
    source2, _, frame = _render("uniform_frame", uniform_frame_width=0.008)
    band_meta = bands.info["canvas_metadata"]; frame_meta = frame.info["canvas_metadata"]
    assert band_meta["padding_x"] > 0 and band_meta["padding_y"] == 0
    assert frame_meta["padding_x"] > 0 and frame_meta["padding_y"] > 0
    assert bands.crop(band_meta["content_box"]).size == source.size
    assert frame.crop(frame_meta["content_box"]).size == source2.size


def test_canvas_changes_recipe_hash_and_is_bounded() -> None:
    schema = load_filter_space().schema
    none = schema.canonicalize({})
    white = schema.canonicalize({"canvas_mode": "white", "canvas_padding_x": 0.01, "canvas_padding_y": 0.005})
    assert none.recipe_hash != white.recipe_hash
    try:
        schema.canonicalize({"canvas_mode": "white", "canvas_padding_x": 0.03, "canvas_padding_y": 0.018, "crop_fraction": 0.011})
    except ValueError:
        pass
    else:
        raise AssertionError("large canvas/crop combination accepted")
