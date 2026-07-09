from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageOps

from .catalog_config import load_settings
from .catalog_db import CatalogDb, canonical_json, init_schema, open_catalog_db, stable_id, utc_now
from .client_render_sampler import apply_recipe
from .listing_photo_review import _safe_listing_code


DEFAULT_EXPORT_ROOT = Path("local") / "generated_annonces"


@dataclass(frozen=True)
class ExportedAnnonceFilter:
    annonce_key: str
    annonce_id: str
    candidate_id: str
    recipe_id: str
    family_key: str
    labels: str
    matches: int
    image_count: int
    max_score: float | None
    avg_score: float | None
    output_dir: Path
    output_paths: tuple[Path, ...]
    marked_used: bool
    selection_rank: int | None


def _row_first(row: Any) -> Any:
    if row is None:
        return None
    return row[0]


def _row_values(row: Any) -> tuple[Any, ...]:
    if row is None:
        return ()
    return tuple(row)


def _json_loads_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return loaded
    return {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _as_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _get_annonce(db: CatalogDb, annonce_key: str) -> dict[str, Any]:
    row = db.execute(
        f"SELECT annonce_id, annonce_key, source_dir, image_count FROM annonces WHERE annonce_key={db.placeholder}",
        [annonce_key],
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Annonce introuvable en DB: {annonce_key}. Lance ingest_annonces avant export.")
    annonce_id, key, source_dir, image_count = _row_values(row)
    return {
        "annonce_id": str(annonce_id),
        "annonce_key": str(key),
        "source_dir": str(source_dir),
        "image_count": _as_int(image_count),
    }


def _get_annonce_images(db: CatalogDb, annonce_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        f"""
        SELECT image_id, image_index, source_path, sha256
        FROM annonce_images
        WHERE annonce_id={db.placeholder} AND status='active'
        ORDER BY image_index ASC
        """,
        [annonce_id],
    ).fetchall()
    images: list[dict[str, Any]] = []
    for row in rows:
        image_id, image_index, source_path, sha256 = _row_values(row)
        images.append(
            {
                "image_id": str(image_id),
                "image_index": _as_int(image_index),
                "source_path": Path(str(source_path)),
                "sha256": str(sha256),
            }
        )
    if not images:
        raise RuntimeError(f"Aucune image active trouvee en DB pour annonce_id={annonce_id}")

    expected = list(range(len(images)))
    found = [int(item["image_index"]) for item in images]
    if found != expected:
        raise RuntimeError(f"Images annonce non contigues en DB. Trouve: {found}. Attendu: {expected}")

    missing_files = [str(item["source_path"]) for item in images if not item["source_path"].exists()]
    if missing_files:
        raise RuntimeError("Images source introuvables sur ce PC: " + ", ".join(missing_files))

    return images


def _candidate_where_clause(db: CatalogDb, *, require_full_image_coverage: bool, candidate_id: str | None) -> tuple[str, list[Any]]:
    clauses = ["c.status='available'", "c.selected_at IS NULL"]
    params: list[Any] = []
    if require_full_image_coverage:
        clauses.append("c.matches >= a.image_count")
    if candidate_id:
        clauses.append(f"c.candidate_id={db.placeholder}")
        params.append(candidate_id)
    return " AND ".join(clauses), params


def select_next_filter_candidate(
    db: CatalogDb,
    *,
    annonce_key: str,
    require_full_image_coverage: bool = True,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Select the next usable filter candidate for one annonce.

    Default behavior requires candidate.matches >= annonce.image_count so the
    selected recipe has target coverage across the whole annonce, not just one
    isolated image.
    """
    where_extra, extra_params = _candidate_where_clause(
        db,
        require_full_image_coverage=require_full_image_coverage,
        candidate_id=candidate_id,
    )
    sql = f"""
        SELECT
            a.annonce_id,
            a.annonce_key,
            a.image_count,
            c.candidate_id,
            c.recipe_id,
            c.run_id,
            c.family_key,
            c.labels,
            c.matches,
            c.suspect_matches,
            c.review_matches,
            c.review_candidate_matches,
            c.max_score,
            c.avg_score,
            c.score_json,
            r.params_json
        FROM annonce_filter_candidates c
        JOIN annonces a ON a.annonce_id = c.annonce_id
        JOIN filter_recipes r ON r.recipe_id = c.recipe_id
        WHERE a.annonce_key={db.placeholder} AND {where_extra}
        ORDER BY
            c.suspect_matches DESC,
            c.review_matches DESC,
            c.max_score DESC,
            c.matches DESC,
            c.avg_score DESC,
            c.created_at ASC,
            c.candidate_id ASC
        LIMIT 1
    """
    row = db.execute(sql, [annonce_key, *extra_params]).fetchone()
    if row is None:
        coverage_note = " avec couverture complete" if require_full_image_coverage else ""
        raise RuntimeError(f"Aucun filtre disponible{coverage_note} pour {annonce_key}")

    (
        annonce_id,
        resolved_key,
        image_count,
        selected_candidate_id,
        recipe_id,
        run_id,
        family_key,
        labels,
        matches,
        suspect_matches,
        review_matches,
        review_candidate_matches,
        max_score,
        avg_score,
        score_json,
        params_json,
    ) = _row_values(row)

    return {
        "annonce_id": str(annonce_id),
        "annonce_key": str(resolved_key),
        "image_count": _as_int(image_count),
        "candidate_id": str(selected_candidate_id),
        "recipe_id": str(recipe_id),
        "run_id": str(run_id) if run_id is not None else "",
        "family_key": str(family_key or ""),
        "labels": str(labels or ""),
        "matches": _as_int(matches),
        "suspect_matches": _as_int(suspect_matches),
        "review_matches": _as_int(review_matches),
        "review_candidate_matches": _as_int(review_candidate_matches),
        "max_score": _as_float_or_none(max_score),
        "avg_score": _as_float_or_none(avg_score),
        "score": _json_loads_dict(score_json),
        "params": _json_loads_dict(params_json),
    }


def _next_selection_rank(db: CatalogDb, annonce_id: str) -> int:
    row = db.execute(
        f"SELECT COALESCE(MAX(selection_rank), 0) FROM annonce_filter_selections WHERE annonce_id={db.placeholder}",
        [annonce_id],
    ).fetchone()
    return _as_int(_row_first(row)) + 1


def _write_contact_sheet(images: list[dict[str, Any]], output_paths: Sequence[Path], path: Path) -> None:
    thumb_w, thumb_h, gap, label_h = 220, 220, 14, 42
    rows = len(images)
    sheet = Image.new("RGB", (thumb_w * 2 + gap * 3, rows * (thumb_h + label_h + gap) + gap), "white")
    draw = ImageDraw.Draw(sheet)

    def thumb(image_path: Path) -> Image.Image:
        with Image.open(image_path) as image:
            img = ImageOps.exif_transpose(image).convert("RGB")
        img.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (thumb_w, thumb_h), "white")
        canvas.paste(img, ((thumb_w - img.width) // 2, (thumb_h - img.height) // 2))
        return canvas

    for idx, (source, out_path) in enumerate(zip(images, output_paths)):
        y = gap + idx * (thumb_h + label_h + gap)
        draw.text((gap, y), f"{source['image_index']}. source", fill=(0, 0, 0))
        draw.text((thumb_w + gap * 2, y), f"{source['image_index']}. export", fill=(0, 0, 0))
        sheet.paste(thumb(source["source_path"]), (gap, y + label_h))
        sheet.paste(thumb(out_path), (thumb_w + gap * 2, y + label_h))

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="JPEG", quality=90, optimize=True)


def _insert_selection(
    db: CatalogDb,
    *,
    annonce_id: str,
    candidate_id: str,
    selection_rank: int,
    output_dir: Path,
    metadata: dict[str, Any],
) -> str:
    selected_at = utc_now()
    selection_id = stable_id("sel", annonce_id, candidate_id, selection_rank)
    ph = db.placeholder
    db.execute(
        f"""
        INSERT INTO annonce_filter_selections (
            selection_id,
            annonce_id,
            candidate_id,
            selection_rank,
            selection_reason,
            min_distance_to_previous,
            avg_distance_to_previous,
            original_delta_avg,
            original_delta_min,
            output_dir,
            selected_at,
            status,
            metadata_json
        ) VALUES ({','.join([ph] * 13)})
        """,
        [
            selection_id,
            annonce_id,
            candidate_id,
            selection_rank,
            "first_available_highest_priority_full_coverage",
            None,
            None,
            None,
            None,
            str(output_dir),
            selected_at,
            "exported",
            canonical_json(metadata),
        ],
    )
    db.execute(
        f"UPDATE annonce_filter_candidates SET status='used', selected_at={ph}, updated_at={ph} WHERE candidate_id={ph}",
        [selected_at, selected_at, candidate_id],
    )
    return selection_id


def export_next_annonce_filter(
    *,
    annonce_key: str,
    output_root: Path = DEFAULT_EXPORT_ROOT,
    db_dsn: str | None = None,
    candidate_id: str | None = None,
    require_full_image_coverage: bool = True,
    mark_used: bool = True,
    init_db: bool = True,
    clean_existing_output: bool = False,
) -> ExportedAnnonceFilter:
    """Select one available filter, apply it to every numbered image, and export.

    The same recipe params are applied to all images of the annonce. By default
    the candidate is marked used and a row is inserted into
    annonce_filter_selections.
    """
    settings = load_settings(db_dsn=db_dsn)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with open_catalog_db(settings) as db:
        if init_db:
            init_schema(db)

        annonce = _get_annonce(db, annonce_key)
        images = _get_annonce_images(db, annonce["annonce_id"])
        candidate = select_next_filter_candidate(
            db,
            annonce_key=annonce_key,
            require_full_image_coverage=require_full_image_coverage,
            candidate_id=candidate_id,
        )
        params = candidate["params"]
        if not params:
            raise RuntimeError(f"Params vides pour candidate_id={candidate['candidate_id']}")

        output_dir = Path(output_root) / _safe_listing_code(annonce_key) / f"{timestamp}_{candidate['candidate_id']}"
        if clean_existing_output and output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        output_paths: list[Path] = []
        for image in images:
            output_path = output_dir / f"{image['image_index']}.jpg"
            apply_recipe(image["source_path"], output_path, params)
            output_paths.append(output_path)

        metadata = {
            "annonce": annonce,
            "candidate": candidate,
            "source_images": [
                {
                    "image_id": image["image_id"],
                    "image_index": image["image_index"],
                    "source_path": str(image["source_path"]),
                    "sha256": image["sha256"],
                }
                for image in images
            ],
            "output_paths": [str(path) for path in output_paths],
            "mark_used": mark_used,
            "require_full_image_coverage": require_full_image_coverage,
        }

        (output_dir / "selected_filter.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_contact_sheet(images, output_paths, output_dir / "before_after.jpg")

        selection_rank: int | None = None
        if mark_used:
            selection_rank = _next_selection_rank(db, annonce["annonce_id"])
            selection_id = _insert_selection(
                db,
                annonce_id=annonce["annonce_id"],
                candidate_id=candidate["candidate_id"],
                selection_rank=selection_rank,
                output_dir=output_dir,
                metadata={**metadata, "selection_rank": selection_rank},
            )
            metadata["selection_id"] = selection_id
            metadata["selection_rank"] = selection_rank
            (output_dir / "selected_filter.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return ExportedAnnonceFilter(
            annonce_key=annonce_key,
            annonce_id=annonce["annonce_id"],
            candidate_id=candidate["candidate_id"],
            recipe_id=candidate["recipe_id"],
            family_key=candidate["family_key"],
            labels=candidate["labels"],
            matches=int(candidate["matches"]),
            image_count=int(annonce["image_count"]),
            max_score=candidate["max_score"],
            avg_score=candidate["avg_score"],
            output_dir=output_dir,
            output_paths=tuple(output_paths),
            marked_used=mark_used,
            selection_rank=selection_rank,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Selectionne et exporte le prochain filtre final d'une annonce.")
    parser.add_argument("--annonce-key", required=True, help="Cle annonce en DB, ex: bijoux/O/O18")
    parser.add_argument("--output-root", default=str(DEFAULT_EXPORT_ROOT), help="Dossier de sortie des exports finaux.")
    parser.add_argument("--candidate-id", default=None, help="Forcer un candidate_id precis au lieu du prochain disponible.")
    parser.add_argument("--db-dsn", default=None, help="Override CATALOG_DB_DSN pour ce run.")
    parser.add_argument("--allow-partial", action="store_true", help="Autoriser un filtre qui ne match pas toutes les images de l'annonce.")
    parser.add_argument("--no-mark-used", action="store_true", help="Exporter sans marquer le filtre comme utilise en DB.")
    parser.add_argument("--no-init-db", action="store_true", help="Ne pas initialiser le schema DB avant export.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = export_next_annonce_filter(
        annonce_key=args.annonce_key,
        output_root=Path(args.output_root),
        db_dsn=args.db_dsn,
        candidate_id=args.candidate_id,
        require_full_image_coverage=not args.allow_partial,
        mark_used=not args.no_mark_used,
        init_db=not args.no_init_db,
    )

    print("FINAL ANNONCE EXPORT READY")
    print(f"annonce_key: {result.annonce_key}")
    print(f"candidate_id: {result.candidate_id}")
    print(f"recipe_id: {result.recipe_id}")
    print(f"labels: {result.labels}")
    print(f"matches: {result.matches}/{result.image_count}")
    if result.max_score is not None:
        print(f"max_score: {result.max_score:.6f}")
    if result.avg_score is not None:
        print(f"avg_score: {result.avg_score:.6f}")
    print(f"family_key: {result.family_key}")
    print(f"marked_used: {result.marked_used}")
    if result.selection_rank is not None:
        print(f"selection_rank: {result.selection_rank}")
    print(f"output_dir: {result.output_dir}")
    print(f"before_after: {result.output_dir / 'before_after.jpg'}")
    print(f"selected_filter_json: {result.output_dir / 'selected_filter.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
