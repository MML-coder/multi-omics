"""Failures are contained per file and leave nothing half-written (WALKTHROUGH §6)."""

import pytest

from omics_pipeline import transform

LAB_A_HEADER = "participant_id,sample_id,analyte,value,unit,collected_at\n"


@pytest.fixture
def crash_on_lab_b(monkeypatch):
    """Simulate a crash in the middle of writing Lab B (after its silver rows are inserted)."""
    original = transform._write_dlq

    def crashing(con, contract, ctx):
        if contract.source == "lab_b":
            raise RuntimeError("simulated crash mid-write")
        return original(con, contract, ctx)

    monkeypatch.setattr(transform, "_write_dlq", crashing)
    return monkeypatch


def test_crash_mid_file_writes_nothing_for_that_file_and_others_commit(env, crash_on_lab_b) -> None:
    env.drop_brief_samples()

    summary = env.run()

    assert summary.files_failed == 1
    assert summary.files_committed == 3
    assert env.scalar("SELECT count(*) FROM silver.measurement WHERE source = 'lab_b'") == 0
    assert env.scalar("SELECT count(*) FROM meta.ingested_files WHERE source = 'lab_b'") == 0
    assert (env.layout.zone("landing") / "lab_b" / "lab_b_sample.csv").exists()


def test_rerun_after_crash_matches_a_clean_run(env, crash_on_lab_b, snapshot, tmp_path) -> None:
    env.drop_brief_samples()
    env.run()
    crash_on_lab_b.undo()

    recovered = env.run()
    assert recovered.files_committed == 1

    from conftest import Env  # a clean environment processing the same files without a crash
    import shutil
    from omics_pipeline.layout import Layout, create_layout

    clean = Env(Layout(tmp_path / "clean"), env.config_dir)
    create_layout(clean.layout)
    clean.drop_brief_samples()
    clean.run()
    clean_gold = clean.query("SELECT record_id, value, global_subject_id FROM gold.measurement_current ORDER BY 1")
    assert env.query("SELECT record_id, value, global_subject_id FROM gold.measurement_current ORDER BY 1") == clean_gold


def test_mostly_bad_file_is_quarantined_whole(env) -> None:
    env.write("lab_a", "lab_a_mostly_bad.csv", LAB_A_HEADER
              + "P001,S100,CRP,3.2,mg/L,2026-01-10\n"
              + "P001,S101,CRP,abc,mg/L,2026-01-10\n"
              + "P001,S102,CRP,,mg/L,2026-01-10\n"
              + "P001,S103,CRP,1.0,furlongs,2026-01-10\n")

    summary = env.run()

    assert summary.quarantined == [("lab_a_mostly_bad.csv", "F005")]
    assert env.scalar("SELECT count(*) FROM silver.measurement") == 0
    assert env.scalar("SELECT count(*) FROM meta.dlq") == 0
