"""Row validation compiled to SQL (ADR-007, ERROR_CATALOG.md).

Structural rules come from the contract (required fields, types, formats, allowed values, no future
dates); semantic rules check against reference data (known analyte, known and convertible unit,
physically possible value). Every failure is recorded in `row_errors`; a row with any failure is
rejected once. Expected non-data rows (control samples) go to `filtered` instead.
"""

import duckdb

from omics_pipeline.config import Contract

ALLOWED_VALUES = {
    "result_status": ("P", "F", "C", "W"),
    "qc_flag": ("PASS", "WARN"),
}
NOW_UTC = "(now() AT TIME ZONE 'UTC')"


def _time_expr(contract: Contract, name: str) -> str:
    """Parse a time field; datetimes with a configured timezone are converted to UTC (assumption A4)."""
    spec = contract.fields.get(name)
    if spec is None:
        return "CAST(NULL AS TIMESTAMP)"
    fmt = spec.format.replace("'", "''")
    parsed = f"try_strptime(s.{name}, '{fmt}')"
    if spec.type == "datetime" and contract.timezone:
        tz = contract.timezone.replace("'", "''")
        return f"(timezone('{tz}', {parsed}) AT TIME ZONE 'UTC')"
    return parsed


def type_rows(con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
    """Create `typed`: `staged` plus parsed value, timestamps and the standardized unit spelling."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE typed AS SELECT s.*, "
        "try_cast(s.source_value AS DOUBLE) AS value_num, "
        f"{_time_expr(contract, 'observed_at')} AS observed_ts, "
        f"CAST({_time_expr(contract, 'assay_run_date')} AS DATE) AS assay_run_date_d, "
        "coalesce(ua.unit, s.source_unit) AS unit_std "
        "FROM staged s LEFT JOIN ref.unit_aliases ua ON ua.raw_unit = s.source_unit"
    )


def filter_rows(con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
    """Create `filtered` and move expected non-data rows (X001 control samples) out of `typed`."""
    con.execute("CREATE OR REPLACE TEMP TABLE filtered (source_row BIGINT, code VARCHAR)")
    if contract.control_sample_pattern:
        con.execute(
            "INSERT INTO filtered SELECT source_row, 'X001' FROM typed WHERE regexp_matches(source_sample_id, ?)",
            [contract.control_sample_pattern],
        )
        con.execute("DELETE FROM typed WHERE source_row IN (SELECT source_row FROM filtered)")


def _structural_checks(contract: Contract) -> list[str]:
    checks = []
    for name, spec in contract.fields.items():
        if spec.required:
            checks.append(f"SELECT source_row, 'R001', '{name}', NULL FROM typed WHERE {name} IS NULL")
    checks.append(
        "SELECT source_row, 'R002', 'source_value', source_value FROM typed "
        "WHERE source_value IS NOT NULL AND value_num IS NULL"
    )
    if "observed_at" in contract.fields:
        checks.append(
            "SELECT source_row, 'R003', 'observed_at', observed_at FROM typed "
            "WHERE observed_at IS NOT NULL AND observed_ts IS NULL"
        )
        checks.append(
            "SELECT source_row, 'R004', 'observed_at', observed_at FROM typed "
            f"WHERE observed_ts > {NOW_UTC}"
        )
    if "assay_run_date" in contract.fields:
        checks.append(
            "SELECT source_row, 'R003', 'assay_run_date', assay_run_date FROM typed "
            "WHERE assay_run_date IS NOT NULL AND assay_run_date_d IS NULL"
        )
    for name, allowed in ALLOWED_VALUES.items():
        if name in contract.fields:
            values = ", ".join(f"'{v}'" for v in allowed)
            checks.append(
                f"SELECT source_row, 'R009', '{name}', {name} FROM typed "
                f"WHERE {name} IS NOT NULL AND {name} NOT IN ({values})"
            )
    return checks


def _semantic_checks(contract: Contract) -> list[str]:
    source = contract.source.replace("'", "''")
    alias = f"a.source = '{source}' AND a.source_code = t.source_analyte_code"
    convertible = "EXISTS (SELECT 1 FROM ref.units u WHERE u.from_unit = t.unit_std AND u.to_unit = a.canonical_unit)"
    known_unit = "t.unit_std IN (SELECT from_unit FROM ref.units UNION SELECT to_unit FROM ref.units)"
    not_rejected = "t.source_row NOT IN (SELECT source_row FROM row_errors)"
    return [
        f"SELECT t.source_row, 'R005', 'source_analyte_code', t.source_analyte_code FROM typed t "
        f"WHERE {not_rejected} AND NOT EXISTS (SELECT 1 FROM ref.aliases a WHERE {alias})",
        f"SELECT t.source_row, CASE WHEN {known_unit} THEN 'R007' ELSE 'R006' END, 'source_unit', t.source_unit "
        f"FROM typed t JOIN ref.aliases a ON {alias} WHERE {not_rejected} AND NOT {convertible}",
        f"SELECT t.source_row, 'R008', 'source_value', t.source_value FROM typed t "
        f"JOIN ref.aliases a ON {alias} "
        "JOIN ref.units u ON u.from_unit = t.unit_std AND u.to_unit = a.canonical_unit "
        "JOIN ref.value_ranges r ON r.analyte_code = a.analyte_code AND r.unit = a.canonical_unit "
        f"WHERE {not_rejected} AND (t.value_num * u.factor < r.hard_min OR t.value_num * u.factor > r.hard_max)",
    ]


def validate(con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
    """Populate `row_errors`. Semantic checks run only on rows that passed structural checks."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE row_errors "
        "(source_row BIGINT, error_code VARCHAR, field VARCHAR, raw_value VARCHAR)"
    )
    for check in _structural_checks(contract):
        con.execute(f"INSERT INTO row_errors {check}")
    # One statement, so a semantic failure does not hide another semantic failure on the same row.
    con.execute("INSERT INTO row_errors " + " UNION ALL ".join(_semantic_checks(contract)))


def reject_rate(con: duckdb.DuckDBPyConnection) -> float:
    rejected, total = con.execute(
        "SELECT (SELECT count(DISTINCT source_row) FROM row_errors), (SELECT count(*) FROM staged)"
    ).fetchone()
    return rejected / total if total else 0.0
