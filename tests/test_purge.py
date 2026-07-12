from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.catalog_photo_control.bench import build_parser as bench_parser, run_benchmark
from common.catalog_photo_control.purge import main, purge_listing


def _copy_listing(source: Path, target: Path) -> Path:
    target.mkdir(parents=True)
    for path in source.iterdir(): (target / path.name).write_bytes(path.read_bytes())
    return target


def _bench(listing: Path, local: Path) -> None:
    args = bench_parser().parse_args(["--listing", str(listing), "--local-root", str(local), "--target-variants", "1", "--max-tests", "20", "--max-duration-minutes", "1", "--patience", "20", "--seed", "4", "--quiet"])
    assert run_benchmark(args)[2]["obtained"] == 1


def test_dry_run_and_scoped_listing_purge_preserve_sources_and_other_listing(synthetic_listing: Path, tmp_path: Path) -> None:
    first = _copy_listing(synthetic_listing, tmp_path / "A")
    second = _copy_listing(synthetic_listing, tmp_path / "B")
    local = tmp_path / "local"; _bench(first, local); _bench(second, local)
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    dry = purge_listing(local, first, "A", False, True)
    assert dry.rows["listing_variants"] == 1
    with sqlite3.connect(local / "databases" / "catalog_variants.sqlite3") as db: assert db.execute("SELECT COUNT(*) FROM listing_variants").fetchone()[0] == 2
    summary = purge_listing(local, first, "A", False, False)
    assert summary.rows["recipe_tests"] > 0 and before == {p.name: p.read_bytes() for p in first.iterdir()}
    with sqlite3.connect(local / "databases" / "catalog_variants.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM listing_variants").fetchone()[0] == 1


def test_current_only_and_rollback(synthetic_listing: Path, tmp_path: Path) -> None:
    listing = _copy_listing(synthetic_listing, tmp_path / "A"); local = tmp_path / "local"; _bench(listing, local)
    with pytest.raises(RuntimeError, match="simulated"):
        purge_listing(local, listing, "A", True, False, fail_after_bench=True)
    with sqlite3.connect(local / "databases" / "catalog_bench.sqlite3") as db: assert db.execute("SELECT COUNT(*) FROM recipe_tests").fetchone()[0] > 0
    purge_listing(local, listing, "A", True, False)


def test_global_requires_yes_and_can_reinitialize(tmp_path: Path) -> None:
    assert main(["--all", "--local-root", str(tmp_path / "local")]) == 2
    unrelated = tmp_path / "local" / "keep.txt"; unrelated.parent.mkdir(); unrelated.write_text("keep")
    assert main(["--all", "--yes", "--reinitialize", "--local-root", str(tmp_path / "local")]) == 0
    assert unrelated.is_file()
    for name in ("catalog_bench.sqlite3", "catalog_variants.sqlite3"): assert (tmp_path / "local" / "databases" / name).is_file()


def test_absent_databases_are_safe(synthetic_listing: Path, tmp_path: Path) -> None:
    assert purge_listing(tmp_path / "missing", synthetic_listing, synthetic_listing.name, False, True).rows == {}
