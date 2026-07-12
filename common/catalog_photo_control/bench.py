from __future__ import annotations

import argparse
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .bench_db import BenchDatabase
from .config import load_filter_space
from .html_report import write_html_report
from .paths import LocalPaths
from .recipe_generator import RecipeGenerator
from .recipe_learning import listing_context_key, proven_recipes
from .selector import load_eligible_candidates, select_and_persist
from .source_loader import load_source_listing, resolve_listing_reference
from .variants_db import VariantsDatabase


def classify_stop_reason(
    *,
    selected: int,
    target: int,
    tests: int,
    max_tests: int,
    elapsed_seconds: float,
    max_duration_seconds: float,
    stale: int,
    patience: int,
    interrupted: bool = False,
    error: bool = False,
) -> str | None:
    if error:
        return "error"
    if interrupted:
        return "interrupted"
    if selected >= target:
        return "target_reached"
    if tests >= max_tests:
        return "max_tests_reached"
    if elapsed_seconds >= max_duration_seconds:
        return "max_duration_reached"
    if stale >= patience:
        return "patience_exhausted"
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_listing(code: str) -> str:
    safe = "_".join(part for part in code.replace("\\", "/").split("/") if part)
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in safe) or "listing"


def _cleanup_work(bench: BenchDatabase, run_work: Path) -> None:
    prefix = str(run_work.resolve())
    with bench.connection:
        test_ids = [
            row[0]
            for row in bench.connection.execute(
                "SELECT test_id FROM recipe_tests WHERE retained_output_dir LIKE ?",
                (prefix + "%",),
            )
        ]
        if test_ids:
            placeholders = ",".join("?" for _ in test_ids)
            bench.connection.execute(
                f"UPDATE recipe_test_images SET output_path=NULL WHERE test_id IN ({placeholders})",
                test_ids,
            )
            bench.connection.execute(
                f"UPDATE recipe_tests SET retained_output_dir=NULL WHERE test_id IN ({placeholders})",
                test_ids,
            )
    shutil.rmtree(run_work, ignore_errors=True)


def run_benchmark(args: argparse.Namespace) -> tuple[str, Path, dict[str, int]]:
    listing_dir, listing_code = resolve_listing_reference(args.listing, args.source_root)
    listing = load_source_listing(listing_dir, listing_code=listing_code)
    paths = LocalPaths.from_root(args.local_root)
    paths.ensure_runtime_directories()
    space = load_filter_space(args.filter_space)
    allocation = dict(space.proposal_allocation)
    if args.random_share is not None:
        supplied = (args.random_share, args.proven_share, args.mutation_share)
        if any(value is None for value in supplied) or abs(sum(supplied) - 1) > 1e-9:
            raise ValueError("all proposal shares are required and must sum to 1")
        allocation = dict(zip(("random", "proven", "mutation"), supplied, strict=True))
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = paths.bench_runs / _safe_listing(listing.listing_code) / run_id
    selected_root = run_dir / "selected_variants"
    run_work = paths.bench_work / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    generator = RecipeGenerator(space.schema, allocation, seed=args.seed)
    counters = {"tested": 0, "cached": 0, "valid": 0, "rejected": 0, "selected": 0}
    started_clock = time.monotonic()
    started_at = _utc_now()
    stop_reason = "error"
    status = "error"
    caught: BaseException | None = None
    with BenchDatabase(paths.bench_database) as bench, VariantsDatabase(paths.variants_database) as variants:
        bench.initialize()
        variants.initialize()
        bench.register_source(listing)
        variants.register_source(listing)
        bench.start_run(run_id, listing, args.target_variants, started_at, space.evaluation_config_hash)
        stale = 0
        try:
            while True:
                selected_count = variants.ready_count(listing.listing_id, listing.source_set_hash)
                pending_stop = classify_stop_reason(
                    selected=selected_count,
                    target=args.target_variants,
                    tests=counters["tested"],
                    max_tests=args.max_tests,
                    elapsed_seconds=time.monotonic() - started_clock,
                    max_duration_seconds=args.max_duration_minutes * 60,
                    stale=stale,
                    patience=args.patience,
                )
                if pending_stop:
                    if pending_stop != "target_reached":
                        new_ids = select_and_persist(
                            bench.connection,
                            variants,
                            listing,
                            args.target_variants,
                            selected_root,
                        )
                        counters["selected"] += len(new_ids)
                        selected_count = variants.ready_count(
                            listing.listing_id, listing.source_set_hash
                        )
                    stop_reason = (
                        "target_reached"
                        if selected_count >= args.target_variants
                        else pending_stop
                    )
                    break
                context = listing_context_key(listing)
                proven = proven_recipes(bench.connection, context)
                proposal = generator.propose(proven)
                execution = bench.execute_recipe_test(
                    listing,
                    proposal.recipe,
                    run_work,
                    space.quality_thresholds,
                    space.evaluation_config_hash,
                    force=False,
                )
                counters["tested"] += 1
                canvas_active = proposal.recipe.parameters.get("canvas_mode", "none") != "none"
                counters["canvas_tested"] = counters.get("canvas_tested", 0) + int(canvas_active)
                counters["cached"] += int(execution.cached)
                counters["valid"] += int(execution.complete and execution.quality_valid)
                counters["canvas_valid"] = counters.get("canvas_valid", 0) + int(canvas_active and execution.quality_valid)
                counters["rejected"] += int(not execution.quality_valid)
                if execution.error:
                    for reason in execution.error.split(","):
                        key = f"rejected_{reason.split(':')[-1]}"
                        counters[key] = counters.get(key, 0) + 1
                bench.add_run_test(
                    run_id, execution.test_id, proposal.source, cached=execution.cached
                )
                stale = 0 if execution.eligible and not execution.cached else stale + 1
                candidates = load_eligible_candidates(bench.connection, listing)
                needed = args.target_variants - selected_count
                if needed > 0 and len(candidates) >= needed * space.selection_pool_multiplier:
                    new_ids = select_and_persist(
                        bench.connection,
                        variants,
                        listing,
                        args.target_variants,
                        selected_root,
                    )
                    counters["selected"] += len(new_ids)
                    counters["canvas_selected"] = counters.get("canvas_selected", 0) + sum(1 for variant_id in new_ids if variants.connection.execute("SELECT recipe_json FROM listing_variants WHERE variant_id=?", (variant_id,)).fetchone()[0].find('"canvas_mode":"none"') < 0)
                after = variants.ready_count(listing.listing_id, listing.source_set_hash)
                if not args.quiet:
                    print(
                        f"tests={counters['tested']} cached={counters['cached']} "
                        f"valid={counters['valid']} selected={after}/{args.target_variants}",
                        flush=True,
                    )
            status = "completed"
        except KeyboardInterrupt as error:
            stop_reason, status, caught = "interrupted", "interrupted", error
        except BaseException as error:
            stop_reason, status, caught = "error", "error", error
        finally:
            obtained = variants.ready_count(listing.listing_id, listing.source_set_hash)
            counters["obtained"] = obtained
            _cleanup_work(bench, run_work)
            bench.finish_run(run_id, status, stop_reason, _utc_now(), counters)
            report = write_html_report(
                run_dir / "index.html",
                variants.connection,
                listing,
                run_id=run_id,
                status=status,
                stop_reason=stop_reason,
                requested=args.target_variants,
                counters=counters,
            )
    if caught is not None and not isinstance(caught, KeyboardInterrupt):
        raise caught
    return stop_reason, report, counters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the unified catalog photo benchmark.")
    parser.add_argument("--listing", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--local-root", default="local")
    parser.add_argument("--filter-space", default=str(Path(__file__).resolve().parents[2] / "config" / "filter_space.json"))
    parser.add_argument("--target-variants", type=int, required=True)
    parser.add_argument("--max-tests", type=int, default=20000)
    parser.add_argument("--max-duration-minutes", type=float, default=180)
    parser.add_argument("--patience", type=int, default=3000)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--fresh-run", action="store_true", help="Start a new run record; final variants and recipe cache remain immutable.")
    parser.add_argument("--random-share", type=float)
    parser.add_argument("--proven-share", type=float)
    parser.add_argument("--mutation-share", type=float)
    parser.add_argument("--quiet", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.target_variants < 0:
        raise ValueError("target variants must be non-negative")
    if args.max_tests <= 0 or args.max_duration_minutes <= 0 or args.patience <= 0:
        raise ValueError("stop limits must be positive")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_args(args)
        stop_reason, report, counters = run_benchmark(args)
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"stop_reason={stop_reason}")
    print(f"selected={counters['obtained']}")
    print(f"report={report}")
    return 0 if stop_reason != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
