from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

_STATUS_KEYS = {
    "status",
    "state",
    "label",
    "labels",
    "decision",
    "verdict",
    "bucket",
    "category",
    "classification",
    "result",
    "review_status",
    "review_label",
}
_PATH_KEYS = {
    "path",
    "file",
    "file_path",
    "filepath",
    "output_path",
    "image_path",
    "preview_path",
    "html_path",
    "report_path",
    "uri",
    "url",
}
_DEFAULT_TARGETS = "suspect,suspects,review_candidate,review_candidates,review-candidate,review-candidates,review"
_RECIPE_DIR_RE = re.compile(r"recipe_\d+_([0-9a-fA-F]{6,64})")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _norm(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _target_set(target_labels: str | Iterable[str] | None) -> set[str]:
    if target_labels is None:
        target_labels = _DEFAULT_TARGETS
    if isinstance(target_labels, str):
        items = [part.strip() for part in target_labels.split(",")]
    else:
        items = [str(part).strip() for part in target_labels]
    return {_norm(item) for item in items if item}


def _file_uri(raw_path: Any) -> str:
    text = str(raw_path or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "file://")):
        return text
    try:
        return Path(text).expanduser().resolve().as_uri()
    except Exception:
        normalized = text.replace("\\", "/")
        if len(normalized) >= 2 and normalized[1] == ":":
            return "file:///" + normalized.replace(" ", "%20")
        return "file://" + normalized.replace(" ", "%20")


def _safe_json(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except Exception:
            return text
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _short(value: Any, max_len: int = 220) -> str:
    text = str(value or "")
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _extract_label(row: dict[str, Any], targets: set[str]) -> str | None:
    for key, value in row.items():
        if _norm(key) not in _STATUS_KEYS:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            normalized = _norm(item)
            if normalized in targets:
                return str(item)
    return None


def _extract_path(row: dict[str, Any]) -> str:
    for key in ("output_path", "image_path", "preview_path", "html_path", "path", "file_path", "file", "url", "uri"):
        if key in row and row[key]:
            return str(row[key])
    for key, value in row.items():
        if _norm(key) in _PATH_KEYS and value:
            return str(value)
    return ""


def _recipe_prefix_from_path(path: str) -> str:
    if not path:
        return ""
    match = _RECIPE_DIR_RE.search(path.replace("\\", "/"))
    return match.group(1).lower() if match else ""


def _collect_targets_from_report(report: dict[str, Any], targets: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in _walk_dicts(report):
        label = _extract_label(row, targets)
        if not label:
            continue
        path = _extract_path(row)
        matches.append(
            {
                "label": label,
                "recipe_id": str(row.get("recipe_id") or row.get("recipe") or row.get("id") or ""),
                "output_id": str(row.get("output_id") or ""),
                "path": path,
                "source": "json",
                "params": row.get("params") or row.get("params_json") or row.get("parameters") or "",
                "recipe_prefix": _recipe_prefix_from_path(path),
            }
        )
    return matches


def _collect_targets_from_csv(path: Path, targets: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return matches
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                data = dict(row)
                label = _extract_label(data, targets)
                if not label:
                    continue
                out_path = _extract_path(data)
                matches.append(
                    {
                        "label": label,
                        "recipe_id": str(data.get("recipe_id") or data.get("recipe") or data.get("id") or ""),
                        "output_id": str(data.get("output_id") or ""),
                        "path": out_path,
                        "source": f"csv:{path.name}",
                        "params": data.get("params") or data.get("params_json") or data.get("parameters") or "",
                        "recipe_prefix": _recipe_prefix_from_path(out_path),
                    }
                )
    except Exception:
        return matches
    return matches


def _collect_targets_from_db(path: Path, targets: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return matches
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            status_cols = [col for col in columns if _norm(col) in _STATUS_KEYS]
            if not status_cols:
                continue
            select_cols = ", ".join([f'"{col}"' for col in columns])
            placeholders = ",".join(["?"] * len(targets))
            for status_col in status_cols:
                query = f'SELECT {select_cols} FROM "{table}" WHERE lower(replace(replace(CAST("{status_col}" AS TEXT), "-", "_"), " ", "_")) IN ({placeholders})'
                for row in conn.execute(query, sorted(targets)).fetchall():
                    data = dict(row)
                    out_path = _extract_path(data)
                    matches.append(
                        {
                            "label": str(data.get(status_col, "")),
                            "recipe_id": str(data.get("recipe_id") or data.get("recipe") or data.get("id") or ""),
                            "output_id": str(data.get("output_id") or ""),
                            "path": out_path,
                            "source": f"db:{table}",
                            "params": data.get("params") or data.get("params_json") or data.get("parameters") or "",
                            "recipe_prefix": _recipe_prefix_from_path(out_path),
                        }
                    )
    except Exception:
        return matches
    finally:
        if conn is not None:
            conn.close()
    return matches


def _collect_targets_from_tree(output_dir: Path, targets: set[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not output_dir.exists() or not output_dir.is_dir():
        return matches
    suffixes = {".html", ".htm", ".json", ".csv", ".jpg", ".jpeg", ".png", ".webp"}
    scanned = 0
    try:
        for path in output_dir.rglob("*"):
            scanned += 1
            if scanned > 10000:
                break
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            normalized = _norm(str(path.relative_to(output_dir)))
            label = next((target for target in targets if target in normalized), "")
            if not label:
                continue
            path_text = str(path)
            matches.append(
                {
                    "label": label,
                    "recipe_id": "",
                    "output_id": "",
                    "path": path_text,
                    "source": "tree",
                    "params": "",
                    "recipe_prefix": _recipe_prefix_from_path(path_text),
                }
            )
    except Exception:
        return matches
    return matches


def _dedupe_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for match in matches:
        key = (
            str(match.get("label", "")),
            str(match.get("recipe_id", "")),
            str(match.get("output_id", "")),
            str(match.get("path", "")),
            str(match.get("source", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _collect_target_matches(report: dict[str, Any], targets: set[str]) -> list[dict[str, Any]]:
    reports = report.get("reports", {}) if isinstance(report.get("reports"), dict) else {}
    output_dir = Path(str(report.get("output_dir", ""))) if report.get("output_dir") else None
    database = Path(str(report.get("database", ""))) if report.get("database") else None
    matches: list[dict[str, Any]] = []
    matches.extend(_collect_targets_from_report(report, targets))
    if reports.get("csv"):
        matches.extend(_collect_targets_from_csv(Path(str(reports["csv"])), targets))
    if database:
        matches.extend(_collect_targets_from_db(database, targets))
    if output_dir:
        matches.extend(_collect_targets_from_tree(output_dir, targets))
    return _dedupe_matches(matches)


def _build_recipe_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for recipe in report.get("recipes", []) or []:
        recipe_id = str(recipe.get("recipe_id") or "")
        if not recipe_id:
            continue
        index.setdefault(recipe_id, {"recipe_id": recipe_id, "params": recipe.get("params") or {}, "outputs": []})
    for row in report.get("outputs", []) or []:
        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            continue
        item = index.setdefault(recipe_id, {"recipe_id": recipe_id, "params": row.get("params") or {}, "outputs": []})
        if not item.get("params"):
            item["params"] = row.get("params") or {}
        output_path = str(row.get("output_path") or "")
        if output_path:
            item.setdefault("outputs", []).append(output_path)
    return index


def _resolve_recipe_id(match: dict[str, Any], recipe_index: dict[str, dict[str, Any]]) -> str:
    raw = str(match.get("recipe_id") or "").strip()
    if raw in recipe_index:
        return raw
    if raw:
        matches = [recipe_id for recipe_id in recipe_index if recipe_id.startswith(raw.lower()) or recipe_id.startswith(raw)]
        if len(matches) == 1:
            return matches[0]
    prefix = str(match.get("recipe_prefix") or "").lower()
    if prefix:
        matches = [recipe_id for recipe_id in recipe_index if recipe_id.lower().startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
    path_prefix = _recipe_prefix_from_path(str(match.get("path") or ""))
    if path_prefix:
        matches = [recipe_id for recipe_id in recipe_index if recipe_id.lower().startswith(path_prefix)]
        if len(matches) == 1:
            return matches[0]
    return raw


def _choose_example_path(group: dict[str, Any], recipe_info: dict[str, Any] | None) -> str:
    for match in group.get("matches", []):
        path = str(match.get("path") or "")
        if path and Path(path).suffix.lower() in _IMAGE_SUFFIXES:
            return path
    for match in group.get("matches", []):
        path = str(match.get("path") or "")
        if path:
            return path
    if recipe_info:
        for path in recipe_info.get("outputs", []) or []:
            if Path(str(path)).suffix.lower() in _IMAGE_SUFFIXES:
                return str(path)
        outputs = recipe_info.get("outputs", []) or []
        if outputs:
            return str(outputs[0])
    return ""


def _group_target_filters(report: dict[str, Any], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recipe_index = _build_recipe_index(report)
    grouped: dict[str, dict[str, Any]] = {}
    loose_index = 0
    for match in matches:
        recipe_id = _resolve_recipe_id(match, recipe_index)
        if not recipe_id:
            loose_index += 1
            recipe_id = f"unresolved_{loose_index:04d}"
        group = grouped.setdefault(recipe_id, {"recipe_id": recipe_id, "matches": []})
        group["matches"].append(match)

    rows: list[dict[str, Any]] = []
    for recipe_id, group in grouped.items():
        recipe_info = recipe_index.get(recipe_id)
        labels = sorted({str(match.get("label") or "") for match in group["matches"] if match.get("label")})
        sources = sorted({str(match.get("source") or "") for match in group["matches"] if match.get("source")})
        params = ""
        if recipe_info and recipe_info.get("params"):
            params = _safe_json(recipe_info["params"])
        else:
            for match in group["matches"]:
                if match.get("params"):
                    params = _safe_json(match["params"])
                    break
        example_path = _choose_example_path(group, recipe_info)
        rows.append(
            {
                "recipe_id": recipe_id,
                "labels": labels,
                "label": ", ".join(labels),
                "match_count": len(group["matches"]),
                "sources": sources,
                "source": ", ".join(sources),
                "params_json": params,
                "example_path": example_path,
                "example_uri": _file_uri(example_path) if example_path else "",
                "matches": group["matches"],
            }
        )
    rows.sort(key=lambda row: (-int(row["match_count"]), row["label"], row["recipe_id"]))
    return rows


def _write_target_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["recipe_id", "label", "match_count", "source", "example_path", "example_uri", "params_json"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _write_target_html(path: Path, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    title = f"Target filters - {report.get('listing_code', '')}"
    lines = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:Arial;margin:24px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ddd;padding:8px;vertical-align:top}th{background:#f5f5f5}code{background:#f7f7f7;padding:2px 4px;white-space:pre-wrap}img{max-width:260px;max-height:260px;border:1px solid #ddd}</style>",
        "</head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p>Strategy: <code>{html.escape(str(report.get('search_strategy', '')))}</code> | target filters: <strong>{len(rows)}</strong></p>",
    ]
    if not rows:
        lines.append("<p>Aucun filtre cible detecte dans le JSON, le CSV, la DB ou l'arborescence de sortie.</p>")
    else:
        lines.append("<table><thead><tr><th>#</th><th>Label</th><th>Recipe</th><th>Example</th><th>Params</th><th>Sources</th></tr></thead><tbody>")
        for idx, row in enumerate(rows, 1):
            example_uri = row.get("example_uri") or ""
            example_path = row.get("example_path") or ""
            if example_uri and Path(str(example_path)).suffix.lower() in _IMAGE_SUFFIXES:
                example_html = f"<a href='{html.escape(example_uri)}'><img src='{html.escape(example_uri)}' alt='example'></a><br><a href='{html.escape(example_uri)}'>open</a>"
            elif example_uri:
                example_html = f"<a href='{html.escape(example_uri)}'>open</a><br><code>{html.escape(_short(example_path, 160))}</code>"
            else:
                example_html = "-"
            lines.append(
                "<tr>"
                f"<td>{idx}</td>"
                f"<td><strong>{html.escape(row.get('label', ''))}</strong><br>matches: {int(row.get('match_count', 0))}</td>"
                f"<td><code>{html.escape(row.get('recipe_id', ''))}</code></td>"
                f"<td>{example_html}</td>"
                f"<td><code>{html.escape(_short(row.get('params_json', ''), 1200))}</code></td>"
                f"<td>{html.escape(row.get('source', ''))}</td>"
                "</tr>"
            )
        lines.append("</tbody></table>")
    lines.append("</body></html>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_filter_archive(report: dict[str, Any], target_labels: str | Iterable[str] | None = None) -> dict[str, Any]:
    """Write a target-only archive, grouped as one row per matching filter.

    This function intentionally does not create an all-filters index. It only
    stores filters that have at least one target label in the report, CSV, DB,
    or output tree.
    """
    targets = _target_set(target_labels)
    output_dir = Path(str(report.get("output_dir", ""))) if report.get("output_dir") else Path.cwd()
    archive_dir = output_dir / "target_filter_archive"
    matches = _collect_target_matches(report, targets)
    rows = _group_target_filters(report, matches)
    html_path = archive_dir / "target_filters_by_recipe.html"
    csv_path = archive_dir / "target_filters_by_recipe.csv"
    _write_target_html(html_path, rows, report)
    _write_target_csv(csv_path, rows)
    return {
        "targets": sorted(targets),
        "target_matches": len(matches),
        "target_filters": len(rows),
        "html": str(html_path),
        "csv": str(csv_path),
        "rows": rows,
    }


def format_filter_archive_summary(archive: dict[str, Any], limit: int = 20) -> str:
    rows = archive.get("rows", []) or []
    lines = ["TARGET FILTERS"]
    lines.append(f"targets: {', '.join(archive.get('targets', []))}")
    lines.append(f"target_filters: {archive.get('target_filters', 0)}")
    lines.append(f"target_matches: {archive.get('target_matches', 0)}")
    if archive.get("html"):
        lines.append(f"html: {_file_uri(archive['html'])}")
    if archive.get("csv"):
        lines.append(f"csv: {_file_uri(archive['csv'])}")
    if not rows:
        lines.append("filters: none detected")
        lines.append("note: aucun champ status/label/decision/verdict/result cible n'a ete trouve dans le JSON, CSV, DB ou l'arborescence de sortie.")
        return "\n".join(lines)

    lines.append("filters:")
    for idx, row in enumerate(rows[: max(1, int(limit))], 1):
        lines.append(f"  {idx:02d}. {row.get('label', '')} | matches={row.get('match_count', 0)} | recipe={str(row.get('recipe_id', ''))[:12]}")
        if row.get("example_uri"):
            lines.append(f"      example: {row['example_uri']}")
        if row.get("params_json"):
            lines.append(f"      params: {_short(row['params_json'], 260)}")
    if len(rows) > limit:
        lines.append(f"  ... {len(rows) - limit} autres filtres dans le HTML/CSV")
    return "\n".join(lines)


# Explicit aliases for future imports while keeping backward compatibility with
# the previous patch that already calls write_filter_archive(...).
write_target_filter_archive = write_filter_archive
format_target_filter_archive_summary = format_filter_archive_summary
