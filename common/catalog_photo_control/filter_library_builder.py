from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sqlite3
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageChops, ImageStat

TARGET_LABELS = {"review", "review_candidate", "review_candidates", "suspect", "suspects"}

PARAM_RANGES: dict[str, tuple[float, float]] = {
    "angle": (-2.2, 2.2),
    "blur": (0.0, 0.55),
    "brightness": (0.94, 1.06),
    "contrast": (0.94, 1.06),
    "saturation": (0.94, 1.06),
    "sharpness": (0.90, 1.10),
    "warmth": (-0.035, 0.035),
    "crop": (0.0, 0.035),
    "quality": (74.0, 96.0),
    "zoom": (0.92, 1.08),
    "canvas_pad": (0.0, 0.06),
    "canvas_gray": (230.0, 255.0),
    "canvas_auto": (0.0, 1.0),
}

DEFAULT_PARAM_KEYS = [
    "angle",
    "blur",
    "crop",
    "quality",
    "zoom",
    "canvas_pad",
    "canvas_gray",
    "canvas_auto",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "warmth",
]


@dataclass
class Candidate:
    recipe_id: str
    labels: str
    matches: int
    suspect_matches: int
    review_matches: int
    review_candidate_matches: int
    max_score: float
    avg_score: float
    example_output_path: str
    params: dict[str, Any]
    signature: dict[str, Any] | None = None


@dataclass
class AcceptedFilter:
    library_filter_id: str
    rank: int
    recipe_id: str
    labels: str
    matches: int
    suspect_matches: int
    review_matches: int
    review_candidate_matches: int
    max_score: float
    avg_score: float
    example_output_path: str
    params: dict[str, Any]
    signature: dict[str, Any]
    min_param_distance: float | None
    min_image_distance: float | None
    closest_library_filter_id: str | None
    family_key: str


@dataclass
class RejectedFilter:
    rejected_id: str
    recipe_id: str
    labels: str
    matches: int
    suspect_matches: int
    review_matches: int
    review_candidate_matches: int
    max_score: float
    avg_score: float
    example_output_path: str
    params: dict[str, Any]
    reject_reason: str
    closest_library_filter_id: str | None
    param_distance: float | None
    image_distance: float | None
    family_key: str


@dataclass
class PairDistance:
    filter_a: str
    filter_b: str
    param_distance: float
    image_distance: float
    combined_distance: float
    compatible: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_code(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_"}:
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "listing"


def stable_id(prefix: str, payload: Any, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:length]}"


def norm_label(row: dict[str, Any]) -> str | None:
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
                if v == "suspects":
                    return "suspect"
                if v == "review_candidates":
                    return "review_candidate"
                return v
    return None


def score_of(row: dict[str, Any]) -> float:
    for key in ("bench_score", "score", "max_score"):
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


def parse_params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def read_source_report(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    outputs = data.get("outputs", [])
    if not isinstance(outputs, list):
        raise RuntimeError("source report JSON does not contain outputs[]")
    return data, [row for row in outputs if isinstance(row, dict)]


def read_clean_csv(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outputs: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            params = parse_params(raw.get("params_json"))
            label = str(raw.get("labels") or raw.get("label") or "").strip()
            if not label:
                continue
            output_path = str(raw.get("example_output_path") or raw.get("example_path") or "")
            score = raw.get("max_score") or raw.get("score") or raw.get("avg_score") or "0"
            try:
                score_float = float(score)
            except Exception:
                score_float = 0.0
            outputs.append(
                {
                    "recipe_id": raw.get("recipe_id"),
                    "label": label,
                    "bench_score": score_float,
                    "output_path": output_path,
                    "params": params,
                }
            )
    return {"listing_code": safe_code(path.parent.parent.name), "source_csv": str(path)}, outputs


def build_candidates(rows: list[dict[str, Any]], min_original_score: float, candidate_limit: int) -> list[Candidate]:
    seen_outputs: set[str] = set()
    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        label = norm_label(row)
        if not label:
            # Clean CSV rows may have combined labels such as "review_candidate, suspect".
            raw_label = str(row.get("label") or "").lower()
            if "suspect" in raw_label:
                label = "suspect"
            elif "review" in raw_label:
                label = "review_candidate"
            else:
                continue

        recipe_id = str(row.get("recipe_id") or "").strip()
        if not recipe_id:
            continue

        output_path = str(row.get("output_path") or row.get("example_output_path") or row.get("example_path") or "").strip()
        output_id = str(row.get("output_id") or output_path or "").strip()
        if output_id and output_id in seen_outputs:
            continue
        if output_id:
            seen_outputs.add(output_id)

        score = score_of(row)
        params = parse_params(row.get("params")) or parse_params(row.get("params_json"))

        item = grouped.setdefault(
            recipe_id,
            {
                "recipe_id": recipe_id,
                "labels": set(),
                "matches": 0,
                "suspect_matches": 0,
                "review_matches": 0,
                "review_candidate_matches": 0,
                "score_sum": 0.0,
                "max_score": 0.0,
                "example_output_path": output_path,
                "params": params,
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
            item["example_output_path"] = output_path
            item["params"] = params

    candidates: list[Candidate] = []
    for item in grouped.values():
        matches = max(1, int(item["matches"]))
        max_score = float(item["max_score"])
        if max_score < min_original_score:
            # still skip early; rejected table focuses on strong-but-too-close filters.
            continue
        labels = ", ".join(sorted(str(label) for label in item["labels"]))
        candidates.append(
            Candidate(
                recipe_id=str(item["recipe_id"]),
                labels=labels,
                matches=int(item["matches"]),
                suspect_matches=int(item["suspect_matches"]),
                review_matches=int(item["review_matches"]),
                review_candidate_matches=int(item["review_candidate_matches"]),
                max_score=max_score,
                avg_score=float(item["score_sum"]) / matches,
                example_output_path=str(item["example_output_path"]),
                params=dict(item["params"]),
            )
        )

    candidates.sort(
        key=lambda c: (
            -c.suspect_matches,
            -c.max_score,
            -c.avg_score,
            -c.matches,
            c.recipe_id,
        )
    )
    if candidate_limit > 0:
        candidates = candidates[:candidate_limit]
    return candidates


def load_image(path: str) -> Image.Image | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        with Image.open(p) as img:
            return img.convert("RGB")
    except Exception:
        return None


def image_signature(path: str, thumb_size: int = 32) -> dict[str, Any] | None:
    img = load_image(path)
    if img is None:
        return None

    small = img.resize((thumb_size, thumb_size), Image.Resampling.LANCZOS)
    gray = small.convert("L")
    gray_arr = np.asarray(gray, dtype=np.float32) / 255.0

    rgb = np.asarray(small, dtype=np.float32) / 255.0
    means = rgb.reshape(-1, 3).mean(axis=0)
    stds = rgb.reshape(-1, 3).std(axis=0)

    # Reduced histogram, 8 bins per channel. Store as float list.
    hist_parts = []
    for channel in range(3):
        hist, _ = np.histogram(rgb[:, :, channel], bins=8, range=(0.0, 1.0), density=False)
        hist = hist.astype(np.float32)
        total = float(hist.sum()) or 1.0
        hist_parts.extend((hist / total).tolist())

    # Basic edge / detail proxy.
    gx = np.abs(np.diff(gray_arr, axis=1)).mean() if gray_arr.shape[1] > 1 else 0.0
    gy = np.abs(np.diff(gray_arr, axis=0)).mean() if gray_arr.shape[0] > 1 else 0.0

    return {
        "thumb_size": thumb_size,
        "gray_thumb": gray_arr.reshape(-1).round(5).tolist(),
        "rgb_mean": means.round(5).tolist(),
        "rgb_std": stds.round(5).tolist(),
        "rgb_hist8": [round(float(v), 6) for v in hist_parts],
        "edge_mean": round(float((gx + gy) / 2.0), 6),
    }


def signature_distance(sig_a: dict[str, Any] | None, sig_b: dict[str, Any] | None) -> float:
    if not sig_a or not sig_b:
        return 1.0

    def arr(key: str) -> np.ndarray:
        return np.asarray(sig_a.get(key, []), dtype=np.float32), np.asarray(sig_b.get(key, []), dtype=np.float32)

    a, b = arr("gray_thumb")
    thumb = float(np.sqrt(np.mean((a - b) ** 2))) if a.size and b.size and a.shape == b.shape else 1.0

    a, b = arr("rgb_hist8")
    hist = float(np.sqrt(np.mean((a - b) ** 2))) if a.size and b.size and a.shape == b.shape else 1.0

    a, b = arr("rgb_mean")
    mean = float(np.sqrt(np.mean((a - b) ** 2))) if a.size and b.size and a.shape == b.shape else 1.0

    edge = abs(float(sig_a.get("edge_mean", 0.0)) - float(sig_b.get("edge_mean", 0.0)))

    # Weighted blend. Keep roughly 0..1.
    dist = 0.55 * thumb + 0.20 * hist + 0.20 * mean + 0.05 * edge
    return max(0.0, min(1.0, float(dist)))


def param_distance(params_a: dict[str, Any], params_b: dict[str, Any], keys: list[str]) -> float:
    vals = []
    for key in keys:
        if key not in PARAM_RANGES:
            continue
        lo, hi = PARAM_RANGES[key]
        span = max(1e-9, hi - lo)
        try:
            a = float(params_a.get(key, 0.0))
            b = float(params_b.get(key, 0.0))
        except Exception:
            continue
        vals.append(((a - b) / span) ** 2)
    if not vals:
        return 1.0
    return max(0.0, min(1.0, math.sqrt(sum(vals) / len(vals))))


def family_key(params: dict[str, Any]) -> str:
    angle = float(params.get("angle", 0.0))
    blur = float(params.get("blur", 0.0))
    crop = float(params.get("crop", 0.0))
    quality = float(params.get("quality", 90.0))
    zoom = float(params.get("zoom", 1.0))
    pad = float(params.get("canvas_pad", 0.0))
    gray = float(params.get("canvas_gray", 255.0))
    auto = int(round(float(params.get("canvas_auto", 0.0))))

    angle_bucket = "pos" if angle >= 0 else "neg"
    blur_bucket = "blur_hi" if blur >= 0.20 else "blur_lo"
    crop_bucket = "crop_hi" if crop >= 0.012 else "crop_lo"
    quality_bucket = "q_hi" if quality >= 90 else "q_lo"
    zoom_bucket = "zoom_hi" if zoom >= 1.0 else "zoom_lo"
    pad_bucket = "pad_hi" if pad >= 0.012 else "pad_lo"
    gray_bucket = "white" if gray >= 245 else "gray"
    auto_bucket = f"auto{auto}"
    return ":".join([angle_bucket, blur_bucket, crop_bucket, quality_bucket, zoom_bucket, pad_bucket, gray_bucket, auto_bucket])


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS filter_library_runs(
            run_id TEXT PRIMARY KEY,
            listing_code TEXT NOT NULL,
            source_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            min_original_score REAL NOT NULL,
            min_param_distance REAL NOT NULL,
            min_image_distance REAL NOT NULL,
            max_per_family INTEGER NOT NULL,
            accepted_count INTEGER NOT NULL,
            rejected_count INTEGER NOT NULL,
            output_dir TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS accepted_filters(
            library_filter_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            source_recipe_id TEXT NOT NULL,
            labels TEXT NOT NULL,
            matches INTEGER NOT NULL,
            suspect_matches INTEGER NOT NULL,
            review_matches INTEGER NOT NULL,
            review_candidate_matches INTEGER NOT NULL,
            max_score REAL NOT NULL,
            avg_score REAL NOT NULL,
            example_output_path TEXT NOT NULL,
            params_json TEXT NOT NULL,
            signature_json TEXT NOT NULL,
            min_param_distance REAL,
            min_image_distance REAL,
            closest_library_filter_id TEXT,
            family_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rejected_filters(
            rejected_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source_recipe_id TEXT NOT NULL,
            labels TEXT NOT NULL,
            matches INTEGER NOT NULL,
            suspect_matches INTEGER NOT NULL,
            review_matches INTEGER NOT NULL,
            review_candidate_matches INTEGER NOT NULL,
            max_score REAL NOT NULL,
            avg_score REAL NOT NULL,
            example_output_path TEXT NOT NULL,
            params_json TEXT NOT NULL,
            reject_reason TEXT NOT NULL,
            closest_library_filter_id TEXT,
            param_distance REAL,
            image_distance REAL,
            family_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS filter_pair_distances(
            run_id TEXT NOT NULL,
            filter_a TEXT NOT NULL,
            filter_b TEXT NOT NULL,
            param_distance REAL NOT NULL,
            image_distance REAL NOT NULL,
            combined_distance REAL NOT NULL,
            compatible INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, filter_a, filter_b)
        );
        """
    )
    conn.commit()


def reset_run_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM filter_pair_distances;
        DELETE FROM rejected_filters;
        DELETE FROM accepted_filters;
        DELETE FROM filter_library_runs;
        """
    )
    conn.commit()


def insert_results(
    conn: sqlite3.Connection,
    run_id: str,
    listing_code: str,
    source_path: Path,
    output_dir: Path,
    target_count: int,
    min_original_score: float,
    min_param_distance: float,
    min_image_distance: float,
    max_per_family: int,
    accepted: list[AcceptedFilter],
    rejected: list[RejectedFilter],
    pairs: list[PairDistance],
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR REPLACE INTO filter_library_runs(
            run_id, listing_code, source_path, created_at, target_count,
            min_original_score, min_param_distance, min_image_distance,
            max_per_family, accepted_count, rejected_count, output_dir
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            listing_code,
            str(source_path),
            now,
            target_count,
            min_original_score,
            min_param_distance,
            min_image_distance,
            max_per_family,
            len(accepted),
            len(rejected),
            str(output_dir),
        ),
    )

    for item in accepted:
        conn.execute(
            """
            INSERT OR REPLACE INTO accepted_filters(
                library_filter_id, run_id, rank, source_recipe_id, labels, matches,
                suspect_matches, review_matches, review_candidate_matches,
                max_score, avg_score, example_output_path, params_json, signature_json,
                min_param_distance, min_image_distance, closest_library_filter_id, family_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.library_filter_id,
                run_id,
                item.rank,
                item.recipe_id,
                item.labels,
                item.matches,
                item.suspect_matches,
                item.review_matches,
                item.review_candidate_matches,
                item.max_score,
                item.avg_score,
                item.example_output_path,
                json.dumps(item.params, ensure_ascii=False, sort_keys=True),
                json.dumps(item.signature, ensure_ascii=False, sort_keys=True),
                item.min_param_distance,
                item.min_image_distance,
                item.closest_library_filter_id,
                item.family_key,
                now,
            ),
        )

    for item in rejected:
        conn.execute(
            """
            INSERT OR REPLACE INTO rejected_filters(
                rejected_id, run_id, source_recipe_id, labels, matches,
                suspect_matches, review_matches, review_candidate_matches,
                max_score, avg_score, example_output_path, params_json,
                reject_reason, closest_library_filter_id, param_distance, image_distance, family_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.rejected_id,
                run_id,
                item.recipe_id,
                item.labels,
                item.matches,
                item.suspect_matches,
                item.review_matches,
                item.review_candidate_matches,
                item.max_score,
                item.avg_score,
                item.example_output_path,
                json.dumps(item.params, ensure_ascii=False, sort_keys=True),
                item.reject_reason,
                item.closest_library_filter_id,
                item.param_distance,
                item.image_distance,
                item.family_key,
                now,
            ),
        )

    for item in pairs:
        conn.execute(
            """
            INSERT OR REPLACE INTO filter_pair_distances(
                run_id, filter_a, filter_b, param_distance, image_distance, combined_distance, compatible, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item.filter_a,
                item.filter_b,
                item.param_distance,
                item.image_distance,
                item.combined_distance,
                1 if item.compatible else 0,
                now,
            ),
        )
    conn.commit()


def select_library(
    candidates: list[Candidate],
    target_count: int,
    min_param_distance: float,
    min_image_distance: float,
    max_per_family: int,
    param_keys: list[str],
) -> tuple[list[AcceptedFilter], list[RejectedFilter], list[PairDistance]]:
    accepted: list[AcceptedFilter] = []
    rejected: list[RejectedFilter] = []
    pairs: list[PairDistance] = []
    family_counts: dict[str, int] = {}

    for candidate in candidates:
        fam = family_key(candidate.params)
        signature = image_signature(candidate.example_output_path)

        if signature is None:
            rejected.append(
                RejectedFilter(
                    rejected_id=stable_id("reject", [candidate.recipe_id, "missing_signature"]),
                    recipe_id=candidate.recipe_id,
                    labels=candidate.labels,
                    matches=candidate.matches,
                    suspect_matches=candidate.suspect_matches,
                    review_matches=candidate.review_matches,
                    review_candidate_matches=candidate.review_candidate_matches,
                    max_score=candidate.max_score,
                    avg_score=candidate.avg_score,
                    example_output_path=candidate.example_output_path,
                    params=candidate.params,
                    reject_reason="missing_or_unreadable_image",
                    closest_library_filter_id=None,
                    param_distance=None,
                    image_distance=None,
                    family_key=fam,
                )
            )
            continue

        if max_per_family > 0 and family_counts.get(fam, 0) >= max_per_family:
            rejected.append(
                RejectedFilter(
                    rejected_id=stable_id("reject", [candidate.recipe_id, "max_per_family"]),
                    recipe_id=candidate.recipe_id,
                    labels=candidate.labels,
                    matches=candidate.matches,
                    suspect_matches=candidate.suspect_matches,
                    review_matches=candidate.review_matches,
                    review_candidate_matches=candidate.review_candidate_matches,
                    max_score=candidate.max_score,
                    avg_score=candidate.avg_score,
                    example_output_path=candidate.example_output_path,
                    params=candidate.params,
                    reject_reason="max_per_family",
                    closest_library_filter_id=None,
                    param_distance=None,
                    image_distance=None,
                    family_key=fam,
                )
            )
            continue

        closest_id = None
        closest_param = None
        closest_image = None
        closest_combined = None
        reject_reason = None

        for existing in accepted:
            pd = param_distance(candidate.params, existing.params, param_keys)
            idist = signature_distance(signature, existing.signature)
            combined = 0.45 * pd + 0.55 * idist
            pair = PairDistance(
                filter_a=stable_id("candidate", candidate.recipe_id),
                filter_b=existing.library_filter_id,
                param_distance=pd,
                image_distance=idist,
                combined_distance=combined,
                compatible=(pd >= min_param_distance and idist >= min_image_distance),
            )
            pairs.append(pair)
            if closest_combined is None or combined < closest_combined:
                closest_combined = combined
                closest_param = pd
                closest_image = idist
                closest_id = existing.library_filter_id
            if pd < min_param_distance:
                reject_reason = "too_similar_params"
                break
            if idist < min_image_distance:
                reject_reason = "too_similar_image"
                break

        if reject_reason:
            rejected.append(
                RejectedFilter(
                    rejected_id=stable_id("reject", [candidate.recipe_id, reject_reason, closest_id]),
                    recipe_id=candidate.recipe_id,
                    labels=candidate.labels,
                    matches=candidate.matches,
                    suspect_matches=candidate.suspect_matches,
                    review_matches=candidate.review_matches,
                    review_candidate_matches=candidate.review_candidate_matches,
                    max_score=candidate.max_score,
                    avg_score=candidate.avg_score,
                    example_output_path=candidate.example_output_path,
                    params=candidate.params,
                    reject_reason=reject_reason,
                    closest_library_filter_id=closest_id,
                    param_distance=closest_param,
                    image_distance=closest_image,
                    family_key=fam,
                )
            )
            continue

        rank = len(accepted) + 1
        library_filter_id = stable_id("lib", [candidate.recipe_id, candidate.params], length=20)
        accepted_item = AcceptedFilter(
            library_filter_id=library_filter_id,
            rank=rank,
            recipe_id=candidate.recipe_id,
            labels=candidate.labels,
            matches=candidate.matches,
            suspect_matches=candidate.suspect_matches,
            review_matches=candidate.review_matches,
            review_candidate_matches=candidate.review_candidate_matches,
            max_score=candidate.max_score,
            avg_score=candidate.avg_score,
            example_output_path=candidate.example_output_path,
            params=candidate.params,
            signature=signature,
            min_param_distance=closest_param,
            min_image_distance=closest_image,
            closest_library_filter_id=closest_id,
            family_key=fam,
        )
        accepted.append(accepted_item)
        family_counts[fam] = family_counts.get(fam, 0) + 1

        if len(accepted) >= target_count:
            break

    return accepted, rejected, pairs


def write_csvs(output_dir: Path, accepted: list[AcceptedFilter], rejected: list[RejectedFilter]) -> tuple[Path, Path]:
    accepted_csv = output_dir / "filter_library_accepted.csv"
    rejected_csv = output_dir / "filter_library_rejected.csv"

    with accepted_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "rank", "library_filter_id", "recipe_id", "labels", "matches", "suspect_matches",
            "review_matches", "review_candidate_matches", "max_score", "avg_score",
            "min_param_distance", "min_image_distance", "closest_library_filter_id",
            "family_key", "example_output_path", "params_json",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in accepted:
            writer.writerow(
                {
                    "rank": item.rank,
                    "library_filter_id": item.library_filter_id,
                    "recipe_id": item.recipe_id,
                    "labels": item.labels,
                    "matches": item.matches,
                    "suspect_matches": item.suspect_matches,
                    "review_matches": item.review_matches,
                    "review_candidate_matches": item.review_candidate_matches,
                    "max_score": round(item.max_score, 6),
                    "avg_score": round(item.avg_score, 6),
                    "min_param_distance": "" if item.min_param_distance is None else round(item.min_param_distance, 6),
                    "min_image_distance": "" if item.min_image_distance is None else round(item.min_image_distance, 6),
                    "closest_library_filter_id": item.closest_library_filter_id or "",
                    "family_key": item.family_key,
                    "example_output_path": item.example_output_path,
                    "params_json": json.dumps(item.params, ensure_ascii=False, sort_keys=True),
                }
            )

    with rejected_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "rejected_id", "recipe_id", "labels", "matches", "suspect_matches", "review_matches",
            "review_candidate_matches", "max_score", "avg_score", "reject_reason",
            "closest_library_filter_id", "param_distance", "image_distance", "family_key",
            "example_output_path", "params_json",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in rejected:
            writer.writerow(
                {
                    "rejected_id": item.rejected_id,
                    "recipe_id": item.recipe_id,
                    "labels": item.labels,
                    "matches": item.matches,
                    "suspect_matches": item.suspect_matches,
                    "review_matches": item.review_matches,
                    "review_candidate_matches": item.review_candidate_matches,
                    "max_score": round(item.max_score, 6),
                    "avg_score": round(item.avg_score, 6),
                    "reject_reason": item.reject_reason,
                    "closest_library_filter_id": item.closest_library_filter_id or "",
                    "param_distance": "" if item.param_distance is None else round(item.param_distance, 6),
                    "image_distance": "" if item.image_distance is None else round(item.image_distance, 6),
                    "family_key": item.family_key,
                    "example_output_path": item.example_output_path,
                    "params_json": json.dumps(item.params, ensure_ascii=False, sort_keys=True),
                }
            )
    return accepted_csv, rejected_csv


def file_uri(path: str | Path) -> str:
    return "file:///" + Path(path).as_posix()


def write_html(
    output_dir: Path,
    listing_code: str,
    source_path: Path,
    accepted: list[AcceptedFilter],
    rejected: list[RejectedFilter],
    pairs: list[PairDistance],
    accepted_csv: Path,
    rejected_csv: Path,
    db_path: Path,
) -> Path:
    html_path = output_dir / "filter_library_report.html"
    rejected_reason_counts: dict[str, int] = {}
    for item in rejected:
        rejected_reason_counts[item.reject_reason] = rejected_reason_counts.get(item.reject_reason, 0) + 1

    lines = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        f"<title>Filter library - {html.escape(listing_code)}</title>",
        "<style>",
        "body{font-family:Arial;margin:24px;background:#fafafa;color:#222}",
        "table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}",
        "td,th{border:1px solid #ddd;padding:7px;vertical-align:top}",
        "th{background:#f5f5f5;position:sticky;top:0}",
        "code{background:#f7f7f7;padding:2px 4px;white-space:pre-wrap}",
        "img{max-width:190px;max-height:190px;border:1px solid #ddd;background:white}",
        ".ok{background:#eefbf1}.suspect{background:#fff0f0}.reject{background:#fff8e8}",
        ".score{font-weight:bold;font-size:15px}.muted{color:#666}",
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}",
        ".card{background:#fff;border:1px solid #ddd;padding:10px;border-radius:8px}",
        "</style></head><body>",
        f"<h1>Filter library - {html.escape(listing_code)}</h1>",
        f"<p>Source: <code>{html.escape(str(source_path))}</code></p>",
        f"<p>Accepted: <strong>{len(accepted)}</strong> | Rejected shown/data: <strong>{len(rejected)}</strong> | Pair checks: <strong>{len(pairs)}</strong></p>",
        f"<p>DB: <code>{html.escape(str(db_path))}</code></p>",
        f"<p>CSV accepted: <a href='{html.escape(file_uri(accepted_csv))}'>open</a> | CSV rejected: <a href='{html.escape(file_uri(rejected_csv))}'>open</a></p>",
        "<h2>Rejected summary</h2><ul>",
    ]

    for reason, count in sorted(rejected_reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"<li><code>{html.escape(reason)}</code>: {count}</li>")
    lines.append("</ul>")

    lines.append("<h2>Accepted filters</h2><div class='grid'>")
    for item in accepted:
        cls = "suspect" if item.suspect_matches > 0 else "ok"
        img_uri = file_uri(item.example_output_path)
        params_json = html.escape(json.dumps(item.params, ensure_ascii=False, sort_keys=True))
        min_pd = "-" if item.min_param_distance is None else f"{item.min_param_distance:.4f}"
        min_id = "-" if item.min_image_distance is None else f"{item.min_image_distance:.4f}"
        lines.append(
            f"<div class='card {cls}'>"
            f"<h3>#{item.rank} - {html.escape(item.labels)}</h3>"
            f"<a href='{html.escape(img_uri)}'><img src='{html.escape(img_uri)}'></a>"
            f"<p class='score'>score={item.max_score:.4f}</p>"
            f"<p>matches={item.matches} | suspect={item.suspect_matches} | review={item.review_matches} | candidate={item.review_candidate_matches}</p>"
            f"<p>min_param_distance={min_pd}<br>min_image_distance={min_id}</p>"
            f"<p>family=<code>{html.escape(item.family_key)}</code></p>"
            f"<p>recipe=<code>{html.escape(item.recipe_id[:16])}</code></p>"
            f"<details><summary>params</summary><code>{params_json}</code></details>"
            "</div>"
        )
    lines.append("</div>")

    lines.append("<h2>Top rejected filters</h2>")
    lines.append("<table><thead><tr><th>#</th><th>Reason</th><th>Score</th><th>Distances</th><th>Closest</th><th>Example</th><th>Params</th></tr></thead><tbody>")
    for idx, item in enumerate(rejected[:200], start=1):
        img_uri = file_uri(item.example_output_path)
        params_json = html.escape(json.dumps(item.params, ensure_ascii=False, sort_keys=True))
        pd = "-" if item.param_distance is None else f"{item.param_distance:.4f}"
        idist = "-" if item.image_distance is None else f"{item.image_distance:.4f}"
        lines.append(
            "<tr class='reject'>"
            f"<td>{idx}</td>"
            f"<td><code>{html.escape(item.reject_reason)}</code><br>{html.escape(item.labels)}</td>"
            f"<td>{item.max_score:.4f}</td>"
            f"<td>param={pd}<br>image={idist}</td>"
            f"<td><code>{html.escape(item.closest_library_filter_id or '-')}</code></td>"
            f"<td><a href='{html.escape(img_uri)}'><img src='{html.escape(img_uri)}'></a></td>"
            f"<td><code>{params_json}</code></td>"
            "</tr>"
        )
    lines.append("</tbody></table>")
    lines.append("</body></html>")
    html_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return html_path


def build_library(
    source_path: Path,
    output_dir: Path | None = None,
    db_path: Path | None = None,
    target_count: int = 50,
    min_original_score: float = 0.48,
    min_param_distance: float = 0.18,
    min_image_distance: float = 0.12,
    max_per_family: int = 3,
    candidate_limit: int = 0,
    param_keys: list[str] | None = None,
    reset_library_db: bool = False,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    if source_path.suffix.lower() == ".csv":
        source_meta, rows = read_clean_csv(source_path)
    else:
        source_meta, rows = read_source_report(source_path)

    listing_code = safe_code(str(source_meta.get("listing_code") or source_path.parent.parent.name or "listing"))
    output_dir = output_dir or (source_path.parent / "filter_library")
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = db_path or (source_path.parent.parent.parent / "_filter_library" / f"{listing_code}_filter_library.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    keys = param_keys or list(DEFAULT_PARAM_KEYS)
    candidates = build_candidates(rows, min_original_score=min_original_score, candidate_limit=candidate_limit)
    accepted, rejected, pairs = select_library(
        candidates=candidates,
        target_count=target_count,
        min_param_distance=min_param_distance,
        min_image_distance=min_image_distance,
        max_per_family=max_per_family,
        param_keys=keys,
    )

    run_id = stable_id(
        "run",
        [str(source_path), target_count, min_original_score, min_param_distance, min_image_distance, max_per_family, utc_now()],
        length=20,
    )

    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)
        if reset_library_db:
            reset_run_tables(conn)
        insert_results(
            conn=conn,
            run_id=run_id,
            listing_code=listing_code,
            source_path=source_path,
            output_dir=output_dir,
            target_count=target_count,
            min_original_score=min_original_score,
            min_param_distance=min_param_distance,
            min_image_distance=min_image_distance,
            max_per_family=max_per_family,
            accepted=accepted,
            rejected=rejected,
            pairs=pairs,
        )
    finally:
        conn.close()

    accepted_csv, rejected_csv = write_csvs(output_dir, accepted, rejected)
    html_path = write_html(output_dir, listing_code, source_path, accepted, rejected, pairs, accepted_csv, rejected_csv, db_path)

    return {
        "run_id": run_id,
        "listing_code": listing_code,
        "source_path": str(source_path),
        "candidates": len(candidates),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "pair_checks": len(pairs),
        "target_count": target_count,
        "html": str(html_path),
        "accepted_csv": str(accepted_csv),
        "rejected_csv": str(rejected_csv),
        "db": str(db_path),
        "top_accepted": accepted[:20],
    }


def smoke_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out_dir = root / "rendered"
        out_dir.mkdir()
        outputs = []
        for i in range(18):
            img = Image.new("RGB", (96, 96), (240, 240, 240))
            # Draw a different rectangle/texture per candidate.
            for x in range(12 + i % 7, 70, 9):
                for y in range(10 + (i * 3) % 11, 80, 13):
                    color = (50 + i * 8 % 160, 80 + i * 11 % 140, 110 + i * 5 % 120)
                    for dx in range(5):
                        for dy in range(5):
                            if 0 <= x + dx < 96 and 0 <= y + dy < 96:
                                img.putpixel((x + dx, y + dy), color)
            p = out_dir / f"out_{i:02d}.jpg"
            img.save(p, quality=90)
            params = {
                "angle": -2.2 + (i % 9) * 0.55,
                "blur": 0.05 * (i % 6),
                "crop": 0.004 * (i % 8),
                "quality": 74 + (i % 8) * 3,
                "zoom": 0.92 + (i % 8) * 0.02,
                "canvas_pad": 0.006 * (i % 8),
                "canvas_gray": 230 + (i % 6) * 5,
                "canvas_auto": i % 2,
            }
            outputs.append(
                {
                    "output_id": f"out_{i}",
                    "recipe_id": f"recipe_{i}",
                    "output_path": str(p),
                    "label": "suspect" if i % 5 == 0 else "review_candidate",
                    "bench_score": 0.50 + i * 0.01,
                    "params": params,
                }
            )
        report = {"listing_code": "smoke/O18", "outputs": outputs}
        report_path = root / "client_render_sampler_report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        result = build_library(
            report_path,
            output_dir=root / "filter_library",
            db_path=root / "filter_library.sqlite3",
            target_count=6,
            min_original_score=0.48,
            min_param_distance=0.05,
            min_image_distance=0.04,
            max_per_family=3,
            reset_library_db=True,
        )
        if result["accepted"] < 3:
            raise RuntimeError(f"smoke accepted too few filters: {result['accepted']}")
        for key in ("html", "accepted_csv", "rejected_csv", "db"):
            if not Path(result[key]).exists():
                raise RuntimeError(f"smoke missing output: {key}")
        print(f"filter library smoke OK accepted={result['accepted']} rejected={result['rejected']}")


def parse_param_keys(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_PARAM_KEYS)
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construit une librairie de filtres diversifiés depuis un bench discovery.")
    parser.add_argument("--source-report", default=None, help="Chemin client_render_sampler_report.json")
    parser.add_argument("--source-clean-csv", default=None, help="Chemin target_filters_by_recipe_clean.csv ou compatible")
    parser.add_argument("--output-dir", default=None, help="Dossier de sortie HTML/CSV")
    parser.add_argument("--db-path", default=None, help="DB filter_library sqlite3")
    parser.add_argument("--target-count", type=int, default=50)
    parser.add_argument("--min-original-score", type=float, default=0.48)
    parser.add_argument("--min-param-distance", type=float, default=0.18)
    parser.add_argument("--min-image-distance", type=float, default=0.12)
    parser.add_argument("--max-per-family", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=0)
    parser.add_argument("--param-keys", default=None)
    parser.add_argument("--reset-library-db", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)

    if args.smoke_test:
        smoke_test()
        return 0

    if bool(args.source_report) == bool(args.source_clean_csv):
        raise SystemExit("Passe exactement un input: --source-report OU --source-clean-csv")

    source_path = Path(args.source_report or args.source_clean_csv)
    result = build_library(
        source_path=source_path,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        db_path=Path(args.db_path) if args.db_path else None,
        target_count=args.target_count,
        min_original_score=args.min_original_score,
        min_param_distance=args.min_param_distance,
        min_image_distance=args.min_image_distance,
        max_per_family=args.max_per_family,
        candidate_limit=args.candidate_limit,
        param_keys=parse_param_keys(args.param_keys),
        reset_library_db=args.reset_library_db,
    )

    print("FILTER LIBRARY")
    print(f"run_id: {result['run_id']}")
    print(f"listing_code: {result['listing_code']}")
    print(f"candidates: {result['candidates']}")
    print(f"accepted: {result['accepted']} / {result['target_count']}")
    print(f"rejected: {result['rejected']}")
    print(f"pair_checks: {result['pair_checks']}")
    print(f"html: file:///{Path(result['html']).as_posix()}")
    print(f"accepted_csv: file:///{Path(result['accepted_csv']).as_posix()}")
    print(f"rejected_csv: file:///{Path(result['rejected_csv']).as_posix()}")
    print(f"db: file:///{Path(result['db']).as_posix()}")
    print("top_accepted:")
    for item in result["top_accepted"][:20]:
        min_pd = "-" if item.min_param_distance is None else f"{item.min_param_distance:.4f}"
        min_id = "-" if item.min_image_distance is None else f"{item.min_image_distance:.4f}"
        print(
            f"  {item.rank:02d}. {item.labels} | score={item.max_score:.4f} | "
            f"param_d={min_pd} | image_d={min_id} | family={item.family_key} | recipe={item.recipe_id[:12]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
