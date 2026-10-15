"""Seeded synthetic deliveries with ground truth (verification and benchmarking).

Generates the four brief formats over several delivery days, plus the exact gold rows and rejected rows
the pipeline must produce. Because the truth is known, tests can assert correctness end to end — real
data has no "right answer" to compare against.

Realism built in:
- each source numbers people its own way (linked only through a generated enrollment registry)
- CRP is sent in mixed units (mg/L, ug/L, mg/dL); EHR times are New York local time
- proteomics for a sample arrives one day before the Lab A file that links the sample to a person
- injected row errors, vendor corrections (C), retractions (W) and a duplicate delivery

Output layout:
    <out>/config/                     base config + generated registry + synthetic proteins
    <out>/deliveries/day=NNN/<source>/<file>.csv
    <out>/truth/expected_gold.csv     source, key, global_subject_id, analyte_code, value, unit, observed_at
    <out>/truth/expected_rejects.csv  source_file, source_row, error_code
    <out>/manifest.json               spec and counts
"""

import csv
import json
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EHR_TZ = ZoneInfo("America/New_York")
BASE_DATE = date(2026, 1, 1)
UNIT_FACTORS = {"mg/L": 1.0, "ug/L": 0.001, "mg/dL": 10.0}  # emitted unit → factor to mg/L
FIXED_PROTEINS = [("CRP", "P02741", "CRP"), ("TNF", "P01375", "TNF")]

LAB_A_COLUMNS = ["participant_id", "sample_id", "analyte", "value", "unit", "collected_at"]
LAB_B_COLUMNS = ["subject", "specimen", "test_code", "result", "unit", "collection_date"]
EHR_COLUMNS = ["patient_id", "encounter_id", "vital_type", "value", "unit", "recorded_at"]
OLINK_COLUMNS = ["SampleID", "AssayTarget", "UniProtID", "NPX_Value", "QC_Flag", "RunDate"]

ERROR_KINDS = ("R001", "R002", "R004", "R006")


@dataclass(frozen=True)
class SynthSpec:
    subjects: int = 100
    proteins: int = 20            # assays per proteomics sample (first two are CRP and TNF)
    days: int = 3
    error_rate: float = 0.05
    correction_rate: float = 0.05
    retraction_rate: float = 0.02
    seed: int = 42


@dataclass
class _Truth:
    gold: dict[tuple[str, str], tuple] = field(default_factory=dict)
    rejects: list[tuple[str, int, str]] = field(default_factory=list)
    linked_samples: set[str] = field(default_factory=set)


def _day(d: int) -> date:
    return BASE_DATE + timedelta(days=d)


def _num(value: float) -> str:
    return f"{value:.6g}"


class _Writer:
    """Collects rows per delivery file and records row numbers for expected rejects."""

    def __init__(self, out: Path):
        self.out = out
        self.files: dict[Path, tuple[list[str], list[list[str]]]] = {}

    def add(self, day: int, source: str, name: str, columns: list[str], row: list[str]) -> tuple[str, int]:
        path = self.out / "deliveries" / f"day={day:03d}" / source / name
        header, rows = self.files.setdefault(path, (columns, []))
        rows.append(row)
        return name, len(rows)

    def flush(self) -> None:
        for path, (header, rows) in self.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(header)
                writer.writerows(rows)


def _corrupt(rng: random.Random, row: list[str], value_idx: int, unit_idx: int | None, subject_idx: int,
             date_idx: int | None, future: str) -> tuple[list[str], str]:
    kinds = [k for k in ERROR_KINDS if not (k == "R006" and unit_idx is None) and not (k == "R004" and date_idx is None)]
    kind = rng.choice(kinds)
    row = list(row)
    if kind == "R001":
        row[subject_idx] = ""
    elif kind == "R002":
        row[value_idx] = "abc"
    elif kind == "R004":
        row[date_idx] = future
    else:
        row[unit_idx] = "xyz"
    return row, kind


def generate(spec: SynthSpec, out: Path, base_config: Path) -> dict:
    rng = random.Random(spec.seed)
    if out.exists():
        shutil.rmtree(out)
    writer = _Writer(out)
    truth = _Truth()
    subjects = [i + 1 for i in range(spec.subjects)]
    day_of = {i: (i - 1) % spec.days + 1 for i in subjects}
    lab_a_rows: dict[int, tuple] = {}
    proteins = FIXED_PROTEINS + [(f"SYNPROT_{k:05d}", f"SYN{k:05d}", f"SYNP{k}") for k in range(1, max(spec.proteins - 1, 1))]
    proteins = proteins[: max(spec.proteins, 2)]

    def err() -> bool:
        return rng.random() < spec.error_rate

    for i in subjects:
        d = day_of[i]
        gid, collected = f"G-{i:06d}", _day(d)

        # EHR vitals: local New York time, stored as UTC.
        for vital, code, unit, low, high in (("HR", "HEART_RATE", "bpm", 55, 95), ("SYS_BP", "SYSTOLIC_BP", "mmHg", 100, 140)):
            local = datetime.combine(collected, datetime.min.time()) + timedelta(minutes=rng.randint(7 * 60, 18 * 60))
            value = rng.randint(low, high)
            row = [f"PT{i:06d}", f"E{i:06d}", vital, str(value), unit, local.strftime("%Y-%m-%d %H:%M")]
            if err():
                row, kind = _corrupt(rng, row, 3, 4, 0, 5, "2099-01-01 10:00")
                truth.rejects.append((*writer.add(d, "ehr_vitals", f"ehr_vitals_day{d:03d}.csv", EHR_COLUMNS, row), kind))
                continue
            writer.add(d, "ehr_vitals", f"ehr_vitals_day{d:03d}.csv", EHR_COLUMNS, row)
            utc = local.replace(tzinfo=EHR_TZ).astimezone(timezone.utc).replace(tzinfo=None)
            truth.gold[("ehr_vitals", f"E{i:06d}|{vital}")] = (gid, code, float(value), unit, utc.isoformat())

        # Lab A CRP on the biobank sample, delivered the day AFTER collection (so proteomics arrives first).
        unit = rng.choice(list(UNIT_FACTORS))
        mg_l = rng.uniform(0.3, 15.0)
        emitted = _num(mg_l / UNIT_FACTORS[unit])
        row = [f"LA{i:06d}", f"BS{i:06d}", "CRP", emitted, unit, collected.isoformat()]
        if err():
            row, kind = _corrupt(rng, row, 3, 4, 0, 5, "2099-01-01")
            truth.rejects.append((*writer.add(d + 1, "lab_a", f"lab_a_day{d + 1:03d}.csv", LAB_A_COLUMNS, row), kind))
        else:
            writer.add(d + 1, "lab_a", f"lab_a_day{d + 1:03d}.csv", LAB_A_COLUMNS, row)
            truth.gold[("lab_a", f"BS{i:06d}|CRP")] = (
                gid, "CRP", float(emitted) * UNIT_FACTORS[unit], "mg/L", datetime.combine(collected, datetime.min.time()).isoformat())
            truth.linked_samples.add(f"BS{i:06d}")
            lab_a_rows[i] = (unit, collected)

        # Lab B hs-CRP on its own specimen, same day.
        unit = rng.choice(list(UNIT_FACTORS))
        emitted = _num(rng.uniform(0.3, 15.0) / UNIT_FACTORS[unit])
        row = [f"LB{i:06d}", f"LBS{i:06d}", "HSCRP", emitted, unit, collected.isoformat()]
        if err():
            row, kind = _corrupt(rng, row, 3, 4, 0, 5, "2099-01-01")
            truth.rejects.append((*writer.add(d, "lab_b", f"lab_b_day{d:03d}.csv", LAB_B_COLUMNS, row), kind))
        else:
            writer.add(d, "lab_b", f"lab_b_day{d:03d}.csv", LAB_B_COLUMNS, row)
            truth.gold[("lab_b", f"LBS{i:06d}|HSCRP")] = (
                gid, "CRP", float(emitted) * UNIT_FACTORS[unit], "mg/L", datetime.combine(collected, datetime.min.time()).isoformat())

    # Proteomics: run on the biobank samples; dated by Lab A collection once linked.
    for i in subjects:
        d = day_of[i]
        for name, uniprot, analyte in proteins:
            npx = _num(rng.gauss(5.0, 2.0))
            qc = "WARN" if rng.random() < 0.05 else "PASS"
            row = [f"BS{i:06d}", name, uniprot, npx, qc, (_day(d) + timedelta(days=30)).isoformat()]
            if err():
                row, kind = _corrupt(rng, row, 3, None, 0, None, "")
                truth.rejects.append((*writer.add(d, "olink", f"olink_day{d:03d}.csv", OLINK_COLUMNS, row), kind))
                continue
            writer.add(d, "olink", f"olink_day{d:03d}.csv", OLINK_COLUMNS, row)
            if f"BS{i:06d}" in truth.linked_samples:
                truth.gold[("olink", f"BS{i:06d}|{uniprot}")] = (
                    f"G-{i:06d}", analyte, float(npx), "NPX", datetime.combine(_day(d), datetime.min.time()).isoformat())

    # Last day: vendor corrections and retractions for Lab A, plus a duplicate delivery.
    last = spec.days + 2
    columns = LAB_A_COLUMNS + ["result_status"]
    for i, (unit, collected) in lab_a_rows.items():
        roll = rng.random()
        key = ("lab_a", f"BS{i:06d}|CRP")
        if roll < spec.correction_rate:
            emitted = _num(rng.uniform(0.3, 15.0))
            writer.add(last, "lab_a", "lab_a_corrections.csv", columns,
                       [f"LA{i:06d}", f"BS{i:06d}", "CRP", emitted, "mg/L", collected.isoformat(), "C"])
            truth.gold[key] = (*truth.gold[key][:2], float(emitted), "mg/L", truth.gold[key][4])
        elif roll < spec.correction_rate + spec.retraction_rate:
            writer.add(last, "lab_a", "lab_a_corrections.csv", columns,
                       [f"LA{i:06d}", f"BS{i:06d}", "CRP", "0", "mg/L", collected.isoformat(), "W"])
            del truth.gold[key]
    writer.flush()
    first_ehr = out / "deliveries" / "day=001" / "ehr_vitals" / "ehr_vitals_day001.csv"
    if first_ehr.exists():
        duplicate = out / "deliveries" / f"day={last:03d}" / "ehr_vitals" / "ehr_vitals_resent.csv"
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(first_ehr, duplicate)

    _write_config(out / "config", base_config, subjects, proteins[len(FIXED_PROTEINS):])
    _write_truth(out / "truth", truth)
    manifest = {
        "spec": asdict(spec),
        "delivery_days": sorted(p.name for p in (out / "deliveries").iterdir()),
        "rows_delivered": sum(len(rows) for _, rows in writer.files.values()),
        "expected_gold_rows": len(truth.gold),
        "expected_rejects": len(truth.rejects),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _write_config(config: Path, base: Path, subjects: list[int], synthetic: list[tuple[str, str, str]]) -> None:
    shutil.copytree(base, config)
    with (config / "registry" / "enrollment.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["study_subject_id", "source", "source_subject_id"])
        for i in subjects:
            for source, prefix in (("ehr_vitals", "PT"), ("lab_a", "LA"), ("lab_b", "LB")):
                writer.writerow([f"G-{i:06d}", source, f"{prefix}{i:06d}"])
    with (config / "reference" / "analytes.csv").open("a") as analytes, \
            (config / "reference" / "analyte_aliases.csv").open("a") as aliases:
        for name, uniprot, analyte in synthetic:
            analytes.write(f"{analyte},Synthetic protein {name},{uniprot}\n")
            aliases.write(f"olink,{uniprot},{analyte},olink_npx,relative_log2,NPX\n")


def _write_truth(folder: Path, truth: _Truth) -> None:
    folder.mkdir(parents=True)
    with (folder / "expected_gold.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["source", "key", "global_subject_id", "analyte_code", "value", "unit", "observed_at"])
        for (source, key), (gid, analyte, value, unit, observed) in sorted(truth.gold.items()):
            writer.writerow([source, key, gid, analyte, repr(value), unit, observed])
    with (folder / "expected_rejects.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["source_file", "source_row", "error_code"])
        writer.writerows(sorted(truth.rejects))


def delivery_days(out: Path) -> list[Path]:
    return sorted((out / "deliveries").iterdir())


def deliver(day_dir: Path, landing: Path) -> list[Path]:
    """Copy one day's files into landing/<source>/ in a fixed order (simulates the vendors' uploads)."""
    delivered = []
    for path in sorted(day_dir.glob("*/*.csv")):
        target = landing / path.parent.name / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, target)
        delivered.append(target)
    return delivered


def verify(warehouse: Path, out: Path) -> dict:
    """Compare gold and open DLQ rows with the generator's truth. Returns counts and example differences."""
    import duckdb

    with (out / "truth" / "expected_gold.csv").open() as handle:
        expected = {(r["source"], r["key"]): r for r in csv.DictReader(handle)}
    with (out / "truth" / "expected_rejects.csv").open() as handle:
        expected_rejects = {(r["source_file"], int(r["source_row"]), r["error_code"]) for r in csv.DictReader(handle)}

    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        actual = {
            (source, key): (gid, analyte, value, unit, observed)
            for source, key, gid, analyte, value, unit, observed in con.execute("""
                SELECT g.source,
                       CASE WHEN g.source = 'ehr_vitals' THEN s.source_encounter_id ELSE s.source_sample_id END
                           || '|' || s.source_analyte_code,
                       g.global_subject_id, g.analyte_code, g.value, g.unit, g.observed_at
                FROM gold.measurement_current g JOIN silver.measurement s USING (record_id, version)
            """).fetchall()
        }
        actual_rejects = {tuple(r) for r in con.execute(
            "SELECT source_file, source_row, error_code FROM meta.dlq WHERE status = 'OPEN'").fetchall()}
    finally:
        con.close()

    mismatched = []
    for key in expected.keys() & actual.keys():
        want, (gid, analyte, value, unit, observed) = expected[key], actual[key]
        same = (
            want["global_subject_id"] == gid and want["analyte_code"] == analyte and want["unit"] == unit
            and abs(float(want["value"]) - value) <= 1e-9 * max(1.0, abs(value))
            and want["observed_at"] == observed.isoformat()
        )
        if not same:
            mismatched.append({"key": key, "expected": want, "actual": actual[key]})
    report = {
        "gold_expected": len(expected),
        "gold_actual": len(actual),
        "missing": sorted(expected.keys() - actual.keys())[:5],
        "unexpected": sorted(actual.keys() - expected.keys())[:5],
        "mismatched": mismatched[:5],
        "rejects_expected": len(expected_rejects),
        "rejects_actual": len(actual_rejects),
        "rejects_missing": sorted(expected_rejects - actual_rejects)[:5],
        "rejects_unexpected": sorted(actual_rejects - expected_rejects)[:5],
    }
    report["ok"] = not (report["missing"] or report["unexpected"] or report["mismatched"]
                        or report["rejects_missing"] or report["rejects_unexpected"])
    return report
