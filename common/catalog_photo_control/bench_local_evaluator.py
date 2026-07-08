from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

_STATUS_COLUMNS: dict[str, str] = {
    "status": "TEXT",
    "label": "TEXT",
    "verdict": "TEXT",
    "bench_score": "REAL",
    "bench_reasons_json": "TEXT",
    "bench_evaluator": "TEXT",
}


def ensure_bench_columns(conn: sqlite3.Connection) -> None:
    """Add optional bench-verdict columns to the existing outputs table.

    This is intentionally a light migration: it only adds columns if they are
    missing and does not modify existing rows or existing table semantics.
    """
    rows = conn.execute("PRAGMA table_info(outputs)").fetchall()
    existing = {str(row[1]) for row in rows}
    for name, column_type in _STATUS_COLUMNS.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE outputs ADD COLUMN "{name}" {column_type}')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outputs_status ON outputs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outputs_label ON outputs(label)")
    conn.commit()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_float(options: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(options.get(key, default))
    except Exception:
        return float(default)


def _parse_focus_params(raw: Any, space: Mapping[str, Any]) -> list[str]:
    if raw is None or raw == "":
        return list(space)
    if isinstance(raw, str):
        requested = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        requested = [str(part).strip() for part in raw if str(part).strip()]
    else:
        requested = []
    return [key for key in requested if key in space] or list(space)


def _span_score(params: Mapping[str, Any], space: Mapping[str, Any], focus_params: list[str]) -> tuple[float, list[str]]:
    scores: list[float] = []
    reasons: list[str] = []
    for key in focus_params:
        if key not in params or key not in space:
            continue
        try:
            low, high, mode = space[key]
            value = float(params[key])
            low_f, high_f, mode_f = float(low), float(high), float(mode)
            distance_to_mode = abs(value - mode_f)
            max_distance = max(abs(high_f - mode_f), abs(mode_f - low_f), 1e-9)
            score = max(0.0, min(1.0, distance_to_mode / max_distance))
        except Exception:
            continue
        scores.append(score)
        if score >= 0.70:
            reasons.append(f"{key}=edge:{score:.2f}")
        elif score >= 0.45:
            reasons.append(f"{key}=mid:{score:.2f}")
    if not scores:
        return 0.0, reasons
    # Highest values matter more than many tiny changes; this favors boundary
    # candidates while still rewarding combined parameter movement.
    ordered = sorted(scores, reverse=True)
    top = sum(ordered[: min(4, len(ordered))]) / min(4, len(ordered))
    avg = sum(scores) / len(scores)
    return round(0.70 * top + 0.30 * avg, 6), reasons


def _delta_score(delta: Mapping[str, Any], options: Mapping[str, Any]) -> tuple[float, list[str]]:
    # Conservative normalizers. They can be tuned from CLI without changing code.
    luma_norm = max(_as_float(options, "luma_norm", 18.0), 1e-9)
    contrast_norm = max(_as_float(options, "contrast_norm", 12.0), 1e-9)
    saturation_norm = max(_as_float(options, "saturation_norm", 18.0), 1e-9)
    detail_norm = max(_as_float(options, "detail_norm", 6.0), 1e-9)

    raw_parts = {
        "luma": abs(float(delta.get("luma", 0.0))) / luma_norm,
        "contrast": abs(float(delta.get("contrast", 0.0))) / contrast_norm,
        "saturation": abs(float(delta.get("saturation", 0.0))) / saturation_norm,
        "detail": abs(float(delta.get("detail", 0.0))) / detail_norm,
    }
    parts = {key: max(0.0, min(1.0, value)) for key, value in raw_parts.items()}
    score = (
        0.30 * parts["luma"]
        + 0.25 * parts["contrast"]
        + 0.25 * parts["saturation"]
        + 0.20 * parts["detail"]
    )
    reasons = [f"delta_{key}:{value:.2f}" for key, value in parts.items() if value >= 0.35]
    return round(score, 6), reasons


def evaluate_bench_output(
    *,
    params: Mapping[str, Any],
    delta: Mapping[str, Any],
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    space: Mapping[str, Any] | None = None,
    evaluator: str = "none",
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a generic bench verdict for one generated output.

    `local_delta` is a local bench heuristic. It does not call any external
    service and does not change generated files. It is only used to rank and
    archive candidates produced by the sampler.
    """
    evaluator_name = str(evaluator or "none").strip().lower()
    options = dict(options or {})
    if evaluator_name in {"", "none", "off", "false", "0"}:
        return {
            "evaluator": "none",
            "status": "",
            "label": "",
            "verdict": "",
            "score": 0.0,
            "reasons": [],
            "details": {},
        }
    if evaluator_name not in {"local_delta", "local"}:
        raise ValueError(f"Evaluateur bench inconnu: {evaluator!r}")

    space = dict(space or {})
    focus_params = _parse_focus_params(options.get("focus_params"), space)
    param_score, param_reasons = _span_score(params, space, focus_params)
    delta_score, delta_reasons = _delta_score(delta, options)
    param_weight = max(0.0, min(1.0, _as_float(options, "param_weight", 0.62)))
    score = round(param_weight * param_score + (1.0 - param_weight) * delta_score, 6)

    review_threshold = _as_float(options, "review_threshold", 0.50)
    suspect_threshold = _as_float(options, "suspect_threshold", 0.76)
    review_label = str(options.get("review_label", "review_candidate"))
    suspect_label = str(options.get("suspect_label", "suspect"))
    normal_label = str(options.get("normal_label", "normal"))
    include_normal = _as_bool(options.get("include_normal"), False)

    if score >= suspect_threshold:
        label = suspect_label
    elif score >= review_threshold:
        label = review_label
    else:
        label = normal_label if include_normal else ""

    reasons = param_reasons + delta_reasons
    return {
        "evaluator": "local_delta",
        "status": label,
        "label": label,
        "verdict": label,
        "score": score,
        "reasons": reasons,
        "details": {
            "param_score": param_score,
            "delta_score": delta_score,
            "param_weight": param_weight,
            "review_threshold": review_threshold,
            "suspect_threshold": suspect_threshold,
            "focus_params": focus_params,
        },
    }


def summarize_bench_evaluator(name: str, options: Mapping[str, Any] | None = None) -> str:
    name = str(name or "none")
    if name.lower() in {"", "none", "off", "false", "0"}:
        return "none"
    return f"{name} {json.dumps(dict(options or {}), ensure_ascii=False, sort_keys=True)}"
