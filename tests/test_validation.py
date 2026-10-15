"""File-level quarantine and row-level rejection (ERROR_CATALOG.md)."""

LAB_B_HEADER = "subject,specimen,test_code,result,unit,collection_date\n"


def test_bad_rows_go_to_dlq_with_codes_and_good_rows_still_load(env) -> None:
    env.write(
        "lab_b",
        "lab_b_bad_rows.csv",
        LAB_B_HEADER
        + "P001,S200,HSCRP,3200,ug/L,2026-03-01\n"   # row 1: good
        + ",S202,HSCRP,1000,ug/L,2026-03-01\n"       # row 2: R001 missing subject
        + "P001,S203,HSCRP,abc,ug/L,2026-03-01\n"    # row 3: R002 not a number
        + "P001,S204,HSCRP,1000,ug/L,2026-13-45\n"   # row 4: R003 invalid date
        + "P001,S205,HSCRPP,1000,ug/L,2026-03-01\n"  # row 5: R005 unknown analyte
        + "P001,S206,HSCRP,1000,xyz,2026-03-01\n"    # row 6: R006 unknown unit
        + "P001,S207,HSCRP,1000,bpm,2026-03-01\n"    # row 7: R007 unit for another analyte
        + "P001,S208,HSCRP,NA,ug/L,2026-03-01\n"     # row 8: R001 'NA' is a missing value
        + "".join(f"P003,S3{i:02d},HSCRP,2000,ug/L,2026-03-02\n" for i in range(7)),  # rows 9-15: good
    )

    summary = env.run()

    assert summary.rows_in == 15
    assert summary.rows_ok == 8
    assert summary.rows_dlq == 7
    assert summary.balanced
    dlq = env.query("SELECT source_row, error_code FROM meta.dlq ORDER BY source_row")
    assert dlq == [(2, "R001"), (3, "R002"), (4, "R003"), (5, "R005"), (6, "R006"), (7, "R007"), (8, "R001")]
    raw = env.scalar("SELECT raw_row FROM meta.dlq WHERE source_row = 3")
    assert '"result":"abc"' in raw


def test_file_with_unknown_columns_is_quarantined(env) -> None:
    env.write("lab_b", "lab_b_renamed.csv", "subject_id,specimen_id,test_code,result,unit,collection_date\n"
              "P001,S200,HSCRP,3200,ug/L,2026-03-01\n")

    summary = env.run()

    assert summary.quarantined == [("lab_b_renamed.csv", "F004")]
    assert env.scalar("SELECT count(*) FROM silver.measurement") == 0
    assert list((env.layout.zone("quarantine") / "source=lab_b").glob("*.csv"))


def test_empty_and_header_only_files_are_quarantined(env) -> None:
    env.write("lab_a", "empty.csv", "")
    env.write("lab_a", "header_only.csv", "participant_id,sample_id,analyte,value,unit,collected_at\n")

    summary = env.run()

    assert sorted(summary.quarantined) == [("empty.csv", "F003"), ("header_only.csv", "F003")]
    assert not list(env.layout.zone("bronze").rglob("*.csv"))


def test_unknown_source_folder_is_quarantined(env) -> None:
    env.write("vendor_x", "data.csv", "a,b\n1,2\n")

    summary = env.run()

    assert summary.quarantined == [("data.csv", "F001")]


def test_in_file_duplicates_conflicting_rejected_exact_filtered(env) -> None:
    env.write(
        "lab_a",
        "lab_a_dups.csv",
        "participant_id,sample_id,analyte,value,unit,collected_at\n"
        "P001,S100,CRP,3.2,mg/L,2026-01-10\n"
        "P001,S100,CRP,3.2,mg/L,2026-01-10\n"   # exact duplicate → filtered
        "P002,S101,CRP,1.7,mg/L,2026-01-11\n"
        "P002,S101,CRP,9.9,mg/L,2026-01-11\n",  # same record, different value → both rejected
    )

    summary = env.run()

    assert (summary.rows_ok, summary.rows_filtered, summary.rows_dlq) == (1, 1, 2)
    assert summary.balanced
    assert env.query("SELECT DISTINCT error_code FROM meta.dlq") == [("R010",)]


def test_future_dates_and_hard_ranges_are_rejected_soft_ranges_warned(env) -> None:
    env.write(
        "ehr_vitals",
        "vitals_ranges.csv",
        "patient_id,encounter_id,vital_type,value,unit,recorded_at\n"
        "P001,E400,HR,72,bpm,2026-01-12 08:00\n"        # ok
        "P001,E401,HR,-5,bpm,2026-01-12 09:00\n"        # R008 below hard minimum
        "P001,E402,HR,210,bpm,2026-01-12 10:00\n"       # W001 above soft maximum
        "P002,E403,SYS_BP,120,mmHg,2099-01-01 10:00\n"  # R004 in the future
        "P002,E404,SYS_BP,118,mmHg,2026-01-12 11:00\n"  # ok
        "P002,E405,HR,64,beats/min,2026-01-12 12:00\n",  # ok via unit alias beats/min -> bpm
    )

    summary = env.run()

    assert (summary.rows_ok, summary.rows_dlq, summary.rows_warn) == (4, 2, 1)
    assert env.query("SELECT error_code FROM meta.dlq ORDER BY source_row") == [("R008",), ("R004",)]
    assert env.query("SELECT quality_flags FROM silver.measurement WHERE source_value = '210'") == [(["W001"],)]


def test_control_samples_are_filtered_not_ingested(env) -> None:
    env.drop("lab_a", "lab_a_sample.csv")
    env.drop("olink", "olink_with_controls.csv")

    summary = env.run()

    assert summary.rows_filtered == 2
    assert summary.balanced
    assert env.query("SELECT DISTINCT source_sample_id FROM silver.measurement WHERE source = 'olink'") == [("S100",)]


def test_silent_unit_change_raises_distribution_alert(env) -> None:
    env.drop("lab_b", "lab_b_sample.csv")
    env.run()

    env.drop("lab_b", "lab_b_silent_unit_change.csv")
    summary = env.run()

    assert [(e.event_type, e.code, e.severity) for e in summary.events] == [("DistributionShift", "B001", "high")]
    assert env.query("SELECT code, source FROM meta.events") == [("B001", "lab_b")]


def test_issue_1_file_outside_a_source_folder_is_quarantined_not_ignored(env) -> None:
    (env.layout.zone("landing") / "stray.csv").write_text("participant_id,sample_id\nP001,S100\n")

    summary = env.run()

    assert summary.quarantined == [("stray.csv", "F001")]
    assert "not inside a source folder" in summary.events[0].detail
    assert not (env.layout.zone("landing") / "stray.csv").exists()
