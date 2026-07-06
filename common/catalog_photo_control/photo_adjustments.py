from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


# Ces ajustements servent au controle qualite interne et a la mesure
# de robustesse du controle de coherence visuelle des photos.

PhotoAdjustmentOp = Callable[[Image.Image], Image.Image]


def _as_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()


def _save_jpeg(image: Image.Image, path: Path, quality: int = 92) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _as_rgb(image).save(path, format="JPEG", quality=quality, optimize=True)


def _resize_roundtrip(scale: float) -> PhotoAdjustmentOp:
    def op(image: Image.Image) -> Image.Image:
        original_size = image.size
        resized = image.resize(
            (max(1, int(original_size[0] * scale)), max(1, int(original_size[1] * scale))),
            Image.Resampling.LANCZOS,
        )
        return resized.resize(original_size, Image.Resampling.LANCZOS)

    return op


def _rotate_keep_size(angle: float) -> PhotoAdjustmentOp:
    def op(image: Image.Image) -> Image.Image:
        original_size = image.size
        rotated = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
        fitted = ImageOps.fit(rotated, original_size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        return fitted

    return op


def _enhance(kind: str, factor: float) -> PhotoAdjustmentOp:
    enhancer_cls = {
        "brightness": ImageEnhance.Brightness,
        "contrast": ImageEnhance.Contrast,
        "sharpness": ImageEnhance.Sharpness,
    }[kind]

    def op(image: Image.Image) -> Image.Image:
        return enhancer_cls(image).enhance(factor)

    return op


def _blur(radius: float) -> PhotoAdjustmentOp:
    return lambda image: image.filter(ImageFilter.GaussianBlur(radius=radius))


def _noise(stddev: float) -> PhotoAdjustmentOp:
    return _noise_with_seed(stddev, seed=12345)


def _noise_with_seed(stddev: float, seed: int | None = None) -> PhotoAdjustmentOp:
    def op(image: Image.Image) -> Image.Image:
        arr = np.asarray(_as_rgb(image)).astype(np.int16)
        rng = np.random.default_rng(12345 if seed is None else seed)
        noisy = np.clip(arr + rng.normal(0, stddev, arr.shape), 0, 255).astype(np.uint8)
        return Image.fromarray(noisy, mode="RGB")

    return op


def _crop_border_keep_size(pct: float) -> PhotoAdjustmentOp:
    def op(image: Image.Image) -> Image.Image:
        width, height = image.size
        dx = max(1, int(width * pct))
        dy = max(1, int(height * pct))
        cropped = image.crop((dx, dy, width - dx, height - dy))
        return cropped.resize((width, height), Image.Resampling.LANCZOS)

    return op


def _pad_border_keep_size(pct: float) -> PhotoAdjustmentOp:
    def op(image: Image.Image) -> Image.Image:
        width, height = image.size
        pad_x = max(1, int(width * pct))
        pad_y = max(1, int(height * pct))
        inner = image.resize((max(1, width - 2 * pad_x), max(1, height - 2 * pad_y)), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(_as_rgb(inner), (pad_x, pad_y))
        return canvas

    return op


def _autocontrast(cutoff: float) -> PhotoAdjustmentOp:
    return lambda image: ImageOps.autocontrast(_as_rgb(image), cutoff=cutoff)


def _photo_adjustment_specs(preset: str) -> list[dict[str, Any]]:
    base = [
        {"name": "jpeg_q92", "params": {"quality": 92}, "comment": "Recompression JPEG legere."},
        {"name": "jpeg_q85", "params": {"quality": 85}, "comment": "Recompression JPEG moyenne."},
        {"name": "strip_exif", "params": {}, "comment": "Suppression des metadonnees EXIF."},
        {
            "name": "resize_roundtrip_098",
            "params": {"scale": 0.98},
            "op": _resize_roundtrip(0.98),
            "comment": "Leger redimensionnement puis retour a la taille initiale.",
        },
        {
            "name": "rotate_1deg",
            "params": {"angle": 1.0},
            "op": _rotate_keep_size(1.0),
            "comment": "Legere rotation avec recadrage pour garder les dimensions.",
        },
        {
            "name": "brightness_104",
            "params": {"factor": 1.04},
            "op": _enhance("brightness", 1.04),
            "comment": "Petit ajustement de luminosite.",
        },
        {
            "name": "contrast_104",
            "params": {"factor": 1.04},
            "op": _enhance("contrast", 1.04),
            "comment": "Petit ajustement de contraste.",
        },
        {
            "name": "sharpness_110",
            "params": {"factor": 1.10},
            "op": _enhance("sharpness", 1.10),
            "comment": "Legere nettete.",
        },
        {
            "name": "blur_03",
            "params": {"radius": 0.3},
            "op": _blur(0.3),
            "comment": "Leger flou.",
        },
        {
            "name": "noise_2",
            "params": {"stddev": 2.0},
            "op": _noise(2.0),
            "comment": "Tres leger bruit.",
        },
    ]
    combos = [
        {
            "name": "jpeg_q88_brightness_103",
            "params": {"quality": 88, "brightness": 1.03},
            "ops": [_enhance("brightness", 1.03)],
            "comment": "Combinaison bornee: luminosite legere et recompression.",
        },
        {
            "name": "resize_099_contrast_103",
            "params": {"scale": 0.99, "contrast": 1.03},
            "ops": [_resize_roundtrip(0.99), _enhance("contrast", 1.03)],
            "comment": "Combinaison bornee: resize leger et contraste leger.",
        },
    ]
    extended_extra = [
        {
            "name": "jpeg_q78",
            "params": {"quality": 78},
            "comment": "Recompression JPEG plus marquee mais bornee.",
        },
        {
            "name": "rotate_minus_1deg",
            "params": {"angle": -1.0},
            "op": _rotate_keep_size(-1.0),
            "comment": "Legere rotation inverse avec dimensions conservees.",
        },
        {
            "name": "resize_roundtrip_102",
            "params": {"scale": 1.02},
            "op": _resize_roundtrip(1.02),
            "comment": "Leger agrandissement puis retour a la taille initiale.",
        },
    ]
    thorough_extra = [
        {"name": "jpeg_q70", "params": {"quality": 70}, "comment": "Recompression JPEG forte mais lisible."},
        {"name": "jpeg_q60", "params": {"quality": 60}, "comment": "Recompression JPEG controle approfondi."},
        {
            "name": "rotate_05deg",
            "params": {"angle": 0.5},
            "op": _rotate_keep_size(0.5),
            "comment": "Rotation tres legere positive.",
        },
        {
            "name": "rotate_minus_05deg",
            "params": {"angle": -0.5},
            "op": _rotate_keep_size(-0.5),
            "comment": "Rotation tres legere negative.",
        },
        {
            "name": "rotate_2deg",
            "params": {"angle": 2.0},
            "op": _rotate_keep_size(2.0),
            "comment": "Rotation bornee positive plus visible.",
        },
        {
            "name": "rotate_minus_2deg",
            "params": {"angle": -2.0},
            "op": _rotate_keep_size(-2.0),
            "comment": "Rotation bornee negative plus visible.",
        },
        {
            "name": "crop_01_resize",
            "params": {"crop_pct": 0.01},
            "op": _crop_border_keep_size(0.01),
            "comment": "Recadrage bord 1% puis retour aux dimensions.",
        },
        {
            "name": "crop_02_resize",
            "params": {"crop_pct": 0.02},
            "op": _crop_border_keep_size(0.02),
            "comment": "Recadrage bord 2% puis retour aux dimensions.",
        },
        {
            "name": "pad_01_white",
            "params": {"pad_pct": 0.01},
            "op": _pad_border_keep_size(0.01),
            "comment": "Marge blanche 1% puis image reduite au centre.",
        },
        {
            "name": "brightness_096",
            "params": {"factor": 0.96},
            "op": _enhance("brightness", 0.96),
            "comment": "Petite baisse de luminosite.",
        },
        {
            "name": "contrast_096",
            "params": {"factor": 0.96},
            "op": _enhance("contrast", 0.96),
            "comment": "Petite baisse de contraste.",
        },
        {
            "name": "saturation_like_autocontrast_1",
            "params": {"cutoff": 1.0},
            "op": _autocontrast(1.0),
            "comment": "Autocontraste faible pour tester la robustesse luminance.",
        },
        {
            "name": "blur_06",
            "params": {"radius": 0.6},
            "op": _blur(0.6),
            "comment": "Flou modere borne.",
        },
        {
            "name": "noise_5",
            "params": {"stddev": 5.0},
            "op": _noise(5.0),
            "comment": "Bruit modere borne.",
        },
        {
            "name": "rotate_1deg_jpeg_q80",
            "params": {"angle": 1.0, "quality": 80},
            "ops": [_rotate_keep_size(1.0)],
            "comment": "Combinaison bornee: rotation 1 degre et recompression.",
        },
        {
            "name": "crop_01_brightness_104",
            "params": {"crop_pct": 0.01, "brightness": 1.04},
            "ops": [_crop_border_keep_size(0.01), _enhance("brightness", 1.04)],
            "comment": "Combinaison bornee: recadrage 1% et luminosite.",
        },
        {
            "name": "resize_098_jpeg_q75",
            "params": {"scale": 0.98, "quality": 75},
            "ops": [_resize_roundtrip(0.98)],
            "comment": "Combinaison bornee: resize leger et recompression.",
        },
    ]

    if preset == "light":
        return base[:4]
    if preset == "default":
        return base + combos
    if preset == "extended":
        return base + combos + extended_extra
    if preset == "thorough":
        return base + combos + extended_extra + thorough_extra
    raise ValueError("preset doit etre light, default, extended ou thorough")


def _json_safe_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "params": deepcopy(spec.get("params", {})),
        "comment": spec.get("comment", ""),
    }


def list_photo_adjustment_specs(preset: str = "default") -> list[dict[str, Any]]:
    """Retourne les ajustements disponibles sans fonctions non-serialisables.

    Cette liste sert aux rapports de controle pour afficher ce qui reste a tester, avec un resultat simple et serialisable.
    """
    return [_json_safe_spec(spec) for spec in _photo_adjustment_specs(preset)]


def _apply_adjustment_spec(image: Image.Image, spec: dict[str, Any]) -> Image.Image:
    mutated = image.copy()
    for op in spec.get("ops", []):
        mutated = op(mutated)
    if "op" in spec:
        mutated = spec["op"](mutated)
    return mutated


def _catalog_photo_ops_from_params(params: dict[str, Any], seed: int | None = None) -> list[PhotoAdjustmentOp]:
    """Builds small catalog-photo adjustment operations from serializable params."""

    ops: list[PhotoAdjustmentOp] = []

    crop_pct = params.get("crop_pct")
    if crop_pct is not None and float(crop_pct) > 0:
        ops.append(_crop_border_keep_size(float(crop_pct)))

    pad_pct = params.get("pad_pct")
    if pad_pct is not None and float(pad_pct) > 0:
        ops.append(_pad_border_keep_size(float(pad_pct)))

    scale = params.get("resize_scale", params.get("scale"))
    if scale is not None and float(scale) != 1.0:
        ops.append(_resize_roundtrip(float(scale)))

    angle = params.get("rotation_angle", params.get("angle"))
    if angle is not None and float(angle) != 0.0:
        ops.append(_rotate_keep_size(float(angle)))

    brightness = params.get("brightness_factor", params.get("brightness"))
    if brightness is not None and float(brightness) != 1.0:
        ops.append(_enhance("brightness", float(brightness)))

    contrast = params.get("contrast_factor", params.get("contrast"))
    if contrast is not None and float(contrast) != 1.0:
        ops.append(_enhance("contrast", float(contrast)))

    sharpness = params.get("sharpness_factor", params.get("sharpness"))
    if sharpness is not None and float(sharpness) != 1.0:
        ops.append(_enhance("sharpness", float(sharpness)))

    cutoff = params.get("autocontrast_cutoff", params.get("cutoff"))
    if cutoff is not None and float(cutoff) > 0:
        ops.append(_autocontrast(float(cutoff)))

    blur_radius = params.get("blur_radius", params.get("radius"))
    if blur_radius is not None and float(blur_radius) > 0:
        ops.append(_blur(float(blur_radius)))

    noise_stddev = params.get("noise_stddev", params.get("stddev"))
    if noise_stddev is not None and float(noise_stddev) > 0:
        ops.append(_noise_with_seed(float(noise_stddev), seed=seed))

    return ops


def generate_catalog_photo_adjustment(
    source_image: str | Path,
    output_path: str | Path,
    adjustment_name: str,
    params: dict[str, Any],
    seed: int | None = None,
) -> dict[str, Any]:
    """Generate a serializable, bounded catalog-photo adjustment for quality checks."""

    source_image = Path(source_image)
    output_path = Path(output_path)
    safe_params = deepcopy(params)
    quality = int(safe_params.get("jpeg_quality", safe_params.get("quality", 92)))
    quality = min(100, max(1, quality))

    with Image.open(source_image) as image:
        mutated = _as_rgb(ImageOps.exif_transpose(image))

    for op in _catalog_photo_ops_from_params(safe_params, seed=seed):
        mutated = op(mutated)

    _save_jpeg(mutated, output_path, quality=quality)
    return {
        "adjustment_name": adjustment_name,
        "name": adjustment_name,
        "params": safe_params,
        "path": str(output_path),
        "seed": seed,
    }


def generate_photo_adjustment_from_spec(
    source_image: str | Path,
    output_path: str | Path,
    adjustment_name: str,
    preset: str = "default",
) -> dict[str, Any]:
    """Genere un ajustement photo borne a partir de son nom."""
    source_image = Path(source_image)
    output_path = Path(output_path)
    specs = {spec["name"]: spec for spec in _photo_adjustment_specs(preset)}
    if adjustment_name not in specs:
        raise ValueError(f"Ajustement inconnue pour preset={preset}: {adjustment_name}")

    spec = specs[adjustment_name]
    quality = int(spec.get("params", {}).get("quality", 92))
    with Image.open(source_image) as image:
        original = _as_rgb(ImageOps.exif_transpose(image))
    mutated = _apply_adjustment_spec(original, spec)
    _save_jpeg(mutated, output_path, quality=quality)
    result = _json_safe_spec(spec)
    result["path"] = str(output_path)
    result["adjustment_name"] = spec["name"]
    return result


def generate_quality_photo_adjustments(
    source_image: str | Path,
    output_dir: str | Path,
    preset: str = "default",
    skip_photo_adjustments: set[str] | None = None,
) -> list[dict[str, Any]]:
    source_image = Path(source_image)
    output_dir = Path(output_dir)
    adjustments: list[dict[str, Any]] = []
    skip_photo_adjustments = skip_photo_adjustments or set()

    specs = _photo_adjustment_specs(preset)
    for index, spec in enumerate(specs):
        if spec["name"] in skip_photo_adjustments:
            continue
        out_path = output_dir / f"{source_image.stem}_{index:02d}_{spec['name']}.jpg"
        adjustment = generate_photo_adjustment_from_spec(source_image, out_path, spec["name"], preset=preset)
        adjustments.append(adjustment)

    return adjustments
