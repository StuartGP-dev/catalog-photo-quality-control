from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

from .models import Recipe, SourceListing


@dataclass(frozen=True, slots=True)
class RenderedImage:
    image_index: int
    source_hash: str
    output_path: Path
    output_hash: str
    width: int
    height: int


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


def apply_recipe(image: Image.Image, recipe: Recipe) -> Image.Image:
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

    padding = float(p["canvas_padding"])
    border = int(p["border_width"])
    if padding or border:
        pad_x = round(result.width * padding) + border
        pad_y = round(result.height * padding) + border
        background = (245, 245, 245) if p["background_mode"] == "light" else _sample_background(result)
        canvas = Image.new("RGB", (result.width + 2 * pad_x, result.height + 2 * pad_y), background)
        canvas.paste(result, (pad_x, pad_y))
        result = canvas
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
                output = apply_recipe(opened, recipe)
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
