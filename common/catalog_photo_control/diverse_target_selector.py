from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


TARGET_LABELS = {"suspect", "suspects", "review", "review_candidate", "review_candidates"}

PARAM_RANGES: dict[str, tuple[float, float]] = {
    "angle": (-2.2, 2.2),
    "crop": (0.0, 0.035),
    "blur": (0.0, 0.55),
    "quality": (74.0, 96.0),
    "zoom": (0.92, 1.08),
    "canvas_pad": (0.0, 0.06),
    "canvas_gray": (230.0, 255.0),
    "canvas_auto": (0.0, 1.0),
}

PARAM_WEIGHTS: dict[str, float] = {
    "angle": 1.25,
    "crop": 1.10,
    "blur": 1.10,
    "quality": 0.95,
    "zoom": 1.10,
    "canvas_pad": 1.05,
    "canvas_gray": 0.70,
    "canvas_auto": 0.80,
}


def _as_float(params: dict[str, Any], name: str, default: float = 0.0) -> float:
    try:
        return float(params.get(name, default))
    except Exception:
        return default


def _family_bucket(params: dict[str, Any], name: str) -> str:
    if name == "angle":
        value = _as_float(params, name, 0.0)
        if value <= -0.55:
            return "angle_neg"
        if value >= 0.55:
            return "angle_pos"
        return "angle_mid"
    if name == "blur":
        return "blur_hi" if _as_float(params, name) >= 0.275 else "blur_lo"
    if name == "crop":
        return "crop_hi" if _as_float(params, name) >= 0.0175 else "crop_lo"
    if name == "quality":
        return "q_hi" if _as_float(params, name, 96.0) >= 86.0 else "q_lo"
    if name == "zoom":
        value = _as_float(params, name, 1.0)
        if value <= 0.985:
            return "zoom_lo"
        if value >= 1.015:
            return "zoom_hi"
        return "zoom_mid"
    if name == "canvas_pad":
        return "pad_hi" if _as_float(params, name) >= 0.03 else "pad_lo"
    if name == "canvas_gray":
        return "white" if _as_float(params, name, 255.0) >= 250 else "gray"
    if name == "canvas_auto":
        return "auto1" if _as_float(params, name) >= 0.5 else "auto0"
    return f"{name}_any"


def _family_key(params: dict[str, Any]) -> str:
    keys = ["angle", "blur", "crop", "quality", "zoom", "canvas_pad", "canvas_gray", "canvas_auto"]
    return ":".join(_family_bucket(params, key) for key in keys)


def _norm_label(row: dict[str, Any]) -> str | None:
    values: list[Any] = []
    for key in ("status", "label", "verdict", "decision", "result"):
        values.append(row.get(key))

    bench_eval = row.get("bench_evaluation")
    if isinstance(bench_eval, dict):
        for key in ("status", "label", "verdict", "decision", "result"):
            values.append(bench_eval.get(key))

    for value in values:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in TARGET_LABELS:
                if v == "review_candidates":
                    return "review_candidate"
                if v == "suspects":
                    return "suspect"
                return v
    return None


def _score(row: dict[str, Any]) -> float:
    for key in ("bench_score", "score"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except Exception:
                pass

    bench_eval = row.get("bench_evaluation")
    if isinstance(bench_eval, dict):
        for key in ("score", "bench_score"):
            value = bench_eval.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except Exception:
                    pass

    return 0.0


def _params(row: dict[str, Any]) -> dict[str, Any]:
    params = row.get("params")
    if isinstance(params, dict):
        return params

    params_json = row.get("params_json")
    if isinstance(params_json, str) and params_json.strip():
        try:
            loaded = json.loads(params_json)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass

    return {}


def _stable_filter_id(params: dict[str, Any], recipe_id: str = "") -> str:
    if params:
        payload = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        payload = recipe_id
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:14]
    return f"flt_{digest}"


def _vector(params: dict[str, Any], focus_params: list[str]) -> list[float]:
    out: list[float] = []
    for name in focus_params:
        low, high = PARAM_RANGES.get(name, (0.0, 1.0))
        raw = params.get(name, low)
        try:
            value = float(raw)
        except Exception:
            value = low

        if high <= low:
            norm = 0.0
        else:
            norm = (value - low) / (high - low)

        out.append(max(0.0, min(1.0, norm)))
    return out


def _param_distance(a: list[float], b: list[float], focus_params: list[str]) -> float:
    total_w = 0.0
    total = 0.0
    for idx, name in enumerate(focus_params):
        weight = PARAM_WEIGHTS.get(name, 1.0)
        delta = a[idx] - b[idx]
        total += weight * delta * delta
        total_w += weight
    if total_w <= 0:
        return 0.0
    return math.sqrt(total / total_w)


def _image_signature(path_str: str) -> np.ndarray | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            small = rgb.resize((24, 24), Image.Resampling.LANCZOS)
            arr = np.asarray(small).astype(np.float32) / 255.0
            gray = np.mean(arr, axis=2)
            thumb = gray.reshape(-1)
            hist_parts = []
            for channel in range(3):
                hist, _ = np.histogram(arr[:, :, channel], bins=8, range=(0.0, 1.0), density=True)
                hist = hist.astype(np.float32)
                hist = hist / max(1e-6, float(np.sum(hist)))
                hist_parts.append(hist)
            stats = np.asarray([
                float(np.mean(arr[:, :, 0])), float(np.mean(arr[:, :, 1])), float(np.mean(arr[:, :, 2])),
                float(np.std(gray)),
                float(np.mean(np.abs(np.diff(gray, axis=0)))) if gray.shape[0] > 1 else 0.0,
                float(np.mean(np.abs(np.diff(gray, axis=1)))) if gray.shape[1] > 1 else 0.0,
            ], dtype=np.float32)
            return np.concatenate([thumb, *hist_parts, stats]).astype(np.float32)
    except Exception:
        return None


def _image_distance(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or a.size == 0 or b.size == 0:
        # Missing signatures should not create a false collision; on the user's PC
        # the paths normally exist, so this mostly protects portable debug runs.
        return 999.0
    n = min(a.size, b.size)
    return float(np.mean(np.abs(a[:n] - b[:n])))


def _combined_distance(param_d: float, image_d: float, param_weight: float, image_weight: float) -> float:
    if image_d >= 999.0:
        return param_d
    total = max(1e-9, param_weight + image_weight)
    return (param_weight * param_d + image_weight * image_d) / total


def _load_candidates(
    reports: list[Path],
    min_score: float,
    labels: set[str],
    focus_params: list[str],
) -> list[dict[str, Any]]:
    by_recipe: dict[str, dict[str, Any]] = {}
    seen_outputs: set[str] = set()

    for report_path in reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        outputs = report.get("outputs", [])
        if not isinstance(outputs, list):
            continue

        for row in outputs:
            if not isinstance(row, dict):
                continue

            label = _norm_label(row)
            if not label or label not in labels:
                continue

            score = _score(row)
            if score < min_score:
                continue

            output_id = str(row.get("output_id") or "")
            output_path = str(row.get("output_path") or "")
            dedupe_key = output_id or output_path
            if not dedupe_key or dedupe_key in seen_outputs:
                continue
            seen_outputs.add(dedupe_key)

            recipe_id = str(row.get("recipe_id") or "")
            if not recipe_id:
                continue

            params = _params(row)
            filter_id = _stable_filter_id(params, recipe_id)

            item = by_recipe.setdefault(
                recipe_id,
                {
                    "filter_id": filter_id,
                    "recipe_id": recipe_id,
                    "labels": set(),
                    "matches": 0,
                    "suspect_matches": 0,
                    "review_matches": 0,
                    "review_candidate_matches": 0,
                    "max_score": 0.0,
                    "score_sum": 0.0,
                    "params": params,
                    "family_key": _family_key(params),
                    "example_output_path": output_path,
                    "source_report": str(report_path),
                },
            )

            item["labels"].add(label)
            item["matches"] += 1
            item["score_sum"] += score

            if label == "suspect":
                item["suspect_matches"] += 1
            elif label == "review":
                item["review_matches"] += 1
            else:
                item["review_candidate_matches"] += 1

            if score >= float(item["max_score"]):
                item["max_score"] = score
                item["params"] = params
                item["family_key"] = _family_key(params)
                item["filter_id"] = _stable_filter_id(params, recipe_id)
                item["example_output_path"] = output_path
                item["source_report"] = str(report_path)

    candidates: list[dict[str, Any]] = []
    for item in by_recipe.values():
        item["labels"] = ", ".join(sorted(item["labels"]))
        item["avg_score"] = float(item["score_sum"]) / max(1, int(item["matches"]))
        item["vector"] = _vector(item["params"], focus_params)
        item["image_signature"] = _image_signature(str(item.get("example_output_path") or ""))
        candidates.append(item)

    candidates.sort(
        key=lambda x: (
            -int(x["suspect_matches"]),
            -int(x["review_matches"]),
            -float(x["max_score"]),
            -int(x["matches"]),
            str(x["filter_id"]),
        )
    )
    return candidates


def select_diverse(
    reports: list[Path],
    output_dir: Path,
    min_score: float,
    min_distance: float,
    max_filters: int,
    labels: set[str],
    focus_params: list[str],
    min_param_distance: float = 0.0,
    min_image_distance: float = 0.0,
    param_weight: float = 0.55,
    image_weight: float = 0.45,
    max_per_family: int = 0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = _load_candidates(
        reports=reports,
        min_score=min_score,
        labels=labels,
        focus_params=focus_params,
    )

    selected: list[dict[str, Any]] = []
    family_counts: dict[str, int] = {}
    rejected_counts: dict[str, int] = {}

    for candidate in candidates:
        family_key = str(candidate.get("family_key") or "")
        if max_per_family > 0 and family_counts.get(family_key, 0) >= max_per_family:
            rejected_counts["max_per_family"] = rejected_counts.get("max_per_family", 0) + 1
            continue

        nearest_id = ""
        nearest_param = 999.0
        nearest_image = 999.0
        nearest_combined = 999.0

        for chosen in selected:
            param_d = _param_distance(candidate["vector"], chosen["vector"], focus_params)
            image_d = _image_distance(candidate.get("image_signature"), chosen.get("image_signature"))
            combined = _combined_distance(param_d, image_d, param_weight, image_weight)
            if combined < nearest_combined:
                nearest_combined = combined
                nearest_param = param_d
                nearest_image = image_d
                nearest_id = str(chosen["filter_id"])

        if selected and min_param_distance > 0 and nearest_param < min_param_distance:
            rejected_counts["too_similar_params"] = rejected_counts.get("too_similar_params", 0) + 1
            continue
        if selected and min_image_distance > 0 and nearest_image < min_image_distance:
            rejected_counts["too_similar_image"] = rejected_counts.get("too_similar_image", 0) + 1
            continue
        if selected and min_distance > 0 and nearest_combined < min_distance:
            rejected_counts["too_similar_combined"] = rejected_counts.get("too_similar_combined", 0) + 1
            continue

        row = dict(candidate)
        row["nearest_filter_id"] = nearest_id
        row["min_distance_to_selected"] = 999.0 if not selected else nearest_combined
        row["min_param_distance_to_selected"] = 999.0 if not selected else nearest_param
        row["min_image_distance_to_selected"] = 999.0 if not selected else nearest_image
        selected.append(row)
        family_counts[family_key] = family_counts.get(family_key, 0) + 1

        if max_filters > 0 and len(selected) >= max_filters:
            break

    csv_path = output_dir / "diverse_target_filters.csv"
    json_path = output_dir / "diverse_target_filters.json"
    html_path = output_dir / "diverse_target_filters.html"

    def export_item(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key not in {"vector", "image_signature"}}

    json_payload = {
        "source_reports": [str(p) for p in reports],
        "min_score": min_score,
        "min_distance": min_distance,
        "min_param_distance": min_param_distance,
        "min_image_distance": min_image_distance,
        "param_weight": param_weight,
        "image_weight": image_weight,
        "max_per_family": max_per_family,
        "max_filters": max_filters,
        "focus_params": focus_params,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "family_counts": family_counts,
        "rejected_counts": rejected_counts,
        "selected": [export_item(item) for item in selected],
    }

    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "rank",
            "filter_id",
            "recipe_id",
            "family_key",
            "labels",
            "matches",
            "suspect_matches",
            "review_matches",
            "review_candidate_matches",
            "max_score",
            "avg_score",
            "min_distance_to_selected",
            "min_param_distance_to_selected",
            "min_image_distance_to_selected",
            "nearest_filter_id",
            "example_output_path",
            "source_report",
            "params_json",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for rank, item in enumerate(selected, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "filter_id": item["filter_id"],
                    "recipe_id": item["recipe_id"],
                    "family_key": item.get("family_key", ""),
                    "labels": item["labels"],
                    "matches": item["matches"],
                    "suspect_matches": item["suspect_matches"],
                    "review_matches": item["review_matches"],
                    "review_candidate_matches": item["review_candidate_matches"],
                    "max_score": round(float(item["max_score"]), 6),
                    "avg_score": round(float(item["avg_score"]), 6),
                    "min_distance_to_selected": round(float(item["min_distance_to_selected"]), 6),
                    "min_param_distance_to_selected": round(float(item["min_param_distance_to_selected"]), 6),
                    "min_image_distance_to_selected": round(float(item["min_image_distance_to_selected"]), 6),
                    "nearest_filter_id": item["nearest_filter_id"],
                    "example_output_path": item["example_output_path"],
                    "source_report": item["source_report"],
                    "params_json": json.dumps(item["params"], ensure_ascii=False, sort_keys=True),
                }
            )

    lines = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        "<title>Diverse target filters</title>",
        "<style>",
        "body{font-family:Arial;margin:24px}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "td,th{border:1px solid #ddd;padding:7px;vertical-align:top}",
        "th{background:#f5f5f5;position:sticky;top:0}",
        "code{background:#f7f7f7;padding:2px 4px;white-space:pre-wrap}",
        "img{max-width:220px;max-height:220px;border:1px solid #ddd}",
        ".suspect{background:#fff1f1}",
        ".review{background:#fff8e8}",
        ".score{font-weight:bold}",
        "</style></head><body>",
        "<h1>Diverse target filters</h1>",
        f"<p>Candidates: <strong>{len(candidates)}</strong> | Selected: <strong>{len(selected)}</strong> | min_score={min_score} | combined={min_distance} | param={min_param_distance} | image={min_image_distance} | max_per_family={max_per_family}</p>",
        f"<p>Rejected: <code>{html.escape(json.dumps(rejected_counts, ensure_ascii=False, sort_keys=True))}</code></p>",
        "<table><thead><tr>",
        "<th>#</th><th>Filter ID</th><th>Family</th><th>Labels</th><th>Score</th><th>Distance</th><th>Example</th><th>Params</th>",
        "</tr></thead><tbody>",
    ]

    for rank, item in enumerate(selected, start=1):
        example = Path(str(item["example_output_path"]))
        uri = "file:///" + example.as_posix()
        cls = "suspect" if int(item["suspect_matches"]) > 0 else ("review" if int(item["review_matches"]) > 0 else "")
        params_json = html.escape(json.dumps(item["params"], ensure_ascii=False, sort_keys=True))
        dist_text = "seed" if rank == 1 else (
            f"combined={float(item['min_distance_to_selected']):.4f}<br>"
            f"param={float(item['min_param_distance_to_selected']):.4f}<br>"
            f"image={float(item['min_image_distance_to_selected']):.4f}"
        )

        lines.append(
            f"<tr class='{cls}'>"
            f"<td>{rank}</td>"
            f"<td><code>{html.escape(str(item['filter_id']))}</code><br><code>{html.escape(str(item['recipe_id'])[:12])}</code></td>"
            f"<td><code>{html.escape(str(item.get('family_key', '')))}</code></td>"
            f"<td><strong>{html.escape(str(item['labels']))}</strong><br>"
            f"suspect={int(item['suspect_matches'])}<br>"
            f"review={int(item['review_matches'])}<br>"
            f"review_candidate={int(item['review_candidate_matches'])}<br>"
            f"matches={int(item['matches'])}</td>"
            f"<td class='score'>{float(item['max_score']):.4f}<br>avg={float(item['avg_score']):.4f}</td>"
            f"<td>{dist_text}<br>nearest=<code>{html.escape(str(item['nearest_filter_id']))}</code></td>"
            f"<td><a href='{html.escape(uri)}'><img src='{html.escape(uri)}'></a><br><a href='{html.escape(uri)}'>open</a></td>"
            f"<td><code>{params_json}</code></td>"
            "</tr>"
        )

    lines.append("</tbody></table></body></html>")
    html_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "rejected_counts": rejected_counts,
        "csv": str(csv_path),
        "json": str(json_path),
        "html": str(html_path),
        "top": selected[:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Selectionne un maximum de filtres cibles distants.")
    parser.add_argument("--source-report", action="append", required=True, help="Chemin d'un client_render_sampler_report.json. Peut etre repete.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--min-score", type=float, default=0.48)
    parser.add_argument("--min-distance", type=float, default=0.16, help="Seuil combine param/image. 0 = desactive.")
    parser.add_argument("--min-param-distance", type=float, default=0.0)
    parser.add_argument("--min-image-distance", type=float, default=0.0)
    parser.add_argument("--param-weight", type=float, default=0.55)
    parser.add_argument("--image-weight", type=float, default=0.45)
    parser.add_argument("--max-per-family", type=int, default=0)
    parser.add_argument("--max-filters", type=int, default=0, help="0 = pas de limite")
    parser.add_argument("--labels", default="suspect,review,review_candidate,review_candidates,suspects")
    parser.add_argument("--focus-params", default="angle,crop,blur,quality,zoom,canvas_pad,canvas_gray,canvas_auto")
    args = parser.parse_args(argv)

    reports = [Path(p) for p in args.source_report]
    focus_params = [p.strip() for p in args.focus_params.split(",") if p.strip()]
    labels = {p.strip().lower() for p in args.labels.split(",") if p.strip()}

    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = reports[-1].parent / "diverse_target_filters"

    result = select_diverse(
        reports=reports,
        output_dir=out_dir,
        min_score=args.min_score,
        min_distance=args.min_distance,
        max_filters=args.max_filters,
        labels=labels,
        focus_params=focus_params,
        min_param_distance=args.min_param_distance,
        min_image_distance=args.min_image_distance,
        param_weight=args.param_weight,
        image_weight=args.image_weight,
        max_per_family=args.max_per_family,
    )

    print("DIVERSE TARGET FILTERS")
    print(f"candidates: {result['candidate_count']}")
    print(f"selected: {result['selected_count']}")
    print(f"rejected: {json.dumps(result['rejected_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"html: file:///{Path(result['html']).as_posix()}")
    print(f"csv: file:///{Path(result['csv']).as_posix()}")
    print(f"json: file:///{Path(result['json']).as_posix()}")

    for idx, item in enumerate(result["top"][:12], start=1):
        print(
            f"  {idx:02d}. {item['filter_id']} | {item['labels']} | "
            f"score={float(item['max_score']):.4f} | "
            f"dist={float(item['min_distance_to_selected']):.4f} | "
            f"param={float(item['min_param_distance_to_selected']):.4f} | "
            f"image={float(item['min_image_distance_to_selected']):.4f} | "
            f"family={item.get('family_key', '')} | "
            f"suspect={int(item['suspect_matches'])} | "
            f"review={int(item['review_matches'])} | "
            f"candidate={int(item['review_candidate_matches'])}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
