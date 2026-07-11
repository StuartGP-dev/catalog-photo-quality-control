from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class QualityResult:
    valid: bool
    score: float
    reasons: tuple[str, ...]


def evaluate_quality(
    per_image: Sequence[Mapping[str, float]], thresholds: Mapping[str, float]
) -> QualityResult:
    if not per_image:
        return QualityResult(False, 0.0, ("no_images",))
    max_clip = float(thresholds.get("maximum_clip_fraction", 0.2))
    min_sharpness = float(thresholds.get("minimum_sharpness_ratio", 0.35))
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
            reasons.append(f"image_{index}:clip_fraction")
        if sharpness < min_sharpness:
            reasons.append(f"image_{index}:sharpness_ratio")
    listing_score = min(image_scores)
    if listing_score < minimum_quality:
        reasons.append("listing:minimum_quality")
    return QualityResult(not reasons, listing_score, tuple(reasons))
