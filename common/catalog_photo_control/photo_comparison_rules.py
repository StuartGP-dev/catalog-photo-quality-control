from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import imagehash
from PIL import Image, ImageOps

from .photo_metadata import compare_photo_metadata_info

HASH_SIZE = 8
HASH_BITS = HASH_SIZE * HASH_SIZE


HASH_NOTES = {
    "phash": (
        "Paliers derives du controle McKeown & Buchanan, "
        "\"Hamming Distributions of Popular Perceptual Hashing Techniques\", "
        "sur ImageHash/pHash 64 bits."
    ),
    "whash": (
        "Seuil de depart derive des resultats Wavehash/ImageHash dans "
        "McKeown & Buchanan; wHash ne valide jamais seul."
    ),
    "dhash": (
        "Seuil de depart conservateur adapte d'un retour open-source de Ben Hoyt "
        "sur dHash 128 bits; a calibrer sur les images reelles du projet. "
        "dHash ne valide jamais seul."
    ),
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_band(name: str, distance: int) -> str:
    if name == "phash":
        # Seuils publics de depart, a calibrer sur les images reelles du projet.
        if distance == 0:
            return "exact"
        if distance <= 5:
            return "strong"
        if distance <= 10:
            return "review"
        if distance <= 16:
            return "weak"
        return "far"

    if name == "whash":
        if distance == 0:
            return "exact"
        if distance <= 6:
            return "strong"
        if distance <= 16:
            return "review"
        return "far"

    if name == "dhash":
        if distance == 0:
            return "exact"
        if distance == 1:
            return "strong"
        if distance == 2:
            return "review"
        return "far"

    raise ValueError(f"Hash inconnu: {name}")


def _compute_hashes(path: str | Path) -> dict[str, imagehash.ImageHash]:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        return {
            "phash": imagehash.phash(image, hash_size=HASH_SIZE),
            "dhash": imagehash.dhash(image, hash_size=HASH_SIZE),
            "whash": imagehash.whash(image, hash_size=HASH_SIZE),
        }


def compare_photo_visual_markers(ref_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    ref_hashes = _compute_hashes(ref_path)
    candidate_hashes = _compute_hashes(candidate_path)
    result: dict[str, Any] = {}

    for name in ("phash", "dhash", "whash"):
        distance = int(ref_hashes[name] - candidate_hashes[name])
        result[name] = {
            "reference_hash": str(ref_hashes[name]),
            "candidate_hash": str(candidate_hashes[name]),
            "hamming_distance": distance,
            "bits": HASH_BITS,
            "normalized_distance": round(distance / HASH_BITS, 6),
            "band": _hash_band(name, distance),
            "threshold_note": HASH_NOTES[name],
        }

    return result


def compare_photo_metadata(ref_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    return compare_photo_metadata_info(ref_path, candidate_path)


def _is_exact_or_strong(hash_report: dict[str, Any]) -> bool:
    return hash_report["band"] in {"exact", "strong"}


def _is_review_or_better(hash_report: dict[str, Any]) -> bool:
    return hash_report["band"] in {"exact", "strong", "review"}


def evaluate_visual_check(
    sha256_equal: bool,
    visual: dict[str, Any],
    sensitivity: str = "standard",
) -> dict[str, Any]:
    if sensitivity not in {"standard", "wide"}:
        raise ValueError("sensitivity doit etre standard ou wide")

    if sha256_equal:
        return {"status": "match", "reason": "Fichiers strictement identiques: SHA-256 egal."}

    if visual["phash"]["hamming_distance"] == 0:
        return {"status": "match", "reason": "pHash exact."}

    exact_or_strong_count = sum(
        1 for name in ("phash", "dhash", "whash") if _is_exact_or_strong(visual[name])
    )
    review_or_better_count = sum(
        1 for name in ("phash", "dhash", "whash") if _is_review_or_better(visual[name])
    )
    phash_strong = visual["phash"]["band"] == "strong"
    other_strong = _is_exact_or_strong(visual["dhash"]) or _is_exact_or_strong(visual["whash"])

    if sensitivity == "wide":
        if phash_strong and other_strong:
            return {
                "status": "match",
                "reason": "Sensibilite large: pHash fort et au moins un autre hash fort.",
            }
        if exact_or_strong_count >= 2:
            return {"status": "review", "reason": "Sensibilite large: au moins deux hashes forts."}
        if review_or_better_count >= 2:
            return {"status": "review", "reason": "Sensibilite large: au moins deux hashes review ou mieux."}
        if visual["phash"]["band"] == "weak" and (
            visual["whash"]["band"] == "review" or visual["dhash"]["band"] == "review"
        ):
            return {"status": "review", "reason": "Sensibilite large: pHash weak avec autre hash en review."}
        return {"status": "clear", "reason": "Sensibilite large: aucun signal visuel suffisant."}

    if exact_or_strong_count == 3:
        return {"status": "match", "reason": "Les trois hashes visuels sont exacts ou forts."}
    if exact_or_strong_count >= 2:
        return {"status": "review", "reason": "Au moins deux hashes visuels sont exacts ou forts."}
    if review_or_better_count >= 2:
        return {"status": "review", "reason": "Au moins deux hashes visuels atteignent la bande review ou mieux."}
    return {"status": "clear", "reason": "Aucun consensus visuel suffisant."}


def evaluate_metadata_check(exif: dict[str, Any], sensitivity: str = "standard") -> dict[str, Any]:
    if sensitivity not in {"standard", "wide"}:
        raise ValueError("sensitivity doit etre standard ou wide")

    metadata_status = exif["exif_status"]
    available_fields = len(exif["matched_fields"]) + len(exif["mismatched_fields"])

    if metadata_status == "conflict":
        return {"status": "review", "reason": "EXIF contradictoire.", "metadata_status": metadata_status}
    if metadata_status == "strong_supportive" and sensitivity == "wide":
        return {
            "status": "review",
            "reason": "Sensibilite large: EXIF fortement corroborant, sans validation automatique.",
            "metadata_status": metadata_status,
        }
    if metadata_status == "supportive" and sensitivity == "wide" and available_fields >= 2:
        return {
            "status": "review",
            "reason": "Sensibilite large: EXIF corroborant avec plusieurs champs disponibles.",
            "metadata_status": metadata_status,
        }
    if metadata_status == "unavailable":
        return {"status": "clear", "reason": "Aucune metadonnee EXIF exploitable.", "metadata_status": metadata_status}
    return {"status": "clear", "reason": "EXIF neutre ou corroborant sans effet de validation directe.", "metadata_status": metadata_status}


def _decide_photo_check_outcome(
    sha256_equal: bool,
    visual: dict[str, Any],
    exif: dict[str, Any],
    sensitivity: str = "standard",
) -> tuple[str, str]:
    exif_conflict = exif["exif_status"] == "conflict"
    exif_supports = exif["exif_status"] in {"supportive", "strong_supportive"}

    if sha256_equal:
        return "match", "Fichiers strictement identiques: SHA-256 egal."

    if visual["phash"]["hamming_distance"] == 0:
        if exif_conflict:
            return "review", "pHash exact, mais EXIF contradictoire: verification manuelle."
        return "match", "pHash exact sans conflit EXIF clair."

    exact_or_strong_count = sum(
        1 for name in ("phash", "dhash", "whash") if _is_exact_or_strong(visual[name])
    )
    review_or_better_count = sum(
        1 for name in ("phash", "dhash", "whash") if _is_review_or_better(visual[name])
    )

    if exact_or_strong_count == 3 and not exif_conflict:
        return "match", "Les trois hashes visuels sont exacts ou forts, sans conflit EXIF."

    phash_strong = visual["phash"]["band"] == "strong"
    other_strong = _is_exact_or_strong(visual["dhash"]) or _is_exact_or_strong(visual["whash"])

    if sensitivity == "wide":
        if phash_strong and other_strong and not exif_conflict:
            return "match", "Sensibilite large: pHash fort et autre hash fort, sans conflit EXIF."
        if exact_or_strong_count >= 2:
            return "review", "Sensibilite large: au moins deux hashes forts."
        if review_or_better_count >= 2:
            return "review", "Sensibilite large: au moins deux hashes review ou mieux."
        if visual["phash"]["band"] == "weak" and (
            visual["whash"]["band"] == "review" or visual["dhash"]["band"] == "review"
        ):
            return "review", "Sensibilite large: pHash weak avec autre hash en review."
        exif_status = evaluate_metadata_check(exif, sensitivity=sensitivity)
        if exif_status["status"] == "review":
            return "review", f"Sensibilite large: {exif_status['reason']}"
        return "clear", "Sensibilite large: aucun signal combine suffisant."

    if phash_strong and other_strong and exif_supports:
        return "match", "pHash fort, autre hash fort, et EXIF corroborant."

    if exact_or_strong_count >= 2:
        return "review", "Au moins deux hashes visuels sont exacts ou forts."

    if review_or_better_count >= 2:
        return "review", "Au moins deux hashes visuels atteignent la bande review ou mieux."

    if visual["phash"]["band"] in {"review", "weak"} and exif_supports:
        return "review", "pHash en bande review/weak avec EXIF corroborant."

    return "clear", "Aucun consensus visuel ou EXIF suffisant pour signaler une coherence visuelle."


def compare_photo_pair(
    ref_path: str | Path,
    candidate_path: str | Path,
    sensitivity: str = "standard",
) -> dict[str, Any]:
    ref_path = Path(ref_path)
    candidate_path = Path(candidate_path)
    ref_sha = sha256_file(ref_path)
    candidate_sha = sha256_file(candidate_path)
    sha_equal = ref_sha == candidate_sha
    visual = compare_photo_visual_markers(ref_path, candidate_path)
    exif = compare_photo_metadata(ref_path, candidate_path)
    visual_status = evaluate_visual_check(sha_equal, visual, sensitivity=sensitivity)
    exif_status = evaluate_metadata_check(exif, sensitivity=sensitivity)
    status, reason = _decide_photo_check_outcome(sha_equal, visual, exif, sensitivity=sensitivity)

    return {
        "status": status,
        "reason": reason,
        "sensitivity": sensitivity,
        "reference_path": str(ref_path),
        "candidate_path": str(candidate_path),
        "sha256_reference": ref_sha,
        "sha256_candidate": candidate_sha,
        "sha256_equal": sha_equal,
        "visual": visual,
        "exif": exif,
        "visual_check": {
            "status": visual_status["status"],
            "reason": visual_status["reason"],
            "hashes": visual,
            "sha256_reference": ref_sha,
            "sha256_candidate": candidate_sha,
            "sha256_equal": sha_equal,
        },
        "metadata_check": {
            "status": exif_status["status"],
            "reason": exif_status["reason"],
            "metadata_status": exif["exif_status"],
            "matched_fields": exif["matched_fields"],
            "mismatched_fields": exif["mismatched_fields"],
            "missing_fields": exif["missing_fields"],
        },
        "overall_check": {
            "status": status,
            "reason": reason,
        },
    }
