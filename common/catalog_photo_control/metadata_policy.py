from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .image_metadata_inspector import read_metadata


METADATA_POLICY: dict[str, Any] = {
    "scope": "metadata_only",
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
    "preserve_existing": [
        "ICC_Profile",
        "ColorSpace",
        "Orientation",
        "all non-targeted metadata",
    ],
    "never_create_or_change": [
        "Make",
        "Model",
        "LensMake",
        "LensModel",
        "DateTimeOriginal",
        "Software",
    ],
    "integrity_guards": [
        "decoded pixel SHA-256 must remain identical",
        "pHash, dHash and wHash must remain identical",
        "JPEG entropy-coded scan data must remain identical",
        "JPEG coding segments DQT, DHT, SOF, SOS and DRI must remain identical",
        "Orientation must remain identical",
    ],
    "filesystem_preservation": {
        "preserve_file_modify_date": True,
        "preserve_file_attributes": True,
    },
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


PROTECTED_TAG_NAMES = {
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


JPEG_CODING_MARKERS = {
    "DQT",
    "DHT",
    "SOS",
    "DRI",
    "SOF0_BASELINE_DCT",
    "SOF1_EXTENDED_SEQUENTIAL_DCT",
    "SOF2_PROGRESSIVE_DCT",
    "SOF3_LOSSLESS_SEQUENTIAL",
    "SOF5_DIFFERENTIAL_SEQUENTIAL_DCT",
    "SOF6_DIFFERENTIAL_PROGRESSIVE_DCT",
    "SOF7_DIFFERENTIAL_LOSSLESS",
    "SOF9_EXTENDED_SEQUENTIAL_ARITHMETIC",
    "SOF10_PROGRESSIVE_ARITHMETIC",
    "SOF11_LOSSLESS_ARITHMETIC",
    "SOF13_DIFFERENTIAL_SEQUENTIAL_ARITHMETIC",
    "SOF14_DIFFERENTIAL_PROGRESSIVE_ARITHMETIC",
    "SOF15_DIFFERENTIAL_LOSSLESS_ARITHMETIC",
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


def _collect_named_values(
    metadata: dict[str, Any],
    names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for path, value in _walk_values(metadata):
        leaf = _leaf_name(path)
        if leaf in names and _non_empty(value):
            found[leaf].append({"path": path, "value": value})
    return {name: rows for name, rows in found.items() if rows}


def _serialized_value_counter(
    metadata: dict[str, Any],
    name: str,
) -> Counter[str]:
    normalized_name = name.lower()
    rows = _collect_named_values(metadata, {normalized_name}).get(
        normalized_name,
        [],
    )
    return Counter(
        json.dumps(
            row["value"],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for row in rows
    )


def _icc_sha256(metadata: dict[str, Any]) -> str | None:
    block = metadata.get("container", {}).get("icc_profile_block")
    if isinstance(block, dict):
        sha256 = block.get("sha256")
        if isinstance(sha256, str) and sha256:
            return sha256
    return None


def _binary_payload_signature(marker: dict[str, Any]) -> dict[str, Any]:
    payload = marker.get("payload")
    if isinstance(payload, dict):
        return {
            "name": marker.get("name"),
            "payload_length": payload.get("length"),
            "payload_sha256": payload.get("sha256"),
        }
    return {
        "name": marker.get("name"),
        "payload_length": marker.get("payload_length"),
        "payload_sha256": None,
    }


def _jpeg_image_data_signature(metadata: dict[str, Any]) -> dict[str, Any]:
    inventory = metadata.get("jpeg", {}).get(
        "full_marker_inventory",
        {},
    )
    markers = inventory.get("markers", []) if isinstance(inventory, dict) else []

    entropy_scans: list[dict[str, Any]] = []
    coding_segments: list[dict[str, Any]] = []

    if isinstance(markers, list):
        for marker in markers:
            if not isinstance(marker, dict):
                continue

            entropy = marker.get("entropy_coded_scan")
            if isinstance(entropy, dict):
                entropy_scans.append(
                    {
                        "length": entropy.get("length"),
                        "sha256": entropy.get("sha256"),
                        "restart_markers": entropy.get(
                            "restart_markers",
                            [],
                        ),
                    }
                )

            name = str(marker.get("name") or "")
            if name in JPEG_CODING_MARKERS:
                coding_segments.append(
                    _binary_payload_signature(marker)
                )

    return {
        "is_jpeg": (
            bool(inventory.get("is_jpeg"))
            if isinstance(inventory, dict)
            else False
        ),
        "progressive": (
            inventory.get("progressive")
            if isinstance(inventory, dict)
            else None
        ),
        "scan_count": (
            inventory.get("scan_count")
            if isinstance(inventory, dict)
            else None
        ),
        "entropy_scans": entropy_scans,
        "coding_segments": coding_segments,
    }


def verify_image_data_integrity(
    before_metadata: dict[str, Any],
    after_metadata: dict[str, Any],
) -> dict[str, Any]:
    before_pixels = before_metadata.get("pixel_fingerprints", {})
    after_pixels = after_metadata.get("pixel_fingerprints", {})
    before_image = before_metadata.get("image", {})
    after_image = after_metadata.get("image", {})

    before_perceptual = before_pixels.get("perceptual_hashes", {})
    after_perceptual = after_pixels.get("perceptual_hashes", {})
    before_jpeg = _jpeg_image_data_signature(before_metadata)
    after_jpeg = _jpeg_image_data_signature(after_metadata)

    checks = {
        "format_unchanged": (
            before_image.get("format") == after_image.get("format")
        ),
        "dimensions_unchanged": (
            before_image.get("width"),
            before_image.get("height"),
        )
        == (
            after_image.get("width"),
            after_image.get("height"),
        ),
        "decoded_pixel_sha256_unchanged": (
            before_pixels.get("sha256_decoded_pixels")
            == after_pixels.get("sha256_decoded_pixels")
        ),
        "decoded_pixel_byte_count_unchanged": (
            before_pixels.get("decoded_pixel_byte_count")
            == after_pixels.get("decoded_pixel_byte_count")
        ),
        "phash_unchanged": (
            before_perceptual.get("phash")
            == after_perceptual.get("phash")
        ),
        "dhash_unchanged": (
            before_perceptual.get("dhash")
            == after_perceptual.get("dhash")
        ),
        "whash_unchanged": (
            before_perceptual.get("whash")
            == after_perceptual.get("whash")
        ),
        "jpeg_entropy_scans_unchanged": (
            before_jpeg["entropy_scans"]
            == after_jpeg["entropy_scans"]
        ),
        "jpeg_coding_segments_unchanged": (
            before_jpeg["coding_segments"]
            == after_jpeg["coding_segments"]
        ),
        "jpeg_progressive_mode_unchanged": (
            before_jpeg["progressive"] == after_jpeg["progressive"]
        ),
        "jpeg_scan_count_unchanged": (
            before_jpeg["scan_count"] == after_jpeg["scan_count"]
        ),
    }

    return {
        "all_preserved": all(checks.values()),
        "checks": checks,
        "before": {
            "file_sha256": before_metadata.get("file_system", {}).get(
                "sha256_complete_file"
            ),
            "decoded_pixel_sha256": before_pixels.get(
                "sha256_decoded_pixels"
            ),
            "perceptual_hashes": before_perceptual,
            "jpeg_image_data_signature": before_jpeg,
        },
        "after": {
            "file_sha256": after_metadata.get("file_system", {}).get(
                "sha256_complete_file"
            ),
            "decoded_pixel_sha256": after_pixels.get(
                "sha256_decoded_pixels"
            ),
            "perceptual_hashes": after_perceptual,
            "jpeg_image_data_signature": after_jpeg,
        },
        "file_sha256_changed_due_to_metadata_rewrite": (
            before_metadata.get("file_system", {}).get(
                "sha256_complete_file"
            )
            != after_metadata.get("file_system", {}).get(
                "sha256_complete_file"
            )
        ),
    }


def verify_metadata_policy(
    filtered_before_metadata: dict[str, Any],
    filtered_after_metadata: dict[str, Any],
) -> dict[str, Any]:
    removed_hits: list[dict[str, Any]] = []
    for path, value in _walk_values(filtered_after_metadata):
        leaf = _leaf_name(path)
        path_lower = path.lower()
        if not _non_empty(value):
            continue
        if leaf in REMOVED_TAG_NAMES or "gps" in path_lower:
            removed_hits.append({"path": path, "value": value})

    protected_changes: list[dict[str, Any]] = []
    for tag_name in sorted(PROTECTED_TAG_NAMES):
        before_values = _serialized_value_counter(
            filtered_before_metadata,
            tag_name,
        )
        after_values = _serialized_value_counter(
            filtered_after_metadata,
            tag_name,
        )
        if before_values != after_values:
            protected_changes.append(
                {
                    "tag": tag_name,
                    "before_values": dict(before_values),
                    "after_values": dict(after_values),
                }
            )

    icc_before_sha = _icc_sha256(filtered_before_metadata)
    icc_after_sha = _icc_sha256(filtered_after_metadata)
    icc_unchanged = icc_before_sha == icc_after_sha

    colorspace_before = _serialized_value_counter(
        filtered_before_metadata,
        "colorspace",
    )
    colorspace_after = _serialized_value_counter(
        filtered_after_metadata,
        "colorspace",
    )
    colorspace_unchanged = colorspace_before == colorspace_after

    orientation_before = _serialized_value_counter(
        filtered_before_metadata,
        "orientation",
    )
    orientation_after = _serialized_value_counter(
        filtered_after_metadata,
        "orientation",
    )
    orientation_unchanged = orientation_before == orientation_after

    image_data_integrity = verify_image_data_integrity(
        filtered_before_metadata,
        filtered_after_metadata,
    )

    violations: list[str] = []
    if removed_hits:
        violations.append("removed_tags_still_present")
    if protected_changes:
        violations.append("protected_metadata_created_or_changed")
    if not icc_unchanged:
        violations.append("icc_profile_changed")
    if not colorspace_unchanged:
        violations.append("colorspace_changed")
    if not orientation_unchanged:
        violations.append("orientation_changed")
    if not image_data_integrity["all_preserved"]:
        violations.append("image_data_changed")

    return {
        "compliant": not violations,
        "violations": violations,
        "removed_tag_hits": removed_hits,
        "protected_metadata_changes": protected_changes,
        "icc_profile": {
            "before_sha256": icc_before_sha,
            "after_sha256": icc_after_sha,
            "unchanged": icc_unchanged,
        },
        "colorspace": {
            "before_values": dict(colorspace_before),
            "after_values": dict(colorspace_after),
            "unchanged": colorspace_unchanged,
        },
        "orientation": {
            "policy": "leave_unchanged",
            "before_values": dict(orientation_before),
            "after_values": dict(orientation_after),
            "unchanged": orientation_unchanged,
        },
        "image_data_integrity": image_data_integrity,
    }


def _run_exiftool_metadata_only(
    filtered_path: Path,
    *,
    exiftool_path: str | None,
    require_exiftool: bool,
    before_metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    executable = _find_exiftool(exiftool_path)
    if executable is None:
        if require_exiftool:
            raise RuntimeError(
                "ExifTool introuvable. Installe-le ou passe "
                "--exiftool-path."
            )
        return (
            {
                "applied": False,
                "reason": "exiftool_not_found",
                "filtered_path": str(filtered_path),
            },
            before_metadata,
        )

    backup_path = filtered_path.with_name(
        f".{filtered_path.name}.metadata_policy_backup"
    )
    if backup_path.exists():
        raise RuntimeError(
            f"Sauvegarde temporaire déjà présente : {backup_path}"
        )

    shutil.copy2(filtered_path, backup_path)

    command = [
        executable,
        "-overwrite_original_in_place",
        "-P",
        *REMOVE_ARGS,
        str(filtered_path),
    ]

    try:
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
                f"(code {process.returncode}): "
                f"{process.stderr.strip() or process.stdout.strip()}"
            )

        after_metadata = read_metadata(
            filtered_path,
            use_exiftool=True,
            exiftool_path=exiftool_path,
        )
        integrity = verify_image_data_integrity(
            before_metadata,
            after_metadata,
        )
        if not integrity["all_preserved"]:
            raise RuntimeError(
                "La vérification d'intégrité indique une "
                "modification des pixels ou des données JPEG."
            )
    except Exception:
        shutil.copy2(backup_path, filtered_path)
        raise
    finally:
        if backup_path.exists():
            backup_path.unlink()

    return (
        {
            "applied": True,
            "mode": "metadata_only",
            "filtered_path": str(filtered_path),
            "executable": executable,
            "command": command,
            "return_code": process.returncode,
            "stdout": process.stdout.strip(),
            "stderr": process.stderr.strip(),
            "temporary_backup_removed": True,
            "image_data_integrity": integrity,
        },
        after_metadata,
    )


def apply_metadata_policy(
    image_path: Path,
    *,
    exiftool_path: str | None = None,
    require_exiftool: bool = True,
) -> dict[str, Any]:
    image_path = Path(image_path)
    if not image_path.is_file():
        raise FileNotFoundError(image_path)

    before_metadata = read_metadata(
        image_path,
        use_exiftool=True,
        exiftool_path=exiftool_path,
    )
    application, after_metadata = _run_exiftool_metadata_only(
        image_path,
        exiftool_path=exiftool_path,
        require_exiftool=require_exiftool,
        before_metadata=before_metadata,
    )
    application["verification"] = verify_metadata_policy(
        before_metadata,
        after_metadata,
    )
    return application


def generate_metadata_report(
    original_path: Path,
    filtered_path: Path,
    *,
    filtered_before_metadata: dict[str, Any] | None = None,
    filtered_after_metadata: dict[str, Any] | None = None,
    output_path: Path | None = None,
    exiftool_path: str | None = None,
) -> dict[str, Any]:
    original_path = Path(original_path)
    filtered_path = Path(filtered_path)

    original_metadata = read_metadata(
        original_path,
        use_exiftool=True,
        exiftool_path=exiftool_path,
    )
    before_metadata = (
        filtered_before_metadata
        if filtered_before_metadata is not None
        else read_metadata(
            filtered_path,
            use_exiftool=True,
            exiftool_path=exiftool_path,
        )
    )
    after_metadata = (
        filtered_after_metadata
        if filtered_after_metadata is not None
        else read_metadata(
            filtered_path,
            use_exiftool=True,
            exiftool_path=exiftool_path,
        )
    )

    report = {
        "policy": METADATA_POLICY,
        "original_path": str(original_path),
        "filtered_path": str(filtered_path),
        "verification": verify_metadata_policy(
            before_metadata,
            after_metadata,
        ),
        "original_metadata_full": original_metadata,
        "filtered_before_metadata_full": before_metadata,
        "filtered_after_metadata_full": after_metadata,
    }
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    return report


def _load_export_pairs(
    export_dir: Path,
) -> list[tuple[Path, Path]]:
    selected_path = export_dir / "selected_filter.json"
    if not selected_path.is_file():
        raise FileNotFoundError(selected_path)

    selected = json.loads(
        selected_path.read_text(encoding="utf-8")
    )
    sources = selected.get("source_images") or []
    outputs = selected.get("output_paths") or []

    pairs: list[tuple[Path, Path]] = []
    for source, output in zip(sources, outputs):
        if not isinstance(source, dict):
            continue
        source_path = Path(
            str(source.get("source_path") or "")
        )
        output_path = Path(str(output))
        pairs.append((source_path, output_path))

    if not pairs:
        raise RuntimeError(
            "Aucune paire originale/filtrée trouvée "
            "dans selected_filter.json"
        )
    return pairs


def process_export_directory(
    export_dir: Path,
    *,
    exiftool_path: str | None = None,
    apply_policy: bool = True,
    require_exiftool: bool = True,
) -> dict[str, Any]:
    export_dir = Path(export_dir)
    pairs = _load_export_pairs(export_dir)

    pair_reports: list[dict[str, Any]] = []
    for index, (original_path, filtered_path) in enumerate(
        pairs,
        start=1,
    ):
        if not original_path.is_file():
            raise FileNotFoundError(original_path)
        if not filtered_path.is_file():
            raise FileNotFoundError(filtered_path)

        original_metadata = read_metadata(
            original_path,
            use_exiftool=True,
            exiftool_path=exiftool_path,
        )
        before_metadata = read_metadata(
            filtered_path,
            use_exiftool=True,
            exiftool_path=exiftool_path,
        )

        if apply_policy:
            application, after_metadata = (
                _run_exiftool_metadata_only(
                    filtered_path,
                    exiftool_path=exiftool_path,
                    require_exiftool=require_exiftool,
                    before_metadata=before_metadata,
                )
            )
        else:
            application = {
                "applied": False,
                "reason": "audit_only",
                "mode": "metadata_only",
            }
            after_metadata = before_metadata

        verification = verify_metadata_policy(
            before_metadata,
            after_metadata,
        )
        audit = {
            "policy": METADATA_POLICY,
            "original_path": str(original_path),
            "filtered_path": str(filtered_path),
            "verification": verification,
            "original_metadata_full": original_metadata,
            "filtered_before_metadata_full": before_metadata,
            "filtered_after_metadata_full": after_metadata,
        }
        pair_reports.append(
            {
                "pair_index": index,
                "application": application,
                "audit": audit,
            }
        )

    output_path = export_dir / "metadata_policy_audit.json"
    all_compliant = all(
        item["audit"]["verification"]["compliant"]
        for item in pair_reports
    )
    all_image_data_preserved = all(
        item["audit"]["verification"]
        ["image_data_integrity"]["all_preserved"]
        for item in pair_reports
    )
    all_orientations_unchanged = all(
        item["audit"]["verification"]
        ["orientation"]["unchanged"]
        for item in pair_reports
    )

    payload = {
        "policy": METADATA_POLICY,
        "export_dir": str(export_dir),
        "pair_count": len(pair_reports),
        "all_compliant": all_compliant,
        "all_image_data_preserved": all_image_data_preserved,
        "all_orientations_unchanged": all_orientations_unchanged,
        "pairs": pair_reports,
    }
    output_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return {
        **payload,
        "output_path": str(output_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Supprime uniquement les métadonnées sensibles ciblées, "
            "sans modifier les pixels, l'encodage JPEG, le profil "
            "colorimétrique ni l'orientation, puis génère un audit."
        )
    )
    parser.add_argument(
        "--export-dir",
        required=True,
        help="Dossier contenant selected_filter.json",
    )
    parser.add_argument(
        "--exiftool-path",
        default=None,
        help="Chemin explicite vers exiftool.exe",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help=(
            "N'applique aucune modification et génère seulement "
            "l'audit de l'état actuel."
        ),
    )
    parser.add_argument(
        "--allow-missing-exiftool",
        action="store_true",
        help="N'échoue pas si ExifTool est absent",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = process_export_directory(
        Path(args.export_dir),
        exiftool_path=args.exiftool_path,
        apply_policy=not args.audit_only,
        require_exiftool=not args.allow_missing_exiftool,
    )

    print("METADATA-ONLY PRIVACY AND INTEGRITY AUDIT")
    print(f"pairs: {result['pair_count']}")
    print(f"all_compliant: {result['all_compliant']}")
    print(
        "all_image_data_preserved: "
        f"{result['all_image_data_preserved']}"
    )
    print(
        "all_orientations_unchanged: "
        f"{result['all_orientations_unchanged']}"
    )
    print(f"json: {result['output_path']}")
    return 0 if result["all_compliant"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
