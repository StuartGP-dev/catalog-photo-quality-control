-- Catalog filter engine schema.
-- The Python DB layer creates the same tables for SQLite smoke tests and PostgreSQL shared runs.
-- PostgreSQL is the intended shared backend between multiple PCs.

CREATE TABLE IF NOT EXISTS annonces (
    annonce_id TEXT PRIMARY KEY,
    annonce_key TEXT NOT NULL UNIQUE,
    source_dir TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
);

CREATE TABLE IF NOT EXISTS filter_recipes (
    recipe_id TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    family_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
);

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
);
