"""Every canonical row traces back to its source file, row, run and configuration versions."""

from omics_pipeline.ingest import file_sha256


def test_every_silver_row_carries_lineage(env) -> None:
    env.drop_brief_samples()
    env.run()

    missing = env.scalar(
        "SELECT count(*) FROM silver.measurement WHERE file_hash IS NULL OR source_file IS NULL "
        "OR source_row IS NULL OR run_id IS NULL OR contract_version IS NULL "
        "OR reference_version IS NULL OR registry_version IS NULL OR pipeline_version IS NULL"
    )

    assert missing == 0


def test_lineage_points_to_an_immutable_bronze_copy(env) -> None:
    env.drop("lab_b", "lab_b_sample.csv")
    env.run()

    file_hash, source_row, bronze_path = env.query(
        "SELECT m.file_hash, m.source_row, f.bronze_path FROM silver.measurement m "
        "JOIN meta.ingested_files f USING (file_hash) WHERE m.source_subject_id = 'P001'"
    )[0]

    from pathlib import Path

    bronze = Path(bronze_path)
    assert file_sha256(bronze) == file_hash
    assert bronze.read_text().splitlines()[source_row].startswith("P001,S200,HSCRP,3200")


def test_all_recorded_timestamps_are_utc(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    # Python-side (received_at) and SQL-side (ingested_at) clocks must agree regardless of machine timezone.
    drift = env.scalar(
        "SELECT max(abs(epoch(m.ingested_at) - epoch(f.received_at))) FROM silver.measurement m "
        "JOIN meta.ingested_files f USING (file_hash)"
    )

    assert drift < 60


def test_stage_timings_are_recorded(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    stages = {row[0] for row in env.query("SELECT stage FROM meta.run_stage_metrics")}

    assert {"read", "map_validate", "normalize_resolve", "write", "reresolve_held", "gold"} <= stages
