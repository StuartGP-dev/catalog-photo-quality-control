# SQLite schemas and runtime invariants

## Historical benchmark database

`local/databases/catalog_bench.sqlite3` is append-oriented and records every
attempted canonical recipe.

- `source_listings`, `source_images`: ordered source versions and hashes;
- `bench_runs`, `run_tests`: run status, stop reason, counters, proposal origin,
  and cache usage;
- `recipes`: canonical JSON keyed by deterministic `recipe_hash`;
- `recipe_tests`, `recipe_test_images`: complete/rejected status plus listing and
  per-image metrics;
- `recipe_pair_distances`: optional inspectable distance components, keyed by an
  ordered pair;
- `recipe_global_stats`, `recipe_context_stats`: lissées pour l’apprentissage
  inter-annonces.

The unique key `(listing_id, source_set_hash, recipe_id)` prevents recomputing a
recipe for the same ordered source version. The effective cache key additionally
contains `evaluation_config_hash`, derived from the complete filter space,
quality thresholds, compatibility rules, probabilities, and metrics version.
Rejected image files are removed,
while their hashes and metrics remain queryable.

## Final variants database

`local/databases/catalog_variants.sqlite3` contains only selected complete
variants.

- `listings`, `listing_images`: active and historical ordered sources;
- `listing_variants`: recipe, selection rank, aggregate metrics, original and
  minimum-selected distances plus average distance/rank among ready variants
  of the same listing, with separately inspectable components, and
  reserved content fields;
- `listing_variant_images`: every output in source order with source/output
  hashes, pixel dimensions, canvas/background diagnostics, per-image metrics,
  and per-image `metadata_json`/`metadata_status` fields.

For a listing source version, selected variants cannot reuse the same ordered
pixel-dimension signature. Dimensions include a small deterministic recipe and
source-index signature, so outputs never equal their originals in both axes.

Content fields are `title_text`, `description_text`, `price_cents`, and
`currency`; this refactor reserves them without populating them. Variant-level
and image-level metadata fields remain `reserved` unless
technical metadata is explicitly applied or indexed. Stored JSON describes only
values actually present in the generated file; it never derives capture data.

Variants are inserted as `draft`. SQLite triggers only allow transition to
`ready` when the number, indices, and hashes of variant images exactly cover the
registered source set. Images cannot be removed from a ready variant.

## Atomicity and ownership

Source paths are read-only. Rendering happens in a temporary generated
directory; the directory is renamed to its final location only after every image
succeeds. Final selection copies a complete candidate to another temporary
directory before database commit. All generated state belongs below `local/`.

Scoped purge attaches `catalog_variants.sqlite3` to the benchmark connection and
deletes both schemas inside one SQLite transaction. Filesystem paths are removed
only after commit, limiting partial failure to harmless leftover generated files.
# Barrière perceptuelle par image

`perceptual_comparisons` conserve SHA égal, les trois distances brutes et
bandes, le verdict, sa raison, le voisin et la version du moteur. Les JSON de
`recipe_test_images` et `listing_variant_images` conservent le même diagnostic
pour le voisin le plus proche. `listing_variants.diversity_valid` doit valoir 1
avant le passage à `ready`. Toutes les références sont des variantes `ready`,
dédupliquées par `output_hash`, et possèdent le même `image_index`. Une seule
image `exact`, `same` ou `near_duplicate` invalide atomiquement le variant.
