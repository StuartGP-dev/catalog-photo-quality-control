from __future__ import annotations

import argparse
import html
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .variants_db import VariantsDatabase


def _value(value: object) -> str:
    if value is None:
        return "<span class=\"null\">NULL</span>"
    text = str(value)
    if len(text) > 500:
        return f"<details><summary>{len(text)} caractères</summary><pre>{html.escape(text)}</pre></details>"
    return html.escape(text)


def _row_table(row: sqlite3.Row, css_class: str = "columns") -> str:
    cells = "".join(
        f"<tr><th>{html.escape(column)}</th><td>{_value(row[column])}</td></tr>"
        for column in row.keys()
    )
    return f'<table class="{css_class}"><tbody>{cells}</tbody></table>'


def _relative_asset(path: str | Path, output_dir: Path) -> str | None:
    target = Path(path)
    if not target.is_file():
        return None
    return quote(Path(os.path.relpath(target, output_dir)).as_posix(), safe="/.-_~")


def write_catalog_report(database_path: str | Path, output_path: str | Path) -> Path:
    database = Path(database_path).resolve()
    output = Path(output_path).resolve()
    if output.name != "index.html":
        raise ValueError("catalog report output must be named index.html")
    with VariantsDatabase(database) as variants:
        variants.initialize()
        connection = variants.connection
        listings = connection.execute("SELECT * FROM listings ORDER BY listing_code").fetchall()
        ready_count = connection.execute(
            "SELECT COUNT(*) FROM listing_variants WHERE status='ready'"
        ).fetchone()[0]
        sections: list[str] = []
        for listing in listings:
            variant_rows = connection.execute(
                """SELECT * FROM listing_variants
                   WHERE listing_id=?
                   ORDER BY source_set_hash, selected_rank, variant_id""",
                (listing["listing_id"],),
            ).fetchall()
            variant_parts: list[str] = []
            for variant in variant_rows:
                images = connection.execute(
                    """SELECT * FROM listing_variant_images
                       WHERE variant_id=? ORDER BY image_index""",
                    (variant["variant_id"],),
                ).fetchall()
                image_parts: list[str] = []
                for image in images:
                    asset = _relative_asset(image["output_path"], output.parent)
                    preview = (
                        f'<a href="{html.escape(asset)}"><img src="{html.escape(asset)}" loading="lazy" alt="image {image["image_index"]}"></a>'
                        if asset else '<div class="missing">Fichier manquant</div>'
                    )
                    image_parts.append(
                        f'<article class="image"><h4>Image #{image["image_index"]}</h4>{preview}{_row_table(image)}</article>'
                    )
                variant_parts.append(
                    f'''<details class="variant" open>
                    <summary>Variant #{variant["variant_id"]} · rang sélection {variant["selected_rank"]} · {html.escape(variant["status"])}</summary>
                    {_row_table(variant)}
                    <div class="images">{"".join(image_parts) or "<p>Aucune image.</p>"}</div>
                    </details>'''
                )
            source_versions = connection.execute(
                "SELECT COUNT(DISTINCT source_set_hash) FROM listing_images WHERE listing_id=?",
                (listing["listing_id"],),
            ).fetchone()[0]
            sections.append(
                f'''<section class="listing" data-search="{html.escape(str(listing["listing_code"]).lower())}">
                <h2>{html.escape(listing["listing_code"])}</h2>
                <p>{source_versions} version(s) source · {len(variant_rows)} variant(s)</p>
                <details><summary>Colonnes de l'annonce</summary>{_row_table(listing)}</details>
                {"".join(variant_parts) or "<p>Aucun variant.</p>"}
                </section>'''
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    document = f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Catalogue des annonces</title>
<style>
body{{font:14px system-ui;margin:0;background:#f3f4f6;color:#17202a}}main{{max-width:1600px;margin:auto;padding:2rem}}
.summary,.listing,.variant,.image{{background:#fff;border:1px solid #dfe3e8;border-radius:10px;padding:1rem;margin:1rem 0}}
.variant>summary,.listing>details>summary{{cursor:pointer;font-weight:700}}.images{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}}
.image{{min-width:0}}img{{display:block;width:100%;height:260px;object-fit:contain;background:#eee;border-radius:6px}}
table{{border-collapse:collapse;width:100%;margin:.75rem 0;table-layout:fixed}}th,td{{border:1px solid #dfe3e8;padding:.4rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}}
th{{width:240px;background:#f7f8fa}}pre{{white-space:pre-wrap;margin:.5rem 0}}.null{{color:#777;font-style:italic}}.missing{{padding:3rem;background:#fee;color:#900}}input{{width:min(500px,100%);padding:.65rem}}
</style></head><body><main>
<h1>Catalogue des annonces</h1>
<section class="summary"><p><strong>{len(listings)}</strong> annonce(s) · <strong>{ready_count}</strong> variant(s) ready</p>
<label>Filtrer par code d'annonce<br><input id="filter" type="search" placeholder="Ex. O18"></label></section>
{"".join(sections) or "<p>La base ne contient aucune annonce.</p>"}
</main><script>
document.getElementById('filter').addEventListener('input', event => {{
 const value=event.target.value.toLowerCase();
 document.querySelectorAll('.listing').forEach(item => item.hidden=!item.dataset.search.includes(value));
}});
</script></body></html>'''
    output.write_text(document, encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a generic HTML view of the final variants database.")
    parser.add_argument("--database", default="local/databases/catalog_variants.sqlite3")
    parser.add_argument("--output", default="local/catalog/index.html")
    args = parser.parse_args(argv)
    report = write_catalog_report(args.database, args.output)
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
