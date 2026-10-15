#!/usr/bin/env python3
"""The onsite demo — and an end-to-end acceptance test.

Plays one story from an empty folder: vendors deliver, data is harmonized, things go wrong in realistic
ways, and the pipeline handles each one. Every step prints the command it runs and its output, then
checks the result in the database. Any failed check stops the demo.

    python demo/run_demo.py            # run straight through (CI)
    python demo/run_demo.py --pause    # wait for Enter between steps (live presentation)

Uses data/demo and a private config copy in data/demo-config; the repository's config/ is never changed.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("tests/fixtures")
DATA = Path("data/demo")
CONFIG = Path("data/demo-config")
TRUTH = Path("data/demo-truth")
SCALE = Path("data/demo-scale")


class CheckFailed(Exception):
    pass


class Demo:
    def __init__(self, pause: bool, data: Path = DATA, config: Path = CONFIG):
        self.pause = pause
        self.number = 0
        self.data = data
        self.config = config

    # ------------------------------------------------------------------------------------------ output

    def step(self, title: str, why: str) -> None:
        self.number += 1
        if self.pause and self.number > 1 and sys.stdin.isatty():
            input("\n[Enter for the next step] ")
        print(f"\n{'=' * 100}\nSTEP {self.number}: {title}\n  {why}\n{'=' * 100}")

    def note(self, text: str) -> None:
        print(f"  → {text}")

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            print(f"  ✘ {message}")
            raise CheckFailed(message)
        print(f"  ✔ {message}")

    # ---------------------------------------------------------------------------------------- commands

    def pipeline(self, *args: str, expect: tuple[int, ...] = (0,), data: Path | None = None,
                 config: Path | None = None) -> str:
        data, config = data or self.data, config or self.config
        prefix = "" if (data, config) == (self.data, self.config) else f"PIPELINE_DATA_DIR={data} PIPELINE_CONFIG_DIR={config} "
        print(f"\n$ {prefix}pipeline {' '.join(args)}")
        env = {**os.environ, "PIPELINE_DATA_DIR": str(data), "PIPELINE_CONFIG_DIR": str(config)}
        result = subprocess.run(
            [sys.executable, "-m", "omics_pipeline.cli", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        out = result.stdout.rstrip()
        if out:
            print(out)
        errors = [line for line in result.stderr.splitlines() if not line.startswith(("[pipeline]", "WARNING"))]
        if errors:
            print("\n".join(errors))
        self.check(result.returncode in expect, f"exit code {result.returncode} (expected {' or '.join(map(str, expect))})")
        return out

    def shell(self, display: str, action) -> None:
        print(f"\n$ {display}")
        action()

    def deliver(self, source: str, fixture: str, as_name: str | None = None) -> None:
        target = self.data / "landing" / source / (as_name or fixture)
        self.shell(f"cp {FIXTURES / fixture} {target}", lambda: (
            (ROOT / target).parent.mkdir(parents=True, exist_ok=True),
            shutil.copy(ROOT / FIXTURES / fixture, ROOT / target),
        ))

    def query(self, sql: str, params: list | None = None, warehouse: Path | None = None) -> list[tuple]:
        con = duckdb.connect(str(ROOT / (warehouse or self.data) / "warehouse.duckdb"), read_only=True)
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def show(self, sql: str, params: list | None = None) -> None:
        con = duckdb.connect(str(ROOT / self.data / "warehouse.duckdb"), read_only=True)
        try:
            print(con.sql(sql, params=params or None))
        finally:
            con.close()

    def gold_fingerprint(self) -> str:
        rows = self.query(
            "SELECT record_id, value, global_subject_id FROM gold.measurement_current ORDER BY record_id"
        )
        return hashlib.sha256(repr(rows).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------------------------- story


def story(d: Demo) -> None:
    d.step("Start from nothing", "Empty data folder and a private copy of the configuration.")
    for path in (DATA, CONFIG, TRUTH, SCALE):
        shutil.rmtree(ROOT / path, ignore_errors=True)
    d.shell(f"cp -R config {CONFIG}", lambda: shutil.copytree(ROOT / "config", ROOT / CONFIG))
    d.pipeline("init")

    d.step("Three vendors deliver; proteomics arrives before Lab A",
           "Proteomics has only sample IDs. Without Lab A's file we cannot know whose samples they are.")
    d.deliver("ehr_vitals", "ehr_vitals_sample.csv")
    d.deliver("lab_b", "lab_b_sample.csv")
    d.deliver("olink", "olink_sample.csv")
    d.pipeline("run")
    d.show("SELECT source, source_sample_id, global_subject_id, resolution_status, hold_reason "
           "FROM silver.measurement WHERE source = 'olink'")
    d.check(d.query("SELECT count(*) FROM silver.measurement WHERE source = 'olink' AND hold_reason = 'H002'")[0][0] == 2,
            "both proteomics rows are held (H002: sample not linked yet), not guessed")
    d.check(d.query("SELECT count(*) FROM gold.measurement_current")[0][0] == 4, "gold has the 4 resolved rows only")

    d.step("Lab A arrives; held proteomics resolves automatically",
           "Lab A links sample S100 to person P001. Same person, three vendors, one canonical view.")
    d.deliver("lab_a", "lab_a_sample.csv")
    d.pipeline("run")
    d.show("SELECT source, assay, value, unit, measurement_scale, observed_at::DATE AS observed "
           "FROM gold.measurement_current WHERE global_subject_id = 'G-0001' AND analyte_code = 'CRP' ORDER BY source")
    d.check(d.query("SELECT count(*) FROM gold.measurement_current")[0][0] == 8, "all 8 brief rows are in gold")
    d.check(d.query("SELECT g.value, g.observed_at::DATE::VARCHAR FROM gold.measurement_current g "
                    "JOIN silver.measurement s USING (record_id, version) WHERE s.source = 'lab_b' AND s.source_subject_id = 'P001'")
            == [(3.2, "2026-03-01")], "Lab B's 3200 ug/L became 3.2 mg/L")
    d.check(d.query("SELECT g.observed_at::DATE::VARCHAR FROM gold.measurement_current g JOIN silver.measurement s "
                    "USING (record_id, version) WHERE s.source = 'olink' AND s.source_sample_id = 'S100'")
            == [("2026-01-10",)], "proteomics is dated by the blood draw (Jan 10), not the assay run (Mar 5)")

    d.step("Lineage: where did this number come from?", "Any value traces back to its file, row and every rule applied.")
    record_id = d.query("SELECT record_id FROM silver.measurement WHERE source = 'lab_b' AND source_subject_id = 'P001'")[0][0]
    out = d.pipeline("lineage", record_id)
    d.check('"from": "3200 ug/L"' in out and '"factor": 0.001' in out, "lineage shows 3200 ug/L x 0.001 = 3.2 mg/L")
    d.check('"source_row": 1' in out and "lab_b_sample.csv" in out, "lineage points to lab_b_sample.csv, row 1")

    d.step("Idempotency: the same file arrives again", "Vendors resend files. Nothing may change.")
    before = d.gold_fingerprint()
    d.deliver("lab_a", "lab_a_sample.csv", as_name="lab_a_resent.csv")
    out = d.pipeline("run")
    d.check("duplicate=1" in out, "the resent file is recognized by its fingerprint and skipped")
    d.check(d.gold_fingerprint() == before, "gold is unchanged")

    d.step("Corrections and retractions", "Lab A corrects S100 (status C) and withdraws S101 (status W).")
    d.deliver("lab_a", "lab_a_correction.csv")
    d.deliver("lab_a", "lab_a_retraction.csv")
    d.pipeline("run")
    d.show("SELECT source_sample_id, version, change_reason, value, result_status FROM silver.measurement "
           "WHERE source = 'lab_a' AND source_sample_id IN ('S100', 'S101') ORDER BY 1, 2")
    d.check(d.query("SELECT g.value FROM gold.measurement_current g JOIN silver.measurement s USING (record_id, version) "
                    "WHERE s.source = 'lab_a' AND s.source_sample_id = 'S100'") == [(2.3,)],
            "S100 now shows the corrected value 2.3; 3.2 is kept in history")
    d.check(d.query("SELECT count(*) FROM gold.measurement_current g JOIN silver.measurement s USING (record_id, version) "
                    "WHERE s.source = 'lab_a' AND s.source_sample_id = 'S101'")[0][0] == 0,
            "S101 is withdrawn from gold, history kept")

    d.step("Validation, dead-letter queue, and replay after a fix",
           "Lab B writes 'mg/dl' — a spelling we don't know. We never guess units.")
    d.deliver("lab_b", "lab_b_unknown_unit.csv")
    d.pipeline("run")
    d.pipeline("dlq", "list")
    d.check(d.query("SELECT error_code, raw_value FROM meta.dlq WHERE status = 'OPEN'") == [("R006", "mg/dl")],
            "the row is rejected with R006 and kept with its raw data")
    d.shell(f"echo 'mg/dl,mg/dL' >> {CONFIG}/reference/unit_aliases.csv",
            lambda: (ROOT / CONFIG / "reference" / "unit_aliases.csv").open("a").write("mg/dl,mg/dL\n"))
    d.note("in production this one line is a reviewed pull request")
    d.pipeline("dlq", "replay", "--error", "R006")
    d.check(d.query("SELECT count(*) FROM meta.dlq WHERE status = 'OPEN'")[0][0] == 0, "the DLQ row is resolved")
    d.check(d.query("SELECT value, unit FROM silver.measurement WHERE source_sample_id = 'S210'") == [(3.2, "mg/L")],
            "0.32 mg/dl is now loaded as 3.2 mg/L")

    d.step("Schema drift: Lab A adds a column", "Data keeps flowing; we are told; a person decides.")
    d.deliver("lab_a", "lab_a_new_column.csv")
    d.pipeline("run", expect=(2,))
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE column_name = 'specimen_type'") == [("OPEN",)],
            "specimen_type is tracked as OPEN (undecided); its values are kept in bronze")
    d.shell(f"echo 'ignored_columns: [specimen_type]' >> {CONFIG}/sources/lab_a_v1.yaml",
            lambda: (ROOT / CONFIG / "sources" / "lab_a_v1.yaml").open("a").write("\nignored_columns: [specimen_type]\n"))
    d.note("decision: not needed — recorded in the contract, reviewed like code")
    d.pipeline("run")
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE column_name = 'specimen_type'") == [("IGNORED",)],
            "the decision is reflected without anyone editing the tracking table")

    d.step("Schema drift: a column we must never receive", "Lab A accidentally sends patient names.")
    d.shell(f"echo 'forbidden_columns: [patient_name]' >> {CONFIG}/sources/lab_a_v1.yaml",
            lambda: (ROOT / CONFIG / "sources" / "lab_a_v1.yaml").open("a").write("forbidden_columns: [patient_name]\n"))
    d.deliver("lab_a", "lab_a_forbidden_column.csv")
    d.pipeline("run", expect=(2,))
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE original_name = 'lab_a_forbidden_column.csv'")
            == [("F007",)], "the file is quarantined (F007) — high severity")
    d.check(d.query("SELECT count(*) FROM silver.measurement WHERE source_sample_id = 'S106'")[0][0] == 0,
            "nothing from it was loaded")

    d.step("Breaking change: Lab B renames its columns", "A required column is missing, so the file cannot be read safely.")
    d.deliver("lab_b", "lab_b_v2_renamed.csv")
    d.pipeline("run", expect=(2,))
    d.pipeline("quarantine", "list")
    file_hash, quarantine_path = d.query(
        "SELECT file_hash, quarantine_path FROM meta.ingested_files WHERE original_name = 'lab_b_v2_renamed.csv'")[0]
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE file_hash = ?", [file_hash]) == [("F004",)],
            "quarantined (F004) with the expected vs received columns")
    d.shell(f"cp {FIXTURES}/contracts/lab_b_v2.yaml {CONFIG}/sources/",
            lambda: shutil.copy(ROOT / FIXTURES / "contracts" / "lab_b_v2.yaml", ROOT / CONFIG / "sources"))
    d.note("the fix: contract v2 maps the new names (a reviewed pull request in production)")
    d.pipeline("check-file", "--source", "lab_b", quarantine_path)
    d.pipeline("quarantine", "release", file_hash[:12])
    d.pipeline("run")
    d.check(d.query("SELECT DISTINCT contract_version FROM silver.measurement WHERE source_sample_id IN ('S202', 'S203')")
            == [("lab_b_v2",)], "the file is loaded under lab_b_v2; old-format files still use lab_b_v1")

    d.step("Silent unit change", "Lab B sends values already in mg/L but still labels them ug/L. The file looks perfect.")
    d.deliver("lab_b", "lab_b_silent_unit_change.csv")
    d.pipeline("run", expect=(2,))
    d.check(d.query("SELECT code FROM meta.events WHERE event_type = 'DistributionShift'") == [("B001",)],
            "only the values reveal it: distribution-shift alert B001")

    d.step("Fault tolerance: a crash in the middle of writing a file",
           "Proven by a test that injects the crash — nothing half-written, other files commit, rerun recovers.")
    print("\n$ pytest -q tests/test_fault_tolerance.py")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "tests/test_fault_tolerance.py"],
                            cwd=ROOT, capture_output=True, text=True)
    print(result.stdout.strip().splitlines()[-1])
    d.check(result.returncode == 0, "fault-tolerance tests pass")

    d.step("Disaster recovery: rebuild everything from the raw files", "Delete the database; replay bronze in arrival order.")
    before = d.gold_fingerprint()
    d.shell(f"rm {DATA}/warehouse.duckdb", lambda: (ROOT / DATA / "warehouse.duckdb").unlink())
    d.pipeline("replay", expect=(0, 2))
    d.check(d.gold_fingerprint() == before, "gold rebuilt from bronze is identical")

    d.step("Correctness at scale: compare with a known answer",
           "Generate 200 people over several days with errors, corrections, retractions and a duplicate.")
    d.pipeline("generate", "--out", str(TRUTH), "--subjects", "200", "--proteins", "10", "--days", "4", "--seed", "7")
    days = sorted(p.name for p in (ROOT / TRUTH / "deliveries").iterdir())
    for day in days:
        d.pipeline("deliver", "--from", str(TRUTH), "--day", day.split("=")[1], data=SCALE, config=TRUTH / "config")
        d.pipeline("run", data=SCALE, config=TRUTH / "config")
    out = d.pipeline("verify", "--from", str(TRUTH), data=SCALE, config=TRUTH / "config")
    d.check("MATCHES GROUND TRUTH" in out, "every gold value and every rejected row matches the answer key")

    d.step("Where we ended up", "Every run, every event, every decision is recorded.")
    d.pipeline("status", "--limit", "20")
    d.pipeline("events", "--limit", "10")
    print(f"\nDEMO COMPLETE: {d.number} steps, all checks passed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    demo = Demo(parser.parse_args().pause)
    try:
        story(demo)
    except CheckFailed as exc:
        print(f"\nDEMO STOPPED at step {demo.number}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
