from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from common.catalog_photo_control.diversity_gate import DiversityGate, nearest_to_json
from common.catalog_photo_control.image_similarity import (
    DEFAULT_BAND_LIMITS,
    SIMILARITY_ENGINE_VERSION,
    ImageHashes,
    compare_hashes,
    compare_images,
    validate_similarity_config,
)
from common.catalog_photo_control.models import ListingVariant, Recipe, SourceImage, SourceListing, ordered_source_set_hash, stable_hash
from common.catalog_photo_control.variants_db import VariantsDatabase


def _hex(distance: int) -> str:
    return f"{(1 << distance) - 1:016x}"


def _hashes(p: int, d: int, w: int, sha: str = "candidate") -> ImageHashes:
    return ImageHashes(sha, _hex(p), _hex(d), _hex(w))


def _config() -> dict[str, object]:
    return {
        "enabled": True, "compare_same_image_index_only": True,
        "include_ready_variants": True, "engine_version": SIMILARITY_ENGINE_VERSION,
        "hash_size": 8, "band_limits": DEFAULT_BAND_LIMITS,
        "reject_verdicts": ["exact", "same", "near_duplicate"],
        "consensus": {"same_strong_count": 3, "near_strong_count": 2, "near_review_count": 2},
        "nearest_neighbors_to_persist": 3,
    }


def _image(path: Path, color: tuple[int, int, int], shift: int = 0) -> str:
    canvas = Image.new("RGB", (90, 70), (245, 245, 245)); draw = ImageDraw.Draw(canvas)
    draw.rectangle((18 + shift, 12, 62 + shift, 54), fill=color); canvas.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listing(root: Path, count: int = 5) -> SourceListing:
    folder = root / "source"; folder.mkdir()
    images = []
    for index in range(count):
        path = folder / f"{index}.png"; digest = _image(path, (60 + index * 25, 80, 130))
        images.append(SourceImage(index, path, digest, 90, 70))
    rows = tuple(images)
    return SourceListing(stable_hash("O18"), "O18", folder, rows, ordered_source_set_hash(rows))


def _save_ready(db: VariantsDatabase, listing: SourceListing, root: Path) -> tuple[int, list[Path]]:
    paths, rows = [], []
    for source in listing.images:
        path = root / f"ready-{source.index}.png"; digest = _image(path, (60 + source.index * 25, 80, 130), 5)
        paths.append(path); rows.append({"image_index": source.index, "source_hash": source.source_hash, "output_path": path, "output_hash": digest, "metrics": {"output_width": 90, "output_height": 70}})
    variant = ListingVariant(None, listing.listing_id, listing.source_set_hash, Recipe.from_parameters({"ready": 1}), tuple(paths), 1, diversity_gate_version=SIMILARITY_ENGINE_VERSION)
    return db.save_complete_variant(variant, rows), paths


def test_consensus_verdicts_and_raw_bands() -> None:
    reference = _hashes(0, 0, 0, "same-sha")
    exact = compare_hashes(reference, _hashes(12, 12, 12, "same-sha"))
    same = compare_hashes(reference, _hashes(4, 3, 2))
    two_strong = compare_hashes(reference, _hashes(4, 4, 20))
    two_review = compare_hashes(reference, _hashes(8, 9, 20))
    different = compare_hashes(reference, _hashes(12, 17, 19))
    assert exact.verdict == "exact" and exact.sha256_equal
    assert same.verdict == "same"
    assert two_strong.verdict == "near_duplicate"
    assert two_review.verdict == "near_duplicate"
    assert different.verdict == "different"
    assert two_review.phash.distance == 8 and two_review.phash.band == "review"


def test_threshold_validation_and_config_hash_changes() -> None:
    config = _config(); validate_similarity_config(config)
    broken = _config(); broken["band_limits"] = {**DEFAULT_BAND_LIMITS, "phash": {"strong": 11, "review": 10, "weak": 16}}
    try:
        validate_similarity_config(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("unordered thresholds accepted")
    first = stable_hash(config); config["engine_version"] = "next"; second = stable_hash(config)
    config["band_limits"] = {**DEFAULT_BAND_LIMITS, "phash": {"strong": 3, "review": 10, "weak": 16}}
    assert len({first, second, stable_hash(config)}) == 3


def test_exif_orientation_is_normalized(tmp_path: Path) -> None:
    base = Image.new("RGB", (40, 20), "white"); ImageDraw.Draw(base).rectangle((2, 2, 15, 17), fill="black")
    upright = tmp_path / "upright.jpg"; base.transpose(Image.Transpose.ROTATE_270).save(upright, quality=100)
    oriented = tmp_path / "oriented.jpg"; exif = base.getexif(); exif[274] = 6; base.save(oriented, quality=100, exif=exif)
    result = compare_images(upright, oriented)
    assert result.verdict == "near_duplicate"
    assert result.dhash.distance == result.whash.distance == 0


def test_gate_uses_ready_same_index_deduplicates_and_ignores_missing(tmp_path: Path) -> None:
    listing = _listing(tmp_path, 2); db = VariantsDatabase(tmp_path / "variants.sqlite3"); db.initialize(); db.register_source(listing)
    variant_id, ready = _save_ready(db, listing, tmp_path)
    missing = tmp_path / "missing.png"
    db.connection.execute("UPDATE listing_variant_images SET output_path=? WHERE variant_id=? AND image_index=1", (str(missing), variant_id)); db.connection.commit()
    candidate = tmp_path / "candidate.png"; candidate.write_bytes(ready[0].read_bytes())
    verdict = DiversityGate(db.connection, _config()).evaluate_image(listing.listing_id, listing.source_set_hash, 0, candidate)
    assert not verdict.valid and verdict.reference_count == 1 and verdict.nearest.comparison.verdict == "exact"
    no_reference = DiversityGate(db.connection, _config()).evaluate_image(listing.listing_id, listing.source_set_hash, 1, candidate)
    assert no_reference.valid and no_reference.status == "no_reference_yet"
    payload = json.loads(nearest_to_json(verdict.nearest))
    assert payload["image_index"] == 0 and {payload[name]["band"] for name in ("phash", "dhash", "whash")} == {"exact"}
    db.close()


def test_five_image_near_duplicate_rejects_atomically_without_final_rows(tmp_path: Path) -> None:
    listing = _listing(tmp_path); db = VariantsDatabase(tmp_path / "variants.sqlite3"); db.initialize(); db.register_source(listing)
    _, ready = _save_ready(db, listing, tmp_path)
    candidates = []
    for index in range(5):
        path = tmp_path / f"candidate-{index}.png"
        if index == 0: path.write_bytes(ready[0].read_bytes())
        else: _image(path, (10, 220 - index * 20, 30 + index * 20), -10)
        candidates.append((index, path))
    before_variants = db.connection.execute("SELECT COUNT(*) FROM listing_variants").fetchone()[0]
    before_images = db.connection.execute("SELECT COUNT(*) FROM listing_variant_images").fetchone()[0]
    verdict = DiversityGate(db.connection, _config()).evaluate_variant(listing.listing_id, listing.source_set_hash, candidates)
    assert not verdict.valid and "perceptual_duplicate_image_0" in verdict.reasons
    rejected = ListingVariant(None, listing.listing_id, listing.source_set_hash, Recipe.from_parameters({"rejected": 1}), tuple(path for _, path in candidates), 2, diversity_gate_version=SIMILARITY_ENGINE_VERSION, diversity_valid=False)
    rows = [{"image_index": index, "source_hash": listing.images[index].source_hash, "output_path": path, "output_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "metrics": {"output_width": 91 + index, "output_height": 71 + index}} for index, path in candidates]
    with pytest.raises(Exception, match="diversity gate"):
        db.save_complete_variant(rejected, rows)
    assert db.connection.execute("SELECT COUNT(*) FROM listing_variants").fetchone()[0] == before_variants
    assert db.connection.execute("SELECT COUNT(*) FROM listing_variant_images").fetchone()[0] == before_images == 5
    db.close()
