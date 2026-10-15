#!/usr/bin/env python3
"""Demo v2 — one week of deliveries: 5 labs, proteomics, and EHR vitals.

Day by day, like production. New labs are onboarded with configuration only, and the week brings the
problems real feeds bring: late proteomics, resends, new columns, renamed and missing columns, bad unit
spellings, unregistered people, corrections, retractions, an identity conflict, personal data that must
not arrive, and a silent unit change. Every step is checked; any failed check stops the demo.

    python demo/run_week_demo.py                 # straight through (CI)
    python demo/run_week_demo.py --pause         # wait for Enter between steps (live)
    python demo/run_week_demo.py --from-day 4    # run days 1-3 quietly, present from day 4

All input files are readable under demo/week/. Uses data/week and a private config copy in
data/week-config; the repository's config/ is never changed.
"""

import argparse
import contextlib
import io
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_demo import ROOT, CheckFailed, Demo  # noqa: E402

WEEK = Path("demo/week")
DATA = Path("data/week")
CONFIG = Path("data/week-config")


# ------------------------------------------------------------------------------------------- helpers


def deliver_day(d: Demo, folder: str) -> None:
    """Copy every file of one delivery folder into landing/<source>/ (sorted, like arrivals)."""
    for path in sorted((ROOT / WEEK / folder).glob("*/*.csv")):
        source = path.parent.name
        target = DATA / "landing" / source / path.name
        d.shell(f"cp {WEEK / folder / source / path.name} {target}", lambda p=path, t=target: (
            (ROOT / t).parent.mkdir(parents=True, exist_ok=True), shutil.copy(p, ROOT / t)))


def append(d: Demo, source_file: str, target: str, note: str = "") -> None:
    """Append onboarding/decision lines to the private config (a reviewed pull request in production)."""
    src, dst = ROOT / WEEK / "onboarding" / source_file, ROOT / CONFIG / target
    d.shell(f"cat {WEEK}/onboarding/{source_file} >> {CONFIG}/{target}", lambda: dst.open("a").write(src.read_text()))
    if note:
        d.note(note)


def add_line(d: Demo, line: str, target: str, note: str = "") -> None:
    dst = ROOT / CONFIG / target
    d.shell(f"echo '{line}' >> {CONFIG}/{target}", lambda: dst.open("a").write(("\n" if target.endswith(".yaml") else "") + line + "\n"))
    if note:
        d.note(note)


def copy_contract(d: Demo, source: Path, note: str = "") -> None:
    d.shell(f"cp {source} {CONFIG}/sources/", lambda: shutil.copy(ROOT / source, ROOT / CONFIG / "sources"))
    if note:
        d.note(note)


def gold_value(d: Demo, source: str, sample: str) -> list[tuple]:
    return d.query(
        "SELECT round(g.value, 6), g.unit, g.global_subject_id FROM gold.measurement_current g "
        "JOIN silver.measurement s USING (record_id, version) WHERE s.source = ? AND s.source_sample_id = ?",
        [source, sample])


def latest(d: Demo, source: str, sample: str, analyte: str | None = None) -> tuple:
    sql = ("SELECT resolution_status, hold_reason, global_subject_id, change_reason FROM silver.measurement "
           "WHERE source = ? AND source_sample_id = ?" + (" AND source_analyte_code = ?" if analyte else "") +
           " ORDER BY version DESC LIMIT 1")
    return d.query(sql, [source, sample] + ([analyte] if analyte else []))[0]


# --------------------------------------------------------------------------------------------- story


def setup(d: Demo) -> None:
    d.step("Setup: start from nothing; onboard Lab C and Lab D by configuration only",
           "Two new vendors: a contract each, their test names, and who their patients are. No code changes.")
    for path in (DATA, CONFIG):
        shutil.rmtree(ROOT / path, ignore_errors=True)
    d.shell(f"cp -R config {CONFIG}", lambda: shutil.copytree(ROOT / "config", ROOT / CONFIG))
    copy_contract(d, WEEK / "onboarding/lab_c_v1.yaml")
    copy_contract(d, WEEK / "onboarding/lab_d_v1.yaml")
    append(d, "aliases_labs_c_d.csv", "reference/analyte_aliases.csv", "their test names map to our CRP")
    append(d, "registry_labs_c_d.csv", "registry/enrollment.csv", "their patient IDs map to study people")
    append(d, "registry_cohort.csv", "registry/enrollment.csv", "the study cohort: 446 enrolled participants")
    append(d, "protein_panel_analytes.csv", "reference/analytes.csv")
    append(d, "protein_panel_aliases.csv", "reference/analyte_aliases.csv", "the proteomics panel: 50 proteins")
    d.pipeline("check-file", "--source", "lab_c", str(WEEK / "day1/lab_c/lab_c_day1.csv"))
    d.pipeline("init")


def day1(d: Demo) -> None:
    d.step("DAY 1: six deliveries — EHR, Labs A-D, and the weekly proteomics batch (15,000 rows)",
           "Lab C reports mg/dL, Lab D writes dates as month/day/year. Half the proteomics samples have no "
           "person link yet: Lab A sends those links tomorrow.")
    deliver_day(d, "day1")
    out = d.pipeline("run")
    d.check("committed=6" in out, "all six files committed")
    d.check(d.query("SELECT count(*) FROM silver.measurement WHERE source = 'olink' AND hold_reason = 'H002'")[0][0] == 7500,
            "7,500 proteomics rows (150 samples x 50 proteins) are held (H002: no person link yet), never guessed")
    d.check(gold_value(d, "lab_c", "C77") == [(3.2, "mg/L", "G-0001")], "Lab C 0.32 mg/dL → 3.2 mg/L (the 10x trap avoided)")
    d.check(d.query("SELECT observed_at::DATE::VARCHAR FROM silver.measurement WHERE source_sample_id = 'T-501'")
            == [("2026-01-13",)], "Lab D's 01/13/2026 read as 13 January (its own date format)")
    d.show("SELECT s.source, s.source_subject_id AS vendor_id, g.global_subject_id AS person, s.source_value || ' ' || "
           "s.source_unit AS as_sent, g.value || ' ' || g.unit AS harmonized FROM gold.measurement_current g "
           "JOIN silver.measurement s USING (record_id, version) WHERE g.analyte_code = 'CRP' "
           "AND g.measurement_scale = 'absolute' AND g.global_subject_id = 'G-0001' ORDER BY 1")


def day2(d: Demo) -> None:
    d.step("DAY 2: Lab A's late file arrives; Lab B resends yesterday's file",
           "The late links resolve all 7,500 held proteomics rows automatically. The resend changes nothing.")
    deliver_day(d, "day2")
    out = d.pipeline("run")
    d.check("duplicate=1" in out, "Lab B's resend is recognized by its fingerprint and skipped")
    d.check("resolved held=7500" in out, "all 7,500 held proteomics rows resolve")
    d.show("SELECT version, change_reason, resolution_status, hold_reason, global_subject_id, observed_at::DATE AS drawn "
           "FROM silver.measurement WHERE source = 'olink' AND source_sample_id = 'S102' AND source_analyte_code = 'P02741' "
           "ORDER BY version")


def day3(d: Demo) -> None:
    d.step("DAY 3: Lab C adds a column; Lab D sends a unit spelling we don't know and an unregistered person",
           "Data keeps flowing. The new column and the bad row are reported; the unknown person is held.")
    deliver_day(d, "day3")
    d.pipeline("run", expect=(2,))
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE source = 'lab_c' AND column_name = 'specimen_type'")
            == [("OPEN",)], "Lab C's specimen_type is tracked as OPEN (undecided)")
    d.check(d.query("SELECT error_code, raw_value FROM meta.dlq WHERE status = 'OPEN'") == [("R006", "mg/l")],
            "Lab D's 'mg/l' is rejected (R006): we never guess units")
    d.check(latest(d, "lab_d", "T-505")[:2] == ("held", "H001"), "D_004 is not in the registry: held (H001)")

    d.step("DAY 3, decisions: ignore Lab C's new column; accept 'mg/l' as a spelling of mg/L",
           "Each decision is one reviewed line of configuration, then a replay of the rejected rows.")
    add_line(d, "ignored_columns: [specimen_type]", "sources/lab_c_v1.yaml", "decision: specimen_type not needed")
    add_line(d, "mg/l,mg/L", "reference/unit_aliases.csv", "decision: 'mg/l' is a spelling of mg/L")
    d.pipeline("dlq", "replay", "--error", "R006")
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE column_name = 'specimen_type'") == [("IGNORED",)],
            "drift status follows the configuration: IGNORED")
    d.check(d.query("SELECT count(*) FROM meta.dlq WHERE status = 'OPEN'")[0][0] == 0, "the rejected row is resolved")
    d.check(gold_value(d, "lab_d", "T-503") == [(3.1, "mg/L", "G-0001")], "Lab D's row is now in gold as 3.1 mg/L")


def day4(d: Demo) -> None:
    d.step("DAY 4: Lab B renames its columns (a breaking change)",
           "A required column is missing, so the file is quarantined whole — nothing is half-loaded.")
    deliver_day(d, "day4")
    d.pipeline("run", expect=(2,))
    d.pipeline("quarantine", "list")
    file_hash, path = d.query(
        "SELECT file_hash, quarantine_path FROM meta.ingested_files WHERE original_name = 'lab_b_day4_renamed.csv'")[0]
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE file_hash = ?", [file_hash]) == [("F004",)],
            "quarantined (F004) with expected vs received columns")
    copy_contract(d, Path("tests/fixtures/contracts/lab_b_v2.yaml"), "the fix: contract v2 maps Lab B's new column names")
    d.pipeline("check-file", "--source", "lab_b", path)
    d.pipeline("quarantine", "release", file_hash[:12])
    d.pipeline("run")
    d.check(d.query("SELECT DISTINCT contract_version FROM silver.measurement WHERE source_sample_id IN ('S210', 'S211')")
            == [("lab_b_v2",)], "loaded under lab_b_v2; Lab B's old-format files still use v1")

    d.step("DAY 4, registry update: D_004 is enrolled", "The held row resolves on the next run, no reprocessing needed.")
    append(d, "registry_d004.csv", "registry/enrollment.csv", "D_004 → G-0004 (enrollment update)")
    out = d.pipeline("run")
    d.check("resolved held=1" in out, "the held Lab D row resolves")
    d.check(gold_value(d, "lab_d", "T-505") == [(4.4, "mg/L", "G-0004")], "it now belongs to G-0004")


def day5(d: Demo) -> None:
    d.step("DAY 5: Lab C drops a required column (unit)",
           "Without a unit the value is meaningless. Quarantine, then the vendor resends.")
    deliver_day(d, "day5")
    d.pipeline("run", expect=(2,))
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE original_name = 'lab_c_day5_missing_unit.csv'")
            == [("F004",)], "quarantined (F004): result_unit missing")
    deliver_day(d, "day5_resend")
    d.pipeline("run")
    d.check(gold_value(d, "lab_c", "C80") == [(2.1, "mg/L", "G-0002")], "the resend loads: 0.21 mg/dL → 2.1 mg/L")


def day6(d: Demo) -> None:
    d.step("DAY 6: Lab A corrects and withdraws results; claims a tube for a second person; Lab D leaks names",
           "Corrections never overwrite. A tube claimed by two people is held, never merged. Names are blocked.")
    add_line(d, "forbidden_columns: [patient_name]", "sources/lab_d_v1.yaml", "decision (security): never accept patient names")
    deliver_day(d, "day6")
    d.pipeline("run", expect=(2,))
    d.show("SELECT source_sample_id, version, change_reason, value, result_status, resolution_status, hold_reason "
           "FROM silver.measurement WHERE source = 'lab_a' AND source_sample_id IN ('S100', 'S101', 'S102') ORDER BY 1, 2")
    d.check(gold_value(d, "lab_a", "S100") == [(2.3, "mg/L", "G-0001")], "S100 corrected to 2.3 (status C); 3.2 kept in history")
    d.check(gold_value(d, "lab_a", "S101") == [], "S101 withdrawn (status W): gone from gold, kept in history")
    d.check(latest(d, "lab_a", "S102")[:3] == ("held", "H003", None),
            "S102 claimed by a second person: held (H003), not merged")
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE original_name = 'lab_d_day6_leaky.csv'")
            == [("F007",)], "Lab D's file with patient names is quarantined (F007)")


def day7(d: Demo) -> None:
    d.step("DAY 7: onboard Lab E (configuration only); proteomics week 2; Lab B's values silently shift",
           "Lab A's links arrive with proteomics, so nothing is held. Lab B's file looks perfect — only its values are off.")
    copy_contract(d, WEEK / "onboarding/lab_e_v1.yaml")
    append(d, "aliases_lab_e.csv", "reference/analyte_aliases.csv")
    append(d, "registry_lab_e.csv", "registry/enrollment.csv", "Lab E onboarded: contract + test name + patient IDs")
    deliver_day(d, "day7")
    d.pipeline("run", expect=(2,))
    d.check(gold_value(d, "lab_e", "E-31") == [(1.9, "mg/L", "G-0002")], "Lab E's first file loads like any other")
    d.check(latest(d, "olink", "S104", "P02741") == ("resolved", None, "G-0001", "NEW"),
            "week-2 proteomics (7,600 rows) resolves immediately: its links arrived in the same run")
    d.check(d.query("SELECT count(*) FROM gold.measurement_current WHERE source = 'olink'")[0][0] == 22600,
            "all 22,600 proteomics rows of the week are in gold")
    d.check(d.query("SELECT code FROM meta.events WHERE file_name = 'lab_b_day7_unit_shift.csv'") == [("B001",)],
            "distribution-shift alert B001 on Lab B")
    d.note("Lab A dropped its optional result_status column today — not detected (known issue #13)")


def finale(d: Demo) -> None:
    d.step("Lineage: where did Lab C's 3.2 mg/L come from?", "File, row, contract, conversion, identity.")
    record_id = d.query("SELECT record_id FROM silver.measurement WHERE source_sample_id = 'C77'")[0][0]
    out = d.pipeline("lineage", record_id)
    d.check('"from": "0.32 mg/dL"' in out and '"factor": 10.0' in out, "0.32 mg/dL x 10 = 3.2 mg/L")
    d.check("lab_c_day1.csv" in out and '"contract_version": "lab_c_v1"' in out, "file lab_c_day1.csv, contract lab_c_v1")

    d.step("Disaster recovery: rebuild the whole week from bronze in a fresh environment",
           "Raw files are the source of truth for the data. (Operational history — runs, events, quarantine — "
           "lives in the metadata store, which has its own backups in production.)")
    rebuilt = Path("data/week-rebuilt")
    shutil.rmtree(ROOT / rebuilt, ignore_errors=True)
    d.shell(f"cp -R {DATA}/bronze {rebuilt}/bronze", lambda: shutil.copytree(ROOT / DATA / "bronze", ROOT / rebuilt / "bronze"))
    d.pipeline("replay", expect=(0, 2), data=rebuilt)
    fingerprint = "SELECT md5(string_agg(record_id || value || global_subject_id, ',' ORDER BY record_id)) FROM gold.measurement_current"
    d.check(d.query(fingerprint, warehouse=rebuilt) == d.query(fingerprint),
            "gold rebuilt from bronze alone is identical to the live gold")

    d.step("The week in numbers", "Every run, event and decision is recorded.")
    d.pipeline("status", "--limit", "20")
    d.pipeline("events", "--limit", "20")
    d.pipeline("drift")
    d.show("SELECT source, count(*) AS measurements, count(DISTINCT global_subject_id) AS people "
           "FROM gold.measurement_current GROUP BY source ORDER BY source")
    print(f"\nWEEK DEMO COMPLETE: {d.number} steps, all checks passed.")
    print(f"Explore it visually: streamlit run app/dashboard.py -- --data-dir {DATA}")


SEGMENTS = [(0, setup), (1, day1), (2, day2), (3, day3), (4, day4), (5, day5), (6, day6), (7, day7), (8, finale)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    parser.add_argument("--from-day", type=int, default=0, help="run earlier days quietly, present from this day")
    args = parser.parse_args()
    demo = Demo(pause=False, data=DATA, config=CONFIG)
    try:
        for day, segment in SEGMENTS:
            if day < args.from_day:
                with contextlib.redirect_stdout(io.StringIO()):
                    segment(demo)
            else:
                demo.pause = args.pause
                segment(demo)
    except CheckFailed as exc:
        print(f"\nDEMO STOPPED at step {demo.number}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
