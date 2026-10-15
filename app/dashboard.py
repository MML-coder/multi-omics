"""Demo dashboard: the harmonized data and the platform's operations, visually.

    streamlit run app/dashboard.py -- --data-dir data/week

Simple by default (the story in one screen per tab); details are one click away. Reads the same gold,
silver and metadata tables the API serves, through short read-only connections (so a pipeline run can
still write). For the demo only; applications use the API.
"""

import argparse
import os
import sys
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omics_pipeline.lineage import measurement_lineage  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", default=os.environ.get("PIPELINE_DATA_DIR", "data"))
DATA_DIR = Path(parser.parse_known_args()[0].data_dir)
WAREHOUSE = DATA_DIR / "warehouse.duckdb"
COHORT_START = "G-1000"          # demo convention: synthetic cohort people are G-1001 and up
FEATURED_PROTEINS = ("CRP", "TNF")

st.set_page_config(page_title="Measurement platform", layout="wide")


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


st.title("Multi-source measurement platform")
if not WAREHOUSE.exists():
    st.error("No warehouse yet. Run the pipeline (e.g. `python demo/run_week_demo.py`) first.")
    st.stop()

with st.sidebar:
    st.header("View")
    show_cohort = st.toggle("Include synthetic cohort (446 people)", value=False)
    show_panel = st.toggle("Full proteomics panel (50 proteins)", value=False)
    st.caption(f"Warehouse: `{WAREHOUSE}` (read-only). Refresh after a pipeline run.")

person_tab, sources_tab, ops_tab = st.tabs(["One person, all sources", "Sources", "Operations"])

# ------------------------------------------------------------------------------------ person view
with person_tab:
    people = query(
        "SELECT global_subject_id, sources FROM gold.dim_subject "
        + ("" if show_cohort else "WHERE global_subject_id < ? ") + "ORDER BY global_subject_id",
        [] if show_cohort else [COHORT_START])
    if people.empty:
        st.info("No resolved measurements yet.")
        st.stop()
    labels = {f"{r.global_subject_id} — {', '.join(r.sources)}": r.global_subject_id for r in people.itertuples()}
    person = labels[st.selectbox("Person", list(labels))]
    rows = query(
        "SELECT g.record_id, g.source, g.analyte_code, g.value, g.unit, g.measurement_scale, g.observed_at, "
        "s.source_value || ' ' || coalesce(s.source_unit, '') AS as_sent "
        "FROM gold.measurement_current g JOIN silver.measurement s USING (record_id, version) "
        "WHERE g.global_subject_id = ? ORDER BY g.observed_at, g.source", [person])
    absolute = rows[rows.measurement_scale == "absolute"]
    relative = rows[rows.measurement_scale != "absolute"]
    if not show_panel:
        relative = relative[relative.analyte_code.isin(FEATURED_PROTEINS)]

    left, right = st.columns(2)
    with left:
        st.subheader("Lab values, one unit")
        if absolute.empty:
            st.info("No lab values for this person.")
        else:
            analytes = sorted(absolute.analyte_code.unique())
            analyte = st.selectbox("Analyte", analytes, index=analytes.index("CRP") if "CRP" in analytes else 0)
            data = absolute[absolute.analyte_code == analyte]
            st.altair_chart(
                alt.Chart(data).mark_point(size=160, filled=True).encode(
                    x=alt.X("observed_at:T", title="drawn on"),
                    y=alt.Y("value:Q", title=f"{analyte} ({data.unit.iloc[0]})"),
                    color=alt.Color("source:N", title="lab"),
                    tooltip=["source", "value", "unit", "as_sent", "observed_at"],
                ),
                width="stretch",
            )
            st.caption("Each lab sent its own unit (hover: 'as sent'); all shown in one unit.")
    with right:
        st.subheader("Proteomics, kept separate")
        if relative.empty:
            st.info("No proteomics for this person.")
        else:
            st.altair_chart(
                alt.Chart(relative).mark_bar().encode(
                    x=alt.X("analyte_code:N", title="protein", sort="-y"),
                    y=alt.Y("value:Q", title="NPX (relative, log2)"),
                    tooltip=["analyte_code", "value", "observed_at"],
                ),
                width="stretch",
            )
            st.caption("Relative scale: never converted to mg/L, never on the same chart.")

    st.subheader("Where did a value come from?")
    options = {f"{r.source} · {r.analyte_code} = {r.value:g} {r.unit} (sent as {r.as_sent})": r.record_id
               for r in rows[rows.analyte_code.isin(FEATURED_PROTEINS) | (rows.measurement_scale == "absolute")].itertuples()}
    if options:
        record_id = options[st.selectbox("Value", list(options))]
        con = duckdb.connect(str(WAREHOUSE), read_only=True)
        try:
            lineage = measurement_lineage(con, record_id)
        finally:
            con.close()
        for step in lineage["steps"]:
            details = " · ".join(f"**{k}**: {v}" for k, v in step.items() if k != "step" and v is not None)
            st.markdown(f"- `{step['step']}` — {details}")
        with st.expander(f"Version history ({len(lineage['history'])})"):
            st.dataframe(pd.DataFrame(lineage["history"]), width="stretch", hide_index=True)
    with st.expander(f"All current measurements for {person} ({len(rows)})"):
        st.dataframe(rows.drop(columns=["record_id"]), width="stretch", hide_index=True)

# ----------------------------------------------------------------------------------- sources view
with sources_tab:
    st.subheader("What each lab sends, and what we store")
    st.dataframe(query("""
        SELECT source AS lab, source_analyte_code AS sent_code, source_unit AS sent_unit,
               analyte_code AS stored_as, unit AS stored_unit
        FROM silver.measurement
        WHERE measurement_scale = 'absolute' OR source_analyte_code IN ('P02741', 'P01375')
        GROUP BY ALL ORDER BY lab
    """), width="stretch", hide_index=True)
    with st.expander("Measurements and people per source"):
        st.dataframe(query("SELECT source, count(*) AS measurements, count(DISTINCT global_subject_id) AS people "
                           "FROM gold.measurement_current GROUP BY ALL ORDER BY source"), width="stretch", hide_index=True)

# -------------------------------------------------------------------------------- operations view
with ops_tab:
    runs = query("SELECT * FROM meta.pipeline_runs ORDER BY run_id")
    events = query("SELECT run_id, severity, code, source, file_name, detail FROM meta.events ORDER BY raised_at")
    open_dlq = query("SELECT source, error_code, raw_value, source_file, source_row FROM meta.dlq WHERE status = 'OPEN'")
    quarantined = query("SELECT source, original_name, quarantine_code, quarantine_reason FROM meta.ingested_files "
                        "WHERE status = 'QUARANTINED' ORDER BY received_at")
    accounting_ok = bool(not runs.empty and
                         (runs.rows_in == runs.rows_ok + runs.rows_dlq + runs.rows_held + runs.rows_filtered).all())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runs", len(runs))
    c2.metric("Row accounting", "OK" if accounting_ok else "VIOLATED")
    c3.metric("Open DLQ rows", len(open_dlq))
    c4.metric("Quarantined files", len(quarantined))

    st.subheader("What needed attention")
    st.dataframe(events[["severity", "code", "source", "file_name", "detail"]], width="stretch", hide_index=True)

    with st.expander("Runs"):
        st.dataframe(runs[["run_id", "run_type", "files_committed", "files_quarantined", "files_duplicate", "rows_in",
                           "rows_ok", "rows_dlq", "rows_held", "rows_resolved"]], width="stretch", hide_index=True)
    with st.expander("Schema-drift decisions"):
        st.dataframe(query("SELECT source, column_name, status, decided_in FROM meta.schema_drift ORDER BY source"),
                     width="stretch", hide_index=True)
    with st.expander("Open DLQ rows and quarantined files"):
        st.dataframe(open_dlq, width="stretch", hide_index=True)
        st.dataframe(quarantined, width="stretch", hide_index=True)
    with st.expander("Time per stage"):
        st.bar_chart(query("SELECT stage, round(sum(duration_ms) / 1000, 3) AS seconds FROM meta.run_stage_metrics "
                           "GROUP BY stage ORDER BY seconds DESC"), x="stage", y="seconds")
