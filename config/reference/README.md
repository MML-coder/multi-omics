# Reference data

| File | Purpose |
|---|---|
| `analytes.csv` | Canonical analytes |
| `analyte_aliases.csv` | Source code → analyte, assay, measurement scale, canonical unit |
| `unit_conversions.csv` | Multiplicative conversions between units (identities included) |
| `unit_aliases.csv` | Alternative spellings of a unit as delivered (e.g. `beats/min` → `bpm`) |
| `value_ranges.csv` | Hard (reject) and soft (warn) ranges in the canonical unit |

**Value ranges are illustrative**, chosen to demonstrate the validation mechanism. They must be reviewed by
a domain expert before any real use.
