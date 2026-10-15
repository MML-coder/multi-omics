"""Storage layout of the data directory (DESIGN.md §7.2, ADR-005).

Raw files live in directories (landing, bronze, quarantine); silver, gold, DLQ and run metadata are
tables in one DuckDB database file. The data directory defaults to ./data and can be overridden with
PIPELINE_DATA_DIR.
"""

import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV = "PIPELINE_DATA_DIR"
DEFAULT_DATA_DIR = Path("data")

ZONES = ("landing", "bronze", "quarantine")
WAREHOUSE_NAME = "warehouse.duckdb"


@dataclass(frozen=True)
class Layout:
    root: Path

    def zone(self, name: str) -> Path:
        if name not in ZONES:
            raise ValueError(f"Unknown zone '{name}'. Expected one of: {', '.join(ZONES)}")
        return self.root / name

    @property
    def warehouse(self) -> Path:
        return self.root / WAREHOUSE_NAME


def resolve_layout(data_dir: Path | None = None) -> Layout:
    """Pick the data directory: explicit argument, then environment variable, then default."""
    if data_dir is not None:
        return Layout(data_dir)
    from_env = os.environ.get(DATA_DIR_ENV)
    return Layout(Path(from_env) if from_env else DEFAULT_DATA_DIR)


def create_layout(layout: Layout) -> list[Path]:
    """Create any missing zone directories. Safe to run repeatedly; returns the directories it created."""
    created = []
    for name in ZONES:
        path = layout.zone(name)
        if not path.exists():
            path.mkdir(parents=True)
            created.append(path)
    return created
