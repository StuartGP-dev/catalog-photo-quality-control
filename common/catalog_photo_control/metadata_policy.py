from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .image_metadata_inspector import read_metadata


METADATA_POLICY: dict[str, Any] = {
    "remove": [
        "GPS:*",
        "SerialNumber",
        "BodySerialNumber",
        "LensSerialNumber",
        "CameraOwnerName",
        "Artist",
        "Copyright",
        "UserComment",
    ],
    "preserve_from_original": [
        "ICC_Profile",
        "EXIF:ColorSpace",
    ],
    "normalize": {
        "Orientation": 1,
    },
    "never_fabricate": [
        "Make",
        "Model",
        "LensMake",
        "LensModel",
        "DateTimeOriginal",
        "Software",
    ],
}


REMOVE_ARGS = [
    "-GPS:all=",
    "-SerialNumber=",
    "-BodySerialNumber=",
    "-LensSerialNumber=",
    "-CameraOwnerName=",
    "-Artist=",
    "-Copyright=",
    "-UserComment=",
]


NEVER_FABRICATE_NAMES = {
    "make",
    "model",
    "lensmake",
    "lensmodel",
    "datetimeoriginal",
    "software",
}


REMOVED_TAG_NAMES = {
    "serialnumber",
    "bodyserialnumber",
    "lensserialnumber",
    "cameraownername",
    "artist",
    "copyright",
    "usercomment",
}


def _find_exiftool(explicit_path: str | None = None) -> str | None:
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.exists():
            return str(candidate)
        return shutil.which(explicit_path)

    for name in ("exiftool", "exiftool.exe"):
        found = shutil.which(name)
        if found:
            return found

    for candidate in (
        Path("C:/Windows/exiftool.exe"),
        Path("C:/Program Files/ExifTool/exiftool.exe"),
        Path("C:/Program Files (x86)/ExifTool/exiftool.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _walk_values(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_walk_values(item, next_prefix))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_walk_values(item, f"{prefix}[{index}]"))
    else:
        rows.append((prefix, value))
    return rows


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {}, False)


def _leaf_name(path: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    leaf = leaf.split(":")[-1]
    leaf = leaf.split("[")[0]
    return leaf.strip().lower()


def _collect_named_values(metadata: dict[str, Any], names: set[str]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for path, value in _walk_values(metadata):
        leaf = _leaf_name(path)
        if leaf in names and _non_empty(value):
            found[leaf].append({"path": path, "value": value})
    return {name: rows for name, rows in found.items() if rows}


def verify_metadata_policy(
    original_metadata: dict[str, Any],
    filtered_metadata: dict[str, Any],
) -> dict[str, Any]:
    removed_hits: list[dict[str, Any]] = []
    for path, value in _walk_values(filtered_metadata):
        leaf = _leaf_name(path)
        path_lower = path.lower()
        if not _non_empty(value):
            continue
        if leaf in REMOVED_TAG_NAMES or "gps" in path_lower:
            removed_hits.append({"path": path, "value": value})

    original_named = _collect_named_values(original_metadata, NEVER_FABRICATE_NAMES)
    filtered_named = _collect_named_values(filtered_metadata, NEVER_FABRICATE_NAMES)
    fabrication_findings: list[dict[str, Any]] = []
    for tag_name, filtered_rows in filtered_named.items():
        original_values = {json.dumps(row["value"], ensure_ascii=False, sort_keys=True, default=str) for row in original_named.get(tag_name, [])}
        for row in filtered_rows:
            serialized = json.dumps(row["value"], ensure_ascii=False, sort_keys=True, default=str)
            if serialized not in original_values:
                fabrication_findings.append(
                    {
                        "tag": tag_name,
                        "filtered_path": row["path"],
                        "filtered_value": row["value"],
                        "original_values": [entry["value"] for entry in original_named.get(tag_name, [])],
                    }
                )

    original_icc = bool(original_metadata.get("icc", {}).get("present"))
    filtered_icc = bool(filtered_metadata.get("icc", {}).get("present"))
    filtered_orientation = _collect_named_values(filtered_metadata, {"orientation"})
    orientation_values = [entry["value"] for entry in filtered_orientation.get("orientation", [])]
    orientation_ok = not orientation_values or all(
        value == 1
        or value == "1"
        or (isinstance(value, dict) and value.get("numerator") == 1)
        for value in orientation_values
    )

    violations = []
    if removed_hits:
        violations.append("removed_tags_still_present")
    if fabrication_findings:
        violations.append("never_fabricate_tags_changed_or_created")
    if original_icc and not filtered_icc:
        violations.append("icc_profile_not_preserved")
    if not orientation_ok:
        violations.append("orientation_not_normalized")

    return {
        "compliant": not violations,
        "violations": violations,
        "removed_tag_hits": removed_hits,
        "fabrication_findings": fabrication_findings,
        "icc_profile": {
            "original_present": original_icc,
            "filtered_present": filtered_icc,
            "preserved_when_original_present": (not original_icc) or filtered_icc,
        },
        "orientation": {
            "values_found": orientation_values,
            "normalized_or_absent": orientation_ok,
        },
    }


def apply_metadata_policy(
    original_path: Path,
    filtered_path: Path,
    *,
    exiftool_path: str | None = None,
    require_exiftool: bool = True,
) -> dict[str, Any]:
    original_path = Path(original_path)
    filtered_path = Path(filtered_path)
    if not original_path.is_file():
        raise FileNotFoundError(original_path)
    if not filtered_path.is_file():
        raise FileNotFoundError(filtered_path)

    executable = _find_exiftool(exiftool_path)
    if executable is None:
        if require_exiftool:
            raise RuntimeError("ExifTool introuvable. Installe-le ou passe --exiftool-path.")
        return {
            "applied": False,
            "reason": "exiftool_not_found",
            "original_path": str(original_path),
            "filtered_path": str(filtered_path),
        }

    command = [
        executable,
        "-overwrite_original",
        "-TagsFromFile",
        str(original_path),
        "-ICC_Profile",
        "-EXIF:ColorSpace",
        *REMOVE_ARGS,
        "-Orientation=1",
        str(filtered_path),
    ]
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "ExifTool a échoué "
            f"(code {process.returncode}): {process.stderr.strip() or process.stdout.strip()}"
        )

    return {
        "applied": True,
        "original_path": str(original_path),
        "filtered_path": str(filtered_path),
        "executable": executable,
        "command": command,
        "return_code": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }


def generate_metadata_report(
    original_path: Path,
    filtered_path: Path,
    *,
    output_path: Path | None = None,
    exiftool_path: str | None = None,
) -> dict[str, Any]:
    original_path = Path(original_path)
    filtered_path = Path(filtered_path)
    original_metadata = read_metadata(original_path, use_exiftool=True, exiftool_path=exiftool_path)
    filtered_metadata = read_metadata(filtered_path, use_exiftool=True, exiftool_path=exiftool_path)
    report = {
        "policy": METADATA_POLICY,
        "original_path": str(original_path),
        "filtered_path": str(filtered_path),
        "verification": verify_metadata_policy(original_metadata, filtered_metadata),
        "original_metadata_full": original_metadata,
        "filtered_metadata_full": filtered_metadata,
    }
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return report


def process_export_directory(
    export_dir: Path,
    *,
    exiftool_path: str | None = None,
    apply_policy: bool = True,
    require_exiftool: bool = True,
) -> dict[str, Any]:
    export_dir = Path(export_dir)
    selected_path = export_dir / "selected_filter.json"
    if not selected_path.is_file():
        raise FileNotFoundError(selected_path)

    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    sources = selected.get("source_images") or []
    outputs = selected.get("output_paths") or []
    pairs: list[tuple[Path, Path]] = []
    for source, output in zip(sources, outputs):
        if not isinstance(source, dict):
            continue
        source_path = Path(str(source.get("source_path") or ""))
        output_path = Path(str(output))
        pairs.append((source_path, output_path))
    if not pairs:
        raise RuntimeError("Aucune paire originale/filtrée trouvée dans selected_filter.json")

    pair_reports = []
    for index, (original_path, filtered_path) in enumerate(pairs, start=1):
        application = (
            apply_metadata_policy(
                original_path,
                filtered_path,
                exiftool_path=exiftool_path,
                require_exiftool=require_exiftool,
            )
            if apply_policy
            else {"applied": False, "reason": "audit_only"}
        )
        audit = generate_metadata_report(
            original_path,
            filtered_path,
            exiftool_path=exiftool_path,
        )
        pair_reports.append(
            {
                "pair_index": index,
                "application": application,
                "audit": audit,
            }
        )

    output_path = export_dir / "metadata_policy_audit.json"
    payload = {
        "policy": METADATA_POLICY,
        "export_dir": str(export_dir),
        "pair_count": len(pair_reports),
        "all_compliant": all(item["audit"]["verification"]["compliant"] for item in pair_reports),
        "pairs": pair_reports,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {**payload, "output_path": str(output_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Applique une politique de confidentialité des métadonnées et génère un audit complet."
    )
    parser.add_argument("--export-dir", required=True, help="Dossier contenant selected_filter.json")
    parser.add_argument("--exiftool-path", default=None, help="Chemin explicite vers exiftool.exe")
    parser.add_argument("--audit-only", action="store_true", help="N'applique aucune modification, génère seulement l'audit")
    parser.add_argument("--allow-missing-exiftool", action="store_true", help="N'échoue pas si ExifTool est absent")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = process_export_directory(
        Path(args.export_dir),
        exiftool_path=args.exiftool_path,
        apply_policy=not args.audit_only,
        require_exiftool=not args.allow_missing_exiftool,
    )
    print("METADATA POLICY AUDIT")
    print(f"pairs: {result['pair_count']}")
    print(f"all_compliant: {result['all_compliant']}")
    print(f"json: {result['output_path']}")
    return 0 if result["all_compliant"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
