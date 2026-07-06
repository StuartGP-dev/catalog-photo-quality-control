from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from .photo_comparison_rules import compare_photo_pair, evaluate_metadata_check
from .decision_margin_search import run_decision_margin_search
from .photo_metadata import compare_photo_metadata_records
from .photo_adjustments import generate_photo_adjustment_from_spec, list_photo_adjustment_specs
from .listing_photo_review import _safe_listing_code, find_listing_images, resolve_listing_dir
from .local_paths import default_annonces_root, default_output_root, describe_local_paths

DB_VERSION = 3


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_id(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _distances(comparison: dict[str, Any]) -> dict[str, int]:
    hashes = comparison["visual_check"]["hashes"]
    return {
        "phash": hashes["phash"]["hamming_distance"],
        "dhash": hashes["dhash"]["hamming_distance"],
        "whash": hashes["whash"]["hamming_distance"],
    }


def _mode(listing_code: str) -> str:
    return listing_code.strip().replace("\\", "/").split("/", 1)[0]


def _db_path(output_root: str | Path, listing_code: str) -> Path:
    return Path(output_root) / "_photo_quality_catalog" / f"{_safe_listing_code(listing_code)}.json"


def _empty_db(listing_code: str) -> dict[str, Any]:
    return {"version": DB_VERSION, "listing_code": listing_code, "items": {}, "updated_at": None}


def _load_db(output_root: str | Path, listing_code: str) -> dict[str, Any]:
    data = _read_json(_db_path(output_root, listing_code))
    if not data:
        return _empty_db(listing_code)
    if data.get("version") != DB_VERSION:
        items = data.get("items", {}) if isinstance(data.get("items"), dict) else {}
        return {"version": DB_VERSION, "listing_code": listing_code, "items": items, "updated_at": data.get("updated_at")}
    data.setdefault("items", {})
    return data


def _test_key(listing_code: str, preset: str, sensitivity: str, source_image: str, adjustment_name: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "listing_code": listing_code,
        "mode": _mode(listing_code),
        "preset": preset,
        "sensitivity": sensitivity,
        "source_image": source_image,
        "adjustment_name": adjustment_name,
        "params": params,
    }


def _test_id(key: dict[str, Any]) -> str:
    return _stable_id({"kind": "reference_photo_adjustment_check", **key})


def _known_test_ids(db: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    for item in db.get("items", {}).values():
        if item.get("test_id"):
            known.add(str(item["test_id"]))
            continue
        key = _test_key(
            str(item.get("listing_code", "")),
            str(item.get("preset", "")),
            str(item.get("sensitivity", "")),
            str(item.get("source_image", "")),
            str(item.get("adjustment_name", "")),
            item.get("params", {}) or {},
        )
        known.add(_test_id(key))
    return known


def _store_results(report: dict[str, Any], output_root: str | Path, output_dir: Path) -> dict[str, Any]:
    listing_code = report["listing_code"]
    db_file = _db_path(output_root, listing_code)
    db = _load_db(output_root, listing_code)
    before = len(db["items"])
    now = datetime.now().isoformat(timespec="seconds")
    selected_dir = output_dir / "cases_for_photo_review"
    selected_dir.mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []

    for item in report["reference_photo_checks"]["comparisons"]:
        photo_adjustment = item.get("photo_adjustment", {})
        comparison = item["comparison"]
        visual = comparison["visual_check"]["status"]
        combined = comparison["overall_check"]["status"]
        source_image = item.get("source_image", "")
        adjustment_name = photo_adjustment.get("adjustment_name", "unknown")
        params = photo_adjustment.get("params", {})
        key = _test_key(listing_code, report["preset"], report["sensitivity"], source_image, adjustment_name, params)
        tid = _test_id(key)
        distances = item["distances"]
        result_id = _stable_id({"kind": "reference_photo_adjustment_result", "test_id": tid, "visual": visual, "combined": combined, "distances": distances})
        adjustment_path = photo_adjustment.get("path")
        copied_path = None
        if visual != "match" or combined != "match":
            if adjustment_path and Path(adjustment_path).exists():
                dst = selected_dir / f"{result_id[:10]}_{Path(adjustment_path).name}"
                if not dst.exists():
                    shutil.copy2(adjustment_path, dst)
                copied_path = str(dst)
        row = {
            "case_id": result_id,
            "test_id": tid,
            **key,
            "adjustment_path": adjustment_path,
            "copied_adjustment_path": copied_path,
            "visual_status": visual,
            "overall_check": combined,
            "distances": distances,
            "first_seen_report": str(output_dir),
            "last_seen_report": str(output_dir),
            "first_seen_at": now,
            "last_seen_at": now,
            "seen_count": 1,
        }
        rows.append(row)
        if result_id in db["items"]:
            existing = db["items"][result_id]
            existing["last_seen_report"] = str(output_dir)
            existing["last_seen_at"] = now
            existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
            existing.setdefault("test_id", tid)
            if copied_path and not existing.get("copied_adjustment_path"):
                existing["copied_adjustment_path"] = copied_path
        else:
            db["items"][result_id] = row

    db["updated_at"] = now
    _write_json(db_file, db)
    run_counts = Counter(row["visual_status"] for row in rows)
    db_counts = Counter(row["visual_status"] for row in db["items"].values())
    by_filter = Counter((row["adjustment_name"], row["visual_status"]) for row in db["items"].values())
    summary = {
        "listing_code": listing_code,
        "mode": _mode(listing_code),
        "preset": report["preset"],
        "sensitivity": report["sensitivity"],
        "catalog_file": str(db_file),
        "records_observed_this_run": len(rows),
        "new_records_added": len(db["items"]) - before,
        "catalog_total": len(db["items"]),
        "this_run_visual_counts": dict(run_counts),
        "catalog_visual_counts": dict(db_counts),
        "catalog_by_adjustment_status": [
            {"adjustment_name": key[0], "visual_status": key[1], "count": value}
            for key, value in sorted(by_filter.items())
        ],
    }
    _write_json(output_dir / "photo_quality_catalog_summary.json", summary)
    return summary


def _write_debug_zip(report: dict[str, Any], output_dir: Path, summary: dict[str, Any]) -> Path:
    zip_path = output_dir / "photo_quality_control_bundle.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("photo_quality_control_report.json", "photo_quality_control_report.md", "photo_quality_catalog_summary.json"):
            path = output_dir / name
            if path.exists():
                archive.write(path, path.name)
        db_file = Path(summary["catalog_file"])
        if db_file.exists():
            archive.write(db_file, Path("catalog") / db_file.name)
        selected_dir = output_dir / "cases_for_photo_review"
        if selected_dir.exists():
            for path in selected_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, selected_dir.name / path.relative_to(selected_dir))
    return zip_path


def find_catalog_listing_dirs(mode: str, annonces_root: str | Path | None = None, limit: int = 20, exclude_dir: str | Path | None = None) -> list[Path]:
    root = (Path(annonces_root) if annonces_root is not None else default_annonces_root()) / mode
    if not root.is_dir():
        return []
    excluded = Path(exclude_dir).resolve() if exclude_dir else None
    dirs: list[Path] = []
    for parent_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not parent_dir.is_dir() or parent_dir.name.lower() in {"autre", "__pycache__"}:
            continue
        for listing_dir in sorted(parent_dir.iterdir(), key=lambda p: p.name.lower()):
            if not listing_dir.is_dir() or listing_dir.name.lower() == "autre":
                continue
            if excluded and listing_dir.resolve() == excluded:
                continue
            if (listing_dir / "config.json").exists() and find_listing_images(listing_dir):
                dirs.append(listing_dir)
            if len(dirs) >= limit:
                return dirs
    return dirs


def compare_listing_photos_pairwise(images: list[Path], sensitivity: str = "standard", relation: str = "same_listing") -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for image_a, image_b in combinations(images, 2):
        comparison = compare_photo_pair(image_a, image_b, sensitivity=sensitivity)
        pairs.append({"image_a": str(image_a), "image_b": str(image_b), "relation": relation, "comparison": comparison, "distances": _distances(comparison)})
    return pairs


def compare_listing_against_catalog_set(listing_code: str, annonces_root: str | Path | None = None, max_other_listings: int = 20, sensitivity: str = "standard") -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    source_images = find_listing_images(listing_dir)
    other_dirs = find_catalog_listing_dirs(_mode(listing_code), annonces_root=annonces_root, limit=max_other_listings, exclude_dir=listing_dir)
    comparisons: list[dict[str, Any]] = []
    for other_dir in other_dirs:
        for image_a in source_images:
            for image_b in find_listing_images(other_dir):
                comparison = compare_photo_pair(image_a, image_b, sensitivity=sensitivity)
                comparisons.append({"image_a": str(image_a), "image_b": str(image_b), "relation": "other_listing", "other_listing_dir": str(other_dir), "comparison": comparison, "distances": _distances(comparison)})
    return {"other_listing_dirs": [str(path) for path in other_dirs], "comparisons": comparisons}


def _planned_tests(listing_code: str, images: list[Path], preset: str, sensitivity: str) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for image_path in images:
        source_image = str(image_path)
        for spec in list_photo_adjustment_specs(preset):
            key = _test_key(listing_code, preset, sensitivity, source_image, spec["name"], spec.get("params", {}))
            planned.append({"test_id": _test_id(key), "image_path": image_path, "source_image": source_image, "adjustment_name": spec["name"], "params": spec.get("params", {})})
    return planned


def run_reference_photo_checks(listing_code: str, annonces_root: str | Path | None = None, output_dir: str | Path | None = None, output_root: str | Path | None = None, preset: str = "default", sensitivity: str = "standard", skip_known: bool = True) -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    comparisons: list[dict[str, Any]] = []
    local_output_root = Path(output_root) if output_root is not None else default_output_root()
    local_output_dir = Path(output_dir) if output_dir is not None else local_output_root / _safe_listing_code(listing_code) / datetime.now().strftime("%Y%m%d_%H%M%S_reference")
    adjustments_root = local_output_dir / "photo_adjustments"
    db = _load_db(local_output_root, listing_code)
    known = _known_test_ids(db) if skip_known else set()
    planned = _planned_tests(listing_code, images, preset, sensitivity)
    skipped = []
    for item in planned:
        if item["test_id"] in known:
            skipped.append({"test_id": item["test_id"], "source_image": item["source_image"], "adjustment_name": item["adjustment_name"], "params": item["params"]})
            continue
        image_path = item["image_path"]
        adjustment = generate_photo_adjustment_from_spec(image_path, adjustments_root / image_path.stem / f"{image_path.stem}_{item['adjustment_name']}_{item['test_id'][:8]}.jpg", item["adjustment_name"], preset=preset)
        comparison = compare_photo_pair(image_path, adjustment["path"], sensitivity=sensitivity)
        comparisons.append({"test_id": item["test_id"], "source_image": str(image_path), "photo_adjustment": adjustment, "comparison": comparison, "distances": _distances(comparison)})
    counts = Counter(item["comparison"]["visual_check"]["status"] for item in comparisons)
    potential = [item for item in comparisons if item["comparison"]["visual_check"]["status"] == "clear"]
    return {"counts": dict(counts), "comparisons": comparisons, "reference_photo_cases_to_review": potential, "planned_total": len(planned), "executed_total": len(comparisons), "skipped_known_total": len(skipped), "skipped_known": skipped, "skip_known": skip_known}


def run_catalog_pair_checks(listing_code: str, annonces_root: str | Path | None = None, max_other_listings: int = 20, sensitivity: str = "standard") -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    same_listing = compare_listing_photos_pairwise(images, sensitivity=sensitivity, relation="same_listing")
    other = compare_listing_against_catalog_set(listing_code, annonces_root=annonces_root, max_other_listings=max_other_listings, sensitivity=sensitivity)
    comparisons = same_listing + other["comparisons"]
    counts = Counter(item["comparison"]["visual_check"]["status"] for item in comparisons)
    return {
        "counts": dict(counts),
        "same_listing_pairs": len(same_listing),
        "other_listing_dirs": other["other_listing_dirs"],
        "comparisons": comparisons,
        "catalog_pair_match_cases": [item for item in comparisons if item["comparison"]["visual_check"]["status"] == "match"],
        "catalog_pair_review_cases": [item for item in comparisons if item["comparison"]["visual_check"]["status"] == "review"],
    }


def _metadata(camera_model: str | None = None, taken_at: str | None = None, original_dimensions: dict[str, int] | None = None, gps_present: bool = False, has_any_exif: bool = True) -> dict[str, Any]:
    return {"camera_model": camera_model, "taken_at": taken_at, "original_dimensions": original_dimensions, "current_dimensions": None, "gps_present": gps_present, "has_any_exif": has_any_exif}


def _exif_scenarios() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    base = _metadata("sony a7", "2026:06:01 10:00:00", {"width": 3000, "height": 2000}, False)
    unrelated = _metadata("canon r6", "2026:06:02 11:00:00", {"width": 2400, "height": 1600}, False)
    no_exif = _metadata(has_any_exif=False)
    return {
        "same_all_fields": (base, base.copy()),
        "same_camera_only": (base, _metadata(camera_model="sony a7")),
        "same_date_only": (base, _metadata(taken_at="2026:06:01 10:00:00")),
        "same_dimensions_only": (base, _metadata(original_dimensions={"width": 3000, "height": 2000})),
        "same_camera_and_dimensions": (base, _metadata(camera_model="sony a7", original_dimensions={"width": 3000, "height": 2000})),
        "same_camera_and_date": (base, _metadata(camera_model="sony a7", taken_at="2026:06:01 10:00:00")),
        "missing_candidate_exif": (base, no_exif),
        "missing_both_exif": (no_exif, no_exif.copy()),
        "conflict_camera": (base, _metadata(camera_model="canon r6")),
        "conflict_date": (base, _metadata(taken_at="2026:06:02 11:00:00")),
        "conflict_dimensions": (base, _metadata(original_dimensions={"width": 2400, "height": 1600})),
        "conflict_gps_presence": (_metadata(camera_model="sony a7", gps_present=True), _metadata(camera_model="sony a7", gps_present=False)),
        "same_metadata_on_separate_photo": (unrelated, unrelated.copy()),
    }


def run_metadata_checks(listing_code: str, sensitivity: str = "standard") -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for name, (ref_metadata, candidate_metadata) in _exif_scenarios().items():
        exif = compare_photo_metadata_records(ref_metadata, candidate_metadata)
        metadata_check = evaluate_metadata_check(exif, sensitivity=sensitivity)
        impact = "none"
        if exif["exif_status"] == "conflict":
            impact = "requires_visual_confirmation"
        elif metadata_check["status"] == "review":
            impact = "would_request_manual_review"
        results.append({
            "scenario": name,
            "listing_code": listing_code,
            "metadata_check": {
                "status": metadata_check["status"],
                "reason": metadata_check["reason"],
                "metadata_status": exif["exif_status"],
                "matched_fields": exif["matched_fields"],
                "mismatched_fields": exif["mismatched_fields"],
                "missing_fields": exif["missing_fields"],
            },
            "combined_impact": impact,
        })
    return {"counts": dict(Counter(item["metadata_check"]["metadata_status"] for item in results)), "scenarios": results}


def _md_row_distances(item: dict[str, Any]) -> tuple[int, int, int]:
    distances = item["distances"]
    return distances["phash"], distances["dhash"], distances["whash"]


def _markdown_report(report: dict[str, Any]) -> str:
    reference_control = report["reference_photo_checks"]
    cross_control = report["catalog_pair_checks"]
    exif = report["metadata_checks"]
    db = report.get("quality_catalog", {})
    lines = [
        "# Controle qualite photos catalogue",
        "",
        "## Contexte",
        f"- annonce: `{report['listing_code']}`",
        f"- dossier annonce: `{report['listing_dir']}`",
        f"- preset: `{report['preset']}`",
        f"- sensibilite: `{report['sensitivity']}`",
        f"- images originales: {report['summary']['original_images']}",
        f"- ajustements planifies: {report['summary']['adjustments_planned']}",
        f"- ajustements executes: {report['summary']['adjustments_executed']}",
        f"- ajustements deja en catalogue ignores: {report['summary']['adjustments_skipped_known']}",
        f"- autres annonces comparees: {report['summary']['other_listings_compared']}",
        f"- Catalogue controle: `{db.get('catalog_file', '-')}`",
        "",
        "## Resume global",
        f"- cas de reference a revoir: {len(reference_control['reference_photo_cases_to_review'])}",
        f"- cas inter-annonces a revoir match: {len(cross_control['catalog_pair_match_cases'])}",
        f"- cas inter-annonces a revoir review: {len(cross_control['catalog_pair_review_cases'])}",
        f"- scenarios EXIF: {exif['counts']}",
        f"- statuts stockes ce run: {db.get('this_run_visual_counts', {})}",
        f"- statuts stockes total catalogue: {db.get('catalog_visual_counts', {})}",
        "",
        "## Controle visuel - ajustements de reference",
        "| Image source | Ajustement | Statut visuel | Statut combine | pHash | dHash | wHash | Raison |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in reference_control["comparisons"]:
        phash, dhash, whash = _md_row_distances(item)
        comparison = item["comparison"]
        lines.append(f"| `{item['source_image']}` | `{item['photo_adjustment']['adjustment_name']}` | {comparison['visual_check']['status']} | {comparison['overall_check']['status']} | {phash} | {dhash} | {whash} | {comparison['overall_check']['reason']} |")
    lines.extend(["", "## Controle visuel - cas inter-annonces a revoir", "| Image A | Image B | Relation | Statut visuel | Statut combine | pHash | dHash | wHash | Raison |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for item in cross_control["comparisons"]:
        phash, dhash, whash = _md_row_distances(item)
        comparison = item["comparison"]
        lines.append(f"| `{item['image_a']}` | `{item['image_b']}` | {item['relation']} | {comparison['visual_check']['status']} | {comparison['overall_check']['status']} | {phash} | {dhash} | {whash} | {comparison['overall_check']['reason']} |")
    lines.extend(["", "## Controle metadonnees simule", "| Scenario | Statut metadonnees | Statut controle | Matched | Mismatched | Missing | Impact combine |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for item in exif["scenarios"]:
        test = item["metadata_check"]
        lines.append(f"| `{item['scenario']}` | {test['metadata_status']} | {test['status']} | {', '.join(test['matched_fields']) or '-'} | {', '.join(test['mismatched_fields']) or '-'} | {', '.join(test['missing_fields']) or '-'} | {item['combined_impact']} |")
    lines.extend(["", "## Catalogue controle", f"- fichier: `{db.get('catalog_file', '-')}`", f"- lignes observees sur ce run: {db.get('records_observed_this_run', 0)}", f"- nouvelles lignes ajoutees: {db.get('new_records_added', 0)}", f"- total catalogue: {db.get('catalog_total', 0)}", f"- comptages run: `{db.get('this_run_visual_counts', {})}`", f"- comptages catalogue: `{db.get('catalog_visual_counts', {})}`", f"- ZIP debug: `{report.get('debug_zip', '-')}`", "", "## Limites", "- Seuils de depart a calibrer sur un dataset reel.", "- Aucun ML, CNN, ANN, FAISS ni signal comportemental n'est utilise."])
    return "\n".join(lines) + "\n"


def run_listing_photo_quality_control(listing_code: str, annonces_root: str | Path | None = None, output_root: str | Path | None = None, preset: str = "default", sensitivity: str = "standard", max_other_listings: int = 20, keep_clear_cases: bool = False, rerun_tested: bool = False) -> dict[str, Any]:
    listing_dir = resolve_listing_dir(listing_code, annonces_root=annonces_root)
    images = find_listing_images(listing_dir)
    if not images:
        raise FileNotFoundError(f"Aucune image trouvee dans le dossier annonce: {listing_dir}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_output_root = Path(output_root) if output_root is not None else default_output_root()
    output_dir = local_output_root / _safe_listing_code(listing_code) / f"{timestamp}_control"
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_control = run_reference_photo_checks(listing_code, annonces_root=annonces_root, output_dir=output_dir, output_root=local_output_root, preset=preset, sensitivity=sensitivity, skip_known=not rerun_tested)
    cross_control = run_catalog_pair_checks(listing_code, annonces_root=annonces_root, max_other_listings=max_other_listings, sensitivity=sensitivity)
    exif = run_metadata_checks(listing_code, sensitivity=sensitivity)
    if keep_clear_cases:
        clear_cases_dir = output_dir / "clear_cases"
        clear_cases_dir.mkdir(exist_ok=True)
        for item in reference_control["reference_photo_cases_to_review"]:
            src = Path(item["photo_adjustment"]["path"])
            if src.exists():
                target = clear_cases_dir / src.name
                target.write_bytes(src.read_bytes())
    report = {"listing_code": listing_code, "listing_dir": str(listing_dir), "preset": preset, "sensitivity": sensitivity, "local_paths": describe_local_paths(annonces_root, local_output_root), "sensitivity_note": "wide sert a explorer les limites et peut creer plus de cas inter-annonces a revoir." if sensitivity == "wide" else "standard correspond au comportement prudent par defaut.", "output_dir": str(output_dir), "reports": {"json": str(output_dir / "photo_quality_control_report.json"), "markdown": str(output_dir / "photo_quality_control_report.md")}, "summary": {"original_images": len(images), "adjustments_planned": reference_control["planned_total"], "adjustments_executed": reference_control["executed_total"], "adjustments_skipped_known": reference_control["skipped_known_total"], "photo_adjustments_total": reference_control["executed_total"], "other_listings_compared": len(cross_control["other_listing_dirs"]), "rerun_tested": rerun_tested}, "reference_photo_checks": reference_control, "catalog_pair_checks": cross_control, "metadata_checks": exif}
    status_summary = _store_results(report, local_output_root, output_dir)
    report["quality_catalog"] = status_summary
    report["photo_review_catalog"] = status_summary
    _write_json(output_dir / "photo_quality_control_report.json", report)
    (output_dir / "photo_quality_control_report.md").write_text(_markdown_report(report), encoding="utf-8")
    debug_zip = _write_debug_zip(report, output_dir, status_summary)
    report["debug_zip"] = str(debug_zip)
    _write_json(output_dir / "photo_quality_control_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controle qualite des photos catalogue.")
    parser.add_argument("--listing", required=True, help="Code annonce, par exemple bijoux/O18.")
    parser.add_argument("--annonces-root", default=str(default_annonces_root()), help="Racine externe des annonces catalogue.")
    parser.add_argument("--output-root", default=str(default_output_root()), help="Dossier local du repo pour rapports, catalogues JSON et bundles debug.")
    parser.add_argument("--preset", choices=("light", "default", "extended", "thorough", "decision_margin_search", "boundary_search"), default="default")
    parser.add_argument("--sensitivity", choices=("standard", "wide"), default="standard")
    parser.add_argument("--policy", choices=("default", "standard", "wide"), default=None, help="Alias lisible pour la sensibilite: default/standard => standard, wide => wide.")
    parser.add_argument("--decision-margin-seed", type=int, default=12345, help="Seed reproductible pour les combinaisons legeres.")
    parser.add_argument("--decision-margin-max-combinations", type=int, default=24, help="Nombre maximum de combinaisons legeres a tester.")
    parser.add_argument("--decision-margin-candidates", type=int, default=4, help="Nombre maximum d'images sources priorisees pour l'analyse des marges.")
    parser.add_argument("--decision-margin-iterations", type=int, default=6, help="Iterations de dichotomie par zone de transition.")
    parser.add_argument("--max-other-listings", type=int, default=20)
    parser.add_argument("--keep-clear-cases", action="store_true")
    parser.add_argument("--rerun-tested", action="store_true", help="Rejoue les ajustements deja presents dans la DB locale.")
    args = parser.parse_args(argv)
    sensitivity = "standard" if args.policy in {"default", "standard"} else args.policy or args.sensitivity

    if args.preset in {"decision_margin_search", "boundary_search"}:
        try:
            report = run_decision_margin_search(
                args.listing,
                annonces_root=args.annonces_root,
                output_root=args.output_root,
                sensitivity=sensitivity,
                seed=args.decision_margin_seed,
                max_combinations=args.decision_margin_max_combinations,
                max_candidates=args.decision_margin_candidates,
                max_iterations=args.decision_margin_iterations,
            )
        except Exception as exc:
            print(f"Erreur: {exc}")
            return 1

        catalog = report.get("decision_margin_catalog", {})
        print(f"Rapport JSON: {report['reports']['json']}")
        print(f"Rapport Markdown: {report['reports']['markdown']}")
        print(f"ZIP debug: {report.get('debug_zip', '-')}")
        print(f"Catalogue marges decision: {catalog.get('catalog_file', '-')}")
        print("")
        print("ANALYSE DES MARGES DE DECISION PHOTO:")
        print(f"familles analysees: {report['summary']['photo_adjustment_families']}")
        print(f"images sources utilisees: {report['summary']['source_images_used']}")
        print(f"tests progressifs: {report['summary']['progressive_tests_performed']}")
        print(f"points de transition observes: {report['summary']['transition_points_found']}")
        print(f"combinaisons legeres testees: {report['summary']['combined_photo_adjustments_tested']}")
        print(f"statuts combinaisons: {report['summary']['combined_photo_adjustment_counts']}")
        return 0

    try:
        report = run_listing_photo_quality_control(args.listing, annonces_root=args.annonces_root, output_root=args.output_root, preset=args.preset, sensitivity=sensitivity, max_other_listings=args.max_other_listings, keep_clear_cases=args.keep_clear_cases, rerun_tested=args.rerun_tested)
    except Exception as exc:
        print(f"Erreur: {exc}")
        return 1
    reference_counts = Counter(report["reference_photo_checks"]["counts"])
    cross_counts = Counter(report["catalog_pair_checks"]["counts"])
    exif_counts = Counter(report["metadata_checks"]["counts"])
    catalog = report.get("quality_catalog", {})
    print(f"Rapport JSON: {report['reports']['json']}")
    print(f"Rapport Markdown: {report['reports']['markdown']}")
    print(f"ZIP debug: {report.get('debug_zip', '-')}")
    print(f"Catalogue controle: {catalog.get('catalog_file', '-')}")
    print("")
    print("CONTROLE VISUEL - AJUSTEMENTS DE REFERENCE:")
    print(f"match: {reference_counts.get('match', 0)}")
    print(f"review: {reference_counts.get('review', 0)}")
    print(f"clear: {reference_counts.get('clear', 0)}")
    print(f"planned: {report['summary']['adjustments_planned']}")
    print(f"executed: {report['summary']['adjustments_executed']}")
    print(f"deja presents dans le catalogue: {report['summary']['adjustments_skipped_known']}")
    print("")
    print("CONTROLE VISUEL - REFERENCES CROISEES:")
    print(f"match: {cross_counts.get('match', 0)}")
    print(f"review: {cross_counts.get('review', 0)}")
    print(f"clear: {cross_counts.get('clear', 0)}")
    print("")
    print("CONTROLE EXIF:")
    for status in ("unavailable", "neutral", "supportive", "strong_supportive", "conflict"):
        print(f"{status}: {exif_counts.get(status, 0)}")
    print("")
    print("Catalogue controle:")
    print(f"- lignes observees sur ce run: {catalog.get('records_observed_this_run', 0)}")
    print(f"- nouvelles lignes ajoutees: {catalog.get('new_records_added', 0)}")
    print(f"- total catalogue: {catalog.get('catalog_total', 0)}")
    print(f"- comptages du run: {catalog.get('this_run_visual_counts', {})}")
    print(f"- comptages catalogue: {catalog.get('catalog_visual_counts', {})}")
    print("")
    print("Points de controle:")
    print(f"- ajustements de reference a revoir: {len(report['reference_photo_checks']['reference_photo_cases_to_review'])}")
    print(f"- correspondances inter-annonces: {len(report['catalog_pair_checks']['catalog_pair_match_cases'])}")
    print(f"- revues inter-annonces: {len(report['catalog_pair_checks']['catalog_pair_review_cases'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
