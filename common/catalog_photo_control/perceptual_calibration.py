from __future__ import annotations

import argparse
import html
import io
import json
import shutil
from pathlib import Path
from statistics import median
from urllib.parse import quote

from PIL import Image, ImageEnhance, ImageOps

from .image_similarity import SIMILARITY_ENGINE_VERSION, SimilarityResult, compare_images


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VERDICT_ORDER = {"different": 0, "near_duplicate": 1, "same": 2, "exact": 3}


def _distance_key(row: dict[str, object]) -> tuple[int, int, int, int, int]:
    result: SimilarityResult = row["result"]
    return (VERDICT_ORDER[result.verdict], -sum(result.distances()), -result.phash.distance, -result.dhash.distance, -result.whash.distance)


def _near_threshold(result: SimilarityResult) -> bool:
    limits = ((result.phash.distance, (4, 10, 16)), (result.dhash.distance, (4, 10, 16)), (result.whash.distance, (4, 10, 18)))
    return any(abs(distance - threshold) <= 1 for distance, thresholds in limits for threshold in thresholds)


def _save_cases(source: Path, assets: Path, index: int) -> list[tuple[str, Path, Path]]:
    reference = assets / f"image_{index}_reference.jpg"
    shutil.copy2(source, reference)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    stream = io.BytesIO(); image.save(stream, "JPEG", quality=82); stream.seek(0)
    inset = max(1, int(min(width, height) * .018))
    cropped = image.crop((inset, inset, width-inset, height-inset)).resize(image.size, Image.Resampling.LANCZOS)
    small = image.resize((int(width*.96), int(height*.96)), Image.Resampling.LANCZOS)
    dezoom = Image.new("RGB", image.size, (245, 245, 245)); dezoom.paste(small, ((width-small.width)//2, (height-small.height)//2))
    offset = Image.new("RGB", image.size, (245, 245, 245)); offset.paste(image, (int(width*.018), 0))
    cases = [
        ("jpeg82", Image.open(stream).copy()), ("brightness_1.03", ImageEnhance.Brightness(image).enhance(1.03)),
        ("rotation_1.5", image.rotate(1.5, Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))),
        ("crop_1.8", cropped), ("zoom_1.8", cropped.copy()), ("dezoom_4", dezoom), ("offset_1.8", offset),
        ("visibly_different", ImageOps.grayscale(image).convert("RGB").transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
    ]
    rows = [("self", reference, reference)]
    for label, candidate in cases:
        path = assets / f"image_{index}_{label}.jpg"; candidate.save(path, "JPEG", quality=93)
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
            rows.append({"image_index": index, "case": label, "reference": reference, "candidate": candidate, "result": compare_images(reference, candidate)})
        other = assets / f"image_{index}_other_o18_view.jpg"; shutil.copy2(sources[(index + 1) % len(sources)], other)
        reference = assets / f"image_{index}_reference.jpg"
        rows.append({"image_index": index, "case": "other_O18_view", "reference": reference, "candidate": other, "result": compare_images(reference, other)})
    other_product = next((path for folder in sorted(source_dir.parent.iterdir()) if folder.is_dir() and folder != source_dir for path in sorted(folder.iterdir()) if path.suffix.lower() in IMAGE_SUFFIXES), None)
    if other_product:
        copied = assets / "other_product.jpg"; shutil.copy2(other_product, copied)
        for index in range(len(sources)):
            reference = assets / f"image_{index}_reference.jpg"
            rows.append({"image_index": index, "case": "other_product", "reference": reference, "candidate": copied, "result": compare_images(reference, copied)})
    rows.sort(key=_distance_key)
    summaries = {}
    for name in ("phash", "dhash", "whash"):
        values = [getattr(row["result"], name).distance for row in rows]
        summaries[name] = {"minimum": min(values), "median": median(values), "maximum": max(values), "observed": sorted(set(values))}
    verdict_counts = {verdict: sum(row["result"].verdict == verdict for row in rows) for verdict in VERDICT_ORDER}
    cards = []
    for rank, row in enumerate(rows, start=1):
        result: SimilarityResult = row["result"]; total = sum(result.distances())
        cards.append(f'''<article id="pair-{rank}" class="pair-card {result.verdict}" data-verdict="{result.verdict}" data-case="{html.escape(str(row['case']))}" data-index="{row['image_index']}" data-rank="{rank}" data-total="{total}" data-phash="{result.phash.distance}" data-dhash="{result.dhash.distance}" data-whash="{result.whash.distance}"><h2><span class="rank">#{rank}</span> {result.verdict}</h2>
<p>Transformation <strong>{html.escape(str(row['case']))}</strong> · image_index {row['image_index']}</p><div class="pair"><figure><img src="{_src(row['candidate'], report_dir)}"><figcaption>Image candidate</figcaption></figure><figure><img src="{_src(row['reference'], report_dir)}"><figcaption>Image de référence</figcaption></figure></div>
<p class="distances">pHash <strong>{result.phash.distance}/64</strong> ({result.phash.band}) · dHash <strong>{result.dhash.distance}/64</strong> ({result.dhash.band}) · wHash <strong>{result.whash.distance}/64</strong> ({result.whash.band}) · somme <strong>{total}/192</strong></p><p>SHA-256 égal : {result.sha256_equal}</p><p><strong>Raison du consensus : {html.escape(result.reason)}</strong></p></article>''')
    def links(selected: list[dict[str, object]]) -> str:
        return " ".join(f'<a href="#pair-{rows.index(row)+1}">#{rows.index(row)+1} {html.escape(str(row["case"]))} (Σ{sum(row["result"].distances())})</a>' for row in selected)
    transformations = sorted({str(row["case"]) for row in rows}); indices = sorted({int(row["image_index"]) for row in rows})
    closest = sorted(rows, key=lambda row: (-VERDICT_ORDER[row["result"].verdict], sum(row["result"].distances()), row["result"].phash.distance, row["result"].dhash.distance, row["result"].whash.distance))[:20]
    near = [row for row in rows if _near_threshold(row["result"])][:20]
    document = f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Calibration perceptuelle O18</title><style>
body{{font:15px system-ui;max-width:1400px;margin:2rem auto;background:#eee;color:#222}}.panel,.pair-card{{background:white;padding:1rem;margin:1rem 0;border-radius:8px}}.controls{{position:sticky;top:0;z-index:2;box-shadow:0 2px 8px #0003}}label{{margin-right:1rem}}select{{padding:.35rem}}.pair-card{{border-left:10px solid #288}}.exact,.same{{border-color:#c33}}.near_duplicate{{border-color:#e90}}.different{{border-color:#298}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}img{{width:100%;height:320px;object-fit:contain;background:#ddd}}figure{{margin:0}}h2{{font-size:2rem;margin:.2rem 0}}.rank{{color:#666}}.distances{{font-size:1.15rem}}.quick a{{display:inline-block;margin:.2rem;padding:.25rem .4rem;background:#eef;border-radius:4px}}.counts{{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem}}.counts strong{{font-size:1.5rem}}</style></head><body>
<h1>Calibration perceptuelle O18</h1><p>Moteur {SIMILARITY_ENGINE_VERSION}. Tri initial : verdict different → near_duplicate → same → exact, puis somme, pHash, dHash et wHash décroissants.</p>
<section class="panel controls"><label>Ordre <select id="sort"><option value="different">Plus différentes d’abord</option><option value="similar">Plus similaires d’abord</option></select></label><label>Verdict <select id="verdict"><option value="all">Tous</option>{''.join(f'<option>{value}</option>' for value in VERDICT_ORDER)}</select></label><label>Transformation <select id="case"><option value="all">Toutes</option>{''.join(f'<option>{html.escape(value)}</option>' for value in transformations)}</select></label><label>image_index <select id="index"><option value="all">Tous</option>{''.join(f'<option>{value}</option>' for value in indices)}</select></label> <span id="visible"></span></section>
<section class="panel"><h2>Distribution des verdicts</h2><div class="counts">{''.join(f'<div class="{key}"><strong>{value}</strong><br>{key}</div>' for key, value in verdict_counts.items())}</div></section><section class="panel quick"><h2>20 paires les plus différentes</h2>{links(rows[:20])}</section><section class="panel quick"><h2>20 paires les plus proches</h2>{links(closest)}</section><section class="panel quick"><h2>Paires proches des seuils (±1 bit)</h2>{links(near) or 'Aucune'}</section><details class="panel"><summary>Distribution brute des distances</summary><pre>{html.escape(json.dumps(summaries, indent=2, ensure_ascii=False))}</pre></details><main id="pairs">{''.join(cards)}</main>
<script>const order={{different:0,near_duplicate:1,same:2,exact:3}},cards=[...document.querySelectorAll('.pair-card')];function key(c,s){{const v=order[c.dataset.verdict],n=[+c.dataset.total,+c.dataset.phash,+c.dataset.dhash,+c.dataset.whash];return s?[-v,...n]:[v,...n.map(x=>-x)]}}function cmp(a,b){{const s=document.querySelector('#sort').value==='similar',ka=key(a,s),kb=key(b,s);for(let i=0;i<ka.length;i++)if(ka[i]!==kb[i])return ka[i]-kb[i];return +a.dataset.rank-+b.dataset.rank}}function update(){{const v=document.querySelector('#verdict').value,k=document.querySelector('#case').value,i=document.querySelector('#index').value;cards.sort(cmp).forEach(c=>document.querySelector('#pairs').appendChild(c));let shown=0;cards.forEach(c=>{{const ok=(v==='all'||c.dataset.verdict===v)&&(k==='all'||c.dataset.case===k)&&(i==='all'||c.dataset.index===i);c.hidden=!ok;shown+=ok}});document.querySelector('#visible').textContent=shown+' paire(s) affichée(s)'}}document.querySelectorAll('select').forEach(x=>x.addEventListener('change',update));update()</script></body></html>'''
    report = report_dir / "index.html"; report.write_text(document, encoding="utf-8")
    payload = {"engine_version": SIMILARITY_ENGINE_VERSION, "pair_count": len(rows), "distributions": summaries, "verdict_counts": verdict_counts,
               "cases": [{"rank": rank, "image_index": row["image_index"], "case": row["case"], "distance_sum": sum(row["result"].distances()), **row["result"].as_dict()} for rank, row in enumerate(rows, start=1)]}
    (report_dir / "calibration.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Short read-only perceptual calibration for one listing")
    parser.add_argument("--listing", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv); report, payload = generate_calibration_report(args.listing, args.output)
    print(report); print(json.dumps(payload["distributions"], ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
