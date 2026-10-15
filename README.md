# multi-omics-pipeline

A prototype data pipeline that ingests, validates, normalizes and exposes multi-source
biological measurement data (clinical vitals, lab results, proteomics) while preserving
source-to-canonical provenance.

**Status:** Milestone 1 working — the brief's four sources flow end to end (ingest → validate →
normalize → gold → API) with idempotency, lineage and entity resolution. See
[docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for what works per topic.

**Demo video:** a 27-minute walkthrough of the design and the pipeline running end to end —
[download (MP4, 1080p, 853 MB)](https://github.com/MML-coder/multi-omics/releases/download/demo-video/multi-omics-demo.mp4)
· [release page](https://github.com/MML-coder/multi-omics/releases/tag/demo-video).

## Documentation

- [DESIGN.md](docs/DESIGN.md) — problem, requirements, architecture, scaling, AWS
- [DATASETS.md](docs/DATASETS.md) — the source datasets
- [DATA_MODEL.md](docs/DATA_MODEL.md) — canonical model, identity, versioning
- [ERROR_CATALOG.md](docs/ERROR_CATALOG.md) — every detected condition and its handling
- [WALKTHROUGH.md](docs/WALKTHROUGH.md) — how each core platform topic is implemented, tested and demoed
- [KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — gaps found during review, with severity and status
- [decisions/](docs/decisions/) — architecture decision records

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
pipeline init                                   # create data/landing, data/bronze, data/quarantine

# deliver files: one folder per source under data/landing/
for s in ehr_vitals lab_a lab_b olink; do
  mkdir -p data/landing/$s && cp tests/fixtures/${s}_sample.csv data/landing/$s/
done

pipeline run                                    # process new files; prints row accounting
pipeline run                                    # again: nothing changes (idempotent)
pipeline status                                 # recent runs
pipeline serve                                  # API on http://127.0.0.1:8000/docs
```

API examples:

```bash
curl "localhost:8000/measurements?subject_id=G-0001&analyte_code=CRP"
curl "localhost:8000/measurements/<record_id>/lineage"
curl "localhost:8000/runs"
```

Tests: `pytest`

Demos (both are end-to-end acceptance tests, run in CI; add `--pause` to step through live):

| Demo | Story |
|---|---|
| `python demo/run_demo.py` | The brief's four sources: 15 checked steps covering every core topic |
| `python demo/run_week_demo.py` | One week, 5 labs + proteomics + EHR: onboarding by config, late proteomics, resends, new / renamed / missing columns, bad units, unregistered people, corrections, retractions, an identity conflict, forbidden personal data, a silent unit change, lineage, rebuild from bronze. `--from-day N` jumps to a day. Input files are readable under `demo/week/`. |

### Event-driven demo: files trigger the pipeline

`demo/run_demo_record.py` tells the story in 8 steps and shows every result through the API: two labs and
entity resolution, proteomics linked through samples, a correction and a withdrawal, a resent file, unit
problems, a new column, a breaking change (quarantine → review → release), and onboarding a new lab with
50 rows by configuration only.

In watch mode, a watcher runs the pipeline whenever a file lands, the local stand-in for S3 event →
queue → worker. Use two terminals.

**Terminal 1 — start the watcher first:**

```bash
PIPELINE_DATA_DIR=data/record PIPELINE_CONFIG_DIR=data/record-config pipeline watch
```

**Terminal 2 — run the demo:**

```bash
python demo/run_demo_record.py --pause --watch
```

Press Enter to move to the next step. The demo drops files into `data/record/landing/`, waits for the
watcher's run, then calls the API (started on port 8000; `--port` to change). Stop the watcher with Ctrl-C.

Without the watcher, `python demo/run_demo_record.py --pause` runs the pipeline itself after each delivery.
The demo never deletes data; before running it again:

```bash
rm -rf data/record data/record-config
```

Dashboard (after the week demo):

```bash
pip install -e ".[ui]"
streamlit run app/dashboard.py -- --data-dir data/week
```

Three tabs: one person across all sources (absolute values in one unit; proteomics kept separate; lineage of
any value), sources and harmonization (what each vendor sends vs what we store), and operations (runs, events,
schema-drift decisions, DLQ, quarantine, time per stage).

## Layout

```
config/            source contracts, reference data, enrollment registry (versioned inputs)
src/omics_pipeline/
  ingest.py        run orchestration: file registration, quarantine, per-file transaction
  adapters.py      readers (csv, tsv) and contract-driven column mapping
  rules.py         row validation compiled to SQL
  transform.py     normalization, identity resolution, versioning
  gold.py          gold tables rebuilt from silver
  lineage.py       source-to-canonical lineage for one record
  api.py           read-only FastAPI access layer
tests/             behavior tests and fixtures (brief sample files)
```

## Schema change workflow

What happens when a vendor changes its file layout, and what people do about it.

### What the pipeline does automatically

| The file… | Pipeline action | Signal |
|---|---|---|
| matches its contract | ingest | exit code 0 |
| matches, plus columns the contract doesn't know | ingest; extra columns kept in bronze only; column tracked as `OPEN` | `SchemaDriftDetected F006` (medium), exit code 2 |
| contains a column listed in `forbidden_columns` | quarantine; nothing ingested | `FileQuarantined F007` (high), exit code 2 |
| lacks a required column (renamed or dropped) | quarantine; nothing ingested | `FileQuarantined F004` (high), exit code 2 |

Inspect with `pipeline events`, `pipeline drift`, `pipeline quarantine list`.

### What people decide for a new column (`F006`)

Triage first: ask the vendor what the column is and whether it is permanent. Record the decision in a
"Schema drift decision" issue. Then one of:

**A. Adopt** — we want the data
1. ADR for any new canonical field (name, allowed values, optional).
2. Pull request: model change (if needed) and a new contract version mapping the column, e.g. `lab_a_v2.yaml`.
3. CI passes; merge; CD deploys the config.
4. Back-fill from bronze: `pipeline replay --source lab_a`.
5. Verify: `pipeline drift` shows `ADOPTED` (decided in `lab_a_v2`); new rows carry `contract_version = lab_a_v2`.

**Adopt end to end — example: Lab A adds `specimen_type`**

```
check-file / F006 ─▶ issue: Adopt ─▶ ADR (field, allowed values, optional)
                                        │
             PR: model + lab_a_v2.yaml (column mapped, optional) + tests
                                        │  CI green, merge, CD deploys
                                        ▼
     new files ──▶ read under lab_a_v2 ──▶ specimen_type in silver; drift = ADOPTED
     old files in bronze ──▶ pipeline replay --source lab_a ──▶ back-filled
                                        (files without the column: specimen_type stays NULL)
```

- No canonical field exists for it today, so adopting is a code change (model, silver column, validation,
  gold/API) plus a config change.
- If the contract is deployed before the first such file arrives, there is no warning and no back-fill.
- Open design points (to resolve before the first adoption):
  - Back-filled versions must be labelled `DERIVATION_CHANGE` (our extraction), not `SOURCE_CORRECTION`.
  - Adding a field to the vendor fingerprint (`source_hash`) must not change fingerprints of records where
    the field is empty; otherwise a replay creates false "corrections" for every record.

**B. Ignore** — we don't need it
1. Pull request adding one line to the contract, with the reason:
   `ignored_columns: [specimen_type]   # 2026-02-10, issue #12: not used`
2. CI passes; merge; CD deploys.
3. Verify: `pipeline drift` shows `IGNORED`; no more `F006` for that column. Values remain in bronze, so
   adopting later is still possible.

**C. Forbid** — we must not receive it (e.g. personal data)
1. Notify security; agree how already-received copies (bronze, quarantine) are handled.
2. Pull request: `forbidden_columns: [patient_name]`; merge and deploy urgently.
3. Verify: files containing it are quarantined (`F007`); `pipeline drift` shows `FORBIDDEN`.
4. Vendor resends without the column; resends ingest normally.

**A renamed or dropped required column (`F004`)** follows path A without the replay: new contract version →
`pipeline quarantine release <hash>` → `pipeline run`.

Check any vendor file before deploying a contract change — nothing is ingested:

```bash
pipeline check-file --source lab_a path/to/new_file.csv
```

### How it runs in production (GitHub + AWS)

```
pipeline run ─▶ event (F006 / F004 / F007) ─▶ event bus ─▶ alert + "Schema drift decision" issue
                                                                   │
          engineer opens a pull request (contract / ignored / forbidden / ADR) ◀┘
                                   │
                  CI (.github/workflows/ci.yml): tests + contract checks on sample files
                                   │  review (source owner + data platform)
                                   ▼  merge to main
                  CD (.github/workflows/cd.yml): package + versioned config bundle
                                   │  deploy job publishes config to S3 (when AWS is configured)
                                   ▼
          next pipeline run reads the new config ─▶ `meta.schema_drift` status updates ─▶ issue closed
          (adopt only: run `pipeline replay --source <source>` to back-fill history from bronze)
```

The CD AWS job is skipped until the repository variables `AWS_DEPLOY_ROLE_ARN` and `CONFIG_BUCKET` are set
(OIDC role; no stored AWS keys).

## Intentionally omitted

Documented as production steps rather than built (see [DESIGN.md](docs/DESIGN.md) §6, §14):

- Probabilistic entity resolution (no demographics in the data; registry-based linking instead)
- Olink batch-bridging normalization (scientific normalization, downstream of the platform)
- Manual data overrides (would break provenance; fixes go through vendor resends or reviewed rules)
- Censored lab values such as `<0.3` (rejected and documented)
- Standard codes (LOINC, HMDB) in reference data
- Streaming ingestion, authentication / authorization on the API, live AWS deployment
