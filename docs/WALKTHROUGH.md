# Walkthrough Guide

How the implementation covers each core platform topic: the mechanism, where it lives in the code, the
test that proves it, the demo step that shows it, and the follow-up questions to expect.

The Status column shows what works today; open gaps are in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

![Local prototype architecture](architecture/local-architecture.svg)

The AWS version and how each piece scales: [DESIGN.md §10](DESIGN.md#10-scaling-how-the-design-evolves).

## Overview

| Topic | Mechanism | Code | Tests | Status |
|---|---|---|---|---|
| Unique entity resolution | Registry-based subject links; sample namespaces; sample → subject links; hold, never merge | `transform.py` (`normalize`, `reresolve_held`), `config/registry/` | `test_pipeline_core.py`, `test_late_and_corrected.py` (identity conflict) | Working |
| Idempotent processing | `file_hash` skip; `record_id` + `source_hash` + `derived_hash` | `ingest.py` (`_handle`), `transform.py` (`key_rows`, `version_rows`) | `test_idempotency.py` | Working |
| Data validation | Contract match (quarantine) + rules compiled to SQL (DLQ) + distribution shift (B001) | `ingest.py` (`_pick_contract`), `rules.py`, `transform.py` (`distribution_alerts`) | `test_validation.py`, `test_ground_truth.py` | Working; B002–B004 batch alerts not built (issue #4) |
| Lineage tracking | Lineage columns on every row; run / stage / file metadata; versions with change reason | `lineage.py`, `api.py` | `test_lineage.py`, `test_api.py` | Working |
| Late-arriving / corrected data | Held rows re-resolved every run; `C` → new version; `W` → tombstone; latest received wins | `transform.py` (`version_rows`, `reresolve_held`) | `test_late_and_corrected.py`, `test_pipeline_core.py` | Working |
| Fault tolerance | One file per unit of work in one DB transaction; quarantine; failed file stays in landing | `ingest.py` (`_handle`, `_process_rows`) | `test_fault_tolerance.py` | Working |
| Schema evolution | Versioned contracts; newest matching version wins; unknown layout → quarantine; drift tracked | `config.py` (`select_contract`), `drift.py`, `adapters.py` | `test_schema_evolution.py`, `test_schema_drift_workflow.py` | Working; adopting a new canonical field blocked by issue #5 |
| Replayability | Immutable bronze; deterministic IDs; versioned reference data; arrival order from bronze sidecars | `ingest.py` (`replay`, `dlq_replay`) | `test_replay.py` | Working; no dry-run plan yet (issue #11) |

---

## 1. Unique entity resolution

**Mechanism.** Identifiers are scoped to their source. People are linked across sources only through the
enrollment registry ([ADR-006](decisions/ADR-006-entity-resolution-registry.md)). Samples are identified
within a namespace shared by sources that handle the same physical tube. Proteomics rows reach a person
through the sample link.

**Talking point.** "Merging two people by mistake is far worse than missing a match, so links require
evidence. Every link records where its evidence came from."

**Follow-ups.**
- *What if two sources disagree about a sample's owner?* → `H003` identity conflict: rows held for
  investigation, never auto-merged.
- *What if the registry is wrong or late?* → The corrected registry is a new version. Held rows (`H001`)
  are re-checked at the end of the next run and become `RESOLVED`; already-resolved rows pick up the
  change through `pipeline replay` as `DERIVATION_CHANGE`.
- *Why not probabilistic matching?* → No demographics in the data, and the measurement platform should
  not hold them; at scale it would run in a restricted PII zone.

## 2. Idempotent processing

**Mechanism.** Three levels: the file hash skips duplicate deliveries; `record_id` identifies the
real-world result; `source_hash` and `derived_hash` decide whether anything changed
([DATA_MODEL.md](DATA_MODEL.md) §3).

**Talking point.** "Processing the same input any number of times produces the same result."

**Follow-ups.**
- *Same data inside a different file?* → File hash differs, but record and content hashes match: no write.
- *Why two content hashes?* → To tell vendor corrections apart from our own derivation changes.

## 3. Data validation

**Mechanism.** File level (contract match, readability, reject-rate), row level (structural, then
semantic), batch level (distribution shift, volume, reject spikes). Rules are YAML compiled to SQL
([ADR-007](decisions/ADR-007-validation-sql-rules.md)). Full list in [ERROR_CATALOG.md](ERROR_CATALOG.md).

**Talking point.** "Three levels, four outcomes, every row accounted for. Rules are data, so they move to
Spark or Glue Data Quality without a rewrite."

**Follow-ups.**
- *What can't you detect?* → A plausible but wrong value; only a vendor correction fixes it.
- *Why quarantine a whole file at a high reject rate?* → The "good" rows of a mostly broken file are
  likely affected by the same problem.

## 4. Lineage tracking

**Mechanism.** Every silver row carries source file, file hash, source row, contract, run, and code /
rules / reference / registry versions. Each version records its change reason. Run and stage metadata
record what each run did.

**Talking point.** "Any value can answer: which file, which row, which run, which versions of code and
reference data — and whether the vendor or we changed it."

**Follow-ups.**
- *Storage cost?* → Lineage columns are highly repetitive and compress well in Parquet; the benchmark
  measures the overhead.
- *Standards?* → OpenLineage export is the production step for cross-team lineage.

## 5. Late-arriving and corrected data

**Mechanism.** Rows that cannot be resolved yet are stored as held and re-resolved at the end of every run
([ADR-002](decisions/ADR-002-late-data-via-re-resolution.md)). Corrections (`C`) create a new version;
retractions (`W`) create a tombstone version that removes the value from gold.

**Talking point.** "Late data is handled by retrying, not by ordering. Corrections never overwrite —
history is always queryable."

**Follow-ups.**
- *Retraction arrives before the original?* → `R011` in the DLQ; `dlq replay` after the original arrives.
- *Correction arrives before the original?* → It becomes version 1. The original, received later, wins
  and is flagged `W004` (a final value changed without `C`), so it is visible but not prevented.
- *Vendor changes a value without marking it a correction?* → Accepted as a correction and flagged `W004`.
- *An old file replayed after a correction?* → Skipped for that record: a file only versions a record if
  it was received no earlier than the file behind the current version.

## 6. Fault tolerance

**Mechanism.** Each file is an independent unit of work, written in one database transaction: silver
rows, DLQ rows, sample links and file status commit together or not at all
([ADR-005](decisions/ADR-005-table-format.md)). The landing file is removed only after commit, so a
failed file is retried on the next run. Bad rows go to the DLQ, bad files to quarantine, and processing
failures (`P001`) are retried.

**Talking point.** "Failures are contained at the smallest unit, and retrying is always safe because
processing is idempotent."

**Follow-ups.**
- *Crash halfway through writing?* → The transaction rolls back, so nothing from that file is visible;
  other files still commit, and a rerun matches a clean run (`test_fault_tolerance.py`).
- *On AWS?* → SQS retries, then its dead-letter queue — distinct from the data DLQ.

## 7. Schema evolution

**Mechanism.** Source formats evolve through versioned contracts: an unknown layout is quarantined and
released under a new contract version. The canonical model is long format, so new analytes are new
values, not new columns.

**Talking point.** "Vendor changes are absorbed at the edge; the canonical model and downstream consumers
don't change. The same edge absorbs different formats: a reader per format, chosen by the contract (CSV
and TSV today; a shape step for wide files is designed, not built —
[ADR-010](decisions/ADR-010-pluggable-source-adapters.md))."

**Follow-ups.**
- *The canonical model itself must change?* → Additive change (new nullable column) plus a replay from
  bronze to backfill. History stays null unless the raw files had the column. Before the first adoption,
  issue #5 must be fixed: a new field changes every record's `source_hash`, so a replay would create
  false corrections.
- *Breaking change?* → New column alongside the old, consumers migrate, old one deprecated.

## 8. Replayability

**Mechanism.** Bronze is immutable. Identifiers are deterministic (registry IDs, hashed sample IDs).
Rules, reference data and registry are versioned. Files are processed in `received_at` order.

**Talking point.** "Everything can be rebuilt from raw with identical results. That's how a bug fix is
applied to all of history."

**Follow-ups.**
- *How long does a full replay take at scale?* → Measured by the benchmark; at scale it runs per
  partition in parallel.
- *Replay changes results after a fix — how is that visible?* → New versions with `DERIVATION_CHANGE`.

---

## Demo order

`python demo/run_demo.py --pause` tells one story in 15 checked steps covering all eight topics
(`python demo/inspect_demo.py` shows the tables behind each step):

| Step | What happens | Topic |
|---|---|---|
| 1 | Start from an empty folder | — |
| 2 | EHR, Lab B and Olink delivered; Olink held (H002) | Entity resolution, late data |
| 3 | Lab A links the samples; held rows resolve | Entity resolution, late data |
| 4 | Trace Lab B's CRP to its file and row | Lineage |
| 5 | The same file arrives again | Idempotency |
| 6 | Correction (C) and retraction (W) | Corrected data |
| 7 | Unknown unit → DLQ → alias fix → `dlq replay` | Validation, replayability |
| 8 | New column tracked, then ignored | Schema evolution |
| 9 | Forbidden column → file quarantined (F007) | Schema evolution, validation |
| 10 | Renamed columns → F004 → contract v2 → release | Schema evolution |
| 11 | Silent unit change caught by distribution (B001) | Validation |
| 12 | Crash mid-write (test) | Fault tolerance |
| 13 | Delete the database; replay bronze; gold identical | Replayability |
| 14 | 200 synthetic people checked against an answer key | All, at volume |
| 15 | Runs and events | Operations |

`python demo/run_week_demo.py` plays a week of deliveries: three more labs (C, D, E) onboarded with
configuration only, large proteomics deliveries (446-person cohort), and the week's real-world problems.
Benchmark results are in [docs/benchmarks/](benchmarks/).
