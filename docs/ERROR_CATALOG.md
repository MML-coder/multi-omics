# Error Catalog

Every condition the pipeline detects, its severity, and the test or demo step that proves it is handled.
Codes marked **Not built** are designed but not implemented.
Related: [DESIGN.md](DESIGN.md) §7.6, §9; [DATA_MODEL.md](DATA_MODEL.md).

## Severities

| Severity | Level | Effect | Counted as |
|---|---|---|---|
| `QUARANTINE` | File | Whole file moved to `quarantine/`; no rows ingested | `files_quarantined` |
| `REJECT` | Row | Row written to `dlq/`; not in silver | `rows_dlq` |
| `HOLD` | Row | Row written to silver with `resolution_status = held`; excluded from gold; retried every run | `rows_held` |
| `WARN` | Row | Row accepted; code added to `quality_flags` | `rows_ok` (and `rows_warn`) |
| `FILTER` | Row | Expected non-data row removed; not an error | `rows_filtered` |
| `ALERT` | Batch | Row outcomes unchanged; run reports an alert | Run alerts |
| `INFO` | File | Logged only | Run counters |

Row accounting for every file: `rows_in = rows_ok + rows_dlq + rows_held + rows_filtered`.
Wide or nested files (planned) would be counted after reshaping to long form (one per value cell or record).

## Evaluation order

1. File checks (F-codes). Any failure → quarantine; stop.
2. Row structural checks: required fields, types, dates, enumerations. A failure skips step 3 for that row
   (a range cannot be checked on a non-number).
3. Row semantic checks: analyte, unit, ranges, retraction target.
4. Within-file checks: duplicates and conflicts.
5. Identity resolution (H-codes).
6. File reject-rate check (`F005`).
7. Batch checks across runs (B-codes).

A row can fail several rules; every failure is recorded in the DLQ (one entry per code), and the row is
counted once. Severity precedence for a row: `REJECT` > `HOLD` > `WARN`.

---

## File level

| Code | Name | Severity | Condition | Recovery | Proved by |
|---|---|---|---|---|---|
| `I001` | DUPLICATE_FILE | INFO | `file_hash` already ingested | None needed | `test_idempotency.py`; demo step 5 |
| `F001` | UNKNOWN_SOURCE | QUARANTINE | File not in a configured source folder | Configure the source | `test_validation.py` |
| `F002` | UNREADABLE_FILE | QUARANTINE | Not parseable as CSV, or invalid encoding | Vendor resend | Built; no dedicated test |
| `F003` | EMPTY_FILE | QUARANTINE | Header only, or zero bytes | Vendor resend | `test_validation.py` |
| `F004` | CONTRACT_MISMATCH | QUARANTINE | Columns match no known contract version for the source | Add a contract version → `quarantine release` | `test_schema_evolution.py`; demo step 10 |
| `F005` | HIGH_REJECT_RATE | QUARANTINE | Share of rejected rows exceeds threshold (illustrative: 50%) | Investigate; vendor resend or rule fix → release | `test_fault_tolerance.py` |
| `F006` | SCHEMA_DRIFT | ALERT (file ingested) | Columns not mapped, ignored or forbidden by the contract; tracked in `meta.schema_drift` as OPEN | Decide: adopt / `ignored_columns` / `forbidden_columns` (README "Schema change workflow") | `test_schema_drift_workflow.py`; demo step 8 |
| `F007` | FORBIDDEN_COLUMN | QUARANTINE | File contains a column listed in `forbidden_columns` | Vendor resends without it; security handles received copies | `test_schema_drift_workflow.py`; demo step 9 |

`F005` exists because partially ingesting a mostly broken file is riskier than holding the whole file:
the remaining rows are likely affected by the same problem.

## Row level: reject

| Code | Name | Condition | Recovery | Proved by |
|---|---|---|---|---|
| `R001` | MISSING_REQUIRED | Required field empty | Vendor resend | `test_validation.py` |
| `R002` | INVALID_NUMBER | Value not numeric | Vendor resend | `test_validation.py` |
| `R003` | INVALID_DATE | Date or timestamp not parseable | Vendor resend | `test_validation.py` |
| `R004` | FUTURE_DATE | Observed time later than file receipt | Vendor resend | `test_validation.py` |
| `R005` | UNKNOWN_ANALYTE | Source code not in `analyte_aliases` | Add alias (reviewed) → `dlq replay` | `test_validation.py` |
| `R006` | UNKNOWN_UNIT | Unit not in `unit_conversions` for the analyte's canonical unit | Add conversion or alias → `dlq replay` | `test_validation.py`, `test_replay.py`; demo step 7 |
| `R007` | UNIT_ANALYTE_MISMATCH | Unit belongs to a different dimension (e.g. CRP in `bpm`) | Vendor resend | `test_validation.py` |
| `R008` | OUT_OF_RANGE_HARD | Normalized value outside physically possible range | Vendor resend | `test_validation.py` |
| `R009` | INVALID_ENUM | `result_status` or `QC_Flag` not an allowed value | Vendor resend | Built; no dedicated test |
| `R010` | CONFLICTING_DUPLICATE | Same `record_id` twice in one file with different content; both rejected | Vendor resend | `test_validation.py` |
| `R011` | RETRACTION_OF_UNKNOWN | Status `W` for a record never ingested | Replay after original arrives | `test_late_and_corrected.py` (`lab_a_retract_unknown.csv`) |
| `R012` | CENSORED_VALUE | Value like `<0.3` or `>200` (out of scope, [ADR-008](decisions/ADR-008-data-realism.md)) | Future support | **Not built** (out of scope) |

## Row level: hold

| Code | Name | Condition | Resolution | Proved by |
|---|---|---|---|---|
| `H001` | SUBJECT_NOT_IN_REGISTRY | Source subject ID has no registry entry | Registry update; held rows are re-checked at the end of every run | Week demo (`run_week_demo.py`) |
| `H002` | SAMPLE_NOT_LINKED | Sample (e.g. proteomics) has no link to a subject yet | Linking file arrives → automatic on next run | `test_pipeline_core.py`; demo steps 2–3 |
| `H003` | IDENTITY_CONFLICT | Sample linked to two different subjects | Human investigation and a corrected source file; never resolved automatically | `test_late_and_corrected.py` (`lab_a_sample_conflict.csv`); week demo |

## Row level: warn

| Code | Name | Condition | Proved by |
|---|---|---|---|
| `W001` | OUT_OF_RANGE_SOFT | Possible but implausible value | `test_validation.py` |
| `W002` | VENDOR_QC_WARN | Proteomics `QC_Flag = WARN` | `test_pipeline_core.py` (sample data S101 / TNF) |
| `W003` | PRELIMINARY_RESULT | Lab `result_status = P` | `test_late_and_corrected.py` |
| `W004` | UNFLAGGED_CHANGE | Known record, new value, but status is not `C` | `test_late_and_corrected.py` (`lab_a_unflagged_change.csv`) |
| `W005` | EXACT_DUPLICATE_ROW | Same record and same content twice in one file; one kept, the other counted as filtered | Built; no dedicated test |

## Row level: filter

| Code | Name | Condition | Proved by |
|---|---|---|---|
| `X001` | CONTROL_SAMPLE | Proteomics row for a control / calibration sample (identified by configured ID pattern) | Built; fixture `olink_with_controls.csv`, no dedicated test |
| `X002` | NOT_REPORTED | Empty value cell in a wide file (analyte not reported for that sample) | **Not built** (wide files are designed, not built) |

## Batch level: alert

| Code | Name | Condition | Proved by |
|---|---|---|---|
| `B001` | DISTRIBUTION_SHIFT | Per source and analyte, median of normalized values differs from the trailing baseline by more than a configured factor | `test_validation.py`; demo step 11 |
| `B002` | VOLUME_ANOMALY | Rows per file far outside the source's trailing range | **Not built** (known issue #4) |
| `B003` | REJECT_RATE_SPIKE | Reject rate above baseline but below the `F005` threshold | **Not built** (known issue #4) |
| `B004` | HELD_AGE_EXCEEDED | A held row older than the configured age | **Not built** (known issue #4) |

Batch thresholds are configuration; the values used are illustrative and documented as such.

## Not a data error

| Code | Name | Meaning |
|---|---|---|
| `P001` | PROCESSING_FAILURE | Crash, timeout or bug. The transaction rolls back, the file stays in landing with no file record, and it is retried on the next run; on AWS the SQS message is retried, then moved to the SQS dead-letter queue. Fixed by fixing code, not data. |

---

## Correction scenarios (not errors)

| Scenario | Outcome | Proved by |
|---|---|---|
| Corrected value, status `C` | New version, `SOURCE_CORRECTION` | `lab_a_correction.csv`; demo step 6 |
| Withdrawn result, status `W` | Tombstone version, `RETRACTION`; removed from gold | `lab_a_retraction.csv`; demo step 6 |
| Reference data fix then replay | New version, `DERIVATION_CHANGE` | `test_replay.py` |
| Held row resolved | New version, `RESOLVED` | `olink_sample.csv` then `lab_a_sample.csv`; demo steps 2–3 |
