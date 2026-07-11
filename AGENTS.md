# Codex instructions for this repository

## Mission

Refactor the repository according to `docs/REFACTOR_PLAN.md` on branch `refactor/clean-bench-pipeline`.

Work autonomously through every phase of the plan. Implement, test, commit, and push one coherent phase at a time. Do not stop after analysis or planning.

## Required workflow

1. Start with:
   - `git status --short`
   - `git branch --show-current`
   - `git log -5 --oneline`
2. If unrelated local changes exist, do not overwrite them; stop and report precisely.
3. Work only on `refactor/clean-bench-pipeline`.
4. Read `docs/REFACTOR_PLAN.md` completely before editing code.
5. Follow the phases and acceptance criteria in that document.
6. Before every commit:
   - inspect `git diff`;
   - run relevant tests;
   - verify that no database, generated image, archive, log, or `local/` artifact is staged.
7. After each validated phase, create a reviewable commit and push it to `origin/refactor/clean-bench-pipeline`.
8. Do not merge the pull request and do not rewrite Git history.

## Non-negotiable invariants

- Never modify or delete user source listings or source images.
- External listing directories are read-only.
- Automated tests use only synthetic images and temporary directories.
- Generated artifacts stay under `local/` and remain ignored by Git.
- A variant is valid only when every active source image was processed successfully.
- Every image in one variant uses the same canonical recipe.
- Recipe hashes and ordered source-set hashes are deterministic.
- A recipe already tested on the same source-set version is reused from cache unless explicitly forced.
- Historical benchmark data and final selected variants use separate databases.
- One benchmark run produces exactly one user-facing HTML file: `index.html`.
- Variant creation is atomic: one image failure invalidates the whole variant.
- Do not introduce fixed filter profiles. Use the single configurable `config/filter_space.json` search space.
- Metadata work in this refactor is limited to removing obsolete modules and reserving future schema fields.

## Target result

The completed project must provide:

- one benchmark CLI;
- `local/databases/catalog_bench.sqlite3` for all tested recipes and per-image results;
- `local/databases/catalog_variants.sqlite3` for complete selected variants only;
- resumable execution up to `--target-variants`;
- stop limits for tests, time, and patience;
- random exploration, proven cross-listing recipes, and bounded mutations;
- quality filtering and max-min diversity selection at complete-listing level;
- one `index.html` per run showing all images of every selected variant;
- reserved title, description, price, currency, and metadata fields;
- cleanup of obsolete metadata, report, archive, cluster, and duplicate pipeline modules.

## Validation

After each phase, run at least:

```powershell
python -m compileall common
python -m pytest -q
```

Adapt only if the repository uses a different existing test command.

At the end:

- run the full test suite;
- run a complete synthetic smoke test in a temporary directory;
- verify both SQLite databases;
- verify cache reuse on a second run;
- verify exactly one HTML file is generated;
- verify every selected variant contains all source images;
- verify `git status` is clean;
- compare the branch against `main`;
- push the final commits;
- report the final branch, HEAD, commits, files added/removed, tests, smoke-test results, remaining limits, and working-tree state.
