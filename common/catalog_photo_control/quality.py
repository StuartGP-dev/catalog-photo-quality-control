from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class QualityResult:
    valid: bool
    score: float
    reasons: tuple[str, ...]


def evaluate_quality(
    per_image: Sequence[Mapping[str, float]],
    thresholds: Mapping[str, float],
    *,
    geometry_active: bool = False,
) -> QualityResult:
    if not per_image:
        return QualityResult(False, 0.0, ("no_images",))
    max_clip = float(thresholds.get("maximum_clip_fraction", 0.2))
    min_sharpness = float(thresholds.get("minimum_sharpness_ratio", 0.35))
    max_sharpness = float(thresholds.get("maximum_sharpness_ratio", 1.8))
    max_pixel_mae = float(thresholds.get("maximum_pixel_mae", 0.055))
    max_luminance_mae = float(thresholds.get("maximum_luminance_mae", 0.045))
    min_ssim = float(thresholds.get("minimum_ssim", 0.97))
    min_geometry_ssim = float(
        thresholds.get("minimum_perceptual_geometry_ssim", min_ssim)
    )
    minimum_quality = float(thresholds.get("minimum_quality", 0.35))
    reasons: list[str] = []
    image_scores: list[float] = []
    for index, metrics in enumerate(per_image):
        clip = float(metrics["clip_fraction"])
        sharpness = float(metrics["sharpness_ratio"])
        exposure = float(metrics["brightness"])
        score = (
            0.45 * min(1.0, sharpness)
            + 0.35 * max(0.0, 1.0 - clip / max(max_clip, 1e-9))
            + 0.20 * max(0.0, 1.0 - abs(exposure - 0.5) / 0.5)
        )
        image_scores.append(score)
        if clip > max_clip:
            reasons.append("fidelity_clip_fraction")
        if sharpness < min_sharpness:
            reasons.append("fidelity_sharpness")
        if sharpness > max_sharpness:
            reasons.append("fidelity_sharpness")
        if float(metrics["pixel_mae"]) > max_pixel_mae:
            reasons.append("fidelity_pixel_mae")
        if float(metrics["luminance_mae"]) > max_luminance_mae:
            reasons.append("fidelity_luminance_mae")
        if geometry_active:
            geometry_ssim = float(
                metrics.get("perceptual_geometry_ssim", metrics["ssim"])
            )
            if geometry_ssim < min_geometry_ssim:
                reasons.append("fidelity_geometry_ssim")
        elif float(metrics["ssim"]) < min_ssim:
            reasons.append("fidelity_ssim")
    listing_score = min(image_scores)
    if listing_score < minimum_quality:
        reasons.append("listing:minimum_quality")
    return QualityResult(not reasons, listing_score, tuple(dict.fromkeys(reasons)))
