"""Schema-drift tracking (README "Schema change workflow").

Every column a contract does not map is tracked in meta.schema_drift with a status derived from the
configuration, never set by hand:

    OPEN       new and undecided            → SchemaDriftDetected on every file until decided
    IGNORED    listed in ignored_columns    → no warning; values stay in bronze
    FORBIDDEN  listed in forbidden_columns  → files containing it are quarantined (F007)
    ADOPTED    mapped by the contract       → stored in silver (after a model change + replay)

The decision itself lives in a reviewed contract change; the table shows its effect and history.
"""

from dataclasses import dataclass
from datetime import datetime

import duckdb

from omics_pipeline.config import Contract


@dataclass(frozen=True)
class ColumnCheck:
    contract: Contract | None
    open: list[str]
    ignored: list[str]
    forbidden: list[str]

    @property
    def verdict(self) -> str:
        if self.contract is None:
            return "quarantine F004 (matches no contract version)"
        if self.forbidden:
            return f"quarantine F007 (forbidden columns {self.forbidden})"
        if self.open:
            return f"ingest with SchemaDriftDetected F006 (undecided columns {self.open})"
        return "ingest cleanly"


def check_columns(contract: Contract | None, columns: list[str]) -> ColumnCheck:
    if contract is None:
        return ColumnCheck(None, [], [], [])
    status = {c: contract.column_status(c) for c in columns}
    return ColumnCheck(
        contract,
        open=[c for c, s in status.items() if s == "OPEN"],
        ignored=[c for c, s in status.items() if s == "IGNORED"],
        forbidden=[c for c, s in status.items() if s == "FORBIDDEN"],
    )


def record(con: duckdb.DuckDBPyConnection, contract: Contract, columns: list[str], file_name: str,
           seen_at: datetime) -> None:
    """Upsert every unmapped column of this file with its current status."""
    for column in columns:
        status = contract.column_status(column)
        if status == "ADOPTED":
            continue
        decided_in = None if status == "OPEN" else contract.name
        updated = con.execute(
            "UPDATE meta.schema_drift SET status = ?, last_seen_at = ?, files_seen = files_seen + 1, decided_in = ? "
            "WHERE source = ? AND column_name = ? RETURNING 1",
            [status, seen_at, decided_in, contract.source, column],
        ).fetchall()
        if not updated:
            con.execute(
                "INSERT INTO meta.schema_drift VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                [contract.source, column, status, file_name, seen_at, seen_at, decided_in],
            )


def reconcile(con: duckdb.DuckDBPyConnection, contracts: dict[str, list[Contract]]) -> None:
    """Re-derive each tracked column's status from the newest contract (config changes take effect)."""
    for source, column in con.execute("SELECT source, column_name FROM meta.schema_drift").fetchall():
        if source not in contracts:
            continue
        newest = contracts[source][0]
        status = newest.column_status(column)
        con.execute(
            "UPDATE meta.schema_drift SET status = ?, decided_in = ? WHERE source = ? AND column_name = ?",
            [status, None if status == "OPEN" else newest.name, source, column],
        )
