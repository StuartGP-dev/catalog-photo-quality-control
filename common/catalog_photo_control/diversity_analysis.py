from __future__ import annotations

import html
import json
import math
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageOps

from .visual_distance import DISTANCE_METRICS_VERSION, ImageDistanceResult, image_distance, visual_signature


@dataclass(frozen=True, slots=True)
class AnalysisImage:
    listing_id: str
    listing_code: str
    source_set_hash: str
    variant_id: int | None
    image_index: int
    path: Path
    output_hash: str
    recipe_family: str
    recipe_json: str
    kind: str


@dataclass(frozen=True, slots=True)
class PairAnalysis:
    candidate: AnalysisImage
    reference: AnalysisImage
    scope: str
    distance: ImageDistanceResult


def _percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float64), percent))


def distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "minimum": min(values) if values else None,
        "p1": _percentile(values, 1),
        "p5": _percentile(values, 5),
        "p10": _percentile(values, 10),
        "median": median(values) if values else None,
        "mean": mean(values) if values else None,
        "p90": _percentile(values, 90),
        "maximum": max(values) if values else None,
    }


def load_analysis_images(connection: sqlite3.Connection, listing_code: str | None = None) -> list[AnalysisImage]:
    parameters: tuple[object, ...] = ()
    listing_filter = ""
    if listing_code:
        listing_filter = " AND listing.listing_code=?"
        parameters = (listing_code,)
    sources = connection.execute(
        """SELECT listing.listing_id, listing.listing_code, image.source_set_hash,
                  NULL AS variant_id, image.image_index, image.source_path AS path,
                  image.source_hash AS output_hash, 'source' AS recipe_family,
                  '{}' AS recipe_json, 'source' AS kind
           FROM listing_images image JOIN listings listing USING(listing_id)
           WHERE image.source_set_hash=listing.active_source_set_hash""" + listing_filter,
        parameters,
    ).fetchall()
    variants = connection.execute(
        """SELECT listing.listing_id, listing.listing_code, variant.source_set_hash,
                  variant.variant_id, image.image_index, image.output_path AS path,
                  image.output_hash, variant.recipe_family, variant.recipe_json,
                  'ready_variant' AS kind
           FROM listing_variant_images image
           JOIN listing_variants variant USING(variant_id)
           JOIN listings listing USING(listing_id)
           WHERE variant.status='ready'
             AND variant.source_set_hash=listing.active_source_set_hash""" + listing_filter,
        parameters,
    ).fetchall()
    images = []
    seen: set[tuple[str, int, str]] = set()
    for row in (*sources, *variants):
        path = Path(row["path"])
        key = (str(row["listing_id"]), int(row["image_index"]), str(row["output_hash"]))
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        images.append(AnalysisImage(
            str(row["listing_id"]), str(row["listing_code"]), str(row["source_set_hash"]),
            int(row["variant_id"]) if row["variant_id"] is not None else None,
            int(row["image_index"]), path, str(row["output_hash"]),
            str(row["recipe_family"]), str(row["recipe_json"]), str(row["kind"]),
        ))
    return images


def analyze_pairs(images: Sequence[AnalysisImage], scope: str = "both", weights: Mapping[str, float] | None = None) -> list[PairAnalysis]:
    signatures = {image.output_hash: visual_signature(image.path) for image in images}
    pairs: list[PairAnalysis] = []
    ordered = sorted(images, key=lambda item: (item.image_index, item.listing_id, item.variant_id or -1, item.output_hash))
    for offset, left in enumerate(ordered):
        for right in ordered[offset + 1:]:
            if right.image_index != left.image_index:
                if right.image_index > left.image_index:
                    break
                continue
            if left.kind != "ready_variant" and right.kind != "ready_variant":
                continue
            if left.output_hash == right.output_hash or left.variant_id is not None and left.variant_id == right.variant_id and left.listing_id == right.listing_id:
                continue
            candidate, reference = (left, right) if left.kind == "ready_variant" else (right, left)
            pair_scope = "listing" if left.listing_id == right.listing_id and left.source_set_hash == right.source_set_hash else "catalog"
            if scope != "both" and pair_scope != scope:
                continue
            pairs.append(PairAnalysis(candidate, reference, pair_scope, image_distance(signatures[candidate.output_hash], signatures[reference.output_hash], weights)))
    return pairs


def nearest_pairs(pairs: Sequence[PairAnalysis]) -> list[PairAnalysis]:
    nearest: dict[tuple[str, int, int, str], PairAnalysis] = {}
    for pair in pairs:
        for candidate, reference in ((pair.candidate, pair.reference), (pair.reference, pair.candidate)):
            if candidate.variant_id is None:
                continue
            reversed_pair = PairAnalysis(candidate, reference, pair.scope, pair.distance)
            key = (candidate.listing_id, candidate.variant_id, candidate.image_index, pair.scope)
            current = nearest.get(key)
            if current is None or (pair.distance.total_distance, reference.output_hash) < (current.distance.total_distance, current.reference.output_hash):
                nearest[key] = reversed_pair
    return sorted(nearest.values(), key=lambda pair: pair.distance.total_distance)


def analysis_summary(pairs: Sequence[PairAnalysis]) -> dict[str, object]:
    nearest = nearest_pairs(pairs)
    output: dict[str, object] = {
        "metrics_version": DISTANCE_METRICS_VERSION,
        "all_pairs": distribution([pair.distance.total_distance for pair in pairs]),
        "nearest_pairs": distribution([pair.distance.total_distance for pair in nearest]),
        "by_scope": {}, "by_image_index": {}, "by_family": {},
    }
    scope_values = sorted({pair.scope for pair in nearest})
    output["by_scope"] = {value: distribution([pair.distance.total_distance for pair in nearest if pair.scope == value]) for value in scope_values}
    index_values = sorted({pair.candidate.image_index for pair in nearest})
    output["by_image_index"] = {str(value): distribution([pair.distance.total_distance for pair in nearest if pair.candidate.image_index == value]) for value in index_values}
    families = sorted({pair.candidate.recipe_family for pair in nearest})
    output["by_family"] = {family: distribution([pair.distance.total_distance for pair in nearest if pair.candidate.recipe_family == family]) for family in families}
    return output


def threshold_outcomes(nearest: Sequence[PairAnalysis], thresholds: Sequence[float]) -> list[dict[str, object]]:
    variants: dict[tuple[str, int], list[PairAnalysis]] = {}
    for pair in nearest:
        variants.setdefault((pair.candidate.listing_id, int(pair.candidate.variant_id or 0)), []).append(pair)
    outcomes = []
    for threshold in thresholds:
        rejected = {key for key, rows in variants.items() if any(row.distance.total_distance < threshold for row in rows)}
        outcomes.append({
            "threshold": threshold,
            "variants_kept": len(variants) - len(rejected),
            "variants_rejected": len(rejected),
            "images_too_close": sum(pair.distance.total_distance < threshold for pair in nearest),
        })
    return outcomes


def _difference_map(left: Path, right: Path, output: Path) -> None:
    with Image.open(left) as opened_left, Image.open(right) as opened_right:
        a = ImageOps.exif_transpose(opened_left).convert("RGB")
        b = ImageOps.exif_transpose(opened_right).convert("RGB").resize(a.size, Image.Resampling.LANCZOS)
        difference = ImageChops.difference(a, b).point(lambda value: min(255, value * 4))
        difference.save(output, format="JPEG", quality=90)


def write_analysis_html(path: Path, pairs: Sequence[PairAnalysis], summary: Mapping[str, object], thresholds: Sequence[float], top_nearest: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nearest = nearest_pairs(pairs)
    around: list[PairAnalysis] = list(nearest[:top_nearest])
    for threshold in thresholds:
        around.extend(sorted(nearest, key=lambda pair: abs(pair.distance.total_distance - threshold))[:8])
    unique: dict[tuple[str, int | None, str], PairAnalysis] = {}
    for pair in around:
        unique[(pair.candidate.output_hash, pair.reference.variant_id, pair.reference.output_hash)] = pair
    shown = sorted(unique.values(), key=lambda pair: pair.distance.total_distance)
    assets = path.parent / "assets"
    assets.mkdir(exist_ok=True)
    cards = []
    for index, pair in enumerate(shown):
        diff = assets / f"diff_{index:04d}.jpg"
        _difference_map(pair.candidate.path, pair.reference.path, diff)
        candidate_uri, reference_uri = pair.candidate.path.resolve().as_uri(), pair.reference.path.resolve().as_uri()
        components = pair.distance.components()
        verdicts = " · ".join(f"{threshold:.4f}: {'rejet' if pair.distance.total_distance < threshold else 'passe'}" for threshold in thresholds)
        cards.append(f'''<article><h3>{pair.distance.total_distance:.5f} · {html.escape(pair.scope)} · index {pair.candidate.image_index}</h3>
        <p>Candidat {html.escape(pair.candidate.listing_code)} / variant {pair.candidate.variant_id} ({html.escape(pair.candidate.recipe_family)}) — référence {html.escape(pair.reference.listing_code)} / {pair.reference.variant_id or 'source'}</p>
        <p>{html.escape(verdicts)}</p><div class="compare"><img src="{candidate_uri}"><img class="overlay" src="{reference_uri}"></div>
        <div class="controls"><input class="slider" type="range" min="0" max="100" value="50"><button class="toggle">Basculer</button><button class="blink">Alternance</button><button class="zoom">Zoom 100 %</button></div>
        <div class="grid"><figure><img src="{candidate_uri}"><figcaption>Candidat</figcaption></figure><figure><img src="{reference_uri}"><figcaption>Voisin</figcaption></figure><figure><img src="assets/{diff.name}"><figcaption>Différence ×4 (inspection uniquement)</figcaption></figure></div>
        <details><summary>Composantes, dimensions et recettes</summary><pre>{html.escape(json.dumps({'total': pair.distance.total_distance, 'components': components, 'candidate_recipe': json.loads(pair.candidate.recipe_json), 'reference_recipe': json.loads(pair.reference.recipe_json)}, indent=2, ensure_ascii=False))}</pre></details></article>''')
    outcomes = threshold_outcomes(nearest, thresholds)
    document = f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Calibration diversité</title><style>
    body{{font:14px system-ui;background:#f3f4f6;color:#172033;margin:1.5rem}}main{{max-width:1500px;margin:auto}}article,.summary{{background:white;padding:1rem;margin:1rem 0;border-radius:10px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:.6rem}}figure{{margin:0}}.grid img{{width:100%;height:300px;object-fit:contain;background:#ddd}}.compare{{position:relative;height:600px;overflow:auto;background:#ddd}}.compare img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}}.compare .overlay{{clip-path:inset(0 0 0 50%)}}.compare.actual img{{width:auto;height:auto;max-width:none;max-height:none}}pre{{overflow:auto;max-height:30rem}}button{{margin:.5rem}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
    </style></head><body><main><h1>Calibration de la barrière de diversité</h1><section class="summary"><p>Version <code>{DISTANCE_METRICS_VERSION}</code>. Le score est borné entre 0 et 1 et sert de barrière calibrée ; il ne prouve pas à lui seul la perception humaine.</p><h2>Distributions</h2><pre>{html.escape(json.dumps(summary, indent=2, ensure_ascii=False))}</pre><h2>Effet des seuils candidats</h2><pre>{html.escape(json.dumps(outcomes, indent=2, ensure_ascii=False))}</pre></section>{''.join(cards)}
    <script>document.querySelectorAll('.compare').forEach(box=>{{let timer=null;const overlay=box.querySelector('.overlay');const parent=box.parentElement;parent.querySelector('.slider').oninput=e=>overlay.style.clipPath=`inset(0 0 0 ${{e.target.value}}%)`;parent.querySelector('.toggle').onclick=()=>overlay.style.display=overlay.style.display==='none'?'block':'none';parent.querySelector('.blink').onclick=()=>{{if(timer){{clearInterval(timer);timer=null}}else timer=setInterval(()=>overlay.style.display=overlay.style.display==='none'?'block':'none',350)}};parent.querySelector('.zoom').onclick=()=>box.classList.toggle('actual')}});</script></main></body></html>'''
    path.write_text(document, encoding="utf-8")


def write_analysis_json(path: Path, pairs: Sequence[PairAnalysis], summary: Mapping[str, object], thresholds: Sequence[float]) -> None:
    nearest = nearest_pairs(pairs)
    payload = {
        "summary": summary,
        "threshold_outcomes": threshold_outcomes(nearest, thresholds),
        "nearest_pairs": [{
            "candidate": asdict(pair.candidate) | {"path": str(pair.candidate.path)},
            "reference": asdict(pair.reference) | {"path": str(pair.reference.path)},
            "scope": pair.scope,
            "distance": asdict(pair.distance),
        } for pair in nearest],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
