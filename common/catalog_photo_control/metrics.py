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


def structural_similarity(left: np.ndarray, right: np.ndarray) -> float:
    """Deterministic luminance SSIM using the standard global formulation."""
    c1, c2 = 0.01**2, 0.03**2
    mean_left, mean_right = float(np.mean(left)), float(np.mean(right))
    variance_left, variance_right = float(np.var(left)), float(np.var(right))
    covariance = float(np.mean((left - mean_left) * (right - mean_right)))
    numerator = (2 * mean_left * mean_right + c1) * (2 * covariance + c2)
    denominator = (mean_left**2 + mean_right**2 + c1) * (
        variance_left + variance_right + c2
    )
    return max(-1.0, min(1.0, numerator / max(denominator, 1e-12)))


def image_metrics(source_path: Path, output_path: Path, canvas_metadata: Mapping[str, object] | None = None) -> dict[str, object]:
    source = _rgb(source_path)
    output = _rgb(output_path)
    metadata = canvas_metadata or {}
    content_box = metadata.get("content_box")
    if content_box:
        with Image.open(output_path) as opened:
            content = opened.convert("RGB").crop(tuple(content_box))
            content = content.resize((source.shape[1], source.shape[0]), Image.Resampling.BILINEAR)
            output_for_distance = np.asarray(content, dtype=np.float32) / 255.0
    elif output.shape[:2] != source.shape[:2]:
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
        1.0 if source_sharpness < 1e-6
        else output_sharpness / max(source_sharpness, 1e-9)
    )
    clip_fraction = float(np.mean((output_luma <= 0.01) | (output_luma >= 0.99)))
    channel_spread = np.max(output, axis=2) - np.min(output, axis=2)
    metrics = {
        "brightness": float(np.mean(output_luma)),
        "contrast": float(np.std(output_luma)),
        "sharpness": output_sharpness,
        "sharpness_ratio": sharpness_ratio,
        "clip_fraction": clip_fraction,
        "colorfulness": float(np.mean(channel_spread)),
        "pixel_mae": float(np.mean(np.abs(source - output_for_distance))),
        "luminance_mae": float(np.mean(np.abs(source_luma - distance_luma))),
        "ssim": structural_similarity(source_luma, distance_luma),
        "content_ssim": structural_similarity(source_luma, distance_luma),
        "canvas_fraction": float(metadata.get("canvas_fraction", 0.0)),
        "foreground_scale_ratio": float(metadata.get("foreground_scale_ratio", 1.0)),
        "output_width": float(output.shape[1]),
        "output_height": float(output.shape[0]),
    }
    metrics.update({key: metadata[key] for key in ("detected_background_rgb", "sampled_background_rgb", "sampled_background_confidence", "sampled_background_fallback_used", "fallback_origin", "background_rgb", "background_origin", "canvas_mode") if key in metadata})
    return metrics


def aggregate_metrics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not rows:
        raise ValueError("cannot aggregate an empty metric set")
    keys = {key for key in set.intersection(*(set(row) for row in rows)) if all(isinstance(row[key], (int, float, bool)) for row in rows)}
    result: dict[str, object] = {}
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
        "canvas_mode_code": 6.0,
        "mean_canvas_fraction": 0.08,
        "rotation_degrees": 2.4,
        "crop_fraction": 0.02,
        "zoom": 0.025,
        "resize_scale": 0.035,
        "offset_x": 0.04,
        "offset_y": 0.04,
    }
    components = {
        key: abs(float(left.get(key, 0)) - float(right.get(key, 0))) / scale
        for key, scale in scales.items()
    }
    return sum(components.values()) / len(components), components
