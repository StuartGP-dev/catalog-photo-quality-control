# Clean bench pipeline refactor plan

## Goal

Refactor the project around one reproducible offline catalog-augmentation and quality-benchmark pipeline:

- one CLI entry point;
- one historical benchmark database containing every tested recipe and its per-image results;
- one final variants database containing only complete accepted listing variants;
- one HTML report per benchmark run;
- one recipe applied consistently to every image in a listing variant;
- reusable cross-listing recipe statistics;
- resumable execution until a requested number of accepted variants exists.

## Non-negotiable invariants

1. A variant is valid only when all active source images were processed successfully.
2. Every image in one variant uses exactly the same canonical recipe.
3. A recipe is identified by a deterministic hash of normalized parameters.
4. A source listing version is identified by a deterministic hash of its ordered source-image hashes.
5. The same recipe is never recomputed for the same source listing version unless explicitly forced.
6. Benchmark history is append-oriented and remains independent from the final selected-variant catalog.
7. Generated images, SQLite databases, logs, and reports remain under `local/` and are never committed.
8. A benchmark run emits exactly one user-facing HTML file: `index.html`.
9. Title, description, price, and metadata fields exist in the final schema but may remain unset until their dedicated implementations are added.
10. Metadata work is limited in this refactor to removing obsolete modules and reserving future schema fields.

## Target package layout

```text
common/catalog_photo_control/
├── __init__.py
├── config.py
├── models.py
├── paths.py
├── source_loader.py
├── recipe_schema.py
├── recipe_generator.py
├── image_pipeline.py
├── metrics.py
├── quality.py
├── diversity.py
├── bench_db.py
├── variants_db.py
├── recipe_learning.py
├── selector.py
├── html_report.py
└── bench.py

config/
└── filter_space.json

metadata_privacy_two_images.py
```

## Runtime layout

```text
local/
├── databases/
│   ├── catalog_bench.sqlite3
│   └── catalog_variants.sqlite3
├── bench_work/
│   └── <run_id>/
└── bench_runs/
    └── <listing_safe>/<run_id>/
        ├── index.html
        └── selected_variants/
            ├── variant_0001/
            └── variant_0002/
```

## Databases

### Historical benchmark database

`catalog_bench.sqlite3` stores all attempted recipes and measurements.

Core tables:

- `source_listings`
- `source_images`
- `bench_runs`
- `recipes`
- `recipe_tests`
- `recipe_test_images`
- `run_tests`
- `recipe_pair_distances`
- `recipe_global_stats`
- `recipe_context_stats`

Important uniqueness rules:

- `recipes(recipe_hash)`
- `recipe_tests(listing_id, source_set_hash, recipe_id)`
- `recipe_test_images(test_id, image_index)`
- `recipe_pair_distances(listing_id, source_set_hash, test_a, test_b)`

### Final variants database

`catalog_variants.sqlite3` stores only complete selected variants.

Core tables:

- `listings`
- `listing_images`
- `listing_variants`
- `listing_variant_images`

Reserved variant fields:

- `title_text`
- `description_text`
- `price_cents`
- `currency`
- `metadata_json`
- `metadata_status`

## Recipe model

There are no fixed profiles. A single configurable search space defines:

- enabled state;
- minimum and maximum values;
- activation probability;
- sampling distribution;
- compatibility constraints;
- canonical defaults.

Recipe families are internal organization only:

- photometric;
- color;
- geometry;
- detail;
- canvas;
- style;
- encoding.

Initial transformations:

- brightness, contrast, saturation, sharpness, gamma, warmth, tint;
- autocontrast blend, equalize blend;
- small rotation, crop, zoom, offsets, proportional resize;
- Gaussian blur, median smoothing, unsharp mask;
- sepia blend, grayscale blend, RGB gains;
- canvas padding, sampled/light background, border, optional rounded image canvas;
- JPEG quality.

Aggressive effects remain disabled by default and are only enabled explicitly in `filter_space.json`.

## Benchmark loop

CLI target:

```powershell
python -m common.catalog_photo_control.bench `
  --listing "bijoux/O/O18" `
  --target-variants 50 `
  --max-tests 20000 `
  --max-duration-minutes 180 `
  --patience 3000
```

Execution:

1. Load and fingerprint the ordered source image set.
2. Count already selected final variants for the current source version.
3. Resume unless `--fresh-run` is explicitly requested.
4. Generate recipe proposals from three sources:
   - random exploration;
   - proven cross-listing recipes;
   - mutations around proven recipes.
5. Skip cached recipe tests for the same source-set hash.
6. Apply one canonical recipe to every source image.
7. Reject incomplete variants.
8. Compute per-image metrics and listing-level aggregates.
9. Persist every test in the benchmark database.
10. Update global and contextual recipe statistics.
11. Build the valid candidate pool.
12. Select variants using quality constraints and max-min diversity.
13. Persist complete selected variants in the final database.
14. Continue until the target or a stop condition is reached.
15. Generate one `index.html` for the run.
16. Remove unselected temporary images while retaining metrics.

Stop reasons:

- `target_reached`
- `max_tests_reached`
- `max_duration_reached`
- `patience_exhausted`
- `interrupted`
- `error`

## Cross-listing recipe learning

The benchmark database records whether each recipe was:

- tested;
- complete;
- quality-valid;
- eligible for selection;
- selected in the final catalog.

`recipe_global_stats` stores aggregated counts and smoothed confidence scores.

`recipe_context_stats` stores performance for coarse listing contexts, initially based on:

- image count bucket;
- dominant aspect-ratio bucket;
- average brightness bucket;
- background-lightness bucket;
- average contrast bucket.

Proposal allocation is configurable and must retain exploration. Initial defaults:

- 50% random exploration;
- 30% proven recipes;
- 20% mutations of proven recipes.

These values are not profiles and may be changed from configuration or CLI.

## Selection rules

Selection is performed at complete-listing level, not individual-image level.

A candidate must satisfy:

- full source-image coverage;
- valid output files;
- minimum quality thresholds;
- no forbidden transformation combination;
- deterministic recipe and source fingerprints.

Among eligible candidates, selection uses max-min diversity:

1. choose the highest-quality eligible seed;
2. for each remaining candidate, compute its minimum distance to selected variants;
3. choose the candidate with the largest minimum distance;
4. continue until the target is reached or no eligible candidate remains.

Distance components remain inspectable separately. The implementation must not hide all behavior behind one opaque score.

## HTML report

Each run creates only `index.html`.

The report shows:

- run status and stop reason;
- requested and obtained variant counts;
- tested, cached, valid, rejected, and selected counts;
- source listing summary;
- one card per selected complete variant;
- every image in that variant, in source order;
- recipe parameters;
- quality metrics;
- distance from original;
- minimum distance from other selected variants;
- placeholders for title, description, price, and metadata status;
- links to local variant folders.

Rounded corners are presentation-only CSS by default. Baking rounded corners into exported images remains an optional canvas transformation.

## Refactor phases and commits

### Phase 0 — inventory and guardrails

Commit: `Document current architecture and add smoke-test harness`

- inventory active entry points and imports;
- add minimal test fixtures using synthetic images;
- assert current generated artifacts remain ignored;
- record the legacy modules scheduled for deletion.

Acceptance:

- tests can run without real catalog data;
- no generated artifact is committed.

### Phase 1 — remove obsolete metadata and report paths

Commit: `Remove obsolete metadata and report pipelines`

- remove old metadata modules and imports;
- keep only `metadata_privacy_two_images.py` at repository root;
- remove legacy HTML/archive/cluster/library output code that will be superseded;
- keep reusable image operations temporarily where necessary.

Acceptance:

- package imports cleanly;
- no legacy metadata module import remains;
- no old report generator is called by the new path.

### Phase 2 — introduce clean configuration and models

Commit: `Add canonical recipe and source models`

- add dataclasses/types for source listings, recipes, tests, variants, and runs;
- add canonical JSON and hashing;
- add `config/filter_space.json` validation;
- add compatibility-rule validation.

Acceptance:

- equivalent recipes produce identical hashes;
- invalid ranges and incompatible settings fail early.

### Phase 3 — create the two database layers

Commit: `Add benchmark and final variant databases`

- implement schema initialization;
- implement typed repositories;
- add foreign keys, uniqueness constraints, and indexes;
- prepare text, price, currency, and metadata fields;
- add transaction boundaries.

Acceptance:

- both empty databases initialize from one command;
- duplicate tests are prevented;
- incomplete variants cannot be committed as ready.

### Phase 4 — implement the unified image pipeline

Commit: `Add deterministic complete-listing image pipeline`

- normalize input orientation for rendering;
- implement transformations in a documented fixed order;
- apply one recipe to every image;
- use a temporary directory and atomic finalization;
- calculate output hashes.

Acceptance:

- every image receives the same recipe;
- failures roll back the whole variant;
- outputs are reproducible with recipe and source hashes.

### Phase 5 — implement metrics and benchmark history

Commit: `Persist per-image and listing benchmark results`

- compute per-image quality and distance metrics;
- aggregate listing-level results;
- store every new test;
- reuse cached tests;
- clean rejected temporary images.

Acceptance:

- rerunning the same recipe uses cache;
- source image changes invalidate only the affected source-set version;
- rejected tests remain queryable without retaining full outputs.

### Phase 6 — add cross-listing recipe learning

Commit: `Add reusable cross-listing recipe statistics`

- aggregate global recipe performance;
- derive listing-context buckets;
- rank proven recipes with smoothed confidence;
- generate bounded mutations;
- preserve configurable random exploration.

Acceptance:

- one successful test cannot dominate rankings;
- previously successful recipes can seed a new listing;
- new recipes continue to be explored.

### Phase 7 — add final max-min selection

Commit: `Select complete variants by quality and diversity`

- build eligible candidate pool;
- calculate candidate-to-selected distances lazily;
- avoid unconditional all-pairs storage;
- write selected complete variants to the final database;
- support resume and target counts.

Acceptance:

- selected variants satisfy full coverage;
- selection stops at target count;
- existing final variants count toward the target;
- no endless loop is possible.

### Phase 8 — add the single HTML report and CLI

Commit: `Add unified bench CLI and single HTML report`

- implement `bench.py` orchestration;
- add stop conditions and progress summaries;
- create one `index.html`;
- expose links to selected variant folders;
- remove remaining legacy entry-point references.

Acceptance:

- one command runs the pipeline;
- exactly one HTML file is generated per run;
- all selected photos are visible in source order;
- run status and stop reason are explicit.

### Phase 9 — cleanup, documentation, and migration removal

Commit: `Finalize clean bench architecture`

- delete obsolete modules after import audit;
- simplify dependencies;
- update README and PowerShell examples;
- add schema and CLI documentation;
- run full smoke tests;
- compare branch against `main` before merge.

Acceptance:

- no dead imports;
- no duplicate DB implementation;
- no duplicate HTML generator;
- no required legacy database or result directory;
- repository starts clean after cloning.

## Test matrix

Required automated tests:

- recipe canonicalization and hashing;
- filter-space validation;
- compatibility constraints;
- source-set hashing and invalidation;
- complete coverage enforcement;
- same-recipe enforcement across images;
- atomic rollback on one-image failure;
- benchmark test deduplication;
- cache reuse;
- global-stat smoothing;
- mutation bounds;
- max-min selector behavior;
- target-count resume;
- every stop condition;
- final database rejects incomplete variants;
- exactly one generated HTML per run;
- report contains every selected image;
- generated artifacts remain ignored by Git.

## Working method

- All implementation work stays on `refactor/clean-bench-pipeline`.
- One phase equals one reviewable commit whenever practical.
- Do not perform a repository-wide rewrite in one commit.
- Before each destructive deletion, remove imports and prove the replacement path with tests.
- Do not merge into `main` until the final smoke test passes and the branch diff has been reviewed.

## Post-refactor fidelity and purge hardening

The default search envelope is intentionally subtle: at most four active
parameters, bounded normalized recipe intensity, and per-image SSIM, MAE,
sharpness, and clipping barriers. Cache identity includes the complete effective
evaluation configuration. `common.catalog_photo_control.purge` is the sole
command for scoped or global removal of pipeline-managed results.
