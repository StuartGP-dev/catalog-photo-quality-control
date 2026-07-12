from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Any, Mapping

import numpy as np

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

from .models import Recipe, SourceListing


@dataclass(frozen=True, slots=True)
class RenderedImage:
    image_index: int
    source_hash: str
    output_path: Path
    output_hash: str
    width: int
    height: int
    canvas_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RenderedVariant:
    listing_id: str
    source_set_hash: str
    recipe_hash: str
    directory: Path
    images: tuple[RenderedImage, ...]


def _blend(base: Image.Image, transformed: Image.Image, amount: float) -> Image.Image:
    return base if amount <= 0 else Image.blend(base, transformed, amount)


def _sample_background(image: Image.Image) -> tuple[int, int, int]:
    width, height = image.size
    band = max(1, min(width, height) // 20)
    edges = Image.new("RGB", (width * 2 + height * 2, band))
    offset = 0
    for crop in (
        image.crop((0, 0, width, band)),
        image.crop((0, height - band, width, height)),
        image.crop((0, 0, band, height)).resize((height, band)),
        image.crop((width - band, 0, width, height)).resize((height, band)),
    ):
        edges.paste(crop.resize((crop.width, band)), (offset, 0))
        offset += crop.width
    return tuple(round(value) for value in ImageStat.Stat(edges).mean[:3])


def detect_background_color(image: Image.Image, edge_fraction: float = 0.08, saturation_limit: float = 0.18, lightness_minimum: float = 0.7) -> tuple[tuple[int, int, int], float, bool]:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    h, w = rgb.shape[:2]; band = max(1, round(min(h, w) * edge_fraction))
    pixels = np.concatenate((rgb[:band].reshape(-1, 3), rgb[-band:].reshape(-1, 3), rgb[:, :band].reshape(-1, 3), rgb[:, -band:].reshape(-1, 3)))
    saturation = pixels.max(axis=1) - pixels.min(axis=1); lightness = pixels.mean(axis=1)
    valid = pixels[(saturation <= saturation_limit) & (lightness >= lightness_minimum)]
    confidence = float(len(valid) / max(1, len(pixels)))
    if len(valid) < max(16, len(pixels) * 0.25):
        return (246, 246, 246), confidence, True
    color = tuple(int(round(v * 255)) for v in np.median(valid, axis=0))
    if max(color) - min(color) > round(saturation_limit * 255) or sum(color) / 3 < lightness_minimum * 255:
        return (246, 246, 246), confidence, True
    return color, confidence, False


def _geometry(image: Image.Image, parameters: dict[str, object]) -> Image.Image:
    width, height = image.size
    crop_fraction = float(parameters["crop_fraction"])
    if crop_fraction > 0:
        dx, dy = round(width * crop_fraction / 2), round(height * crop_fraction / 2)
        image = image.crop((dx, dy, width - dx, height - dy)).resize(
            (width, height), Image.Resampling.LANCZOS
        )
    zoom = float(parameters["zoom"])
    offset_x = float(parameters["offset_x"])
    offset_y = float(parameters["offset_y"])
    if zoom != 1 or offset_x or offset_y:
        scaled = image.resize(
            (max(1, round(width * zoom)), max(1, round(height * zoom))),
            Image.Resampling.LANCZOS,
        )
        left = round((scaled.width - width) / 2 - offset_x * width)
        top = round((scaled.height - height) / 2 - offset_y * height)
        if scaled.width >= width and scaled.height >= height:
            image = scaled.crop((left, top, left + width, top + height))
        else:
            canvas = Image.new("RGB", (width, height), _sample_background(image))
            canvas.paste(scaled, ((width - scaled.width) // 2, (height - scaled.height) // 2))
            image = canvas
    angle = float(parameters["rotation_degrees"])
    if angle:
        image = image.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=False,
            fillcolor=_sample_background(image),
        )
    scale = float(parameters["resize_scale"])
    if scale != 1:
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return image


def apply_recipe(image: Image.Image, recipe: Recipe, *, dimension_salt: int = 0) -> Image.Image:
    """Apply the documented deterministic order to one image.

    Order: orientation/mode, photometric, color, geometry, detail, style,
    canvas, then encoding (performed by :func:`render_listing`).
    """
    p = dict(recipe.parameters)
    result = ImageOps.exif_transpose(image).convert("RGB")

    result = ImageEnhance.Brightness(result).enhance(float(p["brightness"]))
    result = ImageEnhance.Contrast(result).enhance(float(p["contrast"]))
    result = ImageEnhance.Color(result).enhance(float(p["saturation"]))
    result = ImageEnhance.Sharpness(result).enhance(float(p["sharpness"]))
    gamma = float(p["gamma"])
    if gamma != 1:
        inverse = 1.0 / gamma
        lookup = [round(255 * ((value / 255) ** inverse)) for value in range(256)]
        result = result.point(lookup * 3)
    warmth, tint = float(p["warmth"]), float(p["tint"])
    if warmth or tint:
        red, green, blue = result.split()
        red = red.point(lambda value: max(0, min(255, round(value * (1 + warmth)))))
        green = green.point(lambda value: max(0, min(255, round(value * (1 + tint)))))
        blue = blue.point(lambda value: max(0, min(255, round(value * (1 - warmth)))))
        result = Image.merge("RGB", (red, green, blue))
    result = _blend(result, ImageOps.autocontrast(result), float(p["autocontrast_blend"]))
    result = _blend(result, ImageOps.equalize(result), float(p["equalize_blend"]))

    gains = (float(p["red_gain"]), float(p["green_gain"]), float(p["blue_gain"]))
    if gains != (1.0, 1.0, 1.0):
        result = Image.merge(
            "RGB",
            tuple(
                channel.point(lambda value, gain=gain: min(255, round(value * gain)))
                for channel, gain in zip(result.split(), gains, strict=True)
            ),
        )
    result = _geometry(result, p)

    blur = float(p["gaussian_blur_radius"])
    if blur:
        result = result.filter(ImageFilter.GaussianBlur(blur))
    median = int(p["median_size"])
    if median > 1:
        if median % 2 == 0:
            median += 1
        result = result.filter(ImageFilter.MedianFilter(median))
    unsharp = float(p["unsharp_radius"])
    if unsharp:
        result = result.filter(
            ImageFilter.UnsharpMask(unsharp, int(p["unsharp_percent"]), threshold=3)
        )

    grayscale = ImageOps.grayscale(result).convert("RGB")
    result = _blend(result, grayscale, float(p["grayscale_blend"]))
    sepia_gray = ImageOps.grayscale(result)
    sepia = ImageOps.colorize(sepia_gray, "#2b1b0e", "#f4d8a8")
    result = _blend(result, sepia, float(p["sepia_blend"]))

    mode = str(p["canvas_mode"])
    border = int(p["border_width"])
    detected, confidence, fallback = detect_background_color(result, float(p["sampled_edge_fraction"]), float(p["sampled_saturation_limit"]), float(p["sampled_lightness_minimum"]))
    gray = int(p["fixed_background_gray"])
    background = (255, 255, 255) if mode == "white" else (gray, gray, gray)
    origin = "fixed"
    if mode in {"sampled_background", "sampled_edge"}:
        strength = float(p["sampled_color_strength"]); background = tuple(round(v * strength + 255 * (1 - strength)) for v in detected); origin = "fallback" if fallback else mode
    pad_x = pad_y = 0
    if mode == "side_bands": pad_x = max(1, round(result.width * float(p["side_band_width"]))); background = detected if not fallback else (246, 246, 246); origin = "sampled_edge" if not fallback else "fallback"
    elif mode == "uniform_frame": pad_x = max(1, round(result.width * float(p["uniform_frame_width"]))); pad_y = max(1, round(result.height * float(p["uniform_frame_width"]))); background = detected if not fallback else (246, 246, 246); origin = "sampled_edge" if not fallback else "fallback"
    elif mode != "none": pad_x = max(1, round(result.width * float(p["canvas_padding_x"]))); pad_y = max(1, round(result.height * float(p["canvas_padding_y"])))
    # A deterministic few-pixel signature guarantees dimensions differ from the source.
    extra_x = 1 + (int(recipe.recipe_hash[:4], 16) + dimension_salt * 7) % 17; extra_y = 1 + (int(recipe.recipe_hash[4:8], 16) + dimension_salt * 5) % 13
    left = pad_x + extra_x // 2; top = pad_y + extra_y // 2
    canvas = Image.new("RGB", (result.width + 2 * pad_x + extra_x, result.height + 2 * pad_y + extra_y), background)
    canvas.paste(result, (left, top)); content_box = (left, top, left + result.width, top + result.height); result = canvas
    result.info["canvas_metadata"] = {"canvas_mode": mode, "background_rgb": background, "background_origin": origin if mode != "none" else "dimension_signature", "sampled_background_rgb": detected, "sampled_background_confidence": confidence, "sampled_background_fallback_used": fallback, "content_box": content_box, "canvas_fraction": 1.0 - (content_box[2]-content_box[0])*(content_box[3]-content_box[1])/(result.width*result.height), "foreground_scale_ratio": 1.0, "padding_x": pad_x, "padding_y": pad_y}
    radius = int(p["rounded_radius"])
    if radius:
        mask = Image.new("L", result.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, result.width - 1, result.height - 1), radius, fill=255)
        background = Image.new("RGB", result.size, (245, 245, 245))
        background.paste(result, mask=mask)
        result = background
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_listing(
    listing: SourceListing,
    recipe: Recipe,
    destination: str | Path,
    *,
    before_image: Callable[[int], None] | None = None,
) -> RenderedVariant:
    destination_path = Path(destination).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination_path.name}-", dir=destination_path.parent)
    )
    rendered: list[RenderedImage] = []
    try:
        for source in listing.images:
            if before_image is not None:
                before_image(source.index)
            with Image.open(source.path) as opened:
                output = apply_recipe(opened, recipe, dimension_salt=source.index)
            output_path = temporary / f"image_{source.index:04d}.jpg"
            output.save(
                output_path,
                format="JPEG",
                quality=int(recipe.parameters["jpeg_quality"]),
                optimize=False,
                progressive=False,
                subsampling=0,
                exif=b"",
            )
            rendered.append(
                RenderedImage(
                    source.index,
                    source.source_hash,
                    output_path,
                    _sha256(output_path),
                    output.width,
                    output.height,
                    dict(output.info.get("canvas_metadata", {})),
                )
            )
        if len(rendered) != len(listing.images):
            raise RuntimeError("incomplete listing render")
        os.replace(temporary, destination_path)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finalized = tuple(
        RenderedImage(
            item.image_index,
            item.source_hash,
            destination_path / item.output_path.name,
            item.output_hash,
            item.width,
            item.height,
            item.canvas_metadata,
        )
        for item in rendered
    )
    return RenderedVariant(
        listing.listing_id,
        listing.source_set_hash,
        recipe.recipe_hash,
        destination_path,
        finalized,
    )
