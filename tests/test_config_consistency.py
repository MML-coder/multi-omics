"""Static checks that configuration, reference data and fixtures agree with each other."""

import csv
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
FIXTURES = ROOT / "tests" / "fixtures"

CONTRACTS = sorted((CONFIG / "sources").glob("*.yaml"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _header(path: Path) -> list[str]:
    with path.open(newline="") as handle:
        return next(csv.reader(handle))


def _required_columns(contract: dict) -> set[str]:
    columns = set()
    for spec in contract["fields"].values():
        if isinstance(spec, str):
            columns.add(spec)
        elif spec.get("required", True):
            columns.add(spec["column"])
    return columns


def test_every_brief_source_has_a_contract() -> None:
    sources = {yaml.safe_load(p.read_text())["source"] for p in CONTRACTS}
    assert sources == {"ehr_vitals", "lab_a", "lab_b", "olink"}


@pytest.mark.parametrize("contract_path", CONTRACTS, ids=lambda p: p.stem)
def test_sample_fixture_has_contract_columns(contract_path: Path) -> None:
    contract = yaml.safe_load(contract_path.read_text())
    sample = FIXTURES / f"{contract['source']}_sample.csv"

    missing = _required_columns(contract) - set(_header(sample))

    assert not missing, f"{sample.name} lacks {missing}"


@pytest.mark.parametrize("contract_path", CONTRACTS, ids=lambda p: p.stem)
def test_natural_key_uses_mapped_fields(contract_path: Path) -> None:
    contract = yaml.safe_load(contract_path.read_text())
    assert set(contract["natural_key"]) <= set(contract["fields"])


def test_aliases_point_to_known_analytes_and_convertible_units() -> None:
    analytes = {row["analyte_code"] for row in _read_csv(CONFIG / "reference" / "analytes.csv")}
    identity_units = {
        row["to_unit"]
        for row in _read_csv(CONFIG / "reference" / "unit_conversions.csv")
        if row["from_unit"] == row["to_unit"]
    }

    for alias in _read_csv(CONFIG / "reference" / "analyte_aliases.csv"):
        assert alias["analyte_code"] in analytes, alias
        assert alias["canonical_unit"] in identity_units, alias


def test_every_sample_fixture_code_has_an_alias() -> None:
    aliases = {(r["source"], r["source_code"]) for r in _read_csv(CONFIG / "reference" / "analyte_aliases.csv")}
    for contract_path in CONTRACTS:
        contract = yaml.safe_load(contract_path.read_text())
        spec = contract["fields"]["source_analyte_code"]
        column = spec if isinstance(spec, str) else spec["column"]
        for row in _read_csv(FIXTURES / f"{contract['source']}_sample.csv"):
            assert (contract["source"], row[column]) in aliases, (contract["source"], row[column])


def test_registry_maps_each_source_id_once() -> None:
    rows = _read_csv(CONFIG / "registry" / "enrollment.csv")
    keys = [(r["source"], r["source_subject_id"]) for r in rows]
    assert len(keys) == len(set(keys))
