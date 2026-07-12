from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .bench_db import BenchDatabase, initialize_databases
from .models import stable_hash
from .paths import LocalPaths
from .source_loader import load_source_listing, resolve_listing_reference


@dataclass
class PurgeSummary:
    mode: str
    listing_ids: list[str] = field(default_factory=list)
    source_set_hashes: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)
    removed_paths: int = 0
    reinitialized: bool = False

    def render(self) -> str:
        return json.dumps(self.__dict__, indent=2, ensure_ascii=False, sort_keys=True)


def _safe_listing(code: str) -> str:
    safe = "_".join(part for part in code.replace("\\", "/").split("/") if part)
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in safe) or "listing"


def _count(connection: sqlite3.Connection, sql: str, values=()) -> int:
    return int(connection.execute(sql, values).fetchone()[0])


def purge_listing(local_root: Path, listing_dir: Path, listing_code: str, current_only: bool, dry_run: bool, fail_after_bench: bool = False) -> PurgeSummary:
    paths = LocalPaths.from_root(local_root)
    listing_id = stable_hash({"listing_code": listing_code})
    current_hash = load_source_listing(listing_dir, listing_code=listing_code).source_set_hash if current_only else None
    summary = PurgeSummary("listing", [listing_id])
    if not paths.bench_database.exists() and not paths.variants_database.exists():
        return summary
    if dry_run and (not paths.bench_database.exists() or not paths.variants_database.exists()):
        return summary
    initialize_databases(local_root)
    connection = sqlite3.connect(paths.bench_database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("ATTACH DATABASE ? AS variants", (str(paths.variants_database),))
    source_clause = " AND source_set_hash=?" if current_hash else ""
    values = (listing_id, current_hash) if current_hash else (listing_id,)
    source_sets = [r[0] for r in connection.execute(f"SELECT DISTINCT source_set_hash FROM source_images WHERE listing_id=?{source_clause}", values)]
    summary.source_set_hashes = source_sets
    test_ids = [r[0] for r in connection.execute(f"SELECT test_id FROM recipe_tests WHERE listing_id=?{source_clause}", values)]
    recipe_ids = [r[0] for r in connection.execute(f"SELECT DISTINCT recipe_id FROM recipe_tests WHERE listing_id=?{source_clause}", values)]
    run_ids = [r[0] for r in connection.execute(f"SELECT run_id FROM bench_runs WHERE listing_id=?{source_clause}", values)]
    variant_ids = [r[0] for r in connection.execute(f"SELECT variant_id FROM variants.listing_variants WHERE listing_id=?{source_clause}", values)]
    summary.rows = {
        "bench_runs": len(run_ids), "recipe_tests": len(test_ids),
        "recipe_test_images": _count(connection, f"SELECT COUNT(*) FROM recipe_test_images WHERE test_id IN ({','.join('?'*len(test_ids))})", test_ids) if test_ids else 0,
        "recipe_pair_distances": _count(connection, f"SELECT COUNT(*) FROM recipe_pair_distances WHERE test_a IN ({','.join('?'*len(test_ids))}) OR test_b IN ({','.join('?'*len(test_ids))})", test_ids + test_ids) if test_ids else 0,
        "listing_variants": len(variant_ids),
        "listing_variant_images": _count(connection, f"SELECT COUNT(*) FROM variants.listing_variant_images WHERE variant_id IN ({','.join('?'*len(variant_ids))})", variant_ids) if variant_ids else 0,
    }
    output_paths = [Path(r[0]).parent for r in connection.execute(f"SELECT output_path FROM variants.listing_variant_images WHERE variant_id IN ({','.join('?'*len(variant_ids))})", variant_ids)] if variant_ids else []
    generated = set(output_paths)
    generated.update(paths.bench_work / run_id for run_id in run_ids)
    generated.update(p.parent for run_id in run_ids for p in paths.bench_runs.rglob(f"{run_id}/index.html"))
    listing_run_root = paths.bench_runs / _safe_listing(listing_code)
    if listing_run_root.exists() and not current_only: generated.add(listing_run_root)
    summary.paths = sorted(str(p) for p in generated)
    if dry_run:
        connection.close(); return summary
    try:
        connection.execute("BEGIN IMMEDIATE")
        if test_ids:
            marks = ",".join("?" * len(test_ids))
            connection.execute(f"DELETE FROM recipe_pair_distances WHERE test_a IN ({marks}) OR test_b IN ({marks})", test_ids + test_ids)
            connection.execute(f"DELETE FROM run_tests WHERE test_id IN ({marks})", test_ids)
            connection.execute(f"DELETE FROM recipe_tests WHERE test_id IN ({marks})", test_ids)
        if recipe_ids:
            recipe_marks = ",".join("?" * len(recipe_ids))
            connection.execute(f"DELETE FROM recipe_context_stats WHERE recipe_id IN ({recipe_marks})", recipe_ids)
            connection.execute(f"DELETE FROM recipe_global_stats WHERE recipe_id IN ({recipe_marks})", recipe_ids)
        if run_ids:
            connection.executemany("DELETE FROM bench_runs WHERE run_id=?", [(r,) for r in run_ids])
        if fail_after_bench: raise RuntimeError("simulated purge failure")
        if variant_ids:
            marks = ",".join("?" * len(variant_ids))
            connection.execute(f"UPDATE variants.listing_variants SET status='draft' WHERE variant_id IN ({marks})", variant_ids)
            connection.execute(f"DELETE FROM variants.listing_variants WHERE variant_id IN ({marks})", variant_ids)
        connection.execute(f"DELETE FROM source_images WHERE listing_id=?{source_clause}", values)
        connection.execute(f"DELETE FROM variants.listing_images WHERE listing_id=?{source_clause}", values)
        if not current_only:
            connection.execute("DELETE FROM source_listings WHERE listing_id=?", (listing_id,))
            connection.execute("DELETE FROM variants.listings WHERE listing_id=?", (listing_id,))
        connection.commit()
    except Exception:
        connection.rollback(); connection.close(); raise
    from .recipe_learning import refresh_recipe_statistics
    for recipe_id in recipe_ids:
        if connection.execute("SELECT 1 FROM recipe_tests WHERE recipe_id=?", (recipe_id,)).fetchone():
            refresh_recipe_statistics(connection, recipe_id)
    connection.close()
    for path in sorted(generated, key=lambda p: len(p.parts), reverse=True):
        if path.exists(): shutil.rmtree(path) if path.is_dir() else path.unlink(); summary.removed_paths += 1
    return summary


def purge_all(local_root: Path, dry_run: bool, reinitialize: bool) -> PurgeSummary:
    paths = LocalPaths.from_root(local_root); summary = PurgeSummary("all")
    for database, tables in ((paths.bench_database, ("bench_runs", "recipe_tests", "recipe_test_images", "recipe_pair_distances")), (paths.variants_database, ("listing_variants", "listing_variant_images"))):
        if database.exists():
            with sqlite3.connect(database) as connection:
                for table in tables:
                    try: summary.rows[table] = _count(connection, f"SELECT COUNT(*) FROM {table}")
                    except sqlite3.DatabaseError: summary.rows[table] = 0
    managed = [paths.bench_database, paths.variants_database, paths.bench_runs, paths.bench_work]
    managed += [Path(str(db) + suffix) for db in (paths.bench_database, paths.variants_database) for suffix in ("-wal", "-shm")]
    summary.paths = [str(p) for p in managed if p.exists()]
    if dry_run: return summary
    for path in managed:
        if path.is_dir(): shutil.rmtree(path)
        elif path.exists(): path.unlink()
    summary.removed_paths = len(summary.paths)
    if reinitialize: initialize_databases(local_root); summary.reinitialized = True
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Purge generated benchmark data without touching source images.")
    group = parser.add_mutually_exclusive_group(required=True); group.add_argument("--listing"); group.add_argument("--all", action="store_true")
    parser.add_argument("--source-root"); parser.add_argument("--local-root", default="local")
    parser.add_argument("--current-source-only", action="store_true"); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true"); parser.add_argument("--reinitialize", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.current_source_only and not args.listing: print("error: --current-source-only requires --listing", file=sys.stderr); return 2
    if args.reinitialize and not args.all: print("error: --reinitialize requires --all", file=sys.stderr); return 2
    if args.all and not args.dry_run and not args.yes: print("error: --all requires --yes", file=sys.stderr); return 2
    try:
        if args.all: summary = purge_all(Path(args.local_root), args.dry_run, args.reinitialize)
        else:
            directory, code = resolve_listing_reference(args.listing, args.source_root)
            summary = purge_listing(Path(args.local_root), directory, code, args.current_source_only, args.dry_run)
        print(summary.render()); return 0
    except Exception as error:
        print(f"error: {error}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
