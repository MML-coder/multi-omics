"""Source adapters: read → shape → map (ADR-010).

A reader loads a file into a temporary table of text columns with a 1-based `source_row`. Mapping then
turns vendor columns into canonical fields using the source contract. Readers are registered by format
name, so a new file format is a new reader, not a pipeline change.
"""

from collections.abc import Callable
from pathlib import Path

import duckdb

from omics_pipeline.config import CANONICAL_FIELDS, Contract
from omics_pipeline.warehouse import MISSING_VALUE_CODES

Reader = Callable[[duckdb.DuckDBPyConnection, Path, str], None]


# hive_partitioning = false: bronze folders are named source=<name>; without this DuckDB would add the
# folder name as an extra column when a file is read back from bronze (replay, DLQ replay).
def _delimited_reader(delimiter: str) -> Reader:
    def read(con: duckdb.DuckDBPyConnection, path: Path, table: str) -> None:
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {table} AS "
            "SELECT row_number() OVER () AS source_row, * "
            "FROM read_csv(?, header = true, all_varchar = true, delim = ?, hive_partitioning = false)",
            [str(path), delimiter],
        )

    return read


READERS: dict[str, Reader] = {
    "csv": _delimited_reader(","),
    "tsv": _delimited_reader("\t"),
}


def get_reader(file_format: str) -> Reader:
    try:
        return READERS[file_format]
    except KeyError:
        raise ValueError(f"No reader registered for format '{file_format}'. Known: {sorted(READERS)}") from None


def read_columns(con: duckdb.DuckDBPyConnection, path: Path, file_format: str) -> list[str]:
    """Header of a file, used to pick the matching contract version before staging."""
    get_reader(file_format)(con, path, "header_probe")
    columns = [row[0] for row in con.execute("DESCRIBE header_probe").fetchall()]
    con.execute("DROP TABLE header_probe")
    return [c for c in columns if c != "source_row"]


def stage(con: duckdb.DuckDBPyConnection, path: Path, contract: Contract) -> int:
    """Load the raw file into `staged_raw`; returns the number of data rows."""
    get_reader(contract.format)(con, path, "staged_raw")
    return con.execute("SELECT count(*) FROM staged_raw").fetchone()[0]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _clean(column: str) -> str:
    """Trimmed text, with missing-value codes (NA, NULL, -, …) turned into NULL."""
    codes = ", ".join(_literal(code) for code in MISSING_VALUE_CODES)
    col = _quote(column)
    return f"CASE WHEN upper(trim({col})) IN ({codes}) THEN NULL ELSE trim({col}) END"


def _field_expr(contract: Contract, name: str, columns: set[str]) -> str:
    if name in contract.constants:
        return _literal(contract.constants[name])
    spec = contract.fields.get(name)
    if spec is None:
        return "CAST(NULL AS VARCHAR)"
    if spec.column in columns:
        cleaned = _clean(spec.column)
        return f"coalesce({cleaned}, {_literal(spec.default)})" if spec.default is not None else cleaned
    return _literal(spec.default) if spec.default is not None else "CAST(NULL AS VARCHAR)"


def map_rows(con: duckdb.DuckDBPyConnection, contract: Contract, columns: list[str]) -> None:
    """Create `staged`: one text column per canonical field plus the raw row as JSON."""
    present = set(columns)
    selects = [f"{_field_expr(contract, name, present)} AS {name}" for name in CANONICAL_FIELDS]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE staged AS SELECT r.source_row, "
        + ", ".join(selects)
        + ", to_json(r) AS raw_json FROM staged_raw r"
    )
