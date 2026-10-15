# ADR-010: Pluggable source adapters (reader → shape → mapping)

Status: Accepted

## Context
The four sources in the brief are CSV files in long format that differ only in column names. Real vendors
also differ in file format and in data shape:

- Clinical lab exports: CSV with many more columns (accession, LOINC, reference range, status).
- Proteomics vendors: nested JSON (a sample containing a list of assay results).
- Metabolomics vendors: tab-separated, often wide (one row per sample, one column per metabolite).

Supporting a new vendor should not require changing pipeline code.

## Options
1. **Three-layer adapter selected by the source contract:** a reader per file format, a shaping step
   (long / wide-to-long / nested-to-flat), then the existing column mapping. A registered Python adapter
   class as an escape hatch for formats configuration cannot express.
2. One hand-written Python adapter per vendor.
3. Require vendors to deliver one standard format.

## Decision
Option 1.

## Tradeoffs
- Gain: most new vendors need configuration only; format and shape handling is written once and tested
  once; DuckDB reads CSV, TSV and JSON natively and supports `UNPIVOT`, so the layers are thin. Everything
  after the mapping step is unchanged, so validation, identity, versioning and lineage apply uniformly.
- Give up: the contract format grows (format, layout, record path, wide-column rules); a genuinely unusual
  format still needs code, though only an adapter class.
- Option 2 rejected: per-vendor code is the cost that grows linearly with the number of sources.
- Option 3 rejected: the platform rarely controls vendor formats; a standard can be encouraged, not assumed.

## Implementation scope
Flexibility is the design goal; not every reader is built in the prototype.

- **Built:** the reader registry selected by the contract's `format` (CSV and TSV readers), and the
  column mapping. Every source so far is long format; the week demo onboards three more labs with
  configuration only.
- **Designed, not built:** the shape step (`layout: wide` / `nested`) and a wide, tab-separated
  metabolomics source as its end-to-end proof (a new shape and a new kind of data at once), a nested JSON
  proteomics reader, and a richer clinical-lab CSV contract. The contract already carries a `layout`
  field (default `long`) as the hook.

## Local vs AWS
Same adapters. On AWS, readers take S3 paths; very large files are split into chunks before shaping.

## At scale
The reader/shape/mapping split is what makes self-service onboarding possible (DESIGN §10.3): a source
owner supplies a contract and a sample file, and CI validates it. Lineage keeps the original position of
each value (row, and for wide or nested files the column or path), so provenance survives reshaping.

## Revisit when
Several sources need custom adapter classes, which would indicate a missing reader or shape type.
