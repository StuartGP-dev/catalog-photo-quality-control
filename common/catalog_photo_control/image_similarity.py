from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import imagehash
from PIL import Image, ImageOps


SIMILARITY_ENGINE_VERSION = "perceptual-consensus-o18-v1"
HASH_SIZE = 8
HASH_BITS = 64
HASH_NAMES = ("phash", "dhash", "whash")
DEFAULT_BAND_LIMITS: dict[str, dict[str, int]] = {
    "phash": {"strong": 4, "review": 10, "weak": 16},
    "dhash": {"strong": 4, "review": 10, "weak": 16},
    "whash": {"strong": 4, "review": 10, "weak": 18},
}
DEFAULT_REJECT_VERDICTS = ("exact", "same", "near_duplicate")
DEFAULT_CONSENSUS = {"same_strong_count": 3, "near_strong_count": 2, "near_review_count": 2}


@dataclass(frozen=True, slots=True)
class ImageHashes:
    sha256: str
    phash: str
    dhash: str
    whash: str


@dataclass(frozen=True, slots=True)
class HashDistance:
    distance: int
    bits: int
    band: str


@dataclass(frozen=True, slots=True)
class SimilarityResult:
    sha256_equal: bool
    phash: HashDistance
    dhash: HashDistance
    whash: HashDistance
    verdict: str
    reason: str

    def distances(self) -> tuple[int, int, int]:
        return (self.phash.distance, self.dhash.distance, self.whash.distance)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_hashes(path: str | Path) -> ImageHashes:
    source = Path(path)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        return ImageHashes(
            sha256=sha256_file(source),
            phash=str(imagehash.phash(image, hash_size=HASH_SIZE)),
            dhash=str(imagehash.dhash(image, hash_size=HASH_SIZE)),
            whash=str(imagehash.whash(image, hash_size=HASH_SIZE)),
        )


def hash_band(name: str, distance: int, limits: Mapping[str, Mapping[str, int]] | None = None) -> str:
    if name not in HASH_NAMES:
        raise ValueError(f"unknown perceptual hash: {name}")
    if not 0 <= distance <= HASH_BITS:
        raise ValueError(f"{name} distance must be between 0 and {HASH_BITS}")
    selected = (limits or DEFAULT_BAND_LIMITS)[name]
    if distance == 0:
        return "exact"
    if distance <= int(selected["strong"]):
        return "strong"
    if distance <= int(selected["review"]):
        return "review"
    if distance <= int(selected["weak"]):
        return "weak"
    return "far"


def validate_similarity_config(config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    raw_limits = config.get("band_limits", DEFAULT_BAND_LIMITS)
    limits: dict[str, dict[str, int]] = {}
    for name in HASH_NAMES:
        values = raw_limits[name]
        row = {key: int(values[key]) for key in ("strong", "review", "weak")}
        if not 0 <= row["strong"] <= row["review"] <= row["weak"] <= HASH_BITS:
            raise ValueError(f"invalid ordered 64-bit band limits for {name}")
        limits[name] = row
    verdicts = tuple(str(value) for value in config.get("reject_verdicts", DEFAULT_REJECT_VERDICTS))
    if not set(verdicts) <= {"exact", "same", "near_duplicate"}:
        raise ValueError("reject_verdicts may contain only exact, same, near_duplicate")
    consensus = {key: int(value) for key, value in dict(config.get("consensus", DEFAULT_CONSENSUS)).items()}
    if set(consensus) != set(DEFAULT_CONSENSUS) or any(not 1 <= value <= 3 for value in consensus.values()):
        raise ValueError("consensus counts must define three values between 1 and 3")
    normalized.update({
        "engine_version": str(config.get("engine_version", SIMILARITY_ENGINE_VERSION)),
        "hash_size": int(config.get("hash_size", HASH_SIZE)),
        "band_limits": limits,
        "reject_verdicts": verdicts,
        "consensus": consensus,
        "compare_same_image_index_only": True,
        "nearest_neighbors_to_persist": max(1, int(config.get("nearest_neighbors_to_persist", 1))),
    })
    if normalized["hash_size"] != HASH_SIZE:
        raise ValueError("the similarity engine requires 64-bit hashes (hash_size=8)")
    return normalized


def compare_hashes(
    reference: ImageHashes,
    candidate: ImageHashes,
    limits: Mapping[str, Mapping[str, int]] | None = None,
    consensus: Mapping[str, int] | None = None,
) -> SimilarityResult:
    rows: dict[str, HashDistance] = {}
    for name in HASH_NAMES:
        distance = int(imagehash.hex_to_hash(getattr(reference, name)) - imagehash.hex_to_hash(getattr(candidate, name)))
        rows[name] = HashDistance(distance, HASH_BITS, hash_band(name, distance, limits))
    sha_equal = reference.sha256 == candidate.sha256
    bands = [rows[name].band for name in HASH_NAMES]
    strong_count = sum(band in {"exact", "strong"} for band in bands)
    review_count = sum(band in {"exact", "strong", "review"} for band in bands)
    rules = consensus or DEFAULT_CONSENSUS
    if sha_equal:
        verdict, reason = "exact", "SHA-256 identique : fichiers strictement identiques."
    elif strong_count >= int(rules["same_strong_count"]):
        verdict, reason = "same", "Consensus : les trois hashes perceptuels sont exacts ou strong."
    elif strong_count >= int(rules["near_strong_count"]):
        verdict, reason = "near_duplicate", "Consensus : au moins deux hashes perceptuels sont exacts ou strong."
    elif review_count >= int(rules["near_review_count"]):
        verdict, reason = "near_duplicate", "Consensus : au moins deux hashes perceptuels sont en bande review ou mieux."
    else:
        verdict, reason = "different", "Consensus insuffisant : moins de deux hashes atteignent la bande review."
    return SimilarityResult(sha_equal, rows["phash"], rows["dhash"], rows["whash"], verdict, reason)


def compare_images(
    reference_path: str | Path,
    candidate_path: str | Path,
    limits: Mapping[str, Mapping[str, int]] | None = None,
    consensus: Mapping[str, int] | None = None,
) -> SimilarityResult:
    return compare_hashes(compute_hashes(reference_path), compute_hashes(candidate_path), limits, consensus)


def similarity_sort_key(result: SimilarityResult) -> tuple[int, int, int, int]:
    order = {"exact": 0, "same": 1, "near_duplicate": 2, "different": 3}
    return (order[result.verdict], sum(result.distances()), *sorted(result.distances())[:2])
