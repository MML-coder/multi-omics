"""The DuckDB warehouse: schemas, tables and reference-data loading (ADR-005, DATA_MODEL.md)."""

from pathlib import Path

import duckdb

from omics_pipeline.config import REFERENCE_FILES, REGISTRY_FILE

MISSING_VALUE_CODES = ("", "NA", "N/A", "NULL", ".", "-")

DDL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

CREATE TABLE IF NOT EXISTS meta.ingested_files (
    file_hash VARCHAR PRIMARY KEY,
    source VARCHAR,
    original_name VARCHAR,
    received_at TIMESTAMP,
    bronze_path VARCHAR,
    quarantine_path VARCHAR,
    contract_version VARCHAR,
    status VARCHAR,              -- COMMITTED | QUARANTINED
    quarantine_code VARCHAR,
    quarantine_reason VARCHAR,
    last_run_id VARCHAR,
    columns VARCHAR[]
);
-- Warehouses created before column tracking was added.
ALTER TABLE meta.ingested_files ADD COLUMN IF NOT EXISTS columns VARCHAR[];

CREATE TABLE IF NOT EXISTS meta.pipeline_runs (
    run_id VARCHAR PRIMARY KEY,
    run_type VARCHAR,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status VARCHAR,
    pipeline_version VARCHAR,
    reference_version VARCHAR,
    registry_version VARCHAR,
    files_in INTEGER, files_committed INTEGER, files_quarantined INTEGER,
    files_duplicate INTEGER, files_failed INTEGER,
    rows_in BIGINT, rows_ok BIGINT, rows_warn BIGINT, rows_dlq BIGINT,
    rows_held BIGINT, rows_filtered BIGINT, rows_resolved BIGINT
);

CREATE TABLE IF NOT EXISTS meta.run_stage_metrics (
    run_id VARCHAR,
    file_hash VARCHAR,
    stage VARCHAR,
    duration_ms DOUBLE,
    rows_in BIGINT,
    rows_out BIGINT
);

CREATE TABLE IF NOT EXISTS meta.dlq (
    dlq_id VARCHAR PRIMARY KEY,
    run_id VARCHAR,
    source VARCHAR,
    file_hash VARCHAR,
    source_file VARCHAR,
    source_row BIGINT,
    error_code VARCHAR,
    field VARCHAR,
    raw_value VARCHAR,
    raw_row VARCHAR,
    contract_version VARCHAR,
    status VARCHAR,              -- OPEN | RESOLVED | DISCARDED
    attempts INTEGER,
    resolved_record_id VARCHAR,
    resolved_version INTEGER,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta.events (
    run_id VARCHAR,
    event_type VARCHAR,
    severity VARCHAR,
    code VARCHAR,
    source VARCHAR,
    file_name VARCHAR,
    file_hash VARCHAR,
    detail VARCHAR,
    raised_at TIMESTAMP
);

-- One row per source column the contract did not map when first seen; status follows the config.
CREATE TABLE IF NOT EXISTS meta.schema_drift (
    source VARCHAR,
    column_name VARCHAR,
    status VARCHAR,              -- OPEN | IGNORED | FORBIDDEN | ADOPTED
    first_seen_file VARCHAR,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    files_seen INTEGER,
    decided_in VARCHAR,          -- contract version whose config decided the current status
    PRIMARY KEY (source, column_name)
);

CREATE TABLE IF NOT EXISTS meta.sample_links (
    global_sample_id VARCHAR PRIMARY KEY,
    global_subject_id VARCHAR,
    collected_at TIMESTAMP,
    collected_at_precision VARCHAR,
    evidence_source VARCHAR,
    evidence_file_hash VARCHAR,
    evidence_row BIGINT,
    run_id VARCHAR
);

CREATE TABLE IF NOT EXISTS silver.measurement (
    record_id VARCHAR,
    version INTEGER,
    change_reason VARCHAR,
    is_retraction BOOLEAN,
    source_hash VARCHAR,
    derived_hash VARCHAR,
    domain VARCHAR,
    source VARCHAR,
    global_subject_id VARCHAR,
    global_sample_id VARCHAR,
    source_subject_id VARCHAR,
    source_sample_id VARCHAR,
    source_encounter_id VARCHAR,
    analyte_code VARCHAR,
    assay VARCHAR,
    source_analyte_code VARCHAR,
    source_analyte_name VARCHAR,
    value DOUBLE,
    unit VARCHAR,
    measurement_scale VARCHAR,
    source_value VARCHAR,
    source_unit VARCHAR,
    observed_at TIMESTAMP,
    observed_at_precision VARCHAR,
    assay_run_date DATE,
    result_status VARCHAR,
    qc_flag VARCHAR,
    quality_flags VARCHAR[],
    resolution_status VARCHAR,   -- resolved | held
    hold_reason VARCHAR,
    source_file VARCHAR,
    file_hash VARCHAR,
    source_row BIGINT,
    source_locator VARCHAR,
    contract_version VARCHAR,
    run_id VARCHAR,
    ingested_at TIMESTAMP,
    pipeline_version VARCHAR,
    reference_version VARCHAR,
    registry_version VARCHAR,
    PRIMARY KEY (record_id, version)
);
"""


def connect(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if read_only:
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)
    return con


def load_reference_data(con: duckdb.DuckDBPyConnection, config_dir: Path) -> None:
    """Snapshot reference data and the registry into the `ref` schema for this run."""
    tables = dict(zip(("analytes", "aliases", "units", "unit_aliases", "value_ranges"), REFERENCE_FILES))
    for table, filename in tables.items():
        con.execute(
            f"CREATE OR REPLACE TABLE ref.{table} AS "
            "SELECT * FROM read_csv(?, header = true, all_varchar = true)",
            [str(config_dir / "reference" / filename)],
        )
    con.execute("CREATE OR REPLACE TABLE ref.units AS SELECT from_unit, to_unit, CAST(factor AS DOUBLE) AS factor FROM ref.units")
    con.execute(
        "CREATE OR REPLACE TABLE ref.value_ranges AS SELECT analyte_code, unit, "
        "CAST(hard_min AS DOUBLE) AS hard_min, CAST(hard_max AS DOUBLE) AS hard_max, "
        "CAST(soft_min AS DOUBLE) AS soft_min, CAST(soft_max AS DOUBLE) AS soft_max FROM ref.value_ranges"
    )
    con.execute(
        "CREATE OR REPLACE TABLE ref.registry AS "
        "SELECT row_number() OVER () AS registry_row, * "
        "FROM read_csv(?, header = true, all_varchar = true)",
        [str(config_dir / REGISTRY_FILE)],
    )
