from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .listing_photo_review import _safe_listing_code, find_listing_images, resolve_listing_dir
from .photo_adjustments import generate_catalog_photo_adjustment
from .photo_comparison_rules import compare_photo_pair

DECISION_MARGIN_PRESET = "decision_margin_search"
DECISION_MARGIN_DB_VERSION = 1


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_id(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _config_hash(data: dict[str, Any]) -> str:
    return _stable_id({"kind": "catalog_quality_config", **data})[:16]


def _mode(listing_code: str) -> str:
    return listing_code.strip().replace("\\", "/").split("/", 1)[0]


def _distances(comparison: dict[str, Any]) -> dict[str, int]:
    hashes = comparison["visual_check"]["hashes"]
    return {
        "phash": hashes["phash"]["hamming_distance"],
        "dhash": hashes["dhash"]["hamming_distance"],
        "whash": hashes["whash"]["hamming_distance"],
    }


def _distance_score(distances: dict[str, int]) -> int:
    return int(distances.get("phash", 0)) + int(distances.get("dhash", 0)) + int(distances.get("whash", 0))


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "item"


def _format_value(value: float | int, integer: bool = False) -> str:
    if integer:
        return str(int(round(float(value))))
    return f"{float(value):.4f}".rstrip("0").rstrip(".").replace("-", "m").replace(".", "p")


def _decision_margin_db_path(output_root: str | Path, listing_code: str) -> Path:
    return Path(output_root) / "_decision_margin_catalog" / f"{_safe_listing_code(listing_code)}.json"


def _empty_decision_margin_db(listing_code: str) -> dict[str, Any]:
    return {
        "version": DECISION_MARGIN_DB_VERSION,
        "listing_code": listing_code,
        "transition_points": {},
        "combined_photo_adjustments": {},
        "updated_at": None,
    }


def _load_decision_margin_db(output_root: str | Path, listing_code: str) -> dict[str, Any]:
    data = _read_json(_decision_margin_db_path(output_root, listing_code))
    if not data:
        return _empty_decision_margin_db(listing_code)
    if data.get("version") != DECISION_MARGIN_DB_VERSION:
        return {
            "version": DECISION_MARGIN_DB_VERSION,
            "listing_code": listing_code,
            "transition_points": data.get("transition_points", {}) if isinstance(data.get("transition_points"), dict) else {},
            "combined_photo_adjustments": data.get("combined_photo_adjustments", {}) if isinstance(data.get("combined_photo_adjustments"), dict) else {},
            "updated_at": data.get("updated_at"),
        }
    data.setdefault("transition_points", {})
    data.setdefault("combined_photo_adjustments", {})
    return data


def _existing_quality_catalog_path(output_root: str | Path, listing_code: str) -> Path:
    return Path(output_root) / "_photo_quality_catalog" / f"{_safe_listing_code(listing_code)}.json"


def _iter_existing_reference_items(output_root: str | Path, listing_code: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    catalog = _read_json(_existing_quality_catalog_path(output_root, listing_code))
    for item in (catalog.get("items", {}) or {}).values():
        if isinstance(item, dict):
            items.append(item)

    listing_root = Path(output_root) / _safe_listing_code(listing_code)
    if listing_root.exists():
        for report_path in sorted(listing_root.glob("*/photo_quality_control_report.json"), reverse=True):
            report = _read_json(report_path)
            for item in report.get("reference_photo_checks", {}).get("comparisons", []):
                if isinstance(item, dict):
                    photo_adjustment = item.get("photo_adjustment", {})
                    comparison = item.get("comparison", {})
                    items.append(
                        {
                            "source_image": item.get("source_image"),
                            "adjustment_name": photo_adjustment.get("adjustment_name"),
                            "params": photo_adjustment.get("params", {}),
                            "distances": item.get("distances", {}),
                            "visual_status": comparison.get("visual_check", {}).get("status"),
                            "overall_check": comparison.get("overall_check", {}).get("status"),
                            "first_seen_report": str(report_path.parent),
                        }
                    )
    return items


def _review_margin_candidates(output_root: str | Path, listing_code: str, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _iter_existing_reference_items(output_root, listing_code):
        status = str(item.get("visual_status") or item.get("overall_check") or "")
        if status not in {"review", "clear"}:
            continue
        distances = item.get("distances", {}) or {}
        key = _stable_id(
            {
                "source_image": item.get("source_image"),
                "adjustment_name": item.get("adjustment_name"),
                "params": item.get("params", {}),
                "distances": distances,
                "status": status,
            }
        )
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "source_image": item.get("source_image"),
                "adjustment_name": item.get("adjustment_name"),
                "params": item.get("params", {}) or {},
                "visual_status": status,
                "overall_check": item.get("overall_check"),
                "distances": distances,
                "distance_score": _distance_score(distances),
                "first_seen_report": item.get("first_seen_report"),
            }
        )

    status_priority = {"review": 1, "clear": 0}
    candidates.sort(
        key=lambda item: (
            status_priority.get(str(item.get("visual_status")), 0),
            int(item.get("distance_score", 0)),
            int((item.get("distances") or {}).get("phash", 0)),
        ),
        reverse=True,
    )
    return candidates[: max(0, limit)]


def _decision_margin_families() -> list[dict[str, Any]]:
    return [
        {
            "photo_adjustment_family": "rotation",
            "parameter_name": "rotation_angle",
            "report_label": "rotation",
            "values": [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.5, 6.0, 8.0],
            "min_value": 0.0,
            "max_value": 8.0,
            "tolerance": 0.1,
            "integer": False,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "crop_pct",
            "parameter_name": "crop_pct",
            "report_label": "recadrage leger",
            "values": [0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.04],
            "min_value": 0.0,
            "max_value": 0.04,
            "tolerance": 0.001,
            "integer": False,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "blur_radius",
            "parameter_name": "blur_radius",
            "report_label": "flou leger",
            "values": [0.1, 0.2, 0.3, 0.45, 0.6, 0.8, 1.0, 1.25],
            "min_value": 0.0,
            "max_value": 1.25,
            "tolerance": 0.025,
            "integer": False,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "jpeg_quality",
            "parameter_name": "jpeg_quality",
            "report_label": "compression JPEG",
            "values": [96, 94, 92, 88, 84, 80, 75, 70, 65],
            "min_value": 65,
            "max_value": 96,
            "tolerance": 1.0,
            "integer": True,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "noise_stddev",
            "parameter_name": "noise_stddev",
            "report_label": "bruit leger",
            "values": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0],
            "min_value": 0.0,
            "max_value": 6.0,
            "tolerance": 0.25,
            "integer": False,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "resize_rotation",
            "parameter_name": "rotation_angle",
            "report_label": "resize leger + rotation",
            "values": [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.5, 6.0],
            "min_value": 0.0,
            "max_value": 6.0,
            "tolerance": 0.1,
            "integer": False,
            "base_params": {"resize_scale": 0.985},
        },
        {
            "photo_adjustment_family": "brightness_factor",
            "parameter_name": "brightness_factor",
            "report_label": "luminosite",
            "values": [1.01, 1.02, 1.04, 1.06, 1.08, 1.1],
            "min_value": 1.0,
            "max_value": 1.1,
            "tolerance": 0.005,
            "integer": False,
            "base_params": {},
        },
        {
            "photo_adjustment_family": "contrast_factor",
            "parameter_name": "contrast_factor",
            "report_label": "contraste",
            "values": [1.01, 1.02, 1.04, 1.06, 1.08, 1.1],
            "min_value": 1.0,
            "max_value": 1.1,
            "tolerance": 0.005,
            "integer": False,
            "base_params": {},
        },
    ]


def _candidate_anchor_value(candidate: dict[str, Any], family: dict[str, Any]) -> float | None:
    params = candidate.get("params", {}) or {}
    aliases = {
        "rotation_angle": ("rotation_angle", "angle"),
        "crop_pct": ("crop_pct",),
        "blur_radius": ("blur_radius", "radius"),
        "jpeg_quality": ("jpeg_quality", "quality"),
        "noise_stddev": ("noise_stddev", "stddev"),
        "brightness_factor": ("brightness_factor", "brightness", "factor"),
        "contrast_factor": ("contrast_factor", "contrast", "factor"),
    }
    for name in aliases.get(family["parameter_name"], (family["parameter_name"],)):
        if name in params:
            try:
                return float(params[name])
            except (TypeError, ValueError):
                return None
    return None


def _values_with_candidate_anchors(
    family: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_image: Path,
) -> list[float | int]:
    values = [float(value) for value in family["values"]]
    relevant: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_source = candidate.get("source_image")
        if not candidate_source:
            continue
        candidate_path = Path(str(candidate_source))
        same_path = False
        if candidate_path.exists() and source_image.exists():
            same_path = candidate_path.resolve() == source_image.resolve()
        else:
            same_path = str(candidate_path) == str(source_image)
        if same_path:
            relevant.append(candidate)
    for candidate in relevant:
        anchor = _candidate_anchor_value(candidate, family)
        if anchor is None:
            continue
        spread = max(float(family["tolerance"]) * 3, abs(anchor) * 0.25 if anchor else float(family["tolerance"]) * 4)
        values.extend([anchor - spread, anchor, anchor + spread])

    min_value = float(family["min_value"])
    max_value = float(family["max_value"])
    filtered = [min(max(value, min_value), max_value) for value in values]
    if family.get("integer"):
        unique = sorted({int(round(value)) for value in filtered}, reverse=family["photo_adjustment_family"] == "jpeg_quality")
        return unique
    return sorted({round(value, 6) for value in filtered})


def _make_params(family: dict[str, Any], value: float | int) -> dict[str, Any]:
    params = dict(family.get("base_params", {}))
    parameter = family["parameter_name"]
    params[parameter] = int(round(float(value))) if family.get("integer") else float(value)
    return params


def _evaluation_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
    comparison = evaluation["comparison"]
    return {
        "value": evaluation["value"],
        "params": evaluation["params"],
        "adjustment_path": evaluation["photo_adjustment"]["path"],
        "visual_status": comparison["visual_check"]["status"],
        "overall_check": comparison["overall_check"]["status"],
        "reason": comparison["overall_check"]["reason"],
        "distances": evaluation["distances"],
    }


def _evaluate_margin_value(
    source_image: Path,
    family: dict[str, Any],
    value: float | int,
    output_dir: Path,
    listing_code: str,
    sensitivity: str,
    seed: int,
    index: int,
) -> dict[str, Any]:
    params = _make_params(family, value)
    integer = bool(family.get("integer"))
    value_label = _format_value(value, integer=integer)
    family_name = family["photo_adjustment_family"]
    adjustment_name = f"{family_name}_{value_label}"
    test_key = {
        "listing_code": listing_code,
        "mode": _mode(listing_code),
        "preset": DECISION_MARGIN_PRESET,
        "sensitivity": sensitivity,
        "source_image": str(source_image),
        "photo_adjustment_family": family_name,
        "params": params,
        "seed": seed,
    }
    test_id = _stable_id({"kind": "decision_margin_test", **test_key})
    path = output_dir / _safe_name(source_image.stem) / family_name / f"{index:03d}_{adjustment_name}_{test_id[:8]}.jpg"
    photo_adjustment = generate_catalog_photo_adjustment(
        source_image,
        path,
        adjustment_name=adjustment_name,
        params=params,
        seed=seed,
    )
    comparison = compare_photo_pair(source_image, photo_adjustment["path"], sensitivity=sensitivity)
    return {
        "test_id": test_id,
        "source_image": str(source_image),
        "photo_adjustment_family": family_name,
        "parameter_name": family["parameter_name"],
        "value": params[family["parameter_name"]],
        "params": params,
        "photo_adjustment": photo_adjustment,
        "comparison": comparison,
        "distances": _distances(comparison),
    }


def _find_transition_for_family(
    source_image: Path,
    family: dict[str, Any],
    candidates: list[dict[str, Any]],
    output_dir: Path,
    listing_code: str,
    sensitivity: str,
    seed: int,
    max_iterations: int,
) -> dict[str, Any]:
    values = _values_with_candidate_anchors(family, candidates, source_image)
    evaluations: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    transition_pair: tuple[dict[str, Any], dict[str, Any]] | None = None

    for value in values:
        current = _evaluate_margin_value(
            source_image,
            family,
            value,
            output_dir,
            listing_code,
            sensitivity,
            seed,
            len(evaluations),
        )
        evaluations.append(current)
        if previous and current["comparison"]["overall_check"]["status"] != previous["comparison"]["overall_check"]["status"]:
            transition_pair = (previous, current)
            break
        previous = current

    if transition_pair is not None:
        low, high = transition_pair
        for _ in range(max(0, max_iterations)):
            low_value = float(low["value"])
            high_value = float(high["value"])
            if abs(high_value - low_value) <= float(family["tolerance"]):
                break
            mid_value: float | int = (low_value + high_value) / 2
            if family.get("integer"):
                mid_value = int(round(mid_value))
                if mid_value in {int(round(low_value)), int(round(high_value))}:
                    break
            current = _evaluate_margin_value(
                source_image,
                family,
                mid_value,
                output_dir,
                listing_code,
                sensitivity,
                seed,
                len(evaluations),
            )
            evaluations.append(current)
            if current["comparison"]["overall_check"]["status"] == low["comparison"]["overall_check"]["status"]:
                low = current
            else:
                high = current
        transition_pair = (low, high)

    transition_found = transition_pair is not None
    before_transition = _evaluation_summary(transition_pair[0]) if transition_pair else None
    after_transition = _evaluation_summary(transition_pair[1]) if transition_pair else None
    transition_value = None
    decision_margin_id = None
    if transition_pair:
        transition_value = (float(transition_pair[0]["value"]) + float(transition_pair[1]["value"])) / 2
        if family.get("integer"):
            transition_value = int(round(transition_value))
        else:
            transition_value = round(float(transition_value), 6)
        decision_margin_id = _stable_id(
            {
                "kind": "decision_margin_result",
                "listing_code": listing_code,
                "source_image": str(source_image),
                "family": family["photo_adjustment_family"],
                "transition_value": transition_value,
                "before": before_transition,
                "after": after_transition,
                "seed": seed,
            }
        )

    return {
        "decision_margin_id": decision_margin_id,
        "source_image": str(source_image),
        "photo_adjustment_family": family["photo_adjustment_family"],
        "report_label": family["report_label"],
        "parameter_name": family["parameter_name"],
        "transition_found": transition_found,
        "transition_value": transition_value,
        "decision_before_transition": before_transition["overall_check"] if before_transition else None,
        "decision_after_transition": after_transition["overall_check"] if after_transition else None,
        "before_transition": before_transition,
        "after_transition": after_transition,
        "tests_performed": len(evaluations),
        "evaluations": [_evaluation_summary(item) for item in evaluations],
    }


def _candidate_source_images(images: list[Path], candidates: list[dict[str, Any]], limit: int) -> list[Path]:
    existing = {image.resolve(): image for image in images}
    selected: list[Path] = []
    for candidate in candidates:
        source = candidate.get("source_image")
        if not source:
            continue
        path = Path(str(source))
        resolved = path.resolve() if path.exists() else None
        if resolved and resolved in existing and existing[resolved] not in selected:
            selected.append(existing[resolved])
        if len(selected) >= limit:
            return selected
    for image in images:
        if image not in selected:
            selected.append(image)
        if len(selected) >= limit:
            break
    return selected


def _combined_photo_adjustment_params(rng: random.Random, template_name: str) -> dict[str, Any]:
    if template_name == "crop_rotation_brightness":
        return {
            "crop_pct": round(rng.uniform(0.003, 0.015), 5),
            "rotation_angle": round(rng.uniform(-1.2, 1.2), 4),
            "brightness_factor": round(rng.uniform(0.96, 1.05), 4),
            "jpeg_quality": rng.randint(88, 96),
        }
    if template_name == "jpeg_resize_rotation":
        return {
            "jpeg_quality": rng.randint(82, 94),
            "resize_scale": round(rng.uniform(0.985, 1.015), 5),
            "rotation_angle": round(rng.uniform(-1.0, 1.0), 4),
        }
    if template_name == "crop_noise_contrast":
        return {
            "crop_pct": round(rng.uniform(0.003, 0.012), 5),
            "noise_stddev": round(rng.uniform(0.5, 3.0), 4),
            "contrast_factor": round(rng.uniform(0.96, 1.05), 4),
            "jpeg_quality": rng.randint(88, 96),
        }
    return {
        "resize_scale": round(rng.uniform(0.99, 1.01), 5),
        "brightness_factor": round(rng.uniform(0.97, 1.04), 4),
        "contrast_factor": round(rng.uniform(0.97, 1.04), 4),
        "jpeg_quality": rng.randint(88, 96),
    }


def _run_combined_photo_adjustments(
    listing_code: str,
    source_images: list[Path],
    output_dir: Path,
    sensitivity: str,
    seed: int,
    max_combinations: int,
) -> list[dict[str, Any]]:
    if not source_images or max_combinations <= 0:
        return []
    rng = random.Random(seed)
    templates = ["crop_rotation_brightness", "jpeg_resize_rotation", "crop_noise_contrast", "resize_luminance_balance"]
    results: list[dict[str, Any]] = []
    for index in range(max_combinations):
        source_image = source_images[index % len(source_images)]
        template_name = templates[index % len(templates)]
        params = _combined_photo_adjustment_params(rng, template_name)
        key = {
            "listing_code": listing_code,
            "mode": _mode(listing_code),
            "preset": DECISION_MARGIN_PRESET,
            "sensitivity": sensitivity,
            "source_image": str(source_image),
            "combined_photo_adjustment": template_name,
            "params": params,
            "seed": seed,
            "index": index,
        }
        test_id = _stable_id({"kind": "combined_photo_adjustment", **key})
        adjustment_name = f"{template_name}_{index:03d}"
        path = output_dir / _safe_name(source_image.stem) / "combined_photo_adjustments" / f"{adjustment_name}_{test_id[:8]}.jpg"
        photo_adjustment = generate_catalog_photo_adjustment(
            source_image,
            path,
            adjustment_name=adjustment_name,
            params=params,
            seed=seed + index,
        )
        comparison = compare_photo_pair(source_image, photo_adjustment["path"], sensitivity=sensitivity)
        results.append(
            {
                "test_id": test_id,
                "source_image": str(source_image),
                "combined_photo_adjustment": template_name,
                "photo_adjustment": photo_adjustment,
                "params": params,
                "comparison": comparison,
                "visual_status": comparison["visual_check"]["status"],
                "overall_check": comparison["overall_check"]["status"],
                "distances": _distances(comparison),
            }
        )
    status_rank = {"clear": 2, "review": 1, "match": 0}
    results.sort(
        key=lambda item: (
            status_rank.get(str(item.get("overall_check")), 0),
            _distance_score(item.get("distances", {})),
        ),
        reverse=True,
    )
    return results


def _store_decision_margin_results(report: dict[str, Any], output_root: str | Path, output_dir: Path) -> dict[str, Any]:
    db_file = _decision_margin_db_path(output_root, report["listing_code"])
    db = _load_decision_margin_db(output_root, report["listing_code"])
    before_transition_count = len(db["transition_points"])
    before_combined_count = len(db["combined_photo_adjustments"])
    now = datetime.now().isoformat(timespec="seconds")

    for item in report["decision_margin_analysis"]["family_results"]:
        if not item.get("transition_found"):
            continue
        record_id = item.get("decision_margin_id") or _stable_id({"kind": "decision_margin_result", "item": item})
        db["transition_points"][record_id] = {
            "decision_margin_id": record_id,
            "listing_code": report["listing_code"],
            "mode": _mode(report["listing_code"]),
            "preset": report["preset"],
            "sensitivity": report["sensitivity"],
            "seed": report["seed"],
            "run_at": report["run_at"],
            "config_hash": report["config_hash"],
            "source_image": item["source_image"],
            "photo_adjustment_family": item["photo_adjustment_family"],
            "parameter_name": item["parameter_name"],
            "transition_value": item["transition_value"],
            "decision_before_transition": item["decision_before_transition"],
            "decision_after_transition": item["decision_after_transition"],
            "before_transition": item["before_transition"],
            "after_transition": item["after_transition"],
            "tests_performed": item["tests_performed"],
            "last_seen_report": str(output_dir),
            "last_seen_at": now,
        }

    for item in report["decision_margin_analysis"]["combined_photo_adjustments"]:
        record_id = item["test_id"]
        db["combined_photo_adjustments"][record_id] = {
            "test_id": record_id,
            "listing_code": report["listing_code"],
            "mode": _mode(report["listing_code"]),
            "preset": report["preset"],
            "sensitivity": report["sensitivity"],
            "seed": report["seed"],
            "run_at": report["run_at"],
            "config_hash": report["config_hash"],
            "source_image": item["source_image"],
            "combined_photo_adjustment": item["combined_photo_adjustment"],
            "params": item["params"],
            "adjustment_path": item["photo_adjustment"]["path"],
            "visual_status": item["visual_status"],
            "overall_check": item["overall_check"],
            "distances": item["distances"],
            "last_seen_report": str(output_dir),
            "last_seen_at": now,
        }

    db["updated_at"] = now
    _write_json(db_file, db)
    return {
        "catalog_file": str(db_file),
        "transition_points_observed_this_run": len([item for item in report["decision_margin_analysis"]["family_results"] if item.get("transition_found")]),
        "transition_points_total": len(db["transition_points"]),
        "new_transition_points_added": len(db["transition_points"]) - before_transition_count,
        "combined_adjustments_observed_this_run": len(report["decision_margin_analysis"]["combined_photo_adjustments"]),
        "combined_adjustments_total": len(db["combined_photo_adjustments"]),
        "new_combined_adjustments_added": len(db["combined_photo_adjustments"]) - before_combined_count,
    }


def _write_decision_margin_zip(report: dict[str, Any], output_dir: Path, catalog: dict[str, Any]) -> Path:
    zip_path = output_dir / "decision_margin_quality_bundle.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (
            "decision_margin_report.json",
            "decision_margin_report.md",
            "decision_margin_catalog_summary.json",
        ):
            path = output_dir / name
            if path.exists():
                archive.write(path, path.name)
        db_file = Path(catalog.get("catalog_file", ""))
        if db_file.exists():
            archive.write(db_file, Path("catalog") / db_file.name)
        images_dir = output_dir / "decision_margin_images"
        if images_dir.exists():
            for path in images_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, images_dir.name / path.relative_to(images_dir))
    return zip_path


def _transition_points(family_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in family_results if item.get("transition_found")]


def _group_transition_summary(family_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_family: dict[str, list[dict[str, Any]]] = {}
    for item in family_results:
        by_family.setdefault(item["photo_adjustment_family"], []).append(item)
    summary = []
    for family, rows in sorted(by_family.items()):
        found = [row for row in rows if row.get("transition_found")]
        values = [row["transition_value"] for row in found if row.get("transition_value") is not None]
        summary.append(
            {
                "photo_adjustment_family": family,
                "transition_points_found": len(found),
                "tests_performed": sum(int(row.get("tests_performed", 0)) for row in rows),
                "estimated_transition_values": values[:10],
                "cases_to_examine": [row for row in found[:5]],
            }
        )
    return summary


def markdown_decision_margin_report(report: dict[str, Any]) -> str:
    analysis = report["decision_margin_analysis"]
    catalog = report.get("decision_margin_catalog", {})
    lines = [
        "# Analyse des marges de decision photo",
        "",
        "## Contexte",
        f"- controle qualite catalogue: `{report['listing_code']}`",
        f"- dossier annonce: `{report['listing_dir']}`",
        f"- preset: `{report['preset']}`",
        f"- sensibilite: `{report['sensitivity']}`",
        f"- seed: `{report['seed']}`",
        f"- hash configuration: `{report['config_hash']}`",
        f"- images sources analysees: {report['summary']['source_images_used']}",
        f"- familles d'ajustement photo: {report['summary']['photo_adjustment_families']}",
        f"- tests progressifs effectues: {report['summary']['progressive_tests_performed']}",
        f"- combinaisons legeres testees: {report['summary']['combined_photo_adjustments_tested']}",
        "",
        "Cette section mesure la sensibilite aux ajustements photo pour la comparaison de visuels produit. Elle ne modifie pas les seuils metier et sert uniquement a documenter la marge de decision du controle qualite catalogue.",
        "",
        "## Resume par famille d'ajustement",
        "",
        "| Famille | Transitions observees | Tests effectues | Valeurs de transition estimees |",
        "| --- | ---: | ---: | --- |",
    ]
    for item in analysis["visual_margin_summary"]:
        values = ", ".join(str(value) for value in item.get("estimated_transition_values", [])) or "-"
        lines.append(
            f"| `{item['photo_adjustment_family']}` | {item['transition_points_found']} | {item['tests_performed']} | {values} |"
        )

    lines.extend(
        [
            "",
            "## Cas les plus proches d'une zone de transition",
            "",
            "| Famille | Image source | Parametre | Decision avant | Decision apres | Distances avant | Distances apres | Image generee apres |",
            "| --- | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    transition_rows = sorted(
        _transition_points(analysis["family_results"]),
        key=lambda item: (item["photo_adjustment_family"], str(item.get("source_image"))),
    )
    if not transition_rows:
        lines.append("| - | - | - | - | - | - | - | - |")
    for item in transition_rows[:30]:
        before = item.get("before_transition") or {}
        after = item.get("after_transition") or {}
        lines.append(
            f"| `{item['photo_adjustment_family']}` | `{item['source_image']}` | {item.get('transition_value', '-')} | "
            f"{before.get('overall_check', '-')} | {after.get('overall_check', '-')} | "
            f"`{before.get('distances', {})}` | `{after.get('distances', {})}` | `{after.get('adjustment_path', '-')}` |"
        )

    lines.extend(
        [
            "",
            "## Combinaisons legeres les plus sensibles",
            "",
            "| Ajustements combines | Image source | Statut | Distances | Parametres | Image generee |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in analysis["combined_photo_adjustments"][:30]:
        lines.append(
            f"| `{item['combined_photo_adjustment']}` | `{item['source_image']}` | {item['overall_check']} | "
            f"`{item['distances']}` | `{item['params']}` | `{item['photo_adjustment']['path']}` |"
        )
    if not analysis["combined_photo_adjustments"]:
        lines.append("| - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Points de depart priorises",
            "",
            "Les cas ci-dessous proviennent des resultats deja produits par le controle qualite photo et servent a prioriser les zones a examiner.",
            "",
            "| Image source | Ajustement | Statut | Distances | Rapport source |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in report.get("review_margin_candidates", [])[:20]:
        lines.append(
            f"| `{item.get('source_image', '-')}` | `{item.get('adjustment_name', '-')}` | {item.get('visual_status', '-')} | "
            f"`{item.get('distances', {})}` | `{item.get('first_seen_report', '-')}` |"
        )
    if not report.get("review_margin_candidates"):
        lines.append("| - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Stockage controle qualite",
            f"- catalogue des points de transition: `{catalog.get('catalog_file', '-')}`",
            f"- points de transition observes sur ce run: {catalog.get('transition_points_observed_this_run', 0)}",
            f"- points de transition total: {catalog.get('transition_points_total', 0)}",
            f"- ajustements combines observes sur ce run: {catalog.get('combined_adjustments_observed_this_run', 0)}",
            f"- ZIP debug: `{report.get('debug_zip', '-')}`",
            "",
            "## Lecture non technique",
            "Une zone de transition indique l'endroit approximatif ou la decision change quand une variation photo realiste augmente progressivement. Une valeur elevee signifie que la comparaison de visuels produit garde davantage de marge pour cette famille d'ajustement. Une valeur basse signale un cas a examiner dans le cadre du controle qualite catalogue.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_decision_margin_search(
    listing_code: str,
    annonces_root: str | Path = "annonces",
    output_root: str | Path = "local/debug_catalog_photo_control",
    sensitivity: str = "standard",
    seed: int = 12345,
    max_combinations: int = 24,
    max_candidates: int = 4,
    max_iterations: int = 6,
) -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    if not images:
        raise FileNotFoundError(f"Aucune image trouvee dans le dossier annonce: {listing_dir}")

    run_at = datetime.now().isoformat(timespec="seconds")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(output_root) / _safe_listing_code(listing_code) / f"{timestamp}_decision_margin"
    images_output_dir = output_dir / "decision_margin_images"
    output_dir.mkdir(parents=True, exist_ok=True)

    families = _decision_margin_families()
    candidates = _review_margin_candidates(output_root, listing_code, max_candidates)
    source_images = _candidate_source_images(images, candidates, max(1, max_candidates))
    config = {
        "families": families,
        "seed": seed,
        "max_combinations": max_combinations,
        "max_candidates": max_candidates,
        "max_iterations": max_iterations,
        "sensitivity": sensitivity,
    }
    cfg_hash = _config_hash(config)

    family_results: list[dict[str, Any]] = []
    for source_image in source_images:
        for family in families:
            family_results.append(
                _find_transition_for_family(
                    source_image,
                    family,
                    candidates,
                    images_output_dir,
                    listing_code,
                    sensitivity,
                    seed,
                    max_iterations,
                )
            )

    combined_photo_adjustments = _run_combined_photo_adjustments(
        listing_code,
        source_images,
        images_output_dir,
        sensitivity,
        seed,
        max_combinations,
    )

    progressive_tests = sum(int(item.get("tests_performed", 0)) for item in family_results)
    transition_count = len([item for item in family_results if item.get("transition_found")])
    combined_counts = Counter(item["overall_check"] for item in combined_photo_adjustments)
    report = {
        "listing_code": listing_code,
        "listing_dir": str(listing_dir),
        "preset": DECISION_MARGIN_PRESET,
        "sensitivity": sensitivity,
        "seed": seed,
        "run_at": run_at,
        "config_hash": cfg_hash,
        "output_dir": str(output_dir),
        "reports": {
            "json": str(output_dir / "decision_margin_report.json"),
            "markdown": str(output_dir / "decision_margin_report.md"),
        },
        "summary": {
            "original_images": len(images),
            "source_images_used": len(source_images),
            "photo_adjustment_families": len(families),
            "progressive_tests_performed": progressive_tests,
            "transition_points_found": transition_count,
            "combined_photo_adjustments_tested": len(combined_photo_adjustments),
            "combined_photo_adjustment_counts": dict(combined_counts),
        },
        "review_margin_candidates": candidates,
        "decision_margin_analysis": {
            "family_results": family_results,
            "transition_points": _transition_points(family_results),
            "visual_margin_summary": _group_transition_summary(family_results),
            "combined_photo_adjustments": combined_photo_adjustments,
        },
    }

    catalog = _store_decision_margin_results(report, output_root, output_dir)
    report["decision_margin_catalog"] = catalog
    report["debug_zip"] = str(output_dir / "decision_margin_quality_bundle.zip")
    _write_json(output_dir / "decision_margin_catalog_summary.json", catalog)
    _write_json(output_dir / "decision_margin_report.json", report)
    (output_dir / "decision_margin_report.md").write_text(markdown_decision_margin_report(report), encoding="utf-8")
    debug_zip = _write_decision_margin_zip(report, output_dir, catalog)
    report["debug_zip"] = str(debug_zip)
    _write_json(output_dir / "decision_margin_report.json", report)
    return report
