"""Normalize, resolve identity and version validated rows into silver (DATA_MODEL.md §2–4).

Works on the temp tables built by adapters.py and rules.py for one file:
`typed` → `keyed` (record_id, source_hash, in-file duplicates removed) → `candidates` (normalized and
resolved) → `versioned` (only rows that are new or changed) → silver.measurement.
"""

from dataclasses import dataclass

import duckdb

from omics_pipeline.config import Contract

NOW_UTC = "(now() AT TIME ZONE 'UTC')"

# Fields as delivered by the vendor: a change here is a vendor correction (source_hash).
SOURCE_FIELDS = (
    "source_subject_id", "source_sample_id", "source_encounter_id", "source_analyte_code",
    "source_value", "source_unit", "observed_at", "result_status", "qc_flag", "assay_run_date",
)
# Fields we derive: a change here comes from our reference data or identity links (derived_hash).
DERIVED_FIELDS = (
    "value", "unit", "analyte_code", "assay", "measurement_scale", "global_subject_id",
    "global_sample_id", "observed_at", "observed_at_precision", "resolution_status", "hold_reason",
    "is_retraction", "quality_flags",
)

SILVER_COLUMNS = (
    "record_id", "version", "change_reason", "is_retraction", "source_hash", "derived_hash", "domain",
    "source", "global_subject_id", "global_sample_id", "source_subject_id", "source_sample_id",
    "source_encounter_id", "analyte_code", "assay", "source_analyte_code", "source_analyte_name", "value",
    "unit", "measurement_scale", "source_value", "source_unit", "observed_at", "observed_at_precision",
    "assay_run_date", "result_status", "qc_flag", "quality_flags", "resolution_status", "hold_reason",
    "source_file", "file_hash", "source_row", "source_locator", "contract_version", "run_id",
    "ingested_at", "pipeline_version", "reference_version", "registry_version",
)

# B001: a file whose median differs from the source's current median by more than this factor.
DISTRIBUTION_SHIFT_FACTOR = 10.0


@dataclass(frozen=True)
class WriteContext:
    run_id: str
    file_hash: str
    source_file: str
    pipeline_version: str
    reference_version: str
    registry_version: str


@dataclass(frozen=True)
class RowCounts:
    rows_in: int
    ok: int
    warn: int
    dlq: int
    held: int
    filtered: int
    written: int

    @property
    def balanced(self) -> bool:
        return self.rows_in == self.ok + self.dlq + self.held + self.filtered


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _hash(parts: list[str]) -> str:
    joined = ", ".join(f"coalesce(CAST({p} AS VARCHAR), '')" for p in parts)
    return f"sha256(concat_ws('|', {joined}))"


def derived_hash_sql() -> str:
    return _hash(list(DERIVED_FIELDS))


def key_rows(con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
    """Create `keyed`; reject conflicting in-file duplicates (R010), filter exact ones (W005)."""
    record_id = _hash([_lit(contract.source), *contract.natural_key])
    con.execute(
        "CREATE OR REPLACE TEMP TABLE keyed AS "
        f"SELECT t.*, {record_id} AS record_id, {_hash(list(SOURCE_FIELDS))} AS source_hash "
        "FROM typed t WHERE source_row NOT IN (SELECT source_row FROM row_errors)"
    )
    con.execute(
        "INSERT INTO row_errors SELECT source_row, 'R010', 'natural_key', record_id FROM keyed "
        "WHERE record_id IN (SELECT record_id FROM keyed GROUP BY record_id HAVING count(DISTINCT source_hash) > 1)"
    )
    con.execute(
        "INSERT INTO filtered SELECT source_row, 'W005' FROM ("
        "  SELECT source_row, row_number() OVER (PARTITION BY record_id ORDER BY source_row) AS rn"
        "  FROM keyed WHERE source_row NOT IN (SELECT source_row FROM row_errors)"
        ") WHERE rn > 1"
    )
    con.execute(
        "DELETE FROM keyed WHERE source_row IN (SELECT source_row FROM row_errors) "
        "OR source_row IN (SELECT source_row FROM filtered)"
    )


def _identity_sql(contract: Contract) -> tuple[str, str, str, str, str]:
    """(joins, subject, observed_at, precision, hold_reason) expressions for this source."""
    if contract.observed_at_from == "sample_link":
        return (
            "LEFT JOIN meta.sample_links l ON l.global_sample_id = b.global_sample_id",
            "l.global_subject_id",
            "l.collected_at",
            "l.collected_at_precision",
            "CASE WHEN l.global_subject_id IS NULL THEN 'H002' END",
        )
    spec = contract.fields.get("observed_at")
    precision = "'minute'" if spec is not None and spec.type == "datetime" else "'date'"
    # H003: the sample is already linked to a different person, or this file links it to two people.
    conflict = (
        "(l.global_subject_id IS NOT NULL AND l.global_subject_id <> g.study_subject_id) OR "
        "(b.global_sample_id IS NOT NULL AND "
        " min(g.study_subject_id) OVER (PARTITION BY b.global_sample_id) <> "
        " max(g.study_subject_id) OVER (PARTITION BY b.global_sample_id))"
    )
    return (
        f"LEFT JOIN ref.registry g ON g.source = {_lit(contract.source)} AND g.source_subject_id = b.source_subject_id "
        "LEFT JOIN meta.sample_links l ON l.global_sample_id = b.global_sample_id",
        f"CASE WHEN {conflict} THEN NULL ELSE g.study_subject_id END",
        "b.observed_ts",
        precision,
        f"CASE WHEN g.study_subject_id IS NULL THEN 'H001' WHEN {conflict} THEN 'H003' END",
    )


def normalize(con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
    """Create `candidates`: canonical rows with normalized values and resolved (or held) identity."""
    source = _lit(contract.source)
    if contract.sample_namespace:
        prefix = _lit(contract.sample_namespace + "|")
        sample = f"CASE WHEN k.source_sample_id IS NOT NULL THEN 'SMP-' || left(sha256({prefix} || k.source_sample_id), 16) END"
    else:
        sample = "CAST(NULL AS VARCHAR)"
    joins, subject, observed, precision, hold = _identity_sql(contract)
    quality_flags = (
        "list_filter(["
        "CASE WHEN value < soft_min OR value > soft_max THEN 'W001' END, "
        "CASE WHEN qc_flag = 'WARN' THEN 'W002' END, "
        "CASE WHEN result_status = 'P' THEN 'W003' END], x -> x IS NOT NULL)"
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE candidates AS
        WITH base AS (
            SELECT k.*, a.analyte_code, a.assay, a.measurement_scale, a.canonical_unit AS unit,
                   k.value_num * u.factor AS value, r.soft_min, r.soft_max, {sample} AS global_sample_id
            FROM keyed k
            JOIN ref.aliases a ON a.source = {source} AND a.source_code = k.source_analyte_code
            JOIN ref.units u ON u.from_unit = k.unit_std AND u.to_unit = a.canonical_unit
            LEFT JOIN ref.value_ranges r ON r.analyte_code = a.analyte_code AND r.unit = a.canonical_unit
        ), resolved AS (
            SELECT b.*, {subject} AS global_subject_id, {observed} AS observed_final,
                   {precision} AS observed_at_precision, {hold} AS hold_reason
            FROM base b {joins}
        )
        SELECT record_id, source_hash, {_lit(contract.domain)} AS domain, {source} AS source,
               global_subject_id, global_sample_id, source_subject_id, source_sample_id,
               source_encounter_id, analyte_code, assay, source_analyte_code, source_analyte_name,
               value, unit, measurement_scale, source_value, source_unit,
               CAST(observed_final AS TIMESTAMP) AS observed_at, observed_at_precision,
               assay_run_date_d AS assay_run_date, result_status, qc_flag,
               {quality_flags} AS quality_flags,
               CASE WHEN hold_reason IS NULL THEN 'resolved' ELSE 'held' END AS resolution_status,
               hold_reason, coalesce(result_status = 'W', false) AS is_retraction, source_row
        FROM resolved
    """)


def reject_unknown_retractions(con: duckdb.DuckDBPyConnection) -> None:
    """R011: a retraction for a record never ingested cannot be applied; reject it for later replay."""
    con.execute(
        "INSERT INTO row_errors SELECT source_row, 'R011', 'result_status', result_status FROM candidates c "
        "WHERE is_retraction AND NOT EXISTS (SELECT 1 FROM silver.measurement s WHERE s.record_id = c.record_id)"
    )
    con.execute("DELETE FROM candidates WHERE source_row IN (SELECT source_row FROM row_errors)")


def version_rows(con: duckdb.DuckDBPyConnection, tracks_status: bool, received_at) -> None:
    """Create `versioned`: candidates that are new or whose source or derived content changed.

    Latest received wins: a row may only version a record whose current version came from a file
    received no later than this one. Replaying an older file therefore never overrides newer data, and a
    reference-data fix reaches each record through the file that produced its current version.

    W004 flags a changed value that the vendor did not mark as a correction, when the previous version
    was already final. Preliminary → final is a normal update and is not flagged.
    """
    unflagged = (
        "cur.version IS NOT NULL AND c.source_hash <> cur.source_hash "
        "AND coalesce(cur.result_status, 'F') IN ('F', 'C') AND coalesce(c.result_status, 'F') NOT IN ('C', 'W')"
        if tracks_status else "false"
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE versioned AS
        WITH cur AS (
            SELECT s.record_id, max(s.version) AS version,
                   arg_max(s.source_hash, s.version) AS source_hash,
                   arg_max(s.derived_hash, s.version) AS derived_hash,
                   arg_max(s.resolution_status, s.version) AS resolution_status,
                   arg_max(s.result_status, s.version) AS result_status,
                   arg_max(f.received_at, s.version) AS received_at
            FROM silver.measurement s LEFT JOIN meta.ingested_files f ON f.file_hash = s.file_hash
            WHERE s.record_id IN (SELECT record_id FROM candidates) GROUP BY s.record_id
        ), superseded AS (
            SELECT record_id FROM cur WHERE received_at > ?
        ), flagged AS (
            SELECT c.* REPLACE (
                       CASE WHEN {unflagged} THEN list_append(c.quality_flags, 'W004') ELSE c.quality_flags END
                       AS quality_flags),
                   cur.version AS cur_version, cur.source_hash AS cur_source_hash,
                   cur.derived_hash AS cur_derived_hash, cur.resolution_status AS cur_resolution_status
            FROM candidates c LEFT JOIN cur ON c.record_id = cur.record_id
            WHERE c.record_id NOT IN (SELECT record_id FROM superseded)
        ), hashed AS (
            SELECT *, {derived_hash_sql()} AS derived_hash FROM flagged
        )
        SELECT * EXCLUDE (cur_version, cur_source_hash, cur_derived_hash, cur_resolution_status),
               coalesce(cur_version, 0) + 1 AS version,
               CASE WHEN cur_version IS NULL THEN 'NEW'
                    WHEN is_retraction THEN 'RETRACTION'
                    WHEN source_hash <> cur_source_hash THEN 'SOURCE_CORRECTION'
                    WHEN cur_resolution_status = 'held' AND resolution_status = 'resolved' THEN 'RESOLVED'
                    ELSE 'DERIVATION_CHANGE' END AS change_reason
        FROM hashed
        WHERE cur_version IS NULL OR source_hash <> cur_source_hash OR derived_hash <> cur_derived_hash
    """, [received_at])


def write(con: duckdb.DuckDBPyConnection, contract: Contract, ctx: WriteContext) -> RowCounts:
    """Write versioned rows to silver, rejects to the DLQ and new sample links; return row counts."""
    lineage = {
        "source_file": ctx.source_file, "file_hash": ctx.file_hash, "source_locator": None,
        "contract_version": contract.name, "run_id": ctx.run_id, "pipeline_version": ctx.pipeline_version,
        "reference_version": ctx.reference_version, "registry_version": ctx.registry_version,
    }
    selects = []
    params = []
    for column in SILVER_COLUMNS:
        if column in lineage:
            selects.append(f"CAST(? AS VARCHAR) AS {column}")
            params.append(lineage[column])
        elif column == "ingested_at":
            selects.append(f"{NOW_UTC} AS ingested_at")
        else:
            selects.append(column)
    con.execute(
        f"INSERT INTO silver.measurement ({', '.join(SILVER_COLUMNS)}) SELECT {', '.join(selects)} FROM versioned",
        params,
    )
    _write_dlq(con, contract, ctx)
    if contract.observed_at_from != "sample_link":
        con.execute(
            "INSERT OR IGNORE INTO meta.sample_links "
            "SELECT DISTINCT ON (global_sample_id) global_sample_id, global_subject_id, observed_at, "
            "observed_at_precision, source, ?, source_row, ? FROM candidates "
            "WHERE global_sample_id IS NOT NULL AND global_subject_id IS NOT NULL AND observed_at IS NOT NULL "
            "ORDER BY global_sample_id, source_row",
            [ctx.file_hash, ctx.run_id],
        )
    return _counts(con)


def _write_dlq(con: duckdb.DuckDBPyConnection, contract: Contract, ctx: WriteContext) -> None:
    """Record new rejects; resolve earlier rejects whose row is now accepted; count repeated failures."""
    dlq_id = "sha256(? || '|' || e.source_row || '|' || e.error_code || '|' || e.field)"
    con.execute(
        f"UPDATE meta.dlq SET attempts = attempts + 1, updated_at = {NOW_UTC} "
        f"WHERE status = 'OPEN' AND dlq_id IN (SELECT {dlq_id} FROM row_errors e)",
        [ctx.file_hash],
    )
    con.execute(
        f"INSERT OR IGNORE INTO meta.dlq SELECT {dlq_id}, ?, ?, ?, ?, "
        "e.source_row, e.error_code, e.field, e.raw_value, s.raw_json, ?, 'OPEN', 1, NULL, NULL, "
        f"{NOW_UTC} FROM row_errors e JOIN staged s ON s.source_row = e.source_row",
        [ctx.file_hash, ctx.run_id, contract.source, ctx.file_hash, ctx.source_file, contract.name],
    )
    con.execute(
        f"UPDATE meta.dlq SET status = 'RESOLVED', resolved_record_id = c.record_id, updated_at = {NOW_UTC}, "
        "resolved_version = (SELECT max(version) FROM silver.measurement s WHERE s.record_id = c.record_id) "
        "FROM candidates c WHERE meta.dlq.file_hash = ? AND meta.dlq.source_row = c.source_row "
        "AND meta.dlq.status = 'OPEN'",
        [ctx.file_hash],
    )


def _counts(con: duckdb.DuckDBPyConnection) -> RowCounts:
    row = con.execute("""
        SELECT (SELECT count(*) FROM staged),
               (SELECT count(*) FROM candidates WHERE resolution_status = 'resolved'),
               (SELECT count(*) FROM (SELECT source_row FROM candidates WHERE len(quality_flags) > 0
                                      UNION SELECT source_row FROM versioned WHERE len(quality_flags) > 0)),
               (SELECT count(DISTINCT source_row) FROM row_errors),
               (SELECT count(*) FROM candidates WHERE resolution_status = 'held'),
               (SELECT count(*) FROM filtered),
               (SELECT count(*) FROM versioned)
    """).fetchone()
    return RowCounts(*row)


def distribution_alerts(con: duckdb.DuckDBPyConnection, source: str) -> list[tuple[str, str]]:
    """B001: per analyte, compare this file's median with the source's current median in gold."""
    has_gold = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'gold' AND table_name = 'measurement_current'"
    ).fetchone()[0]
    if not has_gold:
        return []
    rows = con.execute(
        "SELECT c.analyte_code, median(c.value) AS file_median, "
        "  (SELECT median(g.value) FROM gold.measurement_current g "
        "   WHERE g.source = ? AND g.analyte_code = c.analyte_code AND g.measurement_scale = 'absolute') AS baseline "
        "FROM candidates c WHERE c.measurement_scale = 'absolute' GROUP BY c.analyte_code",
        [source],
    ).fetchall()
    alerts = []
    for analyte, file_median, baseline in rows:
        if not baseline or not file_median:
            continue
        ratio = file_median / baseline
        if ratio > DISTRIBUTION_SHIFT_FACTOR or ratio < 1 / DISTRIBUTION_SHIFT_FACTOR:
            detail = f"median {file_median:g} vs current {baseline:g} (x{ratio:.3g}); possible unit change"
            alerts.append((analyte, detail))
    return alerts


def reresolve_held(con: duckdb.DuckDBPyConnection, ctx: WriteContext) -> int:
    """Retry identity for held rows (ADR-002); resolvable ones get a new RESOLVED version.

    H001 (not in registry) and H002 (sample not yet linked) resolve automatically when the missing
    evidence arrives. H003 (identity conflict) needs investigation and is never resolved automatically.

    Cost follows what is waiting, not all history: only records with a held version that are not
    already resolved in gold (or that were written in this run, since gold is rebuilt after this step)
    are ranked to find their latest version. Benchmark: docs/benchmarks/.
    """
    has_gold = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'gold' AND table_name = 'measurement_current'"
    ).fetchone()[0]
    not_resolved = (
        "AND (record_id NOT IN (SELECT record_id FROM gold.measurement_current) OR run_id = ?)" if has_gold else ""
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE reresolved AS
        WITH waiting AS (
            SELECT DISTINCT record_id FROM silver.measurement
            WHERE resolution_status = 'held' {not_resolved}
        ), cur AS (
            SELECT * FROM silver.measurement
            WHERE record_id IN (SELECT record_id FROM waiting)
            QUALIFY row_number() OVER (PARTITION BY record_id ORDER BY version DESC) = 1
        )
        SELECT h.* REPLACE (
            CASE WHEN h.hold_reason = 'H002' THEN l.global_subject_id ELSE g.study_subject_id END AS global_subject_id,
            CASE WHEN h.hold_reason = 'H002' THEN l.collected_at ELSE h.observed_at END AS observed_at,
            CASE WHEN h.hold_reason = 'H002' THEN l.collected_at_precision ELSE h.observed_at_precision END
                AS observed_at_precision,
            'resolved' AS resolution_status,
            CAST(NULL AS VARCHAR) AS hold_reason,
            h.version + 1 AS version,
            'RESOLVED' AS change_reason,
            CAST(? AS VARCHAR) AS run_id,
            {NOW_UTC} AS ingested_at,
            CAST(? AS VARCHAR) AS reference_version,
            CAST(? AS VARCHAR) AS registry_version
        )
        FROM cur h
        LEFT JOIN meta.sample_links l ON l.global_sample_id = h.global_sample_id
        LEFT JOIN ref.registry g ON g.source = h.source AND g.source_subject_id = h.source_subject_id
        WHERE h.resolution_status = 'held'
          AND ((h.hold_reason = 'H002' AND l.global_subject_id IS NOT NULL)
               OR (h.hold_reason = 'H001' AND g.study_subject_id IS NOT NULL))
    """, ([ctx.run_id] if has_gold else []) + [ctx.run_id, ctx.reference_version, ctx.registry_version])
    con.execute(f"UPDATE reresolved SET derived_hash = {derived_hash_sql()}")
    con.execute("INSERT INTO silver.measurement SELECT * FROM reresolved")
    return con.execute("SELECT count(*) FROM reresolved").fetchone()[0]
