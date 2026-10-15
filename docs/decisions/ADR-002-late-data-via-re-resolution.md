# ADR-002: Late data handled by re-resolving held rows, not by file ordering

Status: Accepted

## Context
Some records depend on data from another source. Example: an Olink proteomics row has only a sample ID;
it is linked to a subject through the Lab A record for that sample. Vendors deliver files in no
guaranteed order, so the Olink file may arrive before the Lab A file.

## Options
1. **Accept the row, mark it `HELD`, and retry resolution at the end of every run.**
2. Enforce ordering: process Olink only after the matching Lab A data is present (dependency scheduling).
3. Reject unresolved rows to the DLQ and rely on manual replay.

## Decision
Option 1.

## Tradeoffs
- Gain: no coupling to delivery order; no file waits on another; late links resolve automatically.
  Held rows keep full lineage and are visible (`status=HELD`), not lost.
- Give up: held rows are excluded from gold until resolved, so analytics can briefly be incomplete.
  Each run spends time re-checking held rows. A sample that never gets a link stays held indefinitely
  and needs a reporting/alerting threshold.
- Option 2 rejected: it needs a dependency graph between deliveries and blocks a whole file on a few
  rows. Option 3 rejected: unresolved is not invalid, and mixing it with real errors hides both.

## Local vs AWS
Same logic. On AWS the re-resolve step runs after each batch of SQS-triggered files or on a schedule.

## At scale
Re-checking every held row each run grows with the held backlog. The evolution is to re-check only
rows whose missing key has just arrived (join new `entity_links` against held keys).

## Revisit when
Held-row count or re-resolve time grows materially, or rows stay held beyond an agreed age.
