"""Corrections, retractions, preliminary results and identity conflicts (WALKTHROUGH §1, §5)."""


def _history(env, sample: str) -> list[tuple]:
    return env.query(
        "SELECT version, change_reason, value, result_status, quality_flags FROM silver.measurement "
        "WHERE source = 'lab_a' AND source_sample_id = ? ORDER BY version",
        [sample],
    )


def test_correction_creates_a_new_version_and_keeps_history(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    env.drop("lab_a", "lab_a_correction.csv")
    env.run()

    assert _history(env, "S100") == [
        (1, "NEW", 3.2, "F", []),
        (2, "SOURCE_CORRECTION", 2.3, "C", []),
    ]
    current = env.query("SELECT value FROM gold.measurement_current WHERE source = 'lab_a' AND global_sample_id IN "
                        "(SELECT global_sample_id FROM silver.measurement WHERE source_sample_id = 'S100')")
    assert current == [(2.3,)]


def test_unflagged_change_is_accepted_but_flagged(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    env.drop("lab_a", "lab_a_unflagged_change.csv")
    env.run()

    assert _history(env, "S100")[-1] == (2, "SOURCE_CORRECTION", 2.9, "F", ["W004"])


def test_preliminary_then_final_is_a_normal_update(env) -> None:
    header = "participant_id,sample_id,analyte,value,unit,collected_at,result_status\n"
    env.write("lab_a", "prelim.csv", header + "P001,S100,CRP,3.0,mg/L,2026-01-10,P\n")
    env.run()
    env.write("lab_a", "final.csv", header + "P001,S100,CRP,3.2,mg/L,2026-01-10,F\n")
    env.run()

    assert _history(env, "S100") == [
        (1, "NEW", 3.0, "P", ["W003"]),
        (2, "SOURCE_CORRECTION", 3.2, "F", []),
    ]


def test_retraction_removes_value_from_gold_but_keeps_history(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    env.drop("lab_a", "lab_a_retraction.csv")
    env.run()

    history = env.query(
        "SELECT version, change_reason, is_retraction FROM silver.measurement "
        "WHERE source_sample_id = 'S101' ORDER BY version"
    )
    assert history == [(1, "NEW", False), (2, "RETRACTION", True)]
    gold_samples = {r[0] for r in env.query(
        "SELECT s.source_sample_id FROM gold.measurement_current g JOIN silver.measurement s USING (record_id, version)")}
    assert gold_samples == {"S100"}


def test_retraction_of_unknown_record_is_rejected(env) -> None:
    env.drop("lab_a", "lab_a_retract_unknown.csv")

    summary = env.run()

    assert summary.rows_dlq == 1
    assert summary.balanced
    assert env.query("SELECT error_code FROM meta.dlq") == [("R011",)]


def test_sample_claimed_by_a_second_person_is_held_not_merged(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()

    env.drop("lab_a", "lab_a_sample_conflict.csv")
    summary = env.run()

    assert summary.rows_held == 1
    latest = env.query(
        "SELECT global_subject_id, resolution_status, hold_reason FROM silver.measurement "
        "WHERE source_sample_id = 'S100' ORDER BY version DESC LIMIT 1"
    )
    assert latest == [(None, "held", "H003")]
    # The established link is untouched: S100 still belongs to G-0001.
    assert env.query("SELECT global_subject_id FROM meta.sample_links") == [("G-0001",), ("G-0002",)]
