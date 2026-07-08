
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

TARGET_LABELS = {"review", "review_candidate", "review_candidates", "suspect", "suspects"}
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "angle": (-2.2, 2.2),
    "blur": (0.0, 0.55),
    "brightness": (0.96, 1.04),
    "canvas_auto": (0.0, 1.0),
    "canvas_gray": (224.0, 255.0),
    "canvas_pad": (0.0, 0.075),
    "contrast": (0.96, 1.04),
    "crop": (0.0, 0.05),
    "quality": (68.0, 98.0),
    "saturation": (0.96, 1.04),
    "sharpness": (0.96, 1.04),
    "warmth": (-0.04, 0.04),
    "zoom": (0.90, 1.10),
}
DEFAULT_PARAMS = [
    "angle", "blur", "crop", "quality", "zoom", "canvas_pad", "canvas_gray", "canvas_auto",
]


def _family_bucket(params: dict[str, Any], name: str) -> str:
    """Coarse buckets used to avoid merging opposite filter families.

    The goal is not to create a final label; it is to prevent centroid poisoning,
    e.g. +2 deg and -2 deg becoming an average angle close to 0.
    """
    def as_float(default: float = 0.0) -> float:
        try:
            return float(params.get(name, default))
        except Exception:
            return default

    if name == "angle":
        value = as_float(0.0)
        if value <= -0.55:
            return "angle_neg"
        if value >= 0.55:
            return "angle_pos"
        return "angle_mid"
    if name == "blur":
        return "blur_hi" if as_float(0.0) >= 0.275 else "blur_lo"
    if name == "crop":
        return "crop_hi" if as_float(0.0) >= 0.0175 else "crop_lo"
    if name == "quality":
        return "q_hi" if as_float(96.0) >= 86.0 else "q_lo"
    if name == "zoom":
        value = as_float(1.0)
        if value <= 0.985:
            return "zoom_lo"
        if value >= 1.015:
            return "zoom_hi"
        return "zoom_mid"
    if name == "canvas_pad":
        return "pad_hi" if as_float(0.0) >= 0.03 else "pad_lo"
    if name == "canvas_gray":
        return "white" if as_float(255.0) >= 250.0 else "gray"
    if name == "canvas_auto":
        return "auto1" if as_float(0.0) >= 0.5 else "auto0"
    return f"{name}_any"


def _family_key(params: dict[str, Any]) -> str:
    keys = [
        "angle",
        "blur",
        "crop",
        "quality",
        "zoom",
        "canvas_pad",
        "canvas_gray",
        "canvas_auto",
    ]
    return ":".join(_family_bucket(params, key) for key in keys)


def _family_compatible(candidate_key: str, cluster_key: str, mode: str) -> bool:
    if mode == "off":
        return True
    if mode == "strict":
        return candidate_key == cluster_key
    # loose mode keeps the sign/auto/fond constraints but allows neighbouring
    # blur/crop/zoom buckets to merge.
    ca = candidate_key.split(":")
    cb = cluster_key.split(":")
    if len(ca) != len(cb):
        return False
    important_indexes = (0, 6, 7)  # angle sign, gray/white, auto0/auto1
    return all(ca[i] == cb[i] for i in important_indexes)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_short(payload: str, n: int = 12) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:n]


def _norm_label(row: dict[str, Any]) -> str | None:
    values: list[Any] = []
    for key in ("status", "label", "verdict", "decision", "result"):
        values.append(row.get(key))
    bench = row.get("bench_evaluation")
    if isinstance(bench, dict):
        for key in ("status", "label", "verdict", "decision", "result"):
            values.append(bench.get(key))
    for value in values:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TARGET_LABELS:
                return "review_candidate" if lowered == "review_candidates" else ("suspect" if lowered == "suspects" else lowered)
    return None


def _score(row: dict[str, Any]) -> float:
    for key in ("bench_score", "score", "max_score"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except Exception:
                pass
    bench = row.get("bench_evaluation")
    if isinstance(bench, dict):
        for key in ("score", "bench_score"):
            value = bench.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except Exception:
                    pass
    return 0.0


def _load_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            return {}
    return {}


def _param_vector(params: dict[str, Any], names: list[str]) -> np.ndarray:
    values: list[float] = []
    for name in names:
        low, high = PARAM_RANGES.get(name, (0.0, 1.0))
        raw = params.get(name)
        try:
            value = float(raw)
        except Exception:
            value = (low + high) / 2.0
        if high <= low:
            values.append(0.0)
        else:
            values.append(max(0.0, min(1.0, (value - low) / (high - low))))
    return np.asarray(values, dtype=np.float32)


def _param_distance(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    return float(np.mean(np.abs(a - b)))


def _image_signature(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        small = rgb.resize((24, 24), Image.Resampling.LANCZOS)
        arr = np.asarray(small).astype(np.float32) / 255.0
        gray = np.mean(arr, axis=2)
        # 24x24 grayscale captures geometric/canvas differences.
        thumb = gray.reshape(-1)
        # Coarse color distribution captures background/canvas changes.
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


def _image_distance(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0:
        return 0.0
    n = min(a.size, b.size)
    return float(np.mean(np.abs(a[:n] - b[:n])))


@dataclass
class Candidate:
    recipe_id: str
    labels: set[str]
    matches: int
    suspect_matches: int
    review_matches: int
    review_candidate_matches: int
    max_score: float
    avg_score: float
    example_output_path: str
    params: dict[str, Any]
    family_key: str
    param_vector: np.ndarray
    image_signature: np.ndarray

    @property
    def labels_text(self) -> str:
        return ", ".join(sorted(self.labels))


@dataclass
class Cluster:
    cluster_id: str
    count: int
    param_centroid: np.ndarray
    image_centroid: np.ndarray
    params_mean: dict[str, float]
    params_min: dict[str, float]
    params_max: dict[str, float]
    family_key: str
    top_params: dict[str, Any]
    labels: set[str] = field(default_factory=set)
    top_recipe_id: str = ""
    top_score: float = 0.0
    avg_score: float = 0.0
    score_sum: float = 0.0
    suspect_matches: int = 0
    review_matches: int = 0
    review_candidate_matches: int = 0
    example_output_path: str = ""
    members: list[str] = field(default_factory=list)


def _candidate_rows_from_report(report_path: Path, min_score: float, param_names: list[str]) -> tuple[str, list[Candidate]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    listing_code = str(report.get("listing_code") or report_path.parent.parent.name or "listing")
    outputs = report.get("outputs", [])
    if not isinstance(outputs, list):
        raise RuntimeError("Le rapport JSON ne contient pas outputs[].")

    by_recipe: dict[str, dict[str, Any]] = {}
    seen_outputs: set[str] = set()

    for row in outputs:
        if not isinstance(row, dict):
            continue
        label = _norm_label(row)
        if not label:
            continue
        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            continue
        output_id = str(row.get("output_id") or "")
        output_path = str(row.get("output_path") or "")
        dedupe_key = output_id or output_path or f"{recipe_id}:{len(seen_outputs)}"
        if dedupe_key in seen_outputs:
            continue
        seen_outputs.add(dedupe_key)

        score = _score(row)
        params = _load_params(row.get("params") or row.get("params_json"))
        item = by_recipe.setdefault(
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
    for item in by_recipe.values():
        max_score = float(item["max_score"])
        if max_score < min_score:
            continue
        example_path = Path(str(item["example_output_path"]))
        if not example_path.exists():
            # Keep cluster builder conservative: skip rows without image signature.
            continue
        params = dict(item["params"])
        param_vec = _param_vector(params, param_names)
        try:
            image_sig = _image_signature(example_path)
        except Exception:
            continue
        matches = int(item["matches"])
        candidates.append(Candidate(
            recipe_id=str(item["recipe_id"]),
            labels=set(item["labels"]),
            matches=matches,
            suspect_matches=int(item["suspect_matches"]),
            review_matches=int(item["review_matches"]),
            review_candidate_matches=int(item["review_candidate_matches"]),
            max_score=max_score,
            avg_score=float(item["score_sum"]) / max(1, matches),
            example_output_path=str(item["example_output_path"]),
            params=params,
            family_key=_family_key(params),
            param_vector=param_vec,
            image_signature=image_sig,
        ))

    candidates.sort(key=lambda c: (-c.suspect_matches, -c.max_score, -c.matches, c.recipe_id))
    return listing_code.replace("/", "_"), candidates


def _numeric_params(params: dict[str, Any], names: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in names:
        try:
            out[name] = float(params.get(name, 0.0))
        except Exception:
            out[name] = 0.0
    return out


def _make_cluster(candidate: Candidate, index: int, param_names: list[str]) -> Cluster:
    numeric = _numeric_params(candidate.params, param_names)
    cluster_id = f"cluster_{index:04d}_{_hash_short(candidate.recipe_id)}"
    return Cluster(
        cluster_id=cluster_id,
        count=1,
        param_centroid=candidate.param_vector.copy(),
        image_centroid=candidate.image_signature.copy(),
        params_mean=dict(numeric),
        params_min=dict(numeric),
        params_max=dict(numeric),
        family_key=candidate.family_key,
        top_params=dict(candidate.params),
        labels=set(candidate.labels),
        top_recipe_id=candidate.recipe_id,
        top_score=candidate.max_score,
        avg_score=candidate.avg_score,
        score_sum=candidate.avg_score,
        suspect_matches=candidate.suspect_matches,
        review_matches=candidate.review_matches,
        review_candidate_matches=candidate.review_candidate_matches,
        example_output_path=candidate.example_output_path,
        members=[candidate.recipe_id],
    )


def _update_cluster(cluster: Cluster, candidate: Candidate, param_names: list[str]) -> None:
    old_count = cluster.count
    new_count = old_count + 1
    cluster.param_centroid = (cluster.param_centroid * old_count + candidate.param_vector) / new_count
    cluster.image_centroid = (cluster.image_centroid * old_count + candidate.image_signature) / new_count
    numeric = _numeric_params(candidate.params, param_names)
    for name, value in numeric.items():
        cluster.params_mean[name] = (cluster.params_mean.get(name, value) * old_count + value) / new_count
        cluster.params_min[name] = min(cluster.params_min.get(name, value), value)
        cluster.params_max[name] = max(cluster.params_max.get(name, value), value)
    cluster.count = new_count
    cluster.labels.update(candidate.labels)
    cluster.score_sum += candidate.avg_score
    cluster.avg_score = cluster.score_sum / max(1, cluster.count)
    cluster.suspect_matches += candidate.suspect_matches
    cluster.review_matches += candidate.review_matches
    cluster.review_candidate_matches += candidate.review_candidate_matches
    if candidate.max_score >= cluster.top_score:
        cluster.top_score = candidate.max_score
        cluster.top_recipe_id = candidate.recipe_id
        cluster.example_output_path = candidate.example_output_path
        cluster.top_params = dict(candidate.params)
    if len(cluster.members) < 100:
        cluster.members.append(candidate.recipe_id)


def build_clusters(
    source_report: Path,
    output_dir: Path | None = None,
    cluster_db: Path | None = None,
    min_score: float = 0.48,
    cluster_threshold: float = 0.115,
    param_weight: float = 0.35,
    image_weight: float = 0.65,
    max_candidates: int = 0,
    reset_cluster_db: bool = False,
    param_names: list[str] | None = None,
    family_key_mode: str = "strict",
) -> dict[str, Any]:
    param_names = param_names or list(DEFAULT_PARAMS)
    family_key_mode = str(family_key_mode or "strict").strip().lower()
    if family_key_mode not in {"strict", "loose", "off"}:
        family_key_mode = "strict"
    listing_code, candidates = _candidate_rows_from_report(source_report, min_score=min_score, param_names=param_names)
    if max_candidates and max_candidates > 0:
        candidates = candidates[:max_candidates]

    clusters: list[Cluster] = []
    assignments: list[dict[str, Any]] = []

    for candidate in candidates:
        best: tuple[float, float, float, Cluster] | None = None
        for cluster in clusters:
            if not _family_compatible(candidate.family_key, cluster.family_key, family_key_mode):
                continue
            pd = _param_distance(candidate.param_vector, cluster.param_centroid)
            idist = _image_distance(candidate.image_signature, cluster.image_centroid)
            combined = param_weight * pd + image_weight * idist
            if best is None or combined < best[0]:
                best = (combined, pd, idist, cluster)
        if best is not None and best[0] <= cluster_threshold:
            _, pd, idist, cluster = best
            _update_cluster(cluster, candidate, param_names)
            assignments.append({
                "recipe_id": candidate.recipe_id,
                "cluster_id": cluster.cluster_id,
                "family_key": candidate.family_key,
                "param_distance": pd,
                "image_distance": idist,
                "combined_distance": best[0],
                "score": candidate.max_score,
            })
        else:
            cluster = _make_cluster(candidate, len(clusters) + 1, param_names)
            clusters.append(cluster)
            assignments.append({
                "recipe_id": candidate.recipe_id,
                "cluster_id": cluster.cluster_id,
                "family_key": candidate.family_key,
                "param_distance": None,
                "image_distance": None,
                "combined_distance": None,
                "score": candidate.max_score,
            })

    clusters.sort(key=lambda c: (-c.suspect_matches, -c.top_score, -c.count, c.cluster_id))

    out_dir = output_dir or (source_report.parent / "filter_clusters")
    out_dir.mkdir(parents=True, exist_ok=True)
    cluster_json = out_dir / "filter_clusters.json"
    cluster_csv = out_dir / "filter_clusters.csv"
    member_csv = out_dir / "filter_cluster_members.csv"
    html_path = out_dir / "filter_clusters_report.html"

    db_path = cluster_db or (source_report.parent.parent / "_filter_clusters" / f"{listing_code}_filter_clusters.sqlite3")
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset_cluster_db and db_path.exists():
        db_path.unlink()

    run_id = f"cluster_run_{_hash_short(str(source_report) + _now(), 20)}"

    cluster_payloads: list[dict[str, Any]] = []
    for rank, cluster in enumerate(clusters, start=1):
        cluster_payloads.append({
            "rank": rank,
            "cluster_id": cluster.cluster_id,
            "count": cluster.count,
            "labels": ", ".join(sorted(cluster.labels)),
            "top_recipe_id": cluster.top_recipe_id,
            "top_score": round(float(cluster.top_score), 6),
            "avg_score": round(float(cluster.avg_score), 6),
            "suspect_matches": cluster.suspect_matches,
            "review_matches": cluster.review_matches,
            "review_candidate_matches": cluster.review_candidate_matches,
            "example_output_path": cluster.example_output_path,
            "params_mean": {k: round(float(v), 6) for k, v in cluster.params_mean.items()},
            "params_min": {k: round(float(v), 6) for k, v in cluster.params_min.items()},
            "params_max": {k: round(float(v), 6) for k, v in cluster.params_max.items()},
            "family_key": cluster.family_key,
            "top_params": dict(cluster.top_params),
            "param_vector": [round(float(x), 6) for x in cluster.param_centroid.tolist()],
            "param_names": param_names,
            "members": cluster.members[:100],
        })

    payload = {
        "run_id": run_id,
        "created_at": _now(),
        "listing_code": listing_code,
        "source_report": str(source_report),
        "min_score": min_score,
        "cluster_threshold": cluster_threshold,
        "param_weight": param_weight,
        "image_weight": image_weight,
        "family_key_mode": family_key_mode,
        "param_names": param_names,
        "candidates": len(candidates),
        "clusters_count": len(clusters),
        "clusters": cluster_payloads,
    }
    cluster_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with cluster_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rank", "cluster_id", "family_key", "count", "labels", "top_recipe_id", "top_score", "avg_score",
            "suspect_matches", "review_matches", "review_candidate_matches", "example_output_path",
            "top_params_json", "params_mean_json", "params_min_json", "params_max_json",
        ])
        writer.writeheader()
        for row in cluster_payloads:
            writer.writerow({
                **{k: row[k] for k in ["rank", "cluster_id", "family_key", "count", "labels", "top_recipe_id", "top_score", "avg_score", "suspect_matches", "review_matches", "review_candidate_matches", "example_output_path"]},
                "top_params_json": json.dumps(row["top_params"], ensure_ascii=False, sort_keys=True),
                "params_mean_json": json.dumps(row["params_mean"], ensure_ascii=False, sort_keys=True),
                "params_min_json": json.dumps(row["params_min"], ensure_ascii=False, sort_keys=True),
                "params_max_json": json.dumps(row["params_max"], ensure_ascii=False, sort_keys=True),
            })

    with member_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["recipe_id", "cluster_id", "family_key", "param_distance", "image_distance", "combined_distance", "score"])
        writer.writeheader()
        for row in assignments:
            writer.writerow(row)

    _write_html(html_path, source_report, payload, cluster_payloads)
    _write_db(db_path, run_id, payload, cluster_payloads, assignments)

    return {
        "run_id": run_id,
        "listing_code": listing_code,
        "candidates": len(candidates),
        "clusters": len(clusters),
        "json": str(cluster_json),
        "csv": str(cluster_csv),
        "members_csv": str(member_csv),
        "html": str(html_path),
        "db": str(db_path),
        "top_clusters": cluster_payloads[:20],
    }


def _write_html(path: Path, source_report: Path, payload: dict[str, Any], clusters: list[dict[str, Any]]) -> None:
    lines = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        "<title>Filter clusters</title>",
        "<style>body{font-family:Arial;margin:24px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ddd;padding:6px;vertical-align:top}th{background:#f5f5f5;position:sticky;top:0}code{background:#f7f7f7;padding:2px 4px;white-space:pre-wrap}img{max-width:220px;max-height:220px;border:1px solid #ddd}.suspect{background:#fff1f1}.muted{color:#777}</style>",
        "</head><body>",
        f"<h1>Filter clusters - {html.escape(str(payload.get('listing_code')))}</h1>",
        f"<p>source_report: <code>{html.escape(str(source_report))}</code></p>",
        f"<p>candidates: <strong>{payload.get('candidates')}</strong> | clusters: <strong>{payload.get('clusters_count')}</strong> | threshold: <strong>{payload.get('cluster_threshold')}</strong> | family_key_mode: <strong>{payload.get('family_key_mode')}</strong></p>",
        "<table><thead><tr><th>#</th><th>Cluster</th><th>Scores</th><th>Example</th><th>Params mean</th><th>Range</th></tr></thead><tbody>",
    ]
    for row in clusters:
        uri = "file:///" + Path(str(row["example_output_path"])).as_posix()
        cls = "suspect" if int(row.get("suspect_matches", 0)) > 0 else ""
        lines.append(
            f"<tr class='{cls}'>"
            f"<td>{int(row['rank'])}</td>"
            f"<td><strong>{html.escape(str(row['cluster_id']))}</strong><br>family=<code>{html.escape(str(row.get('family_key', '')))}</code><br>count={int(row['count'])}<br>labels={html.escape(str(row['labels']))}<br>top recipe=<code>{html.escape(str(row['top_recipe_id'])[:12])}</code></td>"
            f"<td>top={float(row['top_score']):.4f}<br>avg={float(row['avg_score']):.4f}<br>suspect={int(row['suspect_matches'])}<br>review={int(row['review_matches'])}<br>review_candidate={int(row['review_candidate_matches'])}</td>"
            f"<td><a href='{html.escape(uri)}'><img src='{html.escape(uri)}'></a><br><a href='{html.escape(uri)}'>open</a></td>"
            f"<td><span class='muted'>top</span><br><code>{html.escape(json.dumps(row.get('top_params', {}), ensure_ascii=False, sort_keys=True))}</code><br><span class='muted'>mean</span><br><code>{html.escape(json.dumps(row['params_mean'], ensure_ascii=False, sort_keys=True))}</code></td>"
            f"<td><span class='muted'>min</span><br><code>{html.escape(json.dumps(row['params_min'], ensure_ascii=False, sort_keys=True))}</code><br><span class='muted'>max</span><br><code>{html.escape(json.dumps(row['params_max'], ensure_ascii=False, sort_keys=True))}</code></td>"
            "</tr>"
        )
    lines.append("</tbody></table></body></html>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_db(db_path: Path, run_id: str, payload: dict[str, Any], clusters: list[dict[str, Any]], assignments: list[dict[str, Any]]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS cluster_runs(
                run_id TEXT PRIMARY KEY,
                listing_code TEXT NOT NULL,
                source_report TEXT NOT NULL,
                created_at TEXT NOT NULL,
                min_score REAL NOT NULL,
                cluster_threshold REAL NOT NULL,
                param_weight REAL NOT NULL,
                image_weight REAL NOT NULL,
                candidates INTEGER NOT NULL,
                clusters_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clusters(
                cluster_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                family_key TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL,
                labels TEXT NOT NULL,
                top_recipe_id TEXT NOT NULL,
                top_score REAL NOT NULL,
                avg_score REAL NOT NULL,
                suspect_matches INTEGER NOT NULL,
                review_matches INTEGER NOT NULL,
                review_candidate_matches INTEGER NOT NULL,
                example_output_path TEXT NOT NULL,
                top_params_json TEXT NOT NULL DEFAULT '{}',
                params_mean_json TEXT NOT NULL,
                params_min_json TEXT NOT NULL,
                params_max_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cluster_members(
                run_id TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                family_key TEXT NOT NULL DEFAULT '',
                param_distance REAL,
                image_distance REAL,
                combined_distance REAL,
                score REAL NOT NULL,
                PRIMARY KEY(run_id, recipe_id)
            );
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO cluster_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, payload["listing_code"], payload["source_report"], payload["created_at"], payload["min_score"], payload["cluster_threshold"], payload["param_weight"], payload["image_weight"], payload["candidates"], payload["clusters_count"]),
        )
        for row in clusters:
            con.execute(
                "INSERT OR REPLACE INTO clusters VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["cluster_id"], run_id, row["rank"], row.get("family_key", ""), row["count"], row["labels"], row["top_recipe_id"], row["top_score"], row["avg_score"], row["suspect_matches"], row["review_matches"], row["review_candidate_matches"], row["example_output_path"], json.dumps(row.get("top_params", {}), sort_keys=True), json.dumps(row["params_mean"], sort_keys=True), json.dumps(row["params_min"], sort_keys=True), json.dumps(row["params_max"], sort_keys=True)),
            )
        for row in assignments:
            con.execute(
                "INSERT OR REPLACE INTO cluster_members VALUES(?,?,?,?,?,?,?,?)",
                (run_id, row["recipe_id"], row["cluster_id"], row.get("family_key", ""), row["param_distance"], row["image_distance"], row["combined_distance"], row["score"]),
            )
        con.commit()
    finally:
        con.close()


def _smoke_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outputs = []
        for i in range(10):
            img_path = root / f"out_{i}.jpg"
            image = Image.new("RGB", (96, 96), (240 - i * 3, 240 - i * 2, 240 - i))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20 + i, 20, 76, 76 - i), outline=(80, 80, 80), width=2)
            image.save(img_path, quality=95)
            params = {"angle": 1.7 + i * 0.05, "blur": 0.4 + i * 0.01, "crop": 0.03, "quality": 95, "zoom": 1.06, "canvas_pad": 0.04, "canvas_gray": 255, "canvas_auto": 1}
            outputs.append({"output_id": f"o{i}", "recipe_id": f"r{i}", "output_path": str(img_path), "params": params, "status": "review_candidate", "bench_score": 0.5 + i * 0.02})
        report = {"listing_code": "smoke/O1", "outputs": outputs}
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        result = build_clusters(report_path, output_dir=root / "clusters", cluster_db=root / "clusters.sqlite3", min_score=0.48, cluster_threshold=0.15, reset_cluster_db=True)
        if result["clusters"] < 1:
            raise RuntimeError("smoke cluster builder failed: no clusters")
        print(f"filter cluster smoke OK clusters={result['clusters']} candidates={result['candidates']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construit des clusters de filtres cibles avec moyennes incrementales.")
    parser.add_argument("--source-report", default=None, help="Chemin client_render_sampler_report.json du bench discovery.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--cluster-db", default=None)
    parser.add_argument("--min-score", type=float, default=0.48)
    parser.add_argument("--cluster-threshold", type=float, default=0.115)
    parser.add_argument("--param-weight", type=float, default=0.35)
    parser.add_argument("--image-weight", type=float, default=0.65)
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--param-names", default=",".join(DEFAULT_PARAMS))
    parser.add_argument("--reset-cluster-db", action="store_true")
    parser.add_argument("--family-key-mode", default="strict", choices=["strict", "loose", "off"], help="strict evite de fusionner les familles opposees avant de calculer les moyennes.")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)

    if args.smoke_test:
        return _smoke_test()
    if not args.source_report:
        raise SystemExit("--source-report est requis sauf avec --smoke-test")

    param_names = [part.strip() for part in str(args.param_names).split(",") if part.strip()]
    result = build_clusters(
        Path(args.source_report),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        cluster_db=Path(args.cluster_db) if args.cluster_db else None,
        min_score=args.min_score,
        cluster_threshold=args.cluster_threshold,
        param_weight=args.param_weight,
        image_weight=args.image_weight,
        max_candidates=args.max_candidates,
        reset_cluster_db=args.reset_cluster_db,
        param_names=param_names,
        family_key_mode=args.family_key_mode,
    )

    print("FILTER CLUSTERS")
    print(f"run_id: {result['run_id']}")
    print(f"listing_code: {result['listing_code']}")
    print(f"candidates: {result['candidates']}")
    print(f"clusters: {result['clusters']}")
    print(f"json: file:///{Path(result['json']).as_posix()}")
    print(f"csv: file:///{Path(result['csv']).as_posix()}")
    print(f"members_csv: file:///{Path(result['members_csv']).as_posix()}")
    print(f"html: file:///{Path(result['html']).as_posix()}")
    print(f"db: file:///{Path(result['db']).as_posix()}")
    print("top_clusters:")
    for row in result["top_clusters"][:15]:
        print(
            f"  {int(row['rank']):02d}. count={int(row['count'])} | labels={row['labels']} | "
            f"top_score={float(row['top_score']):.4f} | recipe={str(row['top_recipe_id'])[:12]} | cluster={row['cluster_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
