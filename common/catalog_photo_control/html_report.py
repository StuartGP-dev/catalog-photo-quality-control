from __future__ import annotations

import html
import json
import os
import sqlite3
from pathlib import Path
from typing import Mapping

from .models import SourceListing


def _distribution(counters: Mapping[str, int], prefix: str) -> dict[str, int]:
    return {
        key[len(prefix):]: value
        for key, value in sorted(counters.items())
        if key.startswith(prefix)
    }


def _format_distribution(values: Mapping[str, int]) -> str:
    return " · ".join(
        f"{html.escape(key)}: {value}" for key, value in values.items()
    ) or "none"


def _relative_link(path: str | Path, report_dir: Path) -> str:
    return Path(os.path.relpath(Path(path), report_dir)).as_posix()


def write_html_report(
    path: str | Path,
    variants_connection: sqlite3.Connection,
    listing: SourceListing,
    *,
    run_id: str,
    status: str,
    stop_reason: str,
    requested: int,
    counters: Mapping[str, int],
) -> Path:
    output = Path(path)
    if output.name != "index.html":
        raise ValueError("the user-facing report must be named index.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    variants = variants_connection.execute(
        """SELECT * FROM listing_variants
           WHERE listing_id=? AND source_set_hash=? AND status='ready'
           ORDER BY selected_rank""",
        (listing.listing_id, listing.source_set_hash),
    ).fetchall()
    cards: list[str] = []
    for variant in variants:
        images = variants_connection.execute(
            """SELECT * FROM listing_variant_images
               WHERE variant_id=? ORDER BY image_index""",
            (variant["variant_id"],),
        ).fetchall()
        image_parts = []
        for image in images:
            same = json.loads(image["nearest_same_listing_json"])
            catalog = json.loads(image["nearest_catalog_json"])
            image_parts.append(
                f'<figure><img src="{html.escape(_relative_link(image["output_path"], output.parent))}" '
                f'alt="variant {variant["selected_rank"]}, source image {image["image_index"]}">'
                f'<figcaption>Index #{image["image_index"]}<br>intra {same.get("total_distance", "no_reference_yet")} '
                f'({image["reference_count_same_listing"]} réf.)<br>catalogue {catalog.get("total_distance", "no_reference_yet")} '
                f'({image["reference_count_catalog"]} réf.)</figcaption></figure>'
            )
        image_html = "".join(image_parts)
        recipe = html.escape(
            json.dumps(json.loads(variant["recipe_json"]), indent=2, ensure_ascii=False)
        )
        metrics = html.escape(
            json.dumps(json.loads(variant["aggregate_metrics_json"]), indent=2, ensure_ascii=False)
        )
        aggregate = json.loads(variant["aggregate_metrics_json"])
        recipe_values = json.loads(variant["recipe_json"])
        distance_components = html.escape(
            json.dumps(
                json.loads(variant["minimum_distance_components_json"]),
                indent=2,
                ensure_ascii=False,
            )
        )
        folder = html.escape(_relative_link(Path(images[0]["output_path"]).parent, output.parent)) if images else "#"
        same_threshold = float(aggregate.get("minimum_same_listing_threshold", 0))
        catalog_threshold = float(aggregate.get("minimum_catalog_threshold", 0))
        same_minimum = variant["minimum_same_listing_distance"]
        catalog_minimum = variant["minimum_catalog_distance"]
        same_margin = None if same_minimum is None else float(same_minimum) - same_threshold
        catalog_margin = None if catalog_minimum is None else float(catalog_minimum) - catalog_threshold
        limiting = min(
            ((json.loads(image["nearest_same_listing_json"]).get("total_distance", 2), image["image_index"], json.loads(image["nearest_same_listing_json"])) for image in images),
            default=(None, None, {}), key=lambda item: item[0],
        )
        cards.append(
            f"""<article class="variant">
            <h2 data-margin="{same_margin if same_margin is not None else 9}">Variant {variant['selected_rank']:04d}</h2>
            <p><strong>Recipe family: {html.escape(variant['recipe_family'])}</strong></p>
            <p>Quality: {variant['quality_score']:.4f} · Distance from original:
            {variant['distance_from_original']:.4f} · Minimum selected distance:
            {variant['minimum_selected_distance'] if variant['minimum_selected_distance'] is not None else 'seed'}</p>
            <p><strong>Diversité :</strong> intra {same_minimum if same_minimum is not None else 'no_reference_yet'} / seuil {same_threshold} / marge {same_margin if same_margin is not None else 'n/a'} · catalogue {catalog_minimum if catalog_minimum is not None else 'no_reference_yet'} / seuil {catalog_threshold} / marge {catalog_margin if catalog_margin is not None else 'n/a'}.</p>
            <p>Image limitante : index {limiting[1] if limiting[1] is not None else 'n/a'} · voisin {html.escape(str(limiting[2].get('listing_id', 'n/a')))} / variant {limiting[2].get('variant_id', 'source')} · composantes {html.escape(str(limiting[2].get('components', {})))}</p>
            <p>Minimum SSIM: {aggregate.get('min_ssim', 'n/a')} · Maximum pixel MAE: {aggregate.get('max_pixel_mae', 'n/a')}
            · Maximum luminance MAE: {aggregate.get('max_luminance_mae', 'n/a')} · Maximum sharpness ratio: {aggregate.get('max_sharpness_ratio', 'n/a')}</p>
            <p>Active parameters: {aggregate.get('active_parameter_count', 0)} · Recipe intensity: {aggregate.get('recipe_intensity', 0)}
            · {html.escape(', '.join(aggregate.get('active_parameters', [])) or 'none')}</p>
            <p>Geometry: rotation {recipe_values.get('rotation_degrees', 0)}° · crop {recipe_values.get('crop_fraction', 0)}
            · zoom {recipe_values.get('zoom', 1)} · resize {recipe_values.get('resize_scale', 1)}
            · offset x {recipe_values.get('offset_x', 0)} · offset y {recipe_values.get('offset_y', 0)}</p>
            <p>Canvas: {html.escape(str(recipe_values.get('canvas_mode', 'none')))} · horizontal {recipe_values.get('canvas_padding_x', 0)} · vertical {recipe_values.get('canvas_padding_y', 0)}
            · background origin {html.escape(str(aggregate.get('background_origin', 'per-image')))} · background RGB {html.escape(str(aggregate.get('background_rgb', 'per-image')))}
            · sampled RGB {html.escape(str(aggregate.get('sampled_background_rgb', 'per-image')))} · confidence {aggregate.get('mean_sampled_background_confidence', 'per-image')}
            · canvas fraction {aggregate.get('mean_canvas_fraction', 0)} · foreground scale {aggregate.get('mean_foreground_scale_ratio', 1)}</p>
            <p><a href="{folder}">Open local variant folder</a></p>
            <div class="images">{image_html}</div>
            <details><summary>Canonical recipe</summary><pre>{recipe}</pre></details>
            <details><summary>Quality metrics</summary><pre>{metrics}</pre></details>
            <details><summary>Minimum-distance components</summary><pre>{distance_components}</pre></details>
            <dl><dt>Title</dt><dd>{html.escape(variant['title_text'] or 'Reserved')}</dd>
            <dt>Description</dt><dd>{html.escape(variant['description_text'] or 'Reserved')}</dd>
            <dt>Price</dt><dd>{variant['price_cents'] if variant['price_cents'] is not None else 'Reserved'} {html.escape(variant['currency'] or '')}</dd>
            <dt>Metadata status</dt><dd>{html.escape(variant['metadata_status'])}</dd></dl>
            </article>"""
        )
    counter_text = " · ".join(
        f"{html.escape(key)}: {value}" for key, value in sorted(counters.items())
        if not key.startswith(("family_tested_", "family_valid_", "family_selected_"))
    )
    tested_distribution = _format_distribution(_distribution(counters, "family_tested_"))
    valid_distribution = _format_distribution(_distribution(counters, "family_valid_"))
    selected_values = _distribution(counters, "family_selected_")
    if sum(selected_values.values()) != len(variants):
        raise ValueError("selected recipe-family counters do not match selected variants")
    selected_distribution = _format_distribution(selected_values)
    dezoom_distribution = _format_distribution(_distribution(counters, "dezoom_canvas_"))
    same_distances = [float(row["minimum_same_listing_distance"]) for row in variants if row["minimum_same_listing_distance"] is not None]
    distance_summary = "no_reference_yet" if not same_distances else f"min {min(same_distances):.5f} · moyenne {sum(same_distances)/len(same_distances):.5f} · médiane {sorted(same_distances)[len(same_distances)//2]:.5f} · max {max(same_distances):.5f}"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Catalog benchmark {html.escape(run_id)}</title>
<style>
body{{font:15px system-ui;margin:2rem;background:#f5f5f5;color:#222}}main{{max-width:1400px;margin:auto}}
.summary,.variant{{background:white;padding:1rem 1.25rem;margin:1rem 0;border-radius:10px;box-shadow:0 1px 5px #0002}}
.images{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}}figure{{margin:0}}
img{{display:block;width:100%;height:260px;object-fit:contain;background:#eee;border-radius:8px}}pre{{overflow:auto;max-height:24rem}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:.35rem 1rem}}dt{{font-weight:700}}
</style></head><body><main>
<h1>Catalog benchmark</h1><section class="summary"><p>Run: {html.escape(run_id)}</p>
<p>Status: <strong>{html.escape(status)}</strong> · Stop reason: <strong>{html.escape(stop_reason)}</strong></p>
<p>Listing: {html.escape(listing.listing_code)} · Source images: {len(listing.images)} · Source set: <code>{listing.source_set_hash}</code></p>
<p>Requested variants: {requested} · Obtained variants: {len(variants)}</p><p>{counter_text}</p>
<p>Recipe families tested: {tested_distribution}</p>
<p>Recipe families valid: {valid_distribution}</p>
<p>Recipe families selected: {selected_distribution}</p>
<p>Selected geometry: rotation {counters.get('variants_with_rotation', 0)} · crop {counters.get('variants_with_crop', 0)} · zoom {counters.get('variants_with_zoom', 0)} · dezoom {counters.get('variants_with_dezoom', 0)}</p>
<p>Dezoom canvas modes: {dezoom_distribution}</p>
<p>Diversité intra-annonce des variantes ready : {distance_summary}</p>
<p><button onclick="filterCards('all')">Toutes</button> <button onclick="filterCards('near')">Proches du seuil</button> <button onclick="filterCards('far')">Les plus différentes</button></p></section>
{''.join(cards) or '<p>No complete selected variant.</p>'}
<script>function filterCards(mode){{document.querySelectorAll('.variant').forEach(card=>{{const margin=Number(card.querySelector('h2').dataset.margin);card.style.display=mode==='all'||mode==='near'&&!(margin>=0.01)||mode==='far'&&margin>=0.01?'block':'none'}})}}</script></main></body></html>"""
    output.write_text(document, encoding="utf-8")
    return output
