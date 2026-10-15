#!/usr/bin/env python3
"""Companion to `run_demo.py --pause`: shows the database behind each demo step, in a second terminal.

    python demo/inspect_demo.py          # Enter = next step, a number = jump to that step, q = quit

Run each step here while the demo is paused at its "[Enter for the next step]" prompt. Every query
prints its SQL first, so it can be copied and changed. Read-only and short-lived connections: DuckDB
allows one writer or many readers, so querying while a pipeline step is running fails with a lock error
(just press Enter again once the demo is paused).
"""

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data/demo/warehouse.duckdb"
SCALE = ROOT / "data/demo-scale/warehouse.duckdb"

LAB_A_HISTORY = ("SELECT source_sample_id, version, change_reason, value, result_status, is_retraction, run_id "
                 "FROM silver.measurement WHERE source = 'lab_a' AND source_sample_id IN ('S100', 'S101') ORDER BY 1, 2")
GOLD_BY_SOURCE = "SELECT source, count(*) AS gold_rows FROM gold.measurement_current GROUP BY source ORDER BY source"
LAST_RUN = ("SELECT run_id, run_type, files_in, files_committed, files_quarantined, files_duplicate, rows_in, rows_ok, "
            "rows_dlq, rows_held, rows_resolved FROM meta.pipeline_runs ORDER BY run_id DESC LIMIT 1")
FILES = ("SELECT source, original_name, status, quarantine_code, contract_version, left(file_hash, 12) AS file_hash "
         "FROM meta.ingested_files ORDER BY received_at")

# (title, what to look for, [(label, sql, warehouse)])
STEPS = [
    ("Start from nothing", "Empty tables: the schemas exist, nothing is loaded.", [
        ("tables created by init", "SELECT schema_name, table_name FROM duckdb_tables() ORDER BY 1, 2", DEMO),
        ("files received", "SELECT count(*) AS files FROM meta.ingested_files", DEMO),
    ]),
    ("Three vendors deliver; proteomics arrives before Lab A",
     "Olink rows are held (H002), not guessed. Gold has only the 4 resolved rows.", [
        ("every silver row", "SELECT source, source_sample_id, version, global_subject_id, resolution_status, hold_reason "
                             "FROM silver.measurement ORDER BY source, source_sample_id", DEMO),
        ("gold", GOLD_BY_SOURCE, DEMO),
        ("files: each one committed, original kept in bronze", FILES, DEMO),
        ("this run's row accounting", LAST_RUN, DEMO),
    ]),
    ("Lab A arrives; held proteomics resolves automatically",
     "Olink v1 held → v2 RESOLVED; the draw date comes from Lab A's sample link. Gold = 8.", [
        ("olink history", "SELECT source_sample_id, analyte_code, version, change_reason, resolution_status, hold_reason, "
                          "global_subject_id, observed_at::DATE AS drawn, run_id, ingested_at FROM silver.measurement "
                          "WHERE source = 'olink' ORDER BY source_sample_id, version", DEMO),
        ("sample links: the evidence", "SELECT global_sample_id, global_subject_id, collected_at::DATE AS collected, "
                                       "evidence_source, evidence_row, run_id FROM meta.sample_links ORDER BY 1", DEMO),
        ("one person, three vendors, one unit", "SELECT source, assay, value, unit, measurement_scale, observed_at::DATE "
                                                "AS observed FROM gold.measurement_current WHERE global_subject_id = 'G-0001' "
                                                "AND analyte_code = 'CRP' ORDER BY source", DEMO),
        ("gold", GOLD_BY_SOURCE, DEMO),
    ]),
    ("Lineage: where did this number come from?",
     "The lineage columns on the row the demo traced. The CLI turns these into steps.", [
        ("Lab B P001: sent vs stored", "SELECT record_id, source_file, source_row, source_value, source_unit, value, unit, "
                                       "contract_version, reference_version, run_id FROM silver.measurement "
                                       "WHERE source = 'lab_b' AND source_subject_id = 'P001'", DEMO),
        ("the file it came from", "SELECT f.original_name, f.received_at, f.bronze_path FROM meta.ingested_files f "
                                  "JOIN silver.measurement s USING (file_hash) "
                                  "WHERE s.source = 'lab_b' AND s.source_subject_id = 'P001'", DEMO),
        ("the conversion factor", "SELECT from_unit, to_unit, factor FROM ref.units WHERE to_unit = 'mg/L'", DEMO),
    ]),
    ("Idempotency: the same file arrives again", "duplicate=1; no new file record, no new versions.", [
        ("this run", LAST_RUN, DEMO),
        ("files: lab_a_resent.csv is NOT here (same hash as lab_a_sample.csv)", FILES, DEMO),
        ("versions per source (unchanged)", "SELECT source, count(*) AS versions FROM silver.measurement "
                                            "GROUP BY source ORDER BY source", DEMO),
    ]),
    ("Corrections and retractions", "S100 v2 SOURCE_CORRECTION 2.3; S101 v2 RETRACTION. Gold drops Lab A S101.", [
        ("lab_a history", LAB_A_HISTORY, DEMO),
        ("gold for S100/S101 (olink unaffected)", "SELECT s.source, s.source_sample_id, g.version, g.value, g.unit "
                                                  "FROM gold.measurement_current g JOIN silver.measurement s "
                                                  "USING (record_id, version) WHERE s.source_sample_id IN ('S100', 'S101') "
                                                  "ORDER BY 1, 2", DEMO),
        ("sample links still there", "SELECT global_sample_id, global_subject_id FROM meta.sample_links ORDER BY 1", DEMO),
    ]),
    ("Validation, dead-letter queue, and replay after a fix", "R006 row OPEN → RESOLVED after the alias fix and replay.", [
        ("DLQ", "SELECT source, source_file, source_row, error_code, field, raw_value, status, attempts, "
                "resolved_record_id IS NOT NULL AS loaded FROM meta.dlq ORDER BY updated_at", DEMO),
        ("the replayed row", "SELECT source_sample_id, source_value, source_unit, value, unit, run_id "
                             "FROM silver.measurement WHERE source_sample_id = 'S210'", DEMO),
    ]),
    ("Schema drift: Lab A adds a column", "specimen_type OPEN, then IGNORED once the contract says so.", [
        ("drift tracking", "SELECT source, column_name, status, first_seen_file, files_seen, decided_in "
                           "FROM meta.schema_drift ORDER BY 1, 2", DEMO),
        ("events", "SELECT run_id, severity, code, file_name, detail FROM meta.events ORDER BY raised_at", DEMO),
    ]),
    ("Schema drift: a column we must never receive", "F007: the whole file quarantined, nothing loaded.", [
        ("quarantined files", "SELECT source, original_name, quarantine_code, quarantine_reason FROM meta.ingested_files "
                              "WHERE status = 'QUARANTINED'", DEMO),
        ("rows for S106 (expect none)", "SELECT count(*) AS rows FROM silver.measurement WHERE source_sample_id = 'S106'",
         DEMO),
    ]),
    ("Breaking change: Lab B renames its columns", "F004, then released and loaded under lab_b_v2.", [
        ("the file's journey", "SELECT original_name, status, quarantine_code, contract_version, last_run_id "
                               "FROM meta.ingested_files WHERE source = 'lab_b' ORDER BY received_at", DEMO),
        ("contract per Lab B row", "SELECT source_sample_id, contract_version, value, unit FROM silver.measurement "
                                   "WHERE source = 'lab_b' ORDER BY source_sample_id, version", DEMO),
    ]),
    ("Silent unit change", "The file is valid; only the value distribution reveals it (B001).", [
        ("events", "SELECT run_id, event_type, severity, code, file_name, detail FROM meta.events "
                   "WHERE code = 'B001'", DEMO),
        ("Lab B values over time", "SELECT f.original_name, round(avg(s.value), 3) AS avg_mg_l, count(*) AS rows "
                                   "FROM silver.measurement s JOIN meta.ingested_files f USING (file_hash) "
                                   "WHERE s.source = 'lab_b' GROUP BY ALL ORDER BY min(f.received_at)", DEMO),
    ]),
    ("Fault tolerance: a crash in the middle of writing a file",
     "This step runs tests in their own temporary folders; the demo database does not change.", [
        ("runs so far", "SELECT run_id, run_type, status, files_committed, files_failed FROM meta.pipeline_runs "
                        "ORDER BY run_id", DEMO),
    ]),
    ("Disaster recovery: rebuild everything from the raw files",
     "A brand-new database: one replay run, the same gold. Run history restarts because meta was deleted too.", [
        ("runs", "SELECT run_id, run_type, files_in, files_committed, files_quarantined, rows_in, rows_ok "
                 "FROM meta.pipeline_runs ORDER BY run_id", DEMO),
        ("gold", GOLD_BY_SOURCE, DEMO),
        ("lab_a history, rebuilt", LAB_A_HISTORY, DEMO),
    ]),
    ("Correctness at scale: compare with a known answer", "A separate warehouse (data/demo-scale), checked by `verify`.", [
        ("runs", "SELECT run_id, files_committed, files_duplicate, rows_in, rows_ok, rows_dlq, rows_held, rows_resolved "
                 "FROM meta.pipeline_runs ORDER BY run_id", SCALE),
        ("gold", GOLD_BY_SOURCE, SCALE),
        ("rejected rows by reason", "SELECT error_code, status, count(*) AS rows FROM meta.dlq GROUP BY ALL ORDER BY 1",
         SCALE),
        ("versions by reason", "SELECT change_reason, count(*) AS versions FROM silver.measurement GROUP BY 1 "
                               "ORDER BY 2 DESC", SCALE),
    ]),
    ("Where we ended up", "Everything that happened, from the metadata alone.", [
        ("runs", "SELECT run_id, run_type, files_in, files_committed, files_quarantined, files_duplicate, rows_in, "
                 "rows_ok, rows_dlq, rows_resolved FROM meta.pipeline_runs ORDER BY run_id", DEMO),
        ("events", "SELECT run_id, severity, code, source, file_name FROM meta.events ORDER BY raised_at", DEMO),
        ("files", FILES, DEMO),
    ]),
]


def show(label: str, sql: str, warehouse: Path) -> None:
    print(f"\n-- {label}\n{sql};")
    if not warehouse.exists():
        print(f"   (no {warehouse.relative_to(ROOT)} yet)")
        return
    try:
        con = duckdb.connect(str(warehouse), read_only=True)
    except duckdb.Error as exc:
        print(f"   (cannot open the database, probably a pipeline step is running: {exc})")
        return
    try:
        print(con.sql(sql))
    except duckdb.Error as exc:
        print(f"   ({exc})")
    finally:
        con.close()


def inspect(number: int) -> None:
    title, look_for, queries = STEPS[number - 1]
    print(f"\n{'=' * 100}\nSTEP {number}: {title}\n  look for: {look_for}\n{'=' * 100}")
    for label, sql, warehouse in queries:
        show(label, sql, warehouse)


def main() -> int:
    number = 1
    while True:
        answer = input(f"\n[Enter = step {number} · a step number · q = quit] ").strip().lower()
        if answer == "q":
            return 0
        if answer:
            if not answer.isdigit() or not 1 <= int(answer) <= len(STEPS):
                print(f"   steps are 1-{len(STEPS)}")
                continue
            number = int(answer)
        inspect(number)
        number = min(number + 1, len(STEPS))


if __name__ == "__main__":
    sys.exit(main())
