# Configuration

Versioned inputs that drive the pipeline ([docs/DATA_MODEL.md](../docs/DATA_MODEL.md) §6):

- `sources/` — one contract per source and version (`<source>_v<N>.yaml`)
- `reference/` — analytes, aliases, unit conversions, value ranges
- `rules/` — validation rules per source ([ADR-007](../docs/decisions/ADR-007-validation-sql-rules.md))
- `registry/` — enrollment registry ([ADR-006](../docs/decisions/ADR-006-entity-resolution-registry.md))
