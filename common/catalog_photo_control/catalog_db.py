from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .catalog_config import CatalogSettings, load_settings


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS annonces (
        annonce_id TEXT PRIMARY KEY,
        annonce_key TEXT NOT NULL UNIQUE,
        source_dir TEXT NOT NULL,
        image_count INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_images (
        image_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        image_index INTEGER NOT NULL,
        source_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(annonce_id, image_index),
        UNIQUE(annonce_id, sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filter_recipes (
        recipe_id TEXT PRIMARY KEY,
        params_json TEXT NOT NULL,
        family_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_runs (
        run_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        strategy TEXT NOT NULL,
        source_report_path TEXT,
        output_dir TEXT,
        seed INTEGER,
        duration_seconds REAL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        metadata_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_candidates (
        candidate_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        recipe_id TEXT NOT NULL,
        run_id TEXT,
        family_key TEXT,
        labels TEXT,
        matches INTEGER NOT NULL DEFAULT 0,
        suspect_matches INTEGER NOT NULL DEFAULT 0,
        review_matches INTEGER NOT NULL DEFAULT 0,
        review_candidate_matches INTEGER NOT NULL DEFAULT 0,
        original_delta_avg REAL,
        original_delta_min REAL,
        original_delta_max REAL,
        original_delta_std REAL,
        max_score REAL,
        avg_score REAL,
        score_json TEXT NOT NULL,
        status TEXT NOT NULL,
        selected_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(annonce_id, recipe_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_image_scores (
        image_score_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        image_id TEXT NOT NULL,
        output_path TEXT,
        original_delta_score REAL,
        label TEXT,
        signature_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(candidate_id, image_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_clusters (
        cluster_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        run_id TEXT,
        family_key TEXT,
        count INTEGER NOT NULL,
        top_candidate_id TEXT,
        top_score REAL,
        avg_score REAL,
        params_mean_json TEXT NOT NULL,
        params_min_json TEXT NOT NULL,
        params_max_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_distances (
        distance_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        candidate_a TEXT NOT NULL,
        candidate_b TEXT NOT NULL,
        distance_avg REAL NOT NULL,
        distance_min REAL,
        distance_std REAL,
        param_distance REAL,
        image_distance REAL,
        combined_distance REAL NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(annonce_id, candidate_a, candidate_b)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annonce_filter_selections (
        selection_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        selection_rank INTEGER NOT NULL,
        selection_reason TEXT NOT NULL,
        min_distance_to_previous REAL,
        avg_distance_to_previous REAL,
        original_delta_avg REAL,
        original_delta_min REAL,
        output_dir TEXT,
        selected_at TEXT NOT NULL,
        status TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        UNIQUE(annonce_id, selection_rank),
        UNIQUE(annonce_id, candidate_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bench_jobs (
        job_id TEXT PRIMARY KEY,
        annonce_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        duration_minutes REAL,
        started_at TEXT,
        finished_at TEXT,
        error_message TEXT,
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(annonce_id, stage)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_annonces_key ON annonces(annonce_key)",
    "CREATE INDEX IF NOT EXISTS idx_annonce_images_annonce ON annonce_images(annonce_id, image_index)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_annonce_score ON annonce_filter_candidates(annonce_id, max_score)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_selected ON annonce_filter_candidates(annonce_id, selected_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON bench_jobs(status, priority)",
)


@dataclass
class CatalogDb:
    settings: CatalogSettings
    connection: Any

    @property
    def backend(self) -> str:
        return self.settings.db_backend

    @property
    def placeholder(self) -> str:
        return "%s" if self.backend == "postgres" else "?"

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        return self.connection.execute(sql, tuple(params or ()))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Any:
        return self.connection.executemany(sql, rows)

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\u241f".join(json.dumps(part, sort_keys=True, default=str) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(settings: CatalogSettings | None = None) -> CatalogDb:
    resolved = settings or load_settings()
    if resolved.is_sqlite:
        sqlite_path = resolved.db_dsn.removeprefix("sqlite:///")
        db_path = Path(sqlite_path)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return CatalogDb(resolved, conn)

    if resolved.is_postgres:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("psycopg is required for PostgreSQL. Run pip install -r requirements.txt") from exc
        conn = psycopg.connect(resolved.db_dsn)
        return CatalogDb(resolved, conn)

    raise ValueError(f"Unsupported DB backend: {resolved.db_backend}")


@contextmanager
def open_catalog_db(settings: CatalogSettings | None = None) -> Iterator[CatalogDb]:
    db = connect(settings)
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_schema(db: CatalogDb) -> None:
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    db.commit()


def upsert_sql(db: CatalogDb, table: str, columns: Sequence[str], conflict_columns: Sequence[str], update_columns: Sequence[str]) -> str:
    ph = db.placeholder
    insert_cols = ", ".join(columns)
    values = ", ".join([ph] * len(columns))
    conflict = ", ".join(conflict_columns)
    updates = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    return f"INSERT INTO {table} ({insert_cols}) VALUES ({values}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"


def init_db_from_settings(settings: CatalogSettings | None = None) -> CatalogSettings:
    resolved = settings or load_settings()
    with open_catalog_db(resolved) as db:
        init_schema(db)
    return resolved
