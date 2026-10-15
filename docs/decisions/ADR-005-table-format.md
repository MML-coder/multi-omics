# ADR-005: Storage — one DuckDB database locally, Iceberg on AWS

Status: Accepted (revised 2026-10-10; originally "plain Parquet + metadata tables locally")

## Context
Silver must support versioned records (corrections, retractions), atomic commits and replay. Gold must be
rebuildable. Run metadata, the DLQ and identity links must change in step with silver: if a file's rows
are written, its status, rejects and links must be written too, or none of them. The local choice should
not block the move to AWS.

## Options
1. **Raw files on disk (landing, bronze, quarantine); silver, gold, DLQ and metadata as tables in one
   DuckDB database file. Iceberg on AWS.**
2. Plain Parquet files for silver and gold plus a separate metadata database (the original decision).
3. DuckLake (Parquet data, SQL-database catalog) locally and on AWS.
4. Iceberg locally and on AWS.

## Decision
Option 1.

## Tradeoffs
- Gain: one transaction per file covers silver rows, DLQ rows, identity links and file status together,
  so a failure leaves nothing half-written — fault tolerance without hand-built temp-file-and-rename
  commits. Less code to write and explain. Raw files stay as files, so bronze remains immutable and
  replayable.
- Give up: silver and gold are not visible as files on disk (they can be exported with `COPY … TO`
  Parquet). DuckDB allows one writer process; the API reads with short read-only connections, so it can
  briefly fail to connect while a run is writing.
- Option 2 rejected after review: commits spanning Parquet files and a separate database need custom
  atomicity code; a reference implementation reviewed during design showed how easily partial commits slip
  in.
- Option 3: DuckLake reached v1.0 in 2026 and would give transactions with Parquet storage; not chosen
  because its ecosystem is smaller and AWS-native engine support is unverified.
- Option 4: DuckDB's Iceberg writes require a REST catalog and are still maturing; heavy for a prototype.

## Local vs AWS
Locally: `data/warehouse.duckdb` with schemas `meta`, `ref`, `silver`, `gold`; raw files under `data/`.
AWS: raw files in S3; silver and gold as Iceberg tables in the Glue catalog (corrections via Athena
`MERGE INTO`, periodic `OPTIMIZE`); run metadata and DLQ index in Postgres.

## At scale
A single local database file is a Stage 1 choice (DESIGN §10.2). Concurrent workers need a multi-writer
store: Postgres for metadata and a table format on object storage for silver and gold.

## Revisit when
More than one writer process is needed, or the team standardizes on DuckDB for production (favoring
DuckLake).
