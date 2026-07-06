from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from random import Random
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .listing_photo_review import _safe_listing_code, find_listing_images, resolve_listing_dir
from .local_paths import default_annonces_root, default_output_root, describe_local_paths

DB_VERSION = 1
PIPELINE_VERSION = 1

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
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (str(DB_VERSION),))
    conn.commit()
    return conn


def _known_recipes(conn: sqlite3.Connection, listing_code: str, profile: str) -> set[str]:
    rows = conn.execute("SELECT recipe_id FROM recipes WHERE listing_code=? AND profile=?", (listing_code, profile)).fetchall()
    return {str(row[0]) for row in rows}


def _sample_float(rng: Random, bounds: tuple[float, float, float], digits: int = 4) -> float:
    return round(rng.triangular(bounds[0], bounds[1], bounds[2]), digits)


def _sample_params(rng: Random, profile: str) -> dict[str, Any]:
    p = PROFILES[profile]
    q = p["quality"]  # type: ignore[assignment]
    return {
        "brightness": _sample_float(rng, p["brightness"]),  # type: ignore[arg-type]
        "contrast": _sample_float(rng, p["contrast"]),  # type: ignore[arg-type]
        "saturation": _sample_float(rng, p["saturation"]),  # type: ignore[arg-type]
        "sharpness": _sample_float(rng, p["sharpness"]),  # type: ignore[arg-type]
        "warmth": _sample_float(rng, p["warmth"]),  # type: ignore[arg-type]
        "angle": _sample_float(rng, p["angle"]),  # type: ignore[arg-type]
        "crop": _sample_float(rng, p["crop"]),  # type: ignore[arg-type]
        "blur": _sample_float(rng, p["blur"]),  # type: ignore[arg-type]
        "quality": int(round(rng.triangular(int(q[0]), int(q[1]), int(q[2])))),
    }


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


def _rotate_keep_size(image: Image.Image, angle: float) -> Image.Image:
    if abs(angle) < 0.01:
        return image
    rotated = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    return ImageOps.fit(rotated, image.size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


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
    out = _crop_keep_size(out, float(params["crop"]))
    out = _rotate_keep_size(out, float(params["angle"]))
    out = ImageEnhance.Brightness(out).enhance(float(params["brightness"]))
    out = ImageEnhance.Contrast(out).enhance(float(params["contrast"]))
    out = ImageEnhance.Color(out).enhance(float(params["saturation"]))
    out = ImageEnhance.Sharpness(out).enhance(float(params["sharpness"]))
    out = _warmth(out, float(params["warmth"]))
    if float(params["blur"]) > 0.03:
        out = out.filter(ImageFilter.GaussianBlur(radius=float(params["blur"])))
    out.save(output_path, format="JPEG", quality=max(60, min(98, int(params["quality"]))), optimize=True)


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
        draw.text((gap, y + 18), f"rot={params['angle']} crop={params['crop']} lum={params['brightness']} cont={params['contrast']} sat={params['saturation']} q={params['quality']}", fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="JPEG", quality=90)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["output_id", "recipe_id", "source_image", "output_path", "luma_delta", "contrast_delta", "saturation_delta", "detail_delta", "params_json"]
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
                "luma_delta": d["luma"],
                "contrast_delta": d["contrast"],
                "saturation_delta": d["saturation"],
                "detail_delta": d["detail"],
                "params_json": json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
            })


def _html(report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = ["<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>Render sampler</title><style>body{font-family:Arial;margin:24px}img{max-width:100%;border:1px solid #ddd}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ddd;padding:6px}th{background:#f5f5f5}code{background:#f7f7f7;padding:2px 4px}</style></head><body>"]
    lines.append(f"<h1>Variantes de rendu client - {html.escape(report['listing_code'])}</h1>")
    lines.append(f"<p>Profil: <code>{html.escape(report['profile'])}</code> | Recettes: {report['summary']['recipes_executed']} | Images: {report['summary']['source_images']}</p>")
    lines.append(f"<p><img src='{html.escape(Path(report['reports']['contact_sheet']).name)}' alt='Planche avant apres'></p>")
    lines.append("<table><thead><tr><th>Recette</th><th>Image</th><th>Delta lum.</th><th>Delta cont.</th><th>Delta sat.</th><th>Delta details</th><th>Parametres</th></tr></thead><tbody>")
    for row in rows:
        d = row["delta"]
        lines.append(f"<tr><td><code>{row['recipe_id'][:10]}</code></td><td>{html.escape(Path(row['source_image']).name)}</td><td>{d['luma']:+.2f}</td><td>{d['contrast']:+.2f}</td><td>{d['saturation']:+.2f}</td><td>{d['detail']:+.2f}</td><td><code>{html.escape(json.dumps(row['params'], ensure_ascii=False, sort_keys=True))}</code></td></tr>")
    lines.append("</tbody></table></body></html>")
    return "\n".join(lines) + "\n"


def run_client_render_sampler(
    listing_code: str,
    annonces_root: str | Path | None = None,
    output_root: str | Path | None = None,
    profile: str = "client_wide",
    samples: int = 40,
    seed: int = 12345,
    max_attempts: int | None = None,
    contact_sheet_rows: int = 32,
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
    conn = _connect_db(db_file)
    known = _known_recipes(conn, listing_code, profile)
    rng = Random(seed)
    now = datetime.now().isoformat(timespec="seconds")
    attempts_limit = max_attempts or max(samples * 50, samples + 100)
    attempts = skipped = 0
    recipes: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    base_metrics = {str(path): _metrics(path) for path in images}

    try:
        while len(recipes) < samples and attempts < attempts_limit:
            attempts += 1
            params = _sample_params(rng, profile)
            rid = _recipe_id(listing_code, profile, params)
            if rid in known:
                skipped += 1
                continue
            known.add(rid)
            recipes.append({"recipe_id": rid, "params": params})
            recipe_dir = output_dir / "rendered" / f"recipe_{len(recipes):04d}_{rid[:10]}"
            conn.execute(
                "INSERT INTO recipes(recipe_id, listing_code, profile, params_json, seed, created_at, output_dir) VALUES(?,?,?,?,?,?,?)",
                (rid, listing_code, profile, json.dumps(params, ensure_ascii=False, sort_keys=True), seed, now, str(output_dir)),
            )
            for src in images:
                out_path = recipe_dir / f"{src.stem}_{rid[:10]}.jpg"
                apply_recipe(src, out_path, params)
                after = _metrics(out_path)
                before = base_metrics[str(src)]
                delta = {key: round(after[key] - before[key], 4) for key in before}
                oid = _stable_id({"kind": "client_render_output", "recipe_id": rid, "source_image": str(src)})
                row = {"output_id": oid, "recipe_id": rid, "source_image": str(src), "output_path": str(out_path), "params": params, "before": before, "after": after, "delta": delta}
                rows.append(row)
                conn.execute(
                    "INSERT OR REPLACE INTO outputs(output_id, recipe_id, source_image, output_path, luma_delta, contrast_delta, saturation_delta, detail_delta, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (oid, rid, str(src), str(out_path), delta["luma"], delta["contrast"], delta["saturation"], delta["detail"], now),
                )
            conn.commit()
    finally:
        conn.close()

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
        "seed": seed,
        "output_dir": str(output_dir),
        "database": str(db_file),
        "reports": reports,
        "local_paths": describe_local_paths(annonces_root, local_output_root),
        "summary": {
            "source_images": len(images),
            "recipes_requested": samples,
            "recipes_executed": len(recipes),
            "recipes_skipped_known": skipped,
            "generation_attempts": attempts,
            "outputs_total": len(rows),
        },
        "profiles": PROFILES,
        "recipes": recipes,
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
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--contact-sheet-rows", type=int, default=32)
    args = parser.parse_args(argv)
    try:
        report = run_client_render_sampler(
            args.listing,
            args.annonces_root,
            args.output_root,
            args.profile,
            max(0, args.samples),
            args.seed,
            args.max_attempts,
            max(1, args.contact_sheet_rows),
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
    print(f"seed: {report['seed']}")
    print(f"recettes executees: {report['summary']['recipes_executed']}")
    print(f"recettes deja connues ignorees: {report['summary']['recipes_skipped_known']}")
    print(f"images generees: {report['summary']['outputs_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
