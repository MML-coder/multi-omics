"""Unified access layer: harmonized data and lineage over HTTP."""

import pytest
from fastapi.testclient import TestClient

from omics_pipeline.api import create_app


@pytest.fixture
def client(env) -> TestClient:
    env.drop_brief_samples()
    env.run()
    return TestClient(create_app(env.layout))


def test_health_reports_gold_count(client) -> None:
    body = client.get("/health").json()

    assert body["measurements"] == 8
    assert body["last_run"]["status"] == "completed"


def test_measurements_filter_by_subject_and_analyte(client) -> None:
    rows = client.get("/measurements", params={"subject_id": "G-0001", "analyte_code": "CRP"}).json()

    assert sorted((r["source"], r["value"], r["unit"], r["measurement_scale"]) for r in rows) == [
        ("lab_a", 3.2, "mg/L", "absolute"),
        ("lab_b", 3.2, "mg/L", "absolute"),
        ("olink", 10.4, "NPX", "relative_log2"),
    ]


def test_page_size_is_bounded(client) -> None:
    assert client.get("/measurements", params={"limit": 5000}).status_code == 422


def test_lineage_explains_lab_b_value(client, env) -> None:
    record_id = env.scalar("SELECT record_id FROM silver.measurement WHERE source = 'lab_b' AND source_subject_id = 'P001'")

    body = client.get(f"/measurements/{record_id}/lineage").json()

    steps = {s["step"]: s for s in body["steps"]}
    assert steps["receive"]["source_file"] == "lab_b_sample.csv"
    assert steps["receive"]["source_row"] == 1
    assert steps["map_analyte"]["from"] == "HSCRP"
    assert steps["convert_unit"] == {
        "step": "convert_unit", "from": "3200 ug/L", "factor": 0.001, "to": "3.2 mg/L",
        "reference_version": steps["convert_unit"]["reference_version"],
    }
    assert steps["resolve_identity"]["method"] == "enrollment_registry"
    assert [h["change_reason"] for h in body["history"]] == ["NEW"]


def test_lineage_of_olink_value_shows_sample_link_evidence(client, env) -> None:
    record_id = env.scalar("SELECT record_id FROM silver.measurement WHERE source = 'olink' AND source_sample_id = 'S100'")

    identity = next(s for s in client.get(f"/measurements/{record_id}/lineage").json()["steps"]
                    if s["step"] == "resolve_identity")

    assert identity["method"] == "sample_link"
    assert identity["global_subject_id"] == "G-0001"
    assert identity["evidence"]["source"] == "lab_a"


def test_unknown_record_is_404(client) -> None:
    assert client.get("/measurements/nope/lineage").status_code == 404
