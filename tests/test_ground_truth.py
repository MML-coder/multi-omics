"""End-to-end correctness against generated ground truth (idempotency, validation, normalization)."""

import hashlib
from pathlib import Path

import pytest

from conftest import CONFIG_DIR, Env
from omics_pipeline.layout import Layout, create_layout
from omics_pipeline.synth import SynthSpec, deliver, delivery_days, generate, verify

SPEC = SynthSpec(subjects=60, proteins=6, days=3, error_rate=0.08, correction_rate=0.1, retraction_rate=0.05, seed=7)


@pytest.fixture
def generated(tmp_path) -> tuple[Env, Path]:
    out = tmp_path / "synthetic"
    generate(SPEC, out, CONFIG_DIR)
    env = Env(Layout(tmp_path / "data"), out / "config")
    create_layout(env.layout)
    return env, out


def _run_all_days(env: Env, out: Path) -> list:
    summaries = []
    for day in delivery_days(out):
        deliver(day, env.layout.zone("landing"))
        summaries.append(env.run())
    return summaries


def test_generation_is_deterministic(tmp_path) -> None:
    def fingerprint(folder: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(folder.rglob("*.csv")):
            digest.update(path.relative_to(folder).as_posix().encode() + path.read_bytes())
        return digest.hexdigest()

    generate(SPEC, tmp_path / "a", CONFIG_DIR)
    generate(SPEC, tmp_path / "b", CONFIG_DIR)

    assert fingerprint(tmp_path / "a") == fingerprint(tmp_path / "b")


def test_pipeline_output_matches_ground_truth(generated) -> None:
    env, out = generated

    summaries = _run_all_days(env, out)

    assert all(s.balanced for s in summaries)
    assert sum(s.files_duplicate for s in summaries) == 1
    assert sum(s.rows_resolved for s in summaries) > 0  # proteomics arrived before its Lab A link
    report = verify(env.layout.warehouse, out)
    assert report["ok"], report


def _snapshot(env: Env) -> tuple:
    return env.query(
        "SELECT count(*), string_agg(record_id || ':' || version || ':' || derived_hash, ',' ORDER BY record_id, version) "
        "FROM silver.measurement"
    )[0]


def test_redelivering_everything_and_replaying_changes_nothing(generated) -> None:
    env, out = generated
    _run_all_days(env, out)
    before = _snapshot(env)
    assert before[0] > 0

    redelivered = _run_all_days(env, out)
    replayed = env.replay()

    assert all(s.files_committed == 0 for s in redelivered)
    assert replayed.files_failed == 0
    assert _snapshot(env) == before
    assert verify(env.layout.warehouse, out)["ok"]


def test_rebuild_from_bronze_matches_ground_truth(generated) -> None:
    env, out = generated
    _run_all_days(env, out)

    env.layout.warehouse.unlink()
    env.replay()

    assert verify(env.layout.warehouse, out)["ok"]
