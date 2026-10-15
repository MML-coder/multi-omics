"""The three schema-drift decisions — adopt, ignore, forbid — end to end (README "Schema change workflow")."""

import pytest

from omics_pipeline.cli import main
from omics_pipeline.config import ConfigError, parse_contract

LAB_A_HEADER = "participant_id,sample_id,analyte,value,unit,collected_at"
EHR_HEADER = "patient_id,encounter_id,vital_type,value,unit,recorded_at"


def _append_to_contract(env, name: str, text: str) -> None:
    path = env.config_dir / "sources" / name
    path.write_text(path.read_text() + "\n" + text + "\n")


def _drift(env) -> list[tuple]:
    return env.query("SELECT source, column_name, status, files_seen, decided_in FROM meta.schema_drift ORDER BY 1, 2")


def test_new_column_is_tracked_as_open_and_warns_on_every_file(env) -> None:
    env.write("lab_a", "day1.csv", f"{LAB_A_HEADER},specimen_type\nP001,S100,CRP,3.2,mg/L,2026-01-10,serum\n")
    first = env.run()
    env.write("lab_a", "day2.csv", f"{LAB_A_HEADER},specimen_type\nP002,S101,CRP,1.7,mg/L,2026-01-11,plasma\n")
    second = env.run()

    assert [e.code for e in first.events] == ["F006"]
    assert [e.code for e in second.events] == ["F006"]
    assert _drift(env) == [("lab_a", "specimen_type", "OPEN", 2, None)]


def test_ignore_decision_stops_warnings_and_is_recorded(env) -> None:
    env.write("lab_a", "day1.csv", f"{LAB_A_HEADER},specimen_type\nP001,S100,CRP,3.2,mg/L,2026-01-10,serum\n")
    env.run()

    _append_to_contract(env, "lab_a_v1.yaml", "ignored_columns: [specimen_type]")
    reconciled = env.run()  # no files: the decision alone updates the tracked status
    env.write("lab_a", "day2.csv", f"{LAB_A_HEADER},specimen_type\nP002,S101,CRP,1.7,mg/L,2026-01-11,plasma\n")
    after = env.run()

    assert reconciled.events == []
    assert after.events == []
    assert after.files_committed == 1
    assert _drift(env) == [("lab_a", "specimen_type", "IGNORED", 2, "lab_a_v1")]


def test_forbid_decision_quarantines_files_containing_the_column(env) -> None:
    _append_to_contract(env, "lab_a_v1.yaml", "forbidden_columns: [patient_name]")
    env.write("lab_a", "leaky.csv", f"{LAB_A_HEADER},patient_name\nP001,S100,CRP,3.2,mg/L,2026-01-10,Jane Doe\n")

    summary = env.run()

    assert summary.quarantined == [("leaky.csv", "F007")]
    assert [(e.event_type, e.code, e.severity) for e in summary.events] == [("FileQuarantined", "F007", "high")]
    assert env.scalar("SELECT count(*) FROM silver.measurement") == 0
    assert not list(env.layout.zone("bronze").rglob("*.csv"))
    assert _drift(env) == [("lab_a", "patient_name", "FORBIDDEN", 1, "lab_a_v1")]


def test_adopt_decision_maps_the_column_in_a_new_contract_version(env) -> None:
    env.write("ehr_vitals", "day1.csv", f"{EHR_HEADER},result_status\nP001,E300,HR,72,bpm,2026-01-10 09:30,F\n")
    first = env.run()
    assert [e.code for e in first.events] == ["F006"]

    # Adopt: v2 = v1 plus one mapping line, onto an existing canonical field.
    v1 = (env.config_dir / "sources" / "ehr_vitals_v1.yaml").read_text()
    v2 = v1.replace("contract_version: 1", "contract_version: 2").replace(
        "\nnatural_key:", "  result_status: {column: result_status, required: false, default: F}\n\nnatural_key:", 1
    )
    (env.config_dir / "sources" / "ehr_vitals_v2.yaml").write_text(v2)
    env.run()
    env.write("ehr_vitals", "day2.csv", f"{EHR_HEADER},result_status\nP002,E301,SYS_BP,120,mmHg,2026-01-11 10:15,F\n")
    after = env.run()

    assert after.events == []
    assert _drift(env) == [("ehr_vitals", "result_status", "ADOPTED", 1, "ehr_vitals_v2")]
    assert env.query("SELECT DISTINCT contract_version FROM silver.measurement WHERE source_encounter_id = 'E301'") == [
        ("ehr_vitals_v2",)
    ]


def test_a_column_cannot_be_both_mapped_and_ignored(tmp_path) -> None:
    contract = tmp_path / "bad.yaml"
    contract.write_text(
        "source: lab_a\ncontract_version: 9\ndomain: scientific\nfields:\n  source_value: value\n"
        "ignored_columns: [value]\n"
    )

    with pytest.raises(ConfigError, match="more than one"):
        parse_contract(contract)


@pytest.mark.parametrize(("extra", "expected", "code"), [
    ("", "ingest cleanly", 0),
    (",specimen_type", "SchemaDriftDetected F006", 2),
])
def test_check_file_predicts_the_outcome_without_ingesting(env, capsys, tmp_path, extra, expected, code) -> None:
    sample = tmp_path / "sample.csv"
    sample.write_text(f"{LAB_A_HEADER}{extra}\nP001,S100,CRP,3.2,mg/L,2026-01-10{',serum' if extra else ''}\n")

    exit_code = main(["--config-dir", str(env.config_dir), "check-file", "--source", "lab_a", str(sample)])

    assert exit_code == code
    assert expected in capsys.readouterr().out


def _write_lab_a_v2_renamed(env, extra: str = "") -> None:
    """A v2 for Lab A's renamed column, written from scratch WITHOUT v1's drift decisions."""
    (env.config_dir / "sources" / "lab_a_v2.yaml").write_text(
        "source: lab_a\ncontract_version: 2\ndomain: scientific\nformat: csv\nlayout: long\n"
        "sample_namespace: study_biobank\nfields:\n  source_subject_id: participant_id\n"
        "  source_sample_id: sample_id\n  source_analyte_code: test_name\n  source_value: value\n"
        "  source_unit: unit\n  observed_at: {column: collected_at, type: date, format: \"%Y-%m-%d\"}\n"
        f"{extra}natural_key: [source_sample_id, source_analyte_code]\n"
    )


def test_issue_10_forbidden_column_stays_forbidden_in_a_new_contract_version(env) -> None:
    _append_to_contract(env, "lab_a_v1.yaml", "forbidden_columns: [patient_name]")
    _write_lab_a_v2_renamed(env)  # forgets the forbidden_columns line
    env.write("lab_a", "v2_leaky.csv",
              "participant_id,sample_id,test_name,value,unit,collected_at,patient_name\n"
              "P001,S100,CRP,3.2,mg/L,2026-01-10,Jane Doe\n")

    summary = env.run()

    assert summary.quarantined == [("v2_leaky.csv", "F007")]
    assert env.scalar("SELECT count(*) FROM silver.measurement") == 0


def test_issue_10_ignored_column_stays_ignored_in_a_new_contract_version(env) -> None:
    _append_to_contract(env, "lab_a_v1.yaml", "ignored_columns: [specimen_type]")
    _write_lab_a_v2_renamed(env)  # forgets the ignored_columns line
    env.write("lab_a", "v2.csv",
              "participant_id,sample_id,test_name,value,unit,collected_at,specimen_type\n"
              "P001,S100,CRP,3.2,mg/L,2026-01-10,serum\n")

    summary = env.run()

    assert summary.files_committed == 1
    assert summary.events == []


def test_issue_10_mapping_a_forbidden_column_is_a_configuration_error(env) -> None:
    _append_to_contract(env, "lab_a_v1.yaml", "forbidden_columns: [patient_name]")
    _write_lab_a_v2_renamed(env, extra="  source_analyte_name: patient_name\n")

    with pytest.raises(ConfigError, match="forbids"):
        env.run()
