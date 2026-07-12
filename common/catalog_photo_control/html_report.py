from __future__ import annotations

import html
import json
import os
import sqlite3
from pathlib import Path
from typing import Mapping

from .models import SourceListing


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
        image_html = "".join(
            f'<figure><img src="{html.escape(_relative_link(image["output_path"], output.parent))}" '
            f'alt="variant {variant["selected_rank"]}, source image {image["image_index"]}">'
            f'<figcaption>Source #{image["image_index"]}</figcaption></figure>'
            for image in images
        )
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
        cards.append(
            f"""<article class="variant">
            <h2>Variant {variant['selected_rank']:04d}</h2>
            <p>Quality: {variant['quality_score']:.4f} · Distance from original:
            {variant['distance_from_original']:.4f} · Minimum selected distance:
            {variant['minimum_selected_distance'] if variant['minimum_selected_distance'] is not None else 'seed'}</p>
            <p>Minimum SSIM: {aggregate.get('min_ssim', 'n/a')} · Maximum pixel MAE: {aggregate.get('max_pixel_mae', 'n/a')}
            · Maximum luminance MAE: {aggregate.get('max_luminance_mae', 'n/a')} · Maximum sharpness ratio: {aggregate.get('max_sharpness_ratio', 'n/a')}</p>
            <p>Active parameters: {aggregate.get('active_parameter_count', 0)} · Recipe intensity: {aggregate.get('recipe_intensity', 0)}
            · {html.escape(', '.join(aggregate.get('active_parameters', [])) or 'none')}</p>
            <p>Canvas: {html.escape(str(recipe_values.get('canvas_mode', 'none')))} · horizontal {recipe_values.get('canvas_padding_x', 0)} · vertical {recipe_values.get('canvas_padding_y', 0)}
            · background {aggregate.get('sampled_background_rgb', aggregate.get('background_rgb', 'per-image'))} · confidence {aggregate.get('mean_sampled_background_confidence', 'per-image')}
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
    )
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
<p>Requested variants: {requested} · Obtained variants: {len(variants)}</p><p>{counter_text}</p></section>
{''.join(cards) or '<p>No complete selected variant.</p>'}
</main></body></html>"""
    output.write_text(document, encoding="utf-8")
    return output
