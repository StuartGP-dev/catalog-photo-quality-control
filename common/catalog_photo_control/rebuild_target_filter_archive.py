from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from typing import Any

TARGETS = {"review", "review_candidate", "review_candidates", "suspect", "suspects"}


def _norm_label(row: dict[str, Any]) -> str | None:
    values: list[Any] = []

    for key in ("status", "label", "verdict", "decision", "result"):
        values.append(row.get(key))

    bench_eval = row.get("bench_evaluation")
    if isinstance(bench_eval, dict):
        for key in ("status", "label", "verdict", "decision", "result"):
            values.append(bench_eval.get(key))

    for value in values:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in TARGETS:
                return v

    return None


def _score(row: dict[str, Any]) -> float:
    for key in ("bench_score", "score"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)

    bench_eval = row.get("bench_evaluation")
    if isinstance(bench_eval, dict):
        for key in ("score", "bench_score"):
            value = bench_eval.get(key)
            if isinstance(value, (int, float)):
                return float(value)

    return 0.0


def rebuild(report_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    outputs = report.get("outputs", [])
    if not isinstance(outputs, list):
        raise RuntimeError("Le rapport JSON ne contient pas outputs[]")

    out_dir = output_dir or (report_path.parent / "target_filter_archive_clean")
    out_dir.mkdir(parents=True, exist_ok=True)

    seen_outputs: set[str] = set()
    grouped: dict[str, dict[str, Any]] = {}

    for row in outputs:
        if not isinstance(row, dict):
            continue

        label = _norm_label(row)
        if not label:
            continue

        output_id = str(row.get("output_id") or "")
        output_path = str(row.get("output_path") or "")
        dedupe_key = output_id or output_path
        if not dedupe_key or dedupe_key in seen_outputs:
            continue

        seen_outputs.add(dedupe_key)

        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            continue

        score = _score(row)
        item = grouped.setdefault(
            recipe_id,
            {
                "recipe_id": recipe_id,
                "labels": set(),
                "matches": 0,
                "suspect_matches": 0,
                "review_matches": 0,
                "review_candidate_matches": 0,
                "max_score": 0.0,
                "score_sum": 0.0,
                "example_output_path": output_path,
                "example_score": score,
                "params": row.get("params", {}),
            },
        )

        item["labels"].add(label)
        item["matches"] += 1
        item["score_sum"] += score

        if label == "suspect" or label == "suspects":
            item["suspect_matches"] += 1
        elif label == "review":
            item["review_matches"] += 1
        else:
            item["review_candidate_matches"] += 1

        if score >= float(item["max_score"]):
            item["max_score"] = score
            item["example_score"] = score
            item["example_output_path"] = output_path
            item["params"] = row.get("params", item["params"])

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        item["labels"] = ", ".join(sorted(item["labels"]))
        item["avg_score"] = float(item["score_sum"]) / max(1, int(item["matches"]))
        rows.append(item)

    rows.sort(
        key=lambda r: (
            -int(r["suspect_matches"]),
            -float(r["max_score"]),
            -int(r["matches"]),
            str(r["recipe_id"]),
        )
    )

    csv_path = out_dir / "target_filters_by_recipe_clean.csv"
    html_path = out_dir / "target_filters_by_recipe_clean.html"

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "recipe_id",
                "labels",
                "matches",
                "suspect_matches",
                "review_matches",
                "review_candidate_matches",
                "max_score",
                "avg_score",
                "example_output_path",
                "params_json",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "recipe_id": row["recipe_id"],
                    "labels": row["labels"],
                    "matches": row["matches"],
                    "suspect_matches": row["suspect_matches"],
                    "review_matches": row["review_matches"],
                    "review_candidate_matches": row["review_candidate_matches"],
                    "max_score": round(float(row["max_score"]), 6),
                    "avg_score": round(float(row["avg_score"]), 6),
                    "example_output_path": row["example_output_path"],
                    "params_json": json.dumps(row["params"], ensure_ascii=False, sort_keys=True),
                }
            )

    lines = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        "<title>Target filters clean</title>",
        "<style>",
        "body{font-family:Arial;margin:24px}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "td,th{border:1px solid #ddd;padding:6px;vertical-align:top}",
        "th{background:#f5f5f5;position:sticky;top:0}",
        "code{background:#f7f7f7;padding:2px 4px}",
        "img{max-width:220px;border:1px solid #ddd}",
        ".suspect{background:#fff1f1}",
        ".score{font-weight:bold}",
        "</style></head><body>",
        f"<h1>Target filters clean - {html.escape(str(report.get('listing_code', 'listing')))}</h1>",
        f"<p>Report source: <code>{html.escape(str(report_path))}</code></p>",
        f"<p>Target filters: <strong>{len(rows)}</strong> | Target outputs: <strong>{len(seen_outputs)}</strong></p>",
        "<table><thead><tr>",
        "<th>#</th><th>Labels</th><th>Score max</th><th>Score moyen</th><th>Matches</th><th>Recipe</th><th>Example</th><th>Params</th>",
        "</tr></thead><tbody>",
    ]

    for idx, row in enumerate(rows, start=1):
        example = Path(str(row["example_output_path"]))
        example_uri = "file:///" + example.as_posix()
        params_json = html.escape(json.dumps(row["params"], ensure_ascii=False, sort_keys=True))
        cls = "suspect" if int(row["suspect_matches"]) > 0 else ""
        lines.append(
            f"<tr class='{cls}'>"
            f"<td>{idx}</td>"
            f"<td><strong>{html.escape(str(row['labels']))}</strong><br>"
            f"suspect={int(row['suspect_matches'])}<br>"
            f"review={int(row['review_matches'])}<br>"
            f"review_candidate={int(row['review_candidate_matches'])}</td>"
            f"<td class='score'>{float(row['max_score']):.4f}</td>"
            f"<td>{float(row['avg_score']):.4f}</td>"
            f"<td>{int(row['matches'])}</td>"
            f"<td><code>{html.escape(str(row['recipe_id'])[:12])}</code></td>"
            f"<td><a href='{html.escape(example_uri)}'>open</a><br>"
            f"<img src='{html.escape(example_uri)}'><br>"
            f"<code>{html.escape(str(example))}</code></td>"
            f"<td><code>{params_json}</code></td>"
            "</tr>"
        )

    lines.append("</tbody></table></body></html>")
    html_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "target_filters": len(rows),
        "target_matches": len(seen_outputs),
        "html": str(html_path),
        "csv": str(csv_path),
        "top_filters": rows[:10],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconstruit un target filter archive propre et deduplique.")
    parser.add_argument("--report", required=True, help="Chemin du client_render_sampler_report.json")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args(argv)

    result = rebuild(Path(args.report), Path(args.output_dir) if args.output_dir else None)
    print(f"target_filters: {result['target_filters']}")
    print(f"target_matches: {result['target_matches']}")
    print(f"html: file:///{Path(result['html']).as_posix()}")
    print(f"csv: file:///{Path(result['csv']).as_posix()}")

    for idx, row in enumerate(result["top_filters"][:5], start=1):
        print(
            f"{idx:02d}. {row['labels']} | score={float(row['max_score']):.4f} | "
            f"matches={int(row['matches'])} | recipe={str(row['recipe_id'])[:12]}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
