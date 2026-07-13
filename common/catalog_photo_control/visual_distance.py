from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image, ImageOps

from .metrics import structural_similarity


DISTANCE_METRICS_VERSION = "image-distance-v1"

DEFAULT_DISTANCE_WEIGHTS: dict[str, float] = {
    "structural": 0.30,
    "luminance": 0.10,
    "color": 0.12,
    "edge": 0.16,
    "geometry": 0.24,
    "canvas": 0.08,
}


@dataclass(frozen=True, slots=True)
class ImageDistanceResult:
    """A deterministic diversity score; it is not an absolute human-perception metric."""

    total_distance: float
    structural_distance: float
    luminance_distance: float
    color_distance: float
    edge_distance: float
    geometry_distance: float
    canvas_distance: float

    def components(self) -> dict[str, float]:
        values = asdict(self)
        values.pop("total_distance")
        return values


@dataclass(frozen=True, slots=True)
class VisualSignature:
    rgb: np.ndarray
    luminance: np.ndarray
    edges: np.ndarray
    content_box: tuple[float, float, float, float]
    canvas_fraction: float
    border_rgb: tuple[float, float, float]


def _unit(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


def _content_geometry(rgb: np.ndarray) -> tuple[tuple[float, float, float, float], float, tuple[float, float, float]]:
    height, width = rgb.shape[:2]
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    background = np.median(border, axis=0)
    delta = np.max(np.abs(rgb - background), axis=2)
    mask = delta > 0.055
    rows, columns = np.where(mask)
    if not len(rows):
        box = (0.0, 0.0, 1.0, 1.0)
        canvas_fraction = 0.0
    else:
        x0, x1 = int(columns.min()), int(columns.max()) + 1
        y0, y1 = int(rows.min()), int(rows.max()) + 1
        box = (x0 / width, y0 / height, x1 / width, y1 / height)
        canvas_fraction = 1.0 - ((x1 - x0) * (y1 - y0) / (width * height))
    return box, _unit(canvas_fraction), tuple(float(value) for value in background)


def visual_signature(path: str | Path, *, size: int = 32) -> VisualSignature:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        rgb = np.asarray(image, dtype=np.float32) / 255.0
    luminance = rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
    horizontal = np.pad(np.abs(np.diff(luminance, axis=1)), ((0, 0), (0, 1)))
    vertical = np.pad(np.abs(np.diff(luminance, axis=0)), ((0, 1), (0, 0)))
    edges = (horizontal + vertical) / 2.0
    box, canvas_fraction, border_rgb = _content_geometry(rgb)
    return VisualSignature(rgb, luminance, edges, box, canvas_fraction, border_rgb)


def _geometry_distance(left: VisualSignature, right: VisualSignature) -> float:
    lx0, ly0, lx1, ly1 = left.content_box
    rx0, ry0, rx1, ry1 = right.content_box
    center = (abs((lx0 + lx1) - (rx0 + rx1)) + abs((ly0 + ly1) - (ry0 + ry1))) / 2
    scale = (abs((lx1 - lx0) - (rx1 - rx0)) + abs((ly1 - ly0) - (ry1 - ry0))) / 2
    return _unit(center / 0.20 * 0.55 + scale / 0.20 * 0.45)


def image_distance(
    left: VisualSignature,
    right: VisualSignature,
    weights: Mapping[str, float] | None = None,
) -> ImageDistanceResult:
    selected = dict(DEFAULT_DISTANCE_WEIGHTS)
    if weights:
        selected.update({str(key): float(value) for key, value in weights.items()})
    denominator = sum(max(0.0, value) for value in selected.values())
    if denominator <= 0:
        raise ValueError("distance weights must contain a positive value")

    small_left = np.asarray(Image.fromarray((left.luminance * 255).astype(np.uint8)).resize((16, 16), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    small_right = np.asarray(Image.fromarray((right.luminance * 255).astype(np.uint8)).resize((16, 16), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    ssim = (structural_similarity(left.luminance, right.luminance) + structural_similarity(small_left, small_right)) / 2
    structural = _unit((1.0 - ssim) / 0.45)
    luminance = _unit(float(np.mean(np.abs(left.luminance - right.luminance))) / 0.22)
    color = _unit(float(np.mean(np.abs(left.rgb - right.rgb))) / 0.25)
    edge = _unit(float(np.mean(np.abs(left.edges - right.edges))) / 0.12)
    geometry = _geometry_distance(left, right)
    border_delta = float(np.mean(np.abs(np.asarray(left.border_rgb) - np.asarray(right.border_rgb))))
    canvas = _unit(abs(left.canvas_fraction - right.canvas_fraction) / 0.20 * 0.7 + border_delta / 0.25 * 0.3)
    components = {
        "structural": structural,
        "luminance": luminance,
        "color": color,
        "edge": edge,
        "geometry": geometry,
        "canvas": canvas,
    }
    total = sum(components[key] * max(0.0, selected[key]) for key in components) / denominator
    return ImageDistanceResult(_unit(total), structural, luminance, color, edge, geometry, canvas)
