"""A vendor changes its file layout: quarantine, add a contract version, release (WALKTHROUGH §7)."""

import shutil

from conftest import FIXTURES

from omics_pipeline.ingest import release_quarantined


def test_new_layout_is_quarantined_then_ingested_under_contract_v2(env) -> None:
    env.drop("lab_b", "lab_b_sample.csv")
    env.drop("lab_b", "lab_b_v2_renamed.csv")
    first = env.run()
    assert first.quarantined == [("lab_b_v2_renamed.csv", "F004")]

    shutil.copy(FIXTURES / "contracts" / "lab_b_v2.yaml", env.config_dir / "sources" / "lab_b_v2.yaml")
    assert release_quarantined(env.layout, "") == ["lab_b_v2_renamed.csv"]
    second = env.run()

    assert second.files_committed == 1
    rows = env.query(
        "SELECT contract_version, source_subject_id, value, unit FROM silver.measurement ORDER BY source_sample_id"
    )
    assert rows == [
        ("lab_b_v1", "P001", 3.2, "mg/L"),
        ("lab_b_v1", "P003", 2.1, "mg/L"),
        ("lab_b_v2", "P001", 1.5, "mg/L"),
        ("lab_b_v2", "P003", 2.4, "mg/L"),
    ]


def test_old_layout_still_ingests_after_v2_is_added(env) -> None:
    shutil.copy(FIXTURES / "contracts" / "lab_b_v2.yaml", env.config_dir / "sources" / "lab_b_v2.yaml")
    env.drop("lab_b", "lab_b_sample.csv")

    summary = env.run()

    assert summary.files_committed == 1
    assert env.query("SELECT DISTINCT contract_version FROM silver.measurement") == [("lab_b_v1",)]
