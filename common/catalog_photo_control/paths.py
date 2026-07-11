from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalPaths:
    root: Path

    @classmethod
    def from_root(cls, root: str | Path = "local") -> "LocalPaths":
        return cls(Path(root).resolve())

    @property
    def databases(self) -> Path:
        return self.root / "databases"

    @property
    def bench_database(self) -> Path:
        return self.databases / "catalog_bench.sqlite3"

    @property
    def variants_database(self) -> Path:
        return self.databases / "catalog_variants.sqlite3"

    @property
    def bench_work(self) -> Path:
        return self.root / "bench_work"

    @property
    def bench_runs(self) -> Path:
        return self.root / "bench_runs"

    def ensure_runtime_directories(self) -> None:
        for directory in (self.databases, self.bench_work, self.bench_runs):
            directory.mkdir(parents=True, exist_ok=True)
