"""Rebuild from bronze, apply fixes to history, and replay rejected rows (WALKTHROUGH §8)."""


def _history_scenario(env) -> None:
    """A realistic history: late proteomics, then labs, then a correction and a retraction."""
    env.drop("olink", "olink_sample.csv")
    env.run()
    env.drop("lab_a", "lab_a_sample.csv")
    env.drop("lab_b", "lab_b_sample.csv")
    env.drop("ehr_vitals", "ehr_vitals_sample.csv")
    env.run()
    env.drop("lab_a", "lab_a_correction.csv")
    env.drop("lab_a", "lab_a_retraction.csv")
    env.run()


def test_rebuild_from_bronze_alone_reproduces_silver_and_gold(env, snapshot) -> None:
    _history_scenario(env)
    before = snapshot()

    env.layout.warehouse.unlink()
    summary = env.replay()

    assert summary.files_failed == 0
    assert snapshot() == before


def test_replay_with_unchanged_configuration_writes_nothing(env, snapshot) -> None:
    _history_scenario(env)
    before = snapshot()

    summary = env.replay()

    assert summary.files_committed == 6
    assert snapshot() == before


def test_reference_fix_is_applied_to_history_as_derivation_change(env) -> None:
    env.drop("lab_b", "lab_b_sample.csv")
    env.run()

    # A reviewed fix: Lab B's HSCRP assay should be recorded as 'hs_crp_immunoturbidimetric'.
    aliases = env.config_dir / "reference" / "analyte_aliases.csv"
    aliases.write_text(aliases.read_text().replace("HSCRP,CRP,high_sensitivity", "HSCRP,CRP,hs_crp_immunoturbidimetric"))
    env.replay(source="lab_b")

    history = env.query(
        "SELECT version, change_reason, assay FROM silver.measurement WHERE source_subject_id = 'P001' ORDER BY version"
    )
    assert history == [(1, "NEW", "high_sensitivity"), (2, "DERIVATION_CHANGE", "hs_crp_immunoturbidimetric")]


def test_dlq_replay_after_unit_alias_fix(env) -> None:
    env.drop("lab_b", "lab_b_unknown_unit.csv")
    first = env.run()
    assert first.rows_dlq == 1
    assert env.query("SELECT error_code, raw_value, status FROM meta.dlq") == [("R006", "mg/dl", "OPEN")]

    env.append_reference("unit_aliases.csv", "mg/dl,mg/dL")
    second = env.dlq_replay(error_code="R006")

    assert second.rows_dlq == 0
    status, record_id = env.query("SELECT status, resolved_record_id FROM meta.dlq")[0]
    assert status == "RESOLVED"
    assert env.query("SELECT value, unit, source_unit FROM silver.measurement WHERE record_id = ?", [record_id]) == [
        (3.2, "mg/L", "mg/dl")
    ]


def test_failed_dlq_replay_keeps_rows_open_and_counts_attempts(env) -> None:
    env.drop("lab_b", "lab_b_unknown_unit.csv")
    env.run()

    env.dlq_replay()

    assert env.query("SELECT status, attempts FROM meta.dlq") == [("OPEN", 2)]


def test_replay_over_corrected_history_never_resurrects_old_values(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()
    env.drop("lab_a", "lab_a_correction.csv")
    env.run()

    aliases = env.config_dir / "reference" / "analyte_aliases.csv"
    aliases.write_text(aliases.read_text().replace("lab_a,CRP,CRP,standard", "lab_a,CRP,CRP,standard_turbidimetric"))
    env.replay(source="lab_a")

    history = env.query(
        "SELECT version, change_reason, value, assay FROM silver.measurement "
        "WHERE source_sample_id = 'S100' ORDER BY version"
    )
    # The fix lands on the current (corrected) value; the superseded 3.2 is not brought back.
    assert history == [
        (1, "NEW", 3.2, "standard"),
        (2, "SOURCE_CORRECTION", 2.3, "standard"),
        (3, "DERIVATION_CHANGE", 2.3, "standard_turbidimetric"),
    ]


def test_files_read_back_from_bronze_gain_no_extra_columns(env) -> None:
    # Bronze folders are named source=<name>; reading from them must not add a 'source' column.
    env.drop("lab_b", "lab_b_unknown_unit.csv")
    env.run()
    env.append_reference("unit_aliases.csv", "mg/dl,mg/dL")

    replayed = env.dlq_replay()
    full = env.replay()

    assert replayed.events == [] and full.events == []
    assert env.query("SELECT columns FROM meta.ingested_files") == [
        (["subject", "specimen", "test_code", "result", "unit", "collection_date"],)
    ]
    assert env.scalar("SELECT count(*) FROM meta.schema_drift") == 0
