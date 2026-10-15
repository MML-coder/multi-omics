# ADR-003: Gold is fully rebuilt each run in the prototype

Status: Accepted

## Context
Gold tables present the current version of each record (excluding held rows) in query-friendly shapes.
After each run, gold must reflect new, corrected and newly resolved silver records.

## Options
1. **Rebuild all gold tables from silver after every run.**
2. Incrementally update only the partitions or records affected by the run.

## Decision
Option 1 for the prototype.

## Tradeoffs
- Gain: simple and deterministic; gold is always a pure function of silver, so it can never drift from
  silver and needs no change tracking. Easy to test (rebuild → compare hashes).
- Give up: rebuild cost grows with total history, not with the size of the change. Wasteful once silver
  is large.

## Local vs AWS
Same approach initially (Athena CTAS or a Spark job). With Iceberg, incremental updates via `MERGE` on
affected partitions become straightforward.

## At scale
This is expected to be one of the first things to break. The benchmark records gold rebuild time at
each volume step; incremental rebuild by partition (source, date) replaces full rebuild when needed.

## Revisit when
Gold rebuild time exceeds the run's time budget, or becomes a large share of total run time.
