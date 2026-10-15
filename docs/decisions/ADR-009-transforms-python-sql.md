# ADR-009: Transforms in plain Python + DuckDB SQL, not dbt

Status: Accepted

## Context
Transforms run from mapped source rows to canonical silver, and from silver to gold. The hardest logic
(file hashing, DLQ routing, identity resolution, versioning, replay) is procedural. Gold transforms are
SQL over silver.

## Options
1. **Plain Python orchestrating DuckDB SQL for all stages.**
2. dbt-duckdb for silver → gold models; Python for ingestion and procedural stages.

## Decision
Option 1.

## Tradeoffs
- Gain: one language and one codebase; every step can be walked through end to end; idempotency,
  versioning and DLQ logic sit in one place.
- Give up: no automatically generated model lineage graph, test framework or documentation site; we
  structure transforms ourselves.
- Option 2 rejected for now: the procedural core would stay in Python regardless, so dbt would add a
  second system to learn and explain without removing the hardest code.

## Local vs AWS
SQL transforms carry over to Athena or Spark SQL; Python orchestration runs in ECS tasks or Step
Functions.

## At scale
As gold grows and more analysts contribute models, dbt (or an equivalent) becomes worthwhile for the
silver → gold layer, with its lineage graph and tests. The ingestion core would remain as is.

## Revisit when
Gold models multiply, analysts need to contribute transforms, or model-level lineage documentation is
required.
