"""How operators find out: events, schema drift, and exit codes (docs/DESIGN.md §11)."""

from omics_pipeline.cli import main

LAB_A_HEADER = "participant_id,sample_id,analyte,value,unit,collected_at"


def test_added_column_is_ingested_and_raises_schema_drift(env) -> None:
    env.write("lab_a", "lab_a_new_column.csv",
              f"{LAB_A_HEADER},specimen_type\nP001,S100,CRP,3.2,mg/L,2026-01-10,serum\n")

    summary = env.run()

    assert summary.files_committed == 1
    assert [(e.event_type, e.code, e.severity) for e in summary.events] == [("SchemaDriftDetected", "F006", "medium")]
    assert "specimen_type" in summary.events[0].detail
    assert env.query("SELECT columns FROM meta.ingested_files") == [
        (["participant_id", "sample_id", "analyte", "value", "unit", "collected_at", "specimen_type"],)
    ]
    assert summary.exit_code == 2


def test_optional_column_known_to_the_contract_is_not_drift(env) -> None:
    env.drop("lab_a", "lab_a_correction.csv")  # has result_status, which lab_a_v1 maps as optional

    summary = env.run()

    assert summary.events == []
    assert summary.exit_code == 0


def test_renamed_column_quarantines_and_publishes_high_severity_event(env) -> None:
    env.write("lab_a", "lab_a_renamed.csv",
              "participant_id,sample_id,analyte_code,value,unit,collected_at\nP001,S100,CRP,3.2,mg/L,2026-01-10\n")

    summary = env.run()

    event = summary.events[0]
    assert (event.event_type, event.code, event.severity) == ("FileQuarantined", "F004", "high")
    assert "analyte_code" in event.detail and "analyte" in event.detail
    assert env.scalar("SELECT count(*) FROM meta.events WHERE event_type = 'FileQuarantined'") == 1
    assert summary.exit_code == 2


def test_rolled_back_file_raises_no_drift_event(env, monkeypatch) -> None:
    from omics_pipeline import transform

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(transform, "_write_dlq", crash)
    env.write("lab_a", "lab_a_new_column.csv",
              f"{LAB_A_HEADER},specimen_type\nP001,S100,CRP,3.2,mg/L,2026-01-10,serum\n")

    summary = env.run()

    assert [e.event_type for e in summary.events] == ["ProcessingFailed"]
    assert summary.exit_code == 1


def test_cli_exit_codes(env, capsys) -> None:
    args = ["--data-dir", str(env.layout.root), "--config-dir", str(env.config_dir)]
    env.drop("lab_a", "lab_a_sample.csv")
    assert main([*args, "run"]) == 0

    env.write("lab_a", "bad.csv", "nonsense,columns\n1,2\n")
    assert main([*args, "run"]) == 2
    assert "needs attention" in capsys.readouterr().out


def test_issue_7_missing_config_folder_is_a_clear_configuration_error(env, capsys, tmp_path) -> None:
    missing = tmp_path / "no-such-config"

    run_code = main(["--data-dir", str(env.layout.root), "--config-dir", str(missing), "run"])
    check_code = main(["--config-dir", str(missing), "check-file", "--source", "lab_a", "x.csv"])

    err = capsys.readouterr().err
    assert run_code == check_code == 3
    assert f"config folder '{missing}' not found" in err


def test_issue_7_incomplete_config_folder_names_what_is_missing(env, capsys) -> None:
    (env.config_dir / "reference" / "unit_conversions.csv").unlink()

    code = main(["--data-dir", str(env.layout.root), "--config-dir", str(env.config_dir), "run"])

    assert code == 3
    assert "unit_conversions.csv" in capsys.readouterr().err
