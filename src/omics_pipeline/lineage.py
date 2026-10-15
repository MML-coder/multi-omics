"""Readable source-to-canonical lineage for one record (WALKTHROUGH §4).

The steps are derived from columns stored on every silver row plus the metadata tables, rather than
stored as a JSON document per row; this keeps silver compact at volume while still answering
"which file, which row, what was done, and who changed it".
"""

import duckdb

HISTORY_COLUMNS = (
    "version", "change_reason", "value", "unit", "resolution_status", "hold_reason", "is_retraction",
    "result_status", "run_id", "ingested_at", "reference_version", "registry_version",
)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    cursor = con.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _identity_step(con: duckdb.DuckDBPyConnection, latest: dict) -> dict:
    if latest["global_subject_id"] is None:
        return {"step": "resolve_identity", "status": "held", "hold_reason": latest["hold_reason"]}
    links = _rows(con, "SELECT * FROM meta.sample_links WHERE global_sample_id = ?", [latest["global_sample_id"]])
    if latest["source_subject_id"] is None and links:
        link = links[0]
        return {
            "step": "resolve_identity",
            "method": "sample_link",
            "global_sample_id": latest["global_sample_id"],
            "global_subject_id": latest["global_subject_id"],
            "evidence": {
                "source": link["evidence_source"],
                "file_hash": link["evidence_file_hash"],
                "source_row": link["evidence_row"],
            },
            "observed_at_from": "sample collection date",
        }
    return {
        "step": "resolve_identity",
        "method": "enrollment_registry",
        "source_subject_id": latest["source_subject_id"],
        "global_subject_id": latest["global_subject_id"],
        "global_sample_id": latest["global_sample_id"],
        "registry_version": latest["registry_version"],
    }


def measurement_lineage(con: duckdb.DuckDBPyConnection, record_id: str) -> dict | None:
    versions = _rows(con, "SELECT * FROM silver.measurement WHERE record_id = ? ORDER BY version", [record_id])
    if not versions:
        return None
    latest = versions[-1]
    files = _rows(con, "SELECT * FROM meta.ingested_files WHERE file_hash = ?", [latest["file_hash"]])
    received = files[0] if files else {}
    factor = con.execute(
        "SELECT factor FROM ref.units WHERE to_unit = ? AND from_unit = "
        "coalesce((SELECT unit FROM ref.unit_aliases WHERE raw_unit = ?), ?)",
        [latest["unit"], latest["source_unit"], latest["source_unit"]],
    ).fetchone()
    steps = [
        {
            "step": "receive",
            "source": latest["source"],
            "source_file": latest["source_file"],
            "file_hash": latest["file_hash"],
            "received_at": received.get("received_at"),
            "bronze_path": received.get("bronze_path"),
            "source_row": latest["source_row"],
        },
        {
            "step": "map_columns",
            "contract_version": latest["contract_version"],
            "source_values": {
                "subject": latest["source_subject_id"],
                "sample": latest["source_sample_id"],
                "analyte_code": latest["source_analyte_code"],
                "value": latest["source_value"],
                "unit": latest["source_unit"],
            },
        },
        {
            "step": "map_analyte",
            "from": latest["source_analyte_code"],
            "analyte_code": latest["analyte_code"],
            "assay": latest["assay"],
            "measurement_scale": latest["measurement_scale"],
        },
        {
            "step": "convert_unit",
            "from": f"{latest['source_value']} {latest['source_unit']}",
            "factor": factor[0] if factor else None,
            "to": f"{latest['value']} {latest['unit']}",
            "reference_version": latest["reference_version"],
        },
        _identity_step(con, latest),
    ]
    return {
        "record_id": record_id,
        "current_version": latest["version"],
        "steps": steps,
        "history": [{k: v[k] for k in HISTORY_COLUMNS} for v in versions],
    }
