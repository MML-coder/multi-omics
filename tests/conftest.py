import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from omics_pipeline.ingest import RunSummary, dlq_replay, replay, run
from omics_pipeline.layout import Layout, create_layout

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
FIXTURES = ROOT / "tests" / "fixtures"

BRIEF_SAMPLES = {
    "ehr_vitals": "ehr_vitals_sample.csv",
    "lab_a": "lab_a_sample.csv",
    "lab_b": "lab_b_sample.csv",
    "olink": "olink_sample.csv",
}


@dataclass
class Env:
    layout: Layout
    config_dir: Path

    def drop(self, source: str, fixture: str, as_name: str | None = None) -> Path:
        """Deliver a fixture file into landing/<source>/ (mtime = now, like a real arrival)."""
        target_dir = self.layout.zone("landing") / source
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (as_name or fixture)
        shutil.copy(FIXTURES / fixture, target)
        return target

    def write(self, source: str, name: str, content: str) -> Path:
        """Deliver an inline file (for scenario-specific content)."""
        target_dir = self.layout.zone("landing") / source
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        target.write_text(content)
        return target

    def drop_brief_samples(self) -> None:
        for source, fixture in BRIEF_SAMPLES.items():
            self.drop(source, fixture)

    def run(self, **kwargs) -> RunSummary:
        return run(self.layout, self.config_dir, **kwargs)

    def replay(self, **kwargs) -> RunSummary:
        return replay(self.layout, self.config_dir, **kwargs)

    def dlq_replay(self, **kwargs) -> RunSummary:
        return dlq_replay(self.layout, self.config_dir, **kwargs)

    def append_reference(self, filename: str, line: str) -> None:
        """Simulate a reviewed reference-data change in this test's private config copy."""
        with (self.config_dir / "reference" / filename).open("a") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        con = duckdb.connect(str(self.layout.warehouse), read_only=True)
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def scalar(self, sql: str, params: list | None = None):
        return self.query(sql, params)[0][0]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    layout = Layout(tmp_path / "data")
    create_layout(layout)
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    return Env(layout, config_dir)


@pytest.fixture
def snapshot(env: Env) -> Callable[[], tuple]:
    """Content fingerprint of silver and gold, used to prove that reruns change nothing."""

    def take() -> tuple:
        silver = env.query(
            "SELECT count(*), string_agg(record_id || ':' || version || ':' || derived_hash, ',' "
            "ORDER BY record_id, version) FROM silver.measurement"
        )[0]
        gold = env.query(
            "SELECT count(*), string_agg(record_id || ':' || version, ',' ORDER BY record_id) "
            "FROM gold.measurement_current"
        )[0]
        return silver, gold

    return take
