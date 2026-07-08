from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

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
    text = str(raw_path).strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "file://")):
        return text
    try:
        return Path(text).expanduser().resolve().as_uri()
    except Exception:
        normalized = text.replace("\\", "/")
        if len(normalized) >= 2 and normalized[1] == ":":
            return "file:///" + quote(normalized)
        return "file://" + quote(normalized)


def _short_params(params: Any, max_len: int = 180) -> str:
    if not params:
        return ""
    if isinstance(params, str):
        text = params
    else:
        text = json.dumps(params, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


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


def _extract_path(row: dict[str, Any]) -> str | None:
    for key in ("output_path", "image_path", "preview_path", "html_path", "path", "file_path", "file", "url", "uri"):
        if key in row and row[key]:
            return str(row[key])
    for key, value in row.items():
        if _norm(key) in _PATH_KEYS and value:
            return str(value)
    return None


def _row_entry(row: dict[str, Any], label: str, source: str) -> dict[str, Any]:
    return {
        "label": label,
        "path": _extract_path(row) or "",
        "source": source,
        "recipe_id": row.get("recipe_id") or row.get("recipe") or row.get("id") or "",
        "params": row.get("params") or row.get("params_json") or row.get("parameters") or "",
    }


def _walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _collect_from_report(report: dict[str, Any], targets: set[str], limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in _walk_dicts(report):
        label = _extract_label(row, targets)
        if label is None:
            continue
        entries.append(_row_entry(row, label, "json"))
        if len(entries) >= limit:
            break
    return entries


def _collect_from_csv(path: Path, targets: set[str], limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return entries
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                label = _extract_label(dict(row), targets)
                if label is None:
                    continue
                entries.append(_row_entry(dict(row), label, f"csv:{path.name}"))
                if len(entries) >= limit:
                    break
    except Exception:
        return entries
    return entries


def _collect_from_db(path: Path, targets: set[str], limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return entries
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
            for status_col in status_cols:
                rows = conn.execute(f'SELECT {select_cols} FROM "{table}" WHERE lower(replace(replace(CAST("{status_col}" AS TEXT), "-", "_"), " ", "_")) IN ({",".join(["?"] * len(targets))}) LIMIT ?', [*sorted(targets), limit]).fetchall()
                for db_row in rows:
                    data = dict(db_row)
                    label = str(data.get(status_col, ""))
                    entries.append(_row_entry(data, label, f"db:{table}"))
                    if len(entries) >= limit:
                        return entries
        conn.close()
    except Exception:
        return entries
    return entries


def _collect_from_tree(output_dir: Path, targets: set[str], limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not output_dir.exists() or not output_dir.is_dir():
        return entries
    suffixes = {".html", ".htm", ".json", ".csv", ".jpg", ".jpeg", ".png", ".webp"}
    scanned = 0
    try:
        for path in output_dir.rglob("*"):
            scanned += 1
            if scanned > 5000:
                break
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            normalized = _norm(str(path.relative_to(output_dir)))
            matched = next((target for target in targets if target in normalized), None)
            if matched is None:
                continue
            entries.append({"label": matched, "path": str(path), "source": "tree", "recipe_id": "", "params": ""})
            if len(entries) >= limit:
                break
    except Exception:
        return entries
    return entries


def _dedupe(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (_norm(entry.get("label", "")), str(entry.get("path", "")), str(entry.get("recipe_id", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
        if len(deduped) >= limit:
            break
    return deduped


def format_bench_terminal_summary(report: dict[str, Any], target_labels: str | Iterable[str] | None = None, limit: int = 12) -> str:
    targets = _target_set(target_labels)
    limit = max(1, int(limit))
    reports = report.get("reports", {}) if isinstance(report.get("reports"), dict) else {}
    output_dir = Path(str(report.get("output_dir", ""))) if report.get("output_dir") else None
    database = Path(str(report.get("database", ""))) if report.get("database") else None

    entries: list[dict[str, Any]] = []
    entries.extend(_collect_from_report(report, targets, limit * 2))

    csv_path = reports.get("csv")
    if csv_path:
        entries.extend(_collect_from_csv(Path(str(csv_path)), targets, limit * 2))

    if database:
        entries.extend(_collect_from_db(database, targets, limit * 2))

    if output_dir:
        entries.extend(_collect_from_tree(output_dir, targets, limit * 2))

    entries = _dedupe(entries, limit)

    lines: list[str] = []
    lines.append("BENCH TARGET SUMMARY")
    lines.append(f"targets: {', '.join(sorted(targets))}")
    lines.append(f"matches: {len(entries)}")
    if output_dir:
        lines.append(f"output_dir: {_file_uri(output_dir)}")
    if reports.get("html"):
        lines.append(f"html: {_file_uri(reports['html'])}")
    if reports.get("json"):
        lines.append(f"json: {_file_uri(reports['json'])}")
    if reports.get("csv"):
        lines.append(f"csv: {_file_uri(reports['csv'])}")
    if database:
        lines.append(f"db: {_file_uri(database)}")

    if not entries:
        lines.append("target_outputs: none detected in current report/DB/output tree")
        return "\n".join(lines)

    lines.append("target_outputs:")
    for idx, entry in enumerate(entries, start=1):
        label = entry.get("label", "")
        path = entry.get("path", "")
        recipe_id = entry.get("recipe_id", "")
        params = _short_params(entry.get("params", ""))
        source = entry.get("source", "")
        lines.append(f"  {idx:02d}. [{label}] {source}")
        if path:
            lines.append(f"      link: {_file_uri(path)}")
            lines.append(f"      path: {path}")
        if recipe_id:
            lines.append(f"      recipe_id: {recipe_id}")
        if params:
            lines.append(f"      params: {params}")
    return "\n".join(lines)
