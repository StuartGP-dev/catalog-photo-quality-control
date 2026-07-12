from __future__ import annotations

from pathlib import Path

from common.catalog_photo_control.bench_db import BenchDatabase
from common.catalog_photo_control.config import load_filter_space
from common.catalog_photo_control.recipe_generator import RecipeGenerator
from common.catalog_photo_control.recipe_learning import proven_recipes, smoothed_confidence
from common.catalog_photo_control.source_loader import load_source_listing


def test_one_success_does_not_dominate_confidence() -> None:
    assert smoothed_confidence(1, 1) == 0.4
    assert smoothed_confidence(80, 100) > smoothed_confidence(1, 1)


def test_successful_recipe_can_seed_another_listing(
    synthetic_listing: Path, tmp_path: Path
) -> None:
    listing = load_source_listing(synthetic_listing, listing_code="first")
    space = load_filter_space()
    recipe = space.schema.canonicalize({"contrast": 1.03})
    database = BenchDatabase(tmp_path / "bench.sqlite3")
    database.initialize()
    database.register_source(listing)
    execution = database.execute_recipe_test(
        listing, recipe, tmp_path / "work", space.quality_thresholds
    )
    assert execution.eligible

    ranked = proven_recipes(database.connection)

    assert recipe.recipe_hash in {item.recipe_hash for item in ranked}
    database.close()


def test_mutations_stay_in_bounds_and_exploration_is_retained() -> None:
    space = load_filter_space()
    generator = RecipeGenerator(
        space.schema, dict(space.proposal_allocation), seed=42
    )
    parent = space.schema.canonicalize({})

    mutations = [generator.mutate(parent) for _ in range(30)]
    for mutation in mutations:
        assert mutation.recipe_hash != parent.recipe_hash
        space.schema.canonicalize(mutation.parameters)

    sources = [generator.propose([parent]).source for _ in range(300)]
    assert {"random", "proven", "mutation"} <= set(sources)
    assert sources.count("random") > sources.count("mutation")
