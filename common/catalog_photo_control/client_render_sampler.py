from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .listing_photo_review import _safe_listing_code, find_listing_images, resolve_listing_dir
from .local_paths import default_annonces_root, default_output_root, describe_local_paths
from .strategy import available_strategies, create_strategy, parse_options
from .bench_local_evaluator import evaluate_bench_output, ensure_bench_columns, summarize_bench_evaluator
from .bench_terminal_summary import format_bench_terminal_summary
from .bench_filter_archive import format_filter_archive_summary, write_filter_archive

DB_VERSION = 1
PIPELINE_VERSION = 2
DEFAULT_BATCH_SAMPLES = 40
DURATION_MODE_SAMPLES = 1_000_000

PROFILES: dict[str, dict[str, tuple[float, float, float] | tuple[int, int, int]]] = {
    "natural": {
        "brightness": (0.94, 1.08, 1.0),
        "contrast": (0.94, 1.10, 1.0),
        "saturation": (0.94, 1.10, 1.0),
        "sharpness": (0.94, 1.14, 1.0),
        "warmth": (-0.035, 0.035, 0.0),
        "angle": (-1.2, 1.2, 0.0),
        "crop": (0.0, 0.022, 0.004),
        "blur": (0.0, 0.35, 0.0),
        "quality": (82, 96, 92),
        "zoom": (0.94, 1.06, 1.0),
        "canvas_pad": (0.0, 0.045, 0.0),
        "canvas_gray": (236, 255, 255),
        "canvas_auto": (0, 1, 0),
    },
    "client_wide": {
        "brightness": (0.88, 1.14, 1.0),
        "contrast": (0.88, 1.16, 1.0),
        "saturation": (0.88, 1.16, 1.0),
        "sharpness": (0.88, 1.22, 1.0),
        "warmth": (-0.06, 0.06, 0.0),
        "angle": (-2.2, 2.2, 0.0),
        "crop": (0.0, 0.035, 0.006),
        "blur": (0.0, 0.55, 0.0),
        "quality": (74, 96, 90),
        "zoom": (0.92, 1.08, 1.0),
        "canvas_pad": (0.0, 0.060, 0.0),
        "canvas_gray": (230, 255, 255),
        "canvas_auto": (0, 1, 0),
    },
    "studio_wide": {
        "brightness": (0.84, 1.18, 1.0),
        "contrast": (0.84, 1.20, 1.0),
        "saturation": (0.84, 1.20, 1.0),
        "sharpness": (0.84, 1.30, 1.0),
        "warmth": (-0.08, 0.08, 0.0),
        "angle": (-3.0, 3.0, 0.0),
        "crop": (0.0, 0.050, 0.008),
        "blur": (0.0, 0.75, 0.0),
        "quality": (68, 96, 88),
        "zoom": (0.90, 1.10, 1.0),
        "canvas_pad": (0.0, 0.075, 0.0),
        "canvas_gray": (224, 255, 255),
        "canvas_auto": (0, 1, 0),
    },
}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stable_id(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _db_path(output_root: Path, listing_code: str) -> Path:
    return output_root / "_client_render_sampler" / f"{_safe_listing_code(listing_code)}.sqlite3"


def _connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recipes("
        "recipe_id TEXT PRIMARY KEY, listing_code TEXT NOT NULL, profile TEXT NOT NULL, "
        "params_json TEXT NOT NULL, seed INTEGER NOT NULL, created_at TEXT NOT NULL, output_dir TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outputs("
        "output_id TEXT PRIMARY KEY, recipe_id TEXT NOT NULL, source_image TEXT NOT NULL, output_path TEXT NOT NULL, "
        "luma_delta REAL NOT NULL, contrast_delta REAL NOT NULL, saturation_delta REAL NOT NULL, detail_delta REAL NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recipes_scope ON recipes(listing_code, profile)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outputs_recipe ON outputs(recipe_id)")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (str(DB_VERSION),))
    conn.commit()
    return conn


def _known_recipes(conn: sqlite3.Connection, listing_code: str, profile: str) -> set[str]:
    rows = conn.execute("SELECT recipe_id FROM recipes WHERE listing_code=? AND profile=?", (listing_code, profile)).fetchall()
    return {str(row[0]) for row in rows}


def _recipe_id(listing_code: str, profile: str, params: dict[str, Any]) -> str:
    return _stable_id({"kind": "client_render_recipe", "pipeline": PIPELINE_VERSION, "listing": listing_code, "profile": profile, "params": params})


def _crop_keep_size(image: Image.Image, pct: float) -> Image.Image:
    if pct <= 0:
        return image
    w, h = image.size
    dx, dy = max(1, int(w * pct)), max(1, int(h * pct))
    if w - 2 * dx < 8 or h - 2 * dy < 8:
        return image
    return image.crop((dx, dy, w - dx, h - dy)).resize((w, h), Image.Resampling.LANCZOS)


def _coerce_fill_color(value: int | tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(value, tuple):
        r, g, b = value
        return (
            max(0, min(255, int(r))),
            max(0, min(255, int(g))),
            max(0, min(255, int(b))),
        )
    g = max(0, min(255, int(value)))
    return (g, g, g)


def _rotate_keep_size(
    image: Image.Image,
    angle: float,
    fill_color: int | tuple[int, int, int] = 255,
) -> Image.Image:
    if abs(angle) < 0.01:
        return image

    fill = _coerce_fill_color(fill_color)
    rotated = image.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=fill,
    )
    return ImageOps.fit(
        rotated,
        image.size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _canvas_transform(
    image: Image.Image,
    zoom: float = 1.0,
    canvas_pad: float = 0.0,
    canvas_gray: int = 255,
    fill_color: int | tuple[int, int, int] | None = None,
) -> Image.Image:
    w, h = image.size
    zoom = max(0.5, min(1.5, float(zoom)))
    canvas_pad = max(0.0, min(0.20, float(canvas_pad)))

    fill = _coerce_fill_color(canvas_gray if fill_color is None else fill_color)

    inner_w = max(8, int(round(w * (1.0 - 2.0 * canvas_pad))))
    inner_h = max(8, int(round(h * (1.0 - 2.0 * canvas_pad))))

    if abs(zoom - 1.0) > 0.0001:
        scaled = image.resize(
            (
                max(8, int(round(inner_w * zoom))),
                max(8, int(round(inner_h * zoom))),
            ),
            Image.Resampling.LANCZOS,
        )
    else:
        scaled = image

    if scaled.width > inner_w or scaled.height > inner_h:
        inner = ImageOps.fit(
            scaled,
            (inner_w, inner_h),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    else:
        inner = Image.new("RGB", (inner_w, inner_h), fill)
        inner.paste(scaled, ((inner_w - scaled.width) // 2, (inner_h - scaled.height) // 2))

    canvas = Image.new("RGB", (w, h), fill)
    canvas.paste(inner, ((w - inner_w) // 2, (h - inner_h) // 2))
    return canvas


def _sample_background_color(image: Image.Image, fallback_gray: int = 255, border_pct: float = 0.08) -> tuple[int, int, int]:
    rgb = np.asarray(image.convert("RGB")).astype(np.float32)
    h, w = rgb.shape[:2]
    band_x = max(1, int(round(w * border_pct)))
    band_y = max(1, int(round(h * border_pct)))

    samples = [
        rgb[:band_y, :, :].reshape(-1, 3),
        rgb[h - band_y:, :, :].reshape(-1, 3),
        rgb[:, :band_x, :].reshape(-1, 3),
        rgb[:, w - band_x:, :].reshape(-1, 3),
    ]

    pixels = np.concatenate(samples, axis=0)
    if pixels.size == 0:
        g = max(0, min(255, int(fallback_gray)))
        return (g, g, g)

    median = np.median(pixels, axis=0)
    return tuple(int(max(0, min(255, round(float(v))))) for v in median)


def _warmth(image: Image.Image, amount: float) -> Image.Image:
    if abs(amount) < 0.001:
        return image
    arr = np.asarray(image.convert("RGB")).astype(np.float32)
    arr[..., 0] *= 1.0 + amount
    arr[..., 1] *= 1.0 + amount * 0.08
    arr[..., 2] *= 1.0 - amount
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")


def apply_recipe(source_path: Path, output_path: Path, params: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as image:
        out = ImageOps.exif_transpose(image).convert("RGB")

    canvas_gray = int(params.get("canvas_gray", 255))
    canvas_auto = float(params.get("canvas_auto", 0.0)) >= 0.5

    fill_color: tuple[int, int, int] | int
    if canvas_auto:
        fill_color = _sample_background_color(out, fallback_gray=canvas_gray)
    else:
        fill_color = canvas_gray

    out = _crop_keep_size(out, float(params["crop"]))
    out = _rotate_keep_size(out, float(params["angle"]), fill_color)

    out = ImageEnhance.Brightness(out).enhance(float(params["brightness"]))
    out = ImageEnhance.Contrast(out).enhance(float(params["contrast"]))
    out = ImageEnhance.Color(out).enhance(float(params["saturation"]))
    out = ImageEnhance.Sharpness(out).enhance(float(params["sharpness"]))
    out = _warmth(out, float(params["warmth"]))

    if float(params["blur"]) > 0.03:
        out = out.filter(ImageFilter.GaussianBlur(radius=float(params["blur"])))

    if float(params.get("canvas_pad", 0.0)) > 0.0001 or abs(float(params.get("zoom", 1.0)) - 1.0) > 0.0001:
        out = _canvas_transform(
            out,
            float(params.get("zoom", 1.0)),
            float(params.get("canvas_pad", 0.0)),
            canvas_gray,
            fill_color=fill_color,
        )

    out.save(
        output_path,
        format="JPEG",
        quality=max(60, min(98, int(params["quality"]))),
        optimize=True,
    )


def _metrics(path: Path) -> dict[str, float]:
    with Image.open(path) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
    arr = np.asarray(rgb).astype(np.float32)
    luma = 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
    hsv = np.asarray(rgb.convert("HSV")).astype(np.float32)
    detail = np.asarray(rgb.convert("L").filter(ImageFilter.FIND_EDGES)).astype(np.float32)
    return {
        "luma": round(float(luma.mean()), 4),
        "contrast": round(float(luma.std()), 4),
        "saturation": round(float(hsv[..., 1].mean()), 4),
        "detail": round(float(detail.mean()), 4),
    }


def _thumb(path: Path, size: tuple[int, int] = (220, 220)) -> Image.Image:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "white")
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def _contact_sheet(rows: list[dict[str, Any]], path: Path, limit: int) -> None:
    rows = rows[:limit]
    tw, th, gap, label_h = 220, 220, 12, 40
    sheet = Image.new("RGB", (tw * 2 + gap * 3, max(1, len(rows)) * (th + label_h + gap) + gap), "white")
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(rows):
        y = gap + i * (th + label_h + gap)
        sheet.paste(_thumb(Path(row["source_image"]), (tw, th)), (gap, y + label_h))
        sheet.paste(_thumb(Path(row["output_path"]), (tw, th)), (tw + gap * 2, y + label_h))
        params = row["params"]
        draw.text((gap, y), f"{i+1:02d} {Path(row['source_image']).name} | {row['recipe_id'][:10]}", fill=(0, 0, 0))
        draw.text((gap, y + 18), f"rot={params['angle']} crop={params['crop']} blur={params['blur']} q={params['quality']} zoom={params.get('zoom', 1.0)} pad={params.get('canvas_pad', 0.0)} bg={params.get('canvas_gray', 255)} auto={params.get('canvas_auto', 0)} auto={params.get('canvas_auto', 0)}", fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="JPEG", quality=90)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["output_id", "recipe_id", "source_image", "output_path", "status", "label", "verdict", "bench_score", "bench_evaluator", "bench_reasons_json", "luma_delta", "contrast_delta", "saturation_delta", "detail_delta", "params_json"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            d = row["delta"]
            writer.writerow({
                "output_id": row["output_id"],
                "recipe_id": row["recipe_id"],
                "source_image": row["source_image"],
                "output_path": row["output_path"],
                "status": row.get("status", ""),
                "label": row.get("label", ""),
                "verdict": row.get("verdict", ""),
                "bench_score": row.get("bench_score", 0.0),
                "bench_evaluator": row.get("bench_evaluator", ""),
                "bench_reasons_json": json.dumps(row.get("bench_reasons", []), ensure_ascii=False, sort_keys=True),
                "luma_delta": d["luma"],
                "contrast_delta": d["contrast"],
                "saturation_delta": d["saturation"],
                "detail_delta": d["detail"],
                "params_json": json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
            })


def _html(report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = ["<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Render sampler</title><style>body{font-family:Arial;margin:24px}img{max-width:100%;border:1px solid #ddd}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ddd;padding:6px}th{background:#f5f5f5}code{background:#f7f7f7;padding:2px 4px}</style></head><body>"]
    lines.append(f"<h1>Variantes de rendu client - {html.escape(report['listing_code'])}</h1>")
    lines.append(f"<p>Profil: <code>{html.escape(report['profile'])}</code> | Strategie: <code>{html.escape(report['search_strategy'])}</code> | Recettes executees: {report['summary']['recipes_executed']} | Images sources: {report['summary']['source_images']}</p>")
    lines.append(f"<p>Duree: {report['summary']['duration_seconds']:.1f}s | Lignes incluses dans ce rapport: {report['summary']['report_rows_included']} / {report['summary']['outputs_total']}</p>")
    lines.append(f"<p><img src='{html.escape(Path(report['reports']['contact_sheet']).name)}' alt='Planche avant apres'></p>")
    lines.append("<table><thead><tr><th>Recette</th><th>Image</th><th>Status</th><th>Score</th><th>Delta lum.</th><th>Delta cont.</th><th>Delta sat.</th><th>Delta details</th><th>Parametres</th></tr></thead><tbody>")
    for row in rows:
        d = row["delta"]
        lines.append(f"<tr><td><code>{row['recipe_id'][:10]}</code></td><td>{html.escape(Path(row['source_image']).name)}</td><td>{html.escape(str(row.get('status', '')))}</td><td>{float(row.get('bench_score', 0.0)):.3f}</td><td>{d['luma']:+.2f}</td><td>{d['contrast']:+.2f}</td><td>{d['saturation']:+.2f}</td><td>{d['detail']:+.2f}</td><td><code>{html.escape(json.dumps(row['params'], ensure_ascii=False, sort_keys=True))}</code></td></tr>")
    lines.append("</tbody></table></body></html>")
    return "\n".join(lines) + "\n"


def run_client_render_sampler(
    listing_code: str,
    annonces_root: str | Path | None = None,
    output_root: str | Path | None = None,
    profile: str = "client_wide",
    samples: int = DEFAULT_BATCH_SAMPLES,
    seed: int | None = None,
    max_attempts: int | None = None,
    contact_sheet_rows: int = 32,
    duration_minutes: float | None = None,
    report_row_limit: int = 1000,
    progress_every: int = 25,
    search_strategy: str = "triangular",
    strategy_options: dict[str, Any] | None = None,
    bench_evaluator: str = "none",
    bench_evaluator_options: dict[str, Any] | None = None,
    reset_client_render_db: bool = False,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"Profil inconnu: {profile}")
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    if not images:
        raise FileNotFoundError(f"Aucune image trouvee dans le dossier annonce: {listing_dir}")

    local_output_root = Path(output_root) if output_root else default_output_root()
    output_dir = local_output_root / _safe_listing_code(listing_code) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_client_render_sampler"
    output_dir.mkdir(parents=True, exist_ok=True)
    db_file = _db_path(local_output_root, listing_code)
    if reset_client_render_db and db_file.exists():
        db_file.unlink()
    conn = _connect_db(db_file)
    ensure_bench_columns(conn)
    known = _known_recipes(conn, listing_code, profile)
    strategy_options = dict(strategy_options or {})
    bench_evaluator_options = dict(bench_evaluator_options or {})
    strategy = create_strategy(search_strategy, PROFILES[profile], seed=seed, **strategy_options)
    effective_seed = strategy.seed
    started_monotonic = time.monotonic()
    deadline = started_monotonic + duration_minutes * 60.0 if duration_minutes and duration_minutes > 0 else None
    now = datetime.now().isoformat(timespec="seconds")
    attempts_limit = max_attempts or max(samples * 50, samples + 100)
    attempts = skipped = outputs_total = recipes_executed = 0
    stop_reason = "samples_reached"
    recipes_report: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    base_metrics = {str(path): _metrics(path) for path in images}

    try:
        while recipes_executed < samples and attempts < attempts_limit:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "duration_reached"
                break
            result = strategy.next_values()
            attempts += result.attempts
            params = result.values
            rid = _recipe_id(listing_code, profile, params)
            if rid in known:
                skipped += 1
                continue
            known.add(rid)
            recipes_executed += 1
            if len(recipes_report) < report_row_limit:
                recipes_report.append({"recipe_id": rid, "params": params})
            recipe_dir = output_dir / "rendered" / f"recipe_{recipes_executed:04d}_{rid[:10]}"
            conn.execute(
                "INSERT INTO recipes(recipe_id, listing_code, profile, params_json, seed, created_at, output_dir) VALUES(?,?,?,?,?,?,?)",
                (rid, listing_code, profile, json.dumps(params, ensure_ascii=False, sort_keys=True), effective_seed, now, str(output_dir)),
            )
            for src in images:
                out_path = recipe_dir / f"{src.stem}_{rid[:10]}.jpg"
                apply_recipe(src, out_path, params)
                after = _metrics(out_path)
                before = base_metrics[str(src)]
                delta = {key: round(after[key] - before[key], 4) for key in before}
                oid = _stable_id({"kind": "client_render_output", "recipe_id": rid, "source_image": str(src)})
                evaluation = evaluate_bench_output(
                    params=params,
                    delta=delta,
                    before=before,
                    after=after,
                    space=PROFILES[profile],
                    evaluator=bench_evaluator,
                    options=bench_evaluator_options,
                )
                row = {
                    "output_id": oid,
                    "recipe_id": rid,
                    "source_image": str(src),
                    "output_path": str(out_path),
                    "params": params,
                    "before": before,
                    "after": after,
                    "delta": delta,
                    "status": evaluation.get("status", ""),
                    "label": evaluation.get("label", ""),
                    "verdict": evaluation.get("verdict", ""),
                    "bench_score": evaluation.get("score", 0.0),
                    "bench_reasons": evaluation.get("reasons", []),
                    "bench_evaluator": evaluation.get("evaluator", ""),
                    "bench_evaluation": evaluation,
                }
                outputs_total += 1
                if len(rows) < report_row_limit:
                    rows.append(row)
                conn.execute(
                    "INSERT OR REPLACE INTO outputs(output_id, recipe_id, source_image, output_path, luma_delta, contrast_delta, saturation_delta, detail_delta, created_at, status, label, verdict, bench_score, bench_reasons_json, bench_evaluator) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        oid,
                        rid,
                        str(src),
                        str(out_path),
                        delta["luma"],
                        delta["contrast"],
                        delta["saturation"],
                        delta["detail"],
                        now,
                        row.get("status", ""),
                        row.get("label", ""),
                        row.get("verdict", ""),
                        float(row.get("bench_score", 0.0) or 0.0),
                        json.dumps(row.get("bench_reasons", []), ensure_ascii=False, sort_keys=True),
                        row.get("bench_evaluator", ""),
                    ),
                )
            conn.commit()
            if progress_every > 0 and recipes_executed % progress_every == 0:
                elapsed = time.monotonic() - started_monotonic
                print(f"progression: {recipes_executed} recettes, {outputs_total} images, {elapsed:.1f}s, skips={skipped}")
        else:
            if attempts >= attempts_limit and recipes_executed < samples:
                stop_reason = "max_attempts_reached"
    finally:
        conn.close()

    duration_seconds = time.monotonic() - started_monotonic
    reports = {
        "json": str(output_dir / "client_render_sampler_report.json"),
        "csv": str(output_dir / "client_render_sampler_report.csv"),
        "html": str(output_dir / "client_render_sampler_report.html"),
        "contact_sheet": str(output_dir / "client_render_sampler_before_after.jpg"),
    }
    _contact_sheet(rows, Path(reports["contact_sheet"]), contact_sheet_rows)
    report = {
        "listing_code": listing_code,
        "listing_dir": str(listing_dir),
        "profile": profile,
        "search_strategy": search_strategy,
        "strategy_options": strategy_options,
        "bench_evaluator": bench_evaluator,
        "bench_evaluator_options": bench_evaluator_options,
        "seed": effective_seed,
        "output_dir": str(output_dir),
        "database": str(db_file),
        "reports": reports,
        "local_paths": describe_local_paths(annonces_root, local_output_root),
        "summary": {
            "source_images": len(images),
            "recipes_requested": samples,
            "recipes_executed": recipes_executed,
            "recipes_skipped_known": skipped,
            "generation_attempts": attempts,
            "outputs_total": outputs_total,
            "duration_minutes_requested": duration_minutes,
            "duration_seconds": round(duration_seconds, 3),
            "stop_reason": stop_reason,
            "report_row_limit": report_row_limit,
            "report_rows_included": len(rows),
        },
        "profiles": PROFILES,
        "recipes": recipes_report,
        "outputs": rows,
    }
    _write_json(Path(reports["json"]), report)
    _write_csv(Path(reports["csv"]), rows)
    Path(reports["html"]).write_text(_html(report, rows), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genere des variantes naturelles pour revue client.")
    parser.add_argument("--listing", required=True)
    parser.add_argument("--annonces-root", default=str(default_annonces_root()))
    parser.add_argument("--output-root", default=str(default_output_root()))
    parser.add_argument("--profile", choices=tuple(PROFILES), default="client_wide")
    parser.add_argument("--samples", type=int, default=None, help="Nombre de recettes a generer. Par defaut: 40, ou tres haut si --duration-minutes est fourni.")
    parser.add_argument("--seed", type=int, default=None, help="Nombre optionnel pour reproduire exactement une suite de recettes. Omettre pour une seed automatique.")
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--contact-sheet-rows", type=int, default=32)
    parser.add_argument("--duration-minutes", type=float, default=None, help="Fait tourner le sampler pendant une duree cible, par exemple 120 pour deux heures.")
    parser.add_argument("--report-row-limit", type=int, default=1000, help="Nombre maximum de lignes conservees dans les rapports JSON/CSV/HTML. La DB garde tout.")
    parser.add_argument("--progress-every", type=int, default=25, help="Affiche une progression toutes les N recettes. 0 pour desactiver.")
    parser.add_argument("--search-strategy", choices=available_strategies(), default="triangular", help="Strategie de recherche a utiliser.")
    parser.add_argument("--strategy-params", default=None, help="Options JSON passees a la strategie, par exemple '{\"levels\": 7}'.")
    parser.add_argument("--strategy-param", action="append", default=None, help="Option cle=valeur. Peut etre repete.")
    parser.add_argument("--bench-evaluator", choices=("none", "local_delta"), default="none", help="Evaluateur local optionnel pour produire status/label/verdict dans les rapports.")
    parser.add_argument("--bench-evaluator-params", default=None, help="Options JSON passees a l evaluateur bench.")
    parser.add_argument("--bench-evaluator-param", action="append", default=None, help="Option evaluateur cle=valeur. Peut etre repete.")
    parser.add_argument("--reset-client-render-db", action="store_true", help="Supprime la DB de recettes pour ce listing avant de lancer le bench.")
    parser.add_argument("--bench-summary-targets", default="suspect,suspects,review_candidate,review_candidates,review-candidate,review-candidates,review", help="Labels cibles a afficher dans le resume terminal, separes par des virgules.")
    parser.add_argument("--bench-summary-limit", type=int, default=12, help="Nombre maximum de sorties cibles affichees dans le terminal.")
    parser.add_argument("--no-filter-archive", action="store_true", help="Desactive l'archive cible des filtres detectes.")
    parser.add_argument("--no-bench-summary", action="store_true", help="Desactive le resume terminal des sorties cibles.")
    args = parser.parse_args(argv)
    samples = args.samples
    if samples is None:
        samples = DURATION_MODE_SAMPLES if args.duration_minutes and args.duration_minutes > 0 else DEFAULT_BATCH_SAMPLES
    try:
        strategy_options = parse_options(args.strategy_params, args.strategy_param)
        bench_evaluator_options = parse_options(args.bench_evaluator_params, args.bench_evaluator_param)
        report = run_client_render_sampler(
            args.listing,
            args.annonces_root,
            args.output_root,
            args.profile,
            max(0, samples),
            args.seed,
            args.max_attempts,
            max(1, args.contact_sheet_rows),
            args.duration_minutes,
            max(1, args.report_row_limit),
            max(0, args.progress_every),
            args.search_strategy,
            strategy_options,
            args.bench_evaluator,
            bench_evaluator_options,
            args.reset_client_render_db,
        )
    except Exception as exc:
        print(f"Erreur: {exc}")
        return 1

    print(f"Rapport JSON: {report['reports']['json']}")
    print(f"Rapport CSV: {report['reports']['csv']}")
    print(f"Rapport HTML: {report['reports']['html']}")
    print(f"Planche avant/apres: {report['reports']['contact_sheet']}")
    print(f"DB recettes: {report['database']}")
    print("")
    print("VARIANTES RENDU CLIENT:")
    print(f"profile: {report['profile']}")
    print(f"search_strategy: {report['search_strategy']}")
    print(f"strategy_options: {report['strategy_options']}")
    print(f"bench_evaluator: {summarize_bench_evaluator(report.get('bench_evaluator', 'none'), report.get('bench_evaluator_options', {}))}")
    print(f"seed: {report['seed']}")
    print(f"recettes executees: {report['summary']['recipes_executed']}")
    print(f"recettes deja connues ignorees: {report['summary']['recipes_skipped_known']}")
    print(f"images generees: {report['summary']['outputs_total']}")
    print(f"duree effective secondes: {report['summary']['duration_seconds']}")
    print(f"raison arret: {report['summary']['stop_reason']}")
    print(f"lignes rapport: {report['summary']['report_rows_included']} / {report['summary']['outputs_total']}")
    if not args.no_bench_summary:
        print("")
        print(format_bench_terminal_summary(report, target_labels=args.bench_summary_targets, limit=max(1, args.bench_summary_limit)))
    if not args.no_filter_archive:
        print("")
        filter_archive = write_filter_archive(report, target_labels=args.bench_summary_targets)
        print(format_filter_archive_summary(filter_archive, limit=max(1, args.bench_summary_limit)))
    try:
        from .rebuild_target_filter_archive import rebuild as _rebuild_clean_target_filters
        clean_targets = _rebuild_clean_target_filters(Path(report["reports"]["json"]))
        print("")
        print("CLEAN TARGET FILTERS")
        print(f"target_filters: {clean_targets['target_filters']}")
        print(f"target_matches: {clean_targets['target_matches']}")
        print(f"html: file:///{Path(clean_targets['html']).as_posix()}")
        print(f"csv: file:///{Path(clean_targets['csv']).as_posix()}")
        for idx, row in enumerate(clean_targets.get("top_filters", [])[:20], start=1):
            print(
                f"  {idx:02d}. {row['labels']} | "
                f"score={float(row['max_score']):.4f} | "
                f"matches={int(row['matches'])} | "
                f"suspect={int(row['suspect_matches'])} | "
                f"review={int(row['review_matches'])} | "
                f"review_candidate={int(row['review_candidate_matches'])} | "
                f"recipe={str(row['recipe_id'])[:12]}"
            )
    except Exception as exc:
        print("")
        print(f"CLEAN TARGET FILTERS: unavailable ({exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
