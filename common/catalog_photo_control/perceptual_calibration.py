from __future__ import annotations

import argparse
import html
import io
import json
import shutil
from pathlib import Path
from statistics import median
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from .image_similarity import SIMILARITY_ENGINE_VERSION, SimilarityResult, compare_images


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _save_cases(source: Path, assets: Path, index: int) -> list[tuple[str, Path, Path]]:
    reference = assets / f"image_{index}_reference.jpg"
    shutil.copy2(source, reference)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    cases: list[tuple[str, Image.Image]] = []
    stream = io.BytesIO(); image.save(stream, "JPEG", quality=82); stream.seek(0)
    cases.append(("jpeg82", Image.open(stream).copy()))
    cases.append(("brightness_1.03", ImageEnhance.Brightness(image).enhance(1.03)))
    cases.append(("rotation_1.5", image.rotate(1.5, Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))))
    inset = max(1, int(min(width, height) * .018))
    cropped = image.crop((inset, inset, width-inset, height-inset)).resize(image.size, Image.Resampling.LANCZOS)
    cases.extend((("crop_1.8", cropped), ("zoom_1.8", cropped.copy())))
    small = image.resize((int(width*.96), int(height*.96)), Image.Resampling.LANCZOS)
    dezoom = Image.new("RGB", image.size, (245, 245, 245)); dezoom.paste(small, ((width-small.width)//2, (height-small.height)//2))
    cases.append(("dezoom_4", dezoom))
    offset = Image.new("RGB", image.size, (245, 245, 245)); offset.paste(image, (int(width*.018), 0))
    cases.append(("offset_1.8", offset))
    cases.append(("visibly_different", ImageOps.grayscale(image).convert("RGB").transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
    rows = [("self", reference, reference)]
    for label, candidate in cases:
        path = assets / f"image_{index}_{label}.jpg"
        candidate.save(path, "JPEG", quality=93)
        rows.append((label, reference, path))
    return rows


def _src(path: Path, report_dir: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return quote(path.relative_to(report_dir).as_posix(), safe="/.-_~")


def generate_calibration_report(listing_dir: str | Path, output_dir: str | Path) -> tuple[Path, dict[str, object]]:
    source_dir, report_dir = Path(listing_dir), Path(output_dir)
    sources = sorted(path for path in source_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
    if not sources:
        raise ValueError("no source images found")
    assets = report_dir / "assets"; assets.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for index, source in enumerate(sources):
        for label, reference, candidate in _save_cases(source, assets, index):
            result = compare_images(reference, candidate)
            rows.append({"image_index": index, "case": label, "reference": reference, "candidate": candidate, "result": result})
        other = assets / f"image_{index}_other_o18_view.jpg"
        shutil.copy2(sources[(index + 1) % len(sources)], other)
        rows.append({"image_index": index, "case": "other_O18_view", "reference": assets / f"image_{index}_reference.jpg", "candidate": other, "result": compare_images(assets / f"image_{index}_reference.jpg", other)})
    other_product = next((path for folder in sorted(source_dir.parent.iterdir()) if folder.is_dir() and folder != source_dir for path in sorted(folder.iterdir()) if path.suffix.lower() in IMAGE_SUFFIXES), None)
    if other_product:
        copied = assets / "other_product.jpg"; shutil.copy2(other_product, copied)
        for index in range(len(sources)):
            reference = assets / f"image_{index}_reference.jpg"
            rows.append({"image_index": index, "case": "other_product", "reference": reference, "candidate": copied, "result": compare_images(reference, copied)})
    summaries = {}
    for name in ("phash", "dhash", "whash"):
        values = [getattr(row["result"], name).distance for row in rows]
        summaries[name] = {"minimum": min(values), "median": median(values), "maximum": max(values), "observed": sorted(set(values))}
    cards = []
    for row in rows:
        result: SimilarityResult = row["result"]
        cards.append(f'''<article class="{result.verdict}"><h2>{html.escape(result.verdict)}</h2>
<p>Cas <strong>{html.escape(str(row['case']))}</strong> · image_index {row['image_index']}</p>
<div class="pair"><figure><img src="{_src(row['candidate'], report_dir)}"><figcaption>Candidate</figcaption></figure>
<figure><img src="{_src(row['reference'], report_dir)}"><figcaption>Référence O18</figcaption></figure></div>
<p>SHA-256 égal : {result.sha256_equal}</p>
<p>pHash {result.phash.distance}/64 — {result.phash.band} · dHash {result.dhash.distance}/64 — {result.dhash.band} · wHash {result.whash.distance}/64 — {result.whash.band}</p>
<p><strong>{html.escape(result.reason)}</strong></p><p>Recette candidate : {html.escape(str(row['case']))}</p></article>''')
    document = f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Calibration perceptuelle O18</title>
<style>body{{font:15px system-ui;max-width:1400px;margin:2rem auto;background:#eee}}article{{background:white;padding:1rem;margin:1rem 0;border-left:10px solid #288}}.exact,.same{{border-color:#c33}}.near_duplicate{{border-color:#e90}}.different{{border-color:#298}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}img{{width:100%;height:320px;object-fit:contain;background:#ddd}}figure{{margin:0}}h2{{font-size:2rem}}</style></head><body>
<h1>Calibration perceptuelle O18</h1><p>Moteur {SIMILARITY_ENGINE_VERSION}. Seuils calibrés sur ce corpus, non universels.</p>
<pre>{html.escape(json.dumps(summaries, indent=2, ensure_ascii=False))}</pre>{''.join(cards)}</body></html>'''
    report = report_dir / "index.html"; report.write_text(document, encoding="utf-8")
    payload = {"engine_version": SIMILARITY_ENGINE_VERSION, "pair_count": len(rows), "distributions": summaries,
               "cases": [{"image_index": row["image_index"], "case": row["case"], **row["result"].as_dict()} for row in rows]}
    (report_dir / "calibration.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Short read-only perceptual calibration for one listing")
    parser.add_argument("--listing", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report, payload = generate_calibration_report(args.listing, args.output)
    print(report); print(json.dumps(payload["distributions"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
