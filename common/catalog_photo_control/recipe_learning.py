from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, ImageStat

from .models import Recipe, SourceListing


def _bucket(value: float, low: float, high: float, labels: tuple[str, str, str]) -> str:
    return labels[0] if value < low else labels[2] if value >= high else labels[1]


def listing_context_key(listing: SourceListing) -> str:
    count = len(listing.images)
    count_bucket = "few" if count <= 2 else "medium" if count <= 5 else "many"
    aspects: list[float] = []
    brightness: list[float] = []
    backgrounds: list[float] = []
    contrasts: list[float] = []
    for source in listing.images:
        aspects.append(source.width / source.height)
        with Image.open(source.path) as opened:
            gray = ImageOps.exif_transpose(opened).convert("L").resize((64, 64))
            array = np.asarray(gray, dtype=np.float32) / 255.0
            brightness.append(float(np.mean(array)))
            contrasts.append(float(np.std(array)))
            edge = np.concatenate((array[0], array[-1], array[:, 0], array[:, -1]))
            backgrounds.append(float(np.mean(edge)))
    aspect = sum(aspects) / count
    aspect_bucket = "portrait" if aspect < 0.85 else "landscape" if aspect > 1.15 else "square"
    return ":".join(
        (
            count_bucket,
            aspect_bucket,
            _bucket(sum(brightness) / count, 0.35, 0.7, ("dark", "mid", "bright")),
            _bucket(sum(backgrounds) / count, 0.4, 0.75, ("darkbg", "mixedbg", "lightbg")),
            _bucket(sum(contrasts) / count, 0.12, 0.25, ("lowcontrast", "midcontrast", "highcontrast")),
        )
    )


def smoothed_confidence(eligible: int, tested: int) -> float:
    # Beta(1, 3) prior: one success scores 0.4 rather than appearing perfect.
    return (eligible + 1.0) / (tested + 4.0)


def refresh_recipe_statistics(connection: sqlite3.Connection, recipe_id: int) -> None:
    row = connection.execute(
        """SELECT COUNT(*), COALESCE(SUM(complete), 0),
                  COALESCE(SUM(quality_valid), 0), COALESCE(SUM(eligible), 0),
                  COALESCE(SUM(selected), 0)
           FROM recipe_tests WHERE recipe_id=?""",
        (recipe_id,),
    ).fetchone()
    tested, complete, quality, eligible, selected = map(int, row)
    confidence = smoothed_confidence(eligible, tested)
    with connection:
        connection.execute(
            """INSERT INTO recipe_global_stats
               (recipe_id, tested_count, complete_count, quality_valid_count,
                eligible_count, selected_count, confidence_score, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(recipe_id) DO UPDATE SET
                 tested_count=excluded.tested_count,
                 complete_count=excluded.complete_count,
                 quality_valid_count=excluded.quality_valid_count,
                 eligible_count=excluded.eligible_count,
                 selected_count=excluded.selected_count,
                 confidence_score=excluded.confidence_score,
                 updated_at=CURRENT_TIMESTAMP""",
            (recipe_id, tested, complete, quality, eligible, selected, confidence),
        )
        connection.execute("DELETE FROM recipe_context_stats WHERE recipe_id=?", (recipe_id,))
        contexts = connection.execute(
            """SELECT context_key, COUNT(*), COALESCE(SUM(complete), 0),
                      COALESCE(SUM(quality_valid), 0), COALESCE(SUM(eligible), 0),
                      COALESCE(SUM(selected), 0)
               FROM recipe_tests WHERE recipe_id=? GROUP BY context_key""",
            (recipe_id,),
        ).fetchall()
        connection.executemany(
            """INSERT INTO recipe_context_stats
               (recipe_id, context_key, tested_count, complete_count,
                quality_valid_count, eligible_count, selected_count,
                confidence_score, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            [
                (
                    recipe_id,
                    context,
                    tested_count,
                    complete_count,
                    quality_count,
                    eligible_count,
                    selected_count,
                    smoothed_confidence(int(eligible_count), int(tested_count)),
                )
                for context, tested_count, complete_count, quality_count, eligible_count, selected_count in contexts
            ],
        )


def proven_recipes(
    connection: sqlite3.Connection, context_key: str | None = None, limit: int = 100
) -> list[Recipe]:
    if context_key:
        rows = connection.execute(
            """SELECT r.parameters_json FROM recipes r
               JOIN recipe_global_stats g USING(recipe_id)
               LEFT JOIN recipe_context_stats c
                 ON c.recipe_id=r.recipe_id AND c.context_key=?
               WHERE g.eligible_count > 0
               ORDER BY COALESCE(c.confidence_score, g.confidence_score) DESC,
                        g.tested_count DESC, r.recipe_hash
               LIMIT ?""",
            (context_key, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT r.parameters_json FROM recipes r
               JOIN recipe_global_stats g USING(recipe_id)
               WHERE g.eligible_count > 0
               ORDER BY g.confidence_score DESC, g.tested_count DESC, r.recipe_hash
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [Recipe.from_parameters(json.loads(row[0])) for row in rows]
