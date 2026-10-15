# Architecture Decision Records

Each significant design decision is recorded with its alternatives and tradeoffs, so it can be
revisited deliberately when its assumptions stop holding.

| ADR | Title | Status |
|---|---|---|
| [001](ADR-001-cli-for-pipeline-control.md) | Pipeline control through a CLI, not the API | Accepted |
| [002](ADR-002-late-data-via-re-resolution.md) | Late data handled by re-resolving held rows | Accepted |
| [003](ADR-003-full-gold-rebuild.md) | Full gold rebuild in the prototype | Accepted |
| [004](ADR-004-no-manual-dlq-overrides.md) | No manual DLQ overrides in the prototype | Accepted |
| [005](ADR-005-table-format.md) | One DuckDB database locally, Iceberg on AWS (revised) | Accepted |
| [006](ADR-006-entity-resolution-registry.md) | Entity resolution links identifiers only with registry evidence | Accepted |
| [007](ADR-007-validation-sql-rules.md) | Validation rules as data, compiled to SQL | Accepted |
| [008](ADR-008-data-realism.md) | Model lab result status and proteomics QC / control rows | Accepted |
| [009](ADR-009-transforms-python-sql.md) | Transforms in plain Python + DuckDB SQL, not dbt | Accepted |
| [010](ADR-010-pluggable-source-adapters.md) | Pluggable source adapters (reader → shape → mapping) | Accepted |

## Template

```markdown
# ADR-NNN: Title

Status: Proposed | Accepted | Superseded by ADR-XXX

## Context
What problem forces this decision; relevant constraints.

## Options
1. ... (include the simplest viable option)
2. ...

## Decision
What we chose.

## Tradeoffs
What we gain. What we give up.

## Local vs AWS
What changes in the cloud.

## At scale
Does it hold at 100x volume / many more sources? What breaks first?

## Revisit when
The signal that should make us reopen this decision.
```
