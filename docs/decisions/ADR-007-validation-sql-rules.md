# ADR-007: Validation rules as data, compiled to SQL

Status: Accepted

## Context
Validation runs on every row, so it is among the first stages to feel volume. It is also a component that
must move to a distributed engine on AWS. How rules are expressed determines whether that move is a
rewrite or a swap of the executor.

## Options
1. **Rules declared in YAML per source; compiled to SQL and executed by DuckDB.**
2. pandera for file-level contracts, SQL for row-level rules.
3. Pydantic model per row.

## Decision
Option 1.

## Tradeoffs
- Gain: no additional dependency; set-based (whole-table) execution; the same rule files can compile to
  Spark SQL or be translated to AWS Glue Data Quality (DQDL, built on Deequ). One mechanism for all checks.
  Rules are reviewable data with their own version recorded on every DLQ row.
- Give up: we build the rule-to-SQL compiler and the error report (on the order of a hundred lines)
  instead of using pandera's declarative schemas and failure-case reports.
- Option 2 rejected: two mechanisms to explain and maintain; the pandera part does not carry over to Glue
  Data Quality.
- Option 3 rejected: row-by-row Python is too slow at millions of rows; Pydantic remains appropriate for
  API request and response models.

## Local vs AWS
Same rule files. Locally: DuckDB SQL. AWS: Spark SQL on Glue/EMR, or DQDL rules in Glue Data Quality.

## At scale
Holds. Rules that need history (distribution drift, volume checks) run as batch checks after the per-row
rules, over partition-level aggregates rather than full history.

## Revisit when
Rules appear that cannot be expressed as SQL predicates, or the team standardizes on a data quality tool.
