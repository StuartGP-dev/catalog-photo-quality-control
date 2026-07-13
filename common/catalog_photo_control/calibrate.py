from __future__ import annotations

import argparse
import html
import json
import math
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps

from .config import load_filter_space
from .image_pipeline import RenderedVariant, render_listing
from .metrics import aggregate_metrics, image_metrics
from .models import Recipe, SourceListing, canonical_json, stable_hash
from .source_loader import load_source_listing, resolve_listing_reference


CALIBRATION_VERSION = "visual-geometry-v1"
CANVAS_MODES = (
    "white",
    "light_gray",
    "sampled_background",
    "sampled_edge",
    "side_bands",
    "uniform_frame",
)
EXPLORATION_LIMITS = {
    "minimum_ssim": 0.94,
    "maximum_pixel_mae": 0.04,
    "maximum_luminance_mae": 0.035,
    "minimum_sharpness_ratio": 0.7,
    "maximum_sharpness_ratio": 1.6,
    "maximum_clip_fraction": 0.02,
    "maximum_canvas_fraction": 0.16,
}
COARSE_VALUES = {
    "rotation": (0.25, 0.5, 0.8, 1.2, 1.6, 2.0, 2.5),
    "crop": (0.003, 0.006, 0.01, 0.015, 0.02, 0.03, 0.04),
    "zoom": (1.003, 1.006, 1.01, 1.015, 1.02, 1.03, 1.05),
    "dezoom": (0.995, 0.99, 0.985, 0.975, 0.965, 0.95, 0.93),
    "offset": (0.003, 0.006, 0.01, 0.015, 0.02, 0.03),
}
FAMILY_THRESHOLDS = {
    "rotation": {"perceptible": 0.6, "strong": 1.8},
    "crop": {"perceptible": 0.006, "strong": 0.025},
    "zoom": {"perceptible": 0.006, "strong": 0.03},
    "dezoom": {"perceptible": 0.01, "strong": 0.05},
    "offset": {"perceptible": 0.006, "strong": 0.02},
    "rotation_crop_compensated": {"perceptible": 0.32, "strong": 0.82},
    "rotation_zoom": {"perceptible": 0.32, "strong": 0.82},
    "zoom_offset": {"perceptible": 0.3, "strong": 0.8},
    "rotation_dezoom_canvas": {"perceptible": 0.3, "strong": 0.78},
    "crop_offset": {"perceptible": 0.3, "strong": 0.8},
}
FAMILY_DIRECTORY_CODES = {
    "rotation": "rot",
    "crop": "crop",
    "zoom": "zoom",
    "dezoom": "dezoom",
    "offset": "offset",
    "rotation_crop_compensated": "rot_crop",
    "rotation_zoom": "rot_zoom",
    "zoom_offset": "zoom_off",
    "rotation_dezoom_canvas": "rot_dezoom",
    "crop_offset": "crop_off",
}
FAMILY_ALIASES = {
    "rotation": ("rotation",),
    "crop": ("crop",),
    "zoom": ("zoom",),
    "dezoom": ("dezoom",),
    "offset": ("offset",),
    "geometry-combinations": (
        "rotation_crop_compensated",
        "rotation_zoom",
        "zoom_offset",
        "rotation_dezoom_canvas",
        "crop_offset",
    ),
}


@dataclass(frozen=True, slots=True)
class CalibrationSpec:
    family: str
    branch: str
    intensity: float
    parameters: Mapping[str, object]
    stage: str = "coarse"

    @property
    def key(self) -> str:
        return stable_hash(
            {
                "family": self.family,
                "branch": self.branch,
                "parameters": self.parameters,
                "stage": self.stage,
            }
        )


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    spec: CalibrationSpec
    recipe: Recipe
    directory: Path
    rendered: RenderedVariant
    per_image: tuple[Mapping[str, object], ...]
    aggregate: Mapping[str, object]
    classification: str
    rejection_reasons: tuple[str, ...]


def calibration_config_hash(
    families: Sequence[str], coarse_steps: int, bisection_steps: int
) -> str:
    return stable_hash(
        {
            "version": CALIBRATION_VERSION,
            "families": list(families),
            "coarse_steps": coarse_steps,
            "bisection_steps": bisection_steps,
            "coarse_values": COARSE_VALUES,
            "family_thresholds": FAMILY_THRESHOLDS,
            "exploration_limits": EXPLORATION_LIMITS,
            "canvas_modes": CANVAS_MODES,
        }
    )


def _sample_steps(values: Sequence[float], count: int) -> tuple[float, ...]:
    if count <= 0:
        raise ValueError("coarse steps must be positive")
    if count >= len(values):
        return tuple(values)
    indices = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)] if count > 1 else [0]
    return tuple(values[index] for index in dict.fromkeys(indices))


def neutral_parameters() -> dict[str, object]:
    space = load_filter_space()
    return {name: spec.default for name, spec in space.schema.parameters.items()}


def calibration_recipe(parameters: Mapping[str, object]) -> Recipe:
    values = neutral_parameters()
    defaults = dict(values)
    values.update(parameters)
    if float(values["resize_scale"]) < 1 and values["canvas_mode"] == "none":
        raise ValueError("dezoom requires an active canvas mode")
    active = [
        name
        for name, value in values.items()
        if name != "jpeg_quality" and value != defaults[name]
    ]
    if len(active) > 4:
        raise ValueError("too_many_active_parameters")
    return Recipe.from_parameters(values)


def _spec(family: str, branch: str, intensity: float, **parameters: object) -> CalibrationSpec:
    return CalibrationSpec(family, branch, round(float(intensity), 12), parameters)


def generate_coarse_specs(families: Sequence[str], coarse_steps: int) -> list[CalibrationSpec]:
    requested = set(families)
    specs: list[CalibrationSpec] = []
    if "rotation" in requested:
        for value in _sample_steps(COARSE_VALUES["rotation"], coarse_steps):
            specs.extend((_spec("rotation", "left", value, rotation_degrees=-value), _spec("rotation", "right", value, rotation_degrees=value)))
    if "crop" in requested:
        specs.extend(_spec("crop", "symmetric", value, crop_fraction=value) for value in _sample_steps(COARSE_VALUES["crop"], coarse_steps))
    if "zoom" in requested:
        specs.extend(_spec("zoom", "forward", value - 1, zoom=value) for value in _sample_steps(COARSE_VALUES["zoom"], coarse_steps))
    if "dezoom" in requested:
        for value in sorted(_sample_steps(COARSE_VALUES["dezoom"], coarse_steps), reverse=True):
            for mode in CANVAS_MODES:
                extra: dict[str, object] = {}
                if mode == "side_bands":
                    extra["side_band_width"] = min(0.035, max(0.006, (1 - value) / 2))
                elif mode == "uniform_frame":
                    extra["uniform_frame_width"] = min(0.02, max(0.004, (1 - value) / 3))
                specs.append(_spec("dezoom", mode, 1 - value, resize_scale=value, canvas_mode=mode, **extra))
    if "offset" in requested:
        for value in _sample_steps(COARSE_VALUES["offset"], coarse_steps):
            specs.extend((
                _spec("offset", "left", value, offset_x=-value),
                _spec("offset", "right", value, offset_x=value),
                _spec("offset", "up", value, offset_y=-value),
                _spec("offset", "down", value, offset_y=value),
            ))
    steps = tuple(index / max(1, coarse_steps - 1) for index in range(coarse_steps))
    if "rotation_crop_compensated" in requested:
        for t in steps:
            for sign, branch in ((-1, "left"), (1, "right")):
                specs.append(_spec("rotation_crop_compensated", branch, t, rotation_degrees=sign * (0.8 + t), crop_fraction=0.005 + 0.01 * t))
    if "rotation_zoom" in requested:
        for t in steps:
            for sign, branch in ((-1, "left"), (1, "right")):
                specs.append(_spec("rotation_zoom", branch, t, rotation_degrees=sign * (0.5 + 0.7 * t), zoom=1.005 + 0.015 * t))
    if "zoom_offset" in requested:
        for t in steps:
            for branch, parameter, sign in (("left", "offset_x", -1), ("right", "offset_x", 1), ("up", "offset_y", -1), ("down", "offset_y", 1)):
                specs.append(_spec("zoom_offset", branch, t, zoom=1.01 + 0.02 * t, **{parameter: sign * (0.005 + 0.01 * t)}))
    if "rotation_dezoom_canvas" in requested:
        for t in steps:
            for sign, branch in ((-1, "left"), (1, "right")):
                mode = CANVAS_MODES[min(len(CANVAS_MODES) - 1, round(t * (len(CANVAS_MODES) - 1)))]
                extra = {"uniform_frame_width": 0.006 + 0.006 * t} if mode == "uniform_frame" else ({"side_band_width": 0.008 + 0.012 * t} if mode == "side_bands" else {})
                specs.append(_spec("rotation_dezoom_canvas", f"{branch}-{mode}", t, rotation_degrees=sign * (0.5 + 0.5 * t), resize_scale=0.985 - 0.025 * t, canvas_mode=mode, **extra))
    if "crop_offset" in requested:
        for t in steps:
            for branch, parameter, sign in (("left", "offset_x", -1), ("right", "offset_x", 1), ("up", "offset_y", -1), ("down", "offset_y", 1)):
                specs.append(_spec("crop_offset", branch, t, crop_fraction=0.005 + 0.01 * t, **{parameter: sign * (0.005 + 0.005 * t)}))
    return specs


def deterministic_bisection(
    lower: CalibrationSpec, upper: CalibrationSpec, steps: int
) -> list[CalibrationSpec]:
    if lower.family != upper.family or lower.branch != upper.branch:
        raise ValueError("bisection endpoints must share family and branch")
    results: list[CalibrationSpec] = []
    low, high = lower, upper
    for _ in range(steps):
        parameters: dict[str, object] = {}
        for key in sorted(set(low.parameters) | set(high.parameters)):
            left = low.parameters.get(key)
            right = high.parameters.get(key)
            if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
                parameters[key] = (float(left) + float(right)) / 2
            else:
                parameters[key] = right if right is not None else left
        midpoint = CalibrationSpec(low.family, low.branch, (low.intensity + high.intensity) / 2, parameters, "bisection")
        results.append(midpoint)
        high = midpoint
    return results


def _foreground_box(path: Path, content_box: Sequence[int] | None = None) -> tuple[int, int, int, int] | None:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        if content_box:
            image = image.crop(tuple(content_box))
        array = np.asarray(image, dtype=np.float32)
    h, w = array.shape[:2]
    band = max(1, round(min(h, w) * 0.06))
    edges = np.concatenate((array[:band].reshape(-1, 3), array[-band:].reshape(-1, 3), array[:, :band].reshape(-1, 3), array[:, -band:].reshape(-1, 3)))
    background = np.median(edges, axis=0)
    distance = np.max(np.abs(array - background), axis=2)
    mask = distance > 22
    ys, xs = np.where(mask)
    if len(xs) < max(16, w * h * 0.001):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _geometry_diagnostics(source_path: Path, output_path: Path, metadata: Mapping[str, object], spec: CalibrationSpec) -> dict[str, object]:
    content_box = metadata.get("content_box")
    source_box = _foreground_box(source_path)
    output_box = _foreground_box(output_path, content_box if isinstance(content_box, (tuple, list)) else None)
    diagnostics: dict[str, object] = {
        "angle_degrees": abs(float(spec.parameters.get("rotation_degrees", 0.0))),
        "framing_change": max(float(spec.parameters.get("crop_fraction", 0.0)), abs(float(spec.parameters.get("zoom", 1.0)) - 1), abs(float(spec.parameters.get("resize_scale", 1.0)) - 1)),
        "center_shift": math.hypot(float(spec.parameters.get("offset_x", 0.0)), float(spec.parameters.get("offset_y", 0.0))),
        "foreground_clipped": False,
    }
    if source_box and output_box:
        with Image.open(source_path) as source:
            sw, sh = source.size
        if isinstance(content_box, (tuple, list)):
            ow, oh = int(content_box[2]) - int(content_box[0]), int(content_box[3]) - int(content_box[1])
        else:
            with Image.open(output_path) as output:
                ow, oh = output.size
        source_margin = min(source_box[0] / sw, source_box[1] / sh, (sw - source_box[2]) / sw, (sh - source_box[3]) / sh)
        output_margin = min(output_box[0] / ow, output_box[1] / oh, (ow - output_box[2]) / ow, (oh - output_box[3]) / oh)
        diagnostics.update({"source_foreground_box": source_box, "output_foreground_box": output_box, "foreground_margin": output_margin})
        diagnostics["foreground_clipped"] = bool(output_margin <= 0.002 and source_margin > 0.01)
    return diagnostics


def _exploration_reasons(rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    reasons: list[str] = []
    for row in rows:
        if float(row["ssim"]) < EXPLORATION_LIMITS["minimum_ssim"]:
            reasons.append("ssim")
        if float(row["pixel_mae"]) > EXPLORATION_LIMITS["maximum_pixel_mae"]:
            reasons.append("pixel_mae")
        if float(row["luminance_mae"]) > EXPLORATION_LIMITS["maximum_luminance_mae"]:
            reasons.append("luminance_mae")
        if not EXPLORATION_LIMITS["minimum_sharpness_ratio"] <= float(row["sharpness_ratio"]) <= EXPLORATION_LIMITS["maximum_sharpness_ratio"]:
            reasons.append("sharpness")
        if float(row["clip_fraction"]) > EXPLORATION_LIMITS["maximum_clip_fraction"]:
            reasons.append("clipping")
        if float(row.get("canvas_fraction", 0.0)) > EXPLORATION_LIMITS["maximum_canvas_fraction"]:
            reasons.append("excessive_canvas")
        if bool(row.get("foreground_clipped")):
            reasons.append("product_clipped")
        background = row.get("background_rgb")
        if isinstance(background, (tuple, list)) and (sum(background) / 3 < 178 or max(background) - min(background) > 64):
            reasons.append("unsafe_background")
    return tuple(dict.fromkeys(reasons))


def classify_calibration(family: str, intensity: float, rows: Sequence[Mapping[str, object]]) -> tuple[str, tuple[str, ...]]:
    reasons = _exploration_reasons(rows)
    if reasons:
        return "rejected", reasons
    threshold = FAMILY_THRESHOLDS[family]
    if intensity < threshold["perceptible"]:
        return "very_subtle", ()
    if intensity < threshold["strong"]:
        return "perceptible_candidate", ()
    return "strong_candidate", ()


def _normalized_intensity(spec: CalibrationSpec) -> float:
    if spec.family == "rotation":
        return abs(float(spec.parameters["rotation_degrees"]))
    if spec.family == "crop":
        return float(spec.parameters["crop_fraction"])
    if spec.family == "zoom":
        return float(spec.parameters["zoom"]) - 1
    if spec.family == "dezoom":
        return 1 - float(spec.parameters["resize_scale"])
    if spec.family == "offset":
        return max(abs(float(spec.parameters.get("offset_x", 0))), abs(float(spec.parameters.get("offset_y", 0))))
    return spec.intensity


def _difference_assets(source_path: Path, output_path: Path, metadata: Mapping[str, object], destination: Path) -> dict[str, str]:
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    with Image.open(output_path) as opened:
        output = opened.convert("RGB")
    content_box = metadata.get("content_box")
    comparable = output.crop(tuple(content_box)) if isinstance(content_box, (tuple, list)) else output
    comparable = comparable.resize(source.size, Image.Resampling.LANCZOS)
    difference = ImageEnhance.Contrast(ImageChops.difference(source, comparable)).enhance(4.0)
    heat = ImageOps.colorize(ImageOps.grayscale(difference), black="#000018", white="#ff5a00")
    diff_path = destination / "difference.jpg"
    heat.save(diff_path, quality=94, subsampling=0)
    width, height = source.size
    central_box = (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
    central = Image.new("RGB", (width, height // 2))
    central.paste(source.crop(central_box).resize((width // 2, height // 2)), (0, 0))
    central.paste(comparable.crop(central_box).resize((width // 2, height // 2)), (width // 2, 0))
    central_path = destination / "central.jpg"
    central.save(central_path, quality=94, subsampling=0)
    edge = max(8, round(min(width, height) * 0.14))
    strips = [source.crop((0, 0, width, edge)), comparable.crop((0, 0, width, edge)), source.crop((0, height - edge, width, height)), comparable.crop((0, height - edge, width, height))]
    edges = Image.new("RGB", (width, edge * 4))
    for index, strip in enumerate(strips):
        edges.paste(strip, (0, edge * index))
    edges_path = destination / "edges.jpg"
    edges.save(edges_path, quality=94, subsampling=0)
    boxed = output.copy()
    if isinstance(content_box, (tuple, list)):
        ImageDraw.Draw(boxed).rectangle(tuple(content_box), outline=(255, 40, 20), width=max(2, round(min(boxed.size) / 300)))
    box_path = destination / "content_box.jpg"
    boxed.save(box_path, quality=94, subsampling=0)
    return {"difference": diff_path.name, "central": central_path.name, "edges": edges_path.name, "content_box": box_path.name}


def _execute_spec(listing: SourceListing, spec: CalibrationSpec, examples_root: Path) -> CalibrationResult:
    recipe = calibration_recipe(spec.parameters)
    destination = examples_root / FAMILY_DIRECTORY_CODES[spec.family] / spec.key[:12]
    rendered_root = destination / "variant"
    if destination.exists():
        shutil.rmtree(destination)
    rendered = render_listing(listing, recipe, rendered_root)
    rows: list[Mapping[str, object]] = []
    for source, output in zip(listing.images, rendered.images, strict=True):
        metrics = image_metrics(source.path, output.output_path, output.canvas_metadata)
        metrics.update(_geometry_diagnostics(source.path, output.output_path, output.canvas_metadata, spec))
        asset_dir = destination / "inspection" / f"image_{source.index:04d}"
        assets = _difference_assets(source.path, output.output_path, output.canvas_metadata, asset_dir)
        metrics["inspection_assets"] = assets
        rows.append(metrics)
    aggregate = aggregate_metrics(rows)
    intensity = _normalized_intensity(spec)
    classification, reasons = classify_calibration(spec.family, intensity, rows)
    aggregate = {**aggregate, "intensity": intensity, "classification": classification, "rejection_reasons": list(reasons)}
    return CalibrationResult(spec, recipe, destination, rendered, tuple(rows), aggregate, classification, reasons)


def _transition_pairs(results: Sequence[CalibrationResult]) -> list[tuple[CalibrationSpec, CalibrationSpec]]:
    pairs: list[tuple[CalibrationSpec, CalibrationSpec]] = []
    branches = sorted({result.spec.branch for result in results})
    for branch in branches:
        branch_results = sorted((result for result in results if result.spec.branch == branch), key=lambda result: result.spec.intensity)
        for lower, upper in zip(branch_results, branch_results[1:]):
            if lower.classification == "very_subtle" and upper.classification in {"perceptible_candidate", "strong_candidate"}:
                pairs.append((lower.spec, upper.spec))
                break
    return pairs


def _relative(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _fmt(value: object, digits: int = 5) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else str(value)


def _write_report(path: Path, listing: SourceListing, config_hash: str, results: Sequence[CalibrationResult]) -> None:
    if path.name != "index.html":
        raise ValueError("calibration report must be named index.html")
    sections: list[str] = []
    for family in sorted({result.spec.family for result in results}):
        family_results = sorted((result for result in results if result.spec.family == family), key=lambda result: (result.spec.branch, result.spec.intensity, result.spec.stage))
        cards: list[str] = []
        for number, result in enumerate(family_results):
            image_cards: list[str] = []
            for source, output, metrics in zip(listing.images, result.rendered.images, result.per_image, strict=True):
                original = html.escape(_relative(source.path, path.parent))
                variant = html.escape(_relative(output.output_path, path.parent))
                asset_root = result.directory / "inspection" / f"image_{source.index:04d}"
                assets = metrics["inspection_assets"]
                image_cards.append(f'''<article class="image-card"><h4>Image source #{source.index + 1}</h4>
                <div class="compare" data-original="{original}" data-variant="{variant}"><img class="base" src="{original}" alt="original"><img class="overlay" src="{variant}" alt="variant"></div>
                <div class="controls"><button type="button" class="toggle">Original / variant</button><button type="button" class="blink">Alternance</button><label>Avant/après <input class="slider" type="range" min="0" max="100" value="50"></label><button type="button" class="zoom">Zoom 100 %</button></div>
                <div class="tools"><figure><img src="{html.escape(_relative(asset_root / str(assets['difference']), path.parent))}" alt="difference map"><figcaption>Différence amplifiée ×4</figcaption></figure>
                <figure><img src="{html.escape(_relative(asset_root / str(assets['central']), path.parent))}" alt="central crops"><figcaption>Crop central original / variant</figcaption></figure>
                <figure><img src="{html.escape(_relative(asset_root / str(assets['edges']), path.parent))}" alt="edge crops"><figcaption>Bords original / variant</figcaption></figure>
                <figure><img src="{html.escape(_relative(asset_root / str(assets['content_box']), path.parent))}" alt="content box"><figcaption>Boîte du contenu</figcaption></figure></div>
                <p>SSIM {_fmt(metrics['ssim'])} · pixel MAE {_fmt(metrics['pixel_mae'])} · luminance MAE {_fmt(metrics['luminance_mae'])} · netteté {_fmt(metrics['sharpness_ratio'])} · clipping {_fmt(metrics['clip_fraction'])}</p>
                <p>Dimensions {int(metrics['output_width'])}×{int(metrics['output_height'])} · canvas_fraction {_fmt(metrics['canvas_fraction'])} · foreground_scale_ratio {_fmt(metrics['foreground_scale_ratio'])}</p>
                <p>Fond détecté {html.escape(str(metrics.get('detected_background_rgb', metrics.get('sampled_background_rgb', 'n/a'))))} · utilisé {html.escape(str(metrics.get('background_rgb', 'n/a')))} · origine {html.escape(str(metrics.get('background_origin', 'n/a')))} · confiance {_fmt(metrics.get('sampled_background_confidence', 'n/a'))} · fallback {html.escape(str(metrics.get('sampled_background_fallback_used', 'n/a')))} ({html.escape(str(metrics.get('fallback_origin', 'none')))})</p></article>''')
            recipe_json = html.escape(json.dumps(result.recipe.parameters, indent=2, ensure_ascii=False))
            cards.append(f'''<article class="example {result.classification}"><h3>{html.escape(result.spec.branch)} · {result.spec.stage} · intensité {_fmt(_normalized_intensity(result.spec))}</h3>
            <p class="badge">{result.classification}</p><p>Paramètres exacts : <code>{html.escape(canonical_json(result.spec.parameters))}</code></p>
            <p>Agrégat : min SSIM {_fmt(result.aggregate.get('min_ssim'))} · max pixel MAE {_fmt(result.aggregate.get('max_pixel_mae'))} · max luminance MAE {_fmt(result.aggregate.get('max_luminance_mae'))} · raisons {html.escape(', '.join(result.rejection_reasons) or 'aucune')}</p>
            <details><summary>Recette canonique neutralisée</summary><pre>{recipe_json}</pre></details>{''.join(image_cards)}</article>''')
        sections.append(f'<section><h2>{html.escape(family)}</h2>{"".join(cards)}</section>')
    counts = {name: sum(result.classification == name for result in results) for name in ("very_subtle", "perceptible_candidate", "strong_candidate", "rejected")}
    document = f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Calibration visuelle {html.escape(listing.listing_code)}</title>
    <style>body{{font:14px system-ui;margin:1.5rem;background:#f3f4f6;color:#1f2937}}main{{max-width:1600px;margin:auto}}section,.example,.image-card,.summary{{background:#fff;padding:1rem;margin:1rem 0;border-radius:10px;box-shadow:0 1px 5px #0002}}.example{{border-left:7px solid #94a3b8}}.perceptible_candidate{{border-color:#16a34a}}.strong_candidate{{border-color:#eab308}}.rejected{{border-color:#dc2626}}.compare{{position:relative;width:min(100%,900px);height:520px;overflow:auto;background:#ddd}}.compare img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}}.compare .overlay{{clip-path:inset(0 0 0 50%)}}.compare.actual img{{width:auto;height:auto;max-width:none;max-height:none}}.controls{{display:flex;gap:.6rem;flex-wrap:wrap;margin:.6rem 0}}.tools{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:.5rem}}figure{{margin:0}}.tools img{{width:100%;height:190px;object-fit:contain;background:#eee}}pre{{max-height:22rem;overflow:auto}}code{{overflow-wrap:anywhere}}.badge{{font-weight:700}}</style></head><body><main>
    <h1>Calibration visuelle géométrique</h1><div class="summary"><p>Annonce {html.escape(listing.listing_code)} · {len(listing.images)} images · source_set_hash <code>{listing.source_set_hash}</code></p><p>Hash de configuration <code>{config_hash}</code> · version {CALIBRATION_VERSION}</p><p>{html.escape(str(counts))}</p><p>Les métriques proposent des candidats. Elles ne prouvent pas la perception humaine ; la validation visuelle des cinq images reste déterminante.</p></div>{''.join(sections)}
    <script>document.querySelectorAll('.compare').forEach(box=>{{let timer=null;const overlay=box.querySelector('.overlay');const slider=box.parentElement.querySelector('.slider');slider.addEventListener('input',()=>overlay.style.clipPath=`inset(0 0 0 ${{slider.value}}%)`);box.parentElement.querySelector('.toggle').onclick=()=>{{overlay.style.display=overlay.style.display==='none'?'block':'none'}};box.parentElement.querySelector('.blink').onclick=e=>{{if(timer){{clearInterval(timer);timer=null;e.target.textContent='Alternance'}}else{{timer=setInterval(()=>overlay.style.display=overlay.style.display==='none'?'block':'none',350);e.target.textContent='Arrêter'}}}};box.parentElement.querySelector('.zoom').onclick=()=>box.classList.toggle('actual')}});</script></main></body></html>'''
    path.write_text(document, encoding="utf-8")


def _summary(results: Sequence[CalibrationResult]) -> dict[str, object]:
    output: dict[str, object] = {}
    for family in sorted({result.spec.family for result in results}):
        rows = [result for result in results if result.spec.family == family]
        subtle = [result for result in rows if result.classification == "very_subtle"]
        perceptible = [result for result in rows if result.classification == "perceptible_candidate"]
        accepted = [result for result in rows if result.classification != "rejected"]
        output[family] = {
            "examples": len(rows),
            "images_passing_controls": sum(1 for result in rows for image in result.per_image if not _exploration_reasons([image])),
            "quasi_imperceptible_max": max((_normalized_intensity(result.spec) for result in subtle), default=None),
            "estimated_perceptible_min": min((_normalized_intensity(result.spec) for result in perceptible), default=None),
            "prudent_max": max((_normalized_intensity(result.spec) for result in accepted if result.classification == "perceptible_candidate"), default=None),
            "observed_min_ssim": min((float(result.aggregate["min_ssim"]) for result in rows), default=None),
            "observed_max_pixel_mae": max((float(result.aggregate["max_pixel_mae"]) for result in rows), default=None),
        }
    return output


def _expand_families(value: str) -> tuple[str, ...]:
    expanded: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if name not in FAMILY_ALIASES:
            raise ValueError(f"unknown calibration family: {name}")
        expanded.extend(FAMILY_ALIASES[name])
    return tuple(dict.fromkeys(expanded))


def run_calibration(args: argparse.Namespace) -> tuple[Path, dict[str, object]]:
    listing_dir, listing_code = resolve_listing_reference(args.listing, args.source_root)
    listing = load_source_listing(listing_dir, listing_code=listing_code)
    families = _expand_families(args.families)
    config_hash = calibration_config_hash(families, args.coarse_steps, args.bisection_steps)
    safe_listing = "".join(character if character.isalnum() or character in "-_" else "_" for character in listing.listing_code) or "listing"
    run_dir = Path(args.output_root).resolve() / safe_listing / f"{listing.source_set_hash[:12]}-{config_hash[:12]}"
    manifest_path = run_dir / "manifest.json"
    report = run_dir / "index.html"
    if manifest_path.is_file() and report.is_file() and not args.force:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("source_set_hash") == listing.source_set_hash and manifest.get("config_hash") == config_hash:
            return report, manifest["summary"]
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    coarse_specs = generate_coarse_specs(families, args.coarse_steps)
    results = [_execute_spec(listing, spec, run_dir / "examples") for spec in coarse_specs]
    for family in families:
        family_results = [result for result in results if result.spec.family == family and result.spec.stage == "coarse"]
        for lower, upper in _transition_pairs(family_results):
            results.extend(_execute_spec(listing, spec, run_dir / "examples") for spec in deterministic_bisection(lower, upper, args.bisection_steps))
    _write_report(report, listing, config_hash, results)
    summary = _summary(results)
    result_rows = [
        {
            "family": result.spec.family,
            "branch": result.spec.branch,
            "stage": result.spec.stage,
            "intensity": _normalized_intensity(result.spec),
            "parameters": result.spec.parameters,
            "recipe_hash": result.recipe.recipe_hash,
            "classification": result.classification,
            "rejection_reasons": result.rejection_reasons,
            "aggregate": result.aggregate,
            "per_image": result.per_image,
        }
        for result in results
    ]
    (run_dir / "calibration_results.json").write_text(
        json.dumps(result_rows, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    manifest = {
        "version": CALIBRATION_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "listing": listing.listing_code,
        "source_set_hash": listing.source_set_hash,
        "source_hashes": [image.source_hash for image in listing.images],
        "config_hash": config_hash,
        "families": list(families),
        "example_count": len(results),
        "html_count": len(list(run_dir.rglob("*.html"))),
        "results_file": "calibration_results.json",
        "summary": summary,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report, summary


def run_diversity_calibration(args: argparse.Namespace) -> tuple[Path, dict[str, object]]:
    from .diversity_analysis import (
        analysis_summary,
        analyze_pairs,
        load_analysis_images,
        nearest_pairs,
        threshold_outcomes,
        write_analysis_html,
        write_analysis_json,
    )
    from .paths import LocalPaths
    from .visual_distance import DISTANCE_METRICS_VERSION

    listing_dir, listing_code = resolve_listing_reference(args.listing, args.source_root)
    listing = load_source_listing(listing_dir, listing_code=listing_code)
    database = LocalPaths.from_root(args.local_root).variants_database.resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    before = database.stat()
    try:
        images = load_analysis_images(connection, listing.listing_code)
        if not any(image.variant_id is not None for image in images):
            raise ValueError(f"no ready variants found for {listing.listing_code}")
        pairs = analyze_pairs(images, args.distance_scope, config=load_filter_space().diversity_gate)
    finally:
        connection.close()
    after = database.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("read-only diversity calibration changed the variants database")
    summary = analysis_summary(pairs)
    thresholds = (0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06)
    config_hash = stable_hash({
        "version": DISTANCE_METRICS_VERSION,
        "scope": args.distance_scope,
        "source_set_hash": listing.source_set_hash,
        "thresholds": thresholds,
    })
    root = Path(args.diversity_output_root).resolve() / listing.listing_code
    run_dir = root / f"{listing.source_set_hash[:12]}-{config_hash[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    report = run_dir / "index.html"
    write_analysis_html(report, pairs, summary, thresholds, args.top_nearest)
    write_analysis_json(run_dir / "diversity_results.json", pairs, summary, thresholds)
    manifest = {
        "version": DISTANCE_METRICS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "listing": listing.listing_code,
        "source_set_hash": listing.source_set_hash,
        "source_hashes": [image.source_hash for image in listing.images],
        "config_hash": config_hash,
        "scope": args.distance_scope,
        "pair_count": len(pairs),
        "nearest_pair_count": len(nearest_pairs(pairs)),
        "threshold_outcomes": threshold_outcomes(nearest_pairs(pairs), thresholds),
        "summary": summary,
        "html_count": len(list(run_dir.rglob("*.html"))),
        "read_only_unchanged": True,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return report, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate visible catalog geometry transformations without writing final variants.")
    parser.add_argument("--listing", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--families", default="rotation,crop,zoom,dezoom,offset,geometry-combinations")
    parser.add_argument("--output-root", default="local/calibration_runs")
    parser.add_argument("--coarse-steps", type=int, default=6)
    parser.add_argument("--bisection-steps", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--calibrate-diversity-gate", action="store_true")
    parser.add_argument("--distance-scope", choices=("listing", "catalog", "both"), default="both")
    parser.add_argument("--local-root", default="local")
    parser.add_argument("--diversity-output-root", default="local/diversity_calibration")
    parser.add_argument("--top-nearest", type=int, default=50)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.coarse_steps <= 0 or args.bisection_steps < 0:
        print("error: step counts must be positive", file=sys.stderr)
        return 2
    try:
        report, summary = (
            run_diversity_calibration(args)
            if args.calibrate_diversity_gate
            else run_calibration(args)
        )
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"report={report}")
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
