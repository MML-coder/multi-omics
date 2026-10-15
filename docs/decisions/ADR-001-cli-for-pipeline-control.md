# ADR-001: Pipeline control through a CLI, not the API

Status: Accepted

## Context
Operators need to run ingestion, inspect failures, and trigger reprocessing (DLQ replay, source replay,
gold rebuild). Consumers need to read harmonized data. Both could be served by one HTTP API.

## Options
1. **CLI for control, read-only API for data.**
2. One API with both read and control endpoints (`POST /runs`, `POST /dlq/replay`).
3. An orchestrator UI (Dagster, Airflow) for control.

## Decision
Option 1.

## Tradeoffs
- Gain: a data consumer can never trigger reprocessing; the API surface stays small and read-only, which
  simplifies authorization. The CLI is easy to demo and script, and is the same entry point a scheduler
  would invoke.
- Give up: no remote, self-service way to trigger runs; operators need shell access to where the
  pipeline runs. No UI for run history beyond `pipeline status` and the metadata tables.

## Local vs AWS
The same CLI runs as one-off ECS tasks; scheduled and multi-step operations (backfills, replays) are
wrapped in Step Functions. Automated ingestion is triggered by S3 → SQS, not by a person.

## At scale
Holds. As the number of operators and sources grows, an orchestrator UI (option 3) becomes worthwhile
for visibility and retries; it would call the same CLI/library functions.

## Revisit when
Non-engineers need to trigger reprocessing, or run visibility through the CLI becomes a bottleneck.
