from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import Recipe, RecipeTest, SourceListing, canonical_json
from .paths import LocalPaths


@dataclass(frozen=True, slots=True)
class TestExecution:
    test_id: int
    cached: bool
    complete: bool
    quality_valid: bool
    eligible: bool
    aggregate_metrics: Mapping[str, float]
    output_dir: Path | None
    error: str | None = None


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS source_listings (
    listing_id TEXT PRIMARY KEY,
    listing_code TEXT NOT NULL UNIQUE,
    current_source_set_hash TEXT NOT NULL,
    source_directory TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_images (
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    width INTEGER NOT NULL CHECK(width > 0),
    height INTEGER NOT NULL CHECK(height > 0),
    PRIMARY KEY (listing_id, source_set_hash, image_index)
);
CREATE INDEX IF NOT EXISTS idx_source_images_hash
    ON source_images(listing_id, source_set_hash, source_hash);
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    target_variants INTEGER NOT NULL CHECK(target_variants >= 0),
    status TEXT NOT NULL,
    stop_reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    counters_json TEXT NOT NULL DEFAULT '{}'
    ,evaluation_config_hash TEXT NOT NULL DEFAULT 'legacy'
);
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id INTEGER PRIMARY KEY,
    recipe_hash TEXT NOT NULL UNIQUE,
    parameters_json TEXT NOT NULL,
    recipe_family TEXT NOT NULL DEFAULT 'appearance_only',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recipe_tests (
    test_id INTEGER PRIMARY KEY,
    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
    source_set_hash TEXT NOT NULL,
    recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id),
    recipe_family TEXT NOT NULL DEFAULT 'appearance_only',
    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
    quality_valid INTEGER NOT NULL CHECK(quality_valid IN (0, 1)),
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
    aggregate_metrics_json TEXT NOT NULL,
    error_text TEXT,
    retained_output_dir TEXT,
    context_key TEXT NOT NULL DEFAULT 'unknown',
    evaluation_config_hash TEXT NOT NULL DEFAULT 'legacy',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (listing_id, source_set_hash, recipe_id, evaluation_config_hash)
);
CREATE INDEX IF NOT EXISTS idx_recipe_tests_candidates
    ON recipe_tests(listing_id, source_set_hash, complete, quality_valid, eligible);
CREATE TABLE IF NOT EXISTS recipe_test_images (
    test_id INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL CHECK(image_index >= 0),
    source_hash TEXT NOT NULL,
    success INTEGER NOT NULL CHECK(success IN (0, 1)),
    output_path TEXT,
    output_hash TEXT,
    metrics_json TEXT NOT NULL,
    error_text TEXT,
    PRIMARY KEY (test_id, image_index)
);
CREATE TABLE IF NOT EXISTS run_tests (
    run_id TEXT NOT NULL REFERENCES bench_runs(run_id) ON DELETE CASCADE,
    test_id INTEGER NOT NULL REFERENCES recipe_tests(test_id),
    proposal_source TEXT NOT NULL,
    cached INTEGER NOT NULL CHECK(cached IN (0, 1)),
    PRIMARY KEY (run_id, test_id)
);
CREATE TABLE IF NOT EXISTS recipe_pair_distances (
    listing_id TEXT NOT NULL,
    source_set_hash TEXT NOT NULL,
    test_a INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    test_b INTEGER NOT NULL REFERENCES recipe_tests(test_id) ON DELETE CASCADE,
    components_json TEXT NOT NULL,
    distance REAL NOT NULL,
    CHECK(test_a < test_b),
    UNIQUE(listing_id, source_set_hash, test_a, test_b)
);
CREATE TABLE IF NOT EXISTS recipe_global_stats (
    recipe_id INTEGER PRIMARY KEY REFERENCES recipes(recipe_id) ON DELETE CASCADE,
    tested_count INTEGER NOT NULL DEFAULT 0,
    complete_count INTEGER NOT NULL DEFAULT 0,
    quality_valid_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recipe_context_stats (
    recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE,
    context_key TEXT NOT NULL,
    tested_count INTEGER NOT NULL DEFAULT 0,
    complete_count INTEGER NOT NULL DEFAULT 0,
    quality_valid_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(recipe_id, context_key)
);
"""


class BenchDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "BenchDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        for table, column in (("bench_runs", "evaluation_config_hash"), ("recipe_tests", "evaluation_config_hash")):
            columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT 'legacy'")
        family_columns_added = False
        for table in ("recipes", "recipe_tests"):
            columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            if "recipe_family" not in columns:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN recipe_family TEXT NOT NULL DEFAULT 'appearance_only'"
                )
                family_columns_added = True
        from .recipe_schema import classify_recipe_family

        if family_columns_added:
            for row in self.connection.execute(
                "SELECT recipe_id, parameters_json FROM recipes"
            ).fetchall():
                family = classify_recipe_family(json.loads(row["parameters_json"]))
                self.connection.execute(
                    "UPDATE recipes SET recipe_family=? WHERE recipe_id=?",
                    (family, row["recipe_id"]),
                )
                self.connection.execute(
                    "UPDATE recipe_tests SET recipe_family=? WHERE recipe_id=?",
                    (family, row["recipe_id"]),
                )
        self.connection.commit()
        self._migrate_recipe_test_identity()

    def _migrate_recipe_test_identity(self) -> None:
        """Replace the legacy three-column cache identity without losing history."""
        unique_indexes = [
            row
            for row in self.connection.execute("PRAGMA index_list(recipe_tests)")
            if row[2]
        ]
        identities = {
            tuple(
                item[2]
                for item in self.connection.execute(
                    f"PRAGMA index_info({index[1]})"
                )
            )
            for index in unique_indexes
        }
        legacy = ("listing_id", "source_set_hash", "recipe_id")
        current = legacy + ("evaluation_config_hash",)
        if legacy not in identities or current in identities:
            return
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                CREATE TABLE recipe_tests_new (
                    test_id INTEGER PRIMARY KEY,
                    listing_id TEXT NOT NULL REFERENCES source_listings(listing_id),
                    source_set_hash TEXT NOT NULL,
                    recipe_id INTEGER NOT NULL REFERENCES recipes(recipe_id),
                    recipe_family TEXT NOT NULL DEFAULT 'appearance_only',
                    complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
                    quality_valid INTEGER NOT NULL CHECK(quality_valid IN (0, 1)),
                    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                    selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
                    aggregate_metrics_json TEXT NOT NULL,
                    error_text TEXT,
                    retained_output_dir TEXT,
                    context_key TEXT NOT NULL DEFAULT 'unknown',
                    evaluation_config_hash TEXT NOT NULL DEFAULT 'legacy',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (listing_id, source_set_hash, recipe_id, evaluation_config_hash)
                );
                INSERT INTO recipe_tests_new (
                    test_id, listing_id, source_set_hash, recipe_id, recipe_family,
                    complete, quality_valid, eligible, selected,
                    aggregate_metrics_json, error_text, retained_output_dir,
                    context_key, evaluation_config_hash, created_at
                )
                SELECT test_id, listing_id, source_set_hash, recipe_id, recipe_family,
                       complete, quality_valid, eligible, selected,
                       aggregate_metrics_json, error_text, retained_output_dir,
                       context_key, evaluation_config_hash, created_at
                FROM recipe_tests;
                DROP TABLE recipe_tests;
                ALTER TABLE recipe_tests_new RENAME TO recipe_tests;
                CREATE INDEX idx_recipe_tests_candidates
                    ON recipe_tests(listing_id, source_set_hash, complete, quality_valid, eligible);
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"foreign key violations after recipe-test migration: {violations[:3]}"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.connection:
                yield self.connection
        except Exception:
            self.connection.rollback()
            raise

    def register_source(self, listing: SourceListing) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO source_listings
                   (listing_id, listing_code, current_source_set_hash, source_directory)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(listing_id) DO UPDATE SET
                     current_source_set_hash=excluded.current_source_set_hash,
                     source_directory=excluded.source_directory,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    listing.listing_id,
                    listing.listing_code,
                    listing.source_set_hash,
                    str(listing.directory),
                ),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO source_images
                   (listing_id, source_set_hash, image_index, source_hash,
                    source_path, width, height) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        listing.listing_id,
                        listing.source_set_hash,
                        image.index,
                        image.source_hash,
                        str(image.path),
                        image.width,
                        image.height,
                    )
                    for image in listing.images
                ],
            )

    def start_run(
        self,
        run_id: str,
        listing: SourceListing,
        target_variants: int,
        started_at: str,
        evaluation_config_hash: str = "legacy",
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO bench_runs
                   (run_id, listing_id, source_set_hash, target_variants, status, started_at,
                    evaluation_config_hash) VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (
                    run_id,
                    listing.listing_id,
                    listing.source_set_hash,
                    target_variants,
                    started_at,
                    evaluation_config_hash,
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        stop_reason: str,
        finished_at: str,
        counters: Mapping[str, int],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE bench_runs SET status=?, stop_reason=?, finished_at=?,
                   counters_json=? WHERE run_id=?""",
                (status, stop_reason, finished_at, canonical_json(counters), run_id),
            )

    def recipe_id(self, recipe: Recipe) -> int:
        from .recipe_schema import classify_recipe_family

        family = classify_recipe_family(recipe.parameters)
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO recipes(recipe_hash, parameters_json, recipe_family) VALUES (?, ?, ?)",
                (recipe.recipe_hash, canonical_json(recipe.parameters), family),
            )
            connection.execute(
                "UPDATE recipes SET recipe_family=? WHERE recipe_hash=?",
                (family, recipe.recipe_hash),
            )
            row = connection.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_hash=?", (recipe.recipe_hash,)
            ).fetchone()
        assert row is not None
        return int(row[0])

    def cached_test(
        self, listing_id: str, source_set_hash: str, recipe_hash: str,
        evaluation_config_hash: str = "legacy",
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT t.*, r.recipe_hash, r.parameters_json
               FROM recipe_tests t JOIN recipes r USING(recipe_id)
               WHERE t.listing_id=? AND t.source_set_hash=? AND r.recipe_hash=?
                 AND t.evaluation_config_hash=?""",
            (listing_id, source_set_hash, recipe_hash, evaluation_config_hash),
        ).fetchone()

    def record_test(
        self,
        test: RecipeTest,
        image_rows: Sequence[Mapping[str, Any]],
        *,
        retained_output_dir: str | None = None,
        context_key: str = "unknown",
        evaluation_config_hash: str = "legacy",
    ) -> int:
        recipe_id = self.recipe_id(test.recipe)
        from .recipe_schema import classify_recipe_family

        family = classify_recipe_family(test.recipe.parameters)
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO recipe_tests
                   (listing_id, source_set_hash, recipe_id, recipe_family, complete, quality_valid,
                    eligible, aggregate_metrics_json, error_text, retained_output_dir,
                    context_key, evaluation_config_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test.listing_id,
                    test.source_set_hash,
                    recipe_id,
                    family,
                    int(test.complete),
                    int(test.quality_valid),
                    int(test.eligible),
                    canonical_json(test.aggregate_metrics),
                    test.error,
                    retained_output_dir,
                    context_key,
                    evaluation_config_hash,
                ),
            )
            test_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO recipe_test_images
                   (test_id, image_index, source_hash, success, output_path,
                    output_hash, metrics_json, error_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        test_id,
                        int(row["image_index"]),
                        str(row["source_hash"]),
                        int(bool(row["success"])),
                        row.get("output_path"),
                        row.get("output_hash"),
                        canonical_json(row.get("metrics", {})),
                        row.get("error"),
                    )
                    for row in image_rows
                ],
            )
        return test_id

    def add_run_test(
        self, run_id: str, test_id: int, proposal_source: str, *, cached: bool
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO run_tests VALUES (?, ?, ?, ?)",
                (run_id, test_id, proposal_source, int(cached)),
            )

    def execute_recipe_test(
        self,
        listing: SourceListing,
        recipe: Recipe,
        work_root: str | Path,
        quality_thresholds: Mapping[str, float],
        evaluation_config_hash: str = "legacy",
        *,
        force: bool = False,
    ) -> TestExecution:
        from .image_pipeline import render_listing
        from .metrics import aggregate_metrics, image_metrics
        from .quality import evaluate_quality
        from .recipe_learning import listing_context_key, refresh_recipe_statistics

        cached = self.cached_test(
            listing.listing_id, listing.source_set_hash, recipe.recipe_hash,
            evaluation_config_hash,
        )
        if cached is not None and not force:
            return TestExecution(
                int(cached["test_id"]),
                True,
                bool(cached["complete"]),
                bool(cached["quality_valid"]),
                bool(cached["eligible"]),
                json.loads(cached["aggregate_metrics_json"]),
                Path(cached["retained_output_dir"])
                if cached["retained_output_dir"]
                else None,
                cached["error_text"],
            )
        if cached is not None:
            with self.connection:
                self.connection.execute(
                    "DELETE FROM recipe_tests WHERE test_id=?", (cached["test_id"],)
                )

        output_dir = Path(work_root).resolve() / recipe.recipe_hash
        context_key = listing_context_key(listing)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        try:
            rendered = render_listing(listing, recipe, output_dir)
            if len(rendered.images) != len(listing.images):
                raise RuntimeError("render did not cover every source image")
            metric_rows = [
                image_metrics(source.path, output.output_path, output.canvas_metadata)
                for source, output in zip(listing.images, rendered.images, strict=True)
            ]
            aggregate = aggregate_metrics(metric_rows)
            from .recipe_schema import analyze_recipe, classify_recipe_family
            from .config import load_filter_space
            analysis = analyze_recipe(recipe.parameters, load_filter_space().schema.parameters)
            recipe_family = classify_recipe_family(recipe.parameters)
            aggregate.update({
                "active_parameter_count": float(analysis.active_parameter_count),
                "recipe_intensity": analysis.recipe_intensity,
                "active_parameters": list(analysis.active_parameters),
                "canvas_mode": recipe.parameters.get("canvas_mode", "none"),
                "canvas_mode_code": float(("none", "white", "light_gray", "sampled_background", "sampled_edge", "side_bands", "uniform_frame").index(str(recipe.parameters.get("canvas_mode", "none")))),
                "recipe_family": recipe_family,
                "rotation_degrees": float(recipe.parameters.get("rotation_degrees", 0.0)),
                "crop_fraction": float(recipe.parameters.get("crop_fraction", 0.0)),
                "zoom": float(recipe.parameters.get("zoom", 1.0)),
                "resize_scale": float(recipe.parameters.get("resize_scale", 1.0)),
                "offset_x": float(recipe.parameters.get("offset_x", 0.0)),
                "offset_y": float(recipe.parameters.get("offset_y", 0.0)),
            })
            aggregate["background_origin"] = sorted(
                {str(row.get("background_origin", "unknown")) for row in metric_rows}
            )
            aggregate["background_rgb"] = [row.get("background_rgb") for row in metric_rows]
            aggregate["sampled_background_rgb"] = [
                row.get("sampled_background_rgb") for row in metric_rows
            ]
            quality = evaluate_quality(metric_rows, quality_thresholds)
            aggregate["quality_score"] = quality.score
            rows = [
                {
                    "image_index": output.image_index,
                    "source_hash": output.source_hash,
                    "success": True,
                    "output_path": str(output.output_path) if quality.valid else None,
                    "output_hash": output.output_hash,
                    "metrics": metrics,
                    "output_width": output.width,
                    "output_height": output.height,
                }
                for output, metrics in zip(rendered.images, metric_rows, strict=True)
            ]
            test = RecipeTest(
                None,
                listing.listing_id,
                listing.source_set_hash,
                recipe,
                True,
                quality.valid,
                quality.valid,
                aggregate,
                error=",".join(quality.reasons) or None,
            )
            test_id = self.record_test(
                test,
                rows,
                retained_output_dir=str(output_dir) if quality.valid else None,
                context_key=context_key,
                evaluation_config_hash=evaluation_config_hash,
            )
            refresh_recipe_statistics(self.connection, self.recipe_id(recipe))
            if not quality.valid:
                shutil.rmtree(output_dir, ignore_errors=True)
                output_dir_result = None
            else:
                output_dir_result = output_dir
            return TestExecution(
                test_id,
                False,
                True,
                quality.valid,
                quality.valid,
                aggregate,
                output_dir_result,
                test.error,
            )
        except Exception as error:
            shutil.rmtree(output_dir, ignore_errors=True)
            test = RecipeTest(
                None,
                listing.listing_id,
                listing.source_set_hash,
                recipe,
                False,
                False,
                False,
                {},
                error=f"{type(error).__name__}: {error}",
            )
            test_id = self.record_test(test, [], context_key=context_key, evaluation_config_hash=evaluation_config_hash)
            refresh_recipe_statistics(self.connection, self.recipe_id(recipe))
            return TestExecution(
                test_id, False, False, False, False, {}, None, test.error
            )


def initialize_databases(local_root: str | Path = "local") -> LocalPaths:
    from .variants_db import VariantsDatabase

    paths = LocalPaths.from_root(local_root)
    paths.ensure_runtime_directories()
    with BenchDatabase(paths.bench_database) as bench:
        bench.initialize()
    with VariantsDatabase(paths.variants_database) as variants:
        variants.initialize()
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize both catalog SQLite databases.")
    parser.add_argument("--local-root", default="local")
    args = parser.parse_args(argv)
    paths = initialize_databases(args.local_root)
    print(paths.bench_database)
    print(paths.variants_database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
