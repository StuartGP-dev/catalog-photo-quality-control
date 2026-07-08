from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from .catalog_config import load_settings
from .catalog_db import (
    CatalogDb,
    canonical_json,
    init_schema,
    open_catalog_db,
    stable_id,
    upsert_sql,
    utc_now,
)


def _as_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except Exception:
        return None


def _as_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(value)
    except Exception:
        return 0


def _row_first(row: Any) -> Any:
    if row is None:
        return None
    return row[0]


def _get_annonce_id(db: CatalogDb, annonce_key: str) -> str:
    row = db.execute(
        f"SELECT annonce_id FROM annonces WHERE annonce_key={db.placeholder}",
        [annonce_key],
    ).fetchone()
    annonce_id = _row_first(row)
    if not annonce_id:
        raise RuntimeError(
            f"Annonce not found in shared DB: {annonce_key}. "
            "Run ingest_annonces --annonce-key first."
        )
    return str(annonce_id)


def _recipe_upsert(db: CatalogDb) -> str:
    return upsert_sql(
        db,
        table="filter_recipes",
        columns=["recipe_id", "params_json", "family_key", "created_at", "updated_at"],
        conflict_columns=["recipe_id"],
        update_columns=["params_json", "family_key", "updated_at"],
    )


def _candidate_upsert(db: CatalogDb) -> str:
    return upsert_sql(
        db,
        table="annonce_filter_candidates",
        columns=[
            "candidate_id",
            "annonce_id",
            "recipe_id",
            "run_id",
            "family_key",
            "labels",
            "matches",
            "suspect_matches",
            "review_matches",
            "review_candidate_matches",
            "original_delta_avg",
            "original_delta_min",
            "original_delta_max",
            "original_delta_std",
            "max_score",
            "avg_score",
            "score_json",
            "status",
            "selected_at",
            "created_at",
            "updated_at",
        ],
        conflict_columns=["annonce_id", "recipe_id"],
        update_columns=[
            "run_id",
            "family_key",
            "labels",
            "matches",
            "suspect_matches",
            "review_matches",
            "review_candidate_matches",
            "max_score",
            "avg_score",
            "score_json",
            "status",
            "updated_at",
        ],
    )


def _run_upsert(db: CatalogDb) -> str:
    return upsert_sql(
        db,
        table="annonce_filter_runs",
        columns=[
            "run_id",
            "annonce_id",
            "stage",
            "strategy",
            "source_report_path",
            "output_dir",
            "seed",
            "duration_seconds",
            "status",
            "started_at",
            "finished_at",
            "metadata_json",
        ],
        conflict_columns=["run_id"],
        update_columns=[
            "source_report_path",
            "output_dir",
            "duration_seconds",
            "status",
            "finished_at",
            "metadata_json",
        ],
    )


def import_diverse_json(
    *,
    annonce_key: str,
    diverse_json: Path,
    source_run_label: str | None = None,
    db_dsn: str | None = None,
    init_db: bool = True,
) -> dict[str, Any]:
    payload = json.loads(diverse_json.read_text(encoding="utf-8"))
    selected = payload.get("selected") or []
    if not isinstance(selected, list):
        raise RuntimeError(f"Invalid diverse json: selected is not a list: {diverse_json}")

    settings = load_settings(db_dsn=db_dsn)
    now = utc_now()
    source_reports = payload.get("source_reports") or []
    source_report_path = str(source_reports[-1]) if source_reports else str(diverse_json)
    output_dir = str(diverse_json.parent)

    with open_catalog_db(settings) as db:
        if init_db:
            init_schema(db)

        annonce_id = _get_annonce_id(db, annonce_key)
        run_id = stable_id("run", annonce_id, source_run_label or str(diverse_json.resolve()))
        db.execute(
            _run_upsert(db),
            [
                run_id,
                annonce_id,
                "diverse_import",
                "stage1_stage2_diverse",
                source_report_path,
                output_dir,
                None,
                None,
                "imported",
                now,
                now,
                canonical_json(
                    {
                        "source_run_label": source_run_label,
                        "diverse_json": str(diverse_json),
                        "candidate_count": payload.get("candidate_count"),
                        "selected_count": payload.get("selected_count"),
                        "rejected_counts": payload.get("rejected_counts"),
                        "selection_params": {
                            "min_score": payload.get("min_score"),
                            "min_distance": payload.get("min_distance"),
                            "min_param_distance": payload.get("min_param_distance"),
                            "min_image_distance": payload.get("min_image_distance"),
                            "max_per_family": payload.get("max_per_family"),
                            "max_filters": payload.get("max_filters"),
                        },
                    }
                ),
            ],
        )

        recipe_sql = _recipe_upsert(db)
        candidate_sql = _candidate_upsert(db)
        imported = 0
        scores: list[float] = []

        for item in selected:
            if not isinstance(item, dict):
                continue
            recipe_id = str(item.get("recipe_id") or "").strip()
            if not recipe_id:
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            if not params and isinstance(item.get("params_json"), str):
                try:
                    loaded = json.loads(item["params_json"])
                    if isinstance(loaded, dict):
                        params = loaded
                except Exception:
                    params = {}

            family_key = str(item.get("family_key") or "")
            candidate_id = stable_id("cand", annonce_id, recipe_id)
            max_score = _as_float(item.get("max_score"))
            avg_score = _as_float(item.get("avg_score"))
            if max_score is not None:
                scores.append(max_score)

            db.execute(
                recipe_sql,
                [
                    recipe_id,
                    canonical_json(params),
                    family_key,
                    now,
                    now,
                ],
            )
            db.execute(
                candidate_sql,
                [
                    candidate_id,
                    annonce_id,
                    recipe_id,
                    run_id,
                    family_key,
                    str(item.get("labels") or ""),
                    _as_int(item.get("matches")),
                    _as_int(item.get("suspect_matches")),
                    _as_int(item.get("review_matches")),
                    _as_int(item.get("review_candidate_matches")),
                    None,
                    None,
                    None,
                    None,
                    max_score,
                    avg_score,
                    canonical_json(item),
                    "available",
                    None,
                    now,
                    now,
                ],
            )
            imported += 1

    return {
        "annonce_key": annonce_key,
        "diverse_json": str(diverse_json),
        "imported_candidates": imported,
        "score_min": min(scores) if scores else None,
        "score_avg": statistics.mean(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "db": settings.db_dsn,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import selected diverse filters into the shared catalog DB.")
    parser.add_argument("--annonce-key", required=True, help="Annonce key already ingested in DB, e.g. bijoux/O18.")
    parser.add_argument("--diverse-json", required=True, help="Path to diverse_target_filters.json.")
    parser.add_argument("--source-run-label", default=None, help="Optional run label for DB metadata.")
    parser.add_argument("--db-dsn", default=None, help="Override CATALOG_DB_DSN for this run.")
    parser.add_argument("--no-init-db", action="store_true", help="Do not create schema before importing.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = import_diverse_json(
        annonce_key=args.annonce_key,
        diverse_json=Path(args.diverse_json),
        source_run_label=args.source_run_label,
        db_dsn=args.db_dsn,
        init_db=not args.no_init_db,
    )
    print("DIVERSE FILTERS IMPORTED")
    print(f"annonce_key: {result['annonce_key']}")
    print(f"imported_candidates: {result['imported_candidates']}")
    if result["score_min"] is not None:
        print(f"score_min: {float(result['score_min']):.6f}")
        print(f"score_avg: {float(result['score_avg']):.6f}")
        print(f"score_max: {float(result['score_max']):.6f}")
    print(f"diverse_json: {result['diverse_json']}")
    print(f"db: {result['db']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
