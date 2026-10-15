"""The brief's four sample datasets flow through to harmonized gold data."""

from datetime import date, datetime


def test_brief_samples_reach_gold_with_row_accounting(env) -> None:
    env.drop_brief_samples()

    summary = env.run()

    assert summary.files_committed == 4
    assert summary.rows_in == 8
    assert summary.balanced
    assert summary.rows_dlq == 0
    assert summary.gold_rows == 8
    recorded = env.query(
        "SELECT rows_in, rows_ok + rows_dlq + rows_held + rows_filtered FROM meta.pipeline_runs"
    )
    assert recorded == [(8, 8)]


def test_lab_b_hscrp_is_normalized_to_crp_mg_per_l(env) -> None:
    env.drop("lab_b", "lab_b_sample.csv")
    env.run()

    rows = env.query(
        "SELECT source_subject_id, analyte_code, assay, value, unit, source_value, source_unit "
        "FROM silver.measurement ORDER BY source_subject_id"
    )

    assert rows == [
        ("P001", "CRP", "high_sensitivity", 3.2, "mg/L", "3200", "ug/L"),
        ("P003", "CRP", "high_sensitivity", 2.1, "mg/L", "2100", "ug/L"),
    ]


def test_subjects_resolve_through_registry(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.drop("lab_b", "lab_b_sample.csv")
    env.run()

    subjects = env.query(
        "SELECT s.source, s.source_subject_id, g.global_subject_id FROM gold.measurement_current g "
        "JOIN silver.measurement s USING (record_id, version) ORDER BY 1, 2"
    )

    assert subjects == [
        ("lab_a", "P001", "G-0001"),
        ("lab_a", "P002", "G-0002"),
        ("lab_b", "P001", "G-0001"),
        ("lab_b", "P003", "G-0003"),
    ]


def test_olink_links_to_person_and_collection_date_through_lab_a_sample(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.drop("olink", "olink_sample.csv")
    env.run()

    rows = env.query(
        "SELECT source_sample_id, global_subject_id, observed_at, assay_run_date, value, unit, "
        "measurement_scale, quality_flags FROM silver.measurement WHERE source = 'olink' ORDER BY 1"
    )

    assert rows == [
        ("S100", "G-0001", datetime(2026, 1, 10), date(2026, 3, 5), 10.4, "NPX", "relative_log2", []),
        ("S101", "G-0002", datetime(2026, 1, 11), date(2026, 3, 5), 3.1, "NPX", "relative_log2", ["W002"]),
    ]


def test_olink_before_lab_a_is_held_then_resolved(env) -> None:
    env.drop("olink", "olink_sample.csv")
    first = env.run()
    assert first.rows_held == 2
    assert first.gold_rows == 0

    env.drop("lab_a", "lab_a_sample.csv")
    second = env.run()

    assert second.rows_resolved == 2
    history = env.query(
        "SELECT version, change_reason, resolution_status, hold_reason FROM silver.measurement "
        "WHERE source = 'olink' AND source_sample_id = 'S100' ORDER BY version"
    )
    assert history == [(1, "NEW", "held", "H002"), (2, "RESOLVED", "resolved", None)]
    assert env.scalar("SELECT count(*) FROM gold.measurement_current WHERE source = 'olink'") == 2


def test_ehr_local_timestamps_are_stored_in_utc(env) -> None:
    env.drop("ehr_vitals", "ehr_vitals_sample.csv")
    env.run()

    observed = env.query("SELECT observed_at, observed_at_precision FROM silver.measurement ORDER BY observed_at")

    # 09:30 in New York in January is 14:30 UTC (assumption A4, configured timezone).
    assert observed[0] == (datetime(2026, 1, 10, 14, 30), "minute")


def test_relative_and_absolute_crp_are_kept_apart(env) -> None:
    env.drop_brief_samples()
    env.run()

    scales = env.query(
        "SELECT DISTINCT measurement_scale, unit FROM gold.measurement_current WHERE analyte_code = 'CRP' ORDER BY 1"
    )

    assert scales == [("absolute", "mg/L"), ("relative_log2", "NPX")]
