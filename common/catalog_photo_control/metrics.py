from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
from PIL import Image, ImageOps


def _rgb(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(ImageOps.exif_transpose(opened).convert("RGB"), dtype=np.float32) / 255.0


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def _sharpness(luminance: np.ndarray) -> float:
    if min(luminance.shape) < 2:
        return 0.0
    horizontal = np.diff(luminance, axis=1)
    vertical = np.diff(luminance, axis=0)
    return float((np.mean(horizontal * horizontal) + np.mean(vertical * vertical)) / 2)


def image_metrics(source_path: Path, output_path: Path) -> dict[str, float]:
    source = _rgb(source_path)
    output = _rgb(output_path)
    if output.shape[:2] != source.shape[:2]:
        with Image.open(output_path) as opened:
            resized = opened.convert("RGB").resize(
                (source.shape[1], source.shape[0]), Image.Resampling.BILINEAR
            )
            output_for_distance = np.asarray(resized, dtype=np.float32) / 255.0
    else:
        output_for_distance = output
    source_luma = _luminance(source)
    output_luma = _luminance(output)
    distance_luma = _luminance(output_for_distance)
    source_sharpness = _sharpness(source_luma)
    output_sharpness = _sharpness(output_luma)
    sharpness_ratio = (
        1.0 if source_sharpness < 1e-9 and output_sharpness < 1e-9
        else output_sharpness / max(source_sharpness, 1e-9)
    )
    clip_fraction = float(np.mean((output_luma <= 0.01) | (output_luma >= 0.99)))
    channel_spread = np.max(output, axis=2) - np.min(output, axis=2)
    return {
        "brightness": float(np.mean(output_luma)),
        "contrast": float(np.std(output_luma)),
        "sharpness": output_sharpness,
        "sharpness_ratio": sharpness_ratio,
        "clip_fraction": clip_fraction,
        "colorfulness": float(np.mean(channel_spread)),
        "pixel_mae": float(np.mean(np.abs(source - output_for_distance))),
        "luminance_mae": float(np.mean(np.abs(source_luma - distance_luma))),
    }


def aggregate_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        raise ValueError("cannot aggregate an empty metric set")
    keys = set.intersection(*(set(row) for row in rows))
    result: dict[str, float] = {}
    for key in sorted(keys):
        values = [float(row[key]) for row in rows]
        result[f"mean_{key}"] = sum(values) / len(values)
        result[f"min_{key}"] = min(values)
        result[f"max_{key}"] = max(values)
    result["image_count"] = float(len(rows))
    return result


def metric_distance(
    left: Mapping[str, float], right: Mapping[str, float]
) -> tuple[float, dict[str, float]]:
    scales = {
        "mean_brightness": 1.0,
        "mean_contrast": 0.5,
        "mean_colorfulness": 0.5,
        "mean_pixel_mae": 1.0,
        "mean_luminance_mae": 1.0,
        "mean_sharpness_ratio": 2.0,
    }
    components = {
        key: abs(float(left.get(key, 0)) - float(right.get(key, 0))) / scale
        for key, scale in scales.items()
    }
    return sum(components.values()) / len(components), components
