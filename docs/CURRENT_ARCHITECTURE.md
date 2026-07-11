# Current architecture inventory

This inventory records the pre-refactor architecture at the start of phase 0. It
is intentionally descriptive: the clean pipeline introduced by later phases is
defined in `docs/REFACTOR_PLAN.md`.

## Active entry points

The repository currently exposes module CLIs rather than one supported command:

- `benchmarks.py` / `photo_quality_control.py`: legacy quality-control runner;
- `client_render_sampler.py` and `run_bench_sequence.py`: legacy benchmark and
  rendering flows;
- `listing_photo_review.py` and `decision_margin_search.py`: review/search flows;
- `init_catalog_db.py`, `ingest_annonces.py`, `catalog_db_summary.py`,
  `import_diverse_filters_to_db.py`, and `final_catalog_ops.py`: shared-catalog
  database workflows;
- `filter_library_builder.py`, `filter_cluster_builder.py`,
  `diverse_target_selector.py`, and `rebuild_target_filter_archive.py`: derived
  library/archive workflows;
- `metadata_policy.py` and `image_metadata_inspector.py`: metadata workflows.

PowerShell helpers under `scripts/` set up the environment, start the legacy
PostgreSQL service, clean generated files, and build debug archives.

## Current dependency shape

`common.catalog_photo_control` is the only Python package. The rendering and
selection entry points import reusable operations from `photo_adjustments.py`,
`photo_comparison_rules.py`, and `listing_photo_review.py`, while also importing
legacy report/archive modules directly. The shared catalog path is coupled to
`catalog_config.py`, `catalog_db.py`, and `catalog_schema.sql`. Strategy modules
under `strategy/` are used by `client_render_sampler.py`.

Runtime dependencies are Pillow, NumPy, ImageHash, scikit-image, defusedxml,
psycopg, python-dotenv, and piexif. There was no automated test directory before
this phase.

## Read-only and generated-data boundaries

External listing directories are inputs and must remain read-only. Tests use
only images generated under pytest temporary directories. All databases,
rendered images, reports, logs, archives, and benchmark work belong under
`local/`, which is ignored by Git together with database, archive, and log file
extensions.

## Legacy modules scheduled for deletion

Later phases remove these paths after their replacement imports are proven:

- metadata: `photo_metadata.py`, `metadata_policy.py`, and
  `image_metadata_inspector.py` (replaced only by the root
  `metadata_privacy_two_images.py` utility);
- reports and archives: `bench_filter_archive.py`, `bench_terminal_summary.py`,
  `rebuild_target_filter_archive.py`, and debug/archive report code embedded in
  legacy runners;
- libraries and clusters: `filter_library_builder.py`,
  `filter_cluster_builder.py`, and `diverse_target_selector.py`;
- duplicate pipelines and entry points: `benchmarks.py`,
  `client_render_sampler.py`, `run_bench_sequence.py`,
  `photo_quality_control.py`, `decision_margin_search.py`, and the legacy shared
  catalog ingestion/export commands and database implementation;
- legacy strategy implementations under `strategy/` once the configurable
  recipe generator replaces them.

Reusable image operations may remain temporarily until the unified image
pipeline is validated. Deletion must follow import removal, never precede it.
