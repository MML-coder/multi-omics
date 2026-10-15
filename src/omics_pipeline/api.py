"""Unified access layer: read-only API over gold, with lineage (DESIGN.md §7.7, ADR-001).

Each request opens a short read-only connection, so the API never holds the warehouse lock that a
pipeline run needs. While a run is writing, requests get 503 and can be retried (ADR-005 tradeoff).
"""

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from omics_pipeline import __version__
from omics_pipeline.layout import Layout, resolve_layout
from omics_pipeline.lineage import measurement_lineage

MAX_PAGE = 1000


class Measurement(BaseModel):
    record_id: str
    version: int
    global_subject_id: str | None
    global_sample_id: str | None
    domain: str
    source: str
    analyte_code: str
    assay: str
    value: float
    unit: str
    measurement_scale: str
    observed_at: datetime | None
    observed_at_precision: str | None
    result_status: str | None
    qc_flag: str | None
    quality_flags: list[str]


class Run(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    files_in: int
    files_committed: int
    files_quarantined: int
    files_duplicate: int
    files_failed: int
    rows_in: int
    rows_ok: int
    rows_warn: int
    rows_dlq: int
    rows_held: int
    rows_filtered: int
    rows_resolved: int


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cursor = con.execute(sql, params or [])
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def create_app(layout: Layout | None = None) -> FastAPI:
    layout = layout or resolve_layout()
    app = FastAPI(
        title="Multi-source measurement API",
        version=__version__,
        description="Harmonized measurements with source-to-canonical lineage.",
    )

    def connection() -> Iterator[duckdb.DuckDBPyConnection]:
        if not Path(layout.warehouse).exists():
            raise HTTPException(503, "No warehouse yet: run `pipeline run` first")
        try:
            con = duckdb.connect(str(layout.warehouse), read_only=True)
        except duckdb.IOException as exc:
            raise HTTPException(503, f"Warehouse busy (a pipeline run is writing): {exc}") from exc
        try:
            yield con
        finally:
            con.close()

    @app.get("/health", tags=["ops"])
    def health(con: duckdb.DuckDBPyConnection = Depends(connection)) -> dict:
        last_run = _fetch(con, "SELECT run_id, status, finished_at FROM meta.pipeline_runs ORDER BY run_id DESC LIMIT 1")
        return {
            "status": "ok",
            "measurements": con.execute("SELECT count(*) FROM gold.measurement_current").fetchone()[0],
            "last_run": last_run[0] if last_run else None,
        }

    @app.get("/sources", tags=["ops"])
    def sources(con: duckdb.DuckDBPyConnection = Depends(connection)) -> list[dict]:
        return _fetch(con, """
            SELECT f.source, count(DISTINCT f.file_hash) FILTER (WHERE f.status = 'COMMITTED') AS files_committed,
                   count(DISTINCT f.file_hash) FILTER (WHERE f.status = 'QUARANTINED') AS files_quarantined,
                   max(f.received_at) AS last_received_at,
                   (SELECT count(*) FROM gold.measurement_current g WHERE g.source = f.source) AS measurements
            FROM meta.ingested_files f GROUP BY f.source ORDER BY f.source
        """)

    @app.get("/runs", response_model=list[Run], tags=["ops"])
    def runs(
        limit: int = Query(20, ge=1, le=MAX_PAGE), con: duckdb.DuckDBPyConnection = Depends(connection)
    ) -> list[dict]:
        return _fetch(con, "SELECT * FROM meta.pipeline_runs ORDER BY run_id DESC LIMIT ?", [limit])

    @app.get("/measurements", response_model=list[Measurement], tags=["data"])
    def measurements(
        subject_id: str | None = None,
        analyte_code: str | None = None,
        source: str | None = None,
        measurement_scale: str | None = None,
        observed_from: date | None = None,
        observed_to: date | None = None,
        limit: int = Query(100, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0),
        con: duckdb.DuckDBPyConnection = Depends(connection),
    ) -> list[dict]:
        filters = {
            "global_subject_id = ?": subject_id,
            "analyte_code = ?": analyte_code,
            "source = ?": source,
            "measurement_scale = ?": measurement_scale,
            "observed_at >= ?": observed_from,
            "observed_at < ? + INTERVAL 1 DAY": observed_to,
        }
        active = {clause: value for clause, value in filters.items() if value is not None}
        where = " AND ".join(active) or "TRUE"
        return _fetch(
            con,
            f"SELECT * FROM gold.measurement_current WHERE {where} "
            "ORDER BY global_subject_id, observed_at, analyte_code, record_id LIMIT ? OFFSET ?",
            [*active.values(), limit, offset],
        )

    @app.get("/measurements/{record_id}", response_model=Measurement, tags=["data"])
    def measurement(record_id: str, con: duckdb.DuckDBPyConnection = Depends(connection)) -> dict:
        rows = _fetch(con, "SELECT * FROM gold.measurement_current WHERE record_id = ?", [record_id])
        if not rows:
            raise HTTPException(404, f"No current measurement {record_id}")
        return rows[0]

    @app.get("/measurements/{record_id}/lineage", tags=["provenance"])
    def lineage(record_id: str, con: duckdb.DuckDBPyConnection = Depends(connection)) -> dict:
        result = measurement_lineage(con, record_id)
        if result is None:
            raise HTTPException(404, f"No record {record_id}")
        return result

    return app


app = create_app()
