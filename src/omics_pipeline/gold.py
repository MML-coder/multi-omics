"""Gold layer: rebuilt from silver after every run (ADR-003, DATA_MODEL.md §5)."""

import duckdb

GOLD_SQL = """
CREATE OR REPLACE TABLE gold.measurement_current AS
SELECT record_id, version, global_subject_id, global_sample_id, domain, source, analyte_code, assay,
       value, unit, measurement_scale, observed_at, observed_at_precision, result_status, qc_flag,
       quality_flags
FROM silver.measurement
QUALIFY row_number() OVER (PARTITION BY record_id ORDER BY version DESC) = 1
    AND NOT is_retraction
    AND resolution_status = 'resolved';

CREATE OR REPLACE TABLE gold.dim_subject AS
SELECT global_subject_id,
       min(observed_at) AS first_observed_at,
       max(observed_at) AS last_observed_at,
       count(*) AS measurements,
       count(*) FILTER (WHERE domain = 'operational') AS operational_measurements,
       count(*) FILTER (WHERE domain = 'scientific') AS scientific_measurements,
       list(DISTINCT source ORDER BY source) AS sources
FROM gold.measurement_current
GROUP BY global_subject_id;

CREATE OR REPLACE TABLE gold.dim_sample AS
SELECT l.global_sample_id, l.global_subject_id, l.collected_at, l.evidence_source,
       list(DISTINCT m.source ORDER BY m.source) FILTER (WHERE m.source IS NOT NULL) AS measured_by
FROM meta.sample_links l
LEFT JOIN gold.measurement_current m ON m.global_sample_id = l.global_sample_id
GROUP BY ALL;

CREATE OR REPLACE TABLE gold.dim_analyte AS
SELECT an.analyte_code, an.name, an.uniprot_id, al.source, al.source_code, al.assay,
       al.measurement_scale, al.canonical_unit
FROM ref.analytes an
JOIN ref.aliases al ON al.analyte_code = an.analyte_code;
"""


def rebuild(con: duckdb.DuckDBPyConnection) -> int:
    con.execute(GOLD_SQL)
    return con.execute("SELECT count(*) FROM gold.measurement_current").fetchone()[0]
