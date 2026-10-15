"""Processing the same input again never changes the result (WALKTHROUGH §2)."""


def test_rerun_without_new_files_changes_nothing(env, snapshot) -> None:
    env.drop_brief_samples()
    env.run()
    before = snapshot()

    summary = env.run()

    assert summary.files_in == 0
    assert snapshot() == before


def test_redelivered_file_is_skipped_as_duplicate(env, snapshot) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()
    before = snapshot()

    env.drop("lab_a", "lab_a_sample.csv", as_name="lab_a_resent.csv")
    summary = env.run()

    assert summary.files_duplicate == 1
    assert snapshot() == before


def test_same_records_in_a_different_file_write_no_new_versions(env, snapshot) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.run()
    before = snapshot()

    # Different bytes (rows reordered), same records and values.
    env.write(
        "lab_a",
        "lab_a_reordered.csv",
        "participant_id,sample_id,analyte,value,unit,collected_at\n"
        "P002,S101,CRP,1.7,mg/L,2026-01-11\n"
        "P001,S100,CRP,3.2,mg/L,2026-01-10\n",
    )
    summary = env.run()

    assert summary.files_committed == 1
    assert summary.rows_ok == 2
    assert snapshot() == before
