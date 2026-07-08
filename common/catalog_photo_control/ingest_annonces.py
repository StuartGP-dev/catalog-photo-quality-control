from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .catalog_config import load_settings
from .catalog_db import CatalogDb, init_schema, open_catalog_db, stable_id, upsert_sql, utc_now

try:  # Pillow is installed via requirements, but keep ingestion robust.
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
IGNORED_LEAF_DIR_NAMES = {"autre", "autres", "other", "others"}


@dataclass(frozen=True)
class ImageInfo:
    index: int
    path: Path
    sha256: str
    width: int | None
    height: int | None


@dataclass(frozen=True)
class AnnonceInfo:
    annonce_key: str
    source_dir: Path
    images: tuple[ImageInfo, ...]

    @property
    def annonce_id(self) -> str:
        return stable_id("ann", self.annonce_key, str(self.source_dir.resolve()))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if Image is None:
        return None, None
    try:
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None, None


def is_auxiliary_image_dir(directory: Path) -> bool:
    return directory.name.strip().lower() in IGNORED_LEAF_DIR_NAMES


def list_image_files(directory: Path) -> list[Path]:
    """Return only direct production images named 0..N.

    Non-numbered files are ignored. Subfolders are never traversed here, and
    leaf folders named "autre" / "autres" are treated as helper folders, not
    real annonces.
    """
    if is_auxiliary_image_dir(directory):
        return []

    numbered: list[tuple[int, Path]] = []
    seen_numbers: dict[int, Path] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if not path.stem.isdigit():
            continue
        index = int(path.stem)
        if index in seen_numbers:
            other = seen_numbers[index]
            raise ValueError(
                f"Images annonce dupliquees pour l'index {index}: {other.name} et {path.name} dans {directory}"
            )
        seen_numbers[index] = path
        numbered.append((index, path))

    numbered.sort(key=lambda item: item[0])
    indexes = [index for index, _ in numbered]
    expected = list(range(len(indexes)))
    if indexes and indexes != expected:
        missing = sorted(set(range(indexes[-1] + 1)) - set(indexes))
        raise ValueError(
            f"Images annonce non contigues dans {directory}. "
            f"Trouve: {indexes}. Attendu: 0..{len(indexes) - 1}. Manquants: {missing}"
        )

    return [path for _, path in numbered]


def find_annonce_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        raise FileNotFoundError(f"Annonces root does not exist: {root}")

    try:
        if list_image_files(root):
            yield root
    except ValueError as exc:
        print(f"WARNING invalid annonce skipped: {exc}", file=sys.stderr)

    for child in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: str(p).lower()):
        try:
            if list_image_files(child):
                yield child
        except ValueError as exc:
            print(f"WARNING invalid annonce skipped: {exc}", file=sys.stderr)


def annonce_key_for_dir(root: Path, directory: Path) -> str:
    try:
        annonce_key = directory.relative_to(root).as_posix()
    except ValueError:
        annonce_key = directory.name
    if annonce_key in {"", "."}:
        annonce_key = directory.name or "root"
    return annonce_key


def resolve_annonce_dir(root: Path, annonce_key: str) -> Path:
    wanted = annonce_key.replace("\\", "/").strip("/")
    parts = [part for part in wanted.split("/") if part]
    if not parts:
        raise ValueError("annonce_key vide")

    direct = root.joinpath(*parts)
    if direct.is_dir():
        return direct

    # Compatibility fallback: bijoux/O18 -> bijoux/O/O18.
    if len(parts) >= 2:
        mode = parts[0]
        code = parts[-1]
        parent = ""
        for char in code:
            if char.isalpha():
                parent += char
            else:
                break
        if parent:
            dynamic = root / mode / parent.upper() / code
            if dynamic.is_dir():
                return dynamic

    raise FileNotFoundError(f"Annonce introuvable sous {root}: {annonce_key}")


def build_annonce_info(root: Path, directory: Path) -> AnnonceInfo:
    annonce_key = annonce_key_for_dir(root, directory)
    images: list[ImageInfo] = []
    for image_index, image_path in ((int(path.stem), path) for path in list_image_files(directory)):
        width, height = image_dimensions(image_path)
        images.append(
            ImageInfo(
                index=image_index,
                path=image_path,
                sha256=sha256_file(image_path),
                width=width,
                height=height,
            )
        )
    return AnnonceInfo(annonce_key=annonce_key, source_dir=directory, images=tuple(images))


def upsert_annonce(db: CatalogDb, annonce: AnnonceInfo) -> None:
    now = utc_now()
    columns = [
        "annonce_id",
        "annonce_key",
        "source_dir",
        "image_count",
        "status",
        "created_at",
        "updated_at",
    ]
    sql = upsert_sql(
        db,
        table="annonces",
        columns=columns,
        conflict_columns=["annonce_id"],
        update_columns=["annonce_key", "source_dir", "image_count", "status", "updated_at"],
    )
    db.execute(
        sql,
        [
            annonce.annonce_id,
            annonce.annonce_key,
            str(annonce.source_dir),
            len(annonce.images),
            "active",
            now,
            now,
        ],
    )

    image_columns = [
        "image_id",
        "annonce_id",
        "image_index",
        "source_path",
        "sha256",
        "width",
        "height",
        "status",
        "created_at",
        "updated_at",
    ]
    image_sql = upsert_sql(
        db,
        table="annonce_images",
        columns=image_columns,
        conflict_columns=["image_id"],
        update_columns=["image_index", "source_path", "sha256", "width", "height", "status", "updated_at"],
    )
    for image in annonce.images:
        image_id = stable_id("img", annonce.annonce_id, image.index, image.sha256)
        db.execute(
            image_sql,
            [
                image_id,
                annonce.annonce_id,
                image.index,
                str(image.path),
                image.sha256,
                image.width,
                image.height,
                "active",
                now,
                now,
            ],
        )


def ingest(
    root: Path,
    *,
    limit: int | None = None,
    annonce_key: str | None = None,
    dry_run: bool = False,
    init_db: bool = True,
    db_dsn: str | None = None,
) -> int:
    settings = load_settings(annonces_root=root, db_dsn=db_dsn)

    if annonce_key:
        annonce_dirs = [resolve_annonce_dir(settings.annonces_root, annonce_key)]
    else:
        annonce_dirs = list(find_annonce_dirs(settings.annonces_root))

    if limit is not None:
        annonce_dirs = annonce_dirs[:limit]

    annonces = [build_annonce_info(settings.annonces_root, directory) for directory in annonce_dirs]

    if annonce_key and not annonces:
        raise FileNotFoundError(f"Annonce introuvable sous {settings.annonces_root}: {annonce_key}")

    if dry_run:
        for annonce in annonces:
            print(f"{annonce.annonce_key}: {len(annonce.images)} images | {annonce.source_dir}")
        return len(annonces)

    with open_catalog_db(settings) as db:
        if init_db:
            init_schema(db)
        for annonce in annonces:
            upsert_annonce(db, annonce)

    return len(annonces)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest local annonce folders into the shared catalog DB.")
    parser.add_argument("--annonces-root", default=None, help="Root folder containing annonce subfolders.")
    parser.add_argument("--db-dsn", default=None, help="Override CATALOG_DB_DSN for this run.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of annonce folders for smoke tests.")
    parser.add_argument("--annonce-key", default=None, help="Only ingest one annonce key, e.g. bijoux/O18.")
    parser.add_argument("--dry-run", action="store_true", help="Scan and print only, do not write DB.")
    parser.add_argument("--no-init-db", action="store_true", help="Do not create schema before ingesting.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(annonces_root=args.annonces_root, db_dsn=args.db_dsn)
    count = ingest(
        settings.annonces_root,
        limit=args.limit,
        annonce_key=args.annonce_key,
        dry_run=args.dry_run,
        init_db=not args.no_init_db,
        db_dsn=args.db_dsn,
    )
    action = "scanned" if args.dry_run else "ingested"
    print(f"ANNONCES {action}: {count}")
    print(f"root: {settings.annonces_root}")
    print(f"db: {settings.db_dsn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
